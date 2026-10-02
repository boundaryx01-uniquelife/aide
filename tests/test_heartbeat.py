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

    def ok_sender(self, text, *, token, chat_id):
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

    def test_failed_send_is_retried_and_not_marked(self):
        def boom(text, *, token, chat_id):
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


if __name__ == "__main__":
    unittest.main()
