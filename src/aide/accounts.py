"""Read the calendar / mail of several Google accounts and merge the results.

With a single account nothing is labelled and behaviour is exactly as before. With more than one,
each line carries the account name, an event present in two accounts is shown once, and one
account failing never hides the others.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Callable, List, Tuple
from urllib.parse import quote

from . import google_auth, netutil
from .config import Account, Config
from .gcal import Event
from .models import Finding

log = logging.getLogger("aide.accounts")


def labelled(cfg: Config) -> bool:
    return len(cfg.accounts()) > 1


def gather_events(cfg: Config, call: Callable[[str], List[Event]], key) -> Tuple[List[Event], List[str], int]:
    """Run `call(token)` for every account. Returns (events sorted by `key`, failed account names,
    number of accounts). Events are labelled "[name] title" when there are several accounts; an
    identical event (same day, time and title) in two accounts is merged: "[개인·학교] title"."""
    accts = cfg.accounts()
    many = len(accts) > 1
    entries: List[list] = []   # [event, [account names]] in arrival order
    first_of: dict = {}        # (day, when, title) -> first entry with that key
    failed: List[str] = []
    for acct in accts:
        try:
            token = google_auth.access_token(cfg.resolved_google_client(), acct.token_path)
            events = call(token)
        except (google_auth.GoogleAuthError, netutil.NetError) as e:
            log.warning("일정 조회 실패%s: %s", f" ({acct.name})" if many else "", e)
            failed.append(acct.name or "계정")
            continue
        for e in events:
            k = (e.day, e.when, e.title)
            hit = first_of.get(k)
            if many and hit is not None and acct.name not in hit[1]:
                hit[1].append(acct.name)       # same event seen in another account
                continue
            entry = [e, [acct.name]]
            entries.append(entry)
            first_of.setdefault(k, entry)
    out = [replace(e, title=f"[{'·'.join(names)}] {e.title}") if many else e for e, names in entries]
    return sorted(out, key=key), failed, len(accts)


def failure_lines(failed: List[str], total: int) -> List[str]:
    """Message lines for failed accounts. All failed (or the only one) -> the classic single line."""
    if not failed:
        return []
    if len(failed) == total:
        return ["불러오지 못했어요 (로그 확인)."]
    return [f"불러오지 못한 계정: {', '.join(failed)} (로그 확인)"]


def tag_mail(found: List[Finding], acct: Account, idx: int, many: bool) -> List[Finding]:
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
