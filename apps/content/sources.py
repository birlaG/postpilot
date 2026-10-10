"""Followed sources: podcasts, interview shows, YouTube channels and sites.

A user gives us a link. We turn it into a feed URL, read the newest episodes or videos, and store
short summaries (title + show notes) as TrendItem rows tied to that source. We never download audio,
video or transcripts: posts are written from the title and description only.
"""
import logging
import re
from urllib.parse import parse_qs, urlparse

import requests
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from django.utils import timezone

from .models import FollowedSource, TrendItem
from .netsafe import UnsafeURL, safe_get
from .trends import _strip

log = logging.getLogger(__name__)

YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
CHANNEL_ID = re.compile(r"UC[\w-]{22}")
ALT_LINK = re.compile(r"<link[^>]+rel=[\"']alternate[\"'][^>]*>", re.I)


class SourceError(Exception):
    """A message that is safe to show to the user."""


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(content: bytes):
    """Return (title, entries) for RSS 2.0 or Atom. entries: dicts with url, title, summary."""
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, DefusedXmlException, ValueError):
        raise SourceError("That link is not a readable feed.")
    channel_title, entries = "", []
    for el in root.iter():
        name = _local(el.tag)
        if name in ("channel", "feed"):
            channel_title = next((_strip(c.text, 160) for c in el if _local(c.tag) == "title" and c.text), "")
            break
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in el.iter():
            n = _local(child.tag)
            if n == "link":
                href = child.get("href") or (child.text or "").strip()
                if href and (child.get("rel") in (None, "alternate") or "link" not in fields):
                    fields.setdefault("link", href)
            elif n in ("title", "description", "summary", "content", "encoded", "subtitle") and child.text:
                fields.setdefault(n, child.text)
        summary = fields.get("encoded") or fields.get("description") or fields.get("content") or fields.get("summary") or fields.get("subtitle") or ""
        title = _strip(fields.get("title", ""), 300)
        if title and fields.get("link"):
            entries.append({"url": fields["link"].strip()[:600], "title": title, "summary": _strip(summary, 700)})
    return channel_title, entries


def _youtube_feed(url):
    parts = urlparse(url)
    if parts.hostname and parts.hostname.endswith("youtube.com") and parts.path == "/feeds/videos.xml":
        return url
    m = CHANNEL_ID.search(parts.path)
    if m:
        return YT_FEED.format(m.group(0))
    if parse_qs(parts.query).get("channel_id"):
        return YT_FEED.format(parse_qs(parts.query)["channel_id"][0])
    # @handle or /c/name: read the channel page for its id
    try:
        _, body = safe_get(f"https://www.youtube.com{parts.path}")
    except (UnsafeURL, requests.RequestException):
        raise SourceError("Could not open that YouTube channel. Paste the channel link that contains /channel/UC...")
    text = body.decode("utf-8", "ignore")
    m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', text) or re.search(r"channel/(UC[\w-]{22})", text)
    if not m:
        raise SourceError("Could not find that channel. Paste the channel link that contains /channel/UC...")
    return YT_FEED.format(m.group(1))


def _apple_podcast_feed(url):
    m = re.search(r"/id(\d+)", url)
    if not m:
        raise SourceError("Paste the podcast's RSS feed, or its Apple Podcasts link.")
    try:
        _, body = safe_get(f"https://itunes.apple.com/lookup?id={m.group(1)}")
        import json

        feed = (json.loads(body).get("results") or [{}])[0].get("feedUrl")
    except (UnsafeURL, requests.RequestException, ValueError):
        feed = None
    if not feed:
        raise SourceError("Could not find that podcast's feed. Paste its RSS link instead.")
    return feed


def _discover(html: bytes, base):
    from urllib.parse import urljoin

    for tag in ALT_LINK.findall(html.decode("utf-8", "ignore")):
        if re.search(r"rss|atom|xml", tag, re.I):
            m = re.search(r"href=[\"']([^\"']+)[\"']", tag, re.I)
            if m:
                return urljoin(base, m.group(1))
    return None


def guess_kind(url):
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("youtube.com") or host == "youtu.be":
        return "youtube"
    if host.endswith("podcasts.apple.com"):
        return "podcast"
    return ""


def resolve(url: str, kind: str = ""):
    """Return (kind, name, feed_url, entries) for a link, or raise SourceError."""
    url = (url or "").strip()
    kind = kind or guess_kind(url)
    try:
        if kind == "youtube":
            feed_url = _youtube_feed(url)
        elif guess_kind(url) == "podcast":
            feed_url, kind = _apple_podcast_feed(url), kind or "podcast"
        else:
            feed_url = url
        final, body = safe_get(feed_url)
        try:
            name, entries = parse_feed(body)
        except SourceError:
            found = _discover(body, final)  # a normal web page: look for its feed link
            if not found:
                raise
            feed_url = found
            final, body = safe_get(feed_url)
            name, entries = parse_feed(body)
    except UnsafeURL as exc:
        raise SourceError(str(exc))
    except requests.RequestException:
        raise SourceError("Could not reach that link. Check it opens in a browser.")
    if not entries:
        raise SourceError("That feed has no episodes or posts yet.")
    return kind or "website", name or urlparse(feed_url).hostname, feed_url, entries


def refresh(source: FollowedSource, limit=15) -> int:
    """Fetch the newest entries for one source. Returns the number of new items."""
    from .models import Category

    category = Category.objects.filter(source_type="followed", is_active=True).first()
    if category is None:
        return 0
    try:
        _, body = safe_get(source.feed_url)
        _, entries = parse_feed(body)
        source.last_error = ""
    except (UnsafeURL, SourceError, requests.RequestException) as exc:
        source.last_error = str(exc)[:300]
        source.last_fetched_at = timezone.now()
        source.save(update_fields=["last_error", "last_fetched_at"])
        return 0
    created = 0
    for rank, entry in enumerate(entries[:limit]):
        _, was_new = TrendItem.objects.update_or_create(
            source=source, url=entry["url"], defaults={
                "category": category, "region": "", "title": entry["title"], "summary": entry["summary"],
                "score": 100 - rank, "fetched_at": timezone.now(),
            },
        )
        created += was_new
    source.last_fetched_at = timezone.now()
    source.save(update_fields=["last_error", "last_fetched_at"])
    return created
