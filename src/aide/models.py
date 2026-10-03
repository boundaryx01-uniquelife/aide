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
    priority 1 normal, 2 high (reserved for later stages)
    alt_keys extra ids for the SAME item (e.g. a normalized headline). The item counts as
             already seen if ANY of key/alt_keys was reported; reporting marks all of them.
    """

    key: str
    source: str
    title: str
    detail: str = ""
    url: str = ""
    priority: int = 1
    alt_keys: Tuple[str, ...] = ()
