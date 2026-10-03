"""Google OAuth (installed-app flow, loopback + PKCE), READ-ONLY calendar scope.

The refresh token lives in data/google_token.json (git-ignored). Secrets are never
printed or logged. `login` is run by the user by hand; everything else is silent.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from . import netutil

SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
AUTH_HOST, TOKEN_HOST = "accounts.google.com", "oauth2.googleapis.com"
LOGIN_TIMEOUT = 180


class GoogleAuthError(Exception):
    pass


def _check_host(url: str, host: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or u.hostname != host:
        raise GoogleAuthError("클라이언트 파일의 주소가 Google 이 아닙니다.")


def load_client(path: Path) -> Dict[str, str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GoogleAuthError(f"클라이언트 파일이 없습니다: {path}") from None
    except (ValueError, OSError):
        raise GoogleAuthError("클라이언트 파일을 읽을 수 없습니다.") from None
    block = data.get("installed") or data.get("web") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        raise GoogleAuthError("클라이언트 파일 형식이 올바르지 않습니다 (Desktop app 유형이어야 함).")
    out = {k: str(block.get(k, "")) for k in ("client_id", "client_secret", "auth_uri", "token_uri")}
    out["auth_uri"] = out["auth_uri"] or f"https://{AUTH_HOST}/o/oauth2/v2/auth"
    out["token_uri"] = out["token_uri"] or f"https://{TOKEN_HOST}/token"
    if not out["client_id"]:
        raise GoogleAuthError("클라이언트 파일에 client_id 가 없습니다.")
    _check_host(out["auth_uri"], AUTH_HOST)
    _check_host(out["token_uri"], TOKEN_HOST)
    return out


def _pkce() -> tuple:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client: Dict[str, str], redirect_uri: str, state: str, challenge: str) -> str:
    q = {
        "client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent",
    }
    return f"{client['auth_uri']}?{urlencode(q)}"


def parse_callback(path: str, expected_state: str) -> str:
    q = parse_qs(urlparse(path).query)
    if q.get("state", [""])[0] != expected_state:
        raise GoogleAuthError("state 가 일치하지 않습니다 (요청 거부).")
    if "error" in q:
        raise GoogleAuthError("Google 로그인이 취소되었거나 거부되었습니다.")
    code = q.get("code", [""])[0]
    if not code:
        raise GoogleAuthError("인증 코드가 없습니다.")
    return code


def _save_token(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _token_request(client, form, post):
    try:
        return post(client["token_uri"], form)
    except netutil.NetError as e:
        if e.status in (400, 401):
            raise GoogleAuthError("로그인이 만료되었습니다. `python -m aide google-login` 을 다시 실행하세요.") from None
        raise GoogleAuthError(f"Google 연결 실패: {e}") from None


def exchange_code(client, code, redirect_uri, verifier, token_path, post=netutil.post_form, now=time.time) -> None:
    form = {
        "client_id": client["client_id"], "client_secret": client["client_secret"], "code": code,
        "code_verifier": verifier, "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }
    r = _token_request(client, form, post)
    if not isinstance(r, dict) or not r.get("refresh_token") or not r.get("access_token"):
        raise GoogleAuthError("토큰 응답에 refresh_token 이 없습니다 (다시 로그인하세요).")
    _save_token(token_path, {
        "refresh_token": r["refresh_token"], "access_token": r["access_token"],
        "expires_at": now() + int(r.get("expires_in", 3600)),
    })


class _Handler(BaseHTTPRequestHandler):
    result: Optional[str] = None
    error: Optional[str] = None
    expected_state = ""

    def do_GET(self):  # noqa: N802
        cls = type(self)
        if urlparse(self.path).path != "/":
            self.send_response(404); self.end_headers(); return
        try:
            cls.result = parse_callback(self.path, cls.expected_state)
            body = "로그인 완료. 이 창을 닫아도 됩니다."
        except GoogleAuthError as e:
            cls.error = str(e)
            body = "로그인 실패. 터미널 메시지를 확인하세요."
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # never log the request line (contains the auth code)
        pass


def login(client_path, token_path, open_browser=webbrowser.open, post=netutil.post_form, say=print) -> None:
    client = load_client(client_path)
    state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce()
    handler = type("H", (_Handler,), {"result": None, "error": None, "expected_state": state})
    server = HTTPServer(("127.0.0.1", 0), handler)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = build_auth_url(client, redirect, state, challenge)
    say("브라우저에서 Google 로그인 창이 열립니다. 열리지 않으면 아래 주소를 복사해 여세요:")
    say(url)
    def _open():
        try:
            open_browser(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()  # never block the server loop
    deadline = time.time() + LOGIN_TIMEOUT
    try:
        while time.time() < deadline and handler.result is None and handler.error is None:
            server.handle_request()
    finally:
        server.server_close()
    if handler.error:
        raise GoogleAuthError(handler.error)
    if handler.result is None:
        raise GoogleAuthError("시간 초과: 로그인이 완료되지 않았습니다.")
    exchange_code(client, handler.result, redirect, verifier, token_path, post=post)
    say("로그인 완료. 읽기 전용 권한만 저장되었습니다.")


def access_token(client_path, token_path, post=netutil.post_form, now=time.time) -> str:
    try:
        tok = json.loads(Path(token_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GoogleAuthError("Google 로그인이 안 되어 있습니다 (`python -m aide google-login`).") from None
    except (ValueError, OSError):
        raise GoogleAuthError("토큰 파일을 읽을 수 없습니다.") from None
    if tok.get("access_token") and float(tok.get("expires_at", 0)) - now() > 60:
        return tok["access_token"]
    client = load_client(client_path)
    r = _token_request(client, {
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "refresh_token": tok.get("refresh_token", ""), "grant_type": "refresh_token",
    }, post)
    if not isinstance(r, dict) or not r.get("access_token"):
        raise GoogleAuthError("토큰 갱신 응답이 올바르지 않습니다.")
    tok["access_token"] = r["access_token"]
    tok["expires_at"] = now() + int(r.get("expires_in", 3600))
    _save_token(token_path, tok)
    return tok["access_token"]
