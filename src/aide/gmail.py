"""Gmail, READ-ONLY, headers only.

Never fetches message bodies or snippets. Only mail from an explicit sender allowlist is
ever listed: the search query is built from validated entries, and every result's From
address is re-checked locally. Sender name and subject are untrusted text -> clean().
"""
from __future__ import annotations

import re
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


def unread_from(token: str, senders: List[str], max_items: int = 5, get: Callable = netutil.get_json) -> List[Finding]:
    senders = valid_senders(senders)
    if not senders:
        return []
    hdr = {"Authorization": f"Bearer {token}"}
    q = urlencode({"q": query_for(senders), "maxResults": max_items * 2, "fields": "messages(id)"})
    data = get(f"{BASE}?{q}", headers=hdr)
    ids = [m.get("id", "") for m in (data.get("messages") or []) if isinstance(m, dict)] if isinstance(data, dict) else []
    out: List[Finding] = []
    for mid in ids:
        if not _ID.match(mid) or len(out) >= max_items:
            continue
        mq = urlencode([("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"),
                        ("fields", "id,payload/headers")])
        msg = get(f"{BASE}/{mid}?{mq}", headers=hdr)
        heads = {h.get("name", "").lower(): h.get("value", "")
                 for h in ((msg.get("payload") or {}).get("headers") or []) if isinstance(h, dict)} if isinstance(msg, dict) else {}
        name, addr = parseaddr(heads.get("from", ""))
        if not _allowed(addr, senders):
            continue  # defence in depth: never show mail the allowlist does not cover
        who = clean(name, 40) or clean(addr, 60)
        subject = clean(heads.get("subject", ""), 120) or "(제목 없음)"
        out.append(Finding(key=f"mail:{mid}", source="mail", title=f"{who}: {subject}",
                           url=f"https://mail.google.com/mail/u/0/#all/{mid}"))
    return out
