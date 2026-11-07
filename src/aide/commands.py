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
import os
import time
import unicodedata
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional

from . import accounts, gcal, gmail, google_auth, gtasks, health, netutil, weather
from .checks import news_keywords, notice_pages
from .config import COMMAND_NAMES, Config
from .models import Finding, clean

log = logging.getLogger("aide.commands")

WEEKDAYS = "월화수목금토일"
MAX_TEXT_LEN = 32        # a command is a short word, never a sentence
STALE_SECONDS = 600      # queued more than this long ago (bot/aide was down) -> skip, don't burst-execute
MAX_PER_BATCH = 3        # commands executed from a single getUpdates() result
MAX_PER_HOUR = 20        # across all batches; see State.commands_in_last_hour

HELP_NAME = "도움"  # always available, never gated by config.commands
CONNECTIONS_NAME = "연결"  # read-only status of what is connected; always available like 도움
ALWAYS_ON = (HELP_NAME, CONNECTIONS_NAME)

ALIASES = {
    "오늘": "오늘", "today": "오늘",
    "일정": "일정", "week": "일정",
    "마감": "마감", "due": "마감",
    "메일": "메일", "mail": "메일",
    "날씨": "날씨", "weather": "날씨",
    "할일": "할일", "todo": "할일", "tasks": "할일",
    "도움": HELP_NAME, "help": HELP_NAME, "start": HELP_NAME,
    "연결": CONNECTIONS_NAME, "connections": CONNECTIONS_NAME,
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

    `도움` and `연결` resolve even when `enabled` is empty, so the owner can always ask what is on.
    """
    name = ALIASES.get(normalize(text))
    if name is None or (name not in ALWAYS_ON and name not in enabled):
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


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def _match(keyword: str, text: str) -> bool:
    """Local substring filter used by chat questions ("치과 언제야?"): the keyword never
    goes to Google, it only narrows what was already fetched."""
    return not keyword or _fold(keyword) in _fold(text)


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
            cfg, lambda token: fetch_events(token, now), gcal.order)
        lines.append("일정:")
        if events:
            lines += [f"• {t}" for t in gcal.lines(events)]
        elif not failed:
            lines.append("없어요.")
        lines += accounts.failure_lines(failed, total)
    if len(lines) == 1:
        lines.append("날씨·일정이 꺼져 있어요.")
    return "\n".join(lines)


def render_week(cfg: Config, now: datetime, *, fetch_week=gcal.events_between,
                first: Optional[date] = None, days: int = 7, only: str = "", keyword: str = "") -> str:
    if not cfg.calendar_enabled:
        return "일정 기능이 꺼져 있어요."
    first = first or now.date()
    events, failed, total = accounts.gather_events(
        cfg, lambda token: [e for e in fetch_week(token, first, days) if _match(keyword, e.title)],
        lambda e: (e.day or first,) + gcal.order(e), only)
    last_day = first + timedelta(days=days - 1)
    lines = [f"■ 일정 ({first:%m/%d})" if days == 1 else f"■ 일정 ({first:%m/%d}~{last_day:%m/%d})"]
    if not events and not failed:
        lines.append("없어요.")
    last = None
    for e, text in zip(events, gcal.lines(events)):
        if e.day != last:
            last = e.day
            lines.append(f"{e.day:%m/%d}({WEEKDAYS[e.day.weekday()]})" if e.day else "")
        lines.append(f"  • {text}")
    lines += accounts.failure_lines(failed, total)
    return "\n".join(lines)


def render_due(cfg: Config, now: datetime, *, fetch=news_keywords.fetch_feed, days: int = 7, keyword: str = "") -> str:
    if not cfg.notice_pages:
        return "마감 감시가 꺼져 있어요."
    rows = notice_pages.upcoming(cfg.notice_pages, cfg.notice_keywords, fetch, now, days, cfg.notice_exclude)
    rows = [r for r in rows if _match(keyword, r[0])]
    lines = [f"■ 마감 임박 ({days}일 이내)"]
    lines += [f"• D-{left} {title} (~{due:%m/%d})" for title, due, left, _ in rows] or ["없어요."]
    return "\n".join(lines)


KEYWORD_MAIL_MAX = 20   # a keyword search looks at the newest this-many unread mails, no further


def render_mail(cfg: Config, now: datetime, *, days: Optional[int] = None, only: str = "", keyword: str = "") -> str:
    if not cfg.mail_enabled:
        return "메일 알림이 꺼져 있어요."
    accts = cfg.accounts()
    many = len(accts) > 1
    hours = days * 24 if days else cfg.mail_recent_window_hours
    limit = KEYWORD_MAIL_MAX if keyword else cfg.mail_recent_max
    mail: List[Finding] = []
    failed: List[str] = []
    for idx, acct in enumerate(accts):
        if only and acct.name != only:
            continue
        tag = f" ({acct.name})" if many else ""
        try:
            token = google_auth.access_token(cfg.resolved_google_client(), acct.token_path,
                                              need_scope=google_auth.GMAIL_SCOPE)
            found = gmail.recent_unread(token, limit, hours, cfg.mail_block, now)
        except (google_auth.GoogleAuthError, netutil.NetError, gmail.MailConfigError) as e:
            log.warning("메일 조회 실패%s: %s", tag, e)
            failed.append(acct.name or "계정")
            continue
        if keyword:   # filter on the sender/subject as fetched: the "[계정]" label added below must not match,
            # and the "…이 외에 N건 더" note would be wrong for a filtered list
            found = [replace(f, detail="") for f in found if _match(keyword, f.title)]
        mail += accounts.tag_mail(found, acct, idx, many)
    lines = ["■ 메일 (최근 안 읽음)"]
    if mail:
        lines += [f"• {f.title}" + (f"\n  {f.detail}" if f.detail else "") for f in mail]
    elif not failed:
        lines.append("없어요.")
    if keyword:
        lines.append(f"(최근 안 읽은 메일 최신 {limit}건 안에서만 찾았어요)")
    lines += accounts.failure_lines(failed, len(accts) if not only else 1)
    return "\n".join(lines)


def render_weather(cfg: Config, now: datetime, *, fetch=weather.on_day, offset: int = 0) -> str:
    if not cfg.weather_enabled:
        return "날씨 기능이 꺼져 있어요."
    day = now.date() + timedelta(days=offset)
    head = f"■ 날씨 ({day:%m/%d}, {WEEKDAYS[day.weekday()]})"
    try:
        return f"{head}\n{fetch(cfg.latitude, cfg.longitude, offset).line()}"
    except netutil.NetError as e:
        log.warning("날씨 조회 실패: %s", e)
        return f"{head}\n불러오지 못했어요."


CHAT_EXAMPLES = {"일정": "다음 주 화요일 일정 뭐야?", "날씨": "내일 날씨", "메일": "메일 3일치",
                 "마감": "이번 주 마감 공지", "오늘": "오늘 뭐 있어?", "할일": "이번 주 할 일"}


def render_tasks(cfg: Config, now: datetime, *, fetch=gtasks.fetch, days: int = 7, only: str = "",
                 keyword: str = "") -> str:
    """Google Tasks: what is overdue or due within `days`, plus how many have no date (or, with a
    keyword, the undated ones whose title matches)."""
    if not cfg.task_accounts():
        return "할 일 기능이 꺼져 있어요."
    tasks, failed, total = gtasks.gather(cfg, fetch, only)
    today = now.date()
    if keyword:
        tasks = [t for t in tasks if _match(keyword, t.title)]
    dated, undated = gtasks.split(tasks, today, days)
    lines = [f"■ 할 일 (지난 것 + {days}일 이내)"]
    lines += [gtasks.task_line(t, today) for t in dated[:gtasks.MAX_SHOWN]]
    if len(dated) > gtasks.MAX_SHOWN:
        lines.append(f"…외 {len(dated) - gtasks.MAX_SHOWN}개")
    if undated and keyword:
        lines += [gtasks.task_line(t, today) for t in undated[:gtasks.MAX_SHOWN]]
    elif undated:
        lines.append(gtasks.task_line(undated[0], today))
        if len(undated) > 1:
            lines.append(f"…날짜 없는 할 일 외 {len(undated) - 1}개")
    if not dated and not undated and not failed:
        lines.append("없어요.")
    lines += gtasks.failure_line(failed, total)
    return "\n".join(lines)


def chat_examples(cfg: Config) -> str:
    """"예: ..." built only from the commands that are switched on (a switched-off one is never advertised)."""
    ex = [CHAT_EXAMPLES[n] for n in COMMAND_NAMES if n in cfg.commands and n in CHAT_EXAMPLES][:3]
    return "예: " + " / ".join(ex) if ex else ""


def render_help(cfg: Config, now: datetime) -> str:
    enabled = [n for n in COMMAND_NAMES if n in cfg.commands]
    if not enabled:
        return "사용 가능한 명령이 없어요."
    lines = ["사용 가능한 명령:"] + [f"/{n}" for n in enabled] + [f"/{CONNECTIONS_NAME}"]
    lines.append(f"('/' 없이 '{enabled[0]}'처럼 한 단어만 써도 돼요)")
    if cfg.chat_enabled:
        lines += ["", "문장으로 물어봐도 돼요 (조회만 해요)."]
        ex = chat_examples(cfg)
        if ex:
            lines.append(ex)
        lines.append("※ 쓰신 문장은 AI(Anthropic)로 전송돼요. 비밀번호·개인정보는 쓰지 마세요.")
    if cfg.memory_enabled:
        lines += ["", "/기억 <글> 로 기억해 두고, /기억 으로 목록을 봐요 (지울 수 있어요).",
                  "※ 적은 내용은 AI(Anthropic)로 전송돼요."]
    return "\n".join(lines)


GOOGLE_PERMS = (("일정", google_auth.CAL_SCOPE), ("메일", google_auth.GMAIL_SCOPE), ("할 일", google_auth.TASKS_READ_SCOPES),
              ("할 일 쓰기", google_auth.TASKS_WRITE_SCOPE))


def _google_line(cfg: Config) -> str:
    parts = []
    for a in cfg.accounts():
        name = clean(a.name, 20) or "기본"
        if not a.token_path.exists():
            parts.append(f"{name}(로그인 안 됨)")
            continue
        perms = [label for label, scope in GOOGLE_PERMS if google_auth.token_has_scope(a.token_path, scope)]
        parts.append(f"{name}({'·'.join(perms)})" if perms else f"{name}(권한 없음)")
    return "구글: " + " · ".join(parts)


def _ai_line(cfg: Config, environ) -> str:
    key = "키 있음" if (environ.get("ANTHROPIC_API_KEY") or "").strip() else "키 없음"
    uses = []
    if cfg.llm_enabled:
        uses.append("중요도 판단")
        if cfg.llm_highlights:
            uses.append("오늘의 핵심")
    if cfg.chat_enabled:
        uses.append("문장으로 묻기")
    if cfg.memory_enabled:
        uses.append("기억")
    return f"AI(Anthropic): {key} · 쓰는 곳: {', '.join(uses) if uses else '없음'}"


def _github_line(cfg: Config, get, environ) -> str:
    if not cfg.github_accounts:
        return "GitHub: 꺼짐"
    parts = []
    for account in cfg.github_accounts:
        status, exp = health.github_expiry(account, get=get, environ=environ)
        if status == "no-token":
            parts.append(f"{account}(토큰 없음)")
        elif status == "error":
            parts.append(f"{account}(토큰 있음, 확인 못 함)")
        elif status == "date" and exp is not None:
            parts.append(f"{account}(토큰 있음, 만료 {exp.astimezone(health.KST):%Y-%m-%d})")
        else:
            parts.append(f"{account}(토큰 있음)")
    return "GitHub: " + " · ".join(parts)


def _news_line(cfg: Config) -> str:
    hours = "·".join(str(h) for h in cfg.news_hours) + "시" if cfg.news_hours else "매 점검"
    market = f"검색어 {len(cfg.market_queries)}개" if cfg.market_queries else "꺼짐"
    return f"뉴스: 피드 {len(cfg.news_feeds)}개 · {hours} / 장중 특징주: {market}"


def render_connections(cfg: Config, now: datetime, *, get=netutil.get_json_with_headers, environ=None) -> str:
    """What aide is connected to, read only: Google logins (by the permissions saved in each token file),
    whether the AI key is there and what it is used for, GitHub tokens (and their expiry), news and weather
    switches. No secrets, e-mail addresses or file paths are shown."""
    env = os.environ if environ is None else environ
    lines = ["■ 연결 상태"]
    for build in (lambda: _google_line(cfg), lambda: _ai_line(cfg, env), lambda: _github_line(cfg, get, env),
                  lambda: _news_line(cfg),
                  lambda: f"날씨: {'켜짐' if cfg.weather_enabled else '꺼짐'} / 날씨 판단: "
                          f"{'켜짐' if cfg.weather_alerts else '꺼짐'}"):
        try:
            lines.append(build())
        except Exception:  # noqa: BLE001 -- one broken line must not hide the others
            log.exception("연결 상태 한 줄 조립 중 오류")
            lines.append("(이 줄은 확인하지 못했어요)")
    return "\n".join(lines)


REGISTRY: Dict[str, Callable[[Config, datetime], str]] = {
    "오늘": render_today,
    "일정": render_week,
    "마감": render_due,
    "메일": render_mail,
    "날씨": render_weather,
    "할일": render_tasks,
    HELP_NAME: render_help,
    CONNECTIONS_NAME: render_connections,
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
