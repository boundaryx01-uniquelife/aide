"""One evening wrap-up per day (default off; set `evening_hour`).

Tomorrow's calendar, notices whose deadline is within 3 days, today's commits and folders
with uncommitted changes. Informational only: no buttons, no actions.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional

from . import gcal, google_auth, netutil, telegram
from .checks import git_dirty, news_keywords, notice_pages, yesterday
from .config import Config
from .heartbeat import in_quiet_hours
from .state import State, StateLocked, locked

log = logging.getLogger("aide.evening")
WEEKDAYS = "월화수목금토일"
MAX_COMMITS_SHOWN = 3
DEADLINE_DAYS = 3
WEEK_DEADLINE_DAYS = 7
SUNDAY = 6


def _is_weekly(now: datetime) -> bool:
    return now.weekday() == SUNDAY  # Sunday evening: brief the whole coming week


def _calendar_section(cfg: Config, now: datetime, fetch_events, fetch_week) -> List[str]:
    if not cfg.calendar_enabled:
        return []
    weekly = _is_weekly(now)
    first = (now + timedelta(days=1)).date()
    title = f"■ 다음 주 일정 ({first:%m/%d}~{first + timedelta(days=6):%m/%d})" if weekly else "■ 내일 일정"
    out = [title]
    try:
        token = google_auth.access_token(cfg.resolved_google_client(), cfg.resolved_google_token())
        if weekly:
            events = fetch_week(token, first, 7)
            if not events:
                out.append("일정이 없어요.")
            last: Optional[date] = None
            for e in events:
                if e.day != last:
                    last = e.day
                    out.append(f"{e.day:%m/%d}({WEEKDAYS[e.day.weekday()]})" if e.day else "")
                out.append(f"  • {e.when} {e.title}")
        else:
            events = fetch_events(token, now + timedelta(days=1))
            out += [f"• {e.when} {e.title}" for e in events] or ["일정이 없어요."]
    except (google_auth.GoogleAuthError, netutil.NetError) as e:
        log.warning("일정 조회 실패: %s", e)
        out.append("불러오지 못했어요 (로그 확인).")
    return out + [""]


def _deadline_section(cfg: Config, now: datetime, fetch) -> List[str]:
    if not cfg.notice_pages:
        return []
    days = WEEK_DEADLINE_DAYS if _is_weekly(now) else DEADLINE_DAYS
    rows = notice_pages.upcoming(cfg.notice_pages, cfg.notice_keywords, fetch, now, days, cfg.notice_exclude)
    out = [f"■ 마감 임박 ({days}일 이내)"]
    out += [f"• D-{left} {title} (~{due:%m/%d})" for title, due, left, _ in rows] or ["없어요."]
    return out + [""]


def build_message(cfg: Config, now: datetime, *, fetch_events=gcal.today_events,
                  fetch=news_keywords.fetch_feed, fetch_week=gcal.events_between) -> str:
    lines = [f"저녁 정리 · {now:%m/%d}({WEEKDAYS[now.weekday()]})", ""]
    lines += _calendar_section(cfg, now, fetch_events, fetch_week)
    lines += _deadline_section(cfg, now, fetch)

    if cfg.watch_repos:   # no folders configured (e.g. on the server): leave the section out
        lines.append("■ 오늘 작업")
        any_work = False
        for repo in cfg.watch_repos:
            subjects = yesterday.commits_on(Path(repo), now.date())
            if not subjects:
                continue
            any_work = True
            lines.append(f"• {Path(repo).name}: 커밋 {len(subjects)}개")
            lines += [f"  - {s}" for s in subjects[:MAX_COMMITS_SHOWN]]
            if len(subjects) > MAX_COMMITS_SHOWN:
                lines.append(f"  …외 {len(subjects) - MAX_COMMITS_SHOWN}개")
        if not any_work:
            lines.append("커밋 기록이 없어요.")

    pending = []
    for repo in cfg.watch_repos:
        n = git_dirty.count_changes(Path(repo))
        if n is not None and n >= cfg.git_dirty_threshold:
            pending.append(f"• {Path(repo).name}: 커밋 안 된 변경 {n}개")
    if pending:
        lines += ["", "■ 정리할 것"] + pending
    return "\n".join(lines).rstrip()


def run(cfg: Config, *, send: bool = False, force: bool = False, now: Optional[datetime] = None,
        sender: Callable[..., None] = telegram.send, fetch=news_keywords.fetch_feed,
        fetch_events=gcal.today_events, fetch_week=gcal.events_between) -> int:
    """Exit codes: 0 ok (including 'not time yet' / 'already sent'), 1 send failed, 2 setup problem.

    Dry run (default) prints the message and changes nothing. With --send the day is
    marked only AFTER a successful send. Runs every heartbeat, so the skip cases stay quiet.
    """
    now = now or datetime.now()
    if send and cfg.evening_hour is None and not force:
        log.debug("저녁 정리가 꺼져 있습니다 (evening_hour).")
        return 0
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if send and not (token and chat_id):
        log.error("--send 에는 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 필요합니다 (.env 확인).")
        return 2

    key = f"evening:{now.date().isoformat()}"
    path = cfg.resolved_state_path()
    message = lambda: build_message(cfg, now, fetch_events=fetch_events, fetch=fetch, fetch_week=fetch_week)  # noqa: E731

    if not send:
        print(message())
        return 0
    if not force:
        if now.hour < (cfg.evening_hour or 0):
            return 0
        if in_quiet_hours(now, cfg.quiet_start, cfg.quiet_end):
            return 0
        if State(path).is_seen(key):
            log.debug("오늘 저녁 정리는 이미 보냈습니다.")
            return 0
    try:
        with locked(path):
            state = State(path)
            if not force and state.is_seen(key):
                return 0
            try:
                sender(message(), token=token, chat_id=chat_id)
            except telegram.TelegramError as e:
                log.error("전송 실패: %s (다음 점검에서 재시도)", e)
                return 1
            state.mark(key, now)
            state.prune(now)
            state.save()
            log.info("저녁 정리를 보냈습니다.")
            return 0
    except StateLocked as e:
        log.error("%s 다음 점검에서 다시 시도합니다.", e)
        return 1
