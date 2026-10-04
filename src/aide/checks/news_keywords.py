from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable, List, Optional

from ..models import Finding, clean

log = logging.getLogger("aide.news")

MAX_BYTES = 2_000_000
USER_AGENT = "aide/0.1 (+personal-assistant)"


class FeedError(Exception):
    pass


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    guid: str
    summary: str
    published: str = ""  # raw date text from the feed; see parse_date()


def fetch_feed(url: str) -> bytes:
    if not url.lower().startswith(("http://", "https://")):
        raise FeedError("http(s) 주소만 허용됩니다.")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise FeedError(f"가져오기 실패: HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FeedError(f"가져오기 실패: {type(e).__name__}") from None
    if len(data) > MAX_BYTES:
        raise FeedError("피드가 너무 큽니다.")
    return data


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(el, name: str) -> str:
    for child in el:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _link(el) -> str:
    for child in el:
        if _local(child.tag) == "link":
            href = child.attrib.get("href")  # Atom
            if href:
                return href.strip()
            if (child.text or "").strip():  # RSS
                return child.text.strip()
    return ""


def parse_feed(data: bytes) -> List[FeedItem]:
    """Parse RSS 2.0 / Atom. DTDs and entities are rejected outright: the stdlib XML
    parser is exposed to entity-expansion attacks and feeds are untrusted input."""
    low = data.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise FeedError("DTD/ENTITY 가 포함된 피드는 거부합니다.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise FeedError("XML 해석 실패") from None
    items: List[FeedItem] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        summary = re.sub(r"<[^>]+>", " ", _text(el, "description") or _text(el, "summary"))
        items.append(
            FeedItem(
                title=_text(el, "title"),
                link=_link(el),
                guid=_text(el, "guid") or _text(el, "id"),
                summary=summary,
                published=_text(el, "pubDate") or _text(el, "published") or _text(el, "updated") or _text(el, "date"),
            )
        )
    return items


def match(item: FeedItem, keywords: Iterable[str]) -> List[str]:
    hay = f"{item.title} {item.summary}".lower()
    return [k for k in keywords if k and k.lower() in hay]


def parse_date(text: str) -> Optional[datetime]:
    """Feed date (RSS RFC-822 or Atom ISO-8601) as an aware datetime, or None if unreadable.
    A date without a time zone is taken as local time."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.astimezone().astimezone(timezone.utc)


_SOURCE_SUFFIX = re.compile(r"\s+[-\u2013\u2014|]\s+([^-\u2013\u2014|]{1,30})$")


def normalize_title(title: str) -> str:
    """Headline reduced to its letters/digits, without the trailing " - outlet" that
    Google News appends, so the same headline from two outlets compares equal."""
    t = _SOURCE_SUFFIX.sub("", (title or "").strip())
    return re.sub(r"[\W_]+", "", t).lower()


def check(
    feeds: Iterable[str],
    keywords: Iterable[str],
    fetch: Callable[[str], bytes] = fetch_feed,
    now: Optional[datetime] = None,
    max_age_hours: Optional[int] = None,
) -> List[Finding]:
    """One finding per matching article, newest first.

    - age: with `now` and `max_age_hours`, articles older than that are skipped; articles
      whose date cannot be read are kept (nothing to judge them by);
    - duplicates: the same article (id/link) or the same normalized headline appears once
      per run, and the headline is also stored as an alt key so a re-post by another
      outlet is not reported again later.
    """
    keywords = [k for k in keywords if k]
    if not keywords:
        return []
    cutoff = None
    if now is not None and max_age_hours:
        cutoff = now.astimezone(timezone.utc) - timedelta(hours=max_age_hours)
    seen_keys, seen_titles = set(), set()
    found = []  # (published or None, Finding)
    too_old = dups = 0
    for url in feeds:
        try:
            items = parse_feed(fetch(url))
        except FeedError as e:
            log.warning("피드 건너뜀: %s", e)
            continue
        for item in items:
            hit = match(item, keywords)
            if not hit:
                continue
            published = parse_date(item.published)
            if cutoff is not None and published is not None and published < cutoff:
                too_old += 1
                continue
            ident = item.guid or item.link or item.title
            key = f"news:{ident}"
            norm = normalize_title(item.title)
            if key in seen_keys or (norm and norm in seen_titles):
                dups += 1
                continue
            seen_keys.add(key)
            if norm:
                seen_titles.add(norm)
            found.append(
                (
                    published,
                    Finding(
                        key=key,
                        source="news",
                        title=clean(item.title) or "(제목 없음)",
                        detail=clean("키워드: " + ", ".join(hit), 120),
                        url=clean(item.link, 500),
                        alt_keys=(f"newstitle:{norm}",) if norm else (),
                    ),
                )
            )
    if too_old or dups:
        log.info("뉴스: 오래된 기사 %d건, 중복 %d건 제외", too_old, dups)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    found.sort(key=lambda pf: pf[0] or epoch, reverse=True)  # stable: undated keep feed order, last
    return [f for _, f in found]
