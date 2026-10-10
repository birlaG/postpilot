"""X (Twitter) posting through the X API v2.

Posting is a paid API product on X; check your tier's monthly post cap before enabling X on cheap
plans. Image upload uses the v2 media endpoint; if a tier rejects it, the post is sent as text only.
"""
import logging

import requests
from django.conf import settings

from apps.social import oauth

from .base import PublishError, Publisher

log = logging.getLogger(__name__)
API = "https://api.x.com/2"


def _token(account):
    try:
        return oauth.ensure_fresh(account)
    except oauth.OAuthError as exc:
        raise PublishError(str(exc), retryable=False, auth_failed=True) from exc


def _check(resp, what):
    if resp.status_code < 400:
        return
    if resp.status_code == 401:
        raise PublishError(f"X {what}: unauthorized", retryable=False, auth_failed=True)
    if resp.status_code == 429:
        raise PublishError(f"X {what}: rate or plan limit reached", retryable=True)
    try:
        detail = resp.json().get("detail") or resp.json().get("title", "")
    except ValueError:
        detail = ""
    raise PublishError(f"X {what} error {resp.status_code}: {detail}", retryable=resp.status_code >= 500)


class XPublisher(Publisher):
    platform = "x"

    def check(self, account):
        token = _token(account)
        try:
            _check(requests.get(f"{API}/users/me", headers={"Authorization": f"Bearer {token}"}, timeout=settings.HTTP_TIMEOUT), "check")
        except requests.RequestException as exc:
            raise PublishError(f"X network error: {exc}") from exc

    def _upload_image(self, token, post):
        with open(settings.MEDIA_ROOT / post.media_path, "rb") as fh:
            resp = requests.post(f"{API}/media/upload", headers={"Authorization": f"Bearer {token}"},
                                 data={"media_category": "tweet_image"}, files={"media": fh}, timeout=60)
        if resp.status_code >= 400:
            log.warning("X media upload failed (%s); posting text only", resp.status_code)
            return None
        body = resp.json().get("data", {})
        return body.get("id") or body.get("media_id_string")

    def publish(self, account, post):
        token = _token(account)
        payload = {"text": post.full_text()[:280]}
        try:
            if post.media_path:
                media_id = self._upload_image(token, post)
                if media_id:
                    payload["media"] = {"media_ids": [str(media_id)]}
            resp = requests.post(f"{API}/tweets", json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=settings.HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise PublishError(f"X network error: {exc}") from exc
        _check(resp, "post")
        return str(resp.json()["data"]["id"])

    def fetch_metrics(self, account, post):
        token = _token(account)
        try:
            resp = requests.get(f"{API}/tweets/{post.platform_post_id}", params={"tweet.fields": "public_metrics"},
                                headers={"Authorization": f"Bearer {token}"}, timeout=settings.HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise PublishError(f"X network error: {exc}") from exc
        _check(resp, "metrics")
        m = resp.json().get("data", {}).get("public_metrics", {})
        return {"reach": m.get("impression_count", 0), "likes": m.get("like_count", 0), "comments": m.get("reply_count", 0),
                "shares": m.get("retweet_count", 0) + m.get("quote_count", 0)}
