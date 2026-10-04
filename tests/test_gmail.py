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
        for exc in (ga.GoogleAuthError("x"), netutil.NetError("y")):
            with mock.patch.object(ga, "access_token", side_effect=exc):
                out = heartbeat.collect(self.cfg(mail_enabled=True, mail_senders=["pen.go.kr"]), NOW, lambda u: b"")
            self.assertEqual(out, [])
        def bad(*a, **k):
            raise gmail.MailConfigError("z")
        with mock.patch.object(ga, "access_token", return_value="T"):
            out = heartbeat.collect(self.cfg(mail_enabled=True, mail_senders=["pen.go.kr"]), NOW, lambda u: b"", mail_fetch=bad)
        self.assertEqual(out, [])

    def test_digest_shows_mail_first_with_label(self):
        from aide.models import Finding
        fs = [Finding("g", "git", "작업 폴더 알림"), Finding("n", "notice", "공모 안내"),
              Finding("m", "mail", "교육청: 연수")]
        text = heartbeat.format_digest(fs, NOW, 10)
        self.assertLess(text.index("메일"), text.index("기관 공지·마감"))
        self.assertLess(text.index("기관 공지·마감"), text.index("작업 폴더\n"))


class RecentMailTests(unittest.TestCase):
    def msgs(self, n, frm="a@x.kr"):
        ids = [f"{i:016x}" for i in range(1, n + 1)]
        return ids, {i: {"From": f"사람{k} <{frm}>", "Subject": f"제목{k}"} for k, i in enumerate(ids)}

    def test_query_primary_unread_window_and_blocklist(self):
        ids, m = self.msgs(1)
        calls = []
        gmail.recent_unread("T", 5, 12, ["spam.kr", "a@b.kr"], NOW, get=make_get(m, listing=ids, calls=calls))
        q = parse_qs(urlparse(calls[0][0]).query)["q"][0]
        since = int(NOW.timestamp()) - 12 * 3600
        self.assertEqual(q, f"is:unread in:inbox category:primary after:{since} -from:(spam.kr OR a@b.kr)")
        calls2 = []
        gmail.recent_unread("T", 5, 12, [], NOW, get=make_get(m, listing=ids, calls=calls2))
        self.assertNotIn("-from", parse_qs(urlparse(calls2[0][0]).query)["q"][0])

    def test_limit_and_more_marker(self):
        ids, m = self.msgs(8)
        out = gmail.recent_unread("T", 5, 12, [], NOW, get=make_get(m, listing=ids))
        self.assertEqual(len(out), 5)
        self.assertEqual(out[-1].detail, "…이 외에 3건 더 (Gmail 에서 확인)")
        self.assertTrue(all(f.detail == "" for f in out[:-1]))
        ids, m = self.msgs(5)
        self.assertEqual(gmail.recent_unread("T", 5, 12, [], NOW, get=make_get(m, listing=ids))[-1].detail, "")

    def test_many_more_says_ten_or_more(self):
        ids, m = self.msgs(16)
        out = gmail.recent_unread("T", 5, 12, [], NOW, get=make_get(m, listing=ids))
        self.assertIn("10건 이상", out[-1].detail)

    def test_list_request_size_leaves_room_for_more_marker(self):
        ids, m = self.msgs(1)
        calls = []
        gmail.recent_unread("T", 5, 12, [], NOW, get=make_get(m, listing=ids, calls=calls))
        self.assertEqual(parse_qs(urlparse(calls[0][0]).query)["maxResults"], ["15"])

    def test_blocked_sender_dropped_locally_and_invalid_block_rejected(self):
        ids, m = self.msgs(2)
        m[ids[0]]["From"] = "x@spam.kr"
        out = gmail.recent_unread("T", 5, 12, ["spam.kr"], NOW, get=make_get(m, listing=ids))
        self.assertEqual(len(out), 1)
        with self.assertRaises(gmail.MailConfigError):
            gmail.recent_unread("T", 5, 12, ["x) OR (y"], NOW, get=make_get(m, listing=ids))

    def test_empty_result_has_no_marker(self):
        self.assertEqual(gmail.recent_unread("T", 5, 12, [], NOW, get=make_get({}, listing=[])), [])


class SlotTests(unittest.TestCase):
    def cfg(self, hours=(8, 18), **kw):
        return Config(mail_enabled=True, mail_recent_hours=list(hours), **kw)

    def test_slot_selection(self):
        slot = lambda h, m=0: heartbeat.mail_slot(self.cfg(), datetime(2026, 10, 4, h, m))
        self.assertIsNone(slot(7, 59))
        self.assertEqual(slot(8), "mailslot:2026-10-04:08")
        self.assertEqual(slot(17, 59), "mailslot:2026-10-04:08")
        self.assertEqual(slot(18), "mailslot:2026-10-04:18")
        self.assertEqual(slot(23), "mailslot:2026-10-04:18")
        self.assertEqual(heartbeat.mail_slot(self.cfg(hours=(18, 8)), datetime(2026, 10, 4, 9)), "mailslot:2026-10-04:08")
        self.assertIsNone(heartbeat.mail_slot(self.cfg(hours=()), datetime(2026, 10, 4, 9)))

    def test_collect_only_asks_for_recent_when_due(self):
        calls = []
        rf = lambda *a: calls.append(a) or []
        cfg = self.cfg()
        with mock.patch.object(ga, "access_token", return_value="T"):
            heartbeat.collect(cfg, NOW, lambda u: b"", include_recent=False, recent_fetch=rf)
            self.assertEqual(calls, [])
            st = {}
            heartbeat.collect(cfg, NOW, lambda u: b"", include_recent=True, recent_fetch=rf, mail_status=st)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:4], (5, 12, []))
        self.assertTrue(st["recent_ok"])

    def test_no_hours_configured_means_no_recent_check_even_if_asked(self):
        def boom(*a):
            raise AssertionError("must not run")
        with mock.patch.object(ga, "access_token", return_value="T"):
            heartbeat.collect(self.cfg(hours=()), NOW, lambda u: b"", include_recent=True, recent_fetch=boom,
                              mail_fetch=lambda *a: [])

    def test_recent_failure_leaves_status_unset(self):
        def bad(*a):
            raise netutil.NetError("x")
        st = {}
        with mock.patch.object(ga, "access_token", return_value="T"):
            heartbeat.collect(self.cfg(), NOW, lambda u: b"", include_recent=True, recent_fetch=bad, mail_status=st)
        self.assertNotIn("recent_ok", st)

    def test_duplicate_mail_from_both_modes_is_collapsed(self):
        from aide.models import Finding
        f = Finding("mail:aaaa1111", "mail", "x: y")
        with mock.patch.object(ga, "access_token", return_value="T"):
            out = heartbeat.collect(self.cfg(mail_senders=["x.kr"]), NOW, lambda u: b"",
                                    mail_fetch=lambda *a: [f], recent_fetch=lambda *a: [f], include_recent=True)
        self.assertEqual(len(out), 1)


class SlotRunTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.cfg = Config(mail_enabled=True, mail_recent_hours=[8, 18], state_path=str(Path(self.d.name) / "s.json"),
                          max_items_per_digest=3)
        self.sent = []
        self.recent_calls = 0
        self.items = []
        self.env = mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"})
        self.env.start()
        self.tok = mock.patch.object(ga, "access_token", return_value="T")
        self.tok.start()

    def tearDown(self):
        self.tok.stop()
        self.env.stop()
        self.d.cleanup()

    def run_at(self, hour, minute=0):
        from aide.models import Finding

        def rf(*a):
            self.recent_calls += 1
            return [Finding(f"mail:{i:016x}", "mail", f"s: {i}") for i in self.items]
        with mock.patch.object(gmail, "recent_unread", rf):
            return heartbeat.run(self.cfg, send=True, now=datetime(2026, 10, 4, hour, minute),
                                 sender=lambda text, **k: self.sent.append(text), fetch=lambda u: b"")

    def test_slot_checked_once_even_when_empty(self):
        self.run_at(8, 0)
        self.run_at(8, 30)
        self.run_at(9, 0)
        self.assertEqual(self.recent_calls, 1)
        self.run_at(18, 0)
        self.assertEqual(self.recent_calls, 2)

    def test_sends_once_and_marks_slot(self):
        self.items = [1, 2]
        self.run_at(8, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("s: 1", self.sent[0])
        self.items = [1, 2, 3]
        self.run_at(8, 30)                       # same slot: no new check, nothing sent
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.recent_calls, 1)

    def test_before_first_slot_no_mail_check(self):
        self.run_at(7, 0)
        self.assertEqual(self.recent_calls, 0)

    def test_leftovers_keep_slot_open(self):
        self.items = [1, 2, 3, 4, 5]             # batch limit is 3
        self.run_at(8, 0)
        self.assertEqual(self.recent_calls, 1)
        self.run_at(8, 30)                       # slot still open -> remaining items go out
        self.assertEqual(self.recent_calls, 2)
        self.assertIn("s: 4", self.sent[1])
        self.run_at(9, 0)                        # now complete
        self.assertEqual(self.recent_calls, 2)

    def test_failed_check_does_not_use_up_the_slot(self):
        def bad(*a):
            raise netutil.NetError("down")
        with mock.patch.object(gmail, "recent_unread", bad):
            heartbeat.run(self.cfg, send=True, now=datetime(2026, 10, 4, 8, 0),
                          sender=lambda *a, **k: None, fetch=lambda u: b"")
        self.items = [1]
        self.run_at(8, 30)
        self.assertEqual(self.recent_calls, 1)
        self.assertEqual(len(self.sent), 1)

    def test_preview_ignores_slot_and_changes_no_state(self):
        from aide.models import Finding
        with mock.patch.object(gmail, "recent_unread", lambda *a: [Finding("mail:aaaa1111", "mail", "s: x")]):
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                heartbeat.run(self.cfg, send=False, now=datetime(2026, 10, 4, 7, 0), force=True, fetch=lambda u: b"")
        self.assertIn("s: x", buf.getvalue())
        self.assertFalse(Path(self.cfg.state_path).exists())


class ConfigHoursTests(unittest.TestCase):
    def test_validation(self):
        from aide.config import ConfigError, load_config
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            for bad in ([24], [-1], ["8"], [True], [8.5]):
                p.write_text(json.dumps({"mail_recent_hours": bad}), encoding="utf-8")
                with self.assertRaises(ConfigError, msg=str(bad)):
                    load_config(p)
            p.write_text(json.dumps({"mail_recent_hours": [8, 18]}), encoding="utf-8")
            self.assertEqual(load_config(p).mail_recent_hours, [8, 18])


if __name__ == "__main__":
    unittest.main()
