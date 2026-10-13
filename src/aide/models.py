from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: str, limit: int = 200) -> str:
    """Strip control characters and cap length. All external text (news titles,
    feed summaries, ...) is untrusted and must pass through here before it is
    shown to the user or, in later stages, handed to an LLM."""
    text = _CONTROL.sub("", text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class Finding:
    """One thing worth telling the user about.

    key      stable id used for de-duplication (same key is never reported twice)
    source   "git" | "news" | ... (used to group the digest)
    title    one-line summary
    detail   optional extra line
    url      optional link
    priority 0 low, 1 normal (default), 2 high (AI judgment), 3 pinned by the owner's
             own rule. Display order only -- never affects de-duplication. 2 is set by
             stage 2a's LLM ranking (llm.py), 3 by the owner's rules (rules.py);
             everything else leaves it at the default, so sorting by priority is a
             no-op when those features are off or fail (stable sort keeps the order).
    account  which Google account this came from ("" when there is only one, or the
             finding has no account at all). Used to exclude an account from stage 2a.
    alt_keys extra ids for the SAME item (e.g. a normalized headline). The item counts as
             already seen if ANY of key/alt_keys was reported; reporting marks all of them.
    rule_key "mail:<lowercase address>" when this is mail from a well-formed sender
             address, else "" -- what the owner's rules (rules.py) are keyed by.
    label    sender display name (cleaned), for buttons and the folded-mail line.
    bulk     True when the headers say bulk/list mail (List-Unsubscribe, List-Id or
             Precedence present). Only the PRESENCE is recorded, never the values.
    """

    key: str
    source: str
    title: str
    detail: str = ""
    url: str = ""
    priority: int = 1
    account: str = ""
    alt_keys: Tuple[str, ...] = ()
    rule_key: str = ""
    label: str = ""
    bulk: bool = False
