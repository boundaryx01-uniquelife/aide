"""Gmail, READ-ONLY, headers only.

Never fetches message bodies or snippets. Only mail from an explicit sender allowlist is
ever listed: the search query is built from validated entries, and every result's From
address is re-checked locally. Sender name and subject are untrusted text -> clean().
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Callable, Iterable, List
from urllib.parse import urlencode

from . import netutil
from .models import Finding, clean

BASE = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
_ENTRY = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$|^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_ID = re.compile(r"^[0-9a-f]{8,32}$")
NEWER_THAN = "2d"


class MailConfigError(Exception):
    pass


def valid_senders(senders: Iterable[str]) -> List[str]:
    out = []
    for s in senders:
        s = s.strip().lower()
        if not _ENTRY.match(s):
            raise MailConfigError(f"허용 발신자 형식이 올바르지 않습니다: {clean(s, 40)!r} (예: pen.go.kr 또는 a@b.kr)")
        out.append(s)
    return out


def _allowed(addr: str, senders: List[str]) -> bool:
    addr = addr.strip().lower()
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    for s in senders:
        if "@" in s:
            if addr == s:
                return True
        elif domain == s or domain.endswith("." + s):
            return True
    return False


def query_for(senders: List[str]) -> str:
    who = " OR ".join(senders)
    return f"is:unread in:inbox newer_than:{NEWER_THAN} from:({who})"


def _headers(token: str, mid: str, get: Callable):
    mq = urlencode([("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"),
                    ("fields", "id,payload/headers")])
    msg = get(f"{BASE}/{mid}?{mq}", headers={"Authorization": f"Bearer {token}"})
    heads = {h.get("name", "").lower(): h.get("value", "")
             for h in ((msg.get("payload") or {}).get("headers") or []) if isinstance(h, dict)} if isinstance(msg, dict) else {}
    name, addr = parseaddr(heads.get("from", ""))
    return name, addr, heads.get("subject", "")


def _finding(mid: str, name: str, addr: str, subject: str) -> Finding:
    who = clean(name, 40) or clean(addr, 60)
    return Finding(key=f"mail:{mid}", source="mail", title=f"{who}: {clean(subject, 120) or '(제목 없음)'}",
                   url=f"https://mail.google.com/mail/u/0/#all/{mid}")


def _list_ids(token: str, query: str, limit: int, get: Callable) -> List[str]:
    q = urlencode({"q": query, "maxResults": limit, "fields": "messages(id)"})
    data = get(f"{BASE}?{q}", headers={"Authorization": f"Bearer {token}"})
    ids = [m.get("id", "") for m in (data.get("messages") or []) if isinstance(m, dict)] if isinstance(data, dict) else []
    return [i for i in ids if _ID.match(i)]


def unread_from(token: str, senders: List[str], max_items: int = 5, get: Callable = netutil.get_json) -> List[Finding]:
    senders = valid_senders(senders)
    if not senders:
        return []
    out: List[Finding] = []
    for mid in _list_ids(token, query_for(senders), max_items * 2, get):
        if len(out) >= max_items:
            break
        name, addr, subject = _headers(token, mid, get)
        if not _allowed(addr, senders):
            continue  # defence in depth: never show mail the allowlist does not cover
        out.append(_finding(mid, name, addr, subject))
    return out


def recent_query(blocked: List[str], since_epoch: int) -> str:
    q = f"is:unread in:inbox category:primary after:{since_epoch}"
    return q + (f" -from:({' OR '.join(blocked)})" if blocked else "")


def recent_unread(token: str, max_items: int, window_hours: int, blocked: Iterable[str], now: datetime,
                  get: Callable = netutil.get_json) -> List[Finding]:
    """Newest unread Primary-tab mail from the last `window_hours`, minus a block list.
    Headers only. If more mail exists than `max_items`, the last item says so."""
    blocked = valid_senders(blocked)
    since = int((now - timedelta(hours=window_hours)).timestamp())
    ids = _list_ids(token, recent_query(blocked, since), max_items + 10, get)
    out: List[Finding] = []
    for mid in ids[:max_items]:
        name, addr, subject = _headers(token, mid, get)
        if blocked and _allowed(addr, blocked):
            continue
        out.append(_finding(mid, name, addr, subject))
    extra = len(ids) - max_items
    if out and extra > 0:
        last = out[-1]
        more = "10건 이상" if extra >= 10 else f"{extra}건"
        out[-1] = Finding(last.key, last.source, last.title, f"…이 외에 {more} 더 (Gmail 에서 확인)", last.url)
    return out
