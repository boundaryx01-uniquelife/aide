import _bootstrap  # noqa: F401
import io
import logging
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from aide import google_auth, health, heartbeat, morning, netutil, telegram
from aide.config import Config, ConfigError, load_config
from aide.state import State, locked

NOW = datetime(2026, 11, 7, 8, 0)      # yesterday = 2026-11-06
DAY = datetime(2026, 11, 7, 12, 0)     # outside quiet hours
NIGHT = datetime(2026, 11, 7, 23, 30)
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
HDR = health.EXPIRY_HEADER


def L(day, level, name, body, hms="10:00:00"):
    return f"{day} {hms},123 {level} aide.{name}: {body}"


def Y(level, name, body="x"):
    return L("2026-11-06", level, name, body)


class TempCfg(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def cfg(self, **kw):
        kw.setdefault("state_path", str(self.dir / "state.json"))
        kw.setdefault("health_alerts", True)
        return Config(**kw)


# ---- 1. yesterday's log -------------------------------------------------------------------------

class LogSummaryTests(TempCfg):
    def test_default_off_and_section_empty(self):
        self.assertFalse(Config().health_alerts)
        self.assertEqual(health.section(Config(), NOW, text=Y("ERROR", "news")), [])

    def test_one_error_is_enough_and_counts_by_label(self):
        text = "\n".join([Y("ERROR", "heartbeat"), Y("WARNING", "news"), Y("WARNING", "news"),
                          Y("WARNING", "gcal"), Y("WARNING", "accounts")])
        lines = health.log_lines(self.cfg(), NOW, text=text)
        self.assertEqual(lines, ["오류 1 · 경고 4 (뉴스 피드 2 · 일정 2 · 점검 1)"])

    def test_example_shape(self):
        text = "\n".join([Y("ERROR", "heartbeat")] + [Y("WARNING", "gmail")] * 0
                         + [Y("WARNING", "news")] * 6 + [Y("WARNING", "gcal")] * 2 + [Y("WARNING", "commands")] * 4)
        out = health.log_lines(self.cfg(), NOW, text=text)[0]
        self.assertTrue(out.startswith("오류 1 · 경고 12 ("))
        self.assertIn("뉴스 피드 6", out)
        self.assertIn("일정 2", out)

    def test_quiet_when_only_a_few_ordinary_warnings(self):
        text = "\n".join([Y("WARNING", "news", "연결 실패")] * 9)
        self.assertEqual(health.log_lines(self.cfg(), NOW, text=text), [])

    def test_ten_warnings_is_the_threshold(self):
        self.assertEqual(health.log_lines(self.cfg(), NOW, text="\n".join([Y("WARNING", "news")] * 10)),
                         ["오류 0 · 경고 10 (뉴스 피드 10)"])

    def test_expired_or_permission_warning_triggers_alone(self):
        for word in ("로그인이 만료되었습니다", "이 기능에 필요한 권한이 없습니다"):
            out = health.log_lines(self.cfg(), NOW, text=Y("WARNING", "heartbeat", word))
            self.assertEqual(out, ["오류 0 · 경고 1 (점검 1)"])

    def test_those_words_in_an_info_line_do_not_count(self):
        self.assertEqual(health.log_lines(self.cfg(), NOW, text=L("2026-11-06", "INFO", "heartbeat", "만료 권한")), [])

    def test_date_boundaries(self):
        text = "\n".join([
            L("2026-11-05", "ERROR", "news", "x", "23:59:59"),    # day before yesterday
            L("2026-11-06", "ERROR", "news", "x", "00:00:00"),    # yesterday, first second
            L("2026-11-06", "ERROR", "news", "x", "23:59:59"),    # yesterday, last second
            L("2026-11-07", "ERROR", "news", "x", "00:00:00"),    # today
        ])
        self.assertEqual(health.log_lines(self.cfg(), NOW, text=text), ["오류 2 · 경고 0 (뉴스 피드 2)"])

    def test_yesterday_follows_the_calendar_across_months(self):
        text = L("2026-10-31", "ERROR", "news", "x")
        self.assertEqual(len(health.log_lines(self.cfg(), datetime(2026, 11, 1, 8, 0), text=text)), 1)

    def test_log_text_never_appears(self):
        secret = "sk-ant-SECRET123 chat_id=999 만료 권한 https://evil.example/x"
        text = "\n".join([Y("ERROR", "llm", secret), Y("WARNING", "weird.name", secret)])
        out = "\n".join(health.section(self.cfg(), NOW, text=text))
        for bad in ("SECRET", "chat_id", "evil", "https", "weird"):
            self.assertNotIn(bad, out)
        self.assertIn("AI 1", out)
        self.assertIn("기타 1", out)

    def test_non_log_lines_and_tracebacks_are_ignored(self):
        text = "\n".join(["Traceback (most recent call last):", '  File "x.py", line 1', "ValueError: boom",
                          "■ 알림 2026-11-06 10:00:00,000 ERROR aide.news: nope", "", Y("ERROR", "news")])
        self.assertEqual(health.log_lines(self.cfg(), NOW, text=text), ["오류 1 · 경고 0 (뉴스 피드 1)"])

    def test_labels(self):
        want = {"heartbeat": "점검", "news": "뉴스 피드", "gcal": "일정", "accounts": "일정", "gmail": "메일",
                "gtasks": "할 일", "github": "GitHub", "llm": "AI", "chat": "AI"}
        for name, label in want.items():
            self.assertEqual(health._label(f"aide.{name}"), label)
        self.assertEqual(health._label("aide.unheard"), "기타")
        self.assertEqual(health._label("urllib3"), "기타")

    def test_at_most_five_labels_shown(self):
        names = ["heartbeat", "news", "gcal", "gmail", "gtasks", "github", "llm"]
        out = health.log_lines(self.cfg(), NOW, text="\n".join(Y("ERROR", n) for n in names))[0]
        self.assertTrue(out.endswith("· 그 밖)"))
        self.assertEqual(out.count(" · "), 1 + 5)

    def test_log_path_is_next_to_the_state_file(self):
        self.assertEqual(health.log_path(self.cfg()), self.dir / "aide.log")


class LogFileTests(TempCfg):
    def test_missing_file_means_no_section(self):
        cfg = self.cfg()
        self.assertEqual(health.section(cfg, NOW), [])

    def test_reads_the_file_and_shows_the_section(self):
        cfg = self.cfg()
        (self.dir / "aide.log").write_text(Y("ERROR", "heartbeat") + "\n", encoding="utf-8")
        self.assertEqual(health.section(cfg, NOW),
                         ["■ aide 점검 (어제)", "오류 1 · 경고 0 (점검 1)", ""])

    def test_unreadable_file_is_ignored(self):
        cfg = self.cfg()
        (self.dir / "aide.log").mkdir()           # a directory: open() fails
        self.assertEqual(health.section(cfg, NOW), [])

    def test_invalid_utf8_does_not_break_it(self):
        cfg = self.cfg()
        (self.dir / "aide.log").write_bytes(b"\xff\xfe junk\n" + Y("ERROR", "news").encode() + b"\n")
        self.assertEqual(len(health.section(cfg, NOW)), 3)

    def test_only_the_last_two_megabytes_are_read(self):
        cfg = self.cfg()
        old = [Y("ERROR", "news")] * 50                                        # only in the part that is cut off
        filler = L("2026-11-01", "INFO", "news", "." * 200)
        filler_block = "\n".join([filler] * (health.MAX_LOG_BYTES // len(filler) + 50))
        tail = [Y("WARNING", "gcal", "만료")]
        (self.dir / "aide.log").write_text("\n".join(old) + "\n" + filler_block + "\n" + "\n".join(tail) + "\n",
                                           encoding="utf-8")
        self.assertGreater((self.dir / "aide.log").stat().st_size, health.MAX_LOG_BYTES)
        self.assertEqual(health.section(cfg, NOW), ["■ aide 점검 (어제)", "오류 0 · 경고 1 (일정 1)", ""])

    def test_a_cut_first_line_is_dropped_not_misread(self):
        cfg = self.cfg()
        half = Y("ERROR", "news")
        # the read window starts inside this line: its tail must not be parsed as a log line
        text = half + "\n" + L("2026-11-01", "INFO", "x", "y" * 100) + "\n"
        p = self.dir / "aide.log"
        p.write_text(text, encoding="utf-8")
        self.assertEqual(health.section(cfg, NOW, text=health._read_tail(p, len(text.encode()) - 5)), [])


# ---- 3. GitHub token expiry -----------------------------------------------------------------------

def fake_get(headers=None, raise_=None, calls=None):
    def g(url, headers=None, timeout=15):
        if calls is not None:
            calls.append((url, dict(headers or {})))
        if raise_ is not None:
            raise raise_
        return {"login": "x"}, dict(fake_headers)
    fake_headers = headers or {}
    return g


class ExpiryTests(TempCfg):
    ENV = {"GITHUB_TOKEN_ALICE": "ghp-ALICE-SECRET", "GITHUB_TOKEN_BOB": "ghp-BOB-SECRET"}

    def lines(self, expiry, accounts=("alice",), now=NOW, env=None, **kw):
        get = fake_get({HDR: expiry} if expiry else {}, **kw)
        return health.token_lines(self.cfg(github_accounts=list(accounts)), now, get=get, environ=env or self.ENV)

    def test_parse(self):
        self.assertEqual(health.parse_expiry("2027-11-07 10:00:00 UTC").isoformat(), "2027-11-07T10:00:00+00:00")
        for bad in ("", "soon", "2027-11-07", "2027-13-40 10:00:00 UTC", None):
            self.assertIsNone(health.parse_expiry(bad))

    def test_fourteen_day_boundary(self):
        self.assertEqual(self.lines("2026-11-21 10:00:00 UTC"), ["GitHub 토큰(alice) 만료 D-14"])
        self.assertEqual(self.lines("2026-11-22 10:00:00 UTC"), [])
        self.assertEqual(self.lines("2026-11-07 10:00:00 UTC"), ["GitHub 토큰(alice) 만료 D-0"])

    def test_days_use_the_kst_calendar_date(self):
        # 2026-11-20 20:00 UTC is already 11/21 05:00 in Korea -> 14 days from 11/07
        self.assertEqual(self.lines("2026-11-20 20:00:00 UTC"), ["GitHub 토큰(alice) 만료 D-14"])
        # 2026-11-21 20:00 UTC is 11/22 05:00 KST -> 15 days
        self.assertEqual(self.lines("2026-11-21 20:00:00 UTC"), [])

    def test_already_past_is_not_listed(self):
        self.assertEqual(self.lines("2026-11-01 10:00:00 UTC"), [])

    def test_no_header_means_no_expiry_and_nothing_is_said(self):
        self.assertEqual(self.lines(None), [])
        self.assertEqual(health.github_expiry("alice", get=fake_get({}), environ=self.ENV), ("unlimited", None))

    def test_unparseable_header_is_treated_like_none(self):
        self.assertEqual(self.lines("tomorrow-ish"), [])

    def test_no_token_means_no_call(self):
        calls = []
        out = health.token_lines(self.cfg(github_accounts=["carol"]), NOW, get=fake_get({}, calls=calls),
                                 environ=self.ENV)
        self.assertEqual((out, calls), ([], []))
        self.assertEqual(health.github_expiry("carol", get=fake_get({}), environ={}), ("no-token", None))

    def test_one_call_per_account_with_token_in_header_only(self):
        calls = []
        health.token_lines(self.cfg(github_accounts=["alice", "bob"]), NOW,
                           get=fake_get({HDR: "2026-11-10 00:00:00 UTC"}, calls=calls), environ=self.ENV)
        self.assertEqual([c[0] for c in calls], ["https://api.github.com/user"] * 2)
        self.assertEqual(calls[0][1]["Authorization"], "Bearer ghp-ALICE-SECRET")
        self.assertEqual(calls[1][1]["Authorization"], "Bearer ghp-BOB-SECRET")
        self.assertNotIn("SECRET", calls[0][0])

    def test_network_error_is_skipped_and_the_token_is_never_logged(self):
        with self.assertLogs("aide.health", level="WARNING") as cm:
            out = self.lines("2026-11-10 00:00:00 UTC", raise_=netutil.NetError("HTTP 401", status=401))
        self.assertEqual(out, [])
        self.assertNotIn("SECRET", "\n".join(cm.output))

    def test_each_account_independent(self):
        def get(url, headers=None, timeout=15):
            if "ALICE" in headers["Authorization"]:
                raise netutil.NetError("HTTP 500", status=500)
            return {}, {HDR: "2026-11-09 00:00:00 UTC"}
        with self.assertLogs("aide.health", level="WARNING"):
            out = health.token_lines(self.cfg(github_accounts=["alice", "bob"]), NOW, get=get, environ=self.ENV)
        self.assertEqual(out, ["GitHub 토큰(bob) 만료 D-2"])

    def test_section_combines_log_and_token_lines(self):
        cfg = self.cfg(github_accounts=["alice"])
        out = health.section(cfg, NOW, text=Y("ERROR", "news"), get=fake_get({HDR: "2026-11-09 00:00:00 UTC"}),
                             environ=self.ENV)
        self.assertEqual(out, ["■ aide 점검 (어제)", "오류 1 · 경고 0 (뉴스 피드 1)", "GitHub 토큰(alice) 만료 D-2", ""])

    def test_token_line_alone_makes_the_section(self):
        cfg = self.cfg(github_accounts=["alice"])
        out = health.section(cfg, NOW, text="", get=fake_get({HDR: "2026-11-09 00:00:00 UTC"}), environ=self.ENV)
        self.assertEqual(out, ["■ aide 점검 (어제)", "GitHub 토큰(alice) 만료 D-2", ""])

    def test_off_makes_no_call(self):
        calls = []
        cfg = Config(github_accounts=["alice"])
        self.assertEqual(health.section(cfg, NOW, get=fake_get({}, calls=calls), environ=self.ENV), [])
        self.assertEqual(calls, [])

    def test_section_never_raises(self):
        cfg = self.cfg()
        with mock.patch.object(health, "log_lines", side_effect=RuntimeError("x")), \
                mock.patch.object(health, "token_lines", side_effect=RuntimeError("y")):
            with self.assertLogs("aide.health", level="ERROR"):
                self.assertEqual(health.section(cfg, NOW), [])


class NetutilHeadersTests(unittest.TestCase):
    class Resp:
        def __init__(self, body=b'{"a": 1}', headers=None):
            self._body = body
            self.headers = headers or {"Content-Type": "application/json", "X-Thing": "v"}

        def read(self, n=-1):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(self, resp=None, exc=None):
        o = mock.Mock()
        if exc is not None:
            o.open.side_effect = exc
        else:
            o.open.return_value = resp
        return mock.patch("urllib.request.build_opener", return_value=o)

    def test_headers_are_returned_lower_cased_and_get_json_is_unchanged(self):
        with self.opener(self.Resp()):
            data, headers = netutil.get_json_with_headers("https://api.github.com/user", headers={"A": "b"})
            self.assertEqual(data, {"a": 1})
            self.assertEqual(headers["x-thing"], "v")
            self.assertEqual(netutil.get_json("https://api.github.com/user"), {"a": 1})

    def test_empty_body_gives_empty_dict(self):
        with self.opener(self.Resp(body=b"  ")):
            self.assertEqual(netutil.get_json_with_headers("https://x.example/")[0], {})

    def test_https_only(self):
        with self.assertRaises(netutil.NetError):
            netutil.get_json_with_headers("http://api.github.com/user")

    def test_http_error_does_not_leak_url_or_body(self):
        err = urllib.error.HTTPError("https://api.github.com/user?token=SECRET", 401, "no", {}, io.BytesIO(b"SECRET"))
        with self.opener(exc=err):
            with self.assertRaises(netutil.NetError) as cm:
                netutil.get_json_with_headers("https://api.github.com/user?token=SECRET")
        self.assertEqual(cm.exception.status, 401)
        self.assertNotIn("SECRET", str(cm.exception))

    def test_oversize_and_bad_json_are_errors(self):
        with self.opener(self.Resp(body=b"x" * (netutil.MAX_BYTES + 2))):
            with self.assertRaises(netutil.NetError):
                netutil.get_json_with_headers("https://x.example/")
        with self.opener(self.Resp(body=b"<html>")):
            with self.assertRaises(netutil.NetError):
                netutil.get_json_with_headers("https://x.example/")

    def test_redirects_are_not_followed(self):
        h = netutil._NoRedirect()
        self.assertIsNone(h.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/"))


# ---- 2. Google login failure alert ----------------------------------------------------------------

TWO = [{"name": "개인", "token_path": "data/t1.json"}, {"name": "학교", "token_path": "data/t2.json"}]


class AuthAlertTests(TempCfg):
    def run_check(self, cfg, access, now=DAY, sender=None):
        sent = []

        def send(text, **kw):
            if sender:
                sender(text)
            sent.append(text)

        n = health.check_auth(cfg, now, token="t", chat_id="1", sender=send, access=access)
        return n, sent

    def cfg(self, **kw):
        kw.setdefault("google_accounts", TWO)
        kw.setdefault("calendar_enabled", True)
        return super().cfg(**kw)

    def test_alert_text(self):
        self.assertEqual(health.alert_text("개인"),
                         "⚠ 구글 로그인 확인 필요: 개인 — PC에서 python -m aide google-login --account 개인 후 토큰을 서버로 복사")
        self.assertIn("기본", health.alert_text(""))
        self.assertNotIn("--account", health.alert_text(""))

    def test_failure_alerts_once_per_day_per_account(self):
        cfg = self.cfg()
        calls = []

        def access(client, path, need_scope=None, **kw):
            calls.append((Path(path).name, need_scope))
            if Path(path).name == "t1.json":
                raise google_auth.GoogleAuthError("로그인이 만료되었습니다")
            return "tok"

        n, sent = self.run_check(cfg, access)
        self.assertEqual((n, len(sent)), (1, 1))
        self.assertIn("개인", sent[0])
        self.assertEqual(calls[0][1], google_auth.CAL_SCOPE)
        self.assertTrue(State(cfg.resolved_state_path()).is_seen("authalert:2026-11-07:개인"))
        calls.clear()
        n, sent = self.run_check(cfg, access, now=DAY + timedelta(hours=3))      # same day: no second alert, no call
        self.assertEqual((n, sent), (0, []))
        self.assertNotIn("t1.json", [c[0] for c in calls])
        n, sent = self.run_check(cfg, access, now=DAY + timedelta(days=1))      # next day: again
        self.assertEqual(n, 1)

    def test_both_accounts_each_get_one(self):
        cfg = self.cfg()

        def access(*a, **k):
            raise google_auth.GoogleAuthError("x")

        n, sent = self.run_check(cfg, access)
        self.assertEqual(n, 2)
        self.assertEqual([("개인" in s, "학교" in s) for s in sent], [(True, False), (False, True)])

    def test_send_failure_records_nothing_and_retries(self):
        cfg = self.cfg(google_accounts=TWO[:1])

        def access(*a, **k):
            raise google_auth.GoogleAuthError("x")

        def bad(text):
            raise telegram.TelegramError("전송 실패")

        with self.assertLogs("aide.health", level="ERROR"):
            n, _ = self.run_check(cfg, access, sender=bad)
        self.assertEqual(n, 0)
        self.assertFalse(State(cfg.resolved_state_path()).is_seen("authalert:2026-11-07:개인"))
        n, sent = self.run_check(cfg, access)                  # next heartbeat: sent
        self.assertEqual((n, len(sent)), (1, 1))

    def test_network_error_is_not_an_alert(self):
        cfg = self.cfg(google_accounts=TWO[:1])

        def access(*a, **k):
            raise netutil.NetError("연결 실패: URLError")

        self.assertEqual(self.run_check(cfg, access), (0, []))
        self.assertFalse(State(cfg.resolved_state_path()).is_seen("authalert:2026-11-07:개인"))

    def test_other_exceptions_are_swallowed(self):
        cfg = self.cfg(google_accounts=TWO[:1])

        def access(*a, **k):
            raise RuntimeError("boom")

        with self.assertLogs("aide.health", level="WARNING"):
            self.assertEqual(self.run_check(cfg, access), (0, []))

    def test_healthy_accounts_send_nothing(self):
        self.assertEqual(self.run_check(self.cfg(), lambda *a, **k: "tok"), (0, []))

    def test_off_or_unused_accounts_do_nothing(self):
        def boom(*a, **k):
            raise AssertionError("must not be called")
        self.assertEqual(self.run_check(self.cfg(health_alerts=False), boom), (0, []))
        self.assertEqual(self.run_check(self.cfg(calendar_enabled=False, mail_enabled=False), boom), (0, []))

    def test_state_is_written_under_the_lock_only_after_sending(self):
        cfg = self.cfg(google_accounts=TWO[:1])
        order = []

        def access(*a, **k):
            raise google_auth.GoogleAuthError("x")

        def sender(text, **kw):
            try:
                with locked(cfg.resolved_state_path(), timeout=0.3):     # free: sending happens outside the lock
                    order.append("lock-free")
            except Exception:  # noqa: BLE001
                order.append("locked")
            order.append("sent")

        health.check_auth(cfg, DAY, token="t", chat_id="1", sender=sender, access=access)
        self.assertEqual(order, ["lock-free", "sent"])

    def test_state_lock_failure_does_not_raise(self):
        cfg = self.cfg(google_accounts=TWO[:1])

        def access(*a, **k):
            raise google_auth.GoogleAuthError("x")

        from aide.state import StateLocked
        with mock.patch.object(health, "locked", side_effect=StateLocked("busy")), \
                self.assertLogs("aide.health", level="ERROR"):
            n, _ = self.run_check(cfg, access)
        self.assertEqual(n, 1)


class HeartbeatWiringTests(TempCfg):
    def cfg(self, **kw):
        kw.setdefault("google_accounts", TWO[:1])
        kw.setdefault("calendar_enabled", True)
        return super().cfg(**kw)

    def run_hb(self, cfg, now=DAY, **kw):
        sent = []
        acc = kw.pop("access", None) or mock.Mock(side_effect=google_auth.GoogleAuthError("만료"))
        with mock.patch.dict("os.environ", ENV):
            rc = heartbeat.run(cfg, send=True, now=now, sender=lambda t, **k: sent.append(t), access_token=acc, **kw)
        return rc, sent, acc

    def test_send_run_alerts_about_a_broken_login(self):
        rc, sent, _ = self.run_hb(self.cfg())
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("구글 로그인 확인 필요", sent[0])

    def test_dry_run_does_not_check_or_send(self):
        cfg = self.cfg()
        acc = mock.Mock(side_effect=google_auth.GoogleAuthError("x"))
        with redirect_stdout(io.StringIO()):
            heartbeat.run(cfg, send=False, now=DAY, access_token=acc)
        acc.assert_not_called()

    def test_quiet_hours_do_not_check(self):
        rc, sent, acc = self.run_hb(self.cfg(), now=NIGHT)
        self.assertEqual((rc, sent), (0, []))
        acc.assert_not_called()

    def test_default_off_never_touches_google(self):
        rc, sent, acc = self.run_hb(self.cfg(health_alerts=False))
        self.assertEqual((rc, sent), (0, []))
        acc.assert_not_called()

    def test_alert_does_not_block_the_normal_digest(self):
        cfg = self.cfg(news_feeds=["https://news.example/rss"], news_keywords=["발명"])
        feed = (b'<rss><channel><item><title>\xeb\xb0\x9c\xeb\xaa\x85 \xea\xb5\x90\xec\x9c\xa1</title>'
                b'<link>https://n.example/1</link><guid>1</guid></item></channel></rss>')
        rc, sent, _ = self.run_hb(cfg, fetch=lambda url: feed)
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 2)
        self.assertIn("구글 로그인", sent[0])
        self.assertIn("알림", sent[1])


# ---- morning section --------------------------------------------------------------------------------

class MorningWiringTests(TempCfg):
    def cfg(self, **kw):
        kw.setdefault("watch_repos", [])
        return super().cfg(**kw)

    def test_off_by_default_message_is_unchanged(self):
        with mock.patch.object(health, "section", side_effect=AssertionError("not called")):
            text = morning.build_message(Config(watch_repos=[]), NOW)
        self.assertNotIn("점검", text)

    def test_dry_run_and_send_are_identical_and_the_fetch_is_outside_the_lock(self):
        cfg = self.cfg()
        seen_lock = []

        def fh(c, now):
            try:
                with locked(cfg.resolved_state_path(), timeout=0.3):
                    seen_lock.append(False)
            except Exception:  # noqa: BLE001
                seen_lock.append(True)
            return ["■ aide 점검 (어제)", "오류 1 · 경고 0 (점검 1)", ""]

        preview = morning.build_message(cfg, NOW, fetch_health=fh)
        self.assertIn("■ aide 점검 (어제)\n오류 1 · 경고 0 (점검 1)", preview)
        sent = []
        with mock.patch.dict("os.environ", ENV):
            morning.run(cfg, send=True, now=NOW, sender=lambda m, **k: sent.append(m), fetch_health=fh)
        self.assertEqual(sent[0], preview)
        self.assertEqual(seen_lock, [False, False])

    def test_section_comes_after_github(self):
        cfg = self.cfg(github_accounts=["a"])
        text = morning.build_message(cfg, NOW, fetch_github=lambda c, n: ["■ GitHub (어제 11/06)", "• x", ""],
                                     fetch_health=lambda c, n: ["■ aide 점검 (어제)", "y", ""])
        self.assertLess(text.index("■ GitHub"), text.index("■ aide 점검"))

    def test_exception_drops_only_this_section(self):
        cfg = self.cfg()

        def boom(c, n):
            raise RuntimeError("x")

        with self.assertLogs("aide.morning", level="ERROR"):
            text = morning.build_message(cfg, NOW, fetch_health=boom)
        self.assertTrue(text.startswith("좋은 아침이에요"))
        self.assertNotIn("점검", text)

    def test_real_section_reads_the_log_file(self):
        cfg = self.cfg()
        (self.dir / "aide.log").write_text(Y("ERROR", "heartbeat", "SECRET") + "\n", encoding="utf-8")
        text = morning.build_message(cfg, NOW)
        self.assertIn("오류 1 · 경고 0 (점검 1)", text)
        self.assertNotIn("SECRET", text)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        c = Config()
        self.assertFalse(c.health_alerts)
        self.assertFalse(c.weather_alerts)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"health_alerts": "yes"}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)
            p.write_text('{"health_alerts": true}', encoding="utf-8")
            self.assertTrue(load_config(p).health_alerts)

    def test_example_config_loads_with_both_off(self):
        c = load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        self.assertFalse(c.health_alerts)
        self.assertFalse(c.weather_alerts)


if __name__ == "__main__":
    unittest.main()
