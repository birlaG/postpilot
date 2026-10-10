"""LinkedIn member posts through the Posts API (rest/posts) with an image.

Posting as a personal profile needs the w_member_social scope ("Share on LinkedIn" product).
Posting as a Company Page needs the Community Management API, which LinkedIn reviews separately.
"""
import re

import requests
from django.conf import settings

from apps.social import oauth

from .base import PublishError, Publisher

REST = "https://api.linkedin.com/rest"
RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")


def escape_commentary(text: str) -> str:
    """LinkedIn treats these characters as markup in post text; escape them so text posts verbatim."""
    return RESERVED.sub(r"\\\1", text)


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": settings.LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
    }


def _check(resp, what):
    if resp.status_code < 400:
        return
    if resp.status_code == 401:
        raise PublishError(f"LinkedIn {what}: token invalid or expired", retryable=False, auth_failed=True)
    if resp.status_code == 429:
        raise PublishError(f"LinkedIn {what}: rate limited", retryable=True)
    raise PublishError(f"LinkedIn {what} error {resp.status_code}: {resp.text[:200]}", retryable=resp.status_code >= 500)


class LinkedInPublisher(Publisher):
    platform = "linkedin"

    def _token(self, account):
        try:
            return oauth.ensure_fresh(account)
        except oauth.OAuthError as exc:
            raise PublishError(str(exc), retryable=False, auth_failed=True) from exc

    def check(self, account):
        token = self._token(account)
        try:
            _check(requests.get("https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {token}"},
                                timeout=settings.HTTP_TIMEOUT), "check")
        except requests.RequestException as exc:
            raise PublishError(f"LinkedIn network error: {exc}") from exc

    def _upload_image(self, token, author, post):
        init = requests.post(f"{REST}/images", params={"action": "initializeUpload"}, headers=_headers(token),
                             json={"initializeUploadRequest": {"owner": author}}, timeout=settings.HTTP_TIMEOUT)
        _check(init, "image init")
        value = init.json()["value"]
        with open(settings.MEDIA_ROOT / post.media_path, "rb") as fh:
            up = requests.put(value["uploadUrl"], data=fh, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        _check(up, "image upload")
        return value["image"]

    def publish(self, account, post):
        author = account.extra.get("author_urn")
        if not author:
            raise PublishError("LinkedIn author is missing; reconnect the account", retryable=False)
        token = self._token(account)
        body = {
            "author": author,
            "commentary": escape_commentary(post.full_text())[:3000],
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        try:
            if post.media_path:
                image = self._upload_image(token, author, post)
                body["content"] = {"media": {"title": (post.source_title or post.category.name)[:100], "altText": post.alt_text[:300], "id": image}}
            resp = requests.post(f"{REST}/posts", headers=_headers(token), json=body, timeout=settings.HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise PublishError(f"LinkedIn network error: {exc}") from exc
        _check(resp, "post")
        return resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id") or "published"
