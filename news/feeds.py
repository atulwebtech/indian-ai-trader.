"""Free news headlines from public RSS feeds (no API key, no signup).

Only feeds that were checked and returned fresh items are listed. Extra feeds can be added in .env as
NEWS_EXTRA_FEEDS=url1,url2. A failing feed is skipped; the others still work.
"""
import hashlib
import html
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
G = "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q="
FEEDS = [
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Economic Times", "https://economictimes.indiatimes.com/rssfeedstopstories.cms"),
    ("Livemint", "https://www.livemint.com/rss/markets"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Google News", G + "nifty+OR+sensex+when:1d"),
    ("Google News", G + "RBI+OR+%22Reserve+Bank%22+OR+SEBI+when:1d"),
    ("Google News", G + "%22stock+market%22+OR+%22Wall+Street%22+OR+Fed+OR+crude+when:1d"),
]


@dataclass(frozen=True)
class Headline:
    id: str
    title: str
    source: str
    link: str
    published: datetime   # timezone-aware, UTC


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:90]


def parse_rss(xml_text: str, feed_name: str) -> list:
    root = ET.fromstring(xml_text.strip().encode("utf-8", "ignore"))
    out = []
    for it in root.findall(".//item"):
        title = html.unescape((it.findtext("title") or "").strip())
        if not title:
            continue
        source = feed_name
        if feed_name == "Google News" and " - " in title:        # Google appends " - Publisher"
            title, source = title.rsplit(" - ", 1)
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "")
            pub = pub.astimezone(timezone.utc) if pub.tzinfo else pub.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        out.append(Headline(hashlib.sha1(_norm(title).encode()).hexdigest()[:12], title.strip(), source.strip(),
                            (it.findtext("link") or "").strip(), pub))
    return out


def fetch_all(session=None, now=None, max_age_hours=12, extra=None, timeout=15) -> tuple:
    """Return (headlines newest first, errors). Duplicate stories (same normalised title) are merged."""
    s = session or requests.Session()
    now = now or datetime.now(timezone.utc)
    feeds = list(FEEDS) + [("Extra", u.strip()) for u in (extra if extra is not None else os.getenv("NEWS_EXTRA_FEEDS", "").split(",")) if u.strip()]
    seen, out, errors = set(), [], []
    for name, url in feeds:
        try:
            r = s.get(url, headers={"User-Agent": UA}, timeout=timeout)
            r.raise_for_status()
            items = parse_rss(r.text, name)
        except (requests.RequestException, ET.ParseError, ValueError) as e:
            errors.append(f"{name}: {type(e).__name__}")
            log.warning("news feed %s failed: %s", name, e)
            continue
        for h in items:
            age = (now - h.published).total_seconds() / 3600
            if -0.1 <= age <= max_age_hours and h.id not in seen:
                seen.add(h.id)
                out.append(h)
    out.sort(key=lambda h: h.published, reverse=True)
    return out, errors
