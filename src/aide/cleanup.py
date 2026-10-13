"""Stage L1: the "정리" button flow -- the owner turns a mail sender into a rule.

Everything here is driven by button presses that inbox.py has already verified came
from the owner. Button data is matched against fixed patterns; the only things a
press can change are aide's own rules file (and bookkeeping). Nothing is sent to a
mail sender or anyone else: "수신거부" just opens Gmail on an address aide built itself.

Flow (see ARCHITECTURE.md "선호 학습 L1"):
  m:<digest>:open            -> new message listing the digest's senders
  m:<digest>:t<i>            -> that message becomes the 4-way choice for sender i
  m:<digest>:t<i>:(hide|keep|pin|unsub)
  m:<digest>:close           -> dismiss
  r:list / r:<rule id>:del   -> list / delete rules
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional, Tuple

from . import rules as rules_mod
from . import telegram
from .models import clean
from .state import State

log = logging.getLogger("aide.cleanup")

MENU_RE = re.compile(r"^m:([0-9a-f]{8}):(open|close|t([0-7])(?::(hide|keep|pin|unsub))?)$")
RULE_RE = re.compile(r"^r:(?:(list)|([0-9a-f]{8}):del)$")

EXPIRED = "만료된 알림이에요"


def is_cleanup_data(data) -> bool:
    return isinstance(data, str) and bool(MENU_RE.match(data) or RULE_RE.match(data))


def _addr(key: str) -> str:
    return key[len("mail:"):] if key.startswith("mail:") else key


def _target(state: State, digest_id: str, index: int) -> Optional[dict]:
    rec = state.digests.get(digest_id)
    targets = rec.get("targets") if rec else None
    if not targets or index >= len(targets):
        return None
    t = targets[index]
    return t if rules_mod.valid_key(t.get("k", "")) else None


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


def render_list(rule_set) -> Tuple[str, List[list]]:
    items = rule_set.recent(20)
    if not items:
        return "저장된 규칙이 없어요.", []
    lines = ["내 규칙 (최근 20개)"]
    buttons = []
    for n, (key, rec) in enumerate(items, 1):
        lines.append(f"{n}. {rules_mod.ACTION_LABELS[rec['action']]} · {rec.get('label', '')} <{_addr(key)}>")
        buttons.append([(f"🗑 {n}번 삭제", f"r:{rules_mod.rule_id(key)}:del")])
    return "\n".join(lines), buttons


def handle(data: str, cq: dict, *, state: State, rule_set, token: str, chat_id: str, api, now: datetime) -> str:
    """Process one verified cleanup/rule button. Returns the toast text. `rule_set` and
    `state` are changed in memory only; the caller persists them under the state lock."""
    message_id = (cq.get("message") or {}).get("message_id")

    m = RULE_RE.match(data)
    if m:
        if m.group(1):   # r:list
            text, buttons = render_list(rule_set)
            _send(api, token, chat_id, text, buttons or None)
            return ""
        key = rule_set.find_by_id(m.group(2))
        if key is None:
            return "이미 없어요"
        rule_set.remove(key)
        text, buttons = render_list(rule_set)
        _edit(api, token, chat_id, message_id, text, buttons or None)
        return "삭제했어요"

    m = MENU_RE.match(data)
    if not m:
        return "알 수 없는 버튼이에요"
    digest_id, what, idx, action = m.group(1), m.group(2), m.group(3), m.group(4)

    if what == "close":
        _edit(api, token, chat_id, message_id, "닫았어요")
        return ""
    if what == "open":
        rec = state.digests.get(digest_id)
        targets = (rec or {}).get("targets") or []
        if not targets:
            return EXPIRED
        lines = ["어느 발신자를 정리할까요?"]
        buttons = []
        for i, t in enumerate(targets):
            if not rules_mod.valid_key(t.get("k", "")):
                continue
            existing = rule_set.action(t["k"])
            tag = f" ({rules_mod.ACTION_LABELS[existing]})" if existing else ""
            line = f"{i + 1}. {clean(t.get('l', ''), 20)} <{_addr(t['k'])}>{tag}"
            lines.append(line)
            buttons.append([(f"{i + 1}. {clean(t.get('l', ''), 14) or _addr(t['k'])}", f"m:{digest_id}:t{i}")])
        buttons.append([("📋 규칙 목록", "r:list"), ("닫기", f"m:{digest_id}:close")])
        _send(api, token, chat_id, "\n".join(lines), buttons)
        return ""

    t = _target(state, digest_id, int(idx))
    if t is None:
        return EXPIRED
    key, label, link = t["k"], clean(t.get("l", ""), 40), t.get("u", "")
    who = f"{label} <{_addr(key)}>" if label else f"<{_addr(key)}>"
    base = f"m:{digest_id}:t{idx}"

    if action is None:
        current = rule_set.action(key)
        head = f"{who}\n현재: {rules_mod.ACTION_LABELS[current]}" if current else who
        buttons = [[("알림제외", f"{base}:hide"), ("유지", f"{base}:keep")],
                   [("중요", f"{base}:pin"), ("수신거부", f"{base}:unsub")]]
        _edit(api, token, chat_id, message_id, f"{head}\n이 발신자의 메일을 어떻게 할까요?", buttons)
        return ""
    if action == "unsub":
        text = (f"{who}\nGmail 에서 이 메일을 열어 '수신거부'를 눌러 주세요.\n"
                "(aide 는 발신자에게 아무것도 보내지 않습니다. 반영까지 며칠 걸릴 수 있어요.)\n"
                "알림에서도 뺄까요?")
        buttons = []
        if isinstance(link, str) and link.startswith(telegram.URL_PREFIX):
            buttons.append([("📧 Gmail 열기", link)])
        buttons.append([("알림제외", f"{base}:hide"), ("유지", f"{base}:keep")])
        _edit(api, token, chat_id, message_id, text, buttons)
        return ""
    if action == "keep":
        rule_set.remove(key)
        _edit(api, token, chat_id, message_id, f"{who}\n그대로 알려 드릴게요 (규칙 없음)")
        return "저장했어요"
    if not rule_set.set(key, action, label, now):
        _edit(api, token, chat_id, message_id, f"{who}\n규칙이 너무 많아 더 만들 수 없어요. 목록에서 지워 주세요.",
              [[("📋 규칙 목록", "r:list")]])
        return "규칙이 가득 찼어요"
    done = "알림에서 제외할게요" if action == "hide" else "항상 맨 위 '중요'에 보여 드릴게요"
    _edit(api, token, chat_id, message_id, f"{who}\n{done}", [[("📋 규칙 목록", "r:list")]])
    return "저장했어요"
