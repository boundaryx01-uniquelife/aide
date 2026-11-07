"""Mail -> Google Task: the owner presses buttons and ONE task is added. aide's first "write".

What this can do, and nothing else: create a task (POST .../lists/@default/tasks) with a title and an optional
due date and a Gmail link. It never edits or deletes a task, never reads the mail body, never calls an LLM, and
never adds anything the owner did not pick with two button presses on his own private chat.

Flow (ARCHITECTURE.md "메일 → 할 일"):
  t:<digest>:open              -> new message listing the digest's mails (max 8)
  t:<digest>:p<i>              -> that message becomes "할 일 추가: <제목>" with [오늘] [내일] [날짜 없음] [취소]
  t:<digest>:p<i>:(d0|d1|dn)   -> add it (due today / tomorrow / none)
  t:<digest>:close             -> dismiss

`handle` runs under the state lock and only decides (it edits/sends Telegram messages like cleanup.py does and
returns a `TaskAddRequest` for the final step). The Google call (`run`) is made by `inbox.poll_once` AFTER the
lock is released; on success the caller records `taskadd:<digest>:<i>` and bumps the day's count, so pressing
the same button twice never adds twice and a failed attempt can be retried.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional, Set, Tuple

from . import google_auth, netutil, telegram
from .config import Config
from .models import clean
from .state import State, StateLocked, locked

log = logging.getLogger("aide.taskadd")

MENU_RE = re.compile(r"^t:([0-9a-f]{8}):(open|close|p([0-7])(?::(d0|d1|dn))?)\Z")   # \Z, not $: no trailing newline either
DAILY_LIMIT = 20
TASKS_URL = "https://tasks.googleapis.com/tasks/v1/lists/@default/tasks"
DUE_LABELS = {"d0": "오늘", "d1": "내일", "dn": "날짜 없음"}

EXPIRED = "만료된 알림이에요"
EXPIRED_OFF = "만료된 버튼이에요"           # the feature is switched off
ALREADY = "이미 추가했어요"
BUSY = "추가하는 중이에요"
LIMIT_TEXT = f"오늘은 {DAILY_LIMIT}건까지만 추가할 수 있어요"
FAIL_TEXT = "추가하지 못했어요 (로그 확인)"


def is_task_data(data) -> bool:
    return isinstance(data, str) and bool(MENU_RE.match(data))


def done_key(digest_id: str, index: int) -> str:
    return f"taskadd:{digest_id}:{index}"


@dataclass(frozen=True)
class TaskAddRequest:
    """A verified "add this" press, handed from process_updates (lock held) to poll_once (lock released)."""
    digest_id: str
    index: int
    due: str            # "d0" | "d1" | "dn"
    title: str          # already clean()ed, max 100
    url: str            # "" or an https://mail.google.com/ link
    account: str        # the mail's Google account name ("" if single account)
    message_id: Optional[int] = None


@dataclass(frozen=True)
class Result:
    status: str         # "ok" | "noperm" | "fail"
    text: str = ""      # for the message edit (noperm / fail)


def _mail(state: State, digest_id: str, index: int) -> Optional[dict]:
    rec = state.digests.get(digest_id)
    mails = (rec or {}).get("mails") or []
    if index >= len(mails):
        return None
    return mails[index]


def _edit(api, token: str, chat_id: str, message_id, text: str, buttons=None) -> None:
    try:
        api.edit_text(token, chat_id, int(message_id), text, buttons)
    except (telegram.TelegramError, TypeError, ValueError) as e:
        log.info("메시지 수정 실패(기록은 반영됨): %s", e)


def _send(api, token: str, chat_id: str, text: str, buttons=None) -> None:
    try:
        api.send(text, token=token, chat_id=chat_id, buttons=buttons)
    except telegram.TelegramError as e:
        log.info("안내 전송 실패: %s", e)


def due_buttons(digest_id: str, index: int):
    base = f"t:{digest_id}:p{index}"
    return [[("오늘", f"{base}:d0"), ("내일", f"{base}:d1"), ("날짜 없음", f"{base}:dn")],
            [("취소", f"t:{digest_id}:close")]]


def handle(data: str, cq: dict, *, state: State, token: str, chat_id: str, api, now: datetime,
           pending: Set[Tuple[str, int]]) -> Tuple[str, Optional[TaskAddRequest]]:
    """Process one verified task button. Returns (toast, request to run after the lock or None).
    `pending` holds what this batch already queued, so a double press in one batch adds once and the
    daily limit counts the queued ones too. Nothing is written to Google here."""
    m = MENU_RE.match(data)
    if not m:
        return "알 수 없는 버튼이에요", None
    digest_id, what, idx, due = m.group(1), m.group(2), m.group(3), m.group(4)
    message_id = (cq.get("message") or {}).get("message_id")

    if what == "close":
        _edit(api, token, chat_id, message_id, "닫았어요")
        return "", None
    if what == "open":
        mails = (state.digests.get(digest_id) or {}).get("mails") or []
        if not mails:
            return EXPIRED, None
        lines = ["어느 메일을 할 일로 추가할까요?"]
        buttons = []
        for i, mail in enumerate(mails):
            title = clean(mail.get("t", ""), 100) or "(제목 없음)"
            added = state.is_seen(done_key(digest_id, i))
            lines.append(f"{i + 1}. {'✅ ' if added else ''}{title}")
            buttons.append([(f"{i + 1}. {'✅ ' if added else ''}{clean(title, 24)}", f"t:{digest_id}:p{i}")])
        buttons.append([("닫기", f"t:{digest_id}:close")])
        _send(api, token, chat_id, "\n".join(lines), buttons)
        return "", None

    index = int(idx)
    mail = _mail(state, digest_id, index)
    if mail is None:
        return EXPIRED, None
    title = clean(mail.get("t", ""), 100) or "(제목 없음)"
    if state.is_seen(done_key(digest_id, index)):
        if due:   # a second press on a stale keyboard: say so and tidy the message
            _edit(api, token, chat_id, message_id, f"이미 추가했어요: {title}")
        return ALREADY, None
    if due is None:
        _edit(api, token, chat_id, message_id, f"할 일 추가: {title}", due_buttons(digest_id, index))
        return "", None
    if (digest_id, index) in pending:
        return BUSY, None
    if state.task_adds_today(now) + len(pending) >= DAILY_LIMIT:
        log.info("할 일 추가 하루 한도 도달")
        return LIMIT_TEXT, None
    pending.add((digest_id, index))
    url = mail.get("u", "")
    req = TaskAddRequest(digest_id, index, due, title, url if isinstance(url, str) and url.startswith(telegram.URL_PREFIX) else "",
                         clean(mail.get("a", ""), 40), message_id if isinstance(message_id, int) else None)
    return BUSY, req


# ---- the Google call (runs with the state lock released) ----------------------------------------------

def due_text(due: str, today: date) -> Optional[str]:
    """Google keeps a date only; the time part is always midnight UTC. `today` is the server's local date (KST)."""
    if due == "d0":
        day = today
    elif due == "d1":
        day = today + timedelta(days=1)
    else:
        return None
    return f"{day.isoformat()}T00:00:00.000Z"


def payload(req: TaskAddRequest, today: date) -> dict:
    body = {"title": clean(req.title, 100) or "(제목 없음)"}
    if req.url.startswith(telegram.URL_PREFIX):
        body["notes"] = req.url
    d = due_text(req.due, today)
    if d:
        body["due"] = d
    return body


def choose_account(cfg: Config, name: str):
    """The mail's own account when it is a task account, else the first task account (None when none)."""
    accts = cfg.task_accounts()
    for a in accts:
        if name and a.name == name:
            return a
    return accts[0] if accts else None


def noperm_text(account_name: str) -> str:
    who = clean(account_name, 20)
    flag = f" --account {who}" if who else ""
    return f"할 일 쓰기 권한이 없어요 — PC에서 python -m aide google-login{flag} --tasks-write 후 토큰을 서버로 복사"


def run(req: TaskAddRequest, cfg: Config, now: datetime, *, post: Optional[Callable] = None,
        access: Optional[Callable] = None) -> Result:
    """Create the task. Never raises. Logs only the action, the result and the account name -- never the
    title, the link or a token."""
    post = post or netutil.post_json
    access = access or google_auth.access_token
    acct = choose_account(cfg, req.account)
    if acct is None:
        log.warning("할 일 추가 불가: 할 일 계정이 없음")
        return Result("fail", FAIL_TEXT)
    label = acct.name or "기본"
    if not google_auth.token_has_scope(acct.token_path, google_auth.TASKS_WRITE_SCOPE):
        log.warning("할 일 추가 불가 (%s): 쓰기 권한 없음", label)
        return Result("noperm", noperm_text(acct.name))
    log.info("할 일 추가 시도 (%s)", label)
    try:
        token = access(cfg.resolved_google_client(), acct.token_path, need_scope=google_auth.TASKS_WRITE_SCOPE)
        post(TASKS_URL, payload(req, now.date()), headers={"Authorization": f"Bearer {token}"})
    except google_auth.GoogleAuthError as e:
        log.warning("할 일 추가 실패 (%s): %s", label, e)
        return Result("fail", FAIL_TEXT)
    except netutil.NetError as e:
        log.warning("할 일 추가 실패 (%s): %s", label, e)   # "HTTP 401" / "연결 실패: …" -- no body, no URL
        return Result("fail", FAIL_TEXT)
    except Exception as e:  # noqa: BLE001
        log.warning("할 일 추가 중 오류 (%s): %s", label, type(e).__name__)
        return Result("fail", FAIL_TEXT)
    log.info("할 일 추가 완료 (%s)", label)
    return Result("ok")


def finish(req: TaskAddRequest, res: Result, *, state_path, now: datetime, api, token: str, chat_id: str) -> None:
    """After `run`: on success record it (state lock taken for that write only) and tell the owner;
    otherwise edit the message with the failure and leave nothing recorded."""
    if res.status == "ok":
        try:
            with locked(state_path):
                st = State(state_path)
                st.mark(done_key(req.digest_id, req.index), now)
                st.record_task_add(now)
                st.save()
        except StateLocked:
            log.warning("할 일 추가는 됐지만 기록을 못 했어요(상태 파일 사용 중) — 같은 버튼을 다시 누르면 중복될 수 있어요")
        if req.message_id is not None:
            _edit(api, token, chat_id, req.message_id,
                  f"✅ 할 일에 추가했어요: {req.title} ({DUE_LABELS.get(req.due, '')})")
        return
    if req.message_id is None:
        return
    if res.status == "noperm":
        _edit(api, token, chat_id, req.message_id, res.text)
    else:   # the same choices stay, so the owner can try again
        _edit(api, token, chat_id, req.message_id, f"{FAIL_TEXT}\n{req.title}", due_buttons(req.digest_id, req.index))
