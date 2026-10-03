"""Today's Google Calendar events (read-only). Titles are untrusted text -> clean()."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List
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
        return Event(dt.astimezone(KST).strftime("%H:%M"), title)
    if "date" in st:
        return Event("종일", title)
    return None


def today_events(token: str, now: datetime, get: Callable = netutil.get_json) -> List[Event]:
    lo, hi = day_bounds(now)
    q = urlencode({
        "timeMin": lo.isoformat(), "timeMax": hi.isoformat(), "singleEvents": "true",
        "orderBy": "startTime", "maxResults": MAX_EVENTS,
        "fields": "items(summary,status,start,attendees(self,responseStatus))",
    })
    data = get(f"{URL}?{q}", headers={"Authorization": f"Bearer {token}"})
    items = data.get("items", []) if isinstance(data, dict) else []
    events = [e for e in (_parse(i) for i in items if isinstance(i, dict)) if e]
    return sorted(events, key=lambda e: (e.when != "종일", e.when))
