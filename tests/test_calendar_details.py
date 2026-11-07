import _bootstrap  # noqa: F401
import unittest
from datetime import date, datetime
from unittest import mock

from aide import accounts, commands, evening, gcal, google_auth, morning
from aide.checks import git_dirty, yesterday
from aide.config import Config

D = lambda d: date(2026, 10, d)
NOW = datetime(2026, 10, 21, 8, 0)
OV = " ⚠ 겹침"


def parse(**item):
    item.setdefault("summary", "x")
    return gcal._parse(item)


def timed(s, e=None, **kw):
    d = dict(start={"dateTime": s}, **kw)
    if e:
        d["end"] = {"dateTime": e}
    return parse(**d)


class ParseTests(unittest.TestCase):
    def test_timed_end(self):
        e = timed("2026-10-21T09:00:00+09:00", "2026-10-21T10:30:00+09:00")
        self.assertEqual((e.when, e.end, e.day, e.end_day), ("09:00", "10:30", D(21), D(21)))

    def test_utc_end_converted_to_kst(self):
        e = timed("2026-10-21T00:00:00Z", "2026-10-21T01:00:00Z")
        self.assertEqual((e.when, e.end), ("09:00", "10:00"))

    def test_midnight_end_belongs_to_previous_day(self):
        e = timed("2026-10-21T22:00:00+09:00", "2026-10-22T00:00:00+09:00")
        self.assertEqual((e.end, e.end_day), ("00:00", D(21)))

    def test_multi_day_timed(self):
        e = timed("2026-10-21T18:00:00+09:00", "2026-10-22T10:00:00+09:00")
        self.assertEqual((e.end, e.end_day), ("10:00", D(22)))

    def test_allday_single_uses_exclusive_end(self):
        e = parse(start={"date": "2026-10-21"}, end={"date": "2026-10-22"})
        self.assertEqual((e.when, e.day, e.end_day), ("종일", D(21), D(21)))

    def test_allday_multi_day(self):
        e = parse(start={"date": "2026-10-21"}, end={"date": "2026-10-24"})
        self.assertEqual(e.end_day, D(23))

    def test_bad_end_keeps_start_only(self):
        for end in ({"dateTime": "garbage"}, {}, {"dateTime": 5}):
            e = parse(start={"dateTime": "2026-10-21T09:00:00+09:00"}, end=end)
            self.assertEqual((e.when, e.end, e.end_day), ("09:00", "", None))
        e = parse(start={"date": "2026-10-21"}, end={"date": "bad"})
        self.assertEqual((e.when, e.end_day), ("종일", D(21)))

    def test_end_before_start_ignored(self):
        e = timed("2026-10-21T10:00:00+09:00", "2026-10-21T09:00:00+09:00")
        self.assertEqual((e.when, e.end, e.end_day), ("10:00", "", None))

    def test_location_is_cleaned_and_capped(self):
        e = timed("2026-10-21T09:00:00+09:00", location="  본관\x00 3층\n회의실 " + "가" * 60)
        self.assertNotIn("\x00", e.location)
        self.assertNotIn("\n", e.location)
        self.assertEqual(len(e.location), 40)
        self.assertTrue(e.location.startswith("본관 3층 회의실"))
        self.assertEqual(timed("2026-10-21T09:00:00+09:00", location=5).location, "")

    def test_fields_request_end_and_location(self):
        urls = []

        def get(url, headers=None):
            urls.append(url)
            return {"items": []}
        gcal.today_events("T", NOW, get=get)
        self.assertIn("end%2Clocation", urls[-1])


class LineTests(unittest.TestCase):
    def test_forms(self):
        E = gcal.Event
        cases = [
            (E("09:00", "회의", D(21)), "09:00 회의"),
            (E("09:00", "회의", D(21), end="10:30", end_day=D(21)), "09:00~10:30 회의"),
            (E("18:00", "숙박", D(21), end="10:00", end_day=D(22)), "18:00~10/22 10:00 숙박"),
            (E("종일", "휴가", D(21), end_day=D(21)), "종일 휴가"),
            (E("종일", "휴가"), "종일 휴가"),
            (E("종일", "출장", D(21), end_day=D(23)), "종일 ~10/23 출장"),
            (E("09:00", "회의", D(21), end="10:00", end_day=D(21), location="본관"), "09:00~10:00 회의 @본관"),
            (E("종일", "출장", D(21), end_day=D(23), location="서울"), "종일 ~10/23 출장 @서울"),
        ]
        for ev, want in cases:
            self.assertEqual(gcal.line(ev), want)

    def test_in_progress(self):
        E = gcal.Event
        t = E("18:00", "숙박", D(21), end="10:00", end_day=D(22), began=D(20))
        self.assertEqual(gcal.line(t), "(진행 중, ~10/22 10:00) 숙박")
        a = E("종일", "출장", D(21), end_day=D(23), began=D(19), location="서울")
        self.assertEqual(gcal.line(a), "(진행 중, ~10/23) 출장 @서울")
        self.assertEqual(gcal.line(E("18:00", "x", D(21), began=D(20))), "(진행 중) x")
        self.assertNotIn("18:00", gcal.line(t))

    def test_on_day(self):
        e = gcal.Event("종일", "출장", D(21), end_day=D(23))
        self.assertEqual(gcal.line(e), "종일 ~10/23 출장")
        self.assertEqual(gcal.line(e, on_day=D(22)), "(진행 중, ~10/23) 출장")
        self.assertEqual(gcal.line(e, on_day=D(21)), "종일 ~10/23 출장")


def fake_get(items):
    def get(url, headers=None):
        return {"items": items}
    return get


class ShiftTests(unittest.TestCase):
    ITEMS = [
        {"summary": "숙박", "start": {"dateTime": "2026-10-20T18:00:00+09:00"}, "end": {"dateTime": "2026-10-22T10:00:00+09:00"}},
        {"summary": "회의", "start": {"dateTime": "2026-10-21T09:00:00+09:00"}, "end": {"dateTime": "2026-10-21T10:00:00+09:00"}},
        {"summary": "출장", "start": {"date": "2026-10-19"}, "end": {"date": "2026-10-23"}},
    ]

    def check(self, evs):
        self.assertEqual([e.title for e in evs], ["출장", "숙박", "회의"])   # in progress above timed
        self.assertEqual([e.day for e in evs], [D(21)] * 3)
        self.assertEqual([e.began for e in evs], [D(19), D(20), None])
        self.assertEqual(gcal.line(evs[0]), "(진행 중, ~10/22) 출장")
        self.assertEqual(gcal.line(evs[1]), "(진행 중, ~10/22 10:00) 숙박")
        self.assertEqual(gcal.line(evs[2]), "09:00~10:00 회의")

    def test_today_events(self):
        self.check(gcal.today_events("T", NOW, get=fake_get(self.ITEMS)))

    def test_events_between(self):
        self.check(gcal.events_between("T", D(21), 1, get=fake_get(self.ITEMS)))

    def test_between_later_start_keeps_its_day(self):
        evs = gcal.events_between("T", D(21), 3, get=fake_get([
            {"summary": "a", "start": {"dateTime": "2026-10-22T09:00:00+09:00"},
             "end": {"dateTime": "2026-10-22T10:00:00+09:00"}}]))
        self.assertEqual((evs[0].day, evs[0].began), (D(22), None))


class OverlapTests(unittest.TestCase):
    def ev(self, s, e, day=21, end_day=None):
        return gcal.Event(s, "t", D(day), end=e, end_day=D(end_day or day))

    def test_overlap_and_touching(self):
        evs = [self.ev("10:00", "11:00"), self.ev("10:30", "11:30"), self.ev("11:30", "12:00"), self.ev("12:00", "13:00")]
        self.assertEqual(gcal.overlaps(evs), {0, 1})   # 11:30 touches, 12:00 clear

    def test_touching_is_not_overlap(self):
        self.assertEqual(gcal.overlaps([self.ev("10:00", "11:00"), self.ev("11:00", "12:00")]), set())

    def test_excluded_kinds(self):
        E = gcal.Event
        evs = [self.ev("10:00", "11:00"),
               E("10:30", "no end", D(21)),
               E("종일", "all", D(21), end_day=D(21)),
               E("10:30", "run", D(21), end="11:30", end_day=D(21), began=D(20))]
        self.assertEqual(gcal.overlaps(evs), set())

    def test_other_day_excluded(self):
        self.assertEqual(gcal.overlaps([self.ev("10:00", "11:00"), self.ev("10:30", "11:30", day=22)]), set())

    def test_multi_day_and_midnight_end(self):
        self.assertEqual(gcal.overlaps([self.ev("22:00", "00:00"), self.ev("23:00", "23:30")]), {0, 1})
        self.assertEqual(gcal.overlaps([self.ev("22:00", "08:00", end_day=22), self.ev("23:00", "23:30")]), {0, 1})

    def test_lines_marker(self):
        evs = [self.ev("10:00", "11:00"), self.ev("10:30", "11:30"), self.ev("13:00", "14:00")]
        self.assertEqual(gcal.lines(evs), ["10:00~11:00 t" + OV, "10:30~11:30 t" + OV, "13:00~14:00 t"])


class MergeTests(unittest.TestCase):
    def test_richer_details_kept_and_location_after_label(self):
        cfg = mock.Mock()
        a1, a2 = mock.Mock(), mock.Mock()
        a1.name, a1.calendars, a1.token_path = "개인", [], "p1"
        a2.name, a2.calendars, a2.token_path = "학교", [], "p2"
        cfg.accounts.return_value = [a1, a2]
        bare = gcal.Event("09:00", "회의", D(21))
        rich = gcal.Event("09:00", "회의", D(21), end="10:00", end_day=D(21), location="본관")
        calls = []

        def call(token):
            calls.append(token)
            return [bare] if len(calls) == 1 else [rich]
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            out, failed, total = accounts.gather_events(cfg, call, gcal.order)
        self.assertEqual(len(out), 1)
        self.assertEqual(gcal.line(out[0]), "09:00~10:00 [개인·학교] 회의 @본관")


class RenderTests(unittest.TestCase):
    EVS = [gcal.Event("종일", "출장", D(21), end_day=D(23), began=D(19)),
           gcal.Event("09:00", "회의", D(21), end="10:30", end_day=D(21), location="본관"),
           gcal.Event("10:00", "면담", D(21), end="11:00", end_day=D(21))]
    WANT = ["• (진행 중, ~10/23) 출장", "• 09:00~10:30 회의 @본관" + OV, "• 10:00~11:00 면담" + OV]

    def cfg(self, **kw):
        return Config(calendar_enabled=True, **kw)

    def test_render_today(self):
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            out = commands.render_today(self.cfg(), NOW, fetch_events=lambda t, n: list(self.EVS))
        self.assertEqual(out.split("\n")[-3:], self.WANT)

    def test_render_week(self):
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            out = commands.render_week(self.cfg(), NOW, fetch_week=lambda t, f, d: list(self.EVS))
        for w in self.WANT:
            self.assertIn("  " + w, out)

    def test_render_week_overlap_is_per_day(self):
        evs = [gcal.Event("10:00", "a", D(21), end="11:00", end_day=D(21)),
               gcal.Event("10:30", "b", D(22), end="11:30", end_day=D(22))]
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            out = commands.render_week(self.cfg(), NOW, fetch_week=lambda t, f, d: evs)
        self.assertNotIn("겹침", out)

    def test_morning_section(self):
        out = morning._today_section(self.cfg(), None, list(self.EVS), [], 1)
        self.assertEqual(out[1:4], self.WANT)

    def _evening(self, now, **kw):
        with mock.patch.object(yesterday, "commits_on", lambda r, d: []), \
             mock.patch.object(git_dirty, "count_changes", lambda r: None), \
             mock.patch.object(google_auth, "access_token", return_value="T"):
            return evening.build_message(self.cfg(), now, fetch=lambda u: b"", **kw)

    def test_evening_weekday(self):
        m = self._evening(datetime(2026, 10, 21, 20, 5), fetch_events=lambda t, n: list(self.EVS))
        for w in self.WANT:
            self.assertIn(w, m)

    def test_evening_sunday(self):
        m = self._evening(datetime(2026, 10, 25, 20, 5), fetch_week=lambda t, f, d: list(self.EVS),
                          fetch_events=lambda t, n: self.fail("daily"))
        for w in self.WANT:
            self.assertIn("  " + w, m)


class LlmCandidateTests(unittest.TestCase):
    EVS = [gcal.Event("09:00", f"일정{i}", D(21), end="10:00", end_day=D(21), location="비밀장소") for i in range(4)]

    def test_morning_items_have_no_location_or_end(self):
        seen = {}

        def fake_pick(items, cfg, **kw):
            seen["items"] = items
            return []
        cfg = Config(calendar_enabled=True, llm_enabled=True, llm_highlights=["morning"])
        with mock.patch.object(morning.llm, "pick", side_effect=fake_pick):
            morning.highlight_picks(cfg, self.EVS, None, now=NOW, calls_today=0, api_key="k")
        self.assertEqual(seen["items"][0], {"kind": "event", "when": "09:00", "text": "일정0"})
        self.assertNotIn("비밀장소", str(seen["items"]))
        self.assertNotIn("10:00", str(seen["items"]))

    def test_evening_candidates_have_no_location_or_end(self):
        cands = evening._candidates(Config(calendar_enabled=True), datetime(2026, 10, 25, 20, 5), self.EVS, [])
        self.assertEqual(cands[0], ("event", "10/21(수) 09:00", "일정0"))
        self.assertNotIn("비밀장소", str(cands))
        cands = evening._candidates(Config(calendar_enabled=True), datetime(2026, 10, 21, 20, 5), self.EVS, [])
        self.assertEqual(cands[0], ("event", "09:00", "일정0"))


class ChatReuseTests(unittest.TestCase):
    def test_chat_uses_commands_render_week(self):
        import inspect
        from aide import chat
        self.assertIn("render_week", inspect.getsource(chat))


if __name__ == "__main__":
    unittest.main()
