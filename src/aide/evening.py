"""One evening wrap-up per day (default off; set `evening_hour`).

Tomorrow's calendar, notices whose deadline is within 3 days, today's commits and folders
with uncommitted changes. Informational only: no buttons, no actions.

Stage 2b adds an optional "내일의 핵심"/"이번 주 핵심" section on top, picked by an LLM
from a closed set (index + reason code only -- see llm.py). Off by default; when off, or
when it fails/validates to nothing, the message is unchanged from before.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import accounts, gcal, gtasks, llm, netutil, rules as rules_mod, telegram
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
HIGHLIGHT_SCOPE = "evening"


def _is_weekly(now: datetime) -> bool:
    return now.weekday() == SUNDAY  # Sunday evening: brief the whole coming week


def _gather(cfg: Config, now: datetime, fetch_events, fetch_week, fetch):
    """Every network call the message needs (calendar + deadline notices). Split out
    of message-building so it can run OUTSIDE the state lock, and so the same
    `events`/`rows` feed both the regular sections and (stage 2b) the highlight picker."""
    weekly = _is_weekly(now)
    events, failed, total = [], [], 0
    if cfg.calendar_enabled:
        if weekly:
            first = (now + timedelta(days=1)).date()
            events, failed, total = accounts.gather_events(
                cfg, lambda token: fetch_week(token, first, 7),
                lambda e: (e.day or first, e.when != "종일", e.when))
        else:
            events, failed, total = accounts.gather_events(
                cfg, lambda token: fetch_events(token, now + timedelta(days=1)),
                lambda e: (e.when != "종일", e.when))
    rows = []
    if cfg.notice_pages:
        days = WEEK_DEADLINE_DAYS if weekly else DEADLINE_DAYS
        rows = notice_pages.upcoming(cfg.notice_pages, cfg.notice_keywords, fetch, now, days, cfg.notice_exclude)
    return events, failed, total, rows


def _calendar_section(cfg: Config, now: datetime, events, failed, total) -> List[str]:
    if not cfg.calendar_enabled:
        return []
    weekly = _is_weekly(now)
    first = (now + timedelta(days=1)).date()
    title = f"■ 다음 주 일정 ({first:%m/%d}~{first + timedelta(days=6):%m/%d})" if weekly else "■ 내일 일정"
    out = [title]
    if weekly:
        if not events and not failed:
            out.append("일정이 없어요.")
        last: Optional[date] = None
        for e in events:
            if e.day != last:
                last = e.day
                out.append(f"{e.day:%m/%d}({WEEKDAYS[e.day.weekday()]})" if e.day else "")
            out.append(f"  • {e.when} {e.title}")
    else:
        out += [f"• {e.when} {e.title}" for e in events]
        if not events and not failed:
            out.append("일정이 없어요.")
    out += accounts.failure_lines(failed, total)
    return out + [""]


def _deadline_section(cfg: Config, now: datetime, rows) -> List[str]:
    if not cfg.notice_pages:
        return []
    days = WEEK_DEADLINE_DAYS if _is_weekly(now) else DEADLINE_DAYS
    out = [f"■ 마감 임박 ({days}일 이내)"]
    out += [f"• D-{left} {title} (~{due:%m/%d})" for title, due, left, _ in rows] or ["없어요."]
    return out + [""]


def _candidates(cfg: Config, now: datetime, events, rows) -> List[Tuple[str, str, str]]:
    """(kind, when, text) triples for highlight-picking: tomorrow's/the week's events
    (minus any account on `llm_exclude_accounts`) plus upcoming deadlines. `when` and
    `text` together are exactly what a regular line already shows -- the highlight
    bullet just appends a fixed reason phrase to this same rendering."""
    weekly = _is_weekly(now)
    out: List[Tuple[str, str, str]] = []
    for e in events:
        if accounts.event_excluded(e, cfg.llm_exclude_accounts):
            continue
        when = f"{e.day:%m/%d}({WEEKDAYS[e.day.weekday()]}) {e.when}" if weekly and e.day else e.when
        out.append(("event", when, e.title))
    out += [("deadline", f"D-{left}", f"{title} (~{due:%m/%d})") for title, due, left, _ in rows]
    return out


def highlight_picks(
    cfg: Config, now: datetime, events, rows, *, calls_today: int, api_key: str, call_status: Optional[dict] = None,
) -> List[str]:
    """Rendered highlight bullet lines, or [] (feature off, too few candidates, the
    call failed, or nothing was picked). Every word is either an original event's/
    deadline's own when+text or one of llm.WHY_PHRASES -- never text the model wrote."""
    if not cfg.llm_enabled or HIGHLIGHT_SCOPE not in cfg.llm_highlights:
        return []
    candidates = _candidates(cfg, now, events, rows)
    items = [{"kind": k, "when": w, "text": t} for k, w, t in candidates]
    picks = llm.pick(
        items, cfg, now=now, scope=HIGHLIGHT_SCOPE, calls_today=calls_today, api_key=api_key, call_status=call_status,
    )
    if not picks:
        return []
    return [f"• {candidates[i][1]} {candidates[i][2]} — {llm.WHY_PHRASES[why]}" for i, why in picks]


def hidden_lines(cfg: Config, now: datetime) -> List[str]:
    """Sunday evening (stage L1): what the owner's rules kept out of notifications in the
    last 7 days, per sender. This is the safety net for a rule that hides too much, so it
    is not switchable on its own: it appears whenever rules are on and something was hidden."""
    if not cfg.rules_enabled or not _is_weekly(now):
        return []
    counts = State(cfg.resolved_state_path()).hidden_summary(now)
    if not counts:
        return []
    labels = rules_mod.Rules(rules_mod.path_for(cfg.resolved_state_path())).rules
    out = ["■ 걸러낸 것 (지난 7일)"]
    for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
        name = (labels.get(key) or {}).get("label", "")
        addr = key[len("mail:"):] if key.startswith("mail:") else key
        out.append(f"• {name} <{addr}>: {n}건" if name else f"• <{addr}>: {n}건")
    out.append("잘못 걸러졌다면 아래 [규칙 목록]에서 지워 주세요.")
    return out


def _assemble(cfg: Config, now: datetime, events, failed, total, rows, highlight_lines=(), hidden=(),
              tasks_lines=()) -> str:
    lines = [f"저녁 정리 · {now:%m/%d}({WEEKDAYS[now.weekday()]})", ""]
    if highlight_lines:
        header = "이번 주 핵심" if _is_weekly(now) else "내일의 핵심"
        lines += [f"■ {header} (AI 판단)", *highlight_lines, ""]
    lines += _calendar_section(cfg, now, events, failed, total)
    lines += _deadline_section(cfg, now, rows)
    lines += tasks_lines

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
    if hidden:
        lines += ["", *hidden]
    return "\n".join(lines).rstrip()


def _tasks_lines(cfg: Config, now: datetime, fetch_tasks) -> List[str]:
    return gtasks.section(cfg, now, kind="weekly" if _is_weekly(now) else "evening", fetch_fn=fetch_tasks)


def build_message(cfg: Config, now: datetime, *, fetch_events=gcal.today_events,
                  fetch=news_keywords.fetch_feed, fetch_week=gcal.events_between, fetch_tasks=gtasks.fetch) -> str:
    """Preview/dry-run path: gathers and assembles in one call, with NO highlight
    section -- stage 2b, like 2a, never runs on a preview (it is a paid call)."""
    events, failed, total, rows = _gather(cfg, now, fetch_events, fetch_week, fetch)
    return _assemble(cfg, now, events, failed, total, rows, hidden=hidden_lines(cfg, now),
                     tasks_lines=_tasks_lines(cfg, now, fetch_tasks))


def run(cfg: Config, *, send: bool = False, force: bool = False, now: Optional[datetime] = None,
        sender: Callable[..., None] = telegram.send, fetch=news_keywords.fetch_feed,
        fetch_events=gcal.today_events, fetch_week=gcal.events_between, fetch_tasks=gtasks.fetch) -> int:
    """Exit codes: 0 ok (including 'not time yet' / 'already sent'), 1 send failed, 2 setup problem.

    Dry run (default) prints the message and changes nothing. With --send the day is
    marked only AFTER a successful send. Runs every heartbeat, so the skip cases stay quiet.

    Network calls (calendar, notices, and stage 2b's LLM pick) all happen OUTSIDE the
    state lock; only the final "already sent?" check, the send itself, and bookkeeping
    happen inside it (same principle as heartbeat.py's `collect()`).
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

    if not send:
        print(build_message(cfg, now, fetch_events=fetch_events, fetch=fetch, fetch_week=fetch_week,
                            fetch_tasks=fetch_tasks))
        return 0
    if not force:
        if now.hour < (cfg.evening_hour or 0):
            return 0
        if in_quiet_hours(now, cfg.quiet_start, cfg.quiet_end):
            return 0
        if State(path).is_seen(key):
            log.debug("오늘 저녁 정리는 이미 보냈습니다.")
            return 0

    events, failed, total, rows = _gather(cfg, now, fetch_events, fetch_week, fetch)
    call_status: dict = {}
    highlight_lines = highlight_picks(
        cfg, now, events, rows, calls_today=State(path).llm_calls_today(now),
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""), call_status=call_status,
    )
    hidden = hidden_lines(cfg, now)
    tasks_lines = _tasks_lines(cfg, now, fetch_tasks)   # no lock held, like the other fetches
    message = _assemble(cfg, now, events, failed, total, rows, highlight_lines, hidden, tasks_lines)
    extra = {"buttons": [[("📋 규칙 목록", "r:list")]]} if hidden else {}

    try:
        with locked(path):
            state = State(path)
            if call_status.get("attempted"):
                state.record_llm_call(now)   # every billed attempt counts, even if we bail out below
            if not force and state.is_seen(key):
                if call_status.get("attempted"):
                    state.save()
                return 0
            try:
                sender(message, token=token, chat_id=chat_id, **extra)
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
