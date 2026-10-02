from __future__ import annotations

import logging
import os
from datetime import datetime, time
from typing import Callable, List, Optional

from . import telegram
from .checks import git_dirty, news_keywords
from .config import Config
from .models import Finding
from .state import State

log = logging.getLogger("aide.heartbeat")

SOURCE_LABELS = {"git": "작업 폴더", "news": "뉴스 키워드"}


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """True inside the quiet window. Handles windows that cross midnight (22:30-07:00)."""
    s, e, t = parse_hhmm(start), parse_hhmm(end), now.time()
    if s == e:
        return False
    if s < e:
        return s <= t < e
    return t >= s or t < e


def collect(cfg: Config, now: datetime, fetch=news_keywords.fetch_feed) -> List[Finding]:
    findings: List[Finding] = []
    findings += git_dirty.check(cfg.watch_repos, cfg.git_dirty_threshold, now.date())
    findings += news_keywords.check(cfg.news_feeds, cfg.news_keywords, fetch)
    return findings


def format_digest(findings: List[Finding], now: datetime, limit: int) -> str:
    shown, rest = findings[:limit], max(0, len(findings) - limit)
    lines = [f"[aide] 알림 {len(findings)}건 · {now:%m/%d %H:%M}"]
    for source in ("git", "news"):
        group = [f for f in shown if f.source == source]
        if not group:
            continue
        lines.append(f"\n■ {SOURCE_LABELS.get(source, source)}")
        for f in group:
            lines.append(f"• {f.title}")
            if f.detail:
                lines.append(f"  {f.detail}")
            if f.url:
                lines.append(f"  {f.url}")
    if rest:
        lines.append(f"\n…외 {rest}건 (다음 점검에서 이어서 알림)")
    return "\n".join(lines)


def run(
    cfg: Config,
    *,
    send: bool = False,
    force: bool = False,
    now: Optional[datetime] = None,
    sender: Callable[..., None] = telegram.send,
    fetch=news_keywords.fetch_feed,
) -> int:
    """One heartbeat. Exit codes: 0 ok (including 'nothing to report'), 1 send failed, 2 setup problem.

    Safety properties:
    - dry run (default) prints the digest and changes NO state, so it is repeatable;
    - items are marked as seen only AFTER a successful send, so a failed send is retried;
    - during quiet hours nothing is sent and nothing is marked, so items wait for morning.
    """
    now = now or datetime.now()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if send and not (token and chat_id):
        log.error("--send 에는 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 필요합니다 (.env 확인).")
        return 2

    if not force and in_quiet_hours(now, cfg.quiet_start, cfg.quiet_end):
        log.info("조용한 시간(%s-%s): 점검만 건너뜁니다.", cfg.quiet_start, cfg.quiet_end)
        return 0

    state = State(cfg.resolved_state_path())
    new = [f for f in collect(cfg, now, fetch) if not state.is_seen(f.key)]
    if not new:
        log.info("새로 알릴 것이 없습니다.")  # silence is the normal case
        return 0

    digest = format_digest(new, now, cfg.max_items_per_digest)
    if not send:
        print(digest)
        log.info("드라이런: 전송하지 않았고 상태도 바꾸지 않았습니다. (--send 로 실제 전송)")
        return 0

    try:
        sender(digest, token=token, chat_id=chat_id)
    except telegram.TelegramError as e:
        log.error("전송 실패: %s (다음 점검에서 재시도)", e)
        return 1

    for f in new[: cfg.max_items_per_digest]:
        state.mark(f.key, now)
    state.prune(now)
    state.save()
    log.info("%d건 전송 완료", min(len(new), cfg.max_items_per_digest))
    return 0
