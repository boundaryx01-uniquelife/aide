"""Today's Google Calendar events (read-only). Titles are untrusted text -> clean()."""
from __future__ import annotations

import logging
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import quote, urlencode

from . import netutil
from .models import clean

KST = timezone(timedelta(hours=9))  # Korea has no DST; avoids needing tzdata on Windows
log = logging.getLogger("aide.gcal")
BASE = "https://www.googleapis.com/calendar/v3"
URL = BASE + "/calendars/primary/events"
CAL_LIST = BASE + "/users/me/calendarList?" + urlencode({"fields": "items(id,summary,primary)"})
MAX_EVENTS = 20
MAX_CALENDARS = 8
PRIMARY_NAMES = ("기본", "primary")   # in the config's `calendars` list these mean the account's main calendar

# Which calendars of the account being read right now (set by accounts.gather_events, so the
# many fetch_events/fetch_week callers and their test fakes keep their simple signatures).
_calendars: ContextVar[tuple] = ContextVar("aide_calendars", default=())


@contextmanager
def only_calendars(names: Iterable[str]) -> Iterator[None]:
    token = _calendars.set(tuple(names))
    try:
        yield
    finally:
        _calendars.reset(token)


@dataclass(frozen=True)
class Event:
    when: str   # "09:30" or "종일"
    title: str
    day: Optional[date] = None   # KST calendar day it starts on (None only for hand-made events)
    accounts: Tuple[str, ...] = ()   # which Google account(s) this came from; filled by accounts.gather_events


def day_bounds(now: datetime):
    start = datetime(now.year, now.month, now.day, tzinfo=KST)
    return start, start + timedelta(days=1)


def _parse(item: dict):
    if item.get("status") == "cancelled":
        return None
    for a in item.get("attendees") or []:
        if a.get("self") and a.get("responseStatus") == "declined":
            return None
    title = clean(item.get("summary", ""), 80) or "(제목 없음)"
    st = item.get("start") or {}
    if "dateTime" in st:
        try:
            dt = datetime.fromisoformat(st["dateTime"].replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        k = dt.astimezone(KST)
        return Event(k.strftime("%H:%M"), title, k.date())
    if "date" in st:
        try:
            return Event("종일", title, date.fromisoformat(st["date"]))
        except ValueError:
            return None
    return None


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def calendar_ids(token, get) -> List[str]:
    """Calendar ids to read for the current account. Nothing configured -> only the primary one.
    Configured names are matched against the calendar's name (or id); "기본"/"primary" is the main
    calendar. A name that matches nothing is logged and skipped; trouble listing -> primary only."""
    wanted = _calendars.get()
    if not wanted:
        return ["primary"]
    try:
        data = get(CAL_LIST, headers={"Authorization": f"Bearer {token}"})
    except netutil.NetError as e:
        log.warning("캘린더 목록 조회 실패, 기본 캘린더만 읽음: %s", e)
        return ["primary"]
    items = [c for c in (data.get("items", []) if isinstance(data, dict) else [])
             if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"]]
    ids: List[str] = []
    for name in wanted:
        n = _norm(name)
        if n in PRIMARY_NAMES:
            found = [c["id"] for c in items if c.get("primary") is True] or ["primary"]
        else:
            found = [c["id"] for c in items if isinstance(c.get("summary"), str) and _norm(c["summary"]) == n]
            found = found or [c["id"] for c in items if c["id"] == name]
        if not found:
            log.warning("캘린더를 찾지 못했어요: %s", name)
        for cid in found:
            if cid not in ids:
                ids.append(cid)
    return ids[:MAX_CALENDARS] or ["primary"]


def _query_one(token, cal: str, lo: datetime, hi: datetime, max_results: int, get) -> List[Event]:
    q = urlencode({
        "timeMin": lo.isoformat(), "timeMax": hi.isoformat(), "singleEvents": "true",
        "orderBy": "startTime", "maxResults": max_results,
        "fields": "items(summary,status,start,attendees(self,responseStatus))",
    })
    data = get(f"{BASE}/calendars/{quote(cal, safe='')}/events?{q}", headers={"Authorization": f"Bearer {token}"})
    items = data.get("items", []) if isinstance(data, dict) else []
    return [e for e in (_parse(i) for i in items if isinstance(i, dict)) if e]


def _query(token, lo: datetime, hi: datetime, max_results: int, get) -> List[Event]:
    """Events of all chosen calendars, merged. One broken calendar is skipped; every calendar
    failing re-raises so the caller still reports "could not load"."""
    out: List[Event] = []
    seen = set()
    last_err = None
    ok = 0
    for cal in calendar_ids(token, get):
        try:
            got = _query_one(token, cal, lo, hi, max_results, get)
        except netutil.NetError as e:
            last_err = e
            continue
        ok += 1
        for e in got:
            k = (e.day, e.when, e.title)
            if k not in seen:      # the same event can sit on two calendars
                seen.add(k)
                out.append(e)
    if not ok and last_err is not None:
        raise last_err
    return out


def today_events(token: str, now: datetime, get: Callable = netutil.get_json) -> List[Event]:
    lo, hi = day_bounds(now)
    events = _query(token, lo, hi, MAX_EVENTS, get)
    return sorted(events, key=lambda e: (e.when != "종일", e.when))


def events_between(token: str, first: date, days: int, get: Callable = netutil.get_json) -> List[Event]:
    """Events from `first` for `days` days, ordered by day, all-day first. A multi-day event that
    began before `first` is shown on `first`."""
    lo = datetime(first.year, first.month, first.day, tzinfo=KST)
    events = _query(token, lo, lo + timedelta(days=days), 50, get)
    fixed = [Event(e.when, e.title, max(e.day, first)) if e.day else e for e in events]
    return sorted(fixed, key=lambda e: (e.day or first, e.when != "종일", e.when))
