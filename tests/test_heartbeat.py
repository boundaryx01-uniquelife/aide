import _bootstrap  # noqa: F401
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from aide import heartbeat, telegram
from aide.config import Config
from aide.state import State

DAY = datetime(2026, 10, 2, 12, 0)      # outside quiet hours
NIGHT = datetime(2026, 10, 2, 23, 30)   # inside 22:30-07:00
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}


class QuietHourTests(unittest.TestCase):
    def test_overnight_window(self):
        q = lambda h, m: heartbeat.in_quiet_hours(datetime(2026, 1, 1, h, m), "22:30", "07:00")
        self.assertTrue(q(23, 0))
        self.assertTrue(q(0, 0))
        self.assertTrue(q(6, 59))
        self.assertFalse(q(7, 0))
        self.assertFalse(q(12, 0))
        self.assertFalse(q(22, 29))
        self.assertTrue(q(22, 30))

    def test_same_day_window_and_empty_window(self):
        self.assertTrue(heartbeat.in_quiet_hours(datetime(2026, 1, 1, 13, 0), "12:00", "14:00"))
        self.assertFalse(heartbeat.in_quiet_hours(datetime(2026, 1, 1, 15, 0), "12:00", "14:00"))
        self.assertFalse(heartbeat.in_quiet_hours(datetime(2026, 1, 1, 3, 0), "08:00", "08:00"))


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.repo = base / "proj"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        for i in range(6):
            (self.repo / f"f{i}.txt").write_text("x", encoding="utf-8")
        self.state_path = base / "state.json"
        self.cfg = Config(watch_repos=[str(self.repo)], git_dirty_threshold=5, state_path=str(self.state_path))
        self.sent = []

    def tearDown(self):
        self.dir.cleanup()

    def ok_sender(self, text, *, token, chat_id, buttons=None):
        self.sent.append(text)

    def run_hb(self, **kw):
        kw.setdefault("now", DAY)
        kw.setdefault("sender", self.ok_sender)
        kw.setdefault("fetch", lambda url: b"<rss/>")
        with mock.patch.dict(os.environ, ENV):
            return heartbeat.run(self.cfg, **kw)

    def test_dry_run_prints_and_changes_nothing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(self.run_hb(send=False), 0)
            self.assertEqual(self.run_hb(send=False), 0)  # repeatable
        self.assertIn("커밋 안 된 변경 6개", buf.getvalue())
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state_path.exists())

    def test_send_then_dedup(self):
        self.assertEqual(self.run_hb(send=True), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(State(self.state_path).seen)
        self.assertEqual(self.run_hb(send=True), 0)
        self.assertEqual(len(self.sent), 1)  # same finding is not sent again

    def test_send_attaches_buttons_and_registers_digest(self):
        got = {}

        def spy(text, *, token, chat_id, buttons=None):
            got["buttons"] = buttons

        self.assertEqual(self.run_hb(send=True, sender=spy), 0)
        st = State(self.state_path)
        self.assertEqual(len(st.digests), 1)
        did, rec = next(iter(st.digests.items()))
        self.assertEqual(rec["status"], "pending")
        self.assertTrue(set(rec["keys"]) <= set(st.seen))
        self.assertTrue(all(did in d for _, d in got["buttons"][0]))

    def test_failed_send_registers_no_digest(self):
        def boom(text, *, token, chat_id, buttons=None):
            raise telegram.TelegramError("down")

        self.run_hb(send=True, sender=boom)
        self.assertFalse(self.state_path.exists())

    def test_failed_send_is_retried_and_not_marked(self):
        def boom(text, *, token, chat_id, buttons=None):
            raise telegram.TelegramError("down")

        self.assertEqual(self.run_hb(send=True, sender=boom), 1)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.run_hb(send=True), 0)  # next heartbeat succeeds
        self.assertEqual(len(self.sent), 1)

    def test_quiet_hours_send_nothing_and_mark_nothing(self):
        self.assertEqual(self.run_hb(send=True, now=NIGHT), 0)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.run_hb(send=True, now=NIGHT, force=True), 0)  # --force overrides
        self.assertEqual(len(self.sent), 1)

    def test_send_without_credentials_is_refused(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            code = heartbeat.run(self.cfg, send=True, now=DAY, sender=self.ok_sender)
        self.assertEqual(code, 2)
        self.assertEqual(self.sent, [])

    def test_nothing_new_is_silent(self):
        self.cfg.git_dirty_threshold = 100
        self.assertEqual(self.run_hb(send=True), 0)
        self.assertEqual(self.sent, [])


class DigestTests(unittest.TestCase):
    def test_limit_and_remainder_note(self):
        from aide.models import Finding

        fs = [Finding(key=f"k{i}", source="news", title=f"t{i}") for i in range(20)]
        text = heartbeat.format_digest(fs, DAY, 15)
        self.assertIn("알림 20건", text)
        self.assertIn("t14", text)
        self.assertNotIn("t15", text)
        self.assertIn("외 5건", text)

    def test_default_priority_is_unaffected_by_llm_sections(self):
        """Every finding at the default priority (1) -- the common case, feature off
        or ranking failed -- must render byte-for-byte like before stage 2a."""
        from aide.models import Finding

        fs = [Finding(key="a", source="mail", title="메일"), Finding(key="b", source="notice", title="공지")]
        text = heartbeat.format_digest(fs, DAY, 15)
        self.assertNotIn("중요 (AI 판단)", text)
        self.assertEqual(text.index("메일") < text.index("공지"), True)

    def test_high_priority_pulled_into_its_own_section_regardless_of_source(self):
        from aide.models import Finding

        fs = [
            Finding(key="a", source="mail", title="평범한 메일", priority=1),
            Finding(key="b", source="notice", title="중요 공지", priority=2),
        ]
        text = heartbeat.format_digest(fs, DAY, 15)
        self.assertLess(text.index("중요 (AI 판단)"), text.index("중요 공지"))
        self.assertLess(text.index("중요 공지"), text.index("평범한 메일"))
        self.assertIn("[기관 공지·마감] 중요 공지", text)

    def test_low_priority_sinks_to_the_bottom_of_its_own_section(self):
        from aide.models import Finding

        fs = [
            Finding(key="a", source="mail", title="낮음", priority=0),
            Finding(key="b", source="mail", title="보통", priority=1),
        ]
        text = heartbeat.format_digest(fs, DAY, 15)
        self.assertLess(text.index("보통"), text.index("낮음"))


class LlmHeartbeatTests(unittest.TestCase):
    """Stage 2a wired into heartbeat.run(): off by default, never called on a dry
    run (cost safety), and its budget/record-keeping only happens under the lock."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.dir.name) / "state.json"
        self.cfg = Config(news_feeds=["http://x"], news_keywords=["키워드"], state_path=str(self.state_path))
        self.sent = []

    def tearDown(self):
        self.dir.cleanup()

    def ok_sender(self, text, *, token, chat_id, buttons=None):
        self.sent.append(text)

    @staticmethod
    def feed(title="키워드 알림", link="http://x/1"):
        return f"<rss><channel><item><title>{title}</title><link>{link}</link></item></channel></rss>".encode("utf-8")

    def run_hb(self, **kw):
        kw.setdefault("now", DAY)
        kw.setdefault("sender", self.ok_sender)
        kw.setdefault("fetch", lambda url: self.feed())
        env = dict(ENV, ANTHROPIC_API_KEY=kw.pop("api_key", "key123"))
        with mock.patch.dict(os.environ, env):
            return heartbeat.run(self.cfg, **kw)

    def must_not_be_called(self, *a, **k):
        raise AssertionError("llm.rank must not be called")

    def test_disabled_by_default_never_calls_llm(self):
        with mock.patch.object(heartbeat.llm, "rank", side_effect=self.must_not_be_called):
            self.assertEqual(self.run_hb(send=True), 0)
        self.assertEqual(len(self.sent), 1)

    def test_dry_run_never_calls_llm_even_when_enabled(self):
        self.cfg.llm_enabled = True
        buf = io.StringIO()
        with mock.patch.object(heartbeat.llm, "rank", side_effect=self.must_not_be_called), redirect_stdout(buf):
            self.assertEqual(self.run_hb(send=False), 0)

    def test_enabled_send_calls_llm_with_api_key_and_applies_priority(self):
        self.cfg.llm_enabled = True
        seen_kwargs = {}

        def fake_rank(findings, cfg, *, now, calls_today=0, api_key="", post=None, call_status=None):
            seen_kwargs["api_key"] = api_key
            seen_kwargs["calls_today"] = calls_today
            if call_status is not None:
                call_status["attempted"] = True
            return ["high"] + ["normal"] * (len(findings) - 1)

        with mock.patch.object(heartbeat.llm, "rank", side_effect=fake_rank):
            self.assertEqual(self.run_hb(send=True, api_key="the-key"), 0)
        self.assertEqual(seen_kwargs["api_key"], "the-key")
        self.assertEqual(seen_kwargs["calls_today"], 0)
        self.assertIn("중요 (AI 판단)", self.sent[0])
        self.assertEqual(State(self.state_path).llm_calls_today(DAY), 1)  # a billed attempt was recorded

    def test_rank_returning_none_leaves_digest_unchanged_but_still_bills_an_attempt(self):
        self.cfg.llm_enabled = True

        def fake_rank(findings, cfg, *, now, calls_today=0, api_key="", post=None, call_status=None):
            if call_status is not None:
                call_status["attempted"] = True  # got a response, just failed local validation
            return None

        with mock.patch.object(heartbeat.llm, "rank", side_effect=fake_rank):
            self.assertEqual(self.run_hb(send=True), 0)
        self.assertNotIn("중요 (AI 판단)", self.sent[0])
        self.assertEqual(State(self.state_path).llm_calls_today(DAY), 1)

    def test_network_failure_inside_rank_does_not_spend_budget(self):
        self.cfg.llm_enabled = True

        def fake_rank(findings, cfg, *, now, calls_today=0, api_key="", post=None, call_status=None):
            return None  # call_status left untouched, same as a NetError inside the real rank()

        with mock.patch.object(heartbeat.llm, "rank", side_effect=fake_rank):
            self.assertEqual(self.run_hb(send=True), 0)
        self.assertEqual(State(self.state_path).llm_calls_today(DAY), 0)

    def test_second_run_sees_the_previous_calls_today(self):
        self.cfg.llm_enabled = True
        seen = []

        def fake_rank(findings, cfg, *, now, calls_today=0, api_key="", post=None, call_status=None):
            seen.append(calls_today)
            if call_status is not None:
                call_status["attempted"] = True
            return None

        with mock.patch.object(heartbeat.llm, "rank", side_effect=fake_rank):
            self.run_hb(send=True, now=DAY)
            self.run_hb(send=True, now=DAY.replace(hour=13), fetch=lambda url: self.feed("다른 알림", "http://x/2"))
        self.assertEqual(seen, [0, 1])


if __name__ == "__main__":
    unittest.main()
