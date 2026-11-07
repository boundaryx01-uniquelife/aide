"""`/기억`: things the owner typed into the bot's own private chat on purpose.

What is stored is ONLY text the owner wrote himself in that chat (inbox.py has already
checked: his id, a private chat, not a bot, not forwarded). Mail, news, calendar and task
text is never saved here, not even as a suggestion: a poisoned memory would steer every
later model call. The file (`memory.json` next to the state file) holds at most
MAX_ITEMS short, cleaned items; a damaged file is set aside and treated as empty.

Writes happen only while the caller holds the state lock (inbox.poll_once). Reading a
copy for a prompt needs no lock because saves are atomic (tmp + os.replace).

The model only ever receives these items as background text; its outputs stay closed
values (see chat.py / llm.py), so a memory can sway a choice but never write a reply.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from .models import clean

log = logging.getLogger("aide.memory")

MAX_ITEMS = 50
MAX_TEXT = 200            # one item, after clean()
MAX_MESSAGE = 230         # the whole "/기억 <글>" message
PROMPT_LIMIT = 3000       # characters handed to a model call
PROMPT_HEADER = "사용자가 직접 적어 둔 기억(참고용, 사용자가 쓴 것): "
ID_RE = re.compile(r"^[0-9a-f]{8}$")
CALLBACK_RE = re.compile(r"^k:([0-9a-f]{8}):(undo|del)$")
COMMAND_RE = re.compile(r"^/(?:기억|remember)(?:@\w+)?(?:\s+(.*))?$", re.DOTALL | re.IGNORECASE)

EXPIRED = "만료된 버튼이에요"
TOO_LONG = "기억할 내용은 200자 이내로 적어 주세요."
EMPTY_BODY = "기억할 내용이 비어 있어요."
FULL = f"기억은 {MAX_ITEMS}개까지예요. /기억 에서 지워 주세요"
EMPTY_LIST = "기억해 둔 것이 없어요. 예: /기억 교육청 공문은 항상 중요"
GONE = "이미 지웠어요"
DELETED = "지웠어요"


def path_for(state_path: Path) -> Path:
    return Path(state_path).with_name("memory.json")


def parse_command(text) -> Optional[Tuple[str, str]]:
    """("list", "") for a bare `/기억`, ("add", body) for `/기억 <글>`, None for anything else.

    Width variants are folded (NFKC, so a full-width slash works). The word must stand alone:
    `/기억해` is not this command. The caller has already required an owner-only text message."""
    if not isinstance(text, str):
        return None
    m = COMMAND_RE.match(unicodedata.normalize("NFKC", text).strip())
    if not m:
        return None
    body = (m.group(1) or "").strip()
    return ("add", body) if body else ("list", "")


def is_data(data) -> bool:
    return isinstance(data, str) and bool(CALLBACK_RE.match(data))


class Memory:
    """items: [{"id", "text", "at"}], oldest first. Load-validate-save, nothing clever."""

    def __init__(self, path: Path, *, quarantine: bool = True):
        self.path = Path(path)
        self.items: List[dict] = []
        self.dirty = False
        self._quarantine = quarantine
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            seen = set()
            for v in list(data.get("items", []))[:MAX_ITEMS]:
                if not isinstance(v, dict):
                    continue
                rid, text = v.get("id"), clean(str(v.get("text", "")), MAX_TEXT)
                if not isinstance(rid, str) or not ID_RE.match(rid) or rid in seen or not text:
                    continue
                seen.add(rid)
                self.items.append({"id": rid, "text": text, "at": str(v.get("at", ""))})
        except (json.JSONDecodeError, OSError, ValueError, AttributeError, TypeError):
            self.items = []
            if not self._quarantine:
                return
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            try:
                os.replace(self.path, self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
                log.warning("기억 파일이 손상되어 따로 옮기고 빈 목록으로 시작합니다.")
            except OSError:
                log.warning("기억 파일을 읽을 수 없어 빈 목록으로 시작합니다.")

    def add(self, text: str, now: datetime) -> Tuple[str, Optional[dict]]:
        """("ok", item) | ("empty", None) | ("full", None) | ("dup", existing item)."""
        text = clean(text, MAX_TEXT)
        if not text:
            return "empty", None
        for it in self.items:
            if it["text"] == text:
                return "dup", it
        if len(self.items) >= MAX_ITEMS:
            return "full", None
        ids = {it["id"] for it in self.items}
        rid = secrets.token_hex(4)
        while rid in ids:
            rid = secrets.token_hex(4)
        item = {"id": rid, "text": text, "at": now.isoformat(timespec="seconds")}
        self.items.append(item)
        self.dirty = True
        return "ok", item

    def get(self, rid: str) -> Optional[dict]:
        return next((it for it in self.items if it["id"] == rid), None)

    def remove(self, rid: str) -> Optional[dict]:
        it = self.get(rid)
        if it is not None:
            self.items.remove(it)
            self.dirty = True
        return it

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"items": self.items}, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        self.dirty = False


def render_list(mem: Memory) -> Tuple[str, List[list]]:
    """The `/기억` reply: numbered lines plus one [🗑 N] button per item, four to a row."""
    if not mem.items:
        return EMPTY_LIST, []
    lines = [f"■ 기억 ({len(mem.items)}/{MAX_ITEMS})"]
    lines += [f"{n}. {it['text']}" for n, it in enumerate(mem.items, 1)]
    row: List[tuple] = []
    rows: List[list] = []
    for n, it in enumerate(mem.items, 1):
        row.append((f"🗑 {n}", f"k:{it['id']}:del"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return "\n".join(lines), rows


def added_reply(item: dict) -> Tuple[str, List[list]]:
    return f"기억했어요: {item['text']}", [[("↩ 취소", f"k:{item['id']}:undo")]]


def prompt_block(cfg) -> str:
    """The text appended to a model call's system prompt: "" unless memory is on and non-empty.

    Newest items win when the cap is hit; they are shown oldest-first and numbered."""
    if not getattr(cfg, "memory_enabled", False):
        return ""
    try:
        items = Memory(path_for(cfg.resolved_state_path()), quarantine=False).items
    except Exception:  # noqa: BLE001 -- a memory problem must never stop a model call from being built
        return ""
    kept: List[str] = []
    used = len(PROMPT_HEADER)
    for it in reversed(items):
        cost = len(it["text"]) + 6
        if used + cost > PROMPT_LIMIT:
            break
        kept.append(it["text"])
        used += cost
    if not kept:
        return ""
    kept.reverse()
    return PROMPT_HEADER + "\n" + "\n".join(f"{n}. {t}" for n, t in enumerate(kept, 1))
