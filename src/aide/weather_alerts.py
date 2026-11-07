"""Weather based guidance for the morning greeting (today) and the evening wrap-up (tomorrow).

Rule based, no LLM; every sentence is fixed text. Inputs are numbers from Open-Meteo (forecast + air
quality) and the events the message already gathered (only their titles are matched against
`outdoor_keywords`; titles are shown through `gcal.line`; no extra calendar call).

The section `■ 날씨에 따른 안내` has at most MAX_LINES lines, one per bundle, in this order:
  출근 (rain / snow / gale / freezing)      -> how to commute, leave earlier when a first event exists
  일정 (outdoor events, at most two lines)  -> what to do with the event (move indoors, mornings, warm-up)
  교실 (dust / heat / cold)                 -> classroom routine
  준비 (temperature drop, day range, rain)  -> clothes and things to bring
A factor already named in an earlier bundle is not repeated in the classroom / bring lines.
Every failure only removes the rules that needed the data; nothing here may raise into a message.
"""
from __future__ import annotations

import logging
import unicodedata
from datetime import date, datetime, timedelta
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlencode

from . import gcal, netutil, weather

log = logging.getLogger("aide.weather")

AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"   # the one new host (no key)
RAIN_PCT = 60        # chance of rain at or above this counts as rain (a rain weather code does too)
GALE_MS = 14         # daily max wind (m/s) at or above this
HEAT_C = 33          # forecast high at or above this
COLD_C = -5          # forecast low at or below this
DROP_C = 7           # low at least this much below the previous day's low
RANGE_C = 12         # high minus low at or above this
MAX_LINES = 5
MAX_EVENT_LINES = 2
GRADES = ("보통", "나쁨", "매우나쁨")   # index = severity; "보통" also stands for "좋음"
RAIN_CODES = frozenset({51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99})
SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})


def pm_grade(pm10: Optional[float], pm25: Optional[float]) -> int:
    """0 보통 or better, 1 나쁨, 2 매우나쁨 (Korean Ministry of Environment bands, worse of the two)."""
    g = 0
    if pm10 is not None:
        g = max(g, 2 if pm10 >= 151 else 1 if pm10 >= 81 else 0)
    if pm25 is not None:
        g = max(g, 2 if pm25 >= 76 else 1 if pm25 >= 36 else 0)
    return g


def _number(x) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x else None


def air_max(lat: float, lon: float, day: date, get: Callable = netutil.get_json) -> Tuple[Optional[float], Optional[float]]:
    """(max PM10, max PM2.5) over the hours of `day`; (None, None) when the answer is not in the expected
    shape or has no value for that day. Raises NetError only when the request itself fails."""
    q = urlencode({"latitude": lat, "longitude": lon, "hourly": "pm10,pm2_5", "timezone": "Asia/Seoul",
                   "forecast_days": 2})
    data = get(f"{AIR_URL}?{q}")
    try:
        h = data["hourly"]
        times, pm10, pm25 = h["time"], h["pm10"], h["pm2_5"]
        if not (isinstance(times, list) and isinstance(pm10, list) and isinstance(pm25, list)):
            return None, None
        prefix = day.isoformat()
        a: List[float] = []
        b: List[float] = []
        for i, t in enumerate(times):
            if isinstance(t, str) and t.startswith(prefix):
                v10 = _number(pm10[i]) if i < len(pm10) else None
                v25 = _number(pm25[i]) if i < len(pm25) else None
                if v10 is not None:
                    a.append(v10)
                if v25 is not None:
                    b.append(v25)
        return (max(a) if a else None), (max(b) if b else None)
    except (KeyError, TypeError, IndexError, AttributeError):
        return None, None


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def is_outdoor(title: str, keywords: List[str]) -> bool:
    t = _fold(title)
    return any(_fold(k) in t for k in keywords if k and k.strip())


def _on(e, target: date) -> bool:
    return e.day is None or e.day == target


def first_time(events: list, target: date) -> Optional[str]:
    """Earliest "HH:MM" start among the day's timed events (all-day and still-running ones do not count)."""
    times = [e.when for e in events
             if _on(e, target) and len(e.when) == 5 and e.when[2] == ":" and not gcal._continuing(e, target)]
    return min(times) if times else None


def build_lines(cfg, events: list, target: date, d: Optional["weather.Detail"],
                air: Tuple[Optional[float], Optional[float]]) -> List[str]:
    """The guidance lines (not yet capped). `d` / `air` may be None / (None, None): only the rules that
    have their data run."""
    grade = pm_grade(*air)
    dust = f"미세먼지 {GRADES[grade]}" if grade >= 1 else ""
    rain = snow = gale = freeze = heat = cold = False
    rain_text = "비"
    if d is not None:
        rain = d.rain_pct >= RAIN_PCT or d.code in RAIN_CODES
        rain_text = f"비 {d.rain_pct}%" if d.rain_pct >= RAIN_PCT else "비"
        snow = d.code in SNOW_CODES
        gale = d.wind is not None and d.wind >= GALE_MS
        freeze = d.low <= 0 and (rain or snow)
        heat = d.high >= HEAT_C
        cold = d.low <= COLD_C
    out: List[str] = []
    used = set()

    # 1. commute
    parts = [t for on, t in ((rain, rain_text), (snow, "눈"), (gale, "강풍"), (freeze, "영하")) if on]
    if parts:
        advice = ["우산 대신 우비" if rain and gale else "우산 챙기기" if rain else ""]
        if snow or freeze:
            advice.append("미끄럼 주의")
        elif gale and not rain:
            advice.append("강풍 주의")
        first = first_time(events, target)
        if first:
            mins = 15 if (snow or freeze) else 10 if (rain and gale) else 5
            advice.append(f"첫 일정 {first}이라 {mins}분 일찍 출발")
        out.append("출근: " + "·".join(parts) + " → " + ", ".join(x for x in advice if x))
        used |= {k for k, on in (("rain", rain), ("snow", snow), ("gale", gale), ("freeze", freeze)) if on}

    # 2. outdoor events
    risky: List[Tuple[str, set]] = []
    seen = set()
    for e in events:
        if not _on(e, target) or not is_outdoor(e.title, cfg.outdoor_keywords):
            continue
        text = gcal.line(e, target)
        if text in seen:
            continue
        seen.add(text)
        f: List[str] = []
        keys = set()
        if rain:
            f.append(rain_text)
            keys.add("rain")
        if dust:
            f.append(dust)
            keys.add("dust")
        if heat:
            f.append(f"최고 {d.high}℃")
            keys.add("heat")
        if gale:
            f.append("강풍")
            keys.add("gale")
        if cold:
            f.append(f"최저 {d.low}℃")
            keys.add("cold")
        if not f:
            continue
        if keys & {"rain", "dust", "gale"}:
            act = "실내 대체 또는 일정 조정 검토"
        elif "heat" in keys:
            act = "오전 위주로, 물·그늘 준비"
        else:
            act = "보온 준비, 활동 시간 단축"
        risky.append((f"{text}: {'·'.join(f)} → {act}", keys))
    for line, keys in risky[:MAX_EVENT_LINES]:
        out.append(line)
        used |= keys
    if len(risky) > MAX_EVENT_LINES:
        out[-1] += f" (외 {len(risky) - MAX_EVENT_LINES}건)"

    # 3. classroom
    cparts, cacts = [], []
    if dust and "dust" not in used:
        cparts.append(dust)
        cacts.append("환기는 짧게, 실외 활동 자제")
    if heat and "heat" not in used:
        cparts.append("폭염")
        cacts.append("냉방·수분 섭취 안내")
    if cold and "cold" not in used:
        cparts.append("한파")
        cacts.append("등교 시 보온 안내")
    if cparts:
        out.append("교실: " + "·".join(cparts) + " → " + ", ".join(cacts))

    # 4. clothes / things to bring
    pparts, pacts = [], []
    if d is not None and d.prev_low is not None and d.prev_low - d.low >= DROP_C:
        pparts.append(f"아침 {d.low}℃(전날보다 {d.prev_low - d.low}℃↓)")
        pacts.append("겉옷")
    if d is not None and d.high - d.low >= RANGE_C:
        pparts.append(f"일교차 {d.high - d.low}℃")
        if "겉옷" not in pacts:
            pacts.append("겉옷")
    if rain and "rain" not in used:
        pparts.append(rain_text)
        pacts.append("우산")
    if pparts:
        out.append("준비: " + "·".join(pparts) + " → " + ", ".join(pacts))
    return out


def section(cfg, now: datetime, events: list, offset: int, *,
            get: Callable = netutil.get_json) -> List[str]:
    """`■ 날씨에 따른 안내` lines (ending with a blank line) for today + `offset` days (0 morning, 1 evening),
    or []. Never raises. `events` are the ones the message already holds."""
    try:
        if not (getattr(cfg, "weather_alerts", False) and cfg.weather_enabled):
            return []
        target = now.date() + timedelta(days=offset)
        d = None
        try:
            d = weather.detail(cfg.latitude, cfg.longitude, offset, get)
        except netutil.NetError as e:
            log.warning("날씨 안내용 예보 조회 실패: %s", e)
        air: Tuple[Optional[float], Optional[float]] = (None, None)
        try:
            air = air_max(cfg.latitude, cfg.longitude, target, get)
        except netutil.NetError as e:
            log.warning("대기질 조회 실패: %s", e)
        lines = build_lines(cfg, events, target, d, air)[:MAX_LINES]
        return ["■ 날씨에 따른 안내", *lines, ""] if lines else []
    except Exception:  # noqa: BLE001
        log.exception("날씨 안내 처리 중 오류")
        return []
