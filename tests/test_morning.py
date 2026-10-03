import _bootstrap  # noqa: F401
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from aide import morning, telegram
from aide.checks import yesterday
from aide.config import Config
from aide.state import State

TODAY = datetime(2026, 10, 3, 8, 0)   # Saturday, outside quiet hours
EARLY = datetime(2026, 10, 3, 6, 0)   # inside 22:30-07:00
YDAY = date(2026, 10, 2)
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}


def commit(repo: Path, msg: str, when: str):
    env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    (repo / f"{abs(hash(msg))}.txt").write_text(msg, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg],
        check=True, env=env,
    )


class YesterdayTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.dir.name) / "proj"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        commit(self.repo, "old work", "2026-10-01T12:00:00")
        commit(self.repo, "yesterday A", "2026-10-02T09:00:00")
        commit(self.repo, "yesterday B", "2026-10-02T23:30:00")
        commit(self.repo, "today work", "2026-10-03T07:30:00")

    def tearDown(self):
        self.dir.cleanup()

    def test_only_that_day(self):
        self.assertEqual(sorted(yesterday.commits_on(self.repo, YDAY)), ["yesterday A", "yesterday B"])

    def test_empty_day_is_empty_list_not_none(self):
        self.assertEqual(yesterday.commits_on(self.repo, date(2026, 9, 1)), [])

    def test_not_a_repo_is_none(self):
        plain = Path(self.dir.name) / "plain"
        plain.mkdir()
        self.assertIsNone(yesterday.commits_on(plain, YDAY))


class MorningTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = Path(self.dir.name)
        self.repo = base / "proj"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        commit(self.repo, "fix login bug", "2026-10-02T10:00:00")
        self.state_path = base / "state.json"
        self.cfg = Config(watch_repos=[str(self.repo)], git_dirty_threshold=5, state_path=str(self.state_path))
        self.sent = []

    def tearDown(self):
        self.dir.cleanup()

    def sender(self, text, *, token, chat_id):
        self.sent.append(text)

    def go(self, **kw):
        kw.setdefault("now", TODAY)
        kw.setdefault("sender", self.sender)
        with mock.patch.dict(os.environ, ENV):
            return morning.run(self.cfg, **kw)

    def test_message_content(self):
        msg = morning.build_message(self.cfg, TODAY)
        self.assertIn("10/03(토)", msg)
        self.assertIn("proj: 커밋 1개", msg)
        self.assertIn("fix login bug", msg)

    def test_no_commits_says_so(self):
        self.cfg.watch_repos = []
        self.assertIn("커밋 기록이 없어요", morning.build_message(self.cfg, TODAY))

    def test_many_commits_are_capped(self):
        for i in range(8):
            commit(self.repo, f"c{i}", "2026-10-02T11:00:00")
        msg = morning.build_message(self.cfg, TODAY)
        self.assertIn("커밋 9개", msg)
        self.assertIn("외 4개", msg)
        self.assertEqual(sum(1 for ln in msg.splitlines() if ln.startswith("  - ")), 5)

    def test_pending_changes_listed_only_over_threshold(self):
        for i in range(6):
            (self.repo / f"u{i}.txt").write_text("x", encoding="utf-8")
        self.assertIn("커밋 안 된 변경 6개", morning.build_message(self.cfg, TODAY))
        self.cfg.git_dirty_threshold = 100
        self.assertNotIn("이어서 할 일", morning.build_message(self.cfg, TODAY))

    def test_dry_run_prints_and_changes_nothing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(self.go(send=False), 0)
        self.assertIn("좋은 아침", buf.getvalue())
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state_path.exists())

    def test_once_per_day(self):
        self.assertEqual(self.go(send=True), 0)
        self.assertEqual(self.go(send=True), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.go(send=True, now=datetime(2026, 10, 4, 8, 0)), 0)  # next day sends again
        self.assertEqual(len(self.sent), 2)

    def test_failed_send_is_retried(self):
        def boom(text, *, token, chat_id):
            raise telegram.TelegramError("down")

        self.assertEqual(self.go(send=True, sender=boom), 1)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.go(send=True), 0)
        self.assertEqual(len(self.sent), 1)

    def test_quiet_hours_skip_without_marking_force_overrides(self):
        self.assertEqual(self.go(send=True, now=EARLY), 0)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.go(send=True, now=EARLY, force=True), 0)
        self.assertEqual(len(self.sent), 1)

    def test_send_without_credentials_refused(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            self.assertEqual(morning.run(self.cfg, send=True, now=TODAY, sender=self.sender), 2)

    def test_morning_mark_does_not_disturb_heartbeat_state(self):
        st = State(self.state_path)
        st.mark("news:abc", TODAY)
        st.add_digest("abcd1234", ["news:abc"], TODAY)
        st.offset = 7
        st.save()
        self.go(send=True)
        st = State(self.state_path)
        self.assertTrue(st.is_seen("news:abc"))
        self.assertIn("abcd1234", st.digests)
        self.assertEqual(st.offset, 7)


if __name__ == "__main__":
    unittest.main()
