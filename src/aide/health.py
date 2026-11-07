"""Failure alerts (config `health_alerts`, default off).

1. `section()`: for the morning greeting, `■ aide 점검 (어제)` -- counts of yesterday's ERROR/WARNING lines
   in aide's own log file (per logger, by a fixed Korean label; log TEXT is never shown or sent, it may hold
   outside text or secrets), and GitHub tokens that expire within 14 days.
2. `check_auth()`: from the heartbeat (only with --send, outside quiet hours), a Telegram alert, at most once
   per day per account, when a Google login can no longer be refreshed.
3. `github_expiry()`: one `GET /user` per token, shared with the `/연결` command.

Nothing here may raise into a message or the heartbeat; token values never reach a log line or a message.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from . import github_brief, google_auth, netutil, telegram
from .models import clean
from .state import State, StateLocked, locked

log = logging.getLogger("aide.health")

LOG_NAME = "aide.log"
MAX_LOG_BYTES = 2_000_000          # only the end of the file is read
WARN_THRESHOLD = 10                # this many warnings in a day is worth a line even without errors
TOKEN_WARN_DAYS = 14
EXPIRY_HEADER = "github-authentication-token-expiration"
USER_URL = "https://api.github.com/user"
KST = timezone(timedelta(hours=9))

# Fixed Korean labels; any logger not listed is "기타". Log text is never used.
LOGGER_LABELS = {
    "heartbeat": "점검", "morning": "아침 인사", "evening": "저녁 정리", "poll": "텔레그램 수신",
    "inbox": "텔레그램 수신", "commands": "명령", "chat": "AI", "llm": "AI", "memory": "기억",
    "news": "뉴스 피드", "market": "뉴스 피드", "notice": "공지", "gcal": "일정", "accounts": "일정",
    "gmail": "메일", "gtasks": "할 일", "github": "GitHub", "weather": "날씨", "health": "점검",
    "rules": "규칙", "cleanup": "규칙", "state": "상태 파일", "git": "작업 폴더", "yesterday": "작업 폴더",
    "nudges": "먼저 챙기기",
}
MAX_LABELS_SHOWN = 5

# 2026-11-06 21:07:01,123 WARNING aide.news: text   (basicConfig format of __main__.py)
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2},\d{3} (ERROR|CRITICAL|WARNING) (\S+): (.*)$")


def log_path(cfg) -> Path:
    """data/aide.log, next to the state file (the .bat/.sh runners append there)."""
    return Path(cfg.resolved_state_path()).parent / LOG_NAME


def _read_tail(path: Path, limit: int = MAX_LOG_BYTES) -> Optional[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            start = max(0, size - limit)
            f.seek(start)
            raw = f.read(limit)
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if start > 0:
        text = text.split("\n", 1)[1] if "\n" in text else ""   # the first line is cut in the middle
    return text


def _label(logger: str) -> str:
    return LOGGER_LABELS.get(logger[5:], "기타") if logger.startswith("aide.") else "기타"


def summarize_log(text: str, day: date) -> Optional[Tuple[int, int, Dict[str, int], bool]]:
    """(errors, warnings, count per label, has a 만료/권한 warning) for the lines of `day`; None if no
    such line at all."""
    prefix = day.isoformat()
    errors = warnings = 0
    per: Dict[str, int] = {}
    urgent = False
    for raw in text.splitlines():
        m = _LINE.match(raw)
        if not m or m.group(1) != prefix:
            continue
        level, name, body = m.group(2), m.group(3), m.group(4)
        if level == "WARNING":
            warnings += 1
            if "만료" in body or "권한" in body:
                urgent = True
        else:
            errors += 1
        lab = _label(name)
        per[lab] = per.get(lab, 0) + 1
    if not errors and not warnings:
        return None
    return errors, warnings, per, urgent


def log_lines(cfg, now: datetime, *, text: Optional[str] = None) -> List[str]:
    """One summary line for yesterday, only when it is worth showing; [] otherwise (also: no/unreadable file)."""
    if text is None:
        text = _read_tail(log_path(cfg))
    if text is None:
        return []
    res = summarize_log(text, (now - timedelta(days=1)).date())
    if res is None:
        return []
    errors, warnings, per, urgent = res
    if not (errors >= 1 or urgent or warnings >= WARN_THRESHOLD):
        return []
    ranked = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = " · ".join(f"{k} {n}" for k, n in ranked[:MAX_LABELS_SHOWN])
    if len(ranked) > MAX_LABELS_SHOWN:
        shown += " · 그 밖"
    return [f"오류 {errors} · 경고 {warnings} ({shown})"]


# ---- GitHub token expiry ------------------------------------------------------------------------

def parse_expiry(value: str) -> Optional[datetime]:
    """`2027-11-07 10:00:00 UTC` -> aware datetime; None if it is not in that shape."""
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC", (value or "").strip())
    if not m:
        return None
    try:
        return datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}+00:00")
    except ValueError:
        return None


def github_expiry(account: str, *, get: Callable = netutil.get_json_with_headers,
                  environ: Optional[Mapping[str, str]] = None) -> Tuple[str, Optional[datetime]]:
    """(status, expiry): "no-token", "error" (could not ask / bad answer), "unlimited" (no expiry header)
    or "date" with the expiry (aware UTC). One `GET /user`; the token only goes in the Authorization header."""
    env = os.environ if environ is None else environ
    token = (env.get(github_brief.token_env_name(account)) or "").strip()
    if not token:
        return "no-token", None
    try:
        _, headers = get(USER_URL, headers=github_brief._headers(token))
    except netutil.NetError as e:
        log.warning("GitHub 토큰 만료일 확인 실패 (%s): %s", account, e)
        return "error", None
    except Exception:  # noqa: BLE001
        log.warning("GitHub 토큰 만료일 확인 중 오류 (%s)", account)
        return "error", None
    raw = headers.get(EXPIRY_HEADER) if isinstance(headers, dict) else None
    if not raw:
        return "unlimited", None
    exp = parse_expiry(raw)
    return ("date", exp) if exp is not None else ("unlimited", None)


def days_left(exp: datetime, now: datetime) -> int:
    """Whole calendar days (KST) from `now` (naive local time) to the expiry."""
    return (exp.astimezone(KST).date() - now.date()).days


def token_lines(cfg, now: datetime, *, get: Callable = netutil.get_json_with_headers,
                environ: Optional[Mapping[str, str]] = None) -> List[str]:
    out: List[str] = []
    for account in list(cfg.github_accounts or []):
        status, exp = github_expiry(account, get=get, environ=environ)
        if status == "date" and exp is not None:
            n = days_left(exp, now)
            if 0 <= n <= TOKEN_WARN_DAYS:
                out.append(f"GitHub 토큰({account}) 만료 D-{n}")
    return out


def section(cfg, now: datetime, *, text: Optional[str] = None, get: Callable = netutil.get_json_with_headers,
            environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """`■ aide 점검 (어제)` lines (ending with a blank line), or []. Never raises."""
    if not getattr(cfg, "health_alerts", False):
        return []
    lines: List[str] = []
    try:
        lines += log_lines(cfg, now, text=text)
    except Exception:  # noqa: BLE001
        log.exception("로그 점검 중 오류")
    try:
        lines += token_lines(cfg, now, get=get, environ=environ)
    except Exception:  # noqa: BLE001
        log.exception("토큰 만료 점검 중 오류")
    return ["■ aide 점검 (어제)", *lines, ""] if lines else []


# ---- Google login failure alert -----------------------------------------------------------------

def alert_key(day: date, name: str) -> str:
    return f"authalert:{day.isoformat()}:{name}"


def alert_text(name: str) -> str:
    shown = clean(name, 20) or "기본"
    cmd = f"python -m aide google-login --account {shown}" if name else "python -m aide google-login"
    return f"⚠ 구글 로그인 확인 필요: {shown} — PC에서 {cmd} 후 토큰을 서버로 복사"


def check_auth(cfg, now: datetime, *, token: str, chat_id: str, sender: Callable[..., None] = telegram.send,
               access: Callable = google_auth.access_token) -> int:
    """Try to get a calendar access token for every Google account; a GoogleAuthError (login expired or
    revoked) is told to the owner once per day per account. A network failure is not an alert. The day is
    recorded only after the message was sent; the state lock is held for that write only. Returns the
    number of alerts sent. Never raises."""
    if not getattr(cfg, "health_alerts", False) or not (cfg.calendar_enabled or cfg.mail_enabled):
        return 0
    sent = 0
    path = cfg.resolved_state_path()
    try:
        for acct in cfg.accounts():
            key = alert_key(now.date(), acct.name)
            if State(path).is_seen(key):
                continue
            try:
                access(cfg.resolved_google_client(), acct.token_path, need_scope=google_auth.CAL_SCOPE)
                continue
            except google_auth.GoogleAuthError:
                pass
            except netutil.NetError:
                continue
            except Exception:  # noqa: BLE001
                log.warning("구글 로그인 점검 중 오류 (%s)", acct.name or "기본")
                continue
            try:
                sender(alert_text(acct.name), token=token, chat_id=chat_id)
            except telegram.TelegramError as e:
                log.error("구글 로그인 알림 전송 실패: %s (다음 점검에서 재시도)", e)
                continue
            sent += 1
            try:
                with locked(path):
                    state = State(path)
                    state.mark(key, now)
                    state.save()
            except StateLocked as e:
                log.error("%s 로그인 알림 기록을 못 했습니다.", e)
    except Exception:  # noqa: BLE001
        log.exception("구글 로그인 점검 중 오류")
    return sent
