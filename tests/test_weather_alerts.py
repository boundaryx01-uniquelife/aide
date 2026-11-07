import _bootstrap  # noqa: F401
import functools
import io
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from aide import evening, gcal, morning, netutil, weather, weather_alerts as wa
from aide.config import Config, ConfigError, load_config

MORN = datetime(2026, 7, 14, 8, 0)      # Tuesday
EVE = datetime(2026, 7, 14, 20, 0)      # Tuesday evening -> tomorrow = 7/15
SUN = datetime(2026, 7, 19, 20, 0)      # Sunday evening -> weekly, Monday = 7/20
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}


def D(n):
    return date(2026, 7, n)


def ev(when, title, day=14, **kw):
    return gcal.Event(when, title, D(day), **kw)


def forecast(low=None, high=None, rain=10, prev_low=None, offset=0, code=1, wind=3):
    """Defaults keep the day unremarkable: day range 8 degrees, no drop from the day before."""
    if low is None and high is None:
        low, high = 15, 23
    elif low is None:
        low = high - 8
    elif high is None:
        high = low + 8
    if prev_low is None:
        prev_low = low
    n = offset + 2
    lows = [9] * n
    lows[offset] = prev_low
    lows[offset + 1] = low
    highs, rains, codes, winds = [20] * n, [0] * n, [0] * n, [0] * n
    highs[offset + 1], rains[offset + 1], codes[offset + 1], winds[offset + 1] = high, rain, code, wind
    return {"daily": {"temperature_2m_min": lows, "temperature_2m_max": highs,
                      "precipitation_probability_max": rains, "weather_code": codes, "wind_speed_10m_max": winds}}


def air(day, pm10=None, pm25=None, other=(10, 5)):
    """Two days of hourly values; `day` ISO date gets the given maxima in one hour (others lower)."""
    times, a, b = [], [], []
    for d in ("2026-07-14", "2026-07-15", "2026-07-20"):
        for h in (0, 12):
            times.append(f"{d}T{h:02d}:00")
            if d == day and h == 12:
                a.append(pm10)
                b.append(pm25)
            elif d == day:
                a.append(1)
                b.append(1)
            else:
                a.append(other[0])
                b.append(other[1])
    return {"hourly": {"time": times, "pm10": a, "pm2_5": b}}


class FakeNet:
    def __init__(self, fc=None, aq=None, fc_error=None, aq_error=None):
        self.fc, self.aq, self.fc_error, self.aq_error = fc, aq, fc_error, aq_error
        self.urls = []

    def __call__(self, url, headers=None, timeout=15):
        self.urls.append(url)
        p = urllib.parse.urlparse(url)
        assert p.scheme == "https"
        if p.netloc == "api.open-meteo.com":
            if self.fc_error:
                raise self.fc_error
            return self.fc
        if p.netloc == "air-quality-api.open-meteo.com":
            if self.aq_error:
                raise self.aq_error
            return self.aq
        raise AssertionError(url)


def cfg_of(**kw):
    kw.setdefault("weather_enabled", True)
    kw.setdefault("weather_alerts", True)
    kw.setdefault("calendar_enabled", True)
    kw.setdefault("watch_repos", [])
    return Config(**kw)


def lines_for(events, fc=None, aq=None, offset=0, now=MORN, **kw):
    net = FakeNet(fc if fc is not None else forecast(offset=offset), aq if aq is not None else air("2099-01-01"))
    return wa.section(cfg_of(**kw), now, events, offset, get=net)


# ---- data -----------------------------------------------------------------------------------------

class GradeTests(unittest.TestCase):
    def test_pm10_bands(self):
        g = lambda v: wa.pm_grade(v, None)  # noqa: E731
        self.assertEqual([g(80), g(81), g(150), g(151)], [0, 1, 1, 2])

    def test_pm25_bands(self):
        g = lambda v: wa.pm_grade(None, v)  # noqa: E731
        self.assertEqual([g(35), g(36), g(75), g(76)], [0, 1, 1, 2])

    def test_worse_of_the_two_and_missing(self):
        self.assertEqual(wa.pm_grade(50, 80), 2)
        self.assertEqual(wa.pm_grade(200, 10), 2)
        self.assertEqual(wa.pm_grade(90, 10), 1)
        self.assertEqual(wa.pm_grade(None, None), 0)
        self.assertEqual(wa.pm_grade(80.9, 35.9), 0)


class AirMaxTests(unittest.TestCase):
    def test_max_of_the_requested_day_only(self):
        d = air("2026-07-14", pm10=120, pm25=40)
        self.assertEqual(wa.air_max(35.2, 129.08, D(14), lambda url: d), (120, 40))
        self.assertEqual(wa.air_max(35.2, 129.08, D(15), lambda url: d), (10, 5))

    def test_request_shape(self):
        seen = []
        wa.air_max(35.2, 129.08, D(14), lambda url: seen.append(url) or air("2026-07-14", 1, 1))
        u = urllib.parse.urlparse(seen[0])
        self.assertEqual((u.scheme, u.netloc, u.path), ("https", "air-quality-api.open-meteo.com", "/v1/air-quality"))
        q = urllib.parse.parse_qs(u.query)
        self.assertEqual(q["hourly"], ["pm10,pm2_5"])
        self.assertEqual(q["timezone"], ["Asia/Seoul"])
        self.assertEqual(q["forecast_days"], ["2"])
        self.assertEqual((q["latitude"], q["longitude"]), (["35.2"], ["129.08"]))

    def test_none_and_junk_values_are_skipped(self):
        d = {"hourly": {"time": ["2026-07-14T00:00", "2026-07-14T01:00", "2026-07-14T02:00", "2026-07-14T03:00"],
                        "pm10": [None, "x", True, 90], "pm2_5": [None, None, None, None]}}
        self.assertEqual(wa.air_max(0, 0, D(14), lambda url: d), (90, None))

    def test_unexpected_shapes_mean_no_data(self):
        for bad in ({}, [], "x", None, {"hourly": []}, {"hourly": {"time": "x", "pm10": [], "pm2_5": []}},
                    {"hourly": {"time": [], "pm10": [], "pm2_5": []}},
                    {"hourly": {"time": ["2026-07-14T00:00"], "pm10": 5, "pm2_5": 5}},
                    {"hourly": {"time": ["2026-07-14T00:00"]}}, {"hourly": {"time": [5], "pm10": [1], "pm2_5": [1]}}):
            self.assertEqual(wa.air_max(0, 0, D(14), lambda url, b=bad: b), (None, None), bad)

    def test_shorter_value_lists_do_not_crash(self):
        d = {"hourly": {"time": ["2026-07-14T00:00", "2026-07-14T01:00"], "pm10": [50], "pm2_5": []}}
        self.assertEqual(wa.air_max(0, 0, D(14), lambda url: d), (50, None))


class DetailTests(unittest.TestCase):
    def test_request_asks_for_yesterday_too_and_values_are_picked_by_offset(self):
        seen = []
        d = weather.detail(35.2, 129.08, 1, lambda url: seen.append(url) or forecast(low=3, high=30, rain=70,
                                                                                    prev_low=11, offset=1, code=61))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(seen[0]).query)
        self.assertEqual((q["past_days"], q["forecast_days"]), (["1"], ["2"]))
        self.assertEqual(q["wind_speed_unit"], ["ms"])
        self.assertIn("wind_speed_10m_max", q["daily"][0])
        self.assertEqual(d, weather.Detail(3, 30, 70, 61, 11, 3.0))

    def test_missing_wind_is_none(self):
        f = forecast()
        del f["daily"]["wind_speed_10m_max"]
        self.assertIsNone(weather.detail(0, 0, 0, lambda url: f).wind)

    def test_today_uses_yesterdays_low(self):
        d = weather.detail(0, 0, 0, lambda url: forecast(low=2, prev_low=9))
        self.assertEqual((d.low, d.prev_low), (2, 9))

    def test_bad_shapes_and_range(self):
        for bad in ({}, {"daily": {}}, {"daily": {"temperature_2m_min": [1]}}, []):
            with self.assertRaises(netutil.NetError):
                weather.detail(0, 0, 0, lambda url, b=bad: b)
        with self.assertRaises(netutil.NetError):
            weather.detail(0, 0, 7, lambda url: {})
        with self.assertRaises(netutil.NetError):
            weather.detail(0, 0, True, lambda url: {})

    def test_missing_previous_low_is_none_not_an_error(self):
        f = forecast(low=3)
        f["daily"]["temperature_2m_min"][0] = None
        self.assertIsNone(weather.detail(0, 0, 0, lambda url: f).prev_low)

    def test_existing_forecast_request_is_unchanged(self):
        seen = []
        weather.on_day(35.2, 129.08, 0, lambda url: seen.append(url) or {"daily": {
            "weather_code": [0], "temperature_2m_min": [1], "temperature_2m_max": [2],
            "precipitation_probability_max": [3]}})
        self.assertNotIn("past_days", seen[0])


# ---- rules ------------------------------------------------------------------------------------------

HEAD = "■ 날씨에 따른 안내"
GAME = [ev("14:00", "체육대회")]


def body(*a, **k):
    out = lines_for(*a, **k)
    return out[1:-1] if out else []


class CommuteTests(unittest.TestCase):
    def test_rain_boundary_and_text(self):
        self.assertEqual(body([], fc=forecast(rain=60)), ["출근: 비 60% → 우산 챙기기"])
        self.assertEqual(body([], fc=forecast(rain=59)), [])
        self.assertEqual(lines_for([], fc=forecast(rain=70))[0], HEAD)

    def test_rain_code_counts_even_with_low_percent(self):
        self.assertEqual(body([], fc=forecast(rain=10, code=63)), ["출근: 비 → 우산 챙기기"])
        self.assertEqual(body([], fc=forecast(rain=10, code=3)), [])

    def test_gale_boundary(self):
        self.assertEqual(body([], fc=forecast(wind=14)), ["출근: 강풍 → 강풍 주의"])
        self.assertEqual(body([], fc=forecast(wind=13.9)), [])

    def test_rain_and_gale_with_first_event(self):
        out = body([ev("08:30", "조회"), ev("13:00", "회의")], fc=forecast(rain=70, wind=15))
        self.assertEqual(out, ["출근: 비 70%·강풍 → 우산 대신 우비, 첫 일정 08:30이라 10분 일찍 출발"])

    def test_snow_and_freezing(self):
        out = body([ev("08:30", "조회")], fc=forecast(low=-2, rain=10, code=73))
        self.assertEqual(out, ["출근: 눈·영하 → 미끄럼 주의, 첫 일정 08:30이라 15분 일찍 출발"])

    def test_freezing_needs_precipitation_and_low_at_or_below_zero(self):
        self.assertEqual(body([], fc=forecast(low=-2, rain=10, code=1, prev_low=-2)), [])
        self.assertEqual(body([], fc=forecast(low=0, rain=80, prev_low=0)), ["출근: 비 80%·영하 → 우산 챙기기, 미끄럼 주의"])
        self.assertEqual(body([], fc=forecast(low=1, rain=80, prev_low=1)), ["출근: 비 80% → 우산 챙기기"])

    def test_first_event_ignores_all_day_and_running_ones(self):
        running = gcal.Event("07:00", "연수", D(14), began=D(13))
        out = body([ev("종일", "행사"), running, ev("09:15", "수업")], fc=forecast(rain=80))
        self.assertEqual(out, ["출근: 비 80% → 우산 챙기기, 첫 일정 09:15이라 5분 일찍 출발"])

    def test_no_event_no_departure_hint(self):
        self.assertNotIn("출발", body([], fc=forecast(rain=80))[0])


class EventDirectionTests(unittest.TestCase):
    def test_rain_and_dust_example(self):
        out = body(GAME, fc=forecast(rain=70), aq=air("2026-07-14", pm10=90, pm25=10))
        self.assertEqual(out[1], "14:00 체육대회: 비 70%·미세먼지 나쁨 → 실내 대체 또는 일정 조정 검토")

    def test_heat_only_goes_to_mornings(self):
        out = body([ev("10:00", "현장체험")], fc=forecast(high=34))
        self.assertEqual(out[0], "10:00 현장체험: 최고 34℃ → 오전 위주로, 물·그늘 준비")

    def test_heat_boundary(self):
        self.assertEqual(len(body(GAME, fc=forecast(high=33))), 1)     # the event line; heat is then not repeated
        self.assertEqual(body(GAME, fc=forecast(high=32)), [])

    def test_gale_and_cold(self):
        out = body(GAME, fc=forecast(wind=14))
        self.assertEqual(out[1], "14:00 체육대회: 강풍 → 실내 대체 또는 일정 조정 검토")
        out = body(GAME, fc=forecast(low=-5, high=0, prev_low=-5))
        self.assertEqual(out, ["14:00 체육대회: 최저 -5℃ → 보온 준비, 활동 시간 단축"])
        self.assertEqual(body(GAME, fc=forecast(low=-4, high=0, prev_low=-4)), [])

    def test_no_risk_means_the_event_is_not_mentioned(self):
        self.assertEqual(body(GAME, fc=forecast()), [])

    def test_indoor_events_are_never_mentioned(self):
        out = body([ev("14:00", "교직원 회의")], fc=forecast(rain=90, high=38))
        self.assertFalse(any("회의" in x for x in out))

    def test_all_day_location_and_end_time_use_gcal_line(self):
        out = body([ev("종일", "현장체험학습", location="국립공원")], fc=forecast(rain=80))
        self.assertEqual(out[1], "종일 현장체험학습 @국립공원: 비 80% → 실내 대체 또는 일정 조정 검토")
        e = ev("14:00", "체육대회", end="15:30", end_day=D(14))
        self.assertTrue(body([e], fc=forecast(rain=80))[1].startswith("14:00~15:30 체육대회: "))

    def test_at_most_two_event_lines_with_a_remainder_note(self):
        games = [ev(f"{h}:00", f"체육{h}") for h in (9, 10, 11, 12)]
        out = body(games, fc=forecast(rain=80))
        self.assertEqual(len(out), 3)                       # commute + 2 events
        self.assertIn("체육9", out[1])
        self.assertTrue(out[2].endswith("(외 2건)"))
        self.assertTrue(body(games[:3], fc=forecast(rain=80))[2].endswith("(외 1건)"))
        self.assertNotIn("외", body(games[:2], fc=forecast(rain=80))[2])

    def test_keywords_configurable_and_width_case_folded(self):
        e = [ev("10:00", "ＳＨＯＷ 리허설")]
        self.assertEqual(len(body(e, fc=forecast(rain=90))), 1)                       # commute line only
        self.assertEqual(len(body(e, fc=forecast(rain=90), outdoor_keywords=["show"])), 2)

    def test_default_keywords(self):
        self.assertEqual(Config().outdoor_keywords,
                         ["체육", "운동회", "체험", "소풍", "야외", "현장", "견학", "수련", "캠프", "등산"])
        for title in ("가을 소풍", "수련회", "등산 동아리", "현장 견학"):
            self.assertTrue(wa.is_outdoor(title, Config().outdoor_keywords), title)
        self.assertFalse(wa.is_outdoor("회의", Config().outdoor_keywords))

    def test_duplicates_and_other_days(self):
        e = ev("14:00", "체육대회")
        self.assertEqual(len(body([e, e], fc=forecast(rain=80))), 2)
        out = body([ev("14:00", "체육대회", day=14), ev("15:00", "소풍", day=15)], fc=forecast(rain=80))
        self.assertEqual(len(out), 2)
        self.assertIn("체육대회", out[1])

    def test_running_multi_day_event_is_included(self):
        running = gcal.Event("종일", "수련회", D(14), end_day=D(16), began=D(13))
        self.assertIn("(진행 중, ~07/16) 수련회: 비", body([running], fc=forecast(rain=80))[1])


class ClassroomTests(unittest.TestCase):
    def test_dust_grades_and_boundaries(self):
        self.assertEqual(body([], aq=air("2026-07-14", pm10=81, pm25=10)),
                         ["교실: 미세먼지 나쁨 → 환기는 짧게, 실외 활동 자제"])
        self.assertTrue(body([], aq=air("2026-07-14", pm10=10, pm25=36))[0].startswith("교실: 미세먼지 나쁨"))
        self.assertEqual(body([], aq=air("2026-07-14", pm10=80, pm25=35)), [])
        self.assertEqual(body([], aq=air("2026-07-14", pm10=151, pm25=10)),
                         ["교실: 미세먼지 매우나쁨 → 환기는 짧게, 실외 활동 자제"])
        self.assertTrue(body([], aq=air("2026-07-14", pm10=10, pm25=76))[0].startswith("교실: 미세먼지 매우나쁨"))

    def test_heat_and_cold(self):
        self.assertEqual(body([], fc=forecast(high=33)), ["교실: 폭염 → 냉방·수분 섭취 안내"])
        self.assertEqual(body([], fc=forecast(low=-5, high=0, prev_low=-5)), ["교실: 한파 → 등교 시 보온 안내"])
        self.assertEqual(body([], fc=forecast(low=-4, high=0, prev_low=-4)), [])

    def test_combined_in_one_line(self):
        out = body([], fc=forecast(high=35), aq=air("2026-07-14", pm10=90, pm25=10))
        self.assertEqual(out, ["교실: 미세먼지 나쁨·폭염 → 환기는 짧게, 실외 활동 자제, 냉방·수분 섭취 안내"])

    def test_factor_already_named_in_an_event_line_is_not_repeated(self):
        out = body(GAME, fc=forecast(high=35), aq=air("2026-07-14", pm10=90, pm25=10))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("14:00 체육대회: 미세먼지 나쁨·최고 35℃"))

    def test_other_factors_still_get_their_line(self):
        # the event carries dust only (not outdoor-relevant heat is absent); classroom adds nothing new here
        out = body(GAME, fc=forecast(), aq=air("2026-07-14", pm10=90, pm25=10))
        self.assertEqual(len(out), 1)
        # an indoor-only day: the classroom line is the only one
        out = body([ev("09:00", "회의")], fc=forecast(), aq=air("2026-07-14", pm10=90, pm25=10))
        self.assertEqual(out, ["교실: 미세먼지 나쁨 → 환기는 짧게, 실외 활동 자제"])


class PrepTests(unittest.TestCase):
    def test_drop_and_range_example(self):
        out = body([], fc=forecast(low=-3, high=10, prev_low=5))
        self.assertEqual(out, ["준비: 아침 -3℃(전날보다 8℃↓)·일교차 13℃ → 겉옷"])

    def test_drop_boundary(self):
        self.assertEqual(body([], fc=forecast(low=4, high=10, prev_low=11)), ["준비: 아침 4℃(전날보다 7℃↓) → 겉옷"])
        self.assertEqual(body([], fc=forecast(low=5, high=10, prev_low=11)), [])
        self.assertEqual(body([], fc=forecast(low=20, high=25, prev_low=10)), [])

    def test_range_boundary(self):
        self.assertEqual(body([], fc=forecast(low=10, high=22, prev_low=10)), ["준비: 일교차 12℃ → 겉옷"])
        self.assertEqual(body([], fc=forecast(low=10, high=21, prev_low=10)), [])

    def test_missing_previous_low_skips_only_the_drop(self):
        f = forecast(low=-3, high=10)
        f["daily"]["temperature_2m_min"][0] = None
        self.assertEqual(body([], fc=f), ["준비: 일교차 13℃ → 겉옷"])

    def test_rain_is_not_repeated_after_the_commute_line(self):
        out = body([], fc=forecast(low=3, high=17, prev_low=11, rain=80))
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0].startswith("출근: 비 80%"))
        self.assertEqual(out[1], "준비: 아침 3℃(전날보다 8℃↓)·일교차 14℃ → 겉옷")
        self.assertNotIn("우산", out[1])


class SectionTests(unittest.TestCase):
    def test_calm_weather_omits_the_section(self):
        self.assertEqual(lines_for(GAME, fc=forecast()), [])

    def test_everything_at_once_with_events(self):
        # event lines already carry dust / cold, so the classroom line has nothing new to add
        games = [ev("08:30", "체육대회"), ev("10:00", "소풍")]
        out = body(games, fc=forecast(low=-8, high=4, rain=80, prev_low=2, code=73, wind=15),
                   aq=air("2026-07-14", pm10=200, pm25=10))
        self.assertEqual(len(out), 4)
        self.assertTrue(out[0].startswith("출근: "))
        self.assertTrue(out[1].startswith("08:30 체육대회: ") and out[2].startswith("10:00 소풍: "))
        self.assertTrue(out[3].startswith("준비: "))
        self.assertNotIn("우산", out[3])

    def test_everything_at_once_without_outdoor_events(self):
        out = body([ev("08:30", "회의")], fc=forecast(low=-8, high=4, rain=80, prev_low=2, code=73, wind=15),
                   aq=air("2026-07-14", pm10=200, pm25=10))
        self.assertEqual([x.split(":")[0] for x in out], ["출근", "교실", "준비"])
        self.assertIn("미세먼지 매우나쁨·한파", out[1])

    def test_cap_is_enforced_by_the_section(self):
        with mock.patch.object(wa, "build_lines", return_value=["x"] * 9):
            self.assertEqual(len(lines_for([])), 1 + 5 + 1)


class FailureTests(unittest.TestCase):
    def test_air_error_keeps_the_forecast_rules(self):
        net = FakeNet(forecast(rain=80), aq_error=netutil.NetError("HTTP 500", status=500))
        with self.assertLogs("aide.weather", level="WARNING"):
            out = wa.section(cfg_of(), MORN, [ev("14:00", "체육대회")], 0, get=net)
        self.assertEqual(out[1:-1], ["출근: 비 80% → 우산 챙기기, 첫 일정 14:00이라 5분 일찍 출발",
                                     "14:00 체육대회: 비 80% → 실내 대체 또는 일정 조정 검토"])

    def test_forecast_error_keeps_the_air_rules(self):
        net = FakeNet(fc_error=netutil.NetError("연결 실패: URLError"), aq=air("2026-07-14", 200, 10))
        with self.assertLogs("aide.weather", level="WARNING"):
            out = wa.section(cfg_of(), MORN, [ev("14:00", "체육대회")], 0, get=net)
        self.assertEqual(out[1:-1], ["14:00 체육대회: 미세먼지 매우나쁨 → 실내 대체 또는 일정 조정 검토"])

    def test_bad_air_answer_means_no_air_rules(self):
        net = FakeNet(forecast(rain=10), aq={"hourly": "junk"})
        self.assertEqual(wa.section(cfg_of(), MORN, [ev("14:00", "체육대회")], 0, get=net), [])

    def test_both_failing_gives_nothing(self):
        net = FakeNet(fc_error=netutil.NetError("x"), aq_error=netutil.NetError("y"))
        with self.assertLogs("aide.weather", level="WARNING"):
            self.assertEqual(wa.section(cfg_of(), MORN, [ev("14:00", "체육대회")], 0, get=net), [])

    def test_unexpected_error_never_escapes(self):
        with mock.patch.object(wa, "build_lines", side_effect=RuntimeError("x")), \
                self.assertLogs("aide.weather", level="ERROR"):
            self.assertEqual(wa.section(cfg_of(), MORN, [], 0, get=FakeNet(forecast(), air("2026-07-14"))), [])


class OffTests(unittest.TestCase):
    def test_off_makes_no_calls(self):
        net = FakeNet(forecast(rain=90))
        self.assertFalse(Config().weather_alerts)
        self.assertEqual(wa.section(Config(weather_enabled=True), MORN, [ev("14:00", "체육대회")], 0, get=net), [])
        self.assertEqual(net.urls, [])

    def test_needs_weather_enabled_even_if_forced(self):
        net = FakeNet(forecast(rain=90))
        c = Config(weather_alerts=True, weather_enabled=False)
        self.assertEqual(wa.section(c, MORN, [ev("14:00", "체육대회")], 0, get=net), [])
        self.assertEqual(net.urls, [])

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"weather_alerts": true}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)
            p.write_text('{"weather_alerts": true, "weather_enabled": true}', encoding="utf-8")
            self.assertTrue(load_config(p).weather_alerts)
            for bad in ('"abc"', '[""]', '[1]', '["' + "가" * 21 + '"]', '["a"]' if False else '"x"'):
                p.write_text('{"outdoor_keywords": %s}' % bad, encoding="utf-8")
                with self.assertRaises(ConfigError, msg=bad):
                    load_config(p)
            p.write_text('{"weather_alerts": "yes"}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_default_message_is_unchanged(self):
        with mock.patch.object(wa, "section", side_effect=AssertionError("not called")):
            m = morning.build_message(Config(watch_repos=[]), MORN)
            e = evening.build_message(Config(watch_repos=[], evening_hour=20), EVE)
        self.assertNotIn("날씨에 따른 안내", m + e)


# ---- messages ---------------------------------------------------------------------------------------

def patched(events):
    return mock.patch("aide.accounts.gather_events", side_effect=lambda c, f, o: (list(events), [], 1))


class MorningTests(unittest.TestCase):
    def test_today_section_after_the_today_block_and_identical_for_send(self):
        cfg = cfg_of()
        net = FakeNet(forecast(rain=80), air("2026-07-14"))
        fa = functools.partial(wa.section, get=net)
        today = [ev("14:00", "체육대회")]
        with patched(today), mock.patch("aide.google_auth.access_token", return_value="T"):
            preview = morning.build_message(cfg, MORN, fetch_weather=lambda a, b: weather.Weather("맑음", 1, 2, 3),
                                            fetch_alerts=fa)
            sent = []
            with mock.patch.dict("os.environ", ENV), tempfile.TemporaryDirectory() as d:
                cfg.state_path = str(Path(d) / "s.json")
                morning.run(cfg, send=True, now=MORN, sender=lambda m, **k: sent.append(m),
                            fetch_weather=lambda a, b: weather.Weather("맑음", 1, 2, 3), fetch_alerts=fa)
                cfg.state_path = str(Path(d) / "s2.json")
                out = io.StringIO()
                with redirect_stdout(out):
                    morning.run(cfg, send=False, now=MORN, fetch_weather=lambda a, b: weather.Weather("맑음", 1, 2, 3),
                                fetch_alerts=fa)
        self.assertIn("■ 날씨에 따른 안내\n출근: 비 80% → 우산 챙기기, 첫 일정 14:00이라 5분 일찍 출발\n"
                      "14:00 체육대회: 비 80% → 실내 대체 또는 일정 조정 검토", preview)
        self.assertLess(preview.index("■ 오늘 일정"), preview.index("■ 날씨에 따른 안내"))
        self.assertEqual(sent[0], preview)
        self.assertEqual(out.getvalue().strip(), preview)

    def test_morning_uses_today_and_the_events_already_gathered(self):
        calls = []
        cfg = cfg_of()
        today = [ev("14:00", "체육대회")]
        with patched(today), mock.patch("aide.google_auth.access_token", return_value="T"):
            morning.build_message(cfg, MORN, fetch_weather=lambda a, b: weather.Weather("맑음", 1, 2, 3),
                                  fetch_alerts=lambda c, n, e, off: calls.append((off, list(e))) or [])
        self.assertEqual(calls, [(0, today)])

    def test_failure_drops_only_the_section(self):
        cfg = cfg_of()

        def boom(*a):
            raise RuntimeError("x")

        with patched([]), mock.patch("aide.google_auth.access_token", return_value="T"), \
                self.assertLogs("aide.morning", level="ERROR"):
            text = morning.build_message(cfg, MORN, fetch_weather=lambda a, b: weather.Weather("맑음", 1, 2, 3),
                                         fetch_alerts=boom)
        self.assertTrue(text.startswith("좋은 아침이에요"))
        self.assertNotIn("날씨에 따른 안내", text)

    def test_fetch_runs_outside_the_state_lock(self):
        from aide.state import locked
        cfg = cfg_of()
        seen = []
        with tempfile.TemporaryDirectory() as d, patched([]), mock.patch.dict("os.environ", ENV), \
                mock.patch("aide.google_auth.access_token", return_value="T"):
            cfg.state_path = str(Path(d) / "s.json")

            def fa(c, n, e, off):
                try:
                    with locked(Path(cfg.state_path), timeout=0.3):
                        seen.append(False)
                except Exception:  # noqa: BLE001
                    seen.append(True)
                return []

            morning.run(cfg, send=True, now=MORN, sender=lambda m, **k: None,
                        fetch_weather=lambda a, b: weather.Weather("맑음", 1, 2, 3), fetch_alerts=fa)
        self.assertEqual(seen, [False])


class EveningTests(unittest.TestCase):
    def cfg(self):
        return cfg_of(evening_hour=20)

    def build(self, events, now, fa):
        with patched(events), mock.patch("aide.google_auth.access_token", return_value="T"):
            return evening.build_message(self.cfg(), now, fetch_alerts=fa)

    def test_uses_tomorrow(self):
        calls = []
        tomorrow = [ev("10:00", "소풍", day=15)]
        self.build(tomorrow, EVE, lambda c, n, e, off: calls.append((off, list(e))) or [])
        self.assertEqual(calls, [(1, tomorrow)])

    def test_tomorrows_forecast_and_events(self):
        net = FakeNet(forecast(rain=75, offset=1), air("2026-07-15"))
        text = self.build([ev("10:00", "소풍", day=15)], EVE, functools.partial(wa.section, get=net))
        self.assertIn("■ 내일 일정\n• 10:00 소풍\n\n■ 날씨에 따른 안내\n출근: 비 75% → 우산 챙기기, 첫 일정 10:00이라 5분 일찍 출발\n"
                      "10:00 소풍: 비 75% → 실내 대체 또는 일정 조정 검토", text)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(net.urls[0]).query)
        self.assertEqual(q["forecast_days"], ["2"])        # offset 1 -> today + tomorrow
        # air quality is looked up for tomorrow's date, not today's
        self.assertIn("air-quality-api.open-meteo.com", net.urls[1])

    def test_sunday_weekly_mode_only_looks_at_monday(self):
        net = FakeNet(forecast(rain=80, offset=1), air("2026-07-20"))
        week = [ev("10:00", "월요 체육", day=20), ev("10:00", "화요 소풍", day=21), ev("종일", "수요 캠프", day=22)]
        text = self.build(week, SUN, functools.partial(wa.section, get=net))
        self.assertIn("10:00 월요 체육: 비 80%", text)
        self.assertNotIn("화요 소풍: ", text)
        self.assertNotIn("수요 캠프: ", text)

    def test_sunday_uses_mondays_air_quality(self):
        net = FakeNet(forecast(offset=1), air("2026-07-20", pm10=300, pm25=10))
        text = self.build([], SUN, functools.partial(wa.section, get=net))
        self.assertIn("교실: 미세먼지 매우나쁨 → 환기는 짧게, 실외 활동 자제", text)

    def test_no_extra_calendar_calls(self):
        calls = {"n": 0}

        def fetch_events(token, day):
            calls["n"] += 1
            return []

        def run(alerts):
            calls["n"] = 0
            cfg = cfg_of(evening_hour=20, weather_alerts=alerts)
            with mock.patch("aide.google_auth.access_token", return_value="T"):
                evening.build_message(cfg, EVE, fetch_events=fetch_events,
                                      fetch_alerts=functools.partial(wa.section, get=FakeNet(forecast(offset=1),
                                                                                              air("2026-07-15"))))
            return calls["n"]

        self.assertEqual(run(True), run(False))

    def test_dry_run_and_send_are_identical(self):
        fa = functools.partial(wa.section, get=FakeNet(forecast(rain=80, offset=1), air("2026-07-15")))
        cfg = self.cfg()
        events = [ev("10:00", "소풍", day=15)]
        sent = []
        with patched(events), mock.patch("aide.google_auth.access_token", return_value="T"), \
                mock.patch.dict("os.environ", ENV), tempfile.TemporaryDirectory() as d:
            cfg.state_path = str(Path(d) / "s.json")
            evening.run(cfg, send=True, now=EVE, sender=lambda m, **k: sent.append(m), fetch_alerts=fa)
            out = io.StringIO()
            with redirect_stdout(out):
                evening.run(cfg, send=False, now=EVE, fetch_alerts=fa)
        self.assertEqual(out.getvalue().strip(), sent[0])
        self.assertIn("날씨에 따른 안내", sent[0])

    def test_failure_drops_only_the_section(self):
        def boom(*a):
            raise RuntimeError("x")

        with self.assertLogs("aide.evening", level="ERROR"):
            text = self.build([ev("10:00", "소풍", day=15)], EVE, boom)
        self.assertIn("■ 내일 일정", text)
        self.assertNotIn("날씨에 따른 안내", text)


if __name__ == "__main__":
    unittest.main()
