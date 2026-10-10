"""Facebook Page and Instagram publishing through the Graph API.

Both need a long-lived Page access token and an approved Meta app (PRD section 8: start the
app review in week 1). Instagram additionally needs a PUBLICLY reachable image URL, so
PUBLIC_BASE_URL must be an https URL that serves /media/.
"""
import requests
from django.conf import settings

from .base import PublishError, Publisher


def _graph(path):
    return f"https://graph.facebook.com/{settings.GRAPH_API_VERSION}/{path}"


def _request(method, path, **kwargs):
    try:
        resp = requests.request(method, _graph(path), timeout=settings.HTTP_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise PublishError(f"Meta network error: {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        raise PublishError(f"Meta returned HTTP {resp.status_code}")
    if "error" in data:
        err = data["error"]
        code = err.get("code")
        # 190 = invalid/expired token, 10/200-299 = permission problems; 4, 17, 32, 613 = rate limits
        auth = code == 190 or (isinstance(code, int) and 200 <= code <= 299) or code == 10
        retryable = code in (1, 2, 4, 17, 32, 613) or resp.status_code >= 500
        raise PublishError(f"Meta error {code}: {err.get('message', '')}", retryable=retryable, auth_failed=auth)
    return data


class FacebookPublisher(Publisher):
    platform = "facebook"

    def check(self, account):
        _request("GET", "me", params={"access_token": account.token, "fields": "id,name"})

    def publish(self, account, post):
        page_id = account.extra.get("page_id")
        if not page_id:
            raise PublishError("Facebook page id missing", retryable=False)
        text = post.full_text()
        if post.media_path:
            with open(settings.MEDIA_ROOT / post.media_path, "rb") as fh:
                data = _request(
                    "POST",
                    f"{page_id}/photos",
                    data={"caption": text, "access_token": account.token},
                    files={"source": fh},
                )
            return str(data.get("post_id") or data["id"])
        data = _request("POST", f"{page_id}/feed", data={"message": text, "access_token": account.token})
        return str(data["id"])


    def fetch_metrics(self, account, post):
        token = account.token
        data = _request("GET", post.platform_post_id, params={
            "access_token": token, "fields": "shares,comments.summary(true).limit(0),reactions.summary(true).limit(0)"})
        out = {"likes": (data.get("reactions") or {}).get("summary", {}).get("total_count", 0),
               "comments": (data.get("comments") or {}).get("summary", {}).get("total_count", 0),
               "shares": (data.get("shares") or {}).get("count", 0)}
        try:  # reach needs the read_insights permission; leave it out if the page did not grant it
            ins = _request("GET", f"{post.platform_post_id}/insights", params={"access_token": token, "metric": "post_impressions_unique"})
            out["reach"] = (ins.get("data") or [{}])[0].get("values", [{}])[0].get("value", 0)
        except PublishError:
            pass
        return out


class InstagramPublisher(Publisher):
    platform = "instagram"

    def check(self, account):
        ig_id = account.extra.get("ig_user_id")
        if not ig_id:
            raise PublishError("Instagram business account id missing", retryable=False)
        _request("GET", ig_id, params={"access_token": account.token, "fields": "id,username"})

    def publish(self, account, post):
        ig_id = account.extra.get("ig_user_id")
        if not ig_id:
            raise PublishError("Instagram business account id missing", retryable=False)
        if not post.media_path:
            raise PublishError("Instagram posts need an image", retryable=False)
        base = settings.PUBLIC_BASE_URL
        if not base.startswith("https://"):
            raise PublishError("Instagram needs PUBLIC_BASE_URL to be a public https URL", retryable=False)
        image_url = f"{base}{settings.MEDIA_URL}{post.media_path}"
        container = _request(
            "POST",
            f"{ig_id}/media",
            data={"image_url": image_url, "caption": post.full_text(), "access_token": account.token},
        )
        published = _request(
            "POST",
            f"{ig_id}/media_publish",
            data={"creation_id": container["id"], "access_token": account.token},
        )
        return str(published["id"])

    def fetch_metrics(self, account, post):
        token = account.token
        data = _request("GET", post.platform_post_id, params={"access_token": token, "fields": "like_count,comments_count"})
        out = {"likes": data.get("like_count", 0), "comments": data.get("comments_count", 0)}
        try:
            ins = _request("GET", f"{post.platform_post_id}/insights", params={"access_token": token, "metric": "reach"})
            out["reach"] = (ins.get("data") or [{}])[0].get("values", [{}])[0].get("value", 0)
        except PublishError:
            pass
        return out
