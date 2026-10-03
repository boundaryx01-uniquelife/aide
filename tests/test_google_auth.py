import _bootstrap  # noqa: F401
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aide import google_auth as ga, netutil

CLIENT = {"installed": {"client_id": "cid", "client_secret": "sec",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.dir = Path(self.d.name)
        self.cp = self.dir / "client.json"
        self.tp = self.dir / "token.json"
        self.cp.write_text(json.dumps(CLIENT), encoding="utf-8")

    def tearDown(self):
        self.d.cleanup()


class ClientTests(Base):
    def test_loads(self):
        self.assertEqual(ga.load_client(self.cp)["client_id"], "cid")

    def test_missing_and_bad_files(self):
        with self.assertRaises(ga.GoogleAuthError):
            ga.load_client(self.dir / "nope.json")
        self.cp.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ga.GoogleAuthError):
            ga.load_client(self.cp)
        self.cp.write_text("[]", encoding="utf-8")
        with self.assertRaises(ga.GoogleAuthError):
            ga.load_client(self.cp)

    def test_empty_client_id_rejected(self):
        self.cp.write_text(json.dumps({"installed": {"client_id": ""}}), encoding="utf-8")
        with self.assertRaises(ga.GoogleAuthError):
            ga.load_client(self.cp)

    def test_foreign_hosts_rejected(self):
        for key, url in (("token_uri", "https://evil.example/token"), ("auth_uri", "http://accounts.google.com/x"),
                         ("token_uri", "https://oauth2.googleapis.com.evil.example/token")):
            c = json.loads(json.dumps(CLIENT))
            c["installed"][key] = url
            self.cp.write_text(json.dumps(c), encoding="utf-8")
            with self.assertRaises(ga.GoogleAuthError, msg=url):
                ga.load_client(self.cp)

    def test_defaults_when_uris_absent(self):
        self.cp.write_text(json.dumps({"installed": {"client_id": "x"}}), encoding="utf-8")
        c = ga.load_client(self.cp)
        self.assertIn("accounts.google.com", c["auth_uri"])
        self.assertIn("oauth2.googleapis.com", c["token_uri"])


class UrlTests(Base):
    def test_auth_url_contents(self):
        v, ch = ga._pkce()
        u = ga.build_auth_url(ga.load_client(self.cp), "http://127.0.0.1:5", "ST", ch)
        q = {k: v[0] for k, v in parse_qs(urlparse(u).query).items()}
        self.assertEqual(q["scope"], "https://www.googleapis.com/auth/calendar.readonly")
        self.assertEqual((q["state"], q["code_challenge"], q["code_challenge_method"]), ("ST", ch, "S256"))
        self.assertEqual((q["access_type"], q["prompt"], q["response_type"]), ("offline", "consent", "code"))
        self.assertEqual(q["redirect_uri"], "http://127.0.0.1:5")
        self.assertNotIn("sec", u.replace("scope", ""))  # client secret never in the URL

    def test_pkce_challenge_matches_verifier(self):
        import base64, hashlib
        v, ch = ga._pkce()
        exp = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(ch, exp)
        self.assertNotEqual(ga._pkce()[0], v)

    def test_callback(self):
        self.assertEqual(ga.parse_callback("/?code=abc&state=S", "S"), "abc")
        for bad in ("/?code=abc&state=X", "/?code=abc", "/?state=S", "/?error=access_denied&state=S"):
            with self.assertRaises(ga.GoogleAuthError, msg=bad):
                ga.parse_callback(bad, "S")


class TokenTests(Base):
    def test_exchange_saves_token_and_sends_verifier(self):
        seen = {}

        def post(url, form):
            seen.update(url=url, form=form)
            return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

        ga.exchange_code(ga.load_client(self.cp), "CODE", "http://127.0.0.1:5", "VER", self.tp, post=post, now=lambda: 1000)
        self.assertEqual(seen["url"], CLIENT["installed"]["token_uri"])
        self.assertEqual((seen["form"]["code"], seen["form"]["code_verifier"]), ("CODE", "VER"))
        tok = json.loads(self.tp.read_text())
        self.assertEqual((tok["refresh_token"], tok["access_token"], tok["expires_at"]), ("RT", "AT", 4600))
        self.assertFalse(self.tp.with_name("token.json.tmp").exists())
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(self.tp.stat().st_mode), 0o600)

    def test_exchange_without_refresh_token_fails(self):
        with self.assertRaises(ga.GoogleAuthError):
            ga.exchange_code(ga.load_client(self.cp), "C", "r", "v", self.tp, post=lambda u, f: {"access_token": "A"})
        self.assertFalse(self.tp.exists())

    def write_tok(self, **kw):
        t = {"refresh_token": "RT", "access_token": "OLD", "expires_at": 10_000}
        t.update(kw)
        self.tp.write_text(json.dumps(t), encoding="utf-8")

    def test_valid_token_reused_without_network(self):
        self.write_tok()
        def boom(*a):
            raise AssertionError("no network expected")
        self.assertEqual(ga.access_token(self.cp, self.tp, post=boom, now=lambda: 9_000), "OLD")

    def test_expiring_token_refreshed_and_saved(self):
        self.write_tok()
        def post(url, form):
            self.assertEqual((form["grant_type"], form["refresh_token"]), ("refresh_token", "RT"))
            return {"access_token": "NEW", "expires_in": 100}
        self.assertEqual(ga.access_token(self.cp, self.tp, post=post, now=lambda: 9_950), "NEW")  # <60s left
        tok = json.loads(self.tp.read_text())
        self.assertEqual((tok["access_token"], tok["refresh_token"], tok["expires_at"]), ("NEW", "RT", 10_050))

    def test_revoked_token_gives_login_hint(self):
        self.write_tok(expires_at=0)
        def post(url, form):
            raise netutil.NetError("HTTP 400", status=400)
        with self.assertRaises(ga.GoogleAuthError) as cm:
            ga.access_token(self.cp, self.tp, post=post, now=lambda: 5)
        self.assertIn("google-login", str(cm.exception))

    def test_network_error_is_not_a_relogin_prompt(self):
        self.write_tok(expires_at=0)
        def post(url, form):
            raise netutil.NetError("연결 실패: URLError")
        with self.assertRaises(ga.GoogleAuthError) as cm:
            ga.access_token(self.cp, self.tp, post=post, now=lambda: 5)
        self.assertNotIn("google-login", str(cm.exception))

    def test_missing_or_corrupt_token(self):
        with self.assertRaises(ga.GoogleAuthError):
            ga.access_token(self.cp, self.tp)
        self.tp.write_text("junk", encoding="utf-8")
        with self.assertRaises(ga.GoogleAuthError):
            ga.access_token(self.cp, self.tp)

    def test_errors_never_leak_secrets(self):
        self.write_tok(expires_at=0, refresh_token="SUPERSECRETRT")
        def post(url, form):
            raise netutil.NetError("HTTP 400", status=400)
        try:
            ga.access_token(self.cp, self.tp, post=post, now=lambda: 5)
        except ga.GoogleAuthError as e:
            self.assertNotIn("SUPERSECRETRT", str(e))
            self.assertNotIn("sec", str(e).replace("secret", ""))


class LoginFlowTests(Base):
    def test_full_loopback_login(self):
        import urllib.request
        out = []

        def fake_browser(url):
            q = parse_qs(urlparse(url).query)
            redirect, state = q["redirect_uri"][0], q["state"][0]
            urllib.request.urlopen(f"{redirect}/?code=THECODE&state={state}", timeout=5).read()

        def post(url, form):
            self.assertEqual(form["code"], "THECODE")
            return {"access_token": "A", "refresh_token": "R", "expires_in": 3600}

        ga.login(self.cp, self.tp, open_browser=fake_browser, post=post, say=out.append)
        self.assertEqual(json.loads(self.tp.read_text())["refresh_token"], "R")
        self.assertNotIn("THECODE", "\n".join(out))

    def test_wrong_state_rejected(self):
        import urllib.request

        def fake_browser(url):
            redirect = parse_qs(urlparse(url).query)["redirect_uri"][0]
            urllib.request.urlopen(f"{redirect}/?code=C&state=WRONG", timeout=5).read()

        with self.assertRaises(ga.GoogleAuthError):
            ga.login(self.cp, self.tp, open_browser=fake_browser, post=lambda u, f: {}, say=lambda *_: None)
        self.assertFalse(self.tp.exists())

    def test_timeout(self):
        old = ga.LOGIN_TIMEOUT
        ga.LOGIN_TIMEOUT = 0
        try:
            with self.assertRaises(ga.GoogleAuthError):
                ga.login(self.cp, self.tp, open_browser=lambda u: None, post=lambda u, f: {}, say=lambda *_: None)
        finally:
            ga.LOGIN_TIMEOUT = old


class NetutilTests(unittest.TestCase):
    def test_http_never_attempted(self):
        from unittest import mock
        with mock.patch("urllib.request.build_opener", side_effect=AssertionError("network used")):
            for url in ("http://example.com/x", "httpx://example.com", "HTTP://example.com"):
                with self.assertRaises(netutil.NetError, msg=url):
                    netutil.get_json(url)

    def test_redirects_are_not_followed(self):
        import urllib.request
        h = netutil._NoRedirect()
        req = urllib.request.Request("https://a.example/x", headers={"Authorization": "Bearer X"})
        self.assertIsNone(h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/"))

    def test_https_only(self):
        with self.assertRaises(netutil.NetError):
            netutil.get_json("http://example.com/x")
        with self.assertRaises(netutil.NetError):
            netutil.post_form("ftp://example.com/x", {})


if __name__ == "__main__":
    unittest.main()
