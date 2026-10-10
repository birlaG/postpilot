import requests
from django.conf import settings

from .base import PublishError, Publisher

API = "https://api.telegram.org/bot{token}/{method}"


def _call(token, method, **kwargs):
    try:
        resp = requests.post(API.format(token=token, method=method), timeout=settings.HTTP_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise PublishError(f"Telegram network error: {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        raise PublishError(f"Telegram returned HTTP {resp.status_code}")
    if not data.get("ok"):
        code = data.get("error_code", resp.status_code)
        desc = data.get("description", "unknown error")
        # 401/403/400 (chat not found) will not fix themselves; 429 and 5xx will.
        retryable = code in (429, 500, 502, 503, 504)
        raise PublishError(f"Telegram error {code}: {desc}", retryable=retryable, auth_failed=code in (401, 403))
    return data["result"]


class TelegramPublisher(Publisher):
    platform = "telegram"

    def check(self, account):
        _call(account.token, "getMe")

    def publish(self, account, post):
        chat_id = account.extra.get("chat_id")
        if not chat_id:
            raise PublishError("Telegram chat id missing", retryable=False)
        text = post.full_text()
        if post.media_path:
            path = settings.MEDIA_ROOT / post.media_path
            with open(path, "rb") as fh:
                result = _call(
                    account.token,
                    "sendPhoto",
                    data={"chat_id": chat_id, "caption": text[:1024]},
                    files={"photo": fh},
                )
        else:
            result = _call(account.token, "sendMessage", json={"chat_id": chat_id, "text": text[:4096]})
        return str(result["message_id"])
