"""Receiving side: handle button presses on digests.

Stage 3a is READ-ONLY on purpose. A button press can only change aide's own
bookkeeping (acknowledged / ignored / remind me later). It never runs a command,
touches files, or sends anything beyond a short confirmation. Free-text chat
messages are never requested from Telegram at all.
"""
from __future__ import annotations

import logging
import re

from . import telegram
from .config import Config
from .state import State, locked

log = logging.getLogger("aide.inbox")

CALLBACK_RE = re.compile(r"^d:([0-9a-f]{8}):(ok|skip|later)$")
REPLIES = {
    "ok": "확인했어요",
    "skip": "무시할게요",
    "later": "다음 점검 때 다시 알려드릴게요",
}


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


def process_updates(updates: list, state: State, *, token: str, allowed_chat_id: str, api=telegram) -> int:
    """Apply button presses to `state`. Returns how many were acted on.

    The offset advances past EVERY update (allowed or not) so nothing is read twice.
    Presses are idempotent, so redelivery after a crash is harmless.
    """
    handled = ignored = 0
    for u in updates:
        uid = u.get("update_id") if isinstance(u, dict) else None
        if isinstance(uid, int):
            state.offset = max(state.offset, uid + 1)
        if not isinstance(u, dict) or not is_allowed(u, allowed_chat_id):
            ignored += 1
            log.info("무시한 업데이트: %s", _describe(u, allowed_chat_id))
            continue
        cq = u["callback_query"]
        m = CALLBACK_RE.match(cq.get("data") if isinstance(cq.get("data"), str) else "")
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
        if m and reply in REPLIES.values():
            try:
                api.clear_buttons(token, str(allowed_chat_id), int(cq["message"]["message_id"]))
            except (telegram.TelegramError, KeyError, TypeError, ValueError) as e:
                log.info("버튼 제거 실패(기록은 반영됨): %s", e)
    if ignored:
        log.info("허용되지 않았거나 해석 불가한 업데이트 %d건 무시", ignored)
    return handled


def poll_once(cfg: Config, *, token: str, chat_id: str, timeout: int = 0, api=telegram) -> int:
    """Read pending button presses once and record them. Returns number acted on.

    The network wait happens OUTSIDE the state lock so a long poll never blocks heartbeat.
    """
    path = cfg.resolved_state_path()
    updates = api.get_updates(token, State(path).offset, timeout)
    if not updates:
        return 0
    with locked(path):
        state = State(path)  # re-read: heartbeat may have written meanwhile
        handled = process_updates(updates, state, token=token, allowed_chat_id=chat_id, api=api)
        state.save()
    return handled
