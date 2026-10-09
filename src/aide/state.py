from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("aide.state")


class StateLocked(Exception):
    """Another aide process is writing the state file right now."""


@contextmanager
def locked(path, timeout: float = 10.0, stale: float = 120.0):
    """Cross-process lock around a read-modify-write of the state file.

    heartbeat and poll can run at the same time; without this one of them could
    overwrite the other's changes. A lock older than `stale` seconds is treated as
    left behind by a crash and removed.
    """
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue
            if age > stale:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise StateLocked("다른 aide 프로세스가 상태 파일을 쓰는 중입니다.") from None
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


class State:
    """Remembers what has already been reported, which digests await a reply, and
    how far the Telegram update queue has been read.

    Stored as a small JSON file. Writes are atomic (temp file + os.replace) so a
    crash or power loss mid-write cannot corrupt it. Older files without the
    `digests` / `offset` / `cmd_times` / `llm_calls` keys still load.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.seen: Dict[str, str] = {}
        self.digests: Dict[str, dict] = {}
        self.offset: int = 0
        self.cmd_times: List[float] = []  # stage 3b: timestamps of executed commands, for rate limiting
        self.llm_calls: Dict[str, int] = {}  # stage 2a: date (ISO) -> call count, for the daily budget
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.seen = {str(k): str(v) for k, v in dict(data.get("seen", {})).items()}
            self.digests = {
                str(k): {
                    "keys": [str(x) for x in v["keys"]],
                    "at": str(v["at"]),
                    "status": str(v.get("status", "pending")),
                }
                for k, v in dict(data.get("digests", {})).items()
            }
            self.offset = int(data.get("offset", 0))
            self.cmd_times = [float(x) for x in data.get("cmd_times", [])]
            self.llm_calls = {str(k): int(v) for k, v in dict(data.get("llm_calls", {})).items()}
        except (json.JSONDecodeError, OSError, ValueError, AttributeError, KeyError, TypeError):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            try:
                os.replace(self.path, backup)
                log.warning("상태 파일이 손상되어 %s 로 옮기고 새로 시작합니다.", backup.name)
            except OSError:
                log.warning("상태 파일을 읽을 수 없어 새로 시작합니다.")
            self.seen, self.digests, self.offset, self.cmd_times, self.llm_calls = {}, {}, 0, [], {}

    def is_seen(self, key: str) -> bool:
        return key in self.seen

    def mark(self, key: str, now: datetime) -> None:
        self.seen[key] = now.isoformat(timespec="seconds")

    def add_digest(self, digest_id: str, keys, now: datetime) -> None:
        self.digests[digest_id] = {
            "keys": list(keys),
            "at": now.isoformat(timespec="seconds"),
            "status": "pending",
        }

    def resolve_digest(self, digest_id: str, action: str) -> Optional[str]:
        """Apply a button press. Returns the action taken, "dup" if this digest was
        already answered, or None if the digest is unknown/expired.

        "later" forgets the digest's items so the next heartbeat reports them again.
        Pressing a button twice (or Telegram redelivering the update) changes nothing.
        """
        rec = self.digests.get(digest_id)
        if rec is None:
            return None
        if rec["status"] != "pending":
            return "dup"
        rec["status"] = action
        if action == "later":
            for k in rec["keys"]:
                self.seen.pop(k, None)
        return action

    def commands_in_last_hour(self, now: datetime, window_seconds: float = 3600.0) -> int:
        """Stage 3b rate limit: drop timestamps older than the window, return how many remain."""
        cutoff = now.timestamp() - window_seconds
        self.cmd_times = [t for t in self.cmd_times if t >= cutoff]
        return len(self.cmd_times)

    def record_command(self, now: datetime) -> None:
        self.cmd_times.append(now.timestamp())

    def llm_calls_today(self, now: datetime, days: int = 7) -> int:
        """Stage 2a budget: prune call counts older than `days`, return today's count."""
        cutoff = now.date() - timedelta(days=days)

        def old(k: str) -> bool:
            try:
                return date.fromisoformat(k) < cutoff
            except ValueError:
                return True

        for k in [k for k in self.llm_calls if old(k)]:
            del self.llm_calls[k]
        return self.llm_calls.get(now.date().isoformat(), 0)

    def record_llm_call(self, now: datetime) -> None:
        key = now.date().isoformat()
        self.llm_calls[key] = self.llm_calls.get(key, 0) + 1

    def prune(self, now: datetime, days: int = 45) -> int:
        cutoff = now - timedelta(days=days)

        def old(stamp: str) -> bool:
            try:
                return datetime.fromisoformat(stamp) < cutoff
            except ValueError:
                return True

        drop = [k for k, v in self.seen.items() if old(v)]
        for k in drop:
            del self.seen[k]
        for d in [d for d, rec in self.digests.items() if old(rec["at"])]:
            del self.digests[d]
        return len(drop)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {"seen": self.seen, "digests": self.digests, "offset": self.offset,
                 "cmd_times": self.cmd_times, "llm_calls": self.llm_calls},
                ensure_ascii=False,
                indent=1,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
