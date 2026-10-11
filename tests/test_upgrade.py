"""Content formats and styles, and followed podcasts / channels / sites."""
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import Client, override_settings

from apps.content import netsafe, scheduling, sources
from apps.content.models import AccountCategory, Category, FollowedSource, Post, TrendItem
from apps.content.pipeline import run_generation
from apps.social.limits import resolve_format
from apps.social.models import SocialAccount

from apps.billing import services as billing

from .test_core import BaseCase, fake_call_json, make_workspace

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Founders Talk</title>
<item><title>Interview: how she built a bakery</title><link>https://example.com/ep2</link>
<description>&lt;p&gt;A chat about starting small and hiring slowly.&lt;/p&gt;</description></item>
<item><title>Episode 1</title><link>https://example.com/ep1</link><description>Intro episode</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
<title>Tech Channel</title>
<entry><title>New phone review</title><link rel="alternate" href="https://www.youtube.com/watch?v=abc"/>
<media:group><media:description>We test the new phone for a week.</media:description></media:group></entry></feed>"""


@override_settings(AI_DRY_RUN=False)
class FormatTests(BaseCase):
    def run_with(self, platform="telegram", **sub_fields):
        user, ws, account, sub = make_workspace(platform=platform)
        AccountCategory.objects.filter(pk=sub.pk).update(**sub_fields)
        sub.refresh_from_db()
        post = self.new_post(sub)
        with mock.patch("apps.content.ai.call_json", fake_call_json()):
            return run_generation(post.pk)

    def test_text_only_has_no_image(self):
        post = self.run_with(content_format="text")
        self.assertEqual(post.status, "queued")
        self.assertEqual(post.media_path, "")

    def test_auto_keeps_image_card(self):
        self.assertTrue(self.run_with(content_format="auto").media_path)

    def test_instagram_cannot_be_text_only(self):
        self.assertEqual(resolve_format("instagram", "text"), "image")
        self.assertTrue(self.run_with(platform="instagram", content_format="text").media_path)

    def test_video_format_makes_a_clip_on_pro(self):
        post = self.run_with(platform="youtube", content_format="video")
        self.assertTrue(post.video_path, post.error)

    def test_video_needs_pro_plan(self):
        user, ws, account, sub = make_workspace(platform="youtube", plan="starter")
        AccountCategory.objects.filter(pk=sub.pk).update(content_format="video")
        sub.refresh_from_db()
        post = self.new_post(sub)
        with mock.patch("apps.content.ai.call_json", fake_call_json()):
            post = run_generation(post.pk)
        self.assertEqual(post.status, "failed")
        self.assertIn("Pro", post.error)

    def test_style_and_format_reach_the_prompt(self):
        user, ws, account, sub = make_workspace()
        AccountCategory.objects.filter(pk=sub.pk).update(content_format="text", post_style="listicle")
        sub.refresh_from_db()
        seen = []
        base = fake_call_json()

        def spy(stage, model, system, user_msg, max_tokens=1200):
            if stage == "write":
                seen.append(user_msg)
            return base(stage, model, system, user_msg, max_tokens)

        with mock.patch("apps.content.ai.call_json", spy):
            run_generation(self.new_post(sub).pk)
        self.assertIn("numbered list", seen[0])
        self.assertIn("text only", seen[0])


class FeedTests(BaseCase):
    def test_parse_rss(self):
        name, entries = sources.parse_feed(RSS)
        self.assertEqual(name, "Founders Talk")
        self.assertEqual(entries[0]["url"], "https://example.com/ep2")
        self.assertNotIn("<p>", entries[0]["summary"])

    def test_parse_atom_youtube(self):
        name, entries = sources.parse_feed(ATOM)
        self.assertEqual(name, "Tech Channel")
        self.assertEqual(entries[0]["url"], "https://www.youtube.com/watch?v=abc")
        self.assertIn("test the new phone", entries[0]["summary"])

    def test_bad_feed_and_xml_bombs_are_rejected(self):
        with self.assertRaises(sources.SourceError):
            sources.parse_feed(b"<html>not a feed")
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss><channel><title>&b;</title></channel></rss>'
        with self.assertRaises(sources.SourceError):
            sources.parse_feed(bomb)

    def test_youtube_channel_link_becomes_feed(self):
        with mock.patch("apps.content.sources.safe_get", return_value=("u", ATOM)) as sg:
            kind, name, feed, entries = sources.resolve("https://www.youtube.com/channel/UC" + "a" * 22)
        self.assertEqual((kind, feed), ("youtube", "https://www.youtube.com/feeds/videos.xml?channel_id=UC" + "a" * 22))
        sg.assert_called_with(feed)

    def test_website_page_links_to_its_feed(self):
        page = b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head></html>'
        with mock.patch("apps.content.sources.safe_get", side_effect=[("https://blog.example/", page), ("https://blog.example/feed.xml", RSS)]):
            kind, name, feed, entries = sources.resolve("https://blog.example/")
        self.assertEqual(feed, "https://blog.example/feed.xml")
        self.assertEqual(kind, "website")


class SafeFetchTests(BaseCase):
    def test_private_and_local_addresses_blocked(self):
        for url in ("http://127.0.0.1/x", "http://localhost/x", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/",
                    "ftp://example.com/x", "http://user:pw@example.com/", "http://example.com:8080/"):
            with self.assertRaises(netsafe.UnsafeURL, msg=url):
                netsafe.check_url(url)

    def test_redirect_to_private_address_is_blocked(self):
        resp = mock.MagicMock(status_code=302, headers={"Location": "http://127.0.0.1/admin"})
        resp.__enter__.return_value = resp
        with mock.patch("apps.content.netsafe.check_url", side_effect=[None, netsafe.UnsafeURL("private")]), \
                mock.patch("apps.content.netsafe.requests.get", return_value=resp):
            with self.assertRaises(netsafe.UnsafeURL):
                netsafe.safe_get("https://example.com/feed")


@override_settings(AI_DRY_RUN=False)
class FollowedPipelineTests(BaseCase):
    def follow(self, email="f@example.com"):
        user, ws, account, sub = make_workspace(email=email, category="followed-sources", mode="approve")
        src = FollowedSource.objects.create(workspace=ws, kind="podcast", name="Founders Talk", feed_url="https://example.com/feed")
        AccountCategory.objects.filter(pk=sub.pk).update(followed_source=src)
        sub.refresh_from_db()
        category = Category.objects.get(slug="followed-sources")
        for i, title in enumerate(["Newest interview", "Older episode"]):
            TrendItem.objects.create(category=category, source=src, region="", url=f"https://example.com/ep{i}", title=title, summary="notes", score=100 - i)
        return user, ws, account, sub, src

    def test_post_credits_the_show_and_links_the_episode(self):
        user, ws, account, sub, src = self.follow()
        seen = []
        base = fake_call_json()

        def spy(stage, model, system, user_msg, max_tokens=1200):
            if stage == "write":
                seen.append(user_msg)
            return base(stage, model, system, user_msg, max_tokens)

        with mock.patch("apps.content.ai.call_json", spy):
            post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "pending_approval")
        self.assertTrue(post.source_url.startswith("https://example.com/ep"))
        self.assertIn("Founders Talk", seen[0])
        self.assertIn(post.source_url, post.full_text())  # credit link is always appended

    def test_each_episode_is_used_once(self):
        user, ws, account, sub, src = self.follow()
        urls = set()
        for n in range(2):
            with mock.patch("apps.content.ai.call_json", fake_call_json(write_caption=f"Different words number {n} for the episode post.")):
                p = run_generation(self.new_post(sub).pk)
            urls.add(p.source_url)
        self.assertEqual(len(urls), 2)
        with mock.patch("apps.content.sources.refresh", return_value=0), mock.patch("apps.content.ai.call_json", fake_call_json()):
            p = run_generation(self.new_post(sub).pk)
        self.assertEqual(p.status, "failed")  # nothing new left

    def test_two_workspaces_can_follow_the_same_feed(self):
        category = Category.objects.get(slug="followed-sources")
        _, ws1, *_ = make_workspace(email="x1@example.com")
        _, ws2, *_ = make_workspace(email="x2@example.com")
        for ws in (ws1, ws2):
            src = FollowedSource.objects.create(workspace=ws, kind="podcast", name="Same", feed_url="https://example.com/same")
            TrendItem.objects.create(category=category, source=src, region="", url="https://example.com/e1", title="E1")
        self.assertEqual(TrendItem.objects.filter(url="https://example.com/e1").count(), 2)

    def test_refresh_stores_items(self):
        _, ws, *_ = make_workspace()
        src = FollowedSource.objects.create(workspace=ws, kind="podcast", name="Show", feed_url="https://example.com/feed")
        with mock.patch("apps.content.sources.safe_get", return_value=("u", RSS)):
            self.assertEqual(sources.refresh(src), 2)
            self.assertEqual(sources.refresh(src), 0)  # idempotent
        self.assertEqual(src.items.count(), 2)

    def test_refresh_records_errors_instead_of_crashing(self):
        _, ws, *_ = make_workspace()
        src = FollowedSource.objects.create(workspace=ws, kind="podcast", name="Show", feed_url="https://example.com/feed")
        with mock.patch("apps.content.sources.safe_get", side_effect=netsafe.UnsafeURL("That address is not a public website.")):
            sources.refresh(src)
        src.refresh_from_db()
        self.assertIn("public", src.last_error)


class FollowWebTests(BaseCase):
    def login(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_follow_page_and_add_to_account(self):
        user, ws, account, sub = make_workspace()
        c = self.login(user)
        self.assertEqual(c.get("/sources/").status_code, 200)
        with mock.patch("apps.content.sources.resolve", return_value=("podcast", "Founders Talk", "https://example.com/feed", [])), \
                mock.patch("apps.content.sources.safe_get", return_value=("u", RSS)):
            resp = c.post("/sources/", {"url": "https://example.com/feed", "kind": ""})
        self.assertRedirects(resp, "/sources/", fetch_redirect_response=False)
        src = FollowedSource.objects.get(workspace=ws)
        self.assertEqual(src.items.count(), 2)
        cat = Category.objects.get(slug="followed-sources")
        c.post(f"/accounts/{account.pk}/categories/add/", {"category": cat.pk, "source": src.pk})
        new = AccountCategory.objects.get(account=account, followed_source=src)
        page = c.get(f"/accounts/{account.pk}/")
        self.assertContains(page, "Founders Talk")
        self.assertEqual(c.get(f"/subscriptions/{new.pk}/").status_code, 200)

    def test_private_url_is_rejected_with_a_message(self):
        user, ws, account, sub = make_workspace()
        resp = self.login(user).post("/sources/", {"url": "http://169.254.169.254/latest", "kind": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(FollowedSource.objects.exists())
        self.assertContains(resp, "not a public website")

    def test_cannot_use_another_workspaces_source(self):
        u1, ws1, a1, s1 = make_workspace(email="o1@example.com")
        u2, ws2, a2, s2 = make_workspace(email="o2@example.com")
        src = FollowedSource.objects.create(workspace=ws2, kind="podcast", name="Theirs", feed_url="https://example.com/t")
        cat = Category.objects.get(slug="followed-sources")
        resp = self.login(u1).post(f"/accounts/{a1.pk}/categories/add/", {"category": cat.pk, "source": src.pk})
        self.assertEqual(resp.status_code, 404)

    def test_format_choices_follow_the_platform(self):
        user, ws, account, sub = make_workspace(platform="instagram")
        page = self.login(user).get(f"/subscriptions/{sub.pk}/")
        self.assertNotContains(page, 'value="text"')
        self.assertContains(page, 'value="image"')


@override_settings(AI_DRY_RUN=False)
class AutoPublishChoiceTests(BaseCase):
    def login(self, user):
        c = Client()
        c.force_login(user)
        return c

    def form(self, sub, **over):
        data = {"content_format": "auto", "post_style": "standard", "publish_mode": "auto", "region": "US",
                "custom_topic": "", "post_times_text": "09:00", "enabled": "on"}
        data.update(over)
        return data

    def test_high_risk_auto_needs_the_owner_to_accept_risk(self):
        user, ws, account, sub = make_workspace(category="trending-news", mode="approve")
        c = self.login(user)
        resp = c.post(f"/subscriptions/{sub.pk}/", self.form(sub))
        self.assertEqual(resp.status_code, 200)  # refused, form shown again
        sub.refresh_from_db()
        self.assertEqual(sub.publish_mode, "approve")
        c.post(f"/subscriptions/{sub.pk}/", self.form(sub, auto_ack="on"))
        sub.refresh_from_db()
        self.assertEqual((sub.publish_mode, sub.auto_ack, sub.effective_mode), ("auto", True, "auto"))

    def test_accepted_high_risk_post_is_queued_to_publish(self):
        user, ws, account, sub = make_workspace(category="trending-news", mode="auto")
        AccountCategory.objects.filter(pk=sub.pk).update(auto_ack=True)
        sub.refresh_from_db()
        category = sub.category
        TrendItem.objects.create(category=category, region="US", url="https://news.example/a", title="Headline", summary="s")
        with mock.patch("apps.content.ai.call_json", fake_call_json()):
            post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "queued")

    def test_failed_review_still_blocks_auto_publish(self):
        user, ws, account, sub = make_workspace(category="trending-news", mode="auto")
        AccountCategory.objects.filter(pk=sub.pk).update(auto_ack=True)
        sub.refresh_from_db()
        TrendItem.objects.create(category=sub.category, region="US", url="https://news.example/b", title="Headline", summary="s")
        with mock.patch("apps.content.ai.call_json", fake_call_json(verdict="fail")):
            post = run_generation(self.new_post(sub).pk)
        self.assertEqual(post.status, "needs_review")

    def test_switching_away_from_auto_clears_the_acceptance(self):
        user, ws, account, sub = make_workspace(category="trending-news", mode="auto")
        AccountCategory.objects.filter(pk=sub.pk).update(auto_ack=True)
        self.login(user).post(f"/subscriptions/{sub.pk}/", self.form(sub, publish_mode="approve"))
        sub.refresh_from_db()
        self.assertFalse(sub.auto_ack)

    def test_low_risk_category_auto_needs_no_acceptance(self):
        user, ws, account, sub = make_workspace(category="motivation", mode="approve")
        self.login(user).post(f"/subscriptions/{sub.pk}/", self.form(sub))
        sub.refresh_from_db()
        self.assertEqual(sub.effective_mode, "auto")


class GrowthFeatureTests(BaseCase):
    def login(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_landing_page_is_public_and_shows_plans(self):
        resp = Client().get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Pricing")
        self.assertContains(resp, "Starter")
        self.assertContains(resp, "Turn the shows you follow into posts")

    def test_signed_in_user_sees_dashboard_at_root(self):
        user, *_ = make_workspace()
        self.assertContains(self.login(user).get("/"), "Today")

    def test_card_preview_every_theme_and_bad_input(self):
        from apps.workspaces.models import BrandKit

        c = Client()
        for code, _ in BrandKit.CARD_THEMES:
            resp = c.get("/card-preview/", {"theme": code, "primary": "#ff8800", "secondary": "#003366"})
            self.assertEqual((resp.status_code, resp["Content-Type"]), (200, "image/png"), code)
            self.assertTrue(resp.content.startswith(b"\x89PNG"))
        self.assertEqual(c.get("/card-preview/", {"theme": "../../etc", "primary": "javascript:x"}).status_code, 200)

    def test_card_themes_render_files(self):
        from apps.content import media
        user, ws, account, sub = make_workspace()
        for code in ("solid", "dark", "light", "bold"):
            ws.brand.card_theme = code
            rel = media.render_card(text="A longer headline to check wrapping across several lines", category_name="Tech", brand=ws.brand, handle="@x")
            self.assertTrue((media.settings.MEDIA_ROOT / rel).exists())

    def test_calendar_groups_posts_by_local_day(self):
        from datetime import timedelta
        from django.utils import timezone
        user, ws, account, sub = make_workspace()
        when = timezone.now() + timedelta(days=2)
        post = self.new_post(sub, status="queued", caption="x", scheduled_at=when)
        rejected = self.new_post(sub, status="rejected", caption="y", scheduled_at=when)
        resp = self.login(user).get("/calendar/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"/posts/{post.pk}/")
        self.assertNotContains(resp, f"/posts/{rejected.pk}/")
        self.assertEqual(self.login(user).get("/calendar/?start=garbage").status_code, 200)

    def test_post_times_use_workspace_timezone_across_review_and_history(self):
        user, ws, account, sub = make_workspace()
        scheduled_at = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        post = self.new_post(sub, status="pending_approval", caption="Review this local-time post.", scheduled_at=scheduled_at)
        client = self.login(user)

        self.assertContains(client.get("/queue/"), "Sat 10 Oct, 17:30 Asia/Kolkata")
        self.assertContains(client.get(f"/posts/{post.pk}/"), "Sat 10 Oct 2026, 17:30 Asia/Kolkata")
        self.assertContains(client.get("/posts/"), "10 Oct 17:30")
        self.assertContains(client.get("/posts/"), "When (Asia/Kolkata)")

    def test_post_history_paginates_and_keeps_status_filter(self):
        user, ws, account, sub = make_workspace()
        base_time = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        for index in range(26):
            self.new_post(
                sub,
                status="queued",
                caption=f"Queued post {index}",
                scheduled_at=base_time + timedelta(seconds=index),
            )
        self.new_post(sub, status="draft", caption="Draft outside the filter.", scheduled_at=base_time)
        client = self.login(user)

        first = client.get("/posts/?status=queued")
        second = client.get("/posts/?status=queued&page=2")
        self.assertEqual(first.context["page_obj"].paginator.count, 26)
        self.assertEqual(len(first.context["posts"]), 25)
        self.assertEqual(len(second.context["posts"]), 1)
        self.assertEqual(first.context["status"], "queued")
        self.assertEqual(second.context["status"], "queued")
        first_ids = {post.pk for post in first.context["posts"]}
        second_ids = {post.pk for post in second.context["posts"]}
        self.assertFalse(first_ids & second_ids)
        self.assertContains(first, "?status=queued&amp;page=2")
        self.assertNotContains(first, "Draft outside the filter.")
        self.assertEqual(client.get("/posts/?status=queued&page=not-a-page").status_code, 200)

    def test_checklist_and_impact_follow_progress(self):
        from apps.web import views
        from django.utils import timezone
        user, ws, account, sub = make_workspace()
        posts = Post.objects.filter(workspace=ws)
        steps = {i["label"]: i["done"] for i in views._checklist(ws, posts)["items"]}
        self.assertTrue(steps["Choose a plan"] and steps["Connect your first account"] and steps["Pick categories for it"])
        self.assertFalse(steps["Generate your first post"])
        for n in range(3):  # three days in a row, ending today
            self.new_post(sub, status="published", caption=f"c{n}", published_at=timezone.now())
        from datetime import timedelta
        for n, p in enumerate(Post.objects.filter(workspace=ws).order_by("id")):
            Post.objects.filter(pk=p.pk).update(published_at=timezone.now() - timedelta(days=n))
        impact = views._impact(ws, Post.objects.filter(workspace=ws))
        self.assertEqual((impact["total"], impact["streak"], impact["hours_saved"]), (3, 3, 0.8))
        self.assertContains(self.login(user).get("/"), "Posting streak")

    def test_brand_page_shows_preview_and_saves_theme(self):
        user, ws, account, sub = make_workspace()
        c = self.login(user)
        self.assertContains(c.get("/brand/"), "card-preview")
        resp = c.post("/brand/", {"tone": "friendly", "language": "English", "primary_color": "#112233", "secondary_color": "#445566",
                                  "hashtags": "", "banned_words": "", "cta": "", "watermark": "", "card_theme": "dark",
                                  "country": "US", "timezone": "UTC"})
        ws.brand.refresh_from_db()
        self.assertEqual(ws.brand.card_theme, "dark")


class AnalyticsTests(BaseCase):
    def published(self, sub, platform_id="123", hours_ago=3, **kw):
        from datetime import timedelta
        from django.utils import timezone
        return self.new_post(sub, status="published", caption="Caption here for results", platform_post_id=platform_id,
                             published_at=timezone.now() - timedelta(hours=hours_ago), **kw)

    def test_x_metrics_are_parsed(self):
        from apps.social.publishers.xtwitter import XPublisher
        user, ws, account, sub = make_workspace(platform="x")
        post = self.published(sub, "999")
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = {"data": {"public_metrics": {"impression_count": 500, "like_count": 20, "reply_count": 3, "retweet_count": 4, "quote_count": 1}}}
        with mock.patch("apps.social.publishers.xtwitter._token", return_value="t"), mock.patch("requests.get", return_value=resp):
            self.assertEqual(XPublisher().fetch_metrics(account, post), {"reach": 500, "likes": 20, "comments": 3, "shares": 5})

    def test_youtube_metrics_are_parsed(self):
        from apps.social.publishers.youtube import YouTubePublisher
        user, ws, account, sub = make_workspace(platform="youtube")
        post = self.published(sub, "vid1")
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = {"items": [{"statistics": {"viewCount": "1200", "likeCount": "40", "commentCount": "2"}}]}
        with mock.patch("apps.social.publishers.youtube._token", return_value="t"), mock.patch("requests.get", return_value=resp):
            self.assertEqual(YouTubePublisher().fetch_metrics(account, post), {"reach": 1200, "likes": 40, "comments": 2})

    def test_facebook_metrics_tolerate_missing_insights_permission(self):
        from apps.social.publishers import meta
        user, ws, account, sub = make_workspace(platform="facebook")
        post = self.published(sub, "1_2")
        answers = [{"reactions": {"summary": {"total_count": 9}}, "comments": {"summary": {"total_count": 2}}, "shares": {"count": 1}},
                   meta.PublishError("Meta error 200: no permission", retryable=False)]

        def fake(method, path, **kw):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

        with mock.patch.object(meta, "_request", fake):
            self.assertEqual(meta.FacebookPublisher().fetch_metrics(account, post), {"likes": 9, "comments": 2, "shares": 1})

    def test_collector_saves_once_per_window_and_skips_unsupported(self):
        from apps.content import publishing
        from apps.content.models import PostMetrics
        user, ws, account, sub = make_workspace(platform="x")
        post = self.published(sub)
        fake = mock.MagicMock()
        fake.fetch_metrics.return_value = {"reach": 10, "likes": 2}
        with mock.patch("apps.content.publishing.get_publisher", return_value=fake):
            self.assertEqual(publishing.collect_metrics(), 1)
            self.assertEqual(publishing.collect_metrics(), 0)  # fetched recently
        self.assertEqual(PostMetrics.objects.filter(post=post).count(), 1)
        fake.fetch_metrics.return_value = {}  # Telegram / LinkedIn
        PostMetrics.objects.all().delete()
        with mock.patch("apps.content.publishing.get_publisher", return_value=fake):
            self.assertEqual(publishing.collect_metrics(), 0)

    def test_too_fresh_posts_are_skipped(self):
        from apps.content import publishing
        user, ws, account, sub = make_workspace(platform="x")
        self.published(sub, hours_ago=0)
        with mock.patch("apps.content.publishing.get_publisher") as gp:
            publishing.collect_metrics()
        gp.assert_not_called()

    def test_analytics_page_ranks_and_suggests_hours(self):
        from apps.content.models import PostMetrics
        user, ws, account, sub = make_workspace(platform="x")
        a = self.published(sub, "1", hours_ago=5)
        b = self.published(sub, "2", hours_ago=30)
        PostMetrics.objects.create(post=a, reach=1000, likes=50, comments=5, shares=5)
        PostMetrics.objects.create(post=b, reach=200, likes=2)
        c = Client()
        c.force_login(user)
        resp = c.get("/analytics/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Your best posting times")
        self.assertContains(resp, "By topic")
        self.assertEqual(resp.context["top_posts"][0]["post"].pk, a.pk)
        self.assertEqual(resp.context["totals"]["engagement"], 62)

    def test_analytics_empty_state(self):
        user, ws, account, sub = make_workspace()
        c = Client()
        c.force_login(user)
        self.assertContains(c.get("/analytics/"), "No results yet")


class ReferralTests(BaseCase):
    def signup(self, email, ref=None):
        c = Client()
        if ref:
            c.get(f"/signup/?ref={ref}")
        c.post("/signup/", {"name": email, "email": email, "password": "a-long-password-1", "country": "US", "timezone": "UTC"})
        from apps.workspaces.models import Workspace
        return c, Workspace.objects.get(name=email)

    def test_every_workspace_gets_a_unique_code(self):
        _, a, *_ = make_workspace(email="r1@example.com")
        _, b, *_ = make_workspace(email="r2@example.com")
        self.assertTrue(a.referral_code and b.referral_code and a.referral_code != b.referral_code)

    def test_signup_with_link_records_referral_but_no_reward_yet(self):
        from apps.billing.models import BonusCredit, Referral
        _, owner, *_ = make_workspace(email="o@example.com")
        _, friend = self.signup("friend@example.com", owner.referral_code)
        self.assertEqual(friend.referred_by_id, owner.pk)
        self.assertTrue(Referral.objects.filter(referrer=owner, referred=friend, rewarded_at__isnull=True).exists())
        self.assertFalse(BonusCredit.objects.exists())

    def test_first_payment_rewards_only_referrer_once(self):
        from apps.billing import referrals
        from apps.billing.models import BonusCredit, WalletEntry
        _, owner, *_ = make_workspace(email="o2@example.com")
        _, friend = self.signup("friend2@example.com", owner.referral_code)
        billing._mark(friend.subscription, "active")
        self.assertTrue(referrals.reward_if_first_payment(friend, "USD"))
        self.assertFalse(referrals.reward_if_first_payment(friend, "USD"))
        self.assertEqual(WalletEntry.objects.filter(workspace=owner).count(), 1)
        self.assertEqual(billing.wallet_balance(owner, "USD"), 50)
        self.assertEqual(billing.wallet_balance(owner, "INR"), 0)
        self.assertEqual(WalletEntry.objects.filter(workspace=friend).count(), 0)
        self.assertEqual(BonusCredit.objects.count(), 0)

    def test_expired_bonus_does_not_count(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.billing.models import BonusCredit
        _, ws, *_ = make_workspace(email="o3@example.com")
        base = billing.post_limit(ws)
        BonusCredit.objects.create(workspace=ws, reason="old", extra_posts_per_month=99, expires_at=timezone.now() - timedelta(days=1))
        self.assertEqual(billing.post_limit(ws), base)

    def test_bad_code_and_self_referral_are_ignored(self):
        from apps.billing import referrals
        from apps.billing.models import Referral
        _, ws, *_ = make_workspace(email="o4@example.com")
        self.assertIsNone(referrals.attach(ws, ws.referral_code))
        self.assertIsNone(referrals.attach(ws, "nonsense"))
        _, friend = self.signup("friend4@example.com", "nonsense")
        self.assertIsNone(friend.referred_by_id)
        self.assertEqual(Referral.objects.count(), 0)

    def test_reward_cap(self):
        from apps.billing import referrals
        from apps.billing.models import WalletEntry
        _, owner, *_ = make_workspace(email="o5@example.com")
        with override_settings(REFERRAL_MAX_REWARDS=1):
            for i in range(2):
                _, f = self.signup(f"cap{i}@example.com", owner.referral_code)
                referrals.reward_if_first_payment(f, "INR")
        self.assertEqual(WalletEntry.objects.filter(workspace=owner, kind="referral").count(), 1)
        self.assertEqual(billing.wallet_balance(owner, "INR"), 5000)

    def test_invite_page(self):
        user, ws, *_ = make_workspace(email="o6@example.com")
        c = Client()
        c.force_login(user)
        self.assertContains(c.get("/invite/"), f"ref={ws.referral_code}")
        self.assertContains(c.get("/invite/"), "$0.50")
        self.assertContains(c.get("/invite/"), "₹50.00")


class BestTimeTests(BaseCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def seed(self, account, sub, hour_scores, per_hour=2):
        """Published posts at given local hours (Asia/Kolkata) with engagement = score."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        from django.utils import timezone
        from apps.content.models import PostMetrics
        tz = ZoneInfo("Asia/Kolkata")
        n = 0
        for hour, score in hour_scores.items():
            for i in range(per_hour):
                n += 1
                when = datetime.combine(timezone.now().astimezone(tz).date() - timedelta(days=i + 2), datetime.min.time(), tzinfo=tz).replace(hour=hour)
                p = self.new_post(sub, status="published", caption=f"c{n}", platform_post_id=str(n), published_at=when)
                PostMetrics.objects.create(post=p, likes=score)

    def test_not_enough_data_keeps_manual_times(self):
        from apps.content import learning
        from datetime import date
        user, ws, account, sub = make_workspace()
        self.seed(account, sub, {8: 5, 20: 50}, per_hour=2)  # only 4 posts
        self.assertEqual(learning.learned_times(account, ["09:00"], date(2026, 10, 12)), ["09:00"])

    def test_learns_best_hour_for_single_slot_with_exploration_day(self):
        from datetime import date
        from apps.content import learning
        user, ws, account, sub = make_workspace()
        self.seed(account, sub, {7: 4, 12: 9, 20: 80}, per_hour=4)
        normal = date(2026, 10, 12)
        while normal.toordinal() % 5 == 0:
            normal = date.fromordinal(normal.toordinal() + 1)
        self.assertEqual(learning.learned_times(account, ["09:00"], normal), ["20:00"])
        explore = date.fromordinal(normal.toordinal() + (5 - normal.toordinal() % 5))
        self.assertEqual(learning.learned_times(account, ["09:00"], explore), ["09:00"])

    def test_two_slots_keep_one_manual_slot_for_exploration(self):
        from datetime import date
        from apps.content import learning
        user, ws, account, sub = make_workspace()
        self.seed(account, sub, {7: 4, 12: 9, 20: 80}, per_hour=4)
        times = learning.learned_times(account, ["09:00", "18:30"], date(2026, 10, 12))
        self.assertEqual(len(times), 2)
        self.assertIn("20:00", times)
        self.assertIn("18:30", times)

    def test_scheduler_uses_learned_hour_only_when_enabled(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        user, ws, account, sub = make_workspace()
        self.seed(account, sub, {7: 4, 12: 9, 20: 80}, per_hour=4)
        tz = ZoneInfo("Asia/Kolkata")
        day = datetime(2026, 10, 13, 19, 50, tzinfo=tz)  # 10 min before 20:00; ordinal not divisible by 5
        from datetime import date
        self.assertNotEqual(date(2026, 10, 13).toordinal() % 5, 0)
        self.assertEqual(scheduling.plan_slots(now=day), [])  # manual 09:00 only
        AccountCategory.objects.filter(pk=sub.pk).update(auto_times=True)
        self.assertEqual(len(scheduling.plan_slots(now=day)), 1)
        self.assertEqual(len(scheduling.plan_slots(now=day)), 0)  # idempotent

    def test_never_more_posts_than_asked_in_a_day(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        user, ws, account, sub = make_workspace()
        AccountCategory.objects.filter(pk=sub.pk).update(auto_times=True)
        tz = ZoneInfo("Asia/Kolkata")
        self.new_post(sub, slot_key="2026-10-13T09:00", status="published", caption="x")  # already posted today
        self.seed(account, sub, {7: 4, 12: 9, 20: 80}, per_hour=4)
        self.assertEqual(scheduling.plan_slots(now=datetime(2026, 10, 13, 19, 55, tzinfo=tz)), [])

    def test_setting_saves_and_page_shows_best_hours(self):
        user, ws, account, sub = make_workspace()
        self.seed(account, sub, {7: 4, 12: 9, 20: 80}, per_hour=4)
        c = Client()
        c.force_login(user)
        self.assertContains(c.get(f"/subscriptions/{sub.pk}/"), "20:00")
        c.post(f"/subscriptions/{sub.pk}/", {"content_format": "auto", "post_style": "standard", "publish_mode": "auto", "region": "US",
                                              "custom_topic": "", "post_times_text": "09:00", "auto_times": "on", "enabled": "on"})
        sub.refresh_from_db()
        self.assertTrue(sub.auto_times)


@override_settings(AI_DRY_RUN=False)
class GenerationUpgradeTests(BaseCase):
    def login(self, user):
        c = Client()
        c.force_login(user)
        return c

    def capture(self, **kw):
        seen = {}
        base = fake_call_json(**kw)

        def spy(stage, model, system, user_msg, max_tokens=1200):
            seen.setdefault(stage, []).append((system, user_msg))
            return base(stage, model, system, user_msg, max_tokens)

        return seen, spy

    def test_language_override_and_brand_fallback(self):
        user, ws, account, sub = make_workspace()
        AccountCategory.objects.filter(pk=sub.pk).update(language="Hindi")
        sub.refresh_from_db()
        seen, spy = self.capture()
        with mock.patch("apps.content.ai.call_json", spy):
            run_generation(self.new_post(sub).pk)
        self.assertIn("in Hindi", seen["write"][0][0])
        AccountCategory.objects.filter(pk=sub.pk).update(language="")
        sub.refresh_from_db()
        seen, spy = self.capture()
        with mock.patch("apps.content.ai.call_json", spy):
            run_generation(self.new_post(sub, slot_key="x2").pk)
        self.assertIn("in English", seen["write"][0][0])

    def test_voice_samples_reach_the_prompt(self):
        user, ws, account, sub = make_workspace()
        ws.brand.sample_posts = "Coffee first. Plans second. Always."
        ws.brand.save()
        sub.refresh_from_db()
        seen, spy = self.capture()
        with mock.patch("apps.content.ai.call_json", spy):
            run_generation(self.new_post(sub).pk)
        self.assertIn("Coffee first", seen["write"][0][0])

    def test_own_idea_is_used_without_fetching_and_flagged_as_user_topic(self):
        user, ws, account, sub = make_workspace(category="trending-news", mode="approve")
        seen, spy = self.capture()
        with mock.patch("apps.content.ai.call_json", spy), mock.patch("apps.content.trends.ingest_category") as ing:
            post = run_generation(self.new_post(sub, idea="tips for first-time home buyers").pk)
        ing.assert_not_called()
        self.assertEqual(post.source_title, "tips for first-time home buyers")
        self.assertIn("typed this topic themselves", seen["write"][0][1])
        self.assertNotIn("triage", seen)

    def test_generate_view_passes_idea_and_rejects_tiny_ones(self):
        user, ws, account, sub = make_workspace()
        c = self.login(user)
        with mock.patch("apps.web.views.enqueue"):
            c.post(f"/subscriptions/{sub.pk}/generate/", {"idea": "ab"})
            self.assertFalse(Post.objects.filter(subscription=sub).exclude(idea="").exists())
            c.post(f"/subscriptions/{sub.pk}/generate/", {"idea": "how to start a podcast"})
        self.assertTrue(Post.objects.filter(subscription=sub, idea="how to start a podcast").exists())

    def pending(self, sub, caption="Original caption about starting small today."):
        return self.new_post(sub, status="pending_approval", caption=caption, hashtags=["#a"], source_title="topic")

    def test_revise_applies_new_text_after_review(self):
        from apps.content import pipeline
        user, ws, account, sub = make_workspace()
        post = self.pending(sub)
        seen, spy = self.capture()

        def revising(stage, model, system, user_msg, max_tokens=1200):
            if stage == "revise":
                seen.setdefault("revise", []).append(user_msg)
                return {"caption": "Short and funny version about starting small.", "hashtags": ["#b"]}, {"model": model, "tokens_in": 10, "tokens_out": 5, "cost_usd": 0}
            return spy(stage, model, system, user_msg, max_tokens)

        with mock.patch("apps.content.ai.call_json", revising):
            pipeline.revise(post, "make it shorter and funnier")
        post.refresh_from_db()
        self.assertEqual((post.caption, post.hashtags), ("Short and funny version about starting small.", ["#b"]))
        self.assertIn("review", seen)
        self.assertTrue(post.runs.filter(stage="revise").exists())

    def test_revise_rejected_by_reviewer_keeps_original(self):
        from apps.content import pipeline
        user, ws, account, sub = make_workspace()
        post = self.pending(sub)

        def call(stage, model, system, user_msg, max_tokens=1200):
            u = {"model": model, "tokens_in": 1, "tokens_out": 1, "cost_usd": 0}
            if stage == "revise":
                return {"caption": "A rewritten caption that adds an invented claim.", "hashtags": []}, u
            return {"verdict": "fail", "reasons": ["invented claim"], "risk_flags": []}, u

        with mock.patch("apps.content.ai.call_json", call):
            with self.assertRaises(pipeline.ReviseError):
                pipeline.revise(post, "make it punchier")
        post.refresh_from_db()
        self.assertEqual(post.caption, "Original caption about starting small today.")

    def test_revise_banned_word_and_limits(self):
        from apps.content import pipeline
        user, ws, account, sub = make_workspace()
        ws.brand.banned_words = "forbidden"
        ws.brand.save()
        post = self.pending(sub)
        ok = lambda s, m, sy, u, max_tokens=1200: ({"caption": "This forbidden word should block the rewrite.", "hashtags": []}, {"model": m, "tokens_in": 1, "tokens_out": 1, "cost_usd": 0})
        with mock.patch("apps.content.ai.call_json", ok):
            with self.assertRaises(pipeline.ReviseError):
                pipeline.revise(post, "make it shorter")
        with self.assertRaises(pipeline.ReviseError):
            pipeline.revise(post, "x")
        post.status = "queued"
        with self.assertRaises(pipeline.ReviseError):
            pipeline.revise(post, "make it shorter")

    def test_revise_is_capped(self):
        from apps.content import pipeline
        from apps.content.models import GenerationRun
        user, ws, account, sub = make_workspace()
        post = self.pending(sub)
        for _ in range(pipeline.MAX_REVISIONS):
            GenerationRun.objects.create(post=post, stage="revise", model="m")
        with self.assertRaises(pipeline.ReviseError):
            pipeline.revise(post, "one more time please")

    def test_revise_view_and_pages_render(self):
        user, ws, account, sub = make_workspace()
        post = self.pending(sub)
        c = self.login(user)
        self.assertContains(c.get(f"/posts/{post.pk}/"), "Ask Claude to rewrite it")
        self.assertContains(c.get(f"/accounts/{account.pk}/"), "Write about my idea")
        self.assertContains(c.get(f"/subscriptions/{sub.pk}/"), "Hinglish")
        resp = c.post(f"/posts/{post.pk}/", {"action": "revise", "instruction": ""}, follow=True)
        self.assertContains(resp, "Tell Claude what to change")
        self.assertContains(c.get("/brand/"), "Paste 2 or 3 posts")


class MobileTests(BaseCase):
    def test_manifest_and_icons(self):
        import json
        resp = Client().get("/manifest.webmanifest")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual((data["display"], data["start_url"]), ("standalone", "/"))
        self.assertTrue(any(i.get("purpose") == "maskable" for i in data["icons"]))
        from django.contrib.staticfiles import finders
        for name in ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"):
            self.assertTrue(finders.find(f"web/icons/{name}"), name)

    def test_app_pages_have_viewport_tabbar_and_install_tags(self):
        user, *_ = make_workspace()
        c = Client()
        c.force_login(user)
        html = c.get("/").content.decode()
        for needle in ("viewport-fit=cover", 'class="tabbar"', 'rel="manifest"', "theme-color", "apple-touch-icon"):
            self.assertIn(needle, html)
        self.assertIn("manifest", Client().get("/login/").content.decode())
