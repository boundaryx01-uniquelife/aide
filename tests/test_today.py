import _bootstrap  # noqa: F401
import unittest
from datetime import datetime
from unittest import mock

from aide import gcal, google_auth, morning, netutil, weather
from aide.config import Config

NOW = datetime(2026, 10, 3, 8, 0)


def ev(**kw):
    return dict(kw)


class GcalTests(unittest.TestCase):
    def fetch(self, items):
        calls = []

        def get(url, headers=None):
            if "calendarList" in url:
                return {"items": []}
            calls.append((url, headers))
            return {"items": items}

        return gcal.today_events("TOK", NOW, get=get), calls

    def test_request_shape(self):
        _, calls = self.fetch([])
        url, headers = calls[0]
        self.assertEqual(headers, {"Authorization": "Bearer TOK"})
        self.assertIn("calendars/primary/events", url)
        self.assertIn("singleEvents=true", url)
        self.assertIn("orderBy=startTime", url)
        self.assertIn("timeMin=2026-10-03T00%3A00%3A00%2B09%3A00", url)
        self.assertIn("timeMax=2026-10-04T00%3A00%3A00%2B09%3A00", url)
        self.assertIn("maxResults=20", url)

    def test_parsing_sorting_and_filtering(self):
        items = [
            ev(summary="오후 회의", start={"dateTime": "2026-10-03T14:00:00+09:00"}),
            ev(summary="워크숍", start={"date": "2026-10-03"}),
            ev(summary="아침", start={"dateTime": "2026-10-02T23:30:00Z"}),  # 08:30 KST
            ev(summary="취소됨", status="cancelled", start={"dateTime": "2026-10-03T10:00:00+09:00"}),
            ev(summary="거절", start={"dateTime": "2026-10-03T11:00:00+09:00"},
               attendees=[{"self": True, "responseStatus": "declined"}]),
            ev(summary="수락", start={"dateTime": "2026-10-03T12:00:00+09:00"},
               attendees=[{"self": True, "responseStatus": "accepted"}, {"responseStatus": "declined"}]),
            ev(start={"dateTime": "2026-10-03T16:00:00+09:00"}),
            ev(summary="깨짐", start={"dateTime": "not-a-date"}),
            ev(summary="시작없음"),
        ]
        events, _ = self.fetch(items)
        self.assertEqual([(e.when, e.title) for e in events], [
            ("종일", "워크숍"), ("08:30", "아침"), ("12:00", "수락"), ("14:00", "오후 회의"), ("16:00", "(제목 없음)")])

    def test_title_is_sanitized_and_capped(self):
        events, _ = self.fetch([ev(summary="a\x00b\n" + "x" * 300, start={"date": "2026-10-03"})])
        self.assertNotIn("\x00", events[0].title)
        self.assertNotIn("\n", events[0].title)
        self.assertLessEqual(len(events[0].title), 80)

    def test_odd_payloads(self):
        self.assertEqual(gcal.today_events("T", NOW, get=lambda *a, **k: []), [])
        self.assertEqual(gcal.today_events("T", NOW, get=lambda *a, **k: {"items": ["x", 3]}), [])


class ChosenCalendarsTests(unittest.TestCase):
    """Calendars other than the main one (e.g. "work") are read only when the account's
    `calendars` setting names them; nothing configured = main calendar only."""
    CALS = {"items": [
        {"id": "me@gmail.com", "summary": "me@gmail.com", "primary": True},
        {"id": "w1", "summary": "work"},
        {"id": "c2", "summary": "발명센터"},
        {"id": "f3", "summary": "Family"},
        "junk", {"id": 5}, {"summary": "no id"},
    ]}

    def run_it(self, names, per_cal=None, cals=None, fail=()):
        urls = []
        per_cal = per_cal or {}

        def get(url, headers=None):
            if "calendarList" in url:
                if cals == "FAIL":
                    raise netutil.NetError("x")
                return self.CALS if cals is None else cals
            urls.append(url)
            for name in fail:
                if f"/calendars/{name}/events" in url:
                    raise netutil.NetError("boom")
            for name, items in per_cal.items():
                if f"/calendars/{name}/events" in url:
                    return {"items": items}
            return {"items": []}
        with gcal.only_calendars(names):
            evs = gcal.today_events("T", NOW, get=get)
        return evs, urls

    def test_nothing_configured_reads_only_primary_without_listing(self):
        _, urls = self.run_it(())
        self.assertEqual(len(urls), 1)
        self.assertIn("/calendars/primary/events", urls[0])

    def test_named_calendars_only(self):
        _, urls = self.run_it(("기본", "work"))
        self.assertEqual(len(urls), 2)
        self.assertIn("/calendars/me%40gmail.com/events", urls[0])
        self.assertIn("/calendars/w1/events", urls[1])      # "발명센터", "Family" not asked for

    def test_work_only_does_not_read_primary(self):
        _, urls = self.run_it(("work",))
        self.assertEqual(len(urls), 1)
        self.assertIn("/calendars/w1/events", urls[0])

    def test_korean_names_and_order(self):
        _, urls = self.run_it(("기본", "발명센터", "Family"))
        self.assertEqual([u.split("/calendars/")[1].split("/")[0] for u in urls], ["me%40gmail.com", "c2", "f3"])

    def test_unicode_normalization_and_spaces_do_not_matter(self):
        import unicodedata
        _, urls = self.run_it((unicodedata.normalize("NFD", " 발명센터 "),))
        self.assertIn("/calendars/c2/events", urls[0])

    def test_id_also_works_and_english_primary(self):
        _, urls = self.run_it(("primary", "w1"))
        self.assertEqual(len(urls), 2)

    def test_events_from_chosen_calendars_are_merged_in_order(self):
        evs, _ = self.run_it(("기본", "work"), {
            "me%40gmail.com": [ev(summary="기본 일정", start={"dateTime": "2026-10-03T15:00:00+09:00"})],
            "w1": [ev(summary="수학여행", start={"dateTime": "2026-10-03T09:00:00+09:00"})],
        })
        self.assertEqual([(e.when, e.title) for e in evs], [("09:00", "수학여행"), ("15:00", "기본 일정")])

    def test_same_event_on_two_calendars_shown_once(self):
        same = ev(summary="회의", start={"dateTime": "2026-10-03T09:00:00+09:00"})
        evs, _ = self.run_it(("기본", "work"), {"me%40gmail.com": [same], "w1": [same]})
        self.assertEqual(len(evs), 1)

    def test_same_name_twice_is_read_once(self):
        _, urls = self.run_it(("work", "work", "w1"))
        self.assertEqual(len(urls), 1)

    def test_unknown_name_is_skipped_others_still_read(self):
        with self.assertLogs("aide.gcal", "WARNING"):
            _, urls = self.run_it(("없는캘린더", "work"))
        self.assertEqual(len(urls), 1)
        self.assertIn("/calendars/w1/events", urls[0])

    def test_nothing_matches_falls_back_to_primary(self):
        with self.assertLogs("aide.gcal", "WARNING"):
            _, urls = self.run_it(("없는캘린더",))
        self.assertIn("/calendars/primary/events", urls[0])

    def test_calendar_list_failure_falls_back_to_primary(self):
        with self.assertLogs("aide.gcal", "WARNING"):
            _, urls = self.run_it(("work",), cals="FAIL")
        self.assertEqual(len(urls), 1)
        self.assertIn("/calendars/primary/events", urls[0])

    def test_odd_calendar_list_falls_back_to_primary(self):
        for cals in ({"items": []}, {"items": "x"}, []):
            with self.assertLogs("aide.gcal", "WARNING"):
                _, urls = self.run_it(("work",), cals=cals)
            self.assertIn("/calendars/primary/events", urls[0])

    def test_primary_without_flag_still_means_primary(self):
        _, urls = self.run_it(("기본",), cals={"items": [{"id": "w1", "summary": "work"}]})
        self.assertIn("/calendars/primary/events", urls[0])

    def test_one_broken_calendar_is_skipped(self):
        evs, _ = self.run_it(("기본", "work"), {"w1": [ev(summary="살아있음", start={"date": "2026-10-03"})]},
                             fail=("me%40gmail.com",))
        self.assertEqual([e.title for e in evs], ["살아있음"])

    def test_all_calendars_failing_raises(self):
        with self.assertRaises(netutil.NetError):
            self.run_it(("기본", "work"), fail=("me%40gmail.com", "w1"))

    def test_setting_is_restored_after_the_block(self):
        with gcal.only_calendars(("work",)):
            pass
        self.assertEqual(gcal.calendar_ids("T", lambda *a, **k: self.fail("listed")), ["primary"])

    def test_week_view_reads_chosen_calendars_too(self):
        from datetime import date
        calls = []

        def get(url, headers=None):
            if "calendarList" in url:
                return self.CALS
            calls.append(url)
            if "/calendars/w1/" in url:
                return {"items": [ev(summary="출장", start={"dateTime": "2026-10-05T15:00:00+09:00"})]}
            return {"items": []}
        with gcal.only_calendars(("기본", "work")):
            evs = gcal.events_between("T", date(2026, 10, 5), 7, get=get)
        self.assertEqual([e.title for e in evs], ["출장"])
        self.assertEqual(len(calls), 2)


class EventsBetweenTests(unittest.TestCase):
    def run_it(self, items, first=None, days=7):
        from datetime import date
        calls = []

        def get(url, headers=None):
            if "calendarList" in url:
                return {"items": []}
            calls.append(url)
            return {"items": items}
        return gcal.events_between("T", first or date(2026, 10, 5), days, get=get), calls

    def test_range_and_ordering(self):
        items = [
            ev(summary="수 연수", start={"dateTime": "2026-10-07T14:00:00+09:00"}),
            ev(summary="월 회의", start={"dateTime": "2026-10-05T09:30:00+09:00"}),
            ev(summary="월 종일", start={"date": "2026-10-05"}),
            ev(summary="화 아침", start={"dateTime": "2026-10-05T23:30:00Z"}),   # 10/06 08:30 KST
            ev(summary="깨진 날짜", start={"date": "2026-13-45"}),
        ]
        events, calls = self.run_it(items)
        self.assertEqual([(str(e.day), e.when, e.title) for e in events], [
            ("2026-10-05", "종일", "월 종일"), ("2026-10-05", "09:30", "월 회의"),
            ("2026-10-06", "08:30", "화 아침"), ("2026-10-07", "14:00", "수 연수")])
        self.assertIn("timeMin=2026-10-05T00%3A00%3A00%2B09%3A00", calls[0])
        self.assertIn("timeMax=2026-10-12T00%3A00%3A00%2B09%3A00", calls[0])
        self.assertIn("maxResults=50", calls[0])

    def test_multiday_event_started_earlier_is_shown_on_first_day(self):
        events, _ = self.run_it([ev(summary="연수 주간", start={"date": "2026-10-01"})])
        self.assertEqual(str(events[0].day), "2026-10-05")

    def test_days_parameter_sets_window(self):
        _, calls = self.run_it([], days=3)
        self.assertIn("timeMax=2026-10-08T00%3A00%3A00%2B09%3A00", calls[0])

    def test_event_day_is_set_by_today_events_too(self):
        evs = gcal.today_events("T", NOW, get=lambda *a, **k: {"items": [ev(summary="x", start={"date": "2026-10-03"})]})
        self.assertEqual(str(evs[0].day), "2026-10-03")


class WeatherTests(unittest.TestCase):
    def data(self, code=61, lo=14.4, hi=21.6, pct=70):
        return {"daily": {"weather_code": [code], "temperature_2m_min": [lo],
                          "temperature_2m_max": [hi], "precipitation_probability_max": [pct]}}

    def test_line_with_umbrella(self):
        w = weather.today(35.2, 129.08, get=lambda url: self.data())
        self.assertEqual(w.line(), "비, 14~22°C, 강수확률 70% · 우산 챙기세요")

    def test_umbrella_threshold(self):
        self.assertIn("우산", weather.today(0, 0, get=lambda u: self.data(pct=50)).line())
        self.assertNotIn("우산", weather.today(0, 0, get=lambda u: self.data(pct=49)).line())

    def test_unknown_code_and_null_pct(self):
        w = weather.today(0, 0, get=lambda u: self.data(code=999, pct=None))
        self.assertEqual((w.text, w.rain_pct), ("날씨 정보", 0))

    def test_request_params(self):
        seen = []
        weather.today(35.2, 129.08, get=lambda url: seen.append(url) or self.data())
        for part in ("latitude=35.2", "longitude=129.08", "timezone=Asia%2FSeoul", "forecast_days=1", "precipitation_probability_max"):
            self.assertIn(part, seen[0])

    def test_bad_shape_is_neterror(self):
        for bad in ({}, {"daily": {}}, {"daily": {"weather_code": []}}):
            with self.assertRaises(netutil.NetError):
                weather.today(0, 0, get=lambda u, b=bad: b)


class MorningSectionTests(unittest.TestCase):
    def cfg(self, **kw):
        return Config(watch_repos=["C:/x/none"], **kw)   # a folder is configured, so the work section shows

    def test_off_by_default_leaves_message_unchanged(self):
        def boom(*a):
            raise AssertionError("must not be called")
        m = morning.build_message(self.cfg(), NOW, fetch_weather=boom, fetch_events=boom)
        self.assertNotIn("날씨", m)
        self.assertNotIn("일정", m)

    def test_weather_and_calendar_shown_before_yesterday(self):
        w = weather.Weather("맑음", 12, 20, 10)
        evs = [gcal.Event("종일", "워크숍"), gcal.Event("14:00", "회의")]
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            m = morning.build_message(self.cfg(weather_enabled=True, calendar_enabled=True), NOW,
                                      fetch_weather=lambda la, lo: w, fetch_events=lambda t, n: evs)
        self.assertIn("■ 날씨: 맑음, 12~20°C, 강수확률 10%", m)
        self.assertIn("• 14:00 회의", m)
        self.assertIn("• 종일 워크숍", m)
        self.assertLess(m.index("오늘 일정"), m.index("어제"))

    def test_weather_gets_configured_coordinates(self):
        got = []
        morning.build_message(self.cfg(weather_enabled=True, latitude=1.5, longitude=2.5), NOW,
                              fetch_weather=lambda la, lo: got.append((la, lo)) or weather.Weather("맑음", 1, 2, 3))
        self.assertEqual(got, [(1.5, 2.5)])

    def test_no_events(self):
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            m = morning.build_message(self.cfg(calendar_enabled=True), NOW, fetch_events=lambda t, n: [])
        self.assertIn("일정이 없어요", m)

    def test_failures_never_block_greeting(self):
        def wfail(la, lo):
            raise netutil.NetError("연결 실패")
        with mock.patch.object(google_auth, "access_token", side_effect=google_auth.GoogleAuthError("로그인 안 됨")):
            m = morning.build_message(self.cfg(weather_enabled=True, calendar_enabled=True), NOW, fetch_weather=wfail)
        self.assertIn("좋은 아침", m)
        self.assertIn("어제", m)
        self.assertIn("불러오지 못했어요", m)
        self.assertNotIn("날씨:", m)

    def test_calendar_fetch_error_degrades(self):
        def efail(t, n):
            raise netutil.NetError("HTTP 500")
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            m = morning.build_message(self.cfg(calendar_enabled=True), NOW, fetch_events=efail)
        self.assertIn("불러오지 못했어요", m)
        self.assertNotIn("T", m.replace("Th", ""))  # token never in message


class ConfigTests(unittest.TestCase):
    def test_bad_coordinates_rejected(self):
        import json, tempfile
        from pathlib import Path
        from aide.config import ConfigError, load_config
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"latitude": 99}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)
            p.write_text(json.dumps({"longitude": -181}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)
            p.write_text(json.dumps({"latitude": 35.2, "calendar_enabled": True}), encoding="utf-8")
            self.assertTrue(load_config(p).calendar_enabled)


if __name__ == "__main__":
    unittest.main()
