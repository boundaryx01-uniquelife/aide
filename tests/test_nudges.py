import _bootstrap  # noqa: F401
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from aide import evening, google_auth, gcal, gtasks, morning, netutil, nudges
from aide.config import Config, ConfigError, load_config

NOW = datetime(2026, 10, 22, 9, 0)           # Thursday
EVE = datetime(2026, 10, 22, 20, 0)
SUN = datetime(2026, 10, 25, 20, 0)          # Sunday evening -> weekly mode, tomorrow = Monday 10/26
TWO = [{"name": "개인", "token_path": "data/t1.json"}, {"name": "학교", "token_path": "data/t2.json"}]


def D(n):
    return date(2026, 10, n)


def ev(start, end, title="일정", day=22, **kw):
    return gcal.Event(start, title, D(day), end=end, end_day=D(day), **kw)


def cfg_of(**kw):
    base = dict(google_accounts=TWO, calendar_enabled=True, tasks_accounts=[], nudges=["busy", "prep"], evening_hour=20)
    base.update(kw)
    return Config(**base)


def tasks_of(*dues):
    return lambda token: [gtasks.Task(f"t{i}", d) for i, d in enumerate(dues)]


def patched(events, failed=()):
    return mock.patch("aide.accounts.gather_events",
                      side_effect=lambda c, f, o: (list(events), list(failed), 1))


def morning_text(cfg, events, fetch_tasks=None):
    with mock.patch.object(google_auth, "access_token", return_value="T"), patched(events):
        return morning.build_message(cfg, NOW, fetch_tasks=fetch_tasks or tasks_of())


def evening_text(cfg, events, now=EVE, fetch_tasks=None):
    with mock.patch.object(google_auth, "access_token", return_value="T"), patched(events):
        return evening.build_message(cfg, now, fetch_tasks=fetch_tasks or tasks_of())


def six():
    return [ev(f"{h:02d}:00", f"{h:02d}:30", f"e{h}") for h in (8, 9, 10, 11, 13, 14)]


class BusyTests(unittest.TestCase):
    def test_off_by_default_changes_nothing(self):
        off = cfg_of(nudges=[])
        self.assertNotIn("바쁜 날", morning_text(off, six()))
        self.assertNotIn("내일 준비", evening_text(off, [ev("08:30", "10:00", day=23)]))
        self.assertEqual(Config().nudges, [])

    def test_count_threshold_boundary(self):
        self.assertNotIn("바쁜 날", morning_text(cfg_of(busy_events=5), six()[:4]))
        self.assertIn("⚠ 바쁜 날: 일정 5개", morning_text(cfg_of(busy_events=5), six()[:5]))
        self.assertIn("일정 6개", morning_text(cfg_of(busy_events=5), six()))

    def test_overlap_alone_triggers(self):
        text = morning_text(cfg_of(), [ev("10:00", "11:00"), ev("10:30", "11:30")])
        self.assertIn("⚠ 바쁜 날: 일정 2개 · 겹치는 일정 2개", text)

    def test_line_comes_right_after_the_header(self):
        lines = morning_text(cfg_of(), six()).split("\n")
        self.assertTrue(lines[0].startswith("좋은 아침이에요"))
        self.assertTrue(lines[2].startswith("⚠ 바쁜 날"))
        self.assertEqual(lines[3], "")

    def test_tasks_count_included_only_when_on_and_nonzero(self):
        cfg = cfg_of(tasks_accounts=["개인"])
        text = morning_text(cfg, six(), fetch_tasks=tasks_of(D(21), D(22), D(23), None))
        self.assertIn("⚠ 바쁜 날: 일정 6개 · 오늘까지 할 일 2개", text)   # overdue + today only
        zero = morning_text(cfg, six(), fetch_tasks=tasks_of(D(30)))
        self.assertNotIn("할 일", zero.split("\n")[2])
        off = morning_text(cfg_of(), six(), fetch_tasks=tasks_of(D(21)))
        self.assertNotIn("오늘까지 할 일", off)

    def test_tasks_fetched_once_for_section_and_count(self):
        calls = []

        def fetch(token):
            calls.append(token)
            return [gtasks.Task("a", D(22))]
        text = morning_text(cfg_of(tasks_accounts=["개인"]), six(), fetch_tasks=fetch)
        self.assertEqual(len(calls), 1)
        self.assertIn("■ 할 일 (오늘까지)", text)
        self.assertIn("오늘까지 할 일 1개", text)

    def test_task_failure_does_not_block_and_drops_the_count(self):
        def boom(token):
            raise netutil.NetError("down")
        text = morning_text(cfg_of(tasks_accounts=["개인"]), six(), fetch_tasks=boom)
        self.assertIn("좋은 아침이에요", text)
        self.assertIn("⚠ 바쁜 날: 일정 6개", text)
        self.assertNotIn("오늘까지 할 일", text)
        self.assertIn("할 일을 불러오지 못했어요", text)

    def test_exception_in_the_line_only_drops_the_line(self):
        self.assertEqual(nudges.busy_line(cfg_of(), None, 0), "")   # broken input -> no line, no raise
        self.assertEqual(nudges.busy_line(cfg_of(), [object()], 0), "")

    def test_calendar_failure_still_sends_the_greeting(self):
        with mock.patch("aide.accounts.gather_events", side_effect=lambda c, f, o: ([], ["개인"], 1)):
            text = morning.build_message(cfg_of(), NOW, fetch_tasks=tasks_of())
        self.assertIn("좋은 아침이에요", text)
        self.assertNotIn("바쁜 날", text)


class PrepTests(unittest.TestCase):
    def test_earliest_timed_event_with_location(self):
        evs = [ev("13:00", "14:00", "점심", day=23), ev("08:30", "10:00", "출장", day=23, location="본관"),
               gcal.Event("종일", "행사", D(23), end_day=D(23))]
        lines = evening_text(cfg_of(), evs).split("\n")
        self.assertEqual(lines[2], "▶ 내일 준비: 첫 일정 08:30~10:00 출장 @본관")
        self.assertEqual(lines[3], "")

    def test_all_day_only(self):
        text = evening_text(cfg_of(), [gcal.Event("종일", "행사", D(23), end_day=D(23))])
        self.assertIn("▶ 내일 준비: 첫 일정 종일 행사", text)

    def test_tasks_only_and_with_event(self):
        cfg = cfg_of(tasks_accounts=["개인"])
        only = evening_text(cfg, [], fetch_tasks=tasks_of(D(22), D(23), D(24), None))
        self.assertIn("▶ 내일 준비: 내일까지 할 일 2개", only)
        both = evening_text(cfg, [ev("08:30", "10:00", "출장", day=23)], fetch_tasks=tasks_of(D(21), D(23)))
        self.assertIn("▶ 내일 준비: 첫 일정 08:30~10:00 출장 · 내일까지 할 일 2개", both)

    def test_nothing_means_no_line(self):
        text = evening_text(cfg_of(tasks_accounts=["개인"]), [], fetch_tasks=tasks_of(D(30), None))
        self.assertNotIn("내일 준비", text)
        self.assertNotIn("내일 준비", evening_text(cfg_of(), []))

    def test_sunday_weekly_mode_uses_monday_only(self):
        evs = [ev("07:00", "08:00", "화요일일정", day=27), ev("11:00", "12:00", "월요일일정", day=26)]
        text = evening_text(cfg_of(), evs, now=SUN)
        self.assertIn("▶ 내일 준비: 첫 일정 11:00~12:00 월요일일정", text)
        tue_only = evening_text(cfg_of(), [ev("07:00", "08:00", "화", day=27)], now=SUN)
        self.assertNotIn("내일 준비", tue_only)

    def test_sunday_task_count_is_through_tomorrow(self):
        cfg = cfg_of(tasks_accounts=["개인"])
        text = evening_text(cfg, [], now=SUN, fetch_tasks=tasks_of(D(25), D(26), D(28)))
        self.assertIn("내일까지 할 일 2개", text)
        self.assertIn("■ 다음 주까지 할 일", text)

    def test_continuing_event_is_not_called_the_first_at_its_old_start(self):
        carried = gcal.Event("09:00", "긴 일정", D(23), end="10:00", end_day=D(24), began=D(21))
        later = ev("14:00", "15:00", "회의", day=23)
        self.assertIn("첫 일정 14:00~15:00 회의", evening_text(cfg_of(), [carried, later]))

    def test_exception_drops_only_the_line(self):
        self.assertEqual(nudges.prep_line(cfg_of(), EVE, None, 0), "")   # broken input -> no line, no raise
        broken = [gcal.Event("09:00", "x", "not-a-date")]
        self.assertEqual(nudges.prep_line(cfg_of(), EVE, broken, 0), "")

    def test_tasks_fetched_once_in_evening(self):
        calls = []

        def fetch(token):
            calls.append(1)
            return [gtasks.Task("a", D(23))]
        text = evening_text(cfg_of(tasks_accounts=["개인"]), [], fetch_tasks=fetch)
        self.assertEqual(len(calls), 1)
        self.assertIn("■ 내일까지 할 일", text)
        self.assertIn("내일까지 할 일 1개", text)


class SendPathTests(unittest.TestCase):
    ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}

    def test_morning_send_matches_dry_run_line(self):
        cfg = cfg_of(evening_hour=None)
        sent = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(google_auth, "access_token", return_value="T"), \
                patched(six()), mock.patch.dict("os.environ", self.ENV):
            cfg.state_path = str(Path(d) / "s.json")
            dry = morning.build_message(cfg, NOW, fetch_tasks=tasks_of())
            rc = morning.run(cfg, send=True, force=True, now=NOW, sender=lambda m, **k: sent.append(m),
                             fetch_tasks=tasks_of())
        self.assertEqual(rc, 0)
        self.assertEqual(sent[0].split("\n")[2], dry.split("\n")[2])
        self.assertTrue(sent[0].split("\n")[2].startswith("⚠ 바쁜 날"))

    def test_evening_send_has_the_line(self):
        cfg = cfg_of()
        sent = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(google_auth, "access_token", return_value="T"), \
                patched([ev("08:30", "10:00", "출장", day=23)]), mock.patch.dict("os.environ", self.ENV):
            cfg.state_path = str(Path(d) / "s.json")
            rc = evening.run(cfg, send=True, force=True, now=EVE, sender=lambda m, **k: sent.append(m),
                             fetch_tasks=tasks_of())
        self.assertEqual(rc, 0)
        self.assertEqual(sent[0].split("\n")[2], "▶ 내일 준비: 첫 일정 08:30~10:00 출장")

    def test_highlight_input_unchanged(self):
        cfg = cfg_of(llm_enabled=True, llm_highlights=["morning"])
        with mock.patch("aide.llm.pick", return_value=[]) as pick:
            morning.highlight_picks(cfg, six(), None, now=NOW, calls_today=0, api_key="k")
        self.assertEqual(pick.call_args[0][0][0], {"kind": "event", "when": "08:00", "text": "e8"})


class ConfigTests(unittest.TestCase):
    def write(self, data):
        p = Path(tempfile.mkdtemp()) / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_defaults(self):
        self.assertEqual((Config().nudges, Config().busy_events), ([], 5))

    def test_accepts_valid(self):
        cfg = load_config(self.write({"nudges": ["busy", "prep"], "busy_events": 1}))
        self.assertEqual(cfg.nudges, ["busy", "prep"])
        self.assertEqual(load_config(self.write({"busy_events": 50})).busy_events, 50)

    def test_rejects_bad_nudges(self):
        for bad in (["foo"], ["busy", "busy"], "busy", [1], ["Busy"]):
            with self.assertRaises(ConfigError, msg=str(bad)):
                load_config(self.write({"nudges": bad}))

    def test_rejects_bad_busy_events(self):
        for bad in (0, 51, -1, 2.5, "5", True, None):
            with self.assertRaises(ConfigError, msg=str(bad)):
                load_config(self.write({"busy_events": bad}))

    def test_example_config_loads(self):
        cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.json")
        self.assertEqual((cfg.nudges, cfg.busy_events), ([], 5))


if __name__ == "__main__":
    unittest.main()
