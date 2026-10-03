from __future__ import annotations

import logging
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

from ..models import clean

log = logging.getLogger("aide.yesterday")


def commits_on(repo: Path, day: date) -> Optional[List[str]]:
    """Commit subjects made on `day` (local time), newest first, across all branches.

    None if `repo` is not a usable git repository. Subjects are external text, so each
    one goes through clean().
    """
    start, end = day, day + timedelta(days=1)
    try:
        proc = subprocess.run(
            [
                "git", "-C", str(repo), "log", "--all", "--no-merges",
                f"--since={start.isoformat()} 00:00:00",
                f"--until={end.isoformat()} 00:00:00",
                "--pretty=format:%s",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        log.warning("git 실행 실패(%s): %s", repo, type(e).__name__)
        return None
    if proc.returncode != 0:
        log.warning("git 저장소가 아니거나 읽을 수 없음: %s", repo)
        return None
    return [clean(line, 100) for line in proc.stdout.splitlines() if line.strip()]
