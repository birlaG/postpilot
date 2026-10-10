"""Plan limits, Stripe (USD, worldwide) and Razorpay (INR, India)."""
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

from .models import Plan, Subscription, UsageEvent

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
    if status == "active":
        from . import referrals

        referrals.reward_if_first_payment(sub.workspace)


# ------------------------------------------------------------------ checkout
def stripe_configured():
    return bool(settings.STRIPE_SECRET_KEY)


def razorpay_configured():
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def available_currencies():
    out = []
    if stripe_configured():
        out.append("USD")
    if razorpay_configured():
        out.append("INR")
    return out


def create_checkout(workspace, plan, currency, success_url="", cancel_url=""):
    """Return a hosted payment URL for the plan in the chosen currency."""
    if currency == "INR":
        return _razorpay_checkout(workspace, plan)
    return _stripe_checkout(workspace, plan, success_url, cancel_url)


def _stripe_checkout(workspace, plan, success_url, cancel_url):
    import stripe

    if not stripe_configured():
        raise RuntimeError("Stripe is not configured")
    if not plan.stripe_price_id:
        raise RuntimeError(f"Plan {plan.code} has no stripe_price_id")
    stripe.api_key = settings.STRIPE_SECRET_KEY
    sub = workspace.subscription
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
        success_url=success_url or f"{settings.PUBLIC_BASE_URL}/billing/?paid=1",
        cancel_url=cancel_url or f"{settings.PUBLIC_BASE_URL}/billing/",
        client_reference_id=str(workspace.pk),
        customer=sub.customer_id or None,
        customer_email=None if sub.customer_id else workspace.owner.email,
        subscription_data={"metadata": {"workspace_id": str(workspace.pk), "plan_code": plan.code}},
        metadata={"workspace_id": str(workspace.pk), "plan_code": plan.code},
    )
    _mark(sub, "pending", plan=plan, provider="stripe")
    return session.url


def _razorpay_client():
    import razorpay

    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def _razorpay_checkout(workspace, plan):
    if not razorpay_configured():
        raise RuntimeError("Razorpay is not configured")
    if not plan.razorpay_plan_id:
        raise RuntimeError(f"Plan {plan.code} has no razorpay_plan_id")
    data = _razorpay_client().subscription.create(
        {
            "plan_id": plan.razorpay_plan_id,
            "total_count": 120,
            "customer_notify": 1,
            "notes": {"workspace_id": str(workspace.pk), "plan_code": plan.code},
        }
    )
    _mark(workspace.subscription, "pending", plan=plan, provider="razorpay", external_id=data["id"])
    return data["short_url"]


# ------------------------------------------------------------------ webhooks
def _ts(value):
    return datetime.fromtimestamp(int(value), tz=dt_timezone.utc) if value else None


def verify_razorpay_signature(body: bytes, signature: str) -> bool:
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret or not signature:
        return False
    return hmac.compare_digest(hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), signature)


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
    elif name in ("subscription.halted", "subscription.pending"):
        _mark(sub, "past_due", grace_until=timezone.now() + timedelta(days=settings.BILLING_GRACE_DAYS))
    elif name in ("subscription.cancelled", "subscription.completed"):
        _mark(sub, "cancelled")
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
    if kind == "checkout.session.completed":
        ws_id = (obj.get("metadata") or {}).get("workspace_id") or obj.get("client_reference_id")
        sub = Subscription.objects.filter(workspace_id=ws_id).first() if ws_id else None
        if sub is None:
            return False
        plan = Plan.objects.filter(code=(obj.get("metadata") or {}).get("plan_code")).first() or sub.plan
        _mark(sub, "active", plan=plan, provider="stripe", external_id=obj.get("subscription") or "",
              customer_id=obj.get("customer") or "", grace_until=None)
        return True
    sub_id = obj.get("subscription") if kind.startswith("invoice.") else obj.get("id")
    sub = Subscription.objects.filter(provider="stripe", external_id=sub_id).first() if sub_id else None
    if sub is None:
        return False
    if kind == "invoice.paid":
        period_end = (obj.get("lines", {}).get("data") or [{}])[0].get("period", {}).get("end")
        _mark(sub, "active", renews_at=_ts(period_end), grace_until=None)
    elif kind == "invoice.payment_failed":
        _mark(sub, "past_due", grace_until=timezone.now() + timedelta(days=settings.BILLING_GRACE_DAYS))
    elif kind == "customer.subscription.deleted":
        _mark(sub, "cancelled")
    else:
        return False
    return True
