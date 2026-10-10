"""Trend ingestion: fetch fresh items per category and store them as TrendItem rows.

Only URLs configured by an admin in Category.source_config are fetched (no user supplied
URLs, so no SSRF surface). Items are summaries, never full copies of articles.
"""
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

from .models import Category, TrendItem

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "PostPilotBot/1.0 (+content automation)"}


def _get(url, **kwargs):
    resp = requests.get(url, headers=HEADERS, timeout=settings.HTTP_TIMEOUT, **kwargs)
    resp.raise_for_status()
    return resp


def _strip(text, limit=500):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _fmt(value, region):
    """Fill {cc} / {cc_lower} placeholders in a source config so one recipe works for every country."""
    if isinstance(value, str):
        return value.replace("{cc_lower}", region.lower()).replace("{cc}", region.upper())
    if isinstance(value, list):
        return [_fmt(v, region) for v in value]
    if isinstance(value, dict):
        return {k: _fmt(v, region) for k, v in value.items()}
    return value


def fetch_rss(config):
    items = []
    for feed in config.get("feeds", []):
        try:
            root = ET.fromstring(_get(feed).content)
        except (requests.RequestException, ET.ParseError) as exc:
            log.warning("RSS fetch failed %s: %s", feed, exc)
            continue
        for node in root.iter("item"):
            title, link = node.findtext("title"), node.findtext("link")
            if title and link:
                items.append({"url": link.strip(), "title": _strip(title, 300), "summary": _strip(node.findtext("description"))})
    return items[: config.get("limit", 20)]


def fetch_newsdata(config):
    if not settings.NEWSDATA_API_KEY:
        log.info("NEWSDATA_API_KEY not set; skipping newsdata source")
        return []
    params = {"apikey": settings.NEWSDATA_API_KEY, "language": config.get("language", "en"), "country": config.get("country", "us")}
    if config.get("category"):
        params["category"] = config["category"]
    try:
        data = _get("https://newsdata.io/api/1/latest", params=params).json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("newsdata fetch failed: %s", exc)
        return []
    return [
        {"url": r["link"], "title": _strip(r.get("title"), 300), "summary": _strip(r.get("description"))}
        for r in data.get("results", [])
        if r.get("link") and r.get("title")
    ][: config.get("limit", 20)]


def fetch_hn(config):
    try:
        ids = _get("https://hacker-news.firebaseio.com/v0/topstories.json").json()[: config.get("limit", 15)]
        items = []
        for story_id in ids:
            story = _get(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json").json() or {}
            if story.get("title") and story.get("url"):
                items.append({"url": story["url"], "title": _strip(story["title"], 300), "summary": f"{story.get('score', 0)} points on Hacker News"})
        return items
    except (requests.RequestException, ValueError) as exc:
        log.warning("Hacker News fetch failed: %s", exc)
        return []


def fetch_apple_podcasts(config):
    country, limit = config.get("country", "us"), config.get("limit", 10)
    url = f"https://rss.applemarketingtools.com/api/v2/{country}/podcasts/top/{limit}/podcasts.json"
    try:
        results = _get(url).json().get("feed", {}).get("results", [])
    except (requests.RequestException, ValueError) as exc:
        log.warning("Apple podcasts fetch failed: %s", exc)
        return []
    return [
        {"url": r["url"], "title": f"{r.get('name')} by {r.get('artistName')}", "summary": f"Genre: {', '.join(g.get('name', '') for g in r.get('genres', []))}. Currently in the top podcasts charts."}
        for r in results
        if r.get("url")
    ]


FETCHERS = {"rss": fetch_rss, "newsdata": fetch_newsdata, "hn": fetch_hn, "apple_podcasts": fetch_apple_podcasts}


def ingest_category(category: Category, region: str = "US") -> int:
    fetcher = FETCHERS.get(category.source_type)
    if fetcher is None:  # evergreen categories have nothing to fetch
        return 0
    created = 0
    for rank, item in enumerate(fetcher(_fmt(category.source_config, region))):
        _, was_new = TrendItem.objects.update_or_create(
            category=category,
            region=region,
            url=item["url"][:600],
            defaults={"title": item["title"], "summary": item["summary"], "score": 100 - rank, "fetched_at": timezone.now()},
        )
        created += was_new
    return created


def purge_old():
    cutoff = timezone.now() - timedelta(days=settings.TREND_RETENTION_DAYS)
    return TrendItem.objects.filter(fetched_at__lt=cutoff).delete()[0]
