"""Tiny HTTPS JSON helpers. Errors never include the URL or the response body
(URLs may carry tokens; bodies may carry secrets)."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

MAX_BYTES = 1_000_000


class NetError(Exception):
    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a redirect could leak the Authorization header
        return None


def _open(req: urllib.request.Request, timeout: float) -> Any:
    if not req.full_url.lower().startswith("https://"):
        raise NetError("https 주소만 허용됩니다.")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES + 1)
            ctype = (resp.headers.get("Content-Type") or "?").split(";")[0].strip()[:40]
    except urllib.error.HTTPError as e:
        raise NetError(f"HTTP {e.code}", status=e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NetError(f"연결 실패: {type(e).__name__}") from None
    if len(raw) > MAX_BYTES:
        raise NetError("응답이 너무 큽니다.")
    if not raw.strip():
        return {}  # an empty 200 body means "nothing to list"
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # size and content type only: the body itself may hold secrets
        raise NetError(f"응답을 해석할 수 없습니다 (길이 {len(raw)}, {ctype}).") from None


def get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 15) -> Any:
    return _open(urllib.request.Request(url, headers=headers or {}, method="GET"), timeout)


def post_form(url: str, data: Dict[str, str], timeout: float = 15) -> Any:
    body = urllib.parse.urlencode(data).encode("utf-8")
    return _open(urllib.request.Request(url, data=body, method="POST"), timeout)


def post_json(url: str, payload: Any, headers: Optional[Dict[str, str]] = None, timeout: float = 15) -> Any:
    """POST a JSON body (e.g. the Anthropic Messages API). Same safety properties as
    `_open`: https only, no redirect, response capped, errors never leak the body."""
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    return _open(urllib.request.Request(url, data=body, headers=hdrs, method="POST"), timeout)
