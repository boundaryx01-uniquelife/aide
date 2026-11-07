"""GitHub activity for the morning greeting (config `github_accounts`, default off).

Read-only. Per account (its own token from the environment) it lists the repositories the
account owns and reports, for yesterday in KST: failed GitHub Actions runs (every
non-archived repo -- scheduled runs happen without pushes) and commit / merged-PR / open-PR
counts (only repos pushed since yesterday began). Commit messages and PR titles are never
fetched for display; only counts, plus the first failed workflow's name (clean 40).

No LLM. Every function degrades to fewer lines; nothing here may raise into the greeting.
Errors from netutil never contain the URL or body, and tokens never reach a log line.
"""
from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from . import netutil
from .models import clean

log = logging.getLogger("aide.github")

API = "https://api.github.com"      # the only host this module talks to
KST = timezone(timedelta(hours=9))
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MAX_ACCOUNTS = 5
MAX_PAGES = 3
MAX_LINES = 10
BUDGET_SECONDS = 120    # whole fetch; remaining repos are skipped past this


def token_env_name(account: str) -> str:
    """`boundaryx01-uniquelife` -> `GITHUB_TOKEN_BOUNDARYX01_UNIQUELIFE`."""
    return "GITHUB_TOKEN_" + account.upper().replace("-", "_")


def yesterday_range(now: datetime) -> Tuple[date, datetime, datetime]:
    """(KST date of yesterday, start, end) as aware UTC datetimes. `now` is naive local time (KST)."""
    day = now.date() - timedelta(days=1)
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    end = start + timedelta(days=1)
    return day, start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _url(path: str, **params) -> str:
    return API + path + "?" + urllib.parse.urlencode(params)


def _repo_path(owner: str, name: str) -> str:
    return f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


def _list(data) -> List[dict]:
    if not isinstance(data, list):
        raise netutil.NetError("예상하지 못한 응답 모양")
    return [x for x in data if isinstance(x, dict)]


class _Repo:
    def __init__(self, account: str, name: str):
        self.account, self.name = account, name
        self.fail_count = 0
        self.fail_name = ""
        self.commits = 0
        self.commits_capped = False
        self.merged = 0
        self.open_prs = 0
        self.open_capped = False

    def active(self) -> bool:
        return bool(self.commits or self.merged or self.open_prs)


def _owned_repos(account: str, get, headers) -> List[Tuple[str, Optional[datetime]]]:
    """(name, pushed_at) of the account's non-archived repos, max MAX_PAGES x 100. Raises NetError."""
    out: List[Tuple[str, Optional[datetime]]] = []
    for page in range(1, MAX_PAGES + 1):
        data = _list(get(_url("/user/repos", affiliation="owner", per_page=100, sort="pushed", page=page),
                         headers=headers))
        for r in data:
            name = r.get("name")
            if r.get("archived") is True or not isinstance(name, str):
                continue
            if not REPO_RE.match(name) or name in (".", ".."):
                continue   # never put an odd name into a URL path
            out.append((name, _parse(r.get("pushed_at"))))
        if len(data) < 100:
            break
    return out


def _inspect(repo: _Repo, owner: str, pushed: Optional[datetime], start: datetime, end: datetime, get, headers) -> None:
    """Fill one repo's numbers. Each call fails on its own (that part is just skipped)."""
    base = _repo_path(owner, repo.name)
    try:
        runs = get(_url(base + "/actions/runs", status="failure", created=f"{_iso(start)}..{_iso(end)}",
                        per_page=5), headers=headers)
        items = runs.get("workflow_runs") if isinstance(runs, dict) else None
        if isinstance(items, list) and items:
            total = runs.get("total_count")
            repo.fail_count = total if isinstance(total, int) and total >= len(items) else len(items)
            first = items[0] if isinstance(items[0], dict) else {}
            repo.fail_name = clean(str(first.get("name") or ""), 40)
    except netutil.NetError as e:
        log.warning("GitHub 실패 작업 조회 건너뜀 (%s)", e)
    if pushed is None or pushed < start:
        return
    try:
        n = len(_list(get(_url(base + "/commits", since=_iso(start), until=_iso(end), per_page=100), headers=headers)))
        repo.commits, repo.commits_capped = n, n >= 100
    except netutil.NetError as e:
        log.warning("GitHub 커밋 조회 건너뜀 (%s)", e)
    try:
        closed = _list(get(_url(base + "/pulls", state="closed", sort="updated", direction="desc", per_page=30),
                           headers=headers))
        for p in closed:
            m = _parse(p.get("merged_at"))
            if m is not None and start <= m < end:
                repo.merged += 1
    except netutil.NetError as e:
        log.warning("GitHub 병합 PR 조회 건너뜀 (%s)", e)
    try:
        n = len(_list(get(_url(base + "/pulls", state="open", per_page=100), headers=headers)))
        repo.open_prs, repo.open_capped = n, n >= 100
    except netutil.NetError as e:
        log.warning("GitHub 열린 PR 조회 건너뜀 (%s)", e)


def _label(repo: _Repo, dup: set) -> str:
    return f"{repo.account}/{repo.name}" if repo.name in dup else repo.name


def _activity(repo: _Repo, label: str) -> str:
    parts = []
    if repo.commits:
        parts.append(f"커밋 {repo.commits}{'+' if repo.commits_capped else ''}")
    if repo.merged:
        parts.append(f"PR 병합 {repo.merged}")
    if repo.open_prs:
        parts.append(f"열린 PR {repo.open_prs}{'+' if repo.open_capped else ''}")
    return f"• {label}: " + " · ".join(parts)


def fetch(cfg, now: datetime, get: Callable = netutil.get_json, environ: Optional[Mapping[str, str]] = None,
          clock: Callable[[], float] = time.monotonic) -> List[str]:
    """The "■ GitHub (어제 MM/DD)" section lines (ending with a blank line), or [] when off or
    nothing happened yesterday. Never raises."""
    accounts = list(getattr(cfg, "github_accounts", []) or [])
    if not accounts:
        return []
    try:
        return _build(accounts, now, get, os.environ if environ is None else environ, clock)
    except Exception:  # noqa: BLE001
        log.exception("GitHub 구역 처리 중 오류")
        return []


def _build(accounts: List[str], now: datetime, get, environ, clock) -> List[str]:
    day, start, end = yesterday_range(now)
    deadline = clock() + BUDGET_SECONDS
    notes: List[str] = []
    repos: List[_Repo] = []
    for account in accounts:
        token = (environ.get(token_env_name(account)) or "").strip()
        if not token:
            notes.append(f"GitHub({account}): 토큰 없음")
            continue
        headers = _headers(token)
        try:
            owned = _owned_repos(account, get, headers)
        except netutil.NetError as e:
            log.warning("GitHub(%s) 저장소 목록 실패 (%s)", account, e)
            notes.append(f"GitHub({account}): 불러오지 못함 (토큰 확인)")
            continue
        for name, pushed in owned:
            if clock() > deadline:
                log.warning("GitHub 조회 시간 초과: 남은 저장소 건너뜀")
                break
            repo = _Repo(account, name)
            _inspect(repo, account, pushed, start, end, get, headers)
            repos.append(repo)

    counts: Dict[str, int] = {}
    for r in repos:
        counts[r.name] = counts.get(r.name, 0) + 1
    dup = {n for n, c in counts.items() if c > 1}

    fails = [r for r in repos if r.fail_count]
    fails.sort(key=lambda r: (-r.fail_count, _label(r, dup)))
    acts = [r for r in repos if r.active()]
    acts.sort(key=lambda r: (-r.commits, -r.merged, -r.open_prs, _label(r, dup)))

    body: List[str] = []
    for r in fails:
        name = f" ({r.fail_name})" if r.fail_name else ""
        body.append(f"⚠ {_label(r, dup)}: 자동 작업 실패 {r.fail_count}{name}")
    body += [_activity(r, _label(r, dup)) for r in acts]
    if len(body) > MAX_LINES:
        body = body[:MAX_LINES] + [f"…외 {len(body) - MAX_LINES}개"]
    lines = notes + body
    if not lines:
        return []
    return [f"■ GitHub (어제 {day:%m/%d})", *lines, ""]
