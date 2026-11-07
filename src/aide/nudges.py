"""One-line "먼저 챙겨 알리기" nudges for the morning / evening message (config `nudges`, default off).

Pure local computation over data the message already gathered: no LLM, no new network call.
Texts come from calendar/task data that already went through clean(). Every function here
returns "" on any problem so a nudge can never block a message.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

from . import gcal
from .config import Config

log = logging.getLogger("aide.nudges")


def busy_line(cfg: Config, events: List[gcal.Event], task_count: int = 0) -> str:
    """Morning: "⚠ 바쁜 날: 일정 6개 · 겹치는 일정 2개 · 오늘까지 할 일 3개", or "" when not busy / off."""
    if "busy" not in cfg.nudges:
        return ""
    try:
        overlap = len(gcal.overlaps(events))
        if len(events) < cfg.busy_events and not overlap:
            return ""
        parts = [f"일정 {len(events)}개"]
        if overlap:
            parts.append(f"겹치는 일정 {overlap}개")
        if task_count:
            parts.append(f"오늘까지 할 일 {task_count}개")
        return "⚠ 바쁜 날: " + " · ".join(parts)
    except Exception:  # noqa: BLE001
        log.exception("바쁜 날 줄 계산 중 오류")
        return ""


def first_event(events: List[gcal.Event], tomorrow: date) -> Optional[gcal.Event]:
    """Earliest timed event that starts on `tomorrow`; else the first all-day / still-running one; else None."""
    mine = [e for e in events if e.day == tomorrow]
    timed = sorted((e for e in mine if e.when != "종일" and not gcal._continuing(e)), key=lambda e: e.when)
    if timed:
        return timed[0]
    return min(mine, key=gcal.order) if mine else None


def prep_line(cfg: Config, now: datetime, events: List[gcal.Event], task_count: int = 0) -> str:
    """Evening: "▶ 내일 준비: 첫 일정 08:30~10:00 출장 @본관 · 내일까지 할 일 2개", or "" when empty / off."""
    if "prep" not in cfg.nudges:
        return ""
    try:
        tomorrow = (now + timedelta(days=1)).date()
        parts = []
        first = first_event(events, tomorrow)
        if first is not None:
            parts.append("첫 일정 " + gcal.line(first))
        if task_count:
            parts.append(f"내일까지 할 일 {task_count}개")
        return "▶ 내일 준비: " + " · ".join(parts) if parts else ""
    except Exception:  # noqa: BLE001
        log.exception("내일 준비 줄 계산 중 오류")
        return ""
