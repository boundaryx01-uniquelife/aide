from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, Iterable, List

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


def fetch_feed(url: str) -> bytes:
    if not url.lower().startswith(("http://", "https://")):
        raise FeedError("http(s) 주소만 허용됩니다.")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(MAX_BYTES + 1)
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
            )
        )
    return items


def match(item: FeedItem, keywords: Iterable[str]) -> List[str]:
    hay = f"{item.title} {item.summary}".lower()
    return [k for k in keywords if k and k.lower() in hay]


def check(
    feeds: Iterable[str],
    keywords: Iterable[str],
    fetch: Callable[[str], bytes] = fetch_feed,
) -> List[Finding]:
    """One finding per matching article. The key is the article id/link, so an
    article is reported once, ever (until the state entry is pruned)."""
    keywords = [k for k in keywords if k]
    findings: List[Finding] = []
    if not keywords:
        return findings
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
            ident = item.guid or item.link or item.title
            findings.append(
                Finding(
                    key=f"news:{ident}",
                    source="news",
                    title=clean(item.title) or "(제목 없음)",
                    detail=clean("키워드: " + ", ".join(hit), 120),
                    url=clean(item.link, 500),
                )
            )
    return findings
