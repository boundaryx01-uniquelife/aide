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
        return Config(**kw)

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
