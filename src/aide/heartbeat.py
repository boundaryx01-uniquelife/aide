from __future__ import annotations

import logging
import os
import secrets
from dataclasses import replace
from datetime import datetime, time
from typing import Callable, List, Optional
from urllib.parse import quote

from . import gmail, google_auth, inbox, netutil, telegram
from .checks import git_dirty, news_keywords, notice_pages
from .config import Account, Config
from .models import Finding
from .state import State, StateLocked, locked

log = logging.getLogger("aide.heartbeat")

SOURCE_LABELS = {"git": "작업 폴더", "news": "뉴스 키워드", "notice": "기관 공지·마감", "mail": "메일"}


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


def _keys(f: Finding):
    return (f.key, *f.alt_keys)


def _is_new(state: State, f: Finding) -> bool:
    return not any(state.is_seen(k) for k in _keys(f))


def mail_slot(cfg: Config, now: datetime) -> Optional[str]:
    """State key of today's most recent mail-digest hour that has already started (None before the first)."""
    started = [h for h in cfg.mail_recent_hours if h <= now.hour]
    return f"mailslot:{now.date().isoformat()}:{max(started):02d}" if started else None


def _tag_account(found: List[Finding], acct: Account, idx: int, many: bool) -> List[Finding]:
    """With several accounts: label the title, keep keys unique per account (the first account keeps
    its old keys, so nothing is re-announced), and point the link at the right mailbox."""
    if not many:
        return found
    out = []
    for f in found:
        key = f.key if idx == 0 else f"{f.key}:{acct.name}"
        url = f.url
        if url and acct.email:
            url = url.replace("/mail/u/0/#all/", f"/mail/?authuser={quote(acct.email)}#all/")
        elif url and idx != 0:
            url = ""   # /u/0/ would open the wrong mailbox; better no link than a wrong one
        out.append(replace(f, key=key, title=f"[{acct.name}] {f.title}", url=url))
    return out


def collect(
    cfg: Config,
    now: datetime,
    fetch=news_keywords.fetch_feed,
    mail_fetch=gmail.unread_from,
    include_recent: bool = False,
    mail_status: Optional[dict] = None,
    recent_fetch=None,
) -> List[Finding]:
    findings: List[Finding] = []
    findings += git_dirty.check(cfg.watch_repos, cfg.git_dirty_threshold, now.date())
    findings += news_keywords.check(
        cfg.news_feeds, cfg.news_keywords, fetch, now=now, max_age_hours=cfg.news_max_age_hours
    )
    findings += notice_pages.check(
        cfg.notice_pages, cfg.notice_keywords, fetch, now, cfg.notice_max_per_page, cfg.notice_exclude
    )
    want_recent = include_recent and bool(cfg.mail_recent_hours)
    if cfg.mail_enabled and (cfg.mail_senders or want_recent):
        mail: List[Finding] = []
        accts = cfg.accounts()
        many = len(accts) > 1
        recent_ok = True   # the time-slot digest counts as done only if EVERY account was read
        for idx, acct in enumerate(accts):
            tag = f" ({acct.name})" if many else ""
            got: List[Finding] = []
            try:
                token = google_auth.access_token(cfg.resolved_google_client(), acct.token_path,
                                                 need_scope=google_auth.GMAIL_SCOPE)
            except (google_auth.GoogleAuthError, netutil.NetError) as e:
                log.warning("메일 확인 건너뜀%s: %s", tag, e)  # never blocks the other checks
                recent_ok = False
                continue
            if cfg.mail_senders:
                try:
                    got += mail_fetch(token, cfg.mail_senders, cfg.mail_max_items)
                except (netutil.NetError, gmail.MailConfigError) as e:
                    log.warning("허용 발신자 메일 확인 건너뜀%s: %s", tag, e)
            if want_recent:
                try:
                    got += (recent_fetch or gmail.recent_unread)(token, cfg.mail_recent_max, cfg.mail_recent_window_hours, cfg.mail_block, now)
                except (netutil.NetError, gmail.MailConfigError) as e:
                    log.warning("최근 메일 요약 건너뜀%s: %s", tag, e)
                    recent_ok = False
            mail += _tag_account(got, acct, idx, many)
        if want_recent and recent_ok and mail_status is not None:
            mail_status["recent_ok"] = True
        seen_keys = set()
        for f in mail:
            if f.key not in seen_keys:
                seen_keys.add(f.key)
                findings.append(f)
    return findings


def format_digest(findings: List[Finding], now: datetime, limit: int) -> str:
    shown, rest = findings[:limit], max(0, len(findings) - limit)
    lines = [f"[aide] 알림 {len(findings)}건 · {now:%m/%d %H:%M}"]
    for source in ("mail", "notice", "git", "news"):
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

    path = cfg.resolved_state_path()
    if not send:
        state = State(path)
        new = [f for f in collect(cfg, now, fetch, include_recent=True) if _is_new(state, f)]  # preview ignores the slot
        if not new:
            log.info("새로 알릴 것이 없습니다.")  # silence is the normal case
            return 0
        print(format_digest(new, now, cfg.max_items_per_digest))
        log.info("드라이런: 전송하지 않았고 상태도 바꾸지 않았습니다. (--send 로 실제 전송)")
        return 0

    slot = mail_slot(cfg, now) if cfg.mail_enabled else None
    slot_due = bool(slot) and not State(path).is_seen(slot)
    status: dict = {}
    findings = collect(cfg, now, fetch, include_recent=slot_due, mail_status=status)  # slow network work stays outside the lock
    mark_slot = slot_due and bool(status.get("recent_ok"))
    try:
        with locked(path):
            state = State(path)  # re-read inside the lock: poll may have written meanwhile
            new = [f for f in findings if _is_new(state, f)]
            if not new:
                if mark_slot:
                    state.mark(slot, now)  # this slot's check is done even though nothing new was found
                    state.save()
                log.info("새로 알릴 것이 없습니다.")
                return 0
            batch = new[: cfg.max_items_per_digest]
            digest_id = secrets.token_hex(4)
            digest = format_digest(new, now, cfg.max_items_per_digest)
            try:
                sender(digest, token=token, chat_id=chat_id, buttons=inbox.buttons_for(digest_id))
            except telegram.TelegramError as e:
                log.error("전송 실패: %s (다음 점검에서 재시도)", e)
                return 1
            all_keys = [k for f in batch for k in _keys(f)]
            for k in all_keys:
                state.mark(k, now)
            if mark_slot and len(new) <= len(batch):
                state.mark(slot, now)  # leftovers (if any) keep the slot open for the next check
            state.add_digest(digest_id, all_keys, now)
            state.prune(now)
            state.save()
            log.info("%d건 전송 완료", len(batch))
            return 0
    except StateLocked as e:
        log.error("%s 다음 점검에서 다시 시도합니다.", e)
        return 1
