import _bootstrap  # noqa: F401
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from aide import gmail, google_auth as ga, heartbeat, netutil
from aide.config import Config

NOW = datetime(2026, 10, 4, 14, 0)
IDS = ["aaaa1111bbbb2222", "cccc3333dddd4444", "eeee5555ffff6666"]


def make_get(msgs, listing=None, calls=None):
    def get(url, headers=None):
        if calls is not None:
            calls.append((url, headers))
        u = urlparse(url)
        if u.path.endswith("/messages"):
            return {"messages": [{"id": i} for i in (listing if listing is not None else list(msgs))]}
        mid = u.path.rsplit("/", 1)[-1]
        h = msgs[mid]
        return {"id": mid, "payload": {"headers": [{"name": k, "value": v} for k, v in h.items()]}}
    return get


class SenderTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(gmail.valid_senders(["PEN.go.kr ", "a@b.kr"]), ["pen.go.kr", "a@b.kr"])
        for bad in ("", "pen", "a b.kr", "x.kr) OR (from:", "x.kr OR y.kr", "a@b", "-from:x.kr", "x.kr\nfoo"):
            with self.assertRaises(gmail.MailConfigError, msg=bad):
                gmail.valid_senders([bad])

    def test_matching(self):
        a = gmail._allowed
        self.assertTrue(a("x@pen.go.kr", ["pen.go.kr"]))
        self.assertTrue(a("x@mail.pen.go.kr", ["pen.go.kr"]))
        self.assertFalse(a("x@evilpen.go.kr", ["pen.go.kr"]))
        self.assertFalse(a("x@pen.go.kr.evil.com", ["pen.go.kr"]))
        self.assertTrue(a("Me@Site.kr", ["me@site.kr"]))
        self.assertFalse(a("other@site.kr", ["me@site.kr"]))
        self.assertFalse(a("", ["pen.go.kr"]))
        self.assertFalse(a("nodomain", ["pen.go.kr"]))

    def test_query(self):
        q = gmail.query_for(["pen.go.kr", "a@b.kr"])
        self.assertEqual(q, "is:unread in:inbox newer_than:2d from:(pen.go.kr OR a@b.kr)")


class FetchTests(unittest.TestCase):
    def test_headers_only_and_findings(self):
        msgs = {IDS[0]: {"From": "교육청 <noti@pen.go.kr>", "Subject": "연수 안내\x00 드립니다"},
                IDS[1]: {"From": "noti@mail.pen.go.kr", "Subject": ""}}
        calls = []
        out = gmail.unread_from("TOK", ["pen.go.kr"], 5, get=make_get(msgs, calls=calls))
        self.assertEqual([f.title for f in out], ["교육청: 연수 안내 드립니다", "noti@mail.pen.go.kr: (제목 없음)"])
        self.assertEqual(out[0].key, f"mail:{IDS[0]}")
        self.assertEqual(out[0].source, "mail")
        self.assertTrue(out[0].url.endswith(IDS[0]))
        for url, headers in calls:
            self.assertEqual(headers, {"Authorization": "Bearer TOK"})
            q = parse_qs(urlparse(url).query)
            if "format" in q:
                self.assertEqual(q["format"], ["metadata"])         # never full/raw -> no body
                self.assertNotIn("snippet", url)
        self.assertIn("from%3A%28pen.go.kr%29", calls[0][0])

    def test_list_request_uses_query_and_limit(self):
        calls = []
        gmail.unread_from("T", ["pen.go.kr"], 3, get=make_get({}, listing=[], calls=calls))
        q = parse_qs(urlparse(calls[0][0]).query)
        self.assertEqual(q["maxResults"], ["6"])
        self.assertIn("is:unread", q["q"][0])

    def test_non_allowed_result_is_dropped(self):
        msgs = {IDS[0]: {"From": "x@evil.com", "Subject": "공지 pen.go.kr"},
                IDS[1]: {"From": "a@pen.go.kr", "Subject": "진짜"}}
        out = gmail.unread_from("T", ["pen.go.kr"], 5, get=make_get(msgs))
        self.assertEqual([f.title.split(": ")[-1] for f in out], ["진짜"])

    def test_limit_and_bad_ids(self):
        msgs = {i: {"From": "a@pen.go.kr", "Subject": f"s{n}"} for n, i in enumerate(IDS)}
        out = gmail.unread_from("T", ["pen.go.kr"], 2, get=make_get(msgs))
        self.assertEqual(len(out), 2)
        out = gmail.unread_from("T", ["pen.go.kr"], 5, get=make_get(msgs, listing=["../../x", "ZZ", IDS[0]]))
        self.assertEqual(len(out), 1)

    def test_invalid_sender_in_fetch_raises_before_any_request(self):
        def boom(*a, **k):
            raise AssertionError("no request expected")
        with self.assertRaises(gmail.MailConfigError):
            gmail.unread_from("T", ["x.kr) OR (from:y.kr"], 5, get=boom)

    def test_empty_senders_makes_no_request(self):
        def boom(*a, **k):
            raise AssertionError("no request expected")
        self.assertEqual(gmail.unread_from("T", [], 5, get=boom), [])

    def test_odd_payloads(self):
        self.assertEqual(gmail.unread_from("T", ["pen.go.kr"], 5, get=lambda *a, **k: []), [])
        self.assertEqual(gmail.unread_from("T", ["pen.go.kr"], 5, get=lambda *a, **k: {"messages": None}), [])

    def test_long_and_hostile_text(self):
        msgs = {IDS[0]: {"From": "\x00" + "가" * 200 + " <a@pen.go.kr>", "Subject": "나" * 500}}
        t = gmail.unread_from("T", ["pen.go.kr"], 5, get=make_get(msgs))[0].title
        self.assertLessEqual(len(t), 40 + 2 + 120)
        self.assertNotIn("\x00", t)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.dir = Path(self.d.name)
        self.cp, self.tp = self.dir / "c.json", self.dir / "t.json"
        self.cp.write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}), encoding="utf-8")

    def tearDown(self):
        self.d.cleanup()

    def tok(self, **kw):
        t = {"refresh_token": "R", "access_token": "A", "expires_at": 10_000}
        t.update(kw)
        self.tp.write_text(json.dumps(t), encoding="utf-8")

    def get(self, need):
        return ga.access_token(self.cp, self.tp, post=lambda *a: {}, now=lambda: 1, need_scope=need)

    def test_old_calendar_only_login_cannot_read_mail(self):
        self.tok()  # no "scope" field -> calendar only
        self.assertEqual(self.get(ga.CAL_SCOPE), "A")
        with self.assertRaises(ga.GoogleAuthError) as cm:
            self.get(ga.GMAIL_SCOPE)
        self.assertIn("google-login", str(cm.exception))

    def test_new_login_scope_allows_mail(self):
        self.tok(scope=ga.SCOPE)
        self.assertEqual(self.get(ga.GMAIL_SCOPE), "A")
        self.assertEqual(self.get(ga.CAL_SCOPE), "A")

    def test_exchange_stores_scope(self):
        ga.exchange_code(ga.load_client(self.cp), "C", "r", "v", self.tp,
                         post=lambda u, f: {"access_token": "A", "refresh_token": "R", "scope": ga.SCOPE})
        self.assertEqual(json.loads(self.tp.read_text())["scope"], ga.SCOPE)


class HeartbeatMailTests(unittest.TestCase):
    def cfg(self, **kw):
        return Config(**kw)

    def test_off_by_default(self):
        def boom(*a, **k):
            raise AssertionError("must not run")
        self.assertEqual(heartbeat.collect(self.cfg(), NOW, lambda u: b"", mail_fetch=boom), [])
        self.assertEqual(heartbeat.collect(self.cfg(mail_enabled=True), NOW, lambda u: b"", mail_fetch=boom), [])  # no senders

    def test_senders_without_enable_flag_do_nothing(self):
        def boom(*a, **k):
            raise AssertionError("must not run")
        self.assertEqual(heartbeat.collect(self.cfg(mail_enabled=False, mail_senders=["pen.go.kr"]), NOW,
                                           lambda u: b"", mail_fetch=boom), [])

    def test_enabled_collects_with_config_values(self):
        seen = {}

        def mf(token, senders, n):
            seen.update(token=token, senders=senders, n=n)
            return [gmail.Finding(key="mail:aaaa1111", source="mail", title="x: y")]
        with mock.patch.object(ga, "access_token", return_value="T") as at:
            out = heartbeat.collect(self.cfg(mail_enabled=True, mail_senders=["pen.go.kr"], mail_max_items=3),
                                    NOW, lambda u: b"", mail_fetch=mf)
        self.assertEqual(seen, {"token": "T", "senders": ["pen.go.kr"], "n": 3})
        self.assertEqual(at.call_args.kwargs["need_scope"], ga.GMAIL_SCOPE)
        self.assertEqual([f.source for f in out], ["mail"])

    def test_failures_never_block_other_checks(self):
        for exc in (ga.GoogleAuthError("x"), netutil.NetError("y"), gmail.MailConfigError("z")):
            with mock.patch.object(ga, "access_token", side_effect=exc):
                out = heartbeat.collect(self.cfg(mail_enabled=True, mail_senders=["pen.go.kr"]), NOW, lambda u: b"")
            self.assertEqual(out, [])

    def test_digest_shows_mail_first_with_label(self):
        from aide.models import Finding
        fs = [Finding("g", "git", "작업 폴더 알림"), Finding("n", "notice", "공모 안내"),
              Finding("m", "mail", "교육청: 연수")]
        text = heartbeat.format_digest(fs, NOW, 10)
        self.assertLess(text.index("메일 (허용 발신자)"), text.index("기관 공지·마감"))
        self.assertLess(text.index("기관 공지·마감"), text.index("작업 폴더\n"))


if __name__ == "__main__":
    unittest.main()
