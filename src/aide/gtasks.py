"""Google Tasks, READ-ONLY (scope tasks.readonly), only for the accounts in `tasks_accounts`.

Only the title, due date, status and parent of a task are requested -- never its notes or links.
Titles can be written by other people (shared lists), so they go through clean() and are shown as
plain text. Nothing here is stored in the state file and nothing is sent to the LLM.

A task's due date has no time of day (Google keeps a date only); it is read as the date part of
the timestamp, with no time-zone conversion.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote, urlencode

from . import google_auth, netutil
from .config import Config
from .models import clean

log = logging.getLogger("aide.gtasks")
BASE = "https://tasks.googleapis.com/tasks/v1"
LISTS_URL = BASE + "/users/@me/lists?" + urlencode({"maxResults": 10, "fields": "items(id,title)"})
MAX_LISTS = 10
MAX_PER_LIST = 100
MAX_SHOWN = 15
LIST_ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,100}")   # used with fullmatch
WEEKDAYS = "월화수목금토일"


@dataclass(frozen=True)
class Task:
    title: str
    due: Optional[date]
    list_title: str = ""
    account: str = ""     # which Google account it came from (filled by gather)


def _tasks_url(list_id: str) -> str:
    q = urlencode({"showCompleted": "false", "showHidden": "false", "maxResults": MAX_PER_LIST,
                   "fields": "items(title,due,status)"})
    return f"{BASE}/lists/{quote(list_id, safe='')}/tasks?{q}"


def _parse_due(value) -> Optional[date]:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse(item, list_title: str) -> Optional[Task]:
    if not isinstance(item, dict) or item.get("status") == "completed":
        return None
    return Task(clean(item.get("title", ""), 80) or "(제목 없음)", _parse_due(item.get("due")), list_title)


def fetch(token: str, get: Callable = netutil.get_json) -> List[Task]:
    """Open tasks of every list of the account. One broken list is skipped; every list failing
    re-raises so the caller still reports "could not load"."""
    headers = {"Authorization": f"Bearer {token}"}
    data = get(LISTS_URL, headers=headers)
    raw = data.get("items", []) if isinstance(data, dict) else []
    lists = [(i["id"], clean(i.get("title", ""), 30)) for i in raw
             if isinstance(i, dict) and isinstance(i.get("id"), str) and LIST_ID_RE.fullmatch(i["id"])][:MAX_LISTS]
    out: List[Task] = []
    ok, last_err = 0, None
    for list_id, list_title in lists:
        try:
            body = get(_tasks_url(list_id), headers=headers)
        except netutil.NetError as e:
            last_err = e
            continue
        ok += 1
        items = body.get("items", []) if isinstance(body, dict) else []
        out += [t for t in (_parse(i, list_title) for i in items) if t]
    if lists and not ok and last_err is not None:
        raise last_err
    return out


def gather(cfg: Config, fetch_fn: Callable = fetch, only: str = "") -> Tuple[List[Task], List[str], int]:
    """Tasks of every `tasks_accounts` account (or just `only`). Returns (tasks, failed account names,
    number of accounts asked). One account failing never hides the others. With several task accounts
    each title is labelled "[이름]"."""
    accts = cfg.task_accounts()
    many = len(accts) > 1
    if only:
        accts = [a for a in accts if a.name == only]
    out: List[Task] = []
    failed: List[str] = []
    for acct in accts:
        try:
            token = google_auth.access_token(cfg.resolved_google_client(), acct.token_path,
                                             need_scope=google_auth.TASKS_SCOPE)
            tasks = fetch_fn(token)
        except (google_auth.GoogleAuthError, netutil.NetError) as e:
            log.warning("할 일 조회 실패%s: %s", f" ({acct.name})" if many else "", e)
            failed.append(acct.name or "계정")
            continue
        out += [replace(t, account=acct.name) if many else t for t in tasks]
    return out, failed, len(accts)


def split(tasks: List[Task], today: date, days: int) -> Tuple[List[Task], List[Task]]:
    """(dated tasks due on or before today+days, oldest first, undated tasks)."""
    last = today + timedelta(days=days)
    dated = sorted((t for t in tasks if t.due is not None and t.due <= last), key=lambda t: (t.due, t.title))
    return dated, [t for t in tasks if t.due is None]


def when_text(due: date, today: date) -> str:
    if due < today:
        return f"지남 {due:%m/%d}"
    if due == today:
        return "오늘"
    if due == today + timedelta(days=1):
        return "내일"
    return f"{due:%m/%d}({WEEKDAYS[due.weekday()]})"


def task_line(t: Task, today: date) -> str:
    label = f"[{t.account}] " if t.account else ""
    return f"• {when_text(t.due, today)} {label}{t.title}" if t.due else f"• {label}{t.title}"


def failure_line(failed: List[str], total: int) -> List[str]:
    if not failed:
        return []
    who = "" if total == 1 else f": {', '.join(failed)}"
    return [f"할 일을 불러오지 못했어요{who} (로그 확인)"]


def lines(tasks: List[Task], failed: List[str], total: int, today: date, days: int, *,
          title: str, show_undated: bool) -> List[str]:
    """A message section ("■ title" + lines + blank line), or [] when there is nothing to say."""
    dated, undated = split(tasks, today, days)
    out: List[str] = [task_line(t, today) for t in dated[:MAX_SHOWN]]
    if len(dated) > MAX_SHOWN:
        out.append(f"…외 {len(dated) - MAX_SHOWN}개")
    if show_undated and undated:
        out.append(f"날짜 없는 할 일 {len(undated)}개")
    out += failure_line(failed, total)
    return [f"■ {title}", *out, ""] if out else []


def section(cfg: Config, now: datetime, *, kind: str, fetch_fn: Callable = fetch) -> List[str]:
    """The morning / evening / Sunday-evening section. [] when the feature is off or empty. Never raises."""
    if not cfg.task_accounts():
        return []
    spec = {"morning": ("할 일 (오늘까지)", 0, True),
            "evening": ("내일까지 할 일", 1, False),
            "weekly": ("다음 주까지 할 일", 7, False)}[kind]
    try:
        tasks, failed, total = gather(cfg, fetch_fn)
    except Exception:  # noqa: BLE001 -- a message must always go out
        log.exception("할 일 구역 처리 중 오류")
        return []
    return lines(tasks, failed, total, now.date(), spec[1], title=spec[0], show_undated=spec[2])
