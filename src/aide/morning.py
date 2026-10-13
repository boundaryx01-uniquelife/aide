"""Stage 1.5: one good-morning message per day, with yesterday's work.

Stage 2b adds an optional "오늘의 핵심" (today's highlights) section on top, picked
by an LLM from a closed set (index + reason code only -- see llm.py). Off by default;
when off, or when it fails/validates to nothing, the message is unchanged from before.

Informational only: it reads git history and sends a short text. No buttons, no actions.
Meant to be started when the PC starts; running it again the same day does nothing.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional

from . import accounts, gcal, llm, netutil, telegram, weather
from .checks import git_dirty, yesterday
from .config import Config
from .heartbeat import in_quiet_hours
from .state import State, StateLocked, locked

log = logging.getLogger("aide.morning")
WEEKDAYS = "월화수목금토일"
MAX_COMMITS_SHOWN = 5
HIGHLIGHT_SCOPE = "morning"


def _gather(cfg: Config, now: datetime, fetch_weather, fetch_events):
    """Every network call the message needs (weather + today's calendar). Split out
    of message-building so it can run OUTSIDE the state lock, and so the same
    `events` feed both the calendar section and (stage 2b) the highlight picker."""
    weather_result = None
    if cfg.weather_enabled:
        try:
            weather_result = fetch_weather(cfg.latitude, cfg.longitude)
        except netutil.NetError as e:
            log.warning("날씨 조회 실패: %s", e)
    events, failed, total = [], [], 0
    if cfg.calendar_enabled:
        events, failed, total = accounts.gather_events(
            cfg, lambda token: fetch_events(token, now), lambda e: (e.when != "종일", e.when))
    return weather_result, events, failed, total


def _today_section(cfg: Config, weather_result, events, failed, total) -> List[str]:
    """Weather + calendar lines from already-gathered data. Each part fails on its
    own (weather_result/events empty) and never blocks the greeting."""
    out: List[str] = []
    if cfg.weather_enabled and weather_result is not None:
        out.append(f"■ 날씨: {weather_result.line()}")
    if cfg.calendar_enabled:
        out.append("■ 오늘 일정")
        if events:
            out += [f"• {e.when} {e.title}" for e in events]
        elif not failed:
            out.append("일정이 없어요.")
        out += accounts.failure_lines(failed, total)
    return out + [""] if out else out


def pickable_events(cfg: Config, events: List[gcal.Event]) -> List[gcal.Event]:
    """Events that may be sent to the LLM for highlight-picking (stage 2b): not from
    an account on `llm_exclude_accounts`."""
    return [e for e in events if not accounts.event_excluded(e, cfg.llm_exclude_accounts)]


def highlight_picks(
    cfg: Config,
    events: List[gcal.Event],
    weather_result,
    *,
    now: datetime,
    calls_today: int,
    api_key: str,
    call_status: Optional[dict] = None,
) -> List[str]:
    """Rendered "■ 오늘의 핵심" bullet lines, or [] (feature off, too few candidates,
    the call failed, or nothing was picked). Every word on each line is either the
    original event's own `when`/`title` or one of llm.WHY_PHRASES -- never text the
    model wrote."""
    if not cfg.llm_enabled or HIGHLIGHT_SCOPE not in cfg.llm_highlights:
        return []
    candidates = pickable_events(cfg, events)
    items = [{"kind": "event", "when": e.when, "text": e.title} for e in candidates]
    context = weather_result.line() if weather_result is not None else ""
    picks = llm.pick(
        items, cfg, now=now, scope=HIGHLIGHT_SCOPE, context=context, calls_today=calls_today,
        api_key=api_key, call_status=call_status,
    )
    if not picks:
        return []
    return [f"• {candidates[i].when} {candidates[i].title} — {llm.WHY_PHRASES[why]}" for i, why in picks]


def _assemble(cfg: Config, now: datetime, weather_result, events, failed, total, highlight_lines=()) -> str:
    day = (now - timedelta(days=1)).date()
    lines = [f"좋은 아침이에요 · {now:%m/%d}({WEEKDAYS[now.weekday()]})", ""]
    if highlight_lines:
        lines += ["■ 오늘의 핵심 (AI 판단)", *highlight_lines, ""]
    lines += _today_section(cfg, weather_result, events, failed, total)
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


def build_message(cfg: Config, now: datetime, *, fetch_weather=weather.today, fetch_events=gcal.today_events) -> str:
    """Preview/dry-run path: gathers and assembles in one call, with NO highlight
    section -- stage 2b, like 2a, never runs on a preview (it is a paid call)."""
    weather_result, events, failed, total = _gather(cfg, now, fetch_weather, fetch_events)
    return _assemble(cfg, now, weather_result, events, failed, total)


def run(
    cfg: Config,
    *,
    send: bool = False,
    force: bool = False,
    now: Optional[datetime] = None,
    sender: Callable[..., None] = telegram.send,
    fetch_weather=weather.today,
    fetch_events=gcal.today_events,
) -> int:
    """Exit codes: 0 ok (including 'already sent today' / quiet hours), 1 send failed, 2 setup problem.

    Dry run (default) prints the message and changes nothing. With --send the day is
    marked only AFTER a successful send, so a failure can be retried.

    Network calls (weather, calendar, and stage 2b's LLM pick) all happen OUTSIDE
    the state lock; only the final "already sent?" check, the send itself, and
    bookkeeping happen inside it -- a slow call here must never hold up button/poll
    handling (same principle as heartbeat.py's `collect()`).
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
        print(build_message(cfg, now, fetch_weather=fetch_weather, fetch_events=fetch_events))
        if State(path).is_seen(key):
            log.info("(오늘 인사는 이미 보냈습니다. --send 는 다시 보내지 않습니다.)")
        return 0

    if not force and in_quiet_hours(now, cfg.quiet_start, cfg.quiet_end):
        log.info("조용한 시간(%s-%s): 아침 인사를 건너뜁니다.", cfg.quiet_start, cfg.quiet_end)
        return 0
    if State(path).is_seen(key):   # provisional check outside the lock: skip the gather/LLM work entirely
        log.info("오늘 아침 인사는 이미 보냈습니다.")
        return 0

    weather_result, events, failed, total = _gather(cfg, now, fetch_weather, fetch_events)
    call_status: dict = {}
    highlight_lines = highlight_picks(
        cfg, events, weather_result, now=now, calls_today=State(path).llm_calls_today(now),
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""), call_status=call_status,
    )
    message = _assemble(cfg, now, weather_result, events, failed, total, highlight_lines)

    try:
        with locked(path):
            state = State(path)   # re-read: heartbeat/poll may have written meanwhile
            if call_status.get("attempted"):
                state.record_llm_call(now)   # every billed attempt counts, even if we bail out below
            if state.is_seen(key):
                if call_status.get("attempted"):
                    state.save()
                log.info("오늘 아침 인사는 이미 보냈습니다.")
                return 0
            try:
                sender(message, token=token, chat_id=chat_id)
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
