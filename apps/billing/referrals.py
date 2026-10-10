"""Invite and earn: both sides get extra monthly posts after the invited workspace's first payment."""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import BonusCredit, Referral

log = logging.getLogger(__name__)


def attach(new_workspace, code):
    """Link a freshly signed-up workspace to whoever invited it. Silently ignores bad codes."""
    from apps.workspaces.models import Workspace

    code = (code or "").strip().lower()
    if not code:
        return None
    referrer = Workspace.objects.filter(referral_code=code).first()
    if referrer is None or referrer.pk == new_workspace.pk or referrer.owner_id == new_workspace.owner_id:
        return None
    try:
        with transaction.atomic():
            new_workspace.referred_by = referrer
            new_workspace.save(update_fields=["referred_by"])
            return Referral.objects.create(referrer=referrer, referred=new_workspace)
    except IntegrityError:
        return None


def reward_if_first_payment(workspace):
    """Called when a subscription becomes active. Pays out at most once per referred workspace."""
    ref = Referral.objects.filter(referred=workspace, rewarded_at__isnull=True).select_related('referrer').first()
    if ref is None:
        return False
    claimed = Referral.objects.filter(pk=ref.pk, rewarded_at__isnull=True).update(rewarded_at=timezone.now())
    if not claimed:
        return False
    expires = timezone.now() + timedelta(days=settings.REFERRAL_BONUS_DAYS)
    BonusCredit.objects.create(workspace=workspace, reason="Joined with an invite", extra_posts_per_month=settings.REFERRAL_BONUS_POSTS, expires_at=expires)
    rewarded = Referral.objects.filter(referrer=ref.referrer, rewarded_at__isnull=False).count()
    if rewarded <= settings.REFERRAL_MAX_REWARDS:
        BonusCredit.objects.create(workspace=ref.referrer, reason="Friend subscribed", extra_posts_per_month=settings.REFERRAL_BONUS_POSTS, expires_at=expires)
    return True


def bonus_posts(workspace, now=None):
    from django.db.models import Sum

    now = now or timezone.now()
    return BonusCredit.objects.filter(workspace=workspace, expires_at__gt=now).aggregate(t=Sum("extra_posts_per_month"))["t"] or 0
