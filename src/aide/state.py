from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

log = logging.getLogger("aide.state")


class State:
    """Remembers what has already been reported so the same item is never sent twice.

    Stored as a small JSON file. Writes are atomic (temp file + os.replace) so a
    crash or power loss mid-write cannot corrupt it.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.seen: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.seen = {str(k): str(v) for k, v in dict(data.get("seen", {})).items()}
        except (json.JSONDecodeError, OSError, ValueError, AttributeError):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            try:
                os.replace(self.path, backup)
                log.warning("상태 파일이 손상되어 %s 로 옮기고 새로 시작합니다.", backup.name)
            except OSError:
                log.warning("상태 파일을 읽을 수 없어 새로 시작합니다.")
            self.seen = {}

    def is_seen(self, key: str) -> bool:
        return key in self.seen

    def mark(self, key: str, now: datetime) -> None:
        self.seen[key] = now.isoformat(timespec="seconds")

    def prune(self, now: datetime, days: int = 45) -> int:
        cutoff = now - timedelta(days=days)
        drop = []
        for k, v in self.seen.items():
            try:
                if datetime.fromisoformat(v) < cutoff:
                    drop.append(k)
            except ValueError:
                drop.append(k)
        for k in drop:
            del self.seen[k]
        return len(drop)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps({"seen": self.seen}, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
