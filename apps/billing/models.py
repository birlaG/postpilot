from django.db import models
from django.utils import timezone

from apps.workspaces.models import Workspace


class Plan(models.Model):
    """Every plan is paid. There is no free tier."""

    code = models.SlugField(unique=True)
    name = models.CharField(max_length=60)
    price_usd = models.PositiveIntegerField(help_text="Per month in US dollars (Stripe)")
    price_inr = models.PositiveIntegerField(help_text="Per month in rupees (Razorpay)")
    max_accounts = models.PositiveIntegerField()
    posts_per_month = models.PositiveIntegerField()
    allow_video = models.BooleanField(default=True)
    stripe_price_id = models.CharField(max_length=80, blank=True, help_text="Stripe recurring price id (USD)")
    razorpay_plan_id = models.CharField(max_length=60, blank=True, help_text="Razorpay plan id (INR)")
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_public = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order"]

    def __str__(self):
        return self.name

    def price(self, currency):
        return self.price_inr if currency == "INR" else self.price_usd


class Subscription(models.Model):
    STATUSES = [
        ("unpaid", "Choose a plan"),
        ("pending", "Awaiting payment"),
        ("active", "Active"),
        ("past_due", "Payment failed"),
        ("cancelled", "Cancelled"),
    ]
    PROVIDERS = [("", "None"), ("stripe", "Stripe"), ("razorpay", "Razorpay")]

    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE, related_name="subscription")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    status = models.CharField(max_length=12, choices=STATUSES, default="unpaid")
    provider = models.CharField(max_length=10, choices=PROVIDERS, blank=True)
    external_id = models.CharField(max_length=80, blank=True, db_index=True, help_text="Stripe or Razorpay subscription id")
    customer_id = models.CharField(max_length=80, blank=True, help_text="Stripe customer id")
    renews_at = models.DateTimeField(null=True, blank=True)
    grace_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.workspace}: {self.plan} ({self.status})"

    def is_live(self, now=None):
        """Can this workspace generate and publish right now? Only when paid (or inside the grace period)."""
        now = now or timezone.now()
        if self.status == "active":
            return True
        if self.status == "past_due":
            return bool(self.grace_until and self.grace_until > now)
        return False


class UsageEvent(models.Model):
    """One row per generated post. Enforces plan limits."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="usage")
    kind = models.CharField(max_length=20, default="post")
    count = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)


class Referral(models.Model):
    """One row per referred workspace. The reward is given once, after their first real payment."""

    referrer = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="referrals_made")
    referred = models.OneToOneField(Workspace, on_delete=models.CASCADE, related_name="referral_received")
    created_at = models.DateTimeField(auto_now_add=True)
    rewarded_at = models.DateTimeField(null=True, blank=True)


class BonusCredit(models.Model):
    """Extra posts per month for a limited time (referral rewards, goodwill gestures)."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="bonuses")
    reason = models.CharField(max_length=80)
    extra_posts_per_month = models.PositiveIntegerField()
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"+{self.extra_posts_per_month}/month for {self.workspace} ({self.reason})"
