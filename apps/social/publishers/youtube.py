"""YouTube Shorts upload through the YouTube Data API v3 resumable upload.

Requirements: OAuth connected channel, a generated vertical video (post.video_path), and an
audited API project. Uploads from an unaudited project are forced private by YouTube, and each
upload costs about 1,600 quota units out of a default 10,000 per day (verify in the docs).
"""
import requests
from django.conf import settings

from apps.social import oauth

from .base import PublishError, Publisher

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


def _token(account):
    try:
        return oauth.ensure_fresh(account)
    except oauth.OAuthError as exc:
        raise PublishError(str(exc), retryable=False, auth_failed=True) from exc


def split_title_description(text: str, hashtags):
    """First line is the title (max 100 chars). #Shorts makes YouTube treat it as a Short."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    title = (lines[0] if lines else "Short").strip()[:90].rstrip()
    description = "\n".join(lines[1:]) if len(lines) > 1 else text
    tags = " ".join(hashtags or [])
    return f"{title} #Shorts"[:100], f"{description}\n\n{tags}\n#Shorts".strip()


class YouTubePublisher(Publisher):
    platform = "youtube"

    def check(self, account):
        token = _token(account)
        try:
            resp = requests.get(CHANNELS_URL, params={"part": "id", "mine": "true"},
                                headers={"Authorization": f"Bearer {token}"}, timeout=settings.HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise PublishError(f"YouTube network error: {exc}") from exc
        if resp.status_code in (401, 403):
            raise PublishError("YouTube rejected the credentials", retryable=False, auth_failed=True)
        if resp.status_code >= 400:
            raise PublishError(f"YouTube error {resp.status_code}")

    def publish(self, account, post):
        if not post.video_path:
            raise PublishError("YouTube needs a video and none was generated", retryable=False)
        token = _token(account)
        path = settings.MEDIA_ROOT / post.video_path
        title, description = split_title_description(post.caption, post.hashtags)
        if post.category.disclaimer:
            description += f"\n\n{post.category.disclaimer}"
        if post.source_url and post.category.needs_source:
            description += f"\nSource: {post.source_url}"
        metadata = {
            "snippet": {"title": title, "description": description[:4900], "categoryId": "22"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        }
        headers = {"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4"}
        try:
            start = requests.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                                  json=metadata, headers=headers, timeout=settings.HTTP_TIMEOUT)
            self._raise_for(start)
            with open(path, "rb") as fh:
                done = requests.put(start.headers["Location"], data=fh, headers={"Content-Type": "video/mp4"}, timeout=300)
            self._raise_for(done)
            return str(done.json()["id"])
        except requests.RequestException as exc:
            raise PublishError(f"YouTube network error: {exc}") from exc

    @staticmethod
    def _raise_for(resp):
        if resp.status_code < 400:
            return
        try:
            err = resp.json().get("error", {})
            reasons = " ".join(e.get("reason", "") for e in err.get("errors", []))
            message = err.get("message", "")
        except ValueError:
            reasons, message = "", ""
        if resp.status_code == 401:
            raise PublishError(f"YouTube auth error: {message}", retryable=False, auth_failed=True)
        if "quotaExceeded" in reasons or "rateLimitExceeded" in reasons:
            raise PublishError("YouTube daily upload quota reached; will retry later", retryable=True)
        retryable = resp.status_code >= 500
        raise PublishError(f"YouTube error {resp.status_code}: {message}", retryable=retryable, auth_failed=resp.status_code == 403 and "forbidden" in reasons.lower())

    def fetch_metrics(self, account, post):
        token = _token(account)
        try:
            resp = requests.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "statistics", "id": post.platform_post_id},
                                headers={"Authorization": f"Bearer {token}"}, timeout=settings.HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise PublishError(f"YouTube network error: {exc}") from exc
        if resp.status_code in (401, 403):
            raise PublishError("YouTube rejected the credentials", retryable=False, auth_failed=True)
        if resp.status_code >= 400:
            raise PublishError(f"YouTube error {resp.status_code}")
        items = resp.json().get("items") or []
        stats = items[0].get("statistics", {}) if items else {}
        return {"reach": int(stats.get("viewCount", 0)), "likes": int(stats.get("likeCount", 0)), "comments": int(stats.get("commentCount", 0))}
