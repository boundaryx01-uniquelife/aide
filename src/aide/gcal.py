"""Today's Google Calendar events (read-only). Titles are untrusted text -> clean()."""
from __future__ import annotations

import logging
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
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
    end: str = ""                    # "HH:MM" end of a timed event ("" = unknown / all-day)
    end_day: Optional[date] = None   # last day the event covers (inclusive); None = unknown
    location: str = ""               # untrusted text -> clean(40)
    began: Optional[date] = None     # real start day when `day` was moved to the first queried day


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
    loc = item.get("location")
    loc = clean(loc, 40) if isinstance(loc, str) else ""
    st = item.get("start") or {}
    en = item.get("end") or {}
    if "dateTime" in st:
        try:
            dt = datetime.fromisoformat(st["dateTime"].replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        k = dt.astimezone(KST)
        end, end_day = "", None
        try:
            et = datetime.fromisoformat(str(en["dateTime"]).replace("Z", "+00:00"))
            if et.tzinfo is None:
                et = et.replace(tzinfo=KST)
            ek = et.astimezone(KST)
            if ek >= k:
                end = ek.strftime("%H:%M")
                end_day = ek.date()
                if ek.hour == 0 and ek.minute == 0 and ek > k:
                    end_day = ek.date() - timedelta(days=1)   # ends exactly at midnight -> previous day
        except (KeyError, ValueError, TypeError):
            end, end_day = "", None
        return Event(k.strftime("%H:%M"), title, k.date(), end=end, end_day=end_day, location=loc)
    if "date" in st:
        try:
            d = date.fromisoformat(st["date"])
        except ValueError:
            return None
        end_day = d
        try:
            last = date.fromisoformat(en["date"]) - timedelta(days=1)   # Google's end.date is exclusive
            if last >= d:
                end_day = last
        except (KeyError, ValueError, TypeError):
            pass
        return Event("종일", title, d, end_day=end_day, location=loc)
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
        "fields": "items(summary,status,start,end,location,attendees(self,responseStatus))",
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


def order(e: Event) -> tuple:
    """Sort key within a day: all-day first, then still-running events, then timed ones, then by start time."""
    return (0 if e.when == "종일" else 1 if _continuing(e) else 2, e.when)


def _shift(events: List[Event], first: date) -> List[Event]:
    """An event that began before `first` is shown on `first`; its real start day is kept in `began`."""
    return [replace(e, day=first, began=e.day) if e.day and e.day < first else e for e in events]


def today_events(token: str, now: datetime, get: Callable = netutil.get_json) -> List[Event]:
    lo, hi = day_bounds(now)
    events = _shift(_query(token, lo, hi, MAX_EVENTS, get), lo.date())
    return sorted(events, key=order)


def events_between(token: str, first: date, days: int, get: Callable = netutil.get_json) -> List[Event]:
    """Events from `first` for `days` days, ordered by day, all-day first. A multi-day event that
    began before `first` is shown on `first` as "in progress" (its real start is kept in `began`)."""
    lo = datetime(first.year, first.month, first.day, tzinfo=KST)
    events = _shift(_query(token, lo, lo + timedelta(days=days), 50, get), first)
    return sorted(events, key=lambda e: (e.day or first,) + order(e))


# ---- display ----------------------------------------------------------------------------------

def _md(d: date) -> str:
    return f"{d:%m/%d}"


def _continuing(e: Event, on_day: Optional[date] = None) -> bool:
    cur = on_day or e.day
    orig = e.began or e.day
    return bool(cur and orig and cur > orig)


def line(e: Event, on_day: Optional[date] = None) -> str:
    """One calendar line without the leading bullet (the single place that formats an event)."""
    cur = on_day or e.day
    allday = e.when == "종일"
    multi = bool(e.end_day and e.day and e.end_day > e.day)
    if _continuing(e, on_day):
        if e.end_day is None:
            head = "(진행 중)"
        elif allday:
            head = f"(진행 중, ~{_md(e.end_day)})"
        else:
            head = f"(진행 중, ~{_md(e.end_day)} {e.end})" if e.end else f"(진행 중, ~{_md(e.end_day)})"
    elif allday:
        head = f"종일 ~{_md(e.end_day)}" if multi else "종일"
    elif not e.end:
        head = e.when
    elif multi:
        head = f"{e.when}~{_md(e.end_day)} {e.end}"
    else:
        head = f"{e.when}~{e.end}"
    out = f"{head} {e.title}"
    return f"{out} @{e.location}" if e.location else out


def _minutes(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:5])


def _span(e: Event) -> Optional[Tuple[int, int]]:
    if e.when == "종일" or not e.end or e.end_day is None or e.day is None or _continuing(e):
        return None
    start = _minutes(e.when)
    if e.end_day > e.day or e.end == "00:00":
        stop = 1440
    else:
        stop = _minutes(e.end)
    return (start, stop) if stop > start else None


def overlaps(events: List[Event]) -> set:
    """Indexes of events whose [start, end) overlaps another timed event of the same display day.
    All-day, in-progress and end-less events are ignored; touching (10-11 and 11-12) is not overlap."""
    spans = [(i, e.day, _span(e)) for i, e in enumerate(events)]
    spans = [x for x in spans if x[2] is not None]
    hit = set()
    for a in range(len(spans)):
        for b in range(a + 1, len(spans)):
            (i, di, si), (j, dj, sj) = spans[a], spans[b]
            if di == dj and si[0] < sj[1] and sj[0] < si[1]:
                hit.update((i, j))
    return hit


def lines(events: List[Event]) -> List[str]:
    """`line()` for each event, with " ⚠ 겹침" appended where it overlaps another one."""
    ov = overlaps(events)
    return [line(e) + (" ⚠ 겹침" if i in ov else "") for i, e in enumerate(events)]
