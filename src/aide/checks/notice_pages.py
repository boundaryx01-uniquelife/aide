"""Watch public notice list pages (education offices, foundations, contests).

Server-rendered HTML only, no login. Page text is untrusted: titles go through clean().
An item is reported when its title matches a keyword. If the row shows an application
period ("~2026-10-12") the item is skipped once closed and re-reported as a reminder
at D-3 and D-1 (each stage once).
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from ..models import Finding, clean

log = logging.getLogger("aide.notice")

MIN_TITLE = 8
_END_DATE = re.compile(r"~\s*(20\d\d)[-./ ]\s*(\d{1,2})[-./ ]\s*(\d{1,2})")
# A real list row shows a date next to the title; menus/buttons/footers do not.
_ANY_DATE = re.compile(r"20\d\d\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}|(?<!\d)\d{1,2}\s*[-./]\s*\d{1,2}(?!\d)")
_NORM = re.compile(r"[\W_]+", re.UNICODE)


def decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class _Rows(HTMLParser):
    """Collect (title, href, row_text) for every link; row = enclosing <tr>/<li>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: List[Tuple[str, str, str]] = []
        self._rows: List[dict] = []
        self._a: Optional[dict] = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("tr", "li"):
            self._rows.append({"tag": tag, "text": [], "links": []})
        elif tag == "a":
            self._a = {"href": dict(attrs).get("href") or "", "text": []}

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._a is not None:
            a, self._a = self._a, None
            title = " ".join("".join(a["text"]).split())
            if self._rows:
                self._rows[-1]["links"].append((title, a["href"]))
            else:
                self.items.append((title, a["href"], title))
        elif tag in ("tr", "li") and self._rows:
            row = self._rows.pop()
            if tag == "li" and self._rows and self._rows[-1]["tag"] == "tr":
                self._rows[-1]["links"] += row["links"]  # <li> inside a table cell: the <tr> holds the dates
                return
            text = " ".join(" ".join(row["text"]).split())
            self.items += [(t, h, text) for t, h in row["links"]]

    def handle_data(self, data):
        if self._skip:
            return
        if self._a is not None:
            self._a["text"].append(data)
        for row in self._rows:
            row["text"].append(data)


def parse_items(html: str) -> List[Tuple[str, str, str]]:
    p = _Rows()
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed HTML must never break the heartbeat
        pass
    return p.items


def deadline_of(row_text: str) -> Optional[date]:
    ends = []
    for y, m, d in _END_DATE.findall(row_text):
        try:
            ends.append(date(int(y), int(m), int(d)))
        except ValueError:
            continue
    return max(ends) if ends else None


def _key(page_url: str, title: str) -> str:
    h = hashlib.sha1(f"{urlparse(page_url).netloc}|{_NORM.sub('', title).lower()}".encode()).hexdigest()[:16]
    return f"notice:{h}"


def check(
    pages: Iterable[str],
    keywords: List[str],
    fetch: Callable[[str], bytes],
    now: datetime,
    max_per_page: int = 5,
) -> List[Finding]:
    kws = [k.lower() for k in keywords if k.strip()]
    today = now.date()
    out: List[Finding] = []
    for page in pages:
        try:
            html = decode(fetch(page))
        except Exception as e:  # one broken page must not hide the others
            log.warning("공지 페이지 건너뜀: %s", e)
            continue
        taken = 0
        for raw_title, href, row in parse_items(html):
            title = clean(raw_title, 120)
            if len(title) < MIN_TITLE or not any(k in title.lower() for k in kws):
                continue
            if not _ANY_DATE.search(row.replace(raw_title, " ", 1)):
                continue  # no date in the row -> navigation, not a notice
            if "접수마감" in row or "마감됨" in row:
                continue
            due = deadline_of(row)
            left = (due - today).days if due else None
            if left is not None and left < 0:
                continue
            if taken >= max_per_page:
                break
            taken += 1
            base = _key(page, title)
            stage = "" if left is None or left > 3 else (":d1" if left <= 1 else ":d3")
            link = urljoin(page, href) if href and not href.lower().startswith(("javascript:", "#")) else page
            if urlparse(link).scheme not in ("http", "https"):
                link = page
            prefix = {"": "", ":d3": "마감 임박(D-%d) " % (left or 0), ":d1": "마감 임박(D-%d) " % (left or 0)}[stage]
            detail = f"접수 ~{due:%m/%d} (D-{left})" if due else ""
            out.append(Finding(key=base + stage, source="notice", title=prefix + title, detail=detail, url=link))
    return out
