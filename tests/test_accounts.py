import _bootstrap  # noqa: F401
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from aide import accounts, google_auth, heartbeat, netutil
from aide.__main__ import main
from aide.config import Config, ConfigError, load_config
from aide.gcal import Event
from aide.models import Finding

A = {"name": "개인", "token_path": "data/google_token.json", "email": "me@gmail.com"}
B = {"name": "학교", "token_path": "data/google_token_school.json", "email": "t@school.com"}
DAY = datetime(2026, 10, 2, 12, 0)
D = date(2026, 10, 2)


def cfg2(**kw):
    return Config(google_accounts=[A, B], **kw)


def fake_token(client, path, need_scope=None):
    return Path(path).name


class ConfigTests(unittest.TestCase):
    def write(self, data):
        d = tempfile.mkdtemp()
        p = Path(d) / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_no_accounts_means_the_classic_single_token(self):
        cfg = Config()
        self.assertEqual([a.name for a in cfg.accounts()], [""])
        self.assertEqual(cfg.accounts()[0].token_path, cfg.resolved_google_token())

    def test_accounts_resolved_in_order(self):
        self.assertEqual([a.name for a in cfg2().accounts()], ["개인", "학교"])

    def test_bad_configs_rejected(self):
        bad = [
            [{"name": "a"}],                                              # no token_path
            [{"token_path": "x"}],                                        # no name
            [A, dict(B, name="개인")],                                     # duplicate name
            [A, dict(B, token_path=A["token_path"])],                     # duplicate path
            [dict(A, email="nope")],                                      # bad email
            [dict(A, extra=1)],                                           # unknown key
            [dict(A, name="x" * 21)],                                     # long name
            [dict(A, name="a\nb")],                                       # multi-line name
            [dict(A, name=f"n{i}", token_path=f"t{i}") for i in range(9)],  # too many
            "oops",
        ]
        for accts in bad:
            with self.subTest(accts=str(accts)[:50]):
                with self.assertRaises(ConfigError):
                    load_config(self.write({"google_accounts": accts}))

    def test_valid_config_loads(self):
        cfg = load_config(self.write({"google_accounts": [A, B]}))
        self.assertEqual(len(cfg.accounts()), 2)


class GatherEventsTests(unittest.TestCase):
    key = staticmethod(lambda e: (e.when != "종일", e.when))

    def run_gather(self, cfg, per_token):
        def call(token):
            r = per_token[token]
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(google_auth, "access_token", fake_token):
            return accounts.gather_events(cfg, call, self.key)

    def test_single_account_unlabelled(self):
        ev = [Event("09:00", "회의", D)]
        out, failed, total = self.run_gather(Config(), {"google_token.json": ev})
        self.assertEqual([e.title for e in out], ["회의"])
        self.assertEqual((failed, total), ([], 1))

    def test_labels_and_sort_across_accounts(self):
        out, failed, total = self.run_gather(cfg2(), {
            "google_token.json": [Event("14:00", "수업", D)],
            "google_token_school.json": [Event("09:00", "회의", D)],
        })
        self.assertEqual([e.title for e in out], ["[학교] 회의", "[개인] 수업"])
        self.assertEqual(total, 2)

    def test_same_event_in_both_is_merged(self):
        ev = lambda: [Event("09:00", "회의", D)]
        out, _, _ = self.run_gather(cfg2(), {"google_token.json": ev(), "google_token_school.json": ev()})
        self.assertEqual([e.title for e in out], ["[개인·학교] 회의"])

    def test_same_title_different_day_not_merged(self):
        out, _, _ = self.run_gather(cfg2(), {
            "google_token.json": [Event("09:00", "회의", D)],
            "google_token_school.json": [Event("09:00", "회의", date(2026, 10, 3))],
        })
        self.assertEqual(len(out), 2)

    def test_duplicates_inside_one_account_are_kept(self):
        out, _, _ = self.run_gather(cfg2(), {
            "google_token.json": [Event("09:00", "회의", D), Event("09:00", "회의", D)],
            "google_token_school.json": [],
        })
        self.assertEqual(len(out), 2)

    def test_one_failing_account_does_not_hide_the_other(self):
        out, failed, total = self.run_gather(cfg2(), {
            "google_token.json": [Event("09:00", "회의", D)],
            "google_token_school.json": netutil.NetError("x"),
        })
        self.assertEqual([e.title for e in out], ["[개인] 회의"])
        self.assertEqual((failed, total), (["학교"], 2))

    def test_login_failure_counts_as_failed(self):
        with mock.patch.object(google_auth, "access_token", side_effect=google_auth.GoogleAuthError("no")):
            out, failed, total = accounts.gather_events(cfg2(), lambda t: [], self.key)
        self.assertEqual((out, failed, total), ([], ["개인", "학교"], 2))

    def test_failure_lines(self):
        self.assertEqual(accounts.failure_lines([], 2), [])
        self.assertEqual(accounts.failure_lines(["학교"], 2), ["불러오지 못한 계정: 학교 (로그 확인)"])
        self.assertEqual(accounts.failure_lines(["개인", "학교"], 2), ["불러오지 못했어요 (로그 확인)."])
        self.assertEqual(accounts.failure_lines([""], 1), ["불러오지 못했어요 (로그 확인)."])


class HeartbeatMailTests(unittest.TestCase):
    def mail(self, token, senders, n):
        return [Finding(key="mail:1", source="mail", title="공문", url="https://mail.google.com/mail/u/0/#all/1")]

    def collect(self, cfg, mail_fetch=None, status=None, recent_fetch=None, token=fake_token, include_recent=False):
        with mock.patch.object(google_auth, "access_token", token):
            return heartbeat.collect(cfg, DAY, fetch=lambda u: b"<rss/>", mail_fetch=mail_fetch or self.mail,
                                     include_recent=include_recent, mail_status=status, recent_fetch=recent_fetch)

    def base(self, **kw):
        return cfg2(mail_enabled=True, mail_senders=["a@b.kr"], **kw)

    def test_single_account_unchanged(self):
        out = self.collect(Config(mail_enabled=True, mail_senders=["a@b.kr"]))
        self.assertEqual([(f.key, f.title, f.url) for f in out],
                         [("mail:1", "공문", "https://mail.google.com/mail/u/0/#all/1")])

    def test_two_accounts_keys_titles_links(self):
        out = self.collect(self.base())
        self.assertEqual([f.key for f in out], ["mail:1", "mail:1:학교"])
        self.assertEqual([f.title for f in out], ["[개인] 공문", "[학교] 공문"])
        self.assertIn("authuser=me%40gmail.com#all/1", out[0].url)
        self.assertIn("authuser=t%40school.com#all/1", out[1].url)

    def test_no_email_second_account_has_no_link_first_keeps_it(self):
        cfg = Config(google_accounts=[{"name": "개인", "token_path": "a"}, {"name": "학교", "token_path": "b"}],
                     mail_enabled=True, mail_senders=["a@b.kr"])
        out = self.collect(cfg)
        self.assertIn("/mail/u/0/#all/1", out[0].url)
        self.assertEqual(out[1].url, "")

    def test_failed_account_is_skipped_others_still_read(self):
        def tok(client, path, need_scope=None):
            if "school" in str(path):
                raise google_auth.GoogleAuthError("expired")
            return "t"
        out = self.collect(self.base(), token=tok)
        self.assertEqual([f.key for f in out], ["mail:1"])

    def test_recent_ok_only_if_every_account_read(self):
        recent = lambda t, *a: []
        st = {}
        self.collect(self.base(mail_recent_hours=[8]), status=st, recent_fetch=recent, include_recent=True)
        self.assertTrue(st.get("recent_ok"))

        def tok(client, path, need_scope=None):
            if "school" in str(path):
                raise google_auth.GoogleAuthError("expired")
            return "t"
        st = {}
        self.collect(self.base(mail_recent_hours=[8]), status=st, recent_fetch=recent, include_recent=True, token=tok)
        self.assertFalse(st.get("recent_ok"))

        def bad_recent(t, *a):
            if t == "google_token_school.json":
                raise netutil.NetError("x")
            return []
        st = {}
        self.collect(self.base(mail_recent_hours=[8]), status=st, recent_fetch=bad_recent, include_recent=True)
        self.assertFalse(st.get("recent_ok"))


class CliTests(unittest.TestCase):
    def cfg_path(self, accts):
        d = tempfile.mkdtemp()
        p = Path(d) / "config.json"
        p.write_text(json.dumps({"google_accounts": accts, "state_path": str(Path(d) / "s.json")}), encoding="utf-8")
        return str(p)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_login_needs_account_when_several(self):
        with mock.patch.object(google_auth, "login") as lg:
            code, _, err = self.run_main("google-login", "--config", self.cfg_path([A, B]))
        self.assertEqual(code, 2)
        self.assertIn("개인", err)
        lg.assert_not_called()

    def test_login_unknown_account(self):
        with mock.patch.object(google_auth, "login") as lg:
            code, _, err = self.run_main("google-login", "--account", "없음", "--config", self.cfg_path([A, B]))
        self.assertEqual(code, 2)
        self.assertIn("학교", err)
        lg.assert_not_called()

    def test_login_picks_that_accounts_token(self):
        with mock.patch.object(google_auth, "login") as lg:
            code, _, _ = self.run_main("google-login", "--account", "학교", "--config", self.cfg_path([A, B]))
        self.assertEqual(code, 0)
        self.assertEqual(Path(lg.call_args[0][1]).name, "google_token_school.json")

    def test_login_single_account_needs_no_flag(self):
        with mock.patch.object(google_auth, "login") as lg:
            code, _, _ = self.run_main("google-login", "--config", self.cfg_path([]))
        self.assertEqual(code, 0)
        lg.assert_called_once()

    def test_selfcheck_lists_accounts_without_secrets(self):
        code, out, _ = self.run_main("selfcheck", "--config", self.cfg_path([A, B]))
        self.assertEqual(code, 0)
        self.assertIn("개인", out)
        self.assertIn("학교", out)


if __name__ == "__main__":
    unittest.main()
