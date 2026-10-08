"""Receiving side: handle button presses on digests, and (stage 3b) typed commands.

Stage 3a is READ-ONLY on purpose. A button press can only change aide's own
bookkeeping (acknowledged / ignored / remind me later). It never runs a command,
touches files, or sends anything beyond a short confirmation.

Stage 3b adds a fixed set of read-only commands (see commands.py), off by default.
Telegram is asked for plain chat messages at all ONLY when `cfg.commands` is
non-empty; with it empty, behaviour is byte-for-byte what stage 3a already was.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

from . import commands, telegram
from .config import Config
from .state import State, locked

log = logging.getLogger("aide.inbox")

CALLBACK_RE = re.compile(r"^d:([0-9a-f]{8}):(ok|skip|later)$")
REPLIES = {
    "ok": "확인했어요",
    "skip": "무시할게요",
    "later": "다음 점검 때 다시 알려드릴게요",
}


WATCH_FRESH = 150.0  # seconds; the watch loop touches its lock at least every ~85 s (25 s wait + 60 s backoff)


def _watch_lock(cfg: Config):
    return cfg.resolved_state_path().with_name("poll_watch.lock")


def watcher_active(cfg: Config) -> bool:
    """True while a `poll --watch` loop is alive (its lock file was touched recently).

    Telegram allows only one getUpdates reader at a time (a second one makes the first
    fail with 409), so the scheduled one-shot poll and any second watcher stand down.
    """
    try:
        return time.time() - _watch_lock(cfg).stat().st_mtime < WATCH_FRESH
    except OSError:
        return False


def touch_watcher(cfg: Config) -> None:
    lock = _watch_lock(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")


def release_watcher(cfg: Config) -> None:
    try:
        _watch_lock(cfg).unlink()
    except OSError:
        pass


def buttons_for(digest_id: str):
    return [[("✅ 확인", f"d:{digest_id}:ok"), ("🔕 무시", f"d:{digest_id}:skip"), ("⏰ 나중에", f"d:{digest_id}:later")]]


def is_allowed(update: dict, allowed_chat_id: str) -> bool:
    """Only the owner's own private chat may press buttons. Everyone else is ignored
    silently (no reply, so the bot does not reveal that it exists or how it works)."""
    try:
        cq = update["callback_query"]
        sender, chat = cq["from"], cq["message"]["chat"]
        return (
            bool(allowed_chat_id)
            and not sender.get("is_bot", False)
            and str(sender["id"]) == str(allowed_chat_id)
            and str(chat["id"]) == str(allowed_chat_id)
        )
    except (KeyError, TypeError, AttributeError):
        return False


def _describe(u, allowed_chat_id: str) -> str:
    """Why an update was ignored, without logging any message content."""
    if not isinstance(u, dict):
        return "형식 불명"
    cq = u.get("callback_query")
    if not isinstance(cq, dict):
        return "버튼 입력이 아님"
    try:
        who = "본인" if str(cq["from"]["id"]) == str(allowed_chat_id) else "다른 사람"
        where = "본인 채팅" if str(cq["message"]["chat"]["id"]) == str(allowed_chat_id) else "다른 채팅"
    except (KeyError, TypeError):
        return "버튼 입력이지만 보낸 사람/채팅 정보가 없음 (너무 오래된 메시지일 수 있음)"
    return f"{who} / {where}" + (" / 봇" if cq.get("from", {}).get("is_bot") else "")


def _describe_message(u, allowed_chat_id: str) -> str:
    """Why a text message was ignored or not run, without logging its content."""
    msg = u.get("message") if isinstance(u, dict) else None
    if not isinstance(msg, dict):
        return "메시지가 아님"
    try:
        who = "본인" if str(msg["from"]["id"]) == str(allowed_chat_id) else "다른 사람"
        where = "본인 채팅" if str(msg["chat"]["id"]) == str(allowed_chat_id) else "다른 채팅"
    except (KeyError, TypeError):
        return "메시지이지만 보낸 사람/채팅 정보가 없음"
    extra = ""
    if msg.get("from", {}).get("is_bot"):
        extra += " / 봇"
    if any(k in msg for k in ("forward_origin", "forward_from", "forward_from_chat")):
        extra += " / 전달됨"
    if not isinstance(msg.get("text"), str):
        extra += " / 텍스트 아님"
    return f"{who} / {where}" + extra


def _reply(api, token: str, chat_id: str, text: str) -> None:
    """Best-effort plain-text reply (notices, "모르는 명령이에요", ...). Never raises."""
    try:
        api.send(text, token=token, chat_id=chat_id)
    except telegram.TelegramError as e:
        log.info("안내 전송 실패: %s", e)


def process_updates(
    updates: list,
    state: State,
    *,
    token: str,
    allowed_chat_id: str,
    enabled_commands: Iterable[str] = (),
    now: Optional[datetime] = None,
    api=telegram,
) -> Tuple[int, List[str]]:
    """Apply button presses to `state`, and decide which stage-3b commands may run.

    Returns (how many updates were acted on, canonical command names to execute
    AFTER the caller releases the state lock -- command handlers call Google /
    notice-page APIs, which must never happen while the lock is held).

    The offset advances past EVERY update (allowed or not) so nothing is read twice.
    Button presses are idempotent, so redelivery after a crash is harmless. Commands
    are "at most once": the batch/hour counters are consumed here, under the lock,
    before the command actually runs, so a crash mid-reply never re-runs it.
    """
    now = now or datetime.now()
    enabled_commands = list(enabled_commands)
    handled = ignored = 0
    to_run: List[str] = []
    batch_count = 0
    stale_skipped = limited = False
    for u in updates:
        uid = u.get("update_id") if isinstance(u, dict) else None
        if isinstance(uid, int):
            state.offset = max(state.offset, uid + 1)
        if not isinstance(u, dict):
            ignored += 1
            continue

        if "callback_query" in u:
            if not is_allowed(u, allowed_chat_id):
                ignored += 1
                log.info("무시한 업데이트: %s", _describe(u, allowed_chat_id))
                continue
            cq = u["callback_query"]
            m = CALLBACK_RE.match(cq.get("data") if isinstance(cq.get("data"), str) else "")
            outcome = None
            if not m:
                reply = "알 수 없는 버튼이에요"
            else:
                outcome = state.resolve_digest(m.group(1), m.group(2))
                reply = {None: "만료된 알림이에요", "dup": "이미 처리됐어요"}.get(outcome, REPLIES.get(outcome or "", ""))
                if outcome in REPLIES:
                    handled += 1
            # Two independent best-effort steps: a late press makes answerCallbackQuery fail
            # (Telegram only accepts it for a short time), but the buttons should still go away.
            try:
                api.answer_callback(token, str(cq.get("id", "")), reply)
            except telegram.TelegramError as e:
                log.info("토스트 표시 실패(오래된 버튼이면 정상, 기록은 반영됨): %s", e)
            if m and (outcome in REPLIES or outcome == "dup"):  # also on a repeat press: tidy up a stuck keyboard
                try:
                    api.clear_buttons(token, str(allowed_chat_id), int(cq["message"]["message_id"]))
                except (telegram.TelegramError, KeyError, TypeError, ValueError) as e:
                    log.info("버튼 제거 실패(기록은 반영됨): %s", e)
            continue

        if "message" in u:
            if not commands.is_command_message(u, allowed_chat_id):
                ignored += 1
                log.info("무시한 메시지: %s", _describe_message(u, allowed_chat_id))
                continue
            name = commands.resolve(u["message"]["text"], enabled_commands)
            if name is None:
                log.info("알 수 없는 명령 입력")  # never log the text itself
                _reply(api, token, allowed_chat_id, "모르는 명령이에요. /도움")
                continue
            if commands.is_stale(u, now.timestamp()):
                stale_skipped = True
                log.info("오래된 명령 건너뜀: %s", name)
                continue
            if batch_count >= commands.MAX_PER_BATCH or state.commands_in_last_hour(now) >= commands.MAX_PER_HOUR:
                limited = True
                log.info("명령 제한으로 건너뜀: %s", name)
                continue
            batch_count += 1
            state.record_command(now)
            to_run.append(name)
            handled += 1
            continue

        ignored += 1
    if ignored:
        log.info("허용되지 않았거나 해석 불가한 업데이트 %d건 무시", ignored)
    if stale_skipped:
        _reply(api, token, allowed_chat_id, "늦게 받은 명령은 건너뛰었어요.")
    if limited:
        _reply(api, token, allowed_chat_id, f"요청이 많아 일부 명령은 건너뛰었어요 (최대 {commands.MAX_PER_HOUR}개/시간).")
    return handled, to_run


def poll_once(
    cfg: Config, *, token: str, chat_id: str, timeout: int = 0, api=telegram, now: Optional[datetime] = None
) -> int:
    """Read pending button presses / commands once and record them. Returns how many
    were acted on.

    The network wait happens OUTSIDE the state lock so a long poll never blocks
    heartbeat. Command handlers (Google / notice-page calls) also run outside the
    lock, after it is released; the watcher lock is refreshed before each one so a
    slow handler cannot make a second poller think this one died (SECURITY.md 3b).
    """
    now = now or datetime.now()
    path = cfg.resolved_state_path()
    allowed = ("callback_query", "message") if cfg.commands else ("callback_query",)
    updates = api.get_updates(token, State(path).offset, timeout, allowed=allowed)
    if not updates:
        return 0
    with locked(path):
        state = State(path)  # re-read: heartbeat may have written meanwhile
        handled, to_run = process_updates(
            updates, state, token=token, allowed_chat_id=chat_id, enabled_commands=cfg.commands, now=now, api=api)
        state.save()
    for name in to_run:
        touch_watcher(cfg)
        _reply(api, token, chat_id, commands.run(name, cfg, now))
    return handled
