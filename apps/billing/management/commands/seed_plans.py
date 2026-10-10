from django.core.management.base import BaseCommand

from apps.billing.models import Plan

PLANS = [
    # code, name, usd, inr, accounts, posts/month, video, sort
    ("starter", "Starter", 9, 599, 3, 90, False, 1),
    ("pro", "Pro", 29, 1999, 6, 300, True, 2),
    ("agency", "Agency", 99, 7499, 25, 1500, True, 3),
]


class Command(BaseCommand):
    help = "Create the default paid plans. Prices are proposals: edit them in /admin/ and paste the Stripe/Razorpay ids."

    def handle(self, *args, **opts):
        for code, name, usd, inr, accounts, posts, video, order in PLANS:
            _, created = Plan.objects.get_or_create(
                code=code,
                defaults=dict(name=name, price_usd=usd, price_inr=inr, max_accounts=accounts,
                              posts_per_month=posts, allow_video=video, sort_order=order),
            )
            self.stdout.write(f"{'created' if created else 'exists '} {code}")
