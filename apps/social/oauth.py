"""OAuth connect flows for YouTube (Google), Facebook + Instagram (Meta), X and LinkedIn.

Flow: /oauth/<provider>/start/ -> provider consent screen -> /oauth/<provider>/callback/ ->
exchange the code, discover the account(s) the user can post to, store tokens encrypted.

None of this has been run against the live providers from this repository. Each provider needs a
developer app (and some need review) before real users can connect: see README.
"""
import base64
import hashlib
import logging
import secrets
import time
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.db import transaction

log = logging.getLogger(__name__)


class OAuthError(Exception):
    pass


def _g():
    return settings.GRAPH_API_VERSION


PROVIDERS = {
    "google": {
        "label": "YouTube",
        "platforms": ["youtube"],
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
        "extra_authorize": {"access_type": "offline", "prompt": "consent"},
        "client_id": lambda: settings.GOOGLE_CLIENT_ID,
        "client_secret": lambda: settings.GOOGLE_CLIENT_SECRET,
        "pkce": False,
        "basic_auth": False,
    },
    "meta": {
        "label": "Facebook and Instagram",
        "platforms": ["facebook", "instagram"],
        "authorize": lambda: f"https://www.facebook.com/{_g()}/dialog/oauth",
        "token": lambda: f"https://graph.facebook.com/{_g()}/oauth/access_token",
        "scope": "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish",
        "extra_authorize": {},
        "client_id": lambda: settings.META_APP_ID,
        "client_secret": lambda: settings.META_APP_SECRET,
        "pkce": False,
        "basic_auth": False,
    },
    "x": {
        "label": "X",
        "platforms": ["x"],
        "authorize": "https://x.com/i/oauth2/authorize",
        "token": "https://api.x.com/2/oauth2/token",
        "scope": "tweet.read tweet.write users.read media.write offline.access",
        "extra_authorize": {},
        "client_id": lambda: settings.X_CLIENT_ID,
        "client_secret": lambda: settings.X_CLIENT_SECRET,
        "pkce": True,
        "basic_auth": True,
    },
    "linkedin": {
        "label": "LinkedIn",
        "platforms": ["linkedin"],
        "authorize": "https://www.linkedin.com/oauth/v2/authorization",
        "token": "https://www.linkedin.com/oauth/v2/accessToken",
        "scope": "openid profile w_member_social",
        "extra_authorize": {},
        "client_id": lambda: settings.LINKEDIN_CLIENT_ID,
        "client_secret": lambda: settings.LINKEDIN_CLIENT_SECRET,
        "pkce": False,
        "basic_auth": False,
    },
}

PLATFORM_PROVIDER = {"youtube": "google", "facebook": "meta", "instagram": "meta", "x": "x", "linkedin": "linkedin"}


def _val(spec, key):
    v = spec[key]
    return v() if callable(v) else v


def provider_spec(provider):
    if provider not in PROVIDERS:
        raise OAuthError(f"Unknown provider {provider}")
    return PROVIDERS[provider]


def configured(provider) -> bool:
    spec = PROVIDERS.get(provider)
    return bool(spec and _val(spec, "client_id") and _val(spec, "client_secret"))


def redirect_uri(provider) -> str:
    return f"{settings.PUBLIC_BASE_URL}/oauth/{provider}/callback/"


def _post(url, **kwargs):
    try:
        resp = requests.post(url, timeout=settings.HTTP_TIMEOUT, **kwargs)
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise OAuthError(f"Could not reach the provider: {exc}") from exc
    if resp.status_code >= 400 or "error" in data and "access_token" not in data:
        raise OAuthError(f"Provider rejected the request: {data.get('error_description') or data.get('error') or resp.status_code}")
    return data


def _get(url, **kwargs):
    try:
        resp = requests.get(url, timeout=settings.HTTP_TIMEOUT, **kwargs)
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise OAuthError(f"Could not reach the provider: {exc}") from exc
    if resp.status_code >= 400 or "error" in data:
        err = data.get("error")
        msg = err.get("message") if isinstance(err, dict) else (data.get("error_description") or err or resp.status_code)
        raise OAuthError(f"Provider error: {msg}")
    return data


# ------------------------------------------------------------------ start / finish
def start(provider, session) -> str:
    spec = provider_spec(provider)
    if not configured(provider):
        raise OAuthError(f"{spec['label']} is not configured on this server")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": _val(spec, "client_id"),
        "redirect_uri": redirect_uri(provider),
        "response_type": "code",
        "scope": spec["scope"],
        "state": state,
        **spec["extra_authorize"],
    }
    entry = {"state": state}
    if spec["pkce"]:
        verifier = secrets.token_urlsafe(64)
        entry["verifier"] = verifier
        params["code_challenge"] = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params["code_challenge_method"] = "S256"
    session[f"oauth_{provider}"] = entry
    return f"{_val(spec, 'authorize')}?{urlencode(params)}"


def finish(provider, session, query) -> dict:
    """Validate state and exchange the code. Returns the provider token response."""
    spec = provider_spec(provider)
    entry = session.pop(f"oauth_{provider}", None)
    if query.get("error"):
        raise OAuthError(f"{spec['label']} said: {query.get('error_description') or query['error']}")
    if not entry or not secrets.compare_digest(entry["state"], query.get("state", "")):
        raise OAuthError("The sign-in session expired or was tampered with. Please try again.")
    code = query.get("code")
    if not code:
        raise OAuthError("No authorization code returned")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(provider),
        "client_id": _val(spec, "client_id"),
    }
    kwargs = {}
    if spec["basic_auth"]:
        kwargs["auth"] = (_val(spec, "client_id"), _val(spec, "client_secret"))
    else:
        data["client_secret"] = _val(spec, "client_secret")
    if entry.get("verifier"):
        data["code_verifier"] = entry["verifier"]
    if provider == "meta":  # Meta's token endpoint takes GET parameters
        data.pop("grant_type")
        return _get(_val(spec, "token"), params=data)
    return _post(_val(spec, "token"), data=data, **kwargs)


def expiry_from(token_response):
    seconds = token_response.get("expires_in")
    return int(time.time() + int(seconds)) if seconds else None


# ------------------------------------------------------------------ account discovery
def discover(provider, token_response) -> list:
    """Return candidate accounts: dicts with platform, key, display_name, handle, extra, access_token,
    refresh_token, expires_at. The caller decides which to save."""
    access = token_response["access_token"]
    refresh = token_response.get("refresh_token", "")
    expires_at = expiry_from(token_response)

    if provider == "google":
        data = _get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                    headers={"Authorization": f"Bearer {access}"})
        if not data.get("items"):
            raise OAuthError("This Google account has no YouTube channel. Create one at youtube.com first.")
        return [
            {"platform": "youtube", "key": f"yt-{c['id']}", "display_name": c["snippet"]["title"][:120],
             "handle": c["snippet"].get("customUrl", ""), "extra": {"channel_id": c["id"]},
             "access_token": access, "refresh_token": refresh, "expires_at": expires_at, "structured": True}
            for c in data["items"]
        ]

    if provider == "x":
        me = _get("https://api.x.com/2/users/me", headers={"Authorization": f"Bearer {access}"})["data"]
        return [{"platform": "x", "key": f"x-{me['id']}", "display_name": me.get("name", me["username"])[:120],
                 "handle": f"@{me['username']}", "extra": {"user_id": me["id"]},
                 "access_token": access, "refresh_token": refresh, "expires_at": expires_at, "structured": True}]

    if provider == "linkedin":
        info = _get("https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {access}"})
        return [{"platform": "linkedin", "key": f"li-{info['sub']}", "display_name": info.get("name", "LinkedIn profile")[:120],
                 "handle": "", "extra": {"author_urn": f"urn:li:person:{info['sub']}"},
                 "access_token": access, "refresh_token": refresh, "expires_at": expires_at, "structured": True}]

    if provider == "meta":
        v = _g()
        spec = PROVIDERS["meta"]
        long = _get(f"https://graph.facebook.com/{v}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": _val(spec, "client_id"),
            "client_secret": _val(spec, "client_secret"), "fb_exchange_token": access})
        pages = _get(f"https://graph.facebook.com/{v}/me/accounts", params={
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "access_token": long["access_token"], "limit": 100}).get("data", [])
        if not pages:
            raise OAuthError("No Facebook Pages found. You must be an admin of a Page to connect it.")
        out = []
        for page in pages:  # Page tokens derived from a long-lived user token do not expire
            out.append({"platform": "facebook", "key": f"fb-{page['id']}", "display_name": page["name"][:120], "handle": "",
                        "extra": {"page_id": page["id"]}, "access_token": page["access_token"], "structured": False})
            ig = page.get("instagram_business_account")
            if ig:
                out.append({"platform": "instagram", "key": f"ig-{ig['id']}", "display_name": f"{ig.get('username', 'Instagram')} (via {page['name']})"[:120],
                            "handle": f"@{ig.get('username', '')}", "extra": {"ig_user_id": ig["id"], "page_id": page["id"]},
                            "access_token": page["access_token"], "structured": False})
        return out
    raise OAuthError("Unsupported provider")


EXTERNAL_KEYS = ("channel_id", "user_id", "author_urn", "page_id", "ig_user_id")


def save_candidate(workspace, cand):
    """Create or reconnect a SocialAccount for a candidate. Returns (account, created)."""
    from .models import SocialAccount

    ident = next((k for k in ("channel_id", "user_id", "author_urn", "ig_user_id", "page_id") if k in cand["extra"]), None)
    with transaction.atomic():
        existing = SocialAccount.objects.select_for_update().filter(
            workspace=workspace, platform=cand["platform"]
        ).first()
        if existing and (not ident or existing.extra.get(ident) != cand["extra"][ident]):
            raise OAuthError(f"A {existing.get_platform_display()} account is already connected. Disconnect it before choosing another.")

        account = existing or SocialAccount(workspace=workspace, platform=cand["platform"])
        account.display_name, account.handle, account.extra = cand["display_name"], cand.get("handle", ""), cand["extra"]
        if cand.get("structured"):
            refresh = cand.get("refresh_token") or (account.creds.get("refresh_token", "") if existing else "")
            account.set_creds(cand["access_token"], refresh, cand.get("expires_at"))
            from datetime import datetime, timezone as dt_tz
            account.expires_at = datetime.fromtimestamp(cand["expires_at"], tz=dt_tz.utc) if cand.get("expires_at") else None
        else:
            account.token = cand["access_token"]
        account.status, account.last_error = "active", ""
        account.save()
    return account, existing is None


# ------------------------------------------------------------------ refresh
def ensure_fresh(account) -> str:
    """Return a valid access token, refreshing it if it is about to expire. Raises OAuthError."""
    creds = account.creds
    access, refresh, exp = creds.get("access_token", ""), creds.get("refresh_token", ""), creds.get("expires_at")
    if not exp or exp - time.time() > 120:
        return access
    if not refresh:
        raise OAuthError("Access token expired and cannot be refreshed. Reconnect the account.")
    provider = PLATFORM_PROVIDER[account.platform]
    spec = provider_spec(provider)
    data = {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": _val(spec, "client_id")}
    kwargs = {}
    if spec["basic_auth"]:
        kwargs["auth"] = (_val(spec, "client_id"), _val(spec, "client_secret"))
    else:
        data["client_secret"] = _val(spec, "client_secret")
    tok = _post(_val(spec, "token"), data=data, **kwargs)
    new_exp = expiry_from(tok)
    # X rotates refresh tokens: always store the newest one
    account.set_creds(tok["access_token"], tok.get("refresh_token") or refresh, new_exp)
    if new_exp:
        from datetime import datetime, timezone as dt_tz
        account.expires_at = datetime.fromtimestamp(new_exp, tz=dt_tz.utc)
    account.save(update_fields=["token_enc", "expires_at"])
    return tok["access_token"]
