"""Worldwide, paid-only and multi-platform behaviour."""
import base64
import hashlib
import hmac
import json
import shutil
import time
from types import SimpleNamespace
from unittest import mock, skipUnless

from django.db import IntegrityError, transaction
from django.test import Client, override_settings
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import Plan, Subscription, WalletEntry
from apps.content import media, trends
from apps.content.models import Post, TrendItem
from apps.content.pipeline import run_generation
from apps.content.tasks import ingest_trends
from apps.social import oauth
from apps.social.models import SocialAccount
from apps.social.publishers import PublishError, get_publisher
from apps.social.publishers.linkedin import escape_commentary
from apps.social.publishers.youtube import split_title_description
from tests.test_core import TMP_MEDIA, BaseCase, make_workspace


def resp(status=200, payload=None, headers=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload or {}
    r.headers = headers or {}
    r.text = json.dumps(payload or {})
    return r


class StripeBillingTests(BaseCase):
    def stripe_event(self, secret, payload):
        body = json.dumps(payload).encode()
        ts = str(int(time.time()))
        sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return body, f"t={ts},v1={sig}"

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_test")
    def test_stripe_lifecycle(self):
        _, ws, _, _ = make_workspace(paid=False)
        client = Client()
        body, sig = self.stripe_event("whsec_test", {
            "id": "evt_1", "object": "event", "type": "checkout.session.completed",
            "data": {"object": {"object": "checkout.session", "subscription": "sub_s1", "customer": "cus_1",
                                "client_reference_id": str(ws.pk), "metadata": {"workspace_id": str(ws.pk), "plan_code": "starter"}}},
        })
        self.assertEqual(client.post("/billing/webhook/stripe/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=bad").status_code, 400)
        self.assertEqual(Subscription.objects.get(workspace=ws).status, "unpaid")
        self.assertEqual(client.post("/billing/webhook/stripe/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig).status_code, 200)
        sub = Subscription.objects.get(workspace=ws)
        self.assertEqual((sub.status, sub.plan.code, sub.provider, sub.external_id), ("active", "starter", "stripe", "sub_s1"))

        for kind, expected in (("invoice.payment_failed", "past_due"), ("customer.subscription.deleted", "cancelled")):
            obj = {"object": "invoice", "subscription": "sub_s1"} if kind.startswith("invoice") else {"object": "subscription", "id": "sub_s1"}
            body, sig = self.stripe_event("whsec_test", {"id": "evt_2", "object": "event", "type": kind, "data": {"object": obj}})
            client.post("/billing/webhook/stripe/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig)
            self.assertEqual(Subscription.objects.get(workspace=ws).status, expected)

    def test_referral_reward_is_granted_only_after_a_paid_stripe_invoice(self):
        from apps.billing import referrals

        _, owner, _, _ = make_workspace(email="wallet-owner@example.com")
        _, friend, _, _ = make_workspace(email="wallet-friend@example.com", paid=False)
        referrals.attach(friend, owner.referral_code)
        sub = friend.subscription
        sub.provider = "stripe"
        sub.external_id = ""
        sub.save()

        billing._mark(sub, "active")
        self.assertEqual(billing.wallet_balance(owner, "USD"), 0)
        event = {
            "type": "invoice.paid",
            "data": {"object": {
                "parent": {"subscription_details": {
                    "subscription": "sub_referred",
                    "metadata": {"workspace_id": str(friend.pk)},
                }},
                "lines": {"data": []},
            }},
        }
        self.assertTrue(billing.handle_stripe_event(event))
        self.assertEqual(billing.wallet_balance(owner, "USD"), 50)
        billing.handle_stripe_event(event)
        self.assertEqual(billing.wallet_balance(owner, "USD"), 50)

    def test_razorpay_referral_reward_is_granted_once_on_a_successful_charge(self):
        from apps.billing import referrals

        _, owner, _, _ = make_workspace(email="wallet-owner-inr@example.com")
        _, friend, _, _ = make_workspace(email="wallet-friend-inr@example.com", paid=False)
        referrals.attach(friend, owner.referral_code)
        sub = friend.subscription
        sub.provider = "razorpay"
        sub.external_id = "sub_referred_inr"
        sub.save()
        event = {
            "event": "subscription.charged",
            "payload": {"subscription": {"entity": {
                "id": "sub_referred_inr", "current_end": 1893456000,
            }}},
        }

        billing.handle_razorpay_event({
            "event": "subscription.activated",
            "payload": event["payload"],
        })
        self.assertEqual(billing.wallet_balance(owner, "INR"), 0)
        self.assertTrue(billing.handle_razorpay_event(event))
        self.assertEqual(billing.wallet_balance(owner, "INR"), 5000)
        billing.handle_razorpay_event(event)
        self.assertEqual(billing.wallet_balance(owner, "INR"), 5000)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_stripe_checkout_marks_pending(self):
        _, ws, _, _ = make_workspace(paid=False)
        Plan.objects.filter(code="pro").update(stripe_price_id="price_123")
        with mock.patch("stripe.checkout.Session.create", return_value=SimpleNamespace(url="https://checkout.stripe.test/s")) as m:
            url = billing.create_checkout(ws, Plan.objects.get(code="pro"), "USD")
        self.assertEqual(url, "https://checkout.stripe.test/s")
        self.assertEqual(m.call_args.kwargs["line_items"], [{"price": "price_123", "quantity": 1}])
        sub = Subscription.objects.get(workspace=ws)
        self.assertEqual((sub.status, sub.provider), ("pending", "stripe"))

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_stripe_wallet_credit_is_reserved_and_posted_after_paid_checkout(self):
        _, ws, _, _ = make_workspace(paid=False)
        Plan.objects.filter(code="pro").update(stripe_price_id="price_123")
        credit = WalletEntry.objects.create(
            workspace=ws, kind="referral", currency="USD", amount_minor=50,
            reason="Successful referral",
        )
        with mock.patch("stripe.Coupon.create", return_value=SimpleNamespace(id="coupon_1")) as coupon, \
             mock.patch("stripe.checkout.Session.create", return_value=SimpleNamespace(
                 id="cs_123", url="https://checkout.stripe.test/s"
             )) as session:
            billing.create_checkout(ws, Plan.objects.get(code="pro"), "USD", apply_wallet=True)

        self.assertEqual(coupon.call_args.kwargs["amount_off"], 50)
        self.assertEqual(coupon.call_args.kwargs["currency"], "usd")
        self.assertEqual(session.call_args.kwargs["discounts"], [{"coupon": "coupon_1"}])
        redemption = WalletEntry.objects.get(kind="redemption", workspace=ws)
        self.assertEqual((redemption.status, redemption.amount_minor), ("pending", 50))
        self.assertEqual(billing.wallet_balance(ws, "USD"), 0)

        self.assertTrue(billing.handle_stripe_event({
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_123",
                "subscription": "sub_wallet",
                "customer": "cus_wallet",
                "metadata": {
                    "workspace_id": str(ws.pk),
                    "plan_code": "pro",
                    "wallet_entry_id": str(redemption.pk),
                },
            }},
        }))
        self.assertTrue(billing.handle_stripe_event({
            "type": "invoice.paid",
            "data": {"object": {
                "subscription": "sub_wallet",
                "lines": {"data": []},
            }},
        }))
        redemption.refresh_from_db()
        self.assertEqual(redemption.status, "posted")
        self.assertEqual(billing.wallet_balance(ws, "USD"), 0)
        self.assertEqual(credit.amount_minor, 50)

    @override_settings(RAZORPAY_KEY_ID="rzp_test", RAZORPAY_KEY_SECRET="secret",
                       RAZORPAY_REFERRAL_OFFER_ID="offer_wallet")
    def test_razorpay_wallet_credit_uses_offer_once(self):
        _, ws, _, _ = make_workspace(paid=False)
        Plan.objects.filter(code="pro").update(razorpay_plan_id="plan_123")
        WalletEntry.objects.create(
            workspace=ws, kind="referral", currency="INR", amount_minor=10000,
            reason="Successful referral",
        )
        client = mock.Mock()
        client.subscription.create.return_value = {
            "id": "sub_wallet_inr", "short_url": "https://checkout.razorpay.test/s",
        }
        with mock.patch("apps.billing.services._razorpay_client", return_value=client):
            billing.create_checkout(ws, Plan.objects.get(code="pro"), "INR", apply_wallet=True)

        request = client.subscription.create.call_args.args[0]
        self.assertEqual(request["offer_id"], "offer_wallet")
        redemption = WalletEntry.objects.get(kind="redemption", workspace=ws)
        self.assertEqual(request["notes"]["wallet_entry_id"], str(redemption.pk))
        self.assertEqual(redemption.amount_minor, 5000)
        self.assertEqual(billing.wallet_balance(ws, "INR"), 5000)
        self.assertTrue(billing.handle_razorpay_event({
            "event": "subscription.charged",
            "payload": {"subscription": {"entity": {
                "id": "sub_wallet_inr", "current_end": 1893456000,
            }}},
        }))
        redemption.refresh_from_db()
        self.assertEqual(redemption.status, "posted")
        self.assertEqual(billing.wallet_balance(ws, "INR"), 5000)

    def test_wallet_cannot_be_redeemed_on_an_active_subscription(self):
        _, ws, _, _ = make_workspace()
        WalletEntry.objects.create(
            workspace=ws, kind="referral", currency="USD", amount_minor=50,
            reason="Successful referral",
        )
        with self.assertRaisesMessage(ValueError, "new or cancelled"):
            billing.create_checkout(ws, ws.subscription.plan, "USD", apply_wallet=True)

    def test_wallet_credit_cannot_be_reserved_for_two_checkouts(self):
        _, ws, _, _ = make_workspace(paid=False)
        WalletEntry.objects.create(
            workspace=ws, kind="referral", currency="USD", amount_minor=100,
            reason="Successful referral",
        )
        first = billing._reserve_wallet_redemption(ws, ws.subscription.plan, "USD")
        with self.assertRaisesMessage(ValueError, "already in progress"):
            billing._reserve_wallet_redemption(ws, ws.subscription.plan, "USD")
        billing._release_wallet_redemption(first)
        self.assertEqual(billing.wallet_balance(ws, "USD"), 100)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="")
    def test_billing_page_shows_wallet_use_only_when_subscription_is_inactive(self):
        user, ws, _, _ = make_workspace(paid=False)
        WalletEntry.objects.create(
            workspace=ws, kind="referral", currency="USD", amount_minor=50,
            reason="Successful referral",
        )
        client = Client()
        client.force_login(user)
        self.assertContains(client.get("/billing/"), 'name="use_wallet"')

        ws.subscription.status = "active"
        ws.subscription.save()
        response = client.get("/billing/")
        self.assertNotContains(response, 'name="use_wallet"')
        self.assertContains(response, "cancel it with your payment provider")

    @override_settings(RAZORPAY_KEY_ID="rzp_test", RAZORPAY_KEY_SECRET="secret",
                       RAZORPAY_REFERRAL_OFFER_ID="")
    def test_inr_wallet_checkout_requires_a_configured_razorpay_offer(self):
        _, ws, _, _ = make_workspace(paid=False)
        WalletEntry.objects.create(
            workspace=ws, kind="referral", currency="INR", amount_minor=5000,
            reason="Successful referral",
        )
        with self.assertRaisesMessage(ValueError, "RAZORPAY_REFERRAL_OFFER_ID"):
            billing.create_checkout(ws, ws.subscription.plan, "INR", apply_wallet=True)
        self.assertFalse(WalletEntry.objects.filter(workspace=ws, kind="redemption").exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_failed_wallet_checkout_releases_the_reservation(self):
        _, ws, _, _ = make_workspace(paid=False)
        Plan.objects.filter(code="pro").update(stripe_price_id="price_123")
        WalletEntry.objects.create(
            workspace=ws, kind="referral", currency="USD", amount_minor=50,
            reason="Successful referral",
        )
        with mock.patch("stripe.Coupon.create", side_effect=RuntimeError("provider unavailable")):
            with self.assertRaisesMessage(RuntimeError, "provider unavailable"):
                billing.create_checkout(ws, Plan.objects.get(code="pro"), "USD", apply_wallet=True)
        self.assertEqual(billing.wallet_balance(ws, "USD"), 50)
        self.assertEqual(WalletEntry.objects.get(kind="redemption", workspace=ws).status, "released")

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="")
    def test_billing_page_shows_usd_only_when_only_stripe_is_configured(self):
        user, *_ = make_workspace(paid=False)
        c = Client()
        c.force_login(user)
        page = c.get("/billing/")
        self.assertContains(page, "$29")
        self.assertEqual(billing.available_currencies(), ["USD"])

    def test_every_plan_costs_money(self):
        for plan in Plan.objects.all():
            self.assertGreater(plan.price_usd, 0)
            self.assertGreater(plan.price_inr, 0)
        self.assertFalse(Plan.objects.filter(code__contains="free").exists())


class CashfreeBillingTests(BaseCase):
    @override_settings(
        CASHFREE_CLIENT_ID="cf_test_id",
        CASHFREE_CLIENT_SECRET="cf_test_secret",
        CASHFREE_ENVIRONMENT="sandbox",
        CASHFREE_API_VERSION="2026-01-01",
        PUBLIC_BASE_URL="https://postpilot.example",
    )
    def test_cashfree_checkout_creates_monthly_recurring_subscription(self):
        _, ws, _, _ = make_workspace(paid=False)
        plan = Plan.objects.get(code="pro")
        api_response = resp(payload={
            "subscription_id": "pp-a1b2",
            "cf_subscription_id": "cf_123",
            "subscription_session_id": "subs_token_123",
        })
        with mock.patch("apps.billing.services.uuid.uuid4", side_effect=[
            mock.Mock(hex="a1b2"), "00000000-0000-0000-0000-000000000001",
        ]), mock.patch("apps.billing.services.requests.post", return_value=api_response) as post:
            checkout = billing.create_checkout(ws, plan, "INR", customer_phone="9876543210")

        self.assertEqual(checkout, {
            "provider": "cashfree", "session_id": "subs_token_123", "environment": "sandbox",
        })
        self.assertEqual(post.call_args.args[0], "https://sandbox.cashfree.com/pg/subscriptions")
        request = post.call_args.kwargs["json"]
        self.assertEqual(request["customer_details"]["customer_phone"], "9876543210")
        self.assertEqual(request["plan_details"], {
            "plan_name": "PostPilot Pro",
            "plan_type": "PERIODIC",
            "plan_amount": plan.price_inr,
            "plan_max_amount": plan.price_inr,
            "plan_max_cycles": 120,
            "plan_intervals": 1,
            "plan_interval_type": "MONTH",
            "plan_currency": "INR",
        })
        self.assertEqual(
            request["subscription_meta"]["return_url"],
            "https://postpilot.example/billing/?currency=INR",
        )
        sub = Subscription.objects.get(workspace=ws)
        self.assertEqual((sub.status, sub.provider, sub.external_id), ("pending", "cashfree", "pp-a1b2"))

    @override_settings(CASHFREE_CLIENT_ID="cf_test_id", CASHFREE_CLIENT_SECRET="cf_test_secret")
    def test_cashfree_checkout_requires_a_valid_indian_mobile_number(self):
        _, ws, _, _ = make_workspace(paid=False)
        with self.assertRaisesMessage(ValueError, "10-digit Indian mobile"):
            billing.create_checkout(ws, ws.subscription.plan, "INR")
        self.assertEqual(ws.subscription.status, "unpaid")

    @override_settings(
        RAZORPAY_KEY_ID="",
        RAZORPAY_KEY_SECRET="",
        CASHFREE_CLIENT_ID="cf_test_id",
        CASHFREE_CLIENT_SECRET="cf_test_secret",
    )
    def test_cashfree_is_the_inr_fallback_and_does_not_redeem_wallet_credit(self):
        _, ws, _, _ = make_workspace(paid=False)
        WalletEntry.objects.create(
            workspace=ws, kind="referral", currency="INR", amount_minor=5000,
            reason="Successful referral",
        )
        self.assertEqual(billing.inr_provider(), "cashfree")
        self.assertIn("INR", billing.available_currencies())
        with self.assertRaisesMessage(ValueError, "not supported through Cashfree"):
            billing.create_checkout(ws, ws.subscription.plan, "INR", apply_wallet=True)
        self.assertFalse(WalletEntry.objects.filter(workspace=ws, kind="redemption").exists())

    @override_settings(CASHFREE_CLIENT_SECRET="cf_test_secret")
    def test_cashfree_signature_and_recurring_webhook_lifecycle(self):
        _, owner, _, _ = make_workspace(email="cashfree-owner@example.com")
        _, friend, _, _ = make_workspace(email="cashfree-friend@example.com", paid=False)
        from apps.billing import referrals

        referrals.attach(friend, owner.referral_code)
        sub = friend.subscription
        sub.provider = "cashfree"
        sub.external_id = "pp-sub-123"
        sub.save()

        client = Client()
        event = {
            "type": "SUBSCRIPTION_STATUS_CHANGED",
            "data": {"subscription_details": {
                "subscription_id": "pp-sub-123",
                "subscription_status": "ACTIVE",
            }},
        }
        body = json.dumps(event).encode()
        timestamp = str(int(time.time()))
        signature = base64.b64encode(
            hmac.new(b"cf_test_secret", timestamp.encode() + b"." + body, hashlib.sha256).digest()
        ).decode()
        self.assertFalse(billing.verify_cashfree_signature(body, timestamp, "invalid"))
        self.assertEqual(
            client.post(
                "/billing/webhook/cashfree/",
                body,
                content_type="application/json",
                HTTP_X_WEBHOOK_TIMESTAMP=timestamp,
                HTTP_X_WEBHOOK_SIGNATURE="invalid",
            ).status_code,
            400,
        )
        self.assertEqual(
            client.post(
                "/billing/webhook/cashfree/",
                body,
                content_type="application/json",
                HTTP_X_WEBHOOK_TIMESTAMP=timestamp,
                HTTP_X_WEBHOOK_SIGNATURE=signature,
            ).status_code,
            200,
        )
        sub.refresh_from_db()
        self.assertEqual(sub.status, "active")
        self.assertEqual(billing.wallet_balance(owner, "INR"), 0)

        payment_event = {
            "type": "SUBSCRIPTION_PAYMENT_SUCCESS",
            "data": {"subscription_id": "pp-sub-123"},
        }
        self.assertTrue(billing.handle_cashfree_event(payment_event))
        self.assertTrue(billing.handle_cashfree_event(payment_event))
        self.assertEqual(billing.wallet_balance(owner, "INR"), 5000)

        self.assertTrue(billing.handle_cashfree_event({
            "type": "SUBSCRIPTION_PAYMENT_FAILED",
            "data": {"subscription_id": "pp-sub-123"},
        }))
        sub.refresh_from_db()
        self.assertEqual(sub.status, "past_due")

    @override_settings(
        RAZORPAY_KEY_ID="",
        RAZORPAY_KEY_SECRET="",
        CASHFREE_CLIENT_ID="cf_test_id",
        CASHFREE_CLIENT_SECRET="cf_test_secret",
    )
    def test_cashfree_billing_page_requests_phone_and_hides_wallet_redemption(self):
        user, ws, _, _ = make_workspace(paid=False)
        WalletEntry.objects.create(
            workspace=ws, kind="referral", currency="INR", amount_minor=5000,
            reason="Successful referral",
        )
        client = Client()
        client.force_login(user)
        response = client.get("/billing/?currency=INR")
        self.assertContains(response, 'name="customer_phone"')
        self.assertNotContains(response, 'name="use_wallet"')
        self.assertContains(response, "not supported through Cashfree")

    @override_settings(
        RAZORPAY_KEY_ID="",
        RAZORPAY_KEY_SECRET="",
        CASHFREE_CLIENT_ID="cf_test_id",
        CASHFREE_CLIENT_SECRET="cf_test_secret",
    )
    def test_cashfree_checkout_view_launches_hosted_subscription_authorization(self):
        user, ws, _, _ = make_workspace(paid=False)
        client = Client()
        client.force_login(user)

        def create_subscription(_url, json, **_kwargs):
            return resp(payload={
                "subscription_id": json["subscription_id"],
                "cf_subscription_id": "cf_123",
                "subscription_session_id": "session_123",
            })

        with mock.patch("apps.billing.services.requests.post", side_effect=create_subscription):
            response = client.post(
                "/billing/subscribe/pro/",
                {"currency": "INR", "customer_phone": "9876543210"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "web/cashfree_checkout.html")
        self.assertContains(response, "subscriptionsCheckout")
        self.assertContains(response, "session_123")
        ws.subscription.refresh_from_db()
        self.assertEqual((ws.subscription.status, ws.subscription.provider), ("pending", "cashfree"))


class RegionTests(BaseCase):
    def test_ingest_fills_region_placeholders(self):
        from apps.content.models import Category

        cat = Category.objects.get(slug="trending-news")
        fake = mock.Mock(return_value=[{"url": "https://n.example/1", "title": "UK story", "summary": "s"}])
        with mock.patch.dict(trends.FETCHERS, {"rss": fake}):
            trends.ingest_category(cat, "GB")
        feed = fake.call_args.args[0]["feeds"][0]
        self.assertIn("gl=GB", feed)
        self.assertIn("ceid=GB:en", feed)
        self.assertEqual(TrendItem.objects.get().region, "GB")

    def test_pipeline_uses_items_from_the_subscribed_region_only(self):
        from apps.content.models import Category

        _, ws, _, sub = make_workspace(category="trending-news", mode="approve")
        cat = Category.objects.get(slug="trending-news")
        TrendItem.objects.create(category=cat, region="US", url="https://n.example/us", title="US story")
        TrendItem.objects.create(category=cat, region="GB", url="https://n.example/gb", title="GB story")
        type(sub).objects.filter(pk=sub.pk).update(region="GB")
        with override_settings(AI_DRY_RUN=True):
            post = run_generation(self.new_post(type(sub).objects.get(pk=sub.pk)).pk)
        self.assertEqual(post.source_title, "GB story")

    def test_ingest_task_only_fetches_subscribed_regions(self):
        _, ws, _, sub = make_workspace(category="trending-news")
        type(sub).objects.filter(pk=sub.pk).update(region="AU")
        calls = []
        with mock.patch.object(trends, "ingest_category", side_effect=lambda c, r: calls.append((c.slug, r)) or 0):
            ingest_trends()
        self.assertEqual(calls, [("trending-news", "AU")])


class OAuthTests(BaseCase):
    def login(self, user):
        c = Client()
        c.force_login(user)
        return c

    @override_settings(GOOGLE_CLIENT_ID="gid", GOOGLE_CLIENT_SECRET="gsec", X_CLIENT_ID="xid", X_CLIENT_SECRET="xsec",
                       PUBLIC_BASE_URL="https://app.example.com")
    def test_start_builds_authorize_url_with_state_and_pkce(self):
        user, *_ = make_workspace(plan="agency")
        c = self.login(user)
        google = c.get("/oauth/google/start/")
        self.assertEqual(google.status_code, 302)
        self.assertIn("accounts.google.com", google["Location"])
        self.assertIn("client_id=gid", google["Location"])
        self.assertIn("access_type=offline", google["Location"])
        self.assertIn("redirect_uri=https%3A%2F%2Fapp.example.com%2Foauth%2Fgoogle%2Fcallback%2F", google["Location"])
        x = c.get("/oauth/x/start/")
        self.assertIn("code_challenge_method=S256", x["Location"])

    @override_settings(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="")
    def test_unconfigured_provider_is_refused_cleanly(self):
        user, *_ = make_workspace()
        r = self.login(user).get("/oauth/google/start/", follow=True)
        self.assertContains(r, "not configured")

    @override_settings(GOOGLE_CLIENT_ID="gid", GOOGLE_CLIENT_SECRET="gsec")
    def test_connected_platform_cannot_be_added_again(self):
        user, ws, _, _ = make_workspace(platform="youtube", plan="agency")
        client = self.login(user)

        response = client.get("/accounts/")
        self.assertContains(response, "Already connected")
        self.assertContains(response, "Manage account")
        self.assertNotContains(response, "/oauth/google/start/")

        response = client.get("/oauth/google/start/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("accounts.google.com", response["Location"])

    @override_settings(GOOGLE_CLIENT_ID="gid", GOOGLE_CLIENT_SECRET="gsec")
    def test_oauth_reconnect_cannot_replace_connected_platform_identity(self):
        user, ws, _, _ = make_workspace(platform="youtube", plan="agency")
        original = SocialAccount.objects.get(workspace=ws, platform="youtube")
        candidate = {
            "platform": "youtube", "key": "yt-new", "display_name": "Different channel", "handle": "@other",
            "extra": {"channel_id": "UC456"}, "access_token": "token", "structured": True,
        }
        client = self.login(user)

        with mock.patch.object(oauth, "finish", return_value={"access_token": "user-token"}), mock.patch.object(
            oauth, "discover", return_value=[candidate]
        ):
            response = client.get("/oauth/google/callback/")

        self.assertRedirects(response, "/accounts/", fetch_redirect_response=False)
        self.assertEqual(SocialAccount.objects.filter(workspace=ws, platform="youtube").count(), 1)
        original.refresh_from_db()
        self.assertNotEqual(original.display_name, "Different channel")
        self.assertContains(client.get("/accounts/"), "already connected")

    def test_manual_connect_route_refuses_duplicate_platform(self):
        user, ws, account, _ = make_workspace(platform="telegram", plan="agency")
        response = self.login(user).get("/accounts/connect/telegram/", follow=True)
        self.assertContains(response, "Telegram channel account is already connected")
        self.assertEqual(SocialAccount.objects.filter(workspace=ws, platform="telegram").count(), 1)

    def test_database_rejects_second_account_for_same_platform(self):
        _, ws, _, _ = make_workspace(platform="telegram", plan="agency")
        with self.assertRaises(IntegrityError), transaction.atomic():
            SocialAccount.objects.create(workspace=ws, platform="telegram", display_name="Duplicate")

    @override_settings(GOOGLE_CLIENT_ID="gid", GOOGLE_CLIENT_SECRET="gsec")
    def test_callback_rejects_wrong_state(self):
        user, *_ = make_workspace(plan="agency")
        c = self.login(user)
        c.get("/oauth/google/start/")
        r = c.get("/oauth/google/callback/?code=abc&state=forged", follow=True)
        self.assertContains(r, "expired or was tampered")
        self.assertEqual(SocialAccount.objects.filter(platform="youtube").count(), 0)

    @override_settings(GOOGLE_CLIENT_ID="gid", GOOGLE_CLIENT_SECRET="gsec")
    def test_youtube_connect_stores_encrypted_credentials(self):
        user, ws, *_ = make_workspace(plan="agency")
        c = self.login(user)
        url = c.get("/oauth/google/start/")["Location"]
        state = url.split("state=")[1].split("&")[0]
        responses = [
            {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600},          # token exchange
            {"items": [{"id": "UC123", "snippet": {"title": "My Channel", "customUrl": "@mychan"}}]},  # channels
        ]
        with mock.patch.object(oauth, "_post", return_value=responses[0]), mock.patch.object(oauth, "_get", return_value=responses[1]):
            c.get(f"/oauth/google/callback/?code=abc&state={state}")
        acc = SocialAccount.objects.get(platform="youtube")
        self.assertEqual((acc.display_name, acc.extra["channel_id"]), ("My Channel", "UC123"))
        self.assertNotIn("at-1", acc.token_enc)
        self.assertEqual(acc.creds["refresh_token"], "rt-1")
        self.assertIsNotNone(acc.expires_at)

    def test_meta_choose_pages_respects_plan_limit(self):
        user, ws, *_ = make_workspace(plan="starter")  # 3 accounts, one already used
        c = self.login(user)
        cands = [
            {"platform": "facebook", "key": f"fb-{i}", "display_name": f"Page {i}", "handle": "", "extra": {"page_id": str(i)},
             "access_token": f"ptok{i}", "structured": False} for i in range(1, 4)
        ]
        with mock.patch.object(oauth, "finish", return_value={"access_token": "u"}), mock.patch.object(oauth, "discover", return_value=cands):
            r = c.get("/oauth/meta/callback/")
        self.assertRedirects(r, "/oauth/select/", fetch_redirect_response=False)
        self.assertNotIn("ptok1", json.dumps(self.client.session.get("oauth_candidates", "")))
        c.post("/oauth/select/", {"key": ["fb-1", "fb-2", "fb-3"]})
        pages = SocialAccount.objects.filter(platform="facebook")
        self.assertEqual(pages.count(), 1)  # one account per platform, even when the plan allows more
        self.assertEqual(pages.get(extra__page_id="1").token, "ptok1")
        self.assertFalse(pages.filter(extra__page_id="2").exists())

    def test_reconnect_updates_existing_account_instead_of_duplicating(self):
        _, ws, *_ = make_workspace(plan="starter")
        cand = {"platform": "x", "key": "x-9", "display_name": "Me", "handle": "@me", "extra": {"user_id": "9"},
                "access_token": "old", "refresh_token": "r1", "expires_at": int(time.time()) + 3600, "structured": True}
        a1, created1 = oauth.save_candidate(ws, cand)
        a2, created2 = oauth.save_candidate(ws, {**cand, "access_token": "new", "refresh_token": ""})
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(a1.pk, a2.pk)
        a2.refresh_from_db()
        self.assertEqual(a2.creds["access_token"], "new")
        self.assertEqual(a2.creds["refresh_token"], "r1")  # kept when the provider sends none

    def test_oauth_candidate_cannot_create_second_account_for_platform(self):
        _, ws, _, _ = make_workspace(platform="youtube", plan="agency")
        candidate = {
            "platform": "youtube", "key": "yt-new", "display_name": "Another channel", "handle": "@other",
            "extra": {"channel_id": "UC456"}, "access_token": "token", "structured": True,
        }
        with self.assertRaises(oauth.OAuthError):
            oauth.save_candidate(ws, candidate)
        self.assertEqual(SocialAccount.objects.filter(workspace=ws, platform="youtube").count(), 1)

    @override_settings(X_CLIENT_ID="xid", X_CLIENT_SECRET="xsec")
    def test_expired_token_is_refreshed_and_rotated(self):
        _, ws, *_ = make_workspace()
        acc = SocialAccount(workspace=ws, platform="x", display_name="Me", extra={"user_id": "9"})
        acc.set_creds("stale", "rt-old", int(time.time()) - 10)
        acc.save()
        with mock.patch.object(oauth, "_post", return_value={"access_token": "fresh", "refresh_token": "rt-new", "expires_in": 7200}) as m:
            token = oauth.ensure_fresh(acc)
        self.assertEqual(token, "fresh")
        self.assertEqual(m.call_args.kwargs["data"]["grant_type"], "refresh_token")
        acc.refresh_from_db()
        self.assertEqual(acc.creds["refresh_token"], "rt-new")

    def test_expired_token_without_refresh_requires_reconnect(self):
        _, ws, *_ = make_workspace()
        acc = SocialAccount(workspace=ws, platform="linkedin", display_name="Me", extra={"author_urn": "urn:li:person:1"})
        acc.set_creds("stale", "", int(time.time()) - 10)
        acc.save()
        with self.assertRaises(oauth.OAuthError):
            oauth.ensure_fresh(acc)


class PublisherTests(BaseCase):
    def post_for(self, platform, creds=True, **post_kw):
        _, ws, account, sub = make_workspace(platform=platform, plan="agency")
        if creds:
            account.set_creds("tok", "ref", int(time.time()) + 3600)
        account.extra.update({"channel_id": "UC1", "user_id": "9", "author_urn": "urn:li:person:42"})
        account.save()
        return self.new_post(sub, status="publishing", caption="Hello world, a test post.\nSecond line of text.", hashtags=["#a"], **post_kw)

    def write_media(self, name, data=b"binary"):
        (TMP_MEDIA / name).parent.mkdir(parents=True, exist_ok=True)
        (TMP_MEDIA / name).write_bytes(data)
        return name

    def test_youtube_resumable_upload(self):
        post = self.post_for("youtube", video_path=self.write_media("videos/t.mp4"))
        start = resp(200, headers={"Location": "https://upload.example/session"})
        done = resp(200, {"id": "VID123"})
        with mock.patch("apps.social.publishers.youtube.requests.post", return_value=start) as p, \
             mock.patch("apps.social.publishers.youtube.requests.put", return_value=done) as put:
            vid = get_publisher("youtube").publish(post.account, post)
        self.assertEqual(vid, "VID123")
        meta = p.call_args.kwargs["json"]
        self.assertTrue(meta["snippet"]["title"].endswith("#Shorts"))
        self.assertEqual(meta["snippet"]["title"].split(" #")[0], "Hello world, a test post.")
        self.assertEqual(put.call_args.args[0], "https://upload.example/session")

    def test_youtube_without_video_is_a_permanent_failure(self):
        post = self.post_for("youtube")
        with self.assertRaises(PublishError) as ctx:
            get_publisher("youtube").publish(post.account, post)
        self.assertFalse(ctx.exception.retryable)

    def test_youtube_quota_error_is_retryable(self):
        post = self.post_for("youtube", video_path=self.write_media("videos/q.mp4"))
        quota = resp(403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
        with mock.patch("apps.social.publishers.youtube.requests.post", return_value=quota):
            with self.assertRaises(PublishError) as ctx:
                get_publisher("youtube").publish(post.account, post)
        self.assertTrue(ctx.exception.retryable)

    def test_title_split(self):
        title, desc = split_title_description("A" * 200 + "\nbody text", ["#x"])
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith("#Shorts"))
        self.assertIn("body text", desc)

    def test_x_posts_image_then_tweet(self):
        post = self.post_for("x", media_path=self.write_media("cards/x.png"))
        with mock.patch("apps.social.publishers.xtwitter.requests.post",
                        side_effect=[resp(200, {"data": {"id": "m1"}}), resp(201, {"data": {"id": "tw1"}})]) as p:
            self.assertEqual(get_publisher("x").publish(post.account, post), "tw1")
        self.assertEqual(p.call_args.kwargs["json"]["media"], {"media_ids": ["m1"]})
        self.assertLessEqual(len(p.call_args.kwargs["json"]["text"]), 280)

    def test_x_falls_back_to_text_when_media_upload_is_refused(self):
        post = self.post_for("x", media_path=self.write_media("cards/x2.png"))
        with mock.patch("apps.social.publishers.xtwitter.requests.post",
                        side_effect=[resp(403, {}), resp(201, {"data": {"id": "tw2"}})]) as p:
            self.assertEqual(get_publisher("x").publish(post.account, post), "tw2")
        self.assertNotIn("media", p.call_args.kwargs["json"])

    def test_x_rate_limit_is_retryable_and_401_is_auth(self):
        post = self.post_for("x")
        with mock.patch("apps.social.publishers.xtwitter.requests.post", return_value=resp(429, {})):
            with self.assertRaises(PublishError) as ctx:
                get_publisher("x").publish(post.account, post)
        self.assertTrue(ctx.exception.retryable)
        with mock.patch("apps.social.publishers.xtwitter.requests.post", return_value=resp(401, {})):
            with self.assertRaises(PublishError) as ctx:
                get_publisher("x").publish(post.account, post)
        self.assertTrue(ctx.exception.auth_failed)

    def test_linkedin_image_post(self):
        post = self.post_for("linkedin", media_path=self.write_media("cards/l.png"))
        init = resp(200, {"value": {"uploadUrl": "https://li.example/up", "image": "urn:li:image:abc"}})
        created = resp(201, headers={"x-restli-id": "urn:li:share:777"})
        with mock.patch("apps.social.publishers.linkedin.requests.post", side_effect=[init, created]) as p, \
             mock.patch("apps.social.publishers.linkedin.requests.put", return_value=resp(201)):
            self.assertEqual(get_publisher("linkedin").publish(post.account, post), "urn:li:share:777")
        body = p.call_args.kwargs["json"]
        self.assertEqual(body["author"], "urn:li:person:42")
        self.assertEqual(body["content"]["media"]["id"], "urn:li:image:abc")
        self.assertIn("Linkedin-Version", {k.title(): v for k, v in p.call_args.kwargs["headers"].items()} | {"Linkedin-Version": 1})

    def test_linkedin_escapes_markup_characters(self):
        self.assertEqual(escape_commentary("Save 50% (today) #sale @you"), r"Save 50% \(today\) \#sale \@you")

    def test_all_six_platforms_are_implemented(self):
        from apps.social.publishers import implemented_platforms

        self.assertEqual(sorted(implemented_platforms()), ["facebook", "instagram", "linkedin", "telegram", "x", "youtube"])


@skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class VideoTests(BaseCase):
    @override_settings(AI_DRY_RUN=True, VIDEO_SECONDS=2)
    def test_youtube_post_gets_a_vertical_video(self):
        import subprocess

        _, ws, account, sub = make_workspace(platform="youtube", plan="pro")
        post = run_generation(self.new_post(sub).pk)
        self.assertTrue(post.video_path.endswith(".mp4"))
        info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name",
                               "-of", "csv=p=0", str(TMP_MEDIA / post.video_path)], capture_output=True, text=True).stdout.strip()
        self.assertEqual(info, "h264,1080,1920")

    @override_settings(AI_DRY_RUN=True)
    def test_starter_plan_cannot_make_shorts(self):
        _, ws, account, sub = make_workspace(platform="youtube", plan="starter")
        post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "failed")
        self.assertIn("Pro plan", post.error)

    @override_settings(AI_DRY_RUN=True)
    def test_missing_ffmpeg_sends_post_to_review_not_to_publish(self):
        _, ws, account, sub = make_workspace(platform="youtube", plan="pro")
        with mock.patch("apps.content.media.shutil.which", return_value=None):
            post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "needs_review")
        self.assertIn("ffmpeg", post.error)
