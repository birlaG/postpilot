"""Publishing with idempotency, retries and account health handling (PRD section 8)."""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.models import Subscription
from apps.social.publishers import PublishError, get_publisher

from .models import Post, PostMetrics

log = logging.getLogger(__name__)


def due_post_ids(now=None):
    now = now or timezone.now()
    return list(
        Post.objects.filter(status="queued", dry_run=False, scheduled_at__lte=now)
        .order_by("scheduled_at")
        .values_list("pk", flat=True)[:100]
    )


def claim_for_publishing(post_id: int) -> bool:
    """Atomically move queued -> publishing. Only one worker can win, so a post is never sent twice."""
    return Post.objects.filter(pk=post_id, status="queued", dry_run=False).update(status="publishing") == 1


def publish_post(post_id: int) -> Post:
    post = Post.objects.select_related("account", "workspace__brand", "category").get(pk=post_id)
    if post.status != "publishing":
        return post
    account = post.account

    sub = Subscription.objects.filter(workspace=post.workspace).first()
    if not account.is_usable or sub is None or not sub.is_live():
        post.status, post.error = "failed", "Account not usable or subscription inactive"
        post.save(update_fields=["status", "error", "updated_at"])
        return post

    try:
        platform_id = get_publisher(account.platform).publish(account, post)
    except PublishError as exc:
        post.attempts += 1
        post.error = str(exc)[:500]
        if exc.auth_failed:
            account.status, account.last_error = "broken", str(exc)[:300]
            account.save(update_fields=["status", "last_error"])
        if exc.retryable and post.attempts < settings.PUBLISH_MAX_ATTEMPTS and not exc.auth_failed:
            post.status = "queued"
            post.scheduled_at = timezone.now() + timedelta(minutes=5 * post.attempts)
        else:
            post.status = "failed"
        post.save()
        log.warning("Publish failed for post %s: %s", post_id, exc)
        return post
    except Exception as exc:
        log.exception("Unexpected publish error for post %s", post_id)
        post.status, post.error = "failed", f"Unexpected error: {exc}"[:500]
        post.save()
        return post

    post.status, post.platform_post_id, post.published_at, post.error = "published", platform_id, timezone.now(), ""
    post.save()
    return post


def check_account(account) -> bool:
    """Verify credentials; update the account status. Returns True if healthy."""
    now = timezone.now()
    try:
        get_publisher(account.platform).check(account)
    except PublishError as exc:
        account.status = "broken" if (exc.auth_failed or not exc.retryable) else account.status
        account.last_error = str(exc)[:300]
        account.last_checked_at = now
        account.save(update_fields=["status", "last_error", "last_checked_at"])
        return False
    account.status, account.last_error, account.last_checked_at = "active", "", now
    account.save(update_fields=["status", "last_error", "last_checked_at"])
    return True


def approve(post: Post) -> bool:
    if not post.can_approve:
        return False
    post.status = "queued"
    if post.scheduled_at < timezone.now():
        post.scheduled_at = timezone.now()
    post.save(update_fields=["status", "scheduled_at", "updated_at"])
    return True


METRIC_FIELDS = ("reach", "likes", "comments", "shares", "clicks")


def collect_metrics(now=None, max_age_days=14, refresh_hours=6, limit=300):
    """Fetch engagement for recent published posts. Never raises: a platform without metrics is skipped."""
    now = now or timezone.now()
    saved = 0
    posts = (
        Post.objects.filter(status="published", platform_post_id__gt="", published_at__gte=now - timedelta(days=max_age_days),
                            published_at__lte=now - timedelta(hours=1))
        .select_related("account", "category").order_by("-published_at")[:limit]
    )
    for post in posts:
        last = post.metrics.order_by("-fetched_at").first()
        if last and last.fetched_at > now - timedelta(hours=refresh_hours):
            continue
        if not post.account.is_usable:
            continue
        try:
            data = get_publisher(post.account.platform).fetch_metrics(post.account, post)
        except PublishError as exc:
            log.info("Metrics unavailable for post %s: %s", post.pk, exc)
            continue
        except Exception:
            log.exception("Metrics crashed for post %s", post.pk)
            continue
        if not data:
            continue
        PostMetrics.objects.create(post=post, fetched_at=now, **{k: int(data.get(k, 0) or 0) for k in METRIC_FIELDS})
        saved += 1
    return saved
