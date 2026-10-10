"""Slot planning: turn each subscription's local posting times into Post rows."""
import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import learning
from .models import AccountCategory, Post

log = logging.getLogger(__name__)


def parse_times(values):
    out = []
    for raw in values or []:
        try:
            hh, mm = str(raw).split(":")
            out.append(time(int(hh), int(mm)))
        except (ValueError, TypeError):
            log.warning("Ignoring bad post time %r", raw)
    return sorted(out)


def claim_slot(sub: AccountCategory, slot_key: str, scheduled_at, idea=""):
    """Create the Post row that claims a slot. Returns the Post, or None if already claimed."""
    try:
        with transaction.atomic():
            return Post.objects.create(
                workspace=sub.account.workspace, account=sub.account, subscription=sub, category=sub.category,
                slot_key=slot_key, status="generating", scheduled_at=scheduled_at, idea=idea[:300],
            )
    except IntegrityError:
        return None


def plan_slots(now=None):
    """Claim every slot that is due for generation. Returns the list of new Post ids."""
    now = now or timezone.now()
    lead = timedelta(minutes=settings.GENERATE_LEAD_MINUTES)
    catchup = timedelta(minutes=settings.SLOT_CATCHUP_MINUTES)
    created = []
    subs = AccountCategory.objects.filter(
        enabled=True, category__is_active=True, account__status__in=["active", "expiring"]
    ).select_related("account__workspace", "category")
    for sub in subs:
        tz = sub.account.workspace.tz
        local_now = now.astimezone(tz)
        for day in (local_now.date(), local_now.date() + timedelta(days=1)):
            times = learning.learned_times(sub.account, sub.post_times, day) if sub.auto_times else sub.post_times
            for t in parse_times(times):
                slot_dt = datetime.combine(day, t, tzinfo=tz)
                # generate from `lead` before the slot until `catchup` after it
                if not (slot_dt - lead <= now <= slot_dt + catchup):
                    continue
                if sub.auto_times and Post.objects.filter(subscription=sub, slot_key__startswith=f"{day.isoformat()}T").count() >= len(sub.post_times):
                    continue  # learned hours moved during the day: never post more than the owner asked for
                post = claim_slot(sub, f"{day.isoformat()}T{t:%H:%M}", slot_dt)
                if post:
                    created.append(post.pk)
    return created


def create_manual_post(sub: AccountCategory, when=None, idea=""):
    """'Generate now' button: claims a unique manual slot."""
    now = when or timezone.now()
    return claim_slot(sub, f"manual-{now:%Y%m%dT%H%M%S%f}", now, idea=idea)
