"""Plan limits, Stripe (USD), and Razorpay/Cashfree (INR)."""
import base64
import hashlib
import hmac
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
import requests

from apps.workspaces.models import Workspace

from .models import Plan, Subscription, UsageEvent, WalletEntry

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ limits
def start_unpaid(workspace):
    """New workspaces cannot do anything until a plan is paid for."""
    plan = Plan.objects.filter(is_public=True).order_by("price_usd").first()
    if plan is None:
        raise RuntimeError("No plans exist. Run: python manage.py seed_plans")
    return Subscription.objects.create(workspace=workspace, plan=plan, status="unpaid")


def month_start(now=None):
    now = now or timezone.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def posts_used(workspace, now=None):
    from django.db.models import Sum

    total = UsageEvent.objects.filter(
        workspace=workspace, kind="post", created_at__gte=month_start(now)
    ).aggregate(t=Sum("count"))["t"]
    return total or 0


def post_limit(workspace):
    from . import referrals

    sub = getattr(workspace, "subscription", None)
    return (sub.plan.posts_per_month if sub else 0) + referrals.bonus_posts(workspace)


def can_generate(workspace):
    """Return (ok, reason)."""
    sub = getattr(workspace, "subscription", None)
    if sub is None or not sub.is_live():
        return False, "Choose and pay for a plan to start posting"
    limit = post_limit(workspace)
    if posts_used(workspace) >= limit:
        return False, f"Monthly limit of {limit} posts reached"
    return True, ""


def can_add_account(workspace):
    sub = getattr(workspace, "subscription", None)
    if sub is None or not sub.is_live():
        return False, "Choose and pay for a plan to connect accounts"
    if workspace.accounts.count() >= sub.plan.max_accounts:
        return False, f"Your plan allows {sub.plan.max_accounts} connected account(s)"
    return True, ""


def record_usage(workspace, kind="post", count=1):
    return UsageEvent.objects.create(workspace=workspace, kind=kind, count=count)


def _mark(sub, status, **fields):
    sub.status = status
    for key, value in fields.items():
        setattr(sub, key, value)
    sub.save()


# ------------------------------------------------------------------ checkout
def stripe_configured():
    return bool(settings.STRIPE_SECRET_KEY)


def razorpay_configured():
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def cashfree_configured():
    return bool(settings.CASHFREE_CLIENT_ID and settings.CASHFREE_CLIENT_SECRET)


def inr_provider():
    if razorpay_configured():
        return "razorpay"
    if cashfree_configured():
        return "cashfree"
    return ""


def available_currencies():
    out = []
    if stripe_configured():
        out.append("USD")
    if inr_provider():
        out.append("INR")
    return out


def wallet_balance(workspace, currency, now=None):
    now = now or timezone.now()
    entries = WalletEntry.objects.filter(workspace=workspace, currency=currency)
    credits = entries.filter(kind="referral", status="posted").aggregate(total=Sum("amount_minor"))["total"] or 0
    debits = entries.filter(kind="redemption", status="posted").aggregate(total=Sum("amount_minor"))["total"] or 0
    reservations = entries.filter(
        kind="redemption", status="pending", expires_at__gt=now
    ).aggregate(total=Sum("amount_minor"))["total"] or 0
    return credits - debits - reservations


def formatted_wallet_amount(amount_minor, currency):
    symbol = "$" if currency == "USD" else "₹"
    return f"{symbol}{Decimal(amount_minor) / 100:,.2f}"


def _reserve_wallet_redemption(workspace, plan, currency):
    if currency == "INR" and inr_provider() == "cashfree":
        raise ValueError("INR wallet redemption is not supported through Cashfree checkout.")
    if currency == "INR" and not settings.RAZORPAY_REFERRAL_OFFER_ID:
        raise ValueError("INR wallet checkout needs RAZORPAY_REFERRAL_OFFER_ID configured.")

    with transaction.atomic():
        Workspace.objects.select_for_update().get(pk=workspace.pk)
        sub = Subscription.objects.select_for_update().get(workspace=workspace)
        if sub.status not in ("unpaid", "cancelled"):
            raise ValueError("Wallet credit can only be used for a new or cancelled subscription checkout.")

        if WalletEntry.objects.filter(
            workspace=workspace,
            kind="redemption",
            status="pending",
            expires_at__gt=timezone.now(),
        ).exists():
            raise ValueError("A wallet checkout is already in progress.")

        available = wallet_balance(workspace, currency)
        if currency == "USD":
            amount = min(available, plan.price_usd * 100 - 1)
        else:
            # Razorpay offers have a fixed amount, so redeem one ₹50 reward per checkout.
            amount = min(available, settings.REFERRAL_REWARD_INR_PAISE)
            if amount < settings.REFERRAL_REWARD_INR_PAISE:
                amount = 0
        if amount <= 0:
            raise ValueError(f"No redeemable {currency} wallet credit is available.")

        return WalletEntry.objects.create(
            workspace=workspace,
            kind="redemption",
            status="pending",
            currency=currency,
            amount_minor=amount,
            reason="Reserved for subscription checkout",
            expires_at=timezone.now() + timedelta(hours=24),
        )


def _release_wallet_redemption(wallet_entry):
    if wallet_entry is not None:
        WalletEntry.objects.filter(pk=wallet_entry.pk, status="pending").update(
            status="released",
            reason="Checkout cancelled or could not be created",
            updated_at=timezone.now(),
        )


def _post_wallet_redemption(wallet_entry_id):
    if wallet_entry_id:
        WalletEntry.objects.filter(
            pk=wallet_entry_id, kind="redemption", status="pending"
        ).update(
            status="posted",
            reason="Used on subscription checkout",
            expires_at=None,
            updated_at=timezone.now(),
        )


def _post_wallet_redemption_for_subscription(subscription_id):
    WalletEntry.objects.filter(
        provider_subscription_id=subscription_id,
        kind="redemption",
        status="pending",
    ).update(
        status="posted",
        reason="Used on subscription checkout",
        expires_at=None,
        updated_at=timezone.now(),
    )


def _release_wallet_redemption_for_subscription(subscription_id):
    WalletEntry.objects.filter(
        provider_subscription_id=subscription_id,
        kind="redemption",
        status="pending",
    ).update(
        status="released",
        reason="Subscription checkout cancelled",
        updated_at=timezone.now(),
    )


def create_checkout(workspace, plan, currency, success_url="", cancel_url="", apply_wallet=False, customer_phone=""):
    """Create a hosted new-subscription checkout, optionally reserving wallet credit."""
    if currency not in ("USD", "INR"):
        raise ValueError(f"Unsupported billing currency: {currency}")
    wallet_entry = _reserve_wallet_redemption(workspace, plan, currency) if apply_wallet else None
    try:
        if currency == "INR":
            if inr_provider() == "razorpay":
                return _razorpay_checkout(workspace, plan, wallet_entry)
            if inr_provider() == "cashfree":
                return _cashfree_checkout(workspace, plan, customer_phone, success_url)
            raise RuntimeError("No INR payment provider is configured")
        return _stripe_checkout(workspace, plan, success_url, cancel_url, wallet_entry)
    except Exception:
        _release_wallet_redemption(wallet_entry)
        raise


def _stripe_checkout(workspace, plan, success_url, cancel_url, wallet_entry=None):
    import stripe

    if not stripe_configured():
        raise RuntimeError("Stripe is not configured")
    if not plan.stripe_price_id:
        raise RuntimeError(f"Plan {plan.code} has no stripe_price_id")
    stripe.api_key = settings.STRIPE_SECRET_KEY
    sub = workspace.subscription
    metadata = {"workspace_id": str(workspace.pk), "plan_code": plan.code}
    checkout_params = {
        "mode": "subscription",
        "line_items": [{"price": plan.stripe_price_id, "quantity": 1}],
        "success_url": success_url or f"{settings.PUBLIC_BASE_URL}/billing/?paid=1",
        "cancel_url": cancel_url or f"{settings.PUBLIC_BASE_URL}/billing/",
        "client_reference_id": str(workspace.pk),
        "customer": sub.customer_id or None,
        "customer_email": None if sub.customer_id else workspace.owner.email,
        "subscription_data": {"metadata": metadata},
        "metadata": metadata,
    }
    if wallet_entry:
        coupon = stripe.Coupon.create(
            amount_off=wallet_entry.amount_minor,
            currency="usd",
            duration="once",
            max_redemptions=1,
            name="PostPilot referral wallet credit",
            idempotency_key=f"referral-wallet-coupon-{wallet_entry.pk}",
        )
        metadata["wallet_entry_id"] = str(wallet_entry.pk)
        checkout_params["discounts"] = [{"coupon": coupon.id}]
        checkout_params["subscription_data"]["metadata"] = metadata
        checkout_params["metadata"] = metadata

    session = stripe.checkout.Session.create(**checkout_params)
    if wallet_entry:
        WalletEntry.objects.filter(pk=wallet_entry.pk).update(
            provider_checkout_id=session.id,
            updated_at=timezone.now(),
        )
    _mark(sub, "pending", plan=plan, provider="stripe")
    return session.url


def _razorpay_client():
    import razorpay

    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def _razorpay_checkout(workspace, plan, wallet_entry=None):
    if not razorpay_configured():
        raise RuntimeError("Razorpay is not configured")
    if not plan.razorpay_plan_id:
        raise RuntimeError(f"Plan {plan.code} has no razorpay_plan_id")
    request = {
        "plan_id": plan.razorpay_plan_id,
        "total_count": 120,
        "customer_notify": 1,
        "notes": {"workspace_id": str(workspace.pk), "plan_code": plan.code},
        "expire_by": int((timezone.now() + timedelta(hours=24)).timestamp()),
    }
    if wallet_entry:
        request["offer_id"] = settings.RAZORPAY_REFERRAL_OFFER_ID
        request["notes"]["wallet_entry_id"] = str(wallet_entry.pk)
    data = _razorpay_client().subscription.create(request)
    if wallet_entry:
        WalletEntry.objects.filter(pk=wallet_entry.pk).update(
            provider_checkout_id=data["id"],
            provider_subscription_id=data["id"],
            updated_at=timezone.now(),
        )
    _mark(workspace.subscription, "pending", plan=plan, provider="razorpay", external_id=data["id"])
    return data["short_url"]


def _cashfree_checkout(workspace, plan, customer_phone, success_url=""):
    if not cashfree_configured():
        raise RuntimeError("Cashfree is not configured")
    phone = re.sub(r"\D", "", customer_phone or "")
    if len(phone) == 12 and phone.startswith("91"):
        phone = phone[2:]
    if len(phone) != 10 or phone[0] not in "6789":
        raise ValueError("Enter a valid 10-digit Indian mobile number for Cashfree recurring payments.")

    mode = settings.CASHFREE_ENVIRONMENT.lower()
    if mode not in ("sandbox", "production"):
        raise RuntimeError("CASHFREE_ENVIRONMENT must be either sandbox or production.")
    api_host = "https://sandbox.cashfree.com" if mode == "sandbox" else "https://api.cashfree.com"
    subscription_id = f"pp-{uuid.uuid4().hex}"
    request = {
        "subscription_id": subscription_id,
        "customer_details": {
            "customer_name": (workspace.owner.get_full_name() or workspace.name)[:100],
            "customer_email": workspace.owner.email,
            "customer_phone": phone,
        },
        "plan_details": {
            "plan_name": f"PostPilot {plan.name}"[:40],
            "plan_type": "PERIODIC",
            "plan_amount": plan.price_inr,
            "plan_max_amount": plan.price_inr,
            "plan_max_cycles": 120,
            "plan_intervals": 1,
            "plan_interval_type": "MONTH",
            "plan_currency": "INR",
        },
        "subscription_meta": {
            "return_url": success_url or f"{settings.PUBLIC_BASE_URL}/billing/?currency=INR",
        },
    }
    response = requests.post(
        f"{api_host}/pg/subscriptions",
        json=request,
        headers={
            "x-client-id": settings.CASHFREE_CLIENT_ID,
            "x-client-secret": settings.CASHFREE_CLIENT_SECRET,
            "x-api-version": settings.CASHFREE_API_VERSION,
            "x-idempotency-key": str(uuid.uuid4()),
            "Content-Type": "application/json",
        },
        timeout=settings.HTTP_TIMEOUT,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Cashfree returned an invalid response while creating the subscription.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Cashfree returned an unexpected response while creating the subscription.")
    if response.status_code >= 400:
        message = data.get("message") or data.get("type") or f"HTTP {response.status_code}"
        raise RuntimeError(f"Cashfree subscription creation failed: {message}")

    session_id = data.get("subscription_session_id")
    if not session_id or data.get("subscription_id") != subscription_id:
        raise RuntimeError("Cashfree did not return the expected recurring subscription session.")
    sub = workspace.subscription
    _mark(sub, "pending", plan=plan, provider="cashfree", external_id=subscription_id)
    return {
        "provider": "cashfree",
        "session_id": session_id,
        "environment": mode,
    }


# ------------------------------------------------------------------ webhooks
def _ts(value):
    return datetime.fromtimestamp(int(value), tz=dt_timezone.utc) if value else None


def verify_razorpay_signature(body: bytes, signature: str) -> bool:
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret or not signature:
        return False
    return hmac.compare_digest(hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), signature)


def verify_cashfree_signature(body: bytes, timestamp: str, signature: str) -> bool:
    secret = settings.CASHFREE_CLIENT_SECRET
    if not secret or not timestamp or not signature:
        return False
    signed_payload = timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(secret.encode(), signed_payload, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature)


def handle_cashfree_event(event: dict) -> bool:
    if not isinstance(event, dict):
        return False
    data = event.get("data")
    if not isinstance(data, dict):
        return False
    details = data.get("subscription_details")
    if not isinstance(details, dict):
        details = {}
    subscription_id = details.get("subscription_id") or data.get("subscription_id")
    sub = Subscription.objects.filter(provider="cashfree", external_id=subscription_id).first() if subscription_id else None
    if sub is None:
        log.warning("Cashfree webhook for unknown subscription %s", subscription_id)
        return False

    event_type = event.get("type", "")
    status = details.get("subscription_status", "")
    if event_type == "SUBSCRIPTION_STATUS_CHANGED":
        if status == "ACTIVE":
            _mark(sub, "active", grace_until=None)
        elif status == "ON_HOLD":
            _mark(sub, "past_due", grace_until=timezone.now() + timedelta(days=settings.BILLING_GRACE_DAYS))
        elif status in ("PAUSED", "CUSTOMER_PAUSED"):
            _mark(sub, "past_due", grace_until=timezone.now() + timedelta(days=settings.BILLING_GRACE_DAYS))
        elif status in ("CANCELLED", "CUSTOMER_CANCELLED", "COMPLETED", "EXPIRED", "LINK_EXPIRED", "CARD_EXPIRED"):
            _mark(sub, "cancelled")
            _release_wallet_redemption_for_subscription(subscription_id)
        elif status in ("INITIALIZED", "BANK_APPROVAL_PENDING"):
            if sub.status != "active":
                _mark(sub, "pending")
        else:
            return False
        return True

    if event_type == "SUBSCRIPTION_PAYMENT_SUCCESS":
        _mark(sub, "active", grace_until=None)
        from . import referrals

        referrals.reward_if_first_payment(sub.workspace, "INR")
        return True

    if event_type == "SUBSCRIPTION_PAYMENT_FAILED":
        if sub.status == "pending":
            _mark(sub, "unpaid")
        elif sub.status != "cancelled":
            _mark(sub, "past_due", grace_until=timezone.now() + timedelta(days=settings.BILLING_GRACE_DAYS))
        return True

    if event_type == "SUBSCRIPTION_PAYMENT_CANCELLED":
        if sub.status == "pending":
            _mark(sub, "unpaid")
        elif sub.status != "cancelled":
            _mark(sub, "past_due", grace_until=timezone.now() + timedelta(days=settings.BILLING_GRACE_DAYS))
        return True

    if event_type == "SUBSCRIPTION_AUTH_STATUS":
        authorization_details = data.get("authorization_details")
        if not isinstance(authorization_details, dict):
            return False
        authorization_status = authorization_details.get("authorization_status", "")
        if authorization_status == "FAILED" and sub.status == "pending":
            _mark(sub, "unpaid")
            return True
        return authorization_status in ("SUCCESS", "PENDING")

    return False


def handle_razorpay_event(event: dict) -> bool:
    name = event.get("event", "")
    entity = (event.get("payload", {}).get("subscription", {}) or {}).get("entity", {})
    sub_id = entity.get("id")
    sub = Subscription.objects.filter(provider="razorpay", external_id=sub_id).first() if sub_id else None
    if sub is None:
        log.warning("Razorpay webhook for unknown subscription %s", sub_id)
        return False
    if name in ("subscription.activated", "subscription.charged", "subscription.resumed"):
        _mark(sub, "active", renews_at=_ts(entity.get("current_end")), grace_until=None)
        if name == "subscription.charged":
            _post_wallet_redemption_for_subscription(sub_id)
            from . import referrals

            referrals.reward_if_first_payment(sub.workspace, "INR")
    elif name in ("subscription.halted", "subscription.pending"):
        _mark(sub, "past_due", grace_until=timezone.now() + timedelta(days=settings.BILLING_GRACE_DAYS))
    elif name in ("subscription.cancelled", "subscription.completed"):
        _mark(sub, "cancelled")
        _release_wallet_redemption_for_subscription(sub_id)
    else:
        return False
    return True


def construct_stripe_event(body: bytes, signature: str):
    """Verify the Stripe signature and return the event. Raises ValueError when invalid."""
    import stripe

    if not settings.STRIPE_WEBHOOK_SECRET:
        raise ValueError("Stripe webhook secret not configured")
    try:
        return stripe.Webhook.construct_event(body, signature, settings.STRIPE_WEBHOOK_SECRET)
    except Exception as exc:  # signature or payload errors
        raise ValueError(str(exc)) from exc


def handle_stripe_event(event) -> bool:
    if hasattr(event, "to_dict"):  # StripeObject is not a plain dict
        event = event.to_dict()
    kind = event["type"]
    obj = event["data"]["object"]
    if kind == "checkout.session.expired":
        _release_wallet_redemption((obj.get("metadata") or {}).get("wallet_entry_id"))
        return True
    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        ws_id = (obj.get("metadata") or {}).get("workspace_id") or obj.get("client_reference_id")
        sub = Subscription.objects.filter(workspace_id=ws_id).first() if ws_id else None
        if sub is None:
            return False
        plan = Plan.objects.filter(code=(obj.get("metadata") or {}).get("plan_code")).first() or sub.plan
        _mark(sub, "active", plan=plan, provider="stripe", external_id=obj.get("subscription") or "",
              customer_id=obj.get("customer") or "", grace_until=None)
        wallet_entry_id = (obj.get("metadata") or {}).get("wallet_entry_id")
        if wallet_entry_id:
            WalletEntry.objects.filter(
                pk=wallet_entry_id, workspace=sub.workspace, status="pending"
            ).update(
                provider_checkout_id=obj.get("id") or "",
                provider_subscription_id=obj.get("subscription") or "",
                updated_at=timezone.now(),
            )
            if kind == "checkout.session.async_payment_succeeded" or obj.get("payment_status") == "paid":
                _post_wallet_redemption(wallet_entry_id)
        return True
    if kind.startswith("invoice."):
        parent_details = ((obj.get("parent") or {}).get("subscription_details") or {})
        subscription_details = obj.get("subscription_details") or parent_details
        sub_id = obj.get("subscription") or subscription_details.get("subscription")
        if isinstance(sub_id, dict):
            sub_id = sub_id.get("id")
    else:
        sub_id = obj.get("id")
    sub = Subscription.objects.filter(provider="stripe", external_id=sub_id).first() if sub_id else None
    if sub is None and sub_id and kind.startswith("invoice."):
        metadata = (obj.get("subscription_details") or {}).get("metadata") or parent_details.get("metadata") or {}
        workspace_id = metadata.get("workspace_id")
        if workspace_id:
            sub = Subscription.objects.filter(
                workspace_id=workspace_id,
                provider__in=("", "stripe"),
            ).first()
            if sub:
                sub.provider = "stripe"
                sub.external_id = sub_id
                sub.save(update_fields=["provider", "external_id", "updated_at"])
    if sub is None:
        return False
    if kind == "invoice.paid":
        period_end = (obj.get("lines", {}).get("data") or [{}])[0].get("period", {}).get("end")
        _mark(sub, "active", renews_at=_ts(period_end), grace_until=None)
        _post_wallet_redemption_for_subscription(sub_id)
        from . import referrals

        referrals.reward_if_first_payment(sub.workspace, "USD")
    elif kind == "invoice.payment_failed":
        _mark(sub, "past_due", grace_until=timezone.now() + timedelta(days=settings.BILLING_GRACE_DAYS))
    elif kind == "customer.subscription.deleted":
        _mark(sub, "cancelled")
        _release_wallet_redemption_for_subscription(sub_id)
    else:
        return False
    return True
