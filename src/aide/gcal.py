"""Today's Google Calendar events (read-only). Titles are untrusted text -> clean()."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional
from urllib.parse import urlencode

from . import netutil
from .models import clean

KST = timezone(timedelta(hours=9))  # Korea has no DST; avoids needing tzdata on Windows
URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
MAX_EVENTS = 20


@dataclass(frozen=True)
class Event:
    when: str   # "09:30" or "종일"
    title: str
    day: Optional[date] = None   # KST calendar day it starts on (None only for hand-made events)


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


def _query(token, lo: datetime, hi: datetime, max_results: int, get) -> List[Event]:
    q = urlencode({
        "timeMin": lo.isoformat(), "timeMax": hi.isoformat(), "singleEvents": "true",
        "orderBy": "startTime", "maxResults": max_results,
        "fields": "items(summary,status,start,attendees(self,responseStatus))",
    })
    data = get(f"{URL}?{q}", headers={"Authorization": f"Bearer {token}"})
    items = data.get("items", []) if isinstance(data, dict) else []
    return [e for e in (_parse(i) for i in items if isinstance(i, dict)) if e]


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
