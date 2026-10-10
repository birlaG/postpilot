"""Best-time learning: pick posting hours from how this account's own posts performed.

Rules, kept simple on purpose:
- needs at least MIN_POSTS posts with results in the last 60 days, and at least `k` different hours
  that each have 2+ posts, otherwise nothing is learned and the manual times stay in use;
- hours are ranked by average engagement (likes + comments + shares);
- exploration: with 2+ posts a day the last slot stays on the owner's manual time, and with one post a day
  every 5th day uses the manual time, so the system keeps seeing other hours instead of locking in.
"""
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

MIN_POSTS = 10
MIN_PER_HOUR = 2
WINDOW_DAYS = 60
CACHE_SECONDS = 6 * 3600


def _ranked_hours(account):
    from .models import Post

    tz = account.workspace.tz
    since = timezone.now() - timedelta(days=WINDOW_DAYS)
    by_hour, total = {}, 0
    posts = Post.objects.filter(account=account, status="published", published_at__gte=since).prefetch_related("metrics")
    for post in posts:
        m = post.metrics.order_by("-fetched_at").first()
        if not m:
            continue
        total += 1
        by_hour.setdefault(post.published_at.astimezone(tz).hour, []).append(m.likes + m.comments + m.shares)
    if total < MIN_POSTS:
        return []
    ranked = sorted(((sum(v) / len(v), h) for h, v in by_hour.items() if len(v) >= MIN_PER_HOUR), reverse=True)
    return [h for _, h in ranked]


def ranked_hours(account):
    return cache.get_or_set(f"best-hours:{account.pk}", lambda: _ranked_hours(account), CACHE_SECONDS)


def learned_times(account, manual_times, day):
    """Return the times to use for `day`: learned ones when there is enough data, else the manual list."""
    manual = sorted(manual_times or [])
    k = len(manual)
    hours = ranked_hours(account)
    if not manual or len(hours) < k:
        return manual
    if k == 1:
        return manual if day.toordinal() % 5 == 0 else [f"{hours[0]:02d}:00"]
    learned = [f"{h:02d}:00" for h in hours[: k - 1]]
    keep = next((t for t in reversed(manual) if t not in learned), manual[-1])  # exploration slot
    return sorted(set(learned + [keep]))[:k] if len(set(learned + [keep])) >= k else manual
