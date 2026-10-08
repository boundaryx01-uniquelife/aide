"""Stage 3b: read-only slash commands typed in the bot's own private chat.

Three independent gates, all enforced here:
  WHO   -- the owner's TELEGRAM_CHAT_ID, in a private chat, not a bot, not forwarded.
  WHAT  -- one of a fixed set of command names (not free text). Unknown input never
           reaches a handler.
  HOW OFTEN -- a handful per batch, a cap per hour.

Handlers below take (cfg, now) and return a plain-text message. They are all
READ-ONLY: Google scopes are `*.readonly`, they never receive `State`, and they never
call telegram.send themselves -- the caller (inbox.py) does that, always to the
owner's own chat, never to whoever sent the update.
"""
from __future__ import annotations

import logging
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from . import accounts, gcal, gmail, google_auth, netutil, weather
from .checks import news_keywords, notice_pages
from .config import COMMAND_NAMES, Config
from .models import Finding

log = logging.getLogger("aide.commands")

WEEKDAYS = "월화수목금토일"
MAX_TEXT_LEN = 32        # a command is a short word, never a sentence
STALE_SECONDS = 600      # queued more than this long ago (bot/aide was down) -> skip, don't burst-execute
MAX_PER_BATCH = 3        # commands executed from a single getUpdates() result
MAX_PER_HOUR = 20        # across all batches; see State.commands_in_last_hour

HELP_NAME = "도움"  # always available, never gated by config.commands

ALIASES = {
    "오늘": "오늘", "today": "오늘",
    "일정": "일정", "week": "일정",
    "마감": "마감", "due": "마감",
    "메일": "메일", "mail": "메일",
    "도움": HELP_NAME, "help": HELP_NAME, "start": HELP_NAME,
}


def normalize(text: str) -> str:
    """Fold to a bare lowercase command word, or "" if it cannot possibly be one.

    Strips a leading '/' and a trailing '@botname' (how Telegram's own clients send
    commands in a chat that also has other bots), NFKC-folds width variants, and
    rejects anything longer than a single word -- free text is never parsed further.
    """
    text = unicodedata.normalize("NFKC", (text or "")).strip()
    if not text or len(text) > MAX_TEXT_LEN or any(c.isspace() for c in text):
        return ""
    if text.startswith("/"):
        text = text[1:]
    text = text.split("@", 1)[0]
    return text.strip().lower()


def resolve(text: str, enabled: List[str]) -> Optional[str]:
    """Canonical command name for `text`, or None if it is unknown or not enabled.

    `도움` resolves even when `enabled` is empty, so the owner can always ask what is on.
    """
    name = ALIASES.get(normalize(text))
    if name is None or (name != HELP_NAME and name not in enabled):
        return None
    return name


def is_command_message(update: dict, allowed_chat_id: str) -> bool:
    """Same ownership check as stage 3a's buttons, applied to a plain text message.

    Must be: the owner's id, in the owner's own private chat, not a bot, a plain text
    message (no photo/document/sticker), and not forwarded from anyone or anywhere.
    """
    try:
        msg = update["message"]
        if any(k in msg for k in ("forward_origin", "forward_from", "forward_from_chat")):
            return False
        sender, chat = msg["from"], msg["chat"]
        return (
            bool(allowed_chat_id)
            and chat.get("type") == "private"
            and not sender.get("is_bot", False)
            and str(sender["id"]) == str(allowed_chat_id)
            and str(chat["id"]) == str(allowed_chat_id)
            and isinstance(msg.get("text"), str)
        )
    except (KeyError, TypeError, AttributeError):
        return False


def is_stale(update: dict, now: Optional[float] = None) -> bool:
    """True if the message was sent more than STALE_SECONDS ago (aide/the bot was
    down and Telegram queued it) -- executed late, a batch of old commands could look
    like a burst and confuse whoever receives the replies."""
    try:
        ts = float(update["message"]["date"])
    except (KeyError, TypeError, ValueError):
        return True
    return (now if now is not None else time.time()) - ts > STALE_SECONDS


def render_today(cfg: Config, now: datetime, *, fetch_weather=weather.today, fetch_events=gcal.today_events) -> str:
    lines = [f"■ 오늘 ({now:%m/%d}, {WEEKDAYS[now.weekday()]})"]
    if cfg.weather_enabled:
        try:
            lines.append(f"날씨: {fetch_weather(cfg.latitude, cfg.longitude).line()}")
        except netutil.NetError as e:
            log.warning("날씨 조회 실패: %s", e)
            lines.append("날씨를 불러오지 못했어요.")
    if cfg.calendar_enabled:
        events, failed, total = accounts.gather_events(
            cfg, lambda token: fetch_events(token, now), lambda e: (e.when != "종일", e.when))
        lines.append("일정:")
        if events:
            lines += [f"• {e.when} {e.title}" for e in events]
        elif not failed:
            lines.append("없어요.")
        lines += accounts.failure_lines(failed, total)
    if len(lines) == 1:
        lines.append("날씨·일정이 꺼져 있어요.")
    return "\n".join(lines)


def render_week(cfg: Config, now: datetime, *, fetch_week=gcal.events_between) -> str:
    if not cfg.calendar_enabled:
        return "일정 기능이 꺼져 있어요."
    first = now.date()
    events, failed, total = accounts.gather_events(
        cfg, lambda token: fetch_week(token, first, 7), lambda e: (e.day or first, e.when != "종일", e.when))
    lines = [f"■ 일정 ({first:%m/%d}~{(first + timedelta(days=6)):%m/%d})"]
    if not events and not failed:
        lines.append("없어요.")
    last = None
    for e in events:
        if e.day != last:
            last = e.day
            lines.append(f"{e.day:%m/%d}({WEEKDAYS[e.day.weekday()]})" if e.day else "")
        lines.append(f"  • {e.when} {e.title}")
    lines += accounts.failure_lines(failed, total)
    return "\n".join(lines)


def render_due(cfg: Config, now: datetime, *, fetch=news_keywords.fetch_feed) -> str:
    if not cfg.notice_pages:
        return "마감 감시가 꺼져 있어요."
    rows = notice_pages.upcoming(cfg.notice_pages, cfg.notice_keywords, fetch, now, 7, cfg.notice_exclude)
    lines = ["■ 마감 임박 (7일 이내)"]
    lines += [f"• D-{left} {title} (~{due:%m/%d})" for title, due, left, _ in rows] or ["없어요."]
    return "\n".join(lines)


def render_mail(cfg: Config, now: datetime) -> str:
    if not cfg.mail_enabled:
        return "메일 알림이 꺼져 있어요."
    accts = cfg.accounts()
    many = len(accts) > 1
    mail: List[Finding] = []
    failed: List[str] = []
    for idx, acct in enumerate(accts):
        tag = f" ({acct.name})" if many else ""
        try:
            token = google_auth.access_token(cfg.resolved_google_client(), acct.token_path,
                                              need_scope=google_auth.GMAIL_SCOPE)
            found = gmail.recent_unread(token, cfg.mail_recent_max, cfg.mail_recent_window_hours, cfg.mail_block, now)
        except (google_auth.GoogleAuthError, netutil.NetError, gmail.MailConfigError) as e:
            log.warning("메일 조회 실패%s: %s", tag, e)
            failed.append(acct.name or "계정")
            continue
        mail += accounts.tag_mail(found, acct, idx, many)
    lines = ["■ 메일 (최근 안 읽음)"]
    if mail:
        lines += [f"• {f.title}" + (f"\n  {f.detail}" if f.detail else "") for f in mail]
    elif not failed:
        lines.append("없어요.")
    lines += accounts.failure_lines(failed, len(accts))
    return "\n".join(lines)


def render_help(cfg: Config, now: datetime) -> str:
    enabled = [n for n in COMMAND_NAMES if n in cfg.commands]
    if not enabled:
        return "사용 가능한 명령이 없어요."
    return "사용 가능한 명령:\n" + "\n".join(f"/{n}" for n in enabled)


REGISTRY: Dict[str, Callable[[Config, datetime], str]] = {
    "오늘": render_today,
    "일정": render_week,
    "마감": render_due,
    "메일": render_mail,
    HELP_NAME: render_help,
}


def run(name: str, cfg: Config, now: datetime) -> str:
    """Render one command's reply. Never raises: a handler's own network/parsing
    problems are caught inside it (same policy as heartbeat's checks) -- a broken
    data source answers "불러오지 못했어요", it never leaves the command silent or crashes
    the poll loop."""
    try:
        return REGISTRY[name](cfg, now)
    except Exception:  # noqa: BLE001 -- last-resort: a reply must always go out
        log.exception("명령 처리 중 오류: %s", name)
        return "처리 중 오류가 있었어요."
