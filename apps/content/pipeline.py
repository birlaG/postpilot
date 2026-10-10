"""The generation pipeline: triage -> write -> static checks -> review -> media -> route.

run_generation(post_id) fills a Post row that was created in status 'generating'. Creating the
row first is what claims the slot, so two workers can never generate the same slot twice.
"""
import logging
import random
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing import services as billing
from apps.social.limits import limits_for, resolve_format

from . import ai, media, safety, sources, trends
from .models import GenerationRun, Post, TrendItem

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ source selection
def _used_urls(account, category):
    return set(
        Post.objects.filter(account=account, category=category)
        .exclude(source_url="")
        .order_by("-created_at")
        .values_list("source_url", flat=True)[:200]
    )


def pick_source(post):
    """Return (item_dict, usage). item_dict has title/summary/url, or None when nothing is available."""
    category, account = post.category, post.account
    if post.idea:  # the user told us what to write about; no fetching and no triage
        return {"title": post.idea, "summary": "", "url": "", "from_user": True}, None
    if category.source_type == "evergreen":
        topics = category.source_config.get("topics") or [category.name]
        recent = set(
            Post.objects.filter(account=account, category=category)
            .order_by("-created_at")
            .values_list("source_title", flat=True)[: max(len(topics) // 2, 1)]
        )
        fresh = [t for t in topics if t not in recent] or topics
        chosen = random.Random(f"{post.pk}-{timezone.now():%Y%m%d}").choice(fresh)
        return {"title": chosen, "summary": "", "url": ""}, None

    sub = post.subscription
    if sub and sub.followed_source_id:
        return _pick_followed(post, sub, account, category)

    region = sub.region if sub else "US"
    cutoff = timezone.now() - timedelta(hours=category.freshness_hours)
    used = _used_urls(account, category)

    def _candidates():
        qs = TrendItem.objects.filter(category=category, region=region, fetched_at__gte=cutoff)
        return [t for t in qs[:40] if t.url not in used][:8]

    candidates = _candidates()
    if not candidates:  # first post for a new region: fetch once now instead of waiting for the hourly job
        trends.ingest_category(category, region)
        candidates = _candidates()
    if not candidates:
        return None, None
    options = [{"id": t.pk, "title": t.title, "summary": t.summary[:300]} for t in candidates]
    pick_id, usage = ai.triage(options, category, post.workspace.brand)
    chosen = next(t for t in candidates if t.pk == pick_id)
    return {"title": chosen.title, "summary": chosen.summary, "url": chosen.url}, usage


def _pick_followed(post, sub, account, category):
    """Newest episode or video of a followed source that this account has not posted about yet."""
    source = sub.followed_source
    used = _used_urls(account, category)

    def _candidates():
        return [t for t in TrendItem.objects.filter(source=source)[:30] if t.url not in used][:6]

    candidates = _candidates()
    if not candidates:
        sources.refresh(source)
        candidates = _candidates()
    if not candidates:
        return None, None
    options = [{"id": t.pk, "title": t.title, "summary": t.summary[:300]} for t in candidates]
    pick_id, usage = ai.triage(options, category, post.workspace.brand)
    chosen = next(t for t in candidates if t.pk == pick_id)
    return {"title": chosen.title, "summary": chosen.summary, "url": chosen.url, "from": source.name, "kind": source.get_kind_display()}, usage


# ------------------------------------------------------------------ helpers
def _log_run(post, stage, usage, prompt_version="", verdict=None):
    if usage:
        GenerationRun.objects.create(
            post=post, stage=stage, model=usage["model"], prompt_version=prompt_version,
            tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"], cost_usd=usage["cost_usd"],
            verdict=verdict or {},
        )


def _recent_captions(post, n=10):
    return [
        c[:90]
        for c in Post.objects.filter(account=post.account, category=post.category)
        .exclude(pk=post.pk)
        .exclude(caption="")
        .order_by("-created_at")
        .values_list("caption", flat=True)[:n]
    ]


def _fail(post, status, message):
    post.status, post.error = status, message[:500]
    post.save(update_fields=["status", "error", "updated_at"])
    return post


def _route(post, sub):
    """Decide where a finished post goes."""
    if post.dry_run:
        return "pending_approval"  # visible for testing, never publishable
    mode = sub.effective_mode if sub else "approve"
    return {"auto": "queued", "approve": "pending_approval", "draft": "draft"}[mode]


# ------------------------------------------------------------------ main entry
def run_generation(post_id: int) -> Post:
    post = Post.objects.select_related("workspace__brand", "account", "category", "subscription").get(pk=post_id)
    if post.status != "generating":
        return post  # already handled (idempotent for retries)

    ok, reason = billing.can_generate(post.workspace)
    if not ok:
        return _fail(post, "failed", f"Not generated: {reason}")

    category, brand, account = post.category, post.workspace.brand, post.account
    limits = limits_for(account.platform)
    post.dry_run = settings.AI_DRY_RUN

    try:
        item, usage = pick_source(post)
        _log_run(post, "triage", usage)
        if item is None:
            return _fail(post, "failed", "No fresh source items for this category yet. Wait for the next trend fetch.")
        post.source_title, post.source_url = item["title"][:300], item["url"][:600]

        feedback, draft, review = None, None, {}
        recent = _recent_captions(post)
        custom = post.subscription.custom_topic if post.subscription else ""
        fmt = resolve_format(account.platform, post.subscription.content_format if post.subscription else "auto")
        style = post.subscription.post_style if post.subscription else "standard"
        language = (post.subscription.language if post.subscription else "") or brand.language
        for attempt in range(1, settings.AI_MAX_ATTEMPTS + 1):
            draft, usage = ai.write_post(
                category=category, brand=brand, platform=account.platform, limits=limits,
                item=item, recent=recent, feedback=feedback, custom_topic=custom, fmt=fmt, style=style, language=language,
            )
            _log_run(post, "write", usage, category.prompt_version)

            post.caption = str(draft.get("caption", "")).strip()
            post.hashtags = safety.clean_hashtags(draft.get("hashtags"), limits["max_hashtags"])
            post.image_text = str(draft.get("image_text", ""))[:300]
            post.alt_text = str(draft.get("alt_text", ""))[:300]
            post.content_hash = safety.content_hash(post.caption)

            problems = safety.static_checks(
                caption=post.caption, hashtags=post.hashtags, platform_limits=limits,
                brand=brand, full_text_len=len(post.full_text()),
            )
            if Post.objects.filter(account=account, content_hash=post.content_hash).exclude(pk=post.pk).exists():
                problems.append("Duplicate of an earlier post. Write something different.")
            if problems:
                feedback = problems
                continue

            review, usage = ai.review_post(category=category, draft=draft, item=item)
            _log_run(post, "review", usage, category.prompt_version, review)
            if str(review.get("verdict", "")).lower() == "pass":
                feedback = None
                break
            feedback = review.get("reasons") or ["Reviewer rejected the draft."]
        else:
            post.risk_flags = review.get("risk_flags", []) if review else []
            post.review_notes = "; ".join(str(f) for f in (feedback or []))[:1000]
            post.status = "needs_review"
            post.error = "Could not produce a passing draft after several attempts."
            post.save()
            billing.record_usage(post.workspace)
            return post

        post.risk_flags = review.get("risk_flags", [])
        post.review_notes = "; ".join(review.get("reasons", []))[:1000]
        card_text = post.image_text or post.source_title
        if fmt == "video" and not post.workspace.subscription.plan.allow_video:
            return _fail(post, "failed", "Short videos need the Pro plan or higher")
        if fmt in ("image", "video"):
            post.media_path = media.render_card(text=card_text, category_name=category.name, brand=brand, handle=account.handle)
        if fmt == "video":
            post.video_path = media.render_video(text=card_text, category_name=category.name, brand=brand, handle=account.handle)
        post.status = _route(post, post.subscription)
        post.error = ""
        with transaction.atomic():
            post.save()
            billing.record_usage(post.workspace)
        return post
    except media.MediaError as exc:
        return _fail(post, "needs_review", f"Media problem: {exc}")
    except ai.AIError as exc:
        log.warning("AI failure for post %s: %s", post_id, exc)
        return _fail(post, "failed", str(exc))
    except Exception as exc:  # never leave a slot stuck in 'generating'
        log.exception("Generation crashed for post %s", post_id)
        return _fail(post, "failed", f"Unexpected error: {exc}")


MAX_REVISIONS = 8


class ReviseError(Exception):
    """A message that is safe to show to the user."""


def revise(post: Post, instruction: str) -> Post:
    """Rewrite a post's caption on request. The new text must pass the same checks as a generated one."""
    instruction = (instruction or "").strip()
    if len(instruction) < 3:
        raise ReviseError("Tell Claude what to change, for example: make it shorter and funnier.")
    if post.status not in ("pending_approval", "needs_review", "draft"):
        raise ReviseError("Only posts waiting for review can be rewritten.")
    if post.runs.filter(stage="revise").count() >= MAX_REVISIONS:
        raise ReviseError(f"This post has been rewritten {MAX_REVISIONS} times. Edit the text by hand or write another post.")
    ok, reason = billing.can_generate(post.workspace)
    if not ok:
        raise ReviseError(reason)
    category, brand, account = post.category, post.workspace.brand, post.account
    limits = limits_for(account.platform)
    language = (post.subscription.language if post.subscription else "") or brand.language
    item = {"title": post.source_title, "summary": "", "url": post.source_url}
    post.dry_run = settings.AI_DRY_RUN if not post.dry_run else True
    try:
        draft, usage = ai.revise_post(category=category, brand=brand, platform=account.platform, limits=limits, caption=post.caption,
                                      hashtags=post.hashtags, instruction=instruction, language=language, item=item)
    except ai.AIError as exc:
        raise ReviseError(str(exc))
    _log_run(post, "revise", usage, category.prompt_version, {"instruction": instruction[:200]})
    caption = str(draft.get("caption", "")).strip()
    tags = safety.clean_hashtags(draft.get("hashtags") or post.hashtags, limits["max_hashtags"])
    old = (post.caption, post.hashtags)
    post.caption, post.hashtags = caption, tags
    problems = safety.static_checks(caption=caption, hashtags=tags, platform_limits=limits, brand=brand, full_text_len=len(post.full_text()))
    if problems:
        post.caption, post.hashtags = old
        raise ReviseError("The rewrite was not used: " + " ".join(problems))
    if not post.dry_run:
        review, usage = ai.review_post(category=category, draft={"caption": caption, "hashtags": tags}, item=item)
        _log_run(post, "review", usage, category.prompt_version, review)
        if str(review.get("verdict", "")).lower() != "pass":
            post.caption, post.hashtags = old
            raise ReviseError("The rewrite was not used because the reviewer flagged it: " + "; ".join(str(r) for r in review.get("reasons", []))[:200])
    post.content_hash = safety.content_hash(caption)
    post.save(update_fields=["caption", "hashtags", "content_hash", "updated_at"])
    return post
