"""Fetch user supplied URLs without letting them reach our own network (SSRF guard).

Every hop is checked: http/https only, standard ports, and the host must resolve only to public
addresses. Redirects are followed by hand so each target is checked too. Response size is capped.
Note: DNS can change between this check and the connection; also block private ranges at the
firewall (egress rules) on the server for defence in depth.
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings

HEADERS = {"User-Agent": "PostPilotBot/1.0 (+content automation)"}
MAX_BYTES = 2_000_000


class UnsafeURL(ValueError):
    pass


def check_url(url: str):
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL("Only http and https links are allowed.")
    if parts.username or parts.password:
        raise UnsafeURL("Links with a login in them are not allowed.")
    if parts.port not in (None, 80, 443):
        raise UnsafeURL("Only standard web ports are allowed.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UnsafeURL("That address could not be found.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise UnsafeURL("That address is not a public website.")


def safe_get(url: str, max_redirects=3):
    """Return (final_url, bytes). Raises UnsafeURL or requests.RequestException."""
    for _ in range(max_redirects + 1):
        check_url(url)
        with requests.get(url, headers=HEADERS, timeout=settings.HTTP_TIMEOUT, allow_redirects=False, stream=True) as resp:
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                url = urljoin(url, resp.headers["Location"])
                continue
            resp.raise_for_status()
            body = b""
            for chunk in resp.iter_content(65536):
                body += chunk
                if len(body) > MAX_BYTES:
                    raise UnsafeURL("That page is too large.")
            return url, body
    raise UnsafeURL("Too many redirects.")
