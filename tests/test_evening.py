import _bootstrap  # noqa: F401
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from aide import evening, gcal, google_auth as ga, netutil, telegram
from aide.checks import git_dirty, notice_pages, yesterday
from aide.config import Config, ConfigError, load_config
from aide.state import State

NOW = datetime(2026, 10, 4, 20, 5)   # Sunday
PAGE = "https://example.go.kr/list"
HTML = """<table>
<tr><td><a href="/1">공모 사업 신청 안내 (곧 마감)</a></td><td>2026-09-30 ~ 2026-10-06</td></tr>
<tr><td><a href="/2">연수 안내 공고 (내일 마감)</a></td><td>2026-10-01 ~ 2026-10-05</td></tr>
<tr><td><a href="/3">공모 사업 안내 (여유 있음)</a></td><td>2026-10-01 ~ 2026-10-30</td></tr>
<tr><td><a href="/4">공모 마감 지난 사업 안내</a></td><td>2026-09-01 ~ 2026-10-01</td></tr>
<tr><td><a href="/5">공모 마감일 없는 안내 글</a></td><td>2026-10-03</td></tr>
</table>"""
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}


def fetch(url):
    return HTML.encode()


class UpcomingTests(unittest.TestCase):
    def test_only_within_window_sorted(self):
        rows = notice_pages.upcoming([PAGE], ["공모", "연수"], fetch, NOW, 3)
        self.assertEqual([(r[0][:8], r[2]) for r in rows], [("연수 안내 공고", 1), ("공모 사업 신청", 2)])
        self.assertEqual(rows[0][1].isoformat(), "2026-10-05")
        self.assertEqual(rows[0][3], "https://example.go.kr/2")

    def test_window_edges(self):
        self.assertEqual(len(notice_pages.upcoming([PAGE], ["공모", "연수"], fetch, NOW, 1)), 1)
        self.assertEqual(len(notice_pages.upcoming([PAGE], ["공모", "연수"], fetch, NOW, 2)), 2)
        self.assertEqual(len(notice_pages.upcoming([PAGE], ["공모", "연수"], fetch, NOW, 30)), 3)

    def test_not_limited_by_per_page_cap_of_check(self):
        rows = "".join(f'<tr><td><a href="/{i}">공모 사업 안내 번호 {i}</a></td><td>2026-10-01 ~ 2026-10-05</td></tr>' for i in range(12))
        got = notice_pages.upcoming([PAGE], ["공모"], lambda u: f"<table>{rows}</table>".encode(), NOW, 3)
        self.assertEqual(len(got), 12)


class BuildTests(unittest.TestCase):
    def cfg(self, **kw):
        return Config(watch_repos=["C:/x/proj-a", "C:/x/proj-b"], **kw)

    def build(self, cfg, commits=None, dirty=None, events=None, **kw):
        commits = commits or {}
        with mock.patch.object(yesterday, "commits_on", lambda repo, day: commits.get(Path(repo).name)), \
             mock.patch.object(git_dirty, "count_changes", lambda repo: (dirty or {}).get(Path(repo).name)), \
             mock.patch.object(ga, "access_token", return_value="T"):
            return evening.build_message(cfg, NOW, fetch_events=lambda t, n: events if events is not None else [],
                                         fetch=fetch, **kw)

    def test_minimal_message(self):
        m = self.build(self.cfg())
        self.assertIn("저녁 정리 · 10/04(일)", m)
        self.assertIn("커밋 기록이 없어요.", m)
        self.assertNotIn("내일 일정", m)
        self.assertNotIn("마감 임박", m)
        self.assertNotIn("정리할 것", m)

    def test_today_commits_capped_and_dirty_threshold(self):
        m = self.build(self.cfg(), commits={"proj-a": ["c1", "c2", "c3", "c4", "c5"]}, dirty={"proj-a": 5, "proj-b": 4})
        self.assertIn("proj-a: 커밋 5개", m)
        self.assertIn("  - c3", m)
        self.assertNotIn("  - c4", m)
        self.assertIn("…외 2개", m)
        self.assertIn("proj-a: 커밋 안 된 변경 5개", m)
        self.assertNotIn("proj-b: 커밋 안 된 변경", m)

    def test_commits_are_looked_up_for_today(self):
        days = []
        with mock.patch.object(yesterday, "commits_on", lambda repo, day: days.append(day.isoformat()) or []), \
             mock.patch.object(git_dirty, "count_changes", lambda r: None):
            evening.build_message(self.cfg(), NOW, fetch=fetch)
        self.assertEqual(set(days), {"2026-10-04"})

    def test_tomorrow_events_use_next_day(self):
        got = []
        with mock.patch.object(yesterday, "commits_on", lambda r, d: []), \
             mock.patch.object(git_dirty, "count_changes", lambda r: None), \
             mock.patch.object(ga, "access_token", return_value="T"):
            m = evening.build_message(self.cfg(calendar_enabled=True), NOW, fetch=fetch,
                                      fetch_events=lambda t, n: got.append(n.date().isoformat()) or [gcal.Event("09:30", "회의")])
        self.assertEqual(got, ["2026-10-05"])
        self.assertIn("■ 내일 일정", m)
        self.assertIn("• 09:30 회의", m)

    def test_no_events_and_failures_degrade(self):
        m = self.build(self.cfg(calendar_enabled=True), events=[])
        self.assertIn("일정이 없어요.", m)
        with mock.patch.object(ga, "access_token", side_effect=ga.GoogleAuthError("x")), \
             mock.patch.object(yesterday, "commits_on", lambda r, d: []), mock.patch.object(git_dirty, "count_changes", lambda r: None):
            m = evening.build_message(self.cfg(calendar_enabled=True), NOW, fetch=fetch)
        self.assertIn("불러오지 못했어요", m)
        self.assertIn("오늘 작업", m)

    def test_deadline_section(self):
        m = self.build(self.cfg(notice_pages=[PAGE], notice_keywords=["공모", "연수"]))
        self.assertIn("마감 임박 (3일 이내)", m)
        self.assertIn("• D-1 연수 안내 공고 (내일 마감) (~10/05)", m)
        self.assertIn("• D-2 공모 사업 신청 안내 (곧 마감) (~10/06)", m)
        self.assertLess(m.index("D-1"), m.index("D-2"))
        self.assertNotIn("여유 있음", m)
        m = self.build(self.cfg(notice_pages=[PAGE], notice_keywords=["zzz"]))
        self.assertIn("없어요.", m)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.state = str(Path(self.d.name) / "s.json")
        self.sent = []
        self.p = [mock.patch.dict("os.environ", ENV),
                  mock.patch.object(yesterday, "commits_on", lambda r, d: []),
                  mock.patch.object(git_dirty, "count_changes", lambda r: None)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.d.cleanup()

    def cfg(self, **kw):
        kw.setdefault("evening_hour", 20)
        return Config(state_path=self.state, **kw)

    def go(self, cfg, now, **kw):
        return evening.run(cfg, send=True, now=now, sender=lambda text, **k: self.sent.append(text), fetch=fetch, **kw)

    def test_sends_once_after_the_hour(self):
        c = self.cfg()
        self.assertEqual(self.go(c, datetime(2026, 10, 4, 19, 59)), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.go(c, datetime(2026, 10, 4, 20, 0)), 0)
        self.assertEqual(len(self.sent), 1)
        self.go(c, datetime(2026, 10, 4, 20, 30))
        self.go(c, datetime(2026, 10, 4, 21, 0))
        self.assertEqual(len(self.sent), 1)
        self.go(c, datetime(2026, 10, 5, 20, 0))   # next day
        self.assertEqual(len(self.sent), 2)

    def test_off_by_default(self):
        self.assertEqual(self.go(self.cfg(evening_hour=None), datetime(2026, 10, 4, 21, 0)), 0)
        self.assertEqual(self.sent, [])

    def test_quiet_hours_skip(self):
        self.go(self.cfg(), datetime(2026, 10, 4, 22, 45))
        self.assertEqual(self.sent, [])

    def test_failed_send_is_retried_and_not_marked(self):
        def bad(text, **k):
            raise telegram.TelegramError("down")
        c = self.cfg()
        self.assertEqual(evening.run(c, send=True, now=datetime(2026, 10, 4, 20, 0), sender=bad, fetch=fetch), 1)
        self.assertFalse(State(self.state).is_seen("evening:2026-10-04"))
        self.go(c, datetime(2026, 10, 4, 20, 30))
        self.assertEqual(len(self.sent), 1)

    def test_force_ignores_hour_and_duplicates(self):
        c = self.cfg()
        self.go(c, datetime(2026, 10, 4, 20, 0))
        evening.run(c, send=True, force=True, now=datetime(2026, 10, 4, 9, 0),
                    sender=lambda text, **k: self.sent.append(text), fetch=fetch)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.go(self.cfg(evening_hour=None), datetime(2026, 10, 4, 21, 0)), 0)

    def test_missing_telegram_env_is_setup_error(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            self.assertEqual(self.go(self.cfg(), datetime(2026, 10, 4, 20, 0)), 2)

    def test_dry_run_prints_and_changes_nothing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = evening.run(self.cfg(evening_hour=None), send=False, now=datetime(2026, 10, 4, 7, 0), fetch=fetch)
        self.assertEqual(rc, 0)
        self.assertIn("저녁 정리", buf.getvalue())
        self.assertFalse(Path(self.state).exists())


class ConfigAndCliTests(unittest.TestCase):
    def test_hour_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            for bad in (24, -1, "20", True, 20.5):
                p.write_text(json.dumps({"evening_hour": bad}), encoding="utf-8")
                with self.assertRaises(ConfigError, msg=str(bad)):
                    load_config(p)
            p.write_text(json.dumps({"evening_hour": 20}), encoding="utf-8")
            self.assertEqual(load_config(p).evening_hour, 20)

    def test_cli_dispatch(self):
        from aide import __main__ as cli
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text("{}", encoding="utf-8")
            with mock.patch.object(evening, "run", return_value=0) as r:
                self.assertEqual(cli.main(["evening", "--send", "--force", "--config", str(p)]), 0)
            self.assertEqual((r.call_args.kwargs["send"], r.call_args.kwargs["force"]), (True, True))

    def test_heartbeat_bat_runs_evening(self):
        bat = (Path(__file__).resolve().parents[1] / "scripts" / "run_heartbeat.bat").read_text(encoding="utf-8")
        self.assertIn("python -m aide evening --send", bat)
        bat.encode("ascii")  # cmd must be able to read it


if __name__ == "__main__":
    unittest.main()
