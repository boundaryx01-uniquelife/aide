"""Intraday market headlines ("특징주", "상한가", "거래량 급증", ...) from Google News searches.

Reference material for the owner's own decisions, never advice: this only collects headlines,
groups the ones about the same stock into one line, and reports each stock once per day.
Headlines are untrusted text -> clean(). Nothing here goes to an LLM.

Google News RSS can lag the real report by tens of minutes: the real-time numbers belong to
the market-data project, this is the "what is being written about" side.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from ..models import Finding, clean
from . import news_keywords as nk

log = logging.getLogger("aide.market")

MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(16, 0)     # a little after the 15:30 close: the closing round-up is news too
MAX_QUERIES = 10
MAX_AGE_HOURS = 3
NAME_RE = re.compile(r"\[[^\]]*특징주\]\s*([^,，\[\]]{1,20}?)\s*[,，]")
FEED = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"


def in_market_hours(now: datetime) -> bool:
    """Weekdays 09:00-16:00 (local time). Public holidays are not known and count as weekdays."""
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def feed_url(query: str) -> str:
    """Google News search RSS for the last day of `query`."""
    return FEED.format(q=quote(f"{query} when:1d"))


def stock_name(title: str) -> str:
    """The stock a "[특징주] 한국전력, ..." headline is about, or "" when the headline has no such shape."""
    m = NAME_RE.search(title or "")
    return clean(m.group(1), 20) if m else ""


def _line(name: str, headline: str, n: int) -> str:
    text = nk.strip_source(headline)
    if name:
        m = NAME_RE.search(text)
        rest = text[m.end():].strip() if m else text
        text = f"{name} · {clean(rest, 60)}" if rest else name
    else:
        text = clean(text, 80)
    return f"{text} (기사 {n}건)" if n > 1 else text


def check(
    queries: Iterable[str],
    exclude: Iterable[str],
    fetch: Callable[[str], bytes] = nk.fetch_feed,
    now: Optional[datetime] = None,
    max_age_hours: int = MAX_AGE_HOURS,
) -> List[Finding]:
    """One finding per stock (or per distinct headline), newest first. The key carries today's
    date, so a stock is reported once a day however many articles follow."""
    queries = [q for q in queries if q][:MAX_QUERIES]
    if not queries:
        return []
    now = now or datetime.now()
    bad = [w.lower() for w in exclude if w]
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=max_age_hours) if max_age_hours else None
    groups: Dict[str, dict] = {}   # group id -> {"name", "items": [(published, item)]}
    order: List[str] = []
    for q in queries:
        try:
            items = nk.parse_feed(fetch(feed_url(q)))
        except nk.FeedError as e:
            log.warning("시황 피드 건너뜀: %s", e)
            continue
        for item in items:
            if not nk.match(item, [q]):
                continue
            hay = f"{item.title} {item.summary}".lower()
            if any(w in hay for w in bad):
                continue
            published = nk.parse_date(item.published)
            if cutoff is not None and published is not None and published < cutoff:
                continue
            name = stock_name(item.title)
            gid = f"name:{name}" if name else f"title:{nk.normalize_title(item.title)}"
            if gid == "title:":
                continue
            g = groups.get(gid)
            if g is None:
                g = groups[gid] = {"name": name, "items": [], "seen": set()}
                order.append(gid)
            ident = item.guid or item.link or item.title
            if ident in g["seen"]:
                continue
            g["seen"].add(ident)
            g["items"].append((published, item))
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    out: List[Tuple[datetime, Finding]] = []
    for gid in order:
        g = groups[gid]
        items = sorted(g["items"], key=lambda pi: pi[0] or epoch, reverse=True)
        newest_at, newest = items[0]
        out.append((newest_at or epoch, Finding(
            key=f"market:{now.date().isoformat()}:{gid}",
            source="market",
            title=_line(g["name"], newest.title, len(items)),
            url=clean(newest.link, 500),
        )))
    out.sort(key=lambda pf: pf[0], reverse=True)
    log.info("시황: %d건(종목·제목 묶음)", len(out))
    return [f for _, f in out]
