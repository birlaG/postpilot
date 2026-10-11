"""Referral attribution and one-time wallet rewards."""

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import BonusCredit, Referral, WalletEntry


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


def reward_if_first_payment(workspace, currency):
    """Credit the referrer once, only after the referred workspace has paid."""
    if currency not in ("USD", "INR"):
        raise ValueError(f"Unsupported referral reward currency: {currency}")
    amount = (
        settings.REFERRAL_REWARD_USD_CENTS if currency == "USD"
        else settings.REFERRAL_REWARD_INR_PAISE
    )
    if amount <= 0:
        raise ValueError("Referral reward amounts must be positive.")

    with transaction.atomic():
        ref = (
            Referral.objects.select_for_update()
            .filter(referred=workspace, rewarded_at__isnull=True)
            .select_related("referrer")
            .first()
        )
        if ref is None:
            return False

        rewarded_count = Referral.objects.filter(
            referrer=ref.referrer, rewarded_at__isnull=False
        ).count()
        ref.rewarded_at = timezone.now()
        ref.save(update_fields=["rewarded_at"])

        if rewarded_count >= settings.REFERRAL_MAX_REWARDS:
            return True

        WalletEntry.objects.create(
            workspace=ref.referrer,
            kind="referral",
            status="posted",
            currency=currency,
            amount_minor=amount,
            reason="Successful referral",
            referral=ref,
        )
    return True


def bonus_posts(workspace, now=None):
    from django.db.models import Sum

    now = now or timezone.now()
    return BonusCredit.objects.filter(workspace=workspace, expires_at__gt=now).aggregate(t=Sum("extra_posts_per_month"))["t"] or 0
