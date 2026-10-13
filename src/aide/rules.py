"""Stage L1: the owner's own mail rules (hide / pin), keyed by ONE sender address.

A rule is created only from the owner's button presses (inbox.py), never by a model
and never from text an outsider wrote -- what is stored is a validated address, a
cleaned display name, an action and a timestamp. It lives in its own small file
(`rules.json` next to the state file); a damaged file is set aside and treated as
"no rules" (nothing gets hidden), never as an error that blocks notifications.

Writes happen only while the caller holds the state lock (inbox.poll_once, CLI).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .models import clean

log = logging.getLogger("aide.rules")

ACTIONS = ("hide", "pin")
ACTION_LABELS = {"hide": "알림제외", "pin": "중요"}
MAX_RULES = 200
# Exactly "mail:" + a full lowercase address. A domain-wide rule is deliberately impossible.
KEY_RE = re.compile(r"^mail:[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")


def valid_key(key: str) -> bool:
    return isinstance(key, str) and len(key) <= 120 and bool(KEY_RE.match(key))


def rule_id(key: str) -> str:
    """Short stable id used in button data (`r:<id>:del`)."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def path_for(state_path: Path) -> Path:
    return Path(state_path).with_name("rules.json")


class Rules:
    """key -> {"action", "label", "at"}. Load-validate-save, nothing clever."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.rules: Dict[str, dict] = {}
        self.dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw = dict(data.get("rules", {}))
            for k, v in raw.items():
                if valid_key(k) and isinstance(v, dict) and v.get("action") in ACTIONS:
                    self.rules[k] = {"action": v["action"], "label": clean(str(v.get("label", "")), 40),
                                     "at": str(v.get("at", ""))}
        except (json.JSONDecodeError, OSError, ValueError, AttributeError, TypeError):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            try:
                os.replace(self.path, self.path.with_name(f"{self.path.name}.corrupt-{stamp}"))
                log.warning("규칙 파일이 손상되어 따로 옮기고 규칙 없이 시작합니다.")
            except OSError:
                log.warning("규칙 파일을 읽을 수 없어 규칙 없이 시작합니다.")
            self.rules = {}

    def action(self, key: str) -> Optional[str]:
        rec = self.rules.get(key)
        return rec["action"] if rec else None

    def set(self, key: str, action: str, label: str, now: datetime) -> bool:
        """Add/replace a rule. False (and no change) for a bad key/action or a full file."""
        if not valid_key(key) or action not in ACTIONS:
            return False
        if key not in self.rules and len(self.rules) >= MAX_RULES:
            return False
        self.rules[key] = {"action": action, "label": clean(label, 40), "at": now.isoformat(timespec="seconds")}
        self.dirty = True
        return True

    def remove(self, key: str) -> bool:
        if key in self.rules:
            del self.rules[key]
            self.dirty = True
            return True
        return False

    def find_by_id(self, rid: str) -> Optional[str]:
        for k in self.rules:
            if rule_id(k) == rid:
                return k
        return None

    def recent(self, limit: int = 20) -> List[Tuple[str, dict]]:
        return sorted(self.rules.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)[:limit]

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"rules": self.rules}, ensure_ascii=False, indent=1, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, self.path)
        self.dirty = False


def apply(findings: Iterable, rules: Rules, *, allowed_senders: Iterable[str] = (), fold_bulk: bool = False):
    """Split `findings` into (shown, hidden, folded) under the owner's rules.

    shown   everything that gets listed; pinned ones come back with priority 3.
    hidden  mail the owner chose to leave out of notifications (never when the sender
            is one the owner listed in `mail_senders` -- that explicit allowlist wins).
    folded  bulk/list mail with no rule of its own, when `fold_bulk` is on: summarised
            on one line instead of listed.
    Findings without a `rule_key` (news, notices, git, odd addresses) are never touched.
    """
    allow = [s.strip().lower() for s in allowed_senders if isinstance(s, str) and s.strip()]
    shown: List = []
    hidden: List = []
    folded: List = []
    for f in findings:
        key = f.rule_key
        act = rules.action(key) if key else None
        if act == "hide" and not _sender_allowed(key, allow):
            hidden.append(f)
        elif act == "pin":
            shown.append(replace(f, priority=3))
        elif act is None and fold_bulk and f.bulk and key:
            folded.append(f)
        else:
            shown.append(f)
    return shown, hidden, folded


def _sender_allowed(key: str, allow: List[str]) -> bool:
    addr = key[len("mail:"):] if key.startswith("mail:") else ""
    domain = addr.rsplit("@", 1)[-1]
    for s in allow:
        if "@" in s:
            if addr == s:
                return True
        elif domain == s or domain.endswith("." + s):
            return True
    return False
