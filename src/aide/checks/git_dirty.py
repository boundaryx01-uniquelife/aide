from __future__ import annotations

import logging
import subprocess
from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional

from ..models import Finding, clean

log = logging.getLogger("aide.git")


def count_changes(repo: Path) -> Optional[int]:
    """Number of uncommitted entries (modified + untracked). None if not a usable repo."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        log.warning("git 실행 실패(%s): %s", repo, type(e).__name__)
        return None
    if proc.returncode != 0:
        log.warning("git 저장소가 아니거나 읽을 수 없음: %s", repo)
        return None
    return sum(1 for line in proc.stdout.splitlines() if line.strip())


def check(repos: Iterable[str], threshold: int, today: date) -> List[Finding]:
    """One finding per repo per day when uncommitted changes pile up.

    The key contains the date, so a repo is reported at most once a day.
    """
    findings: List[Finding] = []
    for raw in repos:
        repo = Path(raw)
        n = count_changes(repo)
        if n is None or n < threshold:
            continue
        findings.append(
            Finding(
                key=f"git:{repo.as_posix()}:{today.isoformat()}",
                source="git",
                title=clean(f"{repo.name}: 커밋 안 된 변경 {n}개"),
                detail=clean(str(repo), 300),
            )
        )
    return findings
