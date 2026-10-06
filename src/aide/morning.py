"""Stage 1.5: one good-morning message per day, with yesterday's work.

Informational only: it reads git history and sends a short text. No buttons, no actions.
Meant to be started when the PC starts; running it again the same day does nothing.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional

from . import accounts, gcal, netutil, telegram, weather
from .checks import git_dirty, yesterday
from .config import Config
from .heartbeat import in_quiet_hours
from .state import State, StateLocked, locked

log = logging.getLogger("aide.morning")
WEEKDAYS = "월화수목금토일"
MAX_COMMITS_SHOWN = 5


def _today_section(cfg: Config, now: datetime, fetch_weather, fetch_events) -> List[str]:
    """Weather + calendar lines. Each part fails on its own and never blocks the greeting."""
    out: List[str] = []
    if cfg.weather_enabled:
        try:
            out.append(f"■ 날씨: {fetch_weather(cfg.latitude, cfg.longitude).line()}")
        except netutil.NetError as e:
            log.warning("날씨 조회 실패: %s", e)
    if cfg.calendar_enabled:
        events, failed, total = accounts.gather_events(
            cfg, lambda token: fetch_events(token, now), lambda e: (e.when != "종일", e.when))
        out.append("■ 오늘 일정")
        if events:
            out += [f"• {e.when} {e.title}" for e in events]
        elif not failed:
            out.append("일정이 없어요.")
        out += accounts.failure_lines(failed, total)
    return out + [""] if out else out


def build_message(cfg: Config, now: datetime, *, fetch_weather=weather.today, fetch_events=gcal.today_events) -> str:
    day = (now - timedelta(days=1)).date()
    lines = [f"좋은 아침이에요 · {now:%m/%d}({WEEKDAYS[now.weekday()]})", ""]
    lines += _today_section(cfg, now, fetch_weather, fetch_events)
    if cfg.watch_repos:   # no folders configured (e.g. on the server): leave the section out instead of claiming "no commits"
        lines.append(f"■ 어제({day:%m/%d}) 작업")
        any_work = False
        for repo in cfg.watch_repos:
            subjects = yesterday.commits_on(Path(repo), day)
            if not subjects:
                continue
            any_work = True
            lines.append(f"• {Path(repo).name}: 커밋 {len(subjects)}개")
            lines += [f"  - {s}" for s in subjects[:MAX_COMMITS_SHOWN]]
            if len(subjects) > MAX_COMMITS_SHOWN:
                lines.append(f"  …외 {len(subjects) - MAX_COMMITS_SHOWN}개")
        if not any_work:
            lines.append("커밋 기록이 없어요.")

    pending: List[str] = []
    for repo in cfg.watch_repos:
        n = git_dirty.count_changes(Path(repo))
        if n is not None and n >= cfg.git_dirty_threshold:
            pending.append(f"• {Path(repo).name}: 커밋 안 된 변경 {n}개")
    if pending:
        lines += ["", "■ 이어서 할 일"] + pending
    return "\n".join(lines).rstrip()


def run(
    cfg: Config,
    *,
    send: bool = False,
    force: bool = False,
    now: Optional[datetime] = None,
    sender: Callable[..., None] = telegram.send,
) -> int:
    """Exit codes: 0 ok (including 'already sent today' / quiet hours), 1 send failed, 2 setup problem.

    Dry run (default) prints the message and changes nothing. With --send the day is
    marked only AFTER a successful send, so a failure can be retried.
    """
    now = now or datetime.now()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if send and not (token and chat_id):
        log.error("--send 에는 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 필요합니다 (.env 확인).")
        return 2

    key = f"morning:{now.date().isoformat()}"
    path = cfg.resolved_state_path()

    if not send:
        print(build_message(cfg, now))
        if State(path).is_seen(key):
            log.info("(오늘 인사는 이미 보냈습니다. --send 는 다시 보내지 않습니다.)")
        return 0

    if not force and in_quiet_hours(now, cfg.quiet_start, cfg.quiet_end):
        log.info("조용한 시간(%s-%s): 아침 인사를 건너뜁니다.", cfg.quiet_start, cfg.quiet_end)
        return 0

    try:
        with locked(path):
            state = State(path)
            if state.is_seen(key):
                log.info("오늘 아침 인사는 이미 보냈습니다.")
                return 0
            try:
                sender(build_message(cfg, now), token=token, chat_id=chat_id)
            except telegram.TelegramError as e:
                log.error("전송 실패: %s (다시 실행하면 재시도)", e)
                return 1
            state.mark(key, now)
            state.prune(now)
            state.save()
            log.info("아침 인사를 보냈습니다.")
            return 0
    except StateLocked as e:
        log.error("%s 잠시 뒤 다시 실행하세요.", e)
        return 1
