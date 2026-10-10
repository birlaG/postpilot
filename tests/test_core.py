import hashlib
import hmac
import json
import shutil
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.billing import services as billing
from apps.billing.models import Plan, Subscription
from apps.content import ai, publishing, safety, scheduling
from apps.content.models import AccountCategory, Category, GenerationRun, Post
from apps.content.pipeline import run_generation
from apps.social.models import SocialAccount
from apps.workspaces.models import BrandKit, Member, Workspace

User = get_user_model()
TMP_MEDIA = Path(tempfile.mkdtemp(prefix="postpilot-test-media-"))


def make_workspace(email="a@example.com", platform="telegram", category="motivation", mode="auto", paid=True, plan="pro"):
    user = User.objects.create_user(username=email, email=email, password="pw-12345-long")
    ws = Workspace.objects.create(name=f"WS {email}", owner=user, timezone="Asia/Kolkata")
    Member.objects.create(workspace=ws, user=user, role="owner")
    BrandKit.objects.create(workspace=ws)
    billing.start_unpaid(ws)
    if paid:
        Subscription.objects.filter(workspace=ws).update(status="active", plan=Plan.objects.get(code=plan))
        ws = Workspace.objects.get(pk=ws.pk)
    account = SocialAccount(workspace=ws, platform=platform, display_name="Chan", handle="@chan", extra={"chat_id": "@chan"})
    account.token = "secret-token"
    account.save()
    sub = AccountCategory.objects.create(
        account=account, category=Category.objects.get(slug=category), post_times=["09:00"], publish_mode=mode
    )
    return user, ws, account, sub


def fake_call_json(write_caption="A fresh original thought about starting small today.", verdict="pass"):
    calls = []

    def _call(stage, model, system, user, max_tokens=1200):
        calls.append(stage)
        usage = {"model": model, "tokens_in": 1000, "tokens_out": 200, "cost_usd": Decimal("0.004")}
        if stage == "write":
            return {"caption": write_caption, "hashtags": ["#grow", "#daily", "#x", "#y", "#z"], "image_text": "Start small", "alt_text": "card", "claims": []}, usage
        if stage == "review":
            return {"verdict": verdict, "reasons": ["ok" if verdict == "pass" else "bad claim"], "risk_flags": []}, usage
        return {"pick": 1}, usage

    _call.calls = calls
    return _call


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class BaseCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_plans", verbosity=0)
        call_command("seed_categories", verbosity=0)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def new_post(self, sub, **kw):
        defaults = dict(
            workspace=sub.account.workspace, account=sub.account, subscription=sub, category=sub.category,
            slot_key=f"t-{Post.objects.count()}", status="generating", scheduled_at=timezone.now(),
        )
        defaults.update(kw)
        return Post.objects.create(**defaults)


class PipelineTests(BaseCase):
    @override_settings(AI_DRY_RUN=True)
    def test_dry_run_is_visible_but_never_publishable(self):
        _, ws, _, sub = make_workspace()
        post = run_generation(self.new_post(sub).pk)
        self.assertTrue(post.dry_run)
        self.assertEqual(post.status, "pending_approval")  # not queued even though mode is auto
        self.assertTrue((TMP_MEDIA / post.media_path).exists())
        self.assertFalse(post.can_approve)
        self.assertNotIn(post.pk, publishing.due_post_ids(timezone.now() + timedelta(days=1)))

    @override_settings(AI_DRY_RUN=False)
    def test_low_risk_auto_mode_is_queued_and_costs_recorded(self):
        _, ws, _, sub = make_workspace()
        fake = fake_call_json()
        with mock.patch.object(ai, "call_json", fake):
            post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "queued")
        self.assertFalse(post.dry_run)
        self.assertEqual(len(post.hashtags), 3)  # trimmed to the telegram limit
        self.assertEqual(fake.calls, ["write", "review"])
        self.assertEqual(GenerationRun.objects.filter(post=post).count(), 2)
        self.assertEqual(billing.posts_used(ws), 1)

    @override_settings(AI_DRY_RUN=False)
    def test_high_risk_category_never_auto_publishes(self):
        _, _, _, sub = make_workspace(category="spiritual", mode="auto")
        with mock.patch.object(ai, "call_json", fake_call_json("A gentle thought about patience for your morning.")):
            post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "pending_approval")
        self.assertEqual(sub.effective_mode, "approve")

    @override_settings(AI_DRY_RUN=False)
    def test_failed_review_retries_then_needs_review(self):
        _, _, _, sub = make_workspace()
        fake = fake_call_json(verdict="fail")
        with mock.patch.object(ai, "call_json", fake):
            post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "needs_review")
        self.assertEqual(fake.calls.count("write"), 3)

    @override_settings(AI_DRY_RUN=False)
    def test_banned_word_blocks_publishing(self):
        _, ws, _, sub = make_workspace()
        BrandKit.objects.filter(workspace=ws).update(banned_words="forbidden")
        fake = fake_call_json("This forbidden word must never be posted anywhere at all.")
        with mock.patch.object(ai, "call_json", fake):
            post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "needs_review")
        self.assertNotIn("review", fake.calls)  # static check stops it before the reviewer

    @override_settings(AI_DRY_RUN=False)
    def test_duplicate_caption_is_rejected(self):
        _, _, account, sub = make_workspace()
        same = "Exactly the same caption text used twice in a row."
        self.new_post(sub, status="published", caption=same, content_hash=safety.content_hash(same), slot_key="old")
        with mock.patch.object(ai, "call_json", fake_call_json(same)):
            post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "needs_review")

    def test_monthly_limit_blocks_generation(self):
        _, ws, _, sub = make_workspace()
        Plan.objects.filter(pk=ws.subscription.plan_id).update(posts_per_month=1)
        billing.record_usage(ws)
        post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "failed")
        self.assertIn("limit", post.error)

    def test_unpaid_workspace_cannot_generate_or_connect(self):
        _, ws, _, sub = make_workspace(paid=False)
        self.assertFalse(billing.can_generate(ws)[0])
        self.assertFalse(billing.can_add_account(ws)[0])
        post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "failed")
        self.assertIn("plan", post.error)

    def test_extract_json_tolerates_fences_and_prose(self):
        self.assertEqual(ai.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(ai.extract_json('Sure! {"a": 2} hope that helps'), {"a": 2})
        with self.assertRaises(ai.AIError):
            ai.extract_json("no json here")


class SchedulingTests(BaseCase):
    def test_slot_planning_is_idempotent(self):
        _, _, _, sub = make_workspace()
        now = datetime(2026, 10, 9, 8, 45, tzinfo=ZoneInfo("Asia/Kolkata"))  # 15 min before the 09:00 slot
        first = scheduling.plan_slots(now)
        second = scheduling.plan_slots(now + timedelta(minutes=5))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        post = Post.objects.get(pk=first[0])
        self.assertEqual(post.slot_key, "2026-10-09T09:00")
        self.assertEqual(post.scheduled_at, datetime(2026, 10, 9, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata")))

    def test_slot_outside_window_is_ignored(self):
        make_workspace()
        now = datetime(2026, 10, 9, 14, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # 5h after the slot
        self.assertEqual(scheduling.plan_slots(now), [])

    def test_paused_subscription_is_skipped(self):
        _, _, _, sub = make_workspace()
        AccountCategory.objects.filter(pk=sub.pk).update(enabled=False)
        now = datetime(2026, 10, 9, 8, 45, tzinfo=ZoneInfo("Asia/Kolkata"))
        self.assertEqual(scheduling.plan_slots(now), [])


def tg_response(payload):
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


class PublishingTests(BaseCase):
    def queued_post(self):
        _, ws, account, sub = make_workspace()
        return self.new_post(sub, status="queued", caption="Hello world from the test suite.", hashtags=["#t"])

    def test_publish_success_text_post(self):
        post = self.queued_post()
        with mock.patch("apps.social.publishers.telegram.requests.post", return_value=tg_response({"ok": True, "result": {"message_id": 42}})) as m:
            self.assertTrue(publishing.claim_for_publishing(post.pk))
            post = publishing.publish_post(post.pk)
        self.assertEqual((post.status, post.platform_post_id), ("published", "42"))
        self.assertIn("sendMessage", m.call_args.args[0])
        self.assertEqual(m.call_args.kwargs["json"]["chat_id"], "@chan")

    def test_cannot_claim_twice(self):
        post = self.queued_post()
        self.assertTrue(publishing.claim_for_publishing(post.pk))
        self.assertFalse(publishing.claim_for_publishing(post.pk))

    def test_transient_error_retries_then_fails(self):
        post = self.queued_post()
        bad = tg_response({"ok": False, "error_code": 502, "description": "bad gateway"})
        with mock.patch("apps.social.publishers.telegram.requests.post", return_value=bad):
            for expected in ("queued", "queued", "failed"):
                Post.objects.filter(pk=post.pk).update(scheduled_at=timezone.now() - timedelta(minutes=1), status="queued")
                self.assertTrue(publishing.claim_for_publishing(post.pk))
                post = publishing.publish_post(post.pk)
                self.assertEqual(post.status, expected)
        self.assertEqual(post.attempts, 3)

    def test_auth_failure_marks_account_broken_without_retry(self):
        post = self.queued_post()
        bad = tg_response({"ok": False, "error_code": 401, "description": "Unauthorized"})
        with mock.patch("apps.social.publishers.telegram.requests.post", return_value=bad):
            publishing.claim_for_publishing(post.pk)
            post = publishing.publish_post(post.pk)
        self.assertEqual(post.status, "failed")
        post.account.refresh_from_db()
        self.assertEqual(post.account.status, "broken")

    def test_youtube_post_without_a_video_fails_cleanly(self):
        _, ws, account, sub = make_workspace(platform="youtube")
        post = self.new_post(sub, status="queued", caption="Hello from the test suite, video platform.")
        publishing.claim_for_publishing(post.pk)
        post = publishing.publish_post(post.pk)
        self.assertEqual(post.status, "failed")
        self.assertIn("video", post.error)
        self.assertEqual(post.attempts, 1)


class BillingTests(BaseCase):
    def test_token_is_encrypted_at_rest(self):
        _, _, account, _ = make_workspace()
        self.assertNotIn("secret-token", account.token_enc)
        account.refresh_from_db()
        self.assertEqual(account.token, "secret-token")

    @override_settings(RAZORPAY_WEBHOOK_SECRET="whsec")
    def test_webhook_requires_valid_signature_and_activates(self):
        _, ws, _, _ = make_workspace(paid=False)
        Subscription.objects.filter(workspace=ws).update(provider="razorpay", external_id="sub_123", status="pending")
        body = json.dumps({"event": "subscription.activated", "payload": {"subscription": {"entity": {"id": "sub_123", "current_end": 1893456000}}}}).encode()
        sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
        client = Client()
        bad = client.post("/billing/webhook/razorpay/", body, content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE="nope")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(Subscription.objects.get(workspace=ws).status, "pending")
        good = client.post("/billing/webhook/razorpay/", body, content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE=sig)
        self.assertEqual(good.status_code, 200)
        self.assertEqual(Subscription.objects.get(workspace=ws).status, "active")

    @override_settings(RAZORPAY_WEBHOOK_SECRET="whsec", BILLING_GRACE_DAYS=3)
    def test_payment_failure_starts_grace_period(self):
        _, ws, _, _ = make_workspace()
        Subscription.objects.filter(workspace=ws).update(provider="razorpay", external_id="sub_9", status="active")
        billing.handle_razorpay_event({"event": "subscription.halted", "payload": {"subscription": {"entity": {"id": "sub_9"}}}})
        sub = Subscription.objects.get(workspace=ws)
        self.assertEqual(sub.status, "past_due")
        self.assertTrue(sub.is_live())
        sub.grace_until = timezone.now() - timedelta(minutes=1)
        self.assertFalse(sub.is_live())

    def test_account_limit_enforced(self):
        _, ws, account, _ = make_workspace(plan="starter")  # starter allows 3 accounts, one exists
        self.assertTrue(billing.can_add_account(ws)[0])
        for platform in ("facebook", "youtube"):
            SocialAccount.objects.create(workspace=ws, platform=platform, display_name=platform.title())
        ok, reason = billing.can_add_account(ws)
        self.assertFalse(ok)
        self.assertIn("3 connected", reason)


class WebTests(BaseCase):
    def login(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_signup_creates_unpaid_workspace_with_region_and_timezone(self):
        c = Client()
        resp = c.post("/signup/", {"name": "My Page", "email": "new@example.com", "password": "a-long-password-1",
                                    "country": "GB", "timezone": "Europe/London"})
        self.assertRedirects(resp, "/billing/", fetch_redirect_response=False)
        ws = Workspace.objects.get(name="My Page")
        self.assertTrue(BrandKit.objects.filter(workspace=ws).exists())
        self.assertEqual((ws.country, ws.timezone), ("GB", "Europe/London"))
        self.assertEqual(ws.subscription.status, "unpaid")  # no free tier
        self.assertFalse(ws.subscription.is_live())

    def test_signup_rejects_bad_timezone(self):
        resp = Client().post("/signup/", {"name": "X", "email": "bad@example.com", "password": "a-long-password-1",
                                          "country": "US", "timezone": "Mars/Olympus"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Workspace.objects.filter(name="X").exists())

    def test_pages_require_login(self):
        for url in ("/accounts/", "/queue/", "/brand/", "/billing/", "/calendar/"):
            self.assertEqual(Client().get(url).status_code, 302, url)

    def test_all_pages_render_for_logged_in_user(self):
        user, ws, account, sub = make_workspace()
        post = self.new_post(sub, status="pending_approval", caption="A caption to review here.")
        c = self.login(user)
        for url in ("/", "/accounts/", f"/accounts/{account.pk}/", f"/subscriptions/{sub.pk}/",
                    "/queue/", "/posts/", f"/posts/{post.pk}/", "/brand/", "/billing/"):
            self.assertEqual(c.get(url).status_code, 200, url)
        self.assertEqual(c.get("/accounts/connect/telegram/").status_code, 302)

    def test_approve_moves_post_to_queue(self):
        user, ws, account, sub = make_workspace()
        post = self.new_post(sub, status="pending_approval", caption="A caption to review here.")
        c = self.login(user)
        c.post(f"/posts/{post.pk}/", {"action": "approve", "caption": "Edited caption for approval.", "hashtags": "#a #b"})
        post.refresh_from_db()
        self.assertEqual(post.status, "queued")
        self.assertEqual(post.caption, "Edited caption for approval.")
        self.assertEqual(post.hashtags, ["#a", "#b"])

    def test_dry_run_post_cannot_be_approved(self):
        user, ws, account, sub = make_workspace()
        post = self.new_post(sub, status="pending_approval", caption="Placeholder caption text.", dry_run=True)
        self.login(user).post(f"/posts/{post.pk}/", {"action": "approve", "caption": "Placeholder caption text.", "hashtags": ""})
        post.refresh_from_db()
        self.assertEqual(post.status, "pending_approval")

    def test_other_workspaces_data_is_invisible(self):
        _, _, _, sub_a = make_workspace("a@example.com")
        user_b, *_ = make_workspace("b@example.com")
        post = self.new_post(sub_a, status="pending_approval", caption="Private caption of workspace A.")
        c = self.login(user_b)
        self.assertEqual(c.get(f"/posts/{post.pk}/").status_code, 404)
        self.assertEqual(c.post(f"/posts/{post.pk}/", {"action": "reject"}).status_code, 404)
        self.assertEqual(c.get(f"/accounts/{sub_a.account_id}/").status_code, 404)

    def test_viewer_cannot_change_anything(self):
        user, ws, account, sub = make_workspace()
        Member.objects.filter(user=user).update(role="viewer")
        post = self.new_post(sub, status="pending_approval", caption="Viewer must not approve this.")
        self.login(user).post(f"/posts/{post.pk}/", {"action": "approve", "caption": "x" * 20, "hashtags": ""})
        post.refresh_from_db()
        self.assertEqual(post.status, "pending_approval")

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True, AI_DRY_RUN=True)
    def test_generate_now_button_creates_post(self):
        user, ws, account, sub = make_workspace()
        resp = self.login(user).post(f"/subscriptions/{sub.pk}/generate/")
        self.assertEqual(resp.status_code, 302)
        post = Post.objects.get(subscription=sub)
        self.assertEqual(post.status, "pending_approval")
        self.assertTrue(post.slot_key.startswith("manual-"))

    @override_settings(DEBUG=False)
    def test_dev_activate_disabled_in_production(self):
        user, *_ = make_workspace()
        resp = self.login(user).post("/billing/dev-activate/pro/")
        self.assertEqual(resp.status_code, 400)
