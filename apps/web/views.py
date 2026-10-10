import json
import logging
import io
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from functools import wraps

from django.conf import settings
from django.core.paginator import Paginator
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.billing import referrals
from apps.billing import services as billing
from apps.billing.models import BonusCredit, Plan, Referral
from apps.content import media, publishing
from apps.content import sources as source_tools
from apps.content.models import AccountCategory, Category, FollowedSource, GenerationRun, Post
from apps.content.scheduling import create_manual_post
from apps.content.tasks import enqueue, generate_post_task
from apps.social import crypto, oauth
from apps.social.models import SocialAccount
from apps.social.publishers import PublishError, get_publisher, implemented_platforms
from apps.workspaces.models import BrandKit, Member, Workspace

from . import forms

log = logging.getLogger(__name__)
User = get_user_model()


# ------------------------------------------------------------------ decorators
def workspace_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login

            return redirect_to_login(request.get_full_path())
        if request.workspace is None:
            messages.error(request, "Your user has no workspace. Ask an admin to add you.")
            return redirect("login")
        if request.method == "POST" and not request.member.can_edit:
            messages.error(request, "You have view-only access.")
            return redirect("dashboard")
        return view(request, *args, **kwargs)

    return wrapper


# ------------------------------------------------------------------ auth
def signup(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.GET.get("ref"):
        request.session["ref"] = request.GET["ref"][:12]
    form = forms.SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            user = User.objects.create_user(username=data["email"], email=data["email"], password=data["password"])
            ws = Workspace.objects.create(name=data["name"], owner=user, country=data["country"], timezone=data["timezone"])
            Member.objects.create(workspace=ws, user=user, role="owner")
            BrandKit.objects.create(workspace=ws)
            billing.start_unpaid(ws)
            referrals.attach(ws, request.session.pop("ref", ""))
        login(request, user)
        messages.success(request, "Welcome! Choose a plan to start posting.")
        return redirect("billing")
    return render(request, "web/signup.html", {"form": form})


def manifest(request):
    from django.templatetags.static import static

    data = {
        "name": "PostPilot", "short_name": "PostPilot", "description": "Posts that write and send themselves.",
        "start_url": "/", "scope": "/", "display": "standalone", "background_color": "#eef1ef", "theme_color": "#0f4c55",
        "icons": [
            {"src": static("web/icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static("web/icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": static("web/icons/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return HttpResponse(json.dumps(data), content_type="application/manifest+json")


def health(request):
    return HttpResponse("ok")


# ------------------------------------------------------------------ public pages
def home(request):
    """Marketing page for visitors, dashboard for signed-in users."""
    if request.user.is_authenticated:
        return dashboard(request)
    return render(request, "web/landing.html", {
        "plans": Plan.objects.filter(is_public=True),
        "categories": Category.objects.filter(is_active=True),
        "platforms": [label for _, label in SocialAccount.PLATFORMS],
        "themes": BrandKit.CARD_THEMES,
    })


def card_preview(request):
    """A sample image card in a theme and colours. Public: it only draws fixed sample text."""
    theme = request.GET.get("theme", "gradient")
    if theme not in dict(BrandKit.CARD_THEMES):
        theme = "gradient"
    fake = SimpleNamespace(
        primary_color=request.GET.get("primary", "#4F46E5"), secondary_color=request.GET.get("secondary", "#111827"),
        watermark="@yourbrand", card_theme=theme,
    )
    for attr in ("primary_color", "secondary_color"):
        if not forms.HEX.match(getattr(fake, attr)):
            setattr(fake, attr, "#4F46E5" if attr == "primary_color" else "#111827")
    img = media.draw_card(text="Start before you feel ready", category_name="Motivation", brand=fake, size=media.SQUARE)
    img = img.resize((540, 540))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    resp = HttpResponse(buf.getvalue(), content_type="image/png")
    resp["Cache-Control"] = "public, max-age=3600"
    return resp


# ------------------------------------------------------------------ dashboard
def _impact(ws, posts):
    tz = ws.tz
    published = posts.filter(status="published")
    total = published.count()
    days = {p.astimezone(tz).date() for p in published.filter(published_at__gte=timezone.now() - timedelta(days=90)).values_list("published_at", flat=True) if p}
    today = timezone.now().astimezone(tz).date()
    cursor = today if today in days else today - timedelta(days=1)
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    top = published.values("category__name").annotate(n=Count("id")).order_by("-n").first()
    return {"total": total, "streak": streak, "hours_saved": round(total * 0.25, 1), "top_category": top["category__name"] if top else ""}


def _checklist(ws, posts):
    brand = ws.brand
    customised = bool(brand.cta or brand.watermark or brand.hashtags or brand.card_theme != "gradient"
                      or brand.primary_color.lower() != "#4f46e5" or brand.secondary_color.lower() != "#111827")
    steps = [
        ("Choose a plan", ws.subscription.is_live(), "billing"),
        ("Connect your first account", ws.accounts.exists(), "accounts"),
        ("Pick categories for it", AccountCategory.objects.filter(account__workspace=ws).exists(), "accounts"),
        ("Make it yours: colours, card style, voice", customised, "brand"),
        ("Generate your first post", posts.exclude(status="generating").exists(), "accounts"),
        ("Get your first post published", posts.filter(status="published").exists(), "queue"),
    ]
    items = [{"label": l, "done": d, "url": u} for l, d, u in steps]
    return {"items": items, "done": sum(i["done"] for i in items), "total": len(items), "next": next((i for i in items if not i["done"]), None)}


@workspace_required
def calendar(request):
    ws = request.workspace
    tz = ws.tz
    today = timezone.now().astimezone(tz).date()
    try:
        start = date.fromisoformat(request.GET.get("start", ""))
    except ValueError:
        start = today
    end = start + timedelta(days=14)
    lo = datetime.combine(start, datetime.min.time(), tzinfo=tz)
    hi = datetime.combine(end, datetime.min.time(), tzinfo=tz)
    qs = Post.objects.filter(workspace=ws, scheduled_at__gte=lo, scheduled_at__lt=hi).exclude(status="rejected").select_related("account", "category").order_by("scheduled_at")
    by_day = {start + timedelta(days=i): [] for i in range(14)}
    for post in qs:
        by_day[post.scheduled_at.astimezone(tz).date()].append({"post": post, "local": post.scheduled_at.astimezone(tz)})
    return render(request, "web/calendar.html", {
        "days": [{"date": d, "is_today": d == today, "items": items} for d, items in by_day.items()],
        "prev": start - timedelta(days=14), "next": end, "tz": ws.timezone, "start": start,
    })


def _engagement(m):
    return m.likes + m.comments + m.shares


@workspace_required
def analytics(request):
    ws = request.workspace
    since = timezone.now() - timedelta(days=30)
    posts = list(Post.objects.filter(workspace=ws, status="published", published_at__gte=since).select_related("account", "category"))
    rows = []
    for post in posts:
        m = post.metrics.order_by("-fetched_at").first()
        if m:
            rows.append((post, m))

    def group(key_fn):
        agg = {}
        for post, m in rows:
            a = agg.setdefault(key_fn(post), {"posts": 0, "reach": 0, "engagement": 0})
            a["posts"] += 1
            a["reach"] += m.reach
            a["engagement"] += _engagement(m)
        out = [{"name": k, **v, "avg": round(v["engagement"] / v["posts"], 1)} for k, v in agg.items()]
        top = max((r["engagement"] for r in out), default=0) or 1
        for r in out:
            r["pct"] = int(r["engagement"] * 100 / top)
        return sorted(out, key=lambda r: -r["engagement"])

    hours = {}
    for post, m in rows:
        hours.setdefault(post.published_at.astimezone(ws.tz).hour, []).append(_engagement(m))
    best = sorted(({"hour": f"{h:02d}:00", "avg": round(sum(v) / len(v), 1), "posts": len(v)} for h, v in hours.items()), key=lambda r: -r["avg"])[:3]
    top_posts = sorted(rows, key=lambda r: -_engagement(r[1]))[:5]
    no_metrics = sorted({p.account.get_platform_display() for p in posts} - {p.account.get_platform_display() for p, _ in rows})
    return render(request, "web/analytics.html", {
        "totals": {"posts": len(posts), "tracked": len(rows), "reach": sum(m.reach for _, m in rows),
                   "engagement": sum(_engagement(m) for _, m in rows)},
        "by_category": group(lambda p: p.category.name), "by_platform": group(lambda p: p.account.get_platform_display()),
        "best_hours": best, "top_posts": [{"post": p, "m": m, "eng": _engagement(m)} for p, m in top_posts], "no_metrics": no_metrics,
    })


@workspace_required
def invite(request):
    ws = request.workspace
    link = f"{settings.PUBLIC_BASE_URL.rstrip('/')}/signup/?ref={ws.referral_code}"
    made = Referral.objects.filter(referrer=ws)
    return render(request, "web/invite.html", {
        "link": link, "signed_up": made.count(), "paid": made.filter(rewarded_at__isnull=False).count(),
        "bonuses": BonusCredit.objects.filter(workspace=ws, expires_at__gt=timezone.now()),
        "bonus_posts": settings.REFERRAL_BONUS_POSTS, "bonus_days": settings.REFERRAL_BONUS_DAYS, "max_rewards": settings.REFERRAL_MAX_REWARDS,
        "referred": ws.referred_by_id is not None,
    })


def _timeline(ws, now):
    """Today and tomorrow in the workspace's own timezone, grouped by day, oldest first."""
    tz = ws.tz
    today = now.astimezone(tz).date()
    lo = datetime.combine(today, datetime.min.time(), tzinfo=tz)
    qs = (Post.objects.filter(workspace=ws, scheduled_at__gte=lo, scheduled_at__lt=lo + timedelta(days=2))
          .exclude(status__in=["rejected", "generating"]).select_related("account", "category").order_by("scheduled_at")[:40])
    groups = {}
    for post in qs:
        local = post.scheduled_at.astimezone(tz)
        label = "Today" if local.date() == today else "Tomorrow"
        groups.setdefault(label, {"label": label, "date": local.date(), "items": []})["items"].append({"post": post, "local": local})
    return list(groups.values())


@workspace_required
def dashboard(request):
    ws = request.workspace
    posts = Post.objects.filter(workspace=ws)
    now = timezone.now()
    ctx = {
        "waiting": posts.filter(status__in=["pending_approval", "needs_review"]).count(),
        "queued": posts.filter(status="queued").count(),
        "published_30": posts.filter(status="published", published_at__gte=now - timedelta(days=30)).count(),
        "failed": posts.filter(status="failed", created_at__gte=now - timedelta(days=7)).count(),
        "timeline": _timeline(ws, now),
        "today_label": now.astimezone(ws.tz),
        "recent": posts.exclude(status="generating").select_related("account", "category").order_by("-updated_at")[:8],
        "accounts": ws.accounts.all(),
        "used": billing.posts_used(ws),
        "limit": billing.post_limit(ws),
        "ai_cost": GenerationRun.objects.filter(post__workspace=ws, created_at__gte=billing.month_start()).aggregate(c=Sum("cost_usd"))["c"] or 0,
        "ai_dry_run": settings.AI_DRY_RUN,
        "is_live": ws.subscription.is_live(),
        "impact": _impact(ws, posts),
        "checklist": _checklist(ws, posts),
    }
    return render(request, "web/dashboard.html", ctx)


# ------------------------------------------------------------------ accounts
@workspace_required
def accounts(request):
    ws = request.workspace
    connected_platforms = set(ws.accounts.values_list("platform", flat=True))
    accounts_by_platform = {account.platform: account for account in ws.accounts.all()}
    items = []
    for code, label in SocialAccount.PLATFORMS:
        provider = oauth.PLATFORM_PROVIDER.get(code)
        items.append({
            "code": code, "label": label,
            "connected": code in connected_platforms,
            "account": accounts_by_platform.get(code),
            "manual": code == "telegram",
            "oauth_ready": bool(provider and oauth.configured(provider)),
            "provider": provider,
            "can_paste": code in ("facebook", "instagram"),
        })
    return render(request, "web/accounts.html", {
        "accounts": ws.accounts.annotate(n_cats=Count("subscriptions")),
        "platforms": items,
        "is_live": ws.subscription.is_live(),
    })


@workspace_required
def connect(request, platform):
    """Manual (paste a token) connection: Telegram, plus a fallback for Facebook and Instagram."""
    if platform not in forms.CONNECT_FORMS:
        return redirect("accounts")
    if request.workspace.accounts.filter(platform=platform).exists():
        messages.info(request, f"A {dict(SocialAccount.PLATFORMS)[platform]} account is already connected. Disconnect it before adding another.")
        return redirect("accounts")
    ok, reason = billing.can_add_account(request.workspace)
    if not ok:
        messages.error(request, reason)
        return redirect("billing")
    form = forms.CONNECT_FORMS[platform](request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        account = SocialAccount(workspace=request.workspace, platform=platform, display_name=d["display_name"])
        if platform == "telegram":
            account.token, account.handle, account.extra = d["bot_token"], d["chat_id"], {"chat_id": d["chat_id"]}
        elif platform == "facebook":
            account.token, account.extra = d["page_token"], {"page_id": d["page_id"]}
        else:
            account.token, account.handle, account.extra = d["token"], d.get("handle", ""), {"ig_user_id": d["ig_user_id"]}
        try:
            get_publisher(platform).check(account)
        except PublishError as exc:
            form.add_error(None, f"Could not verify the connection: {exc}")
        else:
            account.last_checked_at = timezone.now()
            try:
                with transaction.atomic():
                    account.save()
            except IntegrityError:
                messages.info(request, f"A {account.get_platform_display()} account was connected in another request. Disconnect it before adding another.")
                return redirect("accounts")
            messages.success(request, f"{account.display_name} connected.")
            return redirect("account_detail", pk=account.pk)
    return render(request, "web/connect.html", {"form": form, "platform": platform, "label": dict(SocialAccount.PLATFORMS)[platform]})


# ------------------------------------------------------------------ OAuth connect
@workspace_required
def oauth_start(request, provider):
    ok, reason = billing.can_add_account(request.workspace)
    if not ok:
        messages.error(request, reason)
        return redirect("billing")
    try:
        return redirect(oauth.start(provider, request.session))
    except oauth.OAuthError as exc:
        messages.error(request, str(exc))
        return redirect("accounts")


@workspace_required
def oauth_callback(request, provider):
    try:
        token_response = oauth.finish(provider, request.session, request.GET)
        candidates = oauth.discover(provider, token_response)
    except oauth.OAuthError as exc:
        messages.error(request, str(exc))
        return redirect("accounts")

    if len(candidates) == 1:
        return _save_candidates(request, candidates)

    # several Pages / Instagram accounts: let the user choose. Tokens stay encrypted inside the session.
    stored = []
    for c in candidates:
        c = dict(c)
        c["access_token"] = crypto.encrypt(c["access_token"])
        stored.append(c)
    request.session["oauth_candidates"] = stored
    return redirect("oauth_select")


def _save_candidates(request, candidates):
    ws = request.workspace
    saved = []
    for cand in candidates:
        if not _fits(ws, cand):
            existing = ws.accounts.filter(platform=cand["platform"]).first()
            if existing:
                messages.info(request, f"A {existing.get_platform_display()} account is already connected. Disconnect it before choosing another.")
            else:
                messages.error(request, billing.can_add_account(ws)[1] or "Plan limit reached")
            break
        try:
            _, created = oauth.save_candidate(ws, cand)
        except oauth.OAuthError as exc:
            messages.info(request, str(exc))
            break
        except IntegrityError:
            messages.info(request, f"A {dict(SocialAccount.PLATFORMS)[cand['platform']]} account was connected in another request. Disconnect it before adding another.")
            break
        saved.append(cand["display_name"])
    if saved:
        messages.success(request, "Connected: " + ", ".join(saved))
    return redirect("accounts")


def _fits(ws, cand):
    """Allow reconnecting the same platform identity, but never adding a second one."""
    account = ws.accounts.filter(platform=cand["platform"]).first()
    if account:
        return any(
            account.extra.get(key) == value
            for key, value in cand["extra"].items()
            if key in oauth.EXTERNAL_KEYS
        )
    return billing.can_add_account(ws)[0]


@workspace_required
def oauth_select(request):
    stored = request.session.get("oauth_candidates")
    if not stored:
        return redirect("accounts")
    if request.method == "POST":
        chosen = set(request.POST.getlist("key"))
        picks = []
        for c in stored:
            if c["key"] in chosen:
                c = dict(c)
                c["access_token"] = crypto.decrypt(c["access_token"])
                picks.append(c)
        request.session.pop("oauth_candidates", None)
        if not picks:
            messages.info(request, "Nothing selected.")
            return redirect("accounts")
        return _save_candidates(request, picks)
    return render(request, "web/oauth_select.html", {"candidates": stored})


@workspace_required
def account_detail(request, pk):
    account = get_object_or_404(SocialAccount, pk=pk, workspace=request.workspace)
    used = set(account.subscriptions.filter(followed_source__isnull=True).values_list("category_id", flat=True))
    followed_used = set(account.subscriptions.filter(followed_source__isnull=False).values_list("followed_source_id", flat=True))
    return render(request, "web/account_detail.html", {
        "account": account,
        "subs": account.subscriptions.select_related("category", "followed_source"),
        "available": Category.objects.filter(is_active=True).exclude(pk__in=used).exclude(source_type="followed"),
        "follow_category": Category.objects.filter(is_active=True, source_type="followed").first(),
        "follow_choices": request.workspace.followed_sources.filter(enabled=True).exclude(pk__in=followed_used),
    })


@workspace_required
@require_POST
def account_action(request, pk, action):
    account = get_object_or_404(SocialAccount, pk=pk, workspace=request.workspace)
    if action == "check":
        healthy = publishing.check_account(account)
        (messages.success if healthy else messages.error)(request, "Connection is healthy." if healthy else f"Connection problem: {account.last_error}")
    elif action == "delete":
        account.delete()  # tokens go with the row
        messages.success(request, "Account disconnected and its stored token deleted.")
        return redirect("accounts")
    return redirect("account_detail", pk=pk)


# ------------------------------------------------------------------ followed sources
@workspace_required
def sources_page(request):
    ws = request.workspace
    form = forms.FollowSourceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if ws.followed_sources.count() >= settings.MAX_FOLLOWED_SOURCES:
            messages.error(request, f"You can follow up to {settings.MAX_FOLLOWED_SOURCES} sources.")
        else:
            try:
                kind, name, feed_url, _ = source_tools.resolve(form.cleaned_data["url"], form.cleaned_data["kind"])
            except source_tools.SourceError as exc:
                form.add_error("url", str(exc))
            else:
                src, created = FollowedSource.objects.get_or_create(
                    workspace=ws, feed_url=feed_url, defaults={"kind": kind, "name": name[:160], "site_url": form.cleaned_data["url"][:600]},
                )
                source_tools.refresh(src)
                messages.success(request, f"Now following {src.name}. Add it to an account from the Accounts page." if created else "You already follow that.")
                return redirect("sources")
    return render(request, "web/sources.html", {
        "form": form,
        "sources": ws.followed_sources.annotate(n_items=Count("items", distinct=True), n_accounts=Count("subscriptions", distinct=True)),
    })


@workspace_required
@require_POST
def source_action(request, pk, action):
    src = get_object_or_404(FollowedSource, pk=pk, workspace=request.workspace)
    if action == "delete":
        src.delete()
        messages.success(request, "Stopped following. Its category settings were removed from your accounts.")
    elif action == "refresh":
        n = source_tools.refresh(src)
        (messages.error if src.last_error else messages.success)(request, src.last_error or f"Checked. {n} new items.")
    return redirect("sources")


# ------------------------------------------------------------------ category subscriptions
@workspace_required
@require_POST
def subscription_add(request, account_pk):
    account = get_object_or_404(SocialAccount, pk=account_pk, workspace=request.workspace)
    category = get_object_or_404(Category, pk=request.POST.get("category"), is_active=True)
    source = None
    if category.source_type == "followed":
        source = get_object_or_404(FollowedSource, pk=request.POST.get("source"), workspace=request.workspace)
    sub, _ = AccountCategory.objects.get_or_create(
        account=account, category=category, followed_source=source,
        defaults={"post_times": ["09:00"], "publish_mode": category.default_mode, "region": request.workspace.country},
    )
    return redirect("subscription_edit", pk=sub.pk)


@workspace_required
def subscription_edit(request, pk):
    sub = get_object_or_404(AccountCategory, pk=pk, account__workspace=request.workspace)
    form = forms.SubscriptionForm(request.POST or None, instance=sub)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Schedule saved.")
        return redirect("account_detail", pk=sub.account_id)
    from apps.content import learning

    best = [f"{h:02d}:00" for h in learning.ranked_hours(sub.account)[:3]]
    return render(request, "web/subscription_edit.html", {"form": form, "sub": sub, "best_hours": best})


@workspace_required
@require_POST
def subscription_delete(request, pk):
    sub = get_object_or_404(AccountCategory, pk=pk, account__workspace=request.workspace)
    account_id = sub.account_id
    sub.delete()
    return redirect("account_detail", pk=account_id)


@workspace_required
@require_POST
def subscription_generate(request, pk):
    sub = get_object_or_404(AccountCategory, pk=pk, account__workspace=request.workspace)
    ok, reason = billing.can_generate(request.workspace)
    if not ok:
        messages.error(request, reason)
        if not request.workspace.subscription.is_live():
            return redirect("billing")
        return redirect("account_detail", pk=sub.account_id)
    idea = request.POST.get("idea", "").strip()
    if idea and len(idea) < 5:
        messages.error(request, "Describe your topic in a few words, for example: tips for first-time home buyers.")
        return redirect("account_detail", pk=sub.account_id)
    post = create_manual_post(sub, idea=idea)
    enqueue(generate_post_task, post.pk)
    messages.success(request, "Generating a post. It will appear in the approval queue or schedule shortly.")
    return redirect("queue")


# ------------------------------------------------------------------ posts and approval
@workspace_required
def queue(request):
    posts = Post.objects.filter(
        workspace=request.workspace, status__in=["pending_approval", "needs_review", "draft", "generating"]
    ).select_related("account", "category").order_by("scheduled_at")
    return render(request, "web/queue.html", {"posts": posts})


@workspace_required
def posts(request):
    status = request.GET.get("status", "")
    qs = Post.objects.filter(workspace=request.workspace).select_related("account", "category")
    if status:
        qs = qs.filter(status=status)
    page_obj = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "web/posts.html", {
        "posts": page_obj.object_list,
        "page_obj": page_obj,
        "status": status,
        "statuses": Post.STATUSES,
    })


@workspace_required
def post_detail(request, pk):
    post = get_object_or_404(Post.objects.select_related("account", "category", "workspace__brand"), pk=pk, workspace=request.workspace)
    editable = post.status in ("pending_approval", "needs_review", "draft", "queued")
    form = forms.PostEditForm(request.POST or None, initial={"caption": post.caption, "hashtags": " ".join(post.hashtags)})
    if request.method == "POST" and editable:
        action = request.POST.get("action")
        if action == "save" and form.is_valid():
            post.caption = form.cleaned_data["caption"].strip()
            post.hashtags = form.cleaned_data["hashtags"].split()
            post.save(update_fields=["caption", "hashtags", "updated_at"])
            messages.success(request, "Saved.")
        elif action == "approve":
            if form.is_valid():
                post.caption = form.cleaned_data["caption"].strip()
                post.hashtags = form.cleaned_data["hashtags"].split()
                post.save(update_fields=["caption", "hashtags", "updated_at"])
            if publishing.approve(post):
                messages.success(request, "Approved. It will publish at the scheduled time.")
                return redirect("queue")
            messages.error(request, "This post cannot be approved (dry-run posts are for testing only).")
        elif action == "reject":
            post.status = "rejected"
            post.save(update_fields=["status", "updated_at"])
            messages.info(request, "Rejected.")
            return redirect("queue")
        elif action == "revise":
            from apps.content import pipeline

            try:
                pipeline.revise(post, request.POST.get("instruction", ""))
                messages.success(request, "Rewritten. Read it, then approve or rewrite again.")
            except pipeline.ReviseError as exc:
                messages.error(request, str(exc))
        elif action == "regenerate" and post.subscription:
            ok, reason = billing.can_generate(request.workspace)
            if not ok:
                messages.error(request, reason)
            else:
                post.status = "rejected"
                post.save(update_fields=["status", "updated_at"])
                new = create_manual_post(post.subscription)
                enqueue(generate_post_task, new.pk)
                messages.success(request, "Generating a new version.")
                return redirect("queue")
        return redirect("post_detail", pk=pk)
    return render(request, "web/post_detail.html", {"post": post, "form": form, "editable": editable, "runs": post.runs.all()})


# ------------------------------------------------------------------ brand
@workspace_required
def brand(request):
    ws = request.workspace
    kit, _ = BrandKit.objects.get_or_create(workspace=ws)
    form = forms.BrandKitForm(request.POST or None, instance=kit, initial={"country": ws.country, "timezone": ws.timezone})
    if request.method == "POST" and form.is_valid():
        form.save()
        ws.country, ws.timezone = form.cleaned_data["country"], form.cleaned_data["timezone"]
        ws.save(update_fields=["country", "timezone"])
        messages.success(request, "Saved. New posts will use it.")
        return redirect("brand")
    return render(request, "web/brand.html", {"form": form})


# ------------------------------------------------------------------ billing
@workspace_required
def billing_page(request):
    ws = request.workspace
    sub = ws.subscription
    currencies = billing.available_currencies()
    currency = request.GET.get("currency") if request.GET.get("currency") in ("USD", "INR") else (currencies[0] if currencies else "USD")
    return render(request, "web/billing.html", {
        "sub": sub,
        "plans": Plan.objects.filter(is_public=True),
        "used": billing.posts_used(ws),
        "limit": billing.post_limit(ws),
        "currencies": currencies,
        "currency": currency,
        "can_pay": bool(currencies),
        "debug": settings.DEBUG,
        "paid_now": request.GET.get("paid") == "1",
    })


@workspace_required
@require_POST
def billing_subscribe(request, code):
    plan = get_object_or_404(Plan, code=code, is_public=True)
    currency = request.POST.get("currency", "USD")
    if currency not in billing.available_currencies():
        messages.error(request, "That payment currency is not available yet.")
        return redirect("billing")
    try:
        url = billing.create_checkout(request.workspace, plan, currency)
    except Exception as exc:
        log.warning("Checkout failed: %s", exc)
        messages.error(request, f"Could not start checkout: {exc}")
        return redirect("billing")
    return redirect(url)


@workspace_required
@require_POST
def billing_dev_activate(request, code):
    """Local development only: pretend a payment succeeded."""
    if not settings.DEBUG:
        return HttpResponseBadRequest("Disabled")
    sub = request.workspace.subscription
    sub.plan = get_object_or_404(Plan, code=code)
    sub.save()
    billing._mark(sub, "active")
    messages.success(request, f"[dev] {sub.plan.name} activated without payment.")
    return redirect("billing")


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    if not billing.verify_razorpay_signature(request.body, request.headers.get("X-Razorpay-Signature", "")):
        return HttpResponse(status=400)
    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)
    billing.handle_razorpay_event(event)
    return HttpResponse("ok")


@csrf_exempt
@require_POST
def stripe_webhook(request):
    try:
        event = billing.construct_stripe_event(request.body, request.headers.get("Stripe-Signature", ""))
    except ValueError:
        return HttpResponse(status=400)
    billing.handle_stripe_event(event)
    return HttpResponse("ok")
