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
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Tuple, Union

from . import chat, cleanup, commands, rules as rules_mod, telegram
from .config import Config
from .state import State, StateLocked, locked

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


def buttons_for(digest_id: str, with_clean: bool = False):
    rows = [[("✅ 확인", f"d:{digest_id}:ok"), ("🔕 무시", f"d:{digest_id}:skip"), ("⏰ 나중에", f"d:{digest_id}:later")]]
    if with_clean:   # stage L1: only when this digest lists at least one mail sender
        rows.append([("🧹 정리", f"m:{digest_id}:open")])
    return rows


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


@dataclass(frozen=True)
class Notice:
    """A short notice for the owner ("늦게 받은 명령…", "모르는 명령이에요"). Collected while the state
    lock is held and sent only after it is released, at most one of each kind per batch."""
    text: str


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
    rule_set=None,
    chat_max_per_day: int = 0,
) -> Tuple[int, List[Union[str, chat.ChatRequest, Notice]]]:
    """Apply button presses to `state`, and decide which stage-3b commands may run.

    Returns (how many updates were acted on, canonical command names to execute
    AFTER the caller releases the state lock -- command handlers call Google /
    notice-page APIs, which must never happen while the lock is held).

    The offset advances past EVERY update (allowed or not) so nothing is read twice.
    Button presses are idempotent, so redelivery after a crash is harmless. Commands
    are "at most once": the batch/hour counters are consumed here, under the lock,
    before the command actually runs, so a crash mid-reply never re-runs it.

    Stage C1: with `chat_max_per_day` > 0 a plain sentence (not starting with "/", not a
    command word) becomes a `chat.ChatRequest` in the same list. It shares the batch/hour
    limits with commands and also spends one unit of the separate daily chat budget, taken
    here under the lock before the model is called. Chat has its own hourly limit, so sentences
    never use up the `/명령` allowance.

    Notices to the owner come back in the same list as `Notice` items: nothing is sent to
    Telegram from inside the lock, and a flood of junk produces at most one notice per kind.
    A message older than the offset this call started with is a redelivery (a second poller
    read it first): it is skipped so it is never run, billed or answered twice.
    """
    now = now or datetime.now()
    enabled_commands = list(enabled_commands)
    handled = ignored = 0
    to_run: List[Union[str, chat.ChatRequest, Notice]] = []
    batch_count = 0
    start_offset = state.offset
    stale_skipped = limited = chat_limited = chat_busy = unknown_noted = long_noted = False
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
            data = cq.get("data") if isinstance(cq.get("data"), str) else ""
            if cleanup.is_cleanup_data(data):
                # Stage L1 buttons. With rules switched off they get the same "expired"
                # answer as any stale button -- nothing is read or changed.
                toast = cleanup.EXPIRED
                if rule_set is not None:
                    toast = cleanup.handle(data, cq, state=state, rule_set=rule_set, token=token,
                                           chat_id=str(allowed_chat_id), api=api, now=now)
                    handled += 1
                try:
                    api.answer_callback(token, str(cq.get("id", "")), toast)
                except telegram.TelegramError as e:
                    log.info("토스트 표시 실패: %s", e)
                continue
            m = CALLBACK_RE.match(data)
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
                keep_clean = rule_set is not None and bool((state.digests.get(m.group(1)) or {}).get("targets"))
                try:
                    if keep_clean:   # stage L1: leave only the 정리 button so the owner can still use it
                        api.set_buttons(token, str(allowed_chat_id), int(cq["message"]["message_id"]),
                                        [[("🧹 정리", f"m:{m.group(1)}:open")]])
                    else:
                        api.clear_buttons(token, str(allowed_chat_id), int(cq["message"]["message_id"]))
                except (telegram.TelegramError, KeyError, TypeError, ValueError) as e:
                    log.info("버튼 제거 실패(기록은 반영됨): %s", e)
            continue

        if "message" in u:
            if not commands.is_command_message(u, allowed_chat_id):
                ignored += 1
                log.info("무시한 메시지: %s", _describe_message(u, allowed_chat_id))
                continue
            if isinstance(uid, int) and uid < start_offset:
                log.info("이미 읽은 메시지가 다시 와서 건너뜀")
                continue
            text = u["message"]["text"]
            name = commands.resolve(text, enabled_commands)
            if name is None and chat_max_per_day > 0 and chat.looks_like_chat(text):
                if len(text) > chat.MAX_CHAT_LEN:
                    if not long_noted:
                        long_noted = True
                        to_run.append(Notice(chat.TOO_LONG))
                elif commands.is_stale(u, now.timestamp()):
                    stale_skipped = True
                    log.info("오래된 대화 건너뜀")
                elif batch_count >= commands.MAX_PER_BATCH or state.chats_in_last_hour(now) >= chat.MAX_PER_HOUR:
                    chat_busy = True
                    log.info("대화 제한으로 건너뜀")
                elif state.chat_calls_today(now) >= chat_max_per_day:
                    chat_limited = True
                    log.info("하루 대화 한도 도달")
                else:
                    batch_count += 1
                    state.record_chat_call(now)
                    to_run.append(chat.ChatRequest(text))
                    handled += 1
                continue
            if name is None:
                log.info("알 수 없는 명령 입력")  # never log the text itself
                if not unknown_noted:
                    unknown_noted = True
                    to_run.append(Notice("모르는 명령이에요. /도움"))
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
        to_run.append(Notice("늦게 받은 명령은 건너뛰었어요."))
    if chat_limited:
        to_run.append(Notice(chat.OUT_OF_BUDGET))
    if chat_busy:
        to_run.append(Notice(chat.CHAT_BUSY))
    if limited:
        to_run.append(Notice(f"요청이 많아 일부 명령은 건너뛰었어요 (최대 {commands.MAX_PER_HOUR}개/시간)."))
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
    given_now = now
    path = cfg.resolved_state_path()
    allowed = ("callback_query", "message") if cfg.commands else ("callback_query",)
    updates = api.get_updates(token, State(path).offset, timeout, allowed=allowed)
    if not updates:
        return 0
    now = given_now or datetime.now()   # taken AFTER the long wait: a message that arrives past midnight is "today"
    with locked(path):
        state = State(path)  # re-read: heartbeat may have written meanwhile
        rule_set = rules_mod.Rules(rules_mod.path_for(path)) if cfg.rules_enabled else None
        handled, to_run = process_updates(
            updates, state, token=token, allowed_chat_id=chat_id, enabled_commands=cfg.commands, now=now, api=api,
            rule_set=rule_set, chat_max_per_day=cfg.chat_max_per_day if cfg.chat_enabled else 0)
        if rule_set is not None:
            rule_set.save()   # same lock as the state: one writer at a time
        state.save()
    refunds = 0
    for item in to_run:
        touch_watcher(cfg)
        if isinstance(item, Notice):
            reply = item.text
        elif isinstance(item, chat.ChatRequest):
            reply, refund = chat.answer_full(item, cfg, now, api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
            refunds += 1 if refund else 0
        else:
            reply = commands.run(item, cfg, now)
        _reply(api, token, chat_id, reply)
    if refunds:   # no answer was generated (no key / network down): the sentence does not use up the day's budget
        try:
            with locked(path):
                state = State(path)
                for _ in range(refunds):
                    state.refund_chat_call(now)
                state.save()
        except StateLocked:
            log.info("대화 한도 환불을 건너뜀(상태 파일 사용 중)")
    return handled
