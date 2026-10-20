"""Today's weather from Open-Meteo (no key; non-commercial use). Read-only, tiny."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlencode

from . import netutil

URL = "https://api.open-meteo.com/v1/forecast"
UMBRELLA_PCT = 50
_WMO = {0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "안개",
        51: "이슬비", 53: "이슬비", 55: "이슬비", 56: "어는 이슬비", 57: "어는 이슬비",
        61: "비", 63: "비", 65: "강한 비", 66: "어는 비", 67: "어는 비",
        71: "눈", 73: "눈", 75: "많은 눈", 77: "싸락눈", 80: "소나기", 81: "소나기", 82: "강한 소나기",
        85: "눈 소나기", 86: "눈 소나기", 95: "뇌우", 96: "뇌우(우박)", 99: "뇌우(우박)"}


@dataclass(frozen=True)
class Weather:
    text: str
    low: int
    high: int
    rain_pct: int

    def line(self) -> str:
        s = f"{self.text}, {self.low}~{self.high}°C, 강수확률 {self.rain_pct}%"
        return s + (" · 우산 챙기세요" if self.rain_pct >= UMBRELLA_PCT else "")


MAX_OFFSET = 6   # Open-Meteo is asked for at most 7 days


def today(lat: float, lon: float, get: Callable = netutil.get_json) -> Weather:
    return on_day(lat, lon, 0, get)


def on_day(lat: float, lon: float, offset: int, get: Callable = netutil.get_json) -> Weather:
    """Forecast for today + `offset` days (0..MAX_OFFSET)."""
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= MAX_OFFSET:
        raise netutil.NetError("날씨는 오늘부터 7일 이내만 조회할 수 있습니다.")
    q = urlencode({
        "latitude": lat, "longitude": lon, "timezone": "Asia/Seoul", "forecast_days": offset + 1,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
    })
    d = get(f"{URL}?{q}").get("daily", {})
    try:
        return Weather(
            _WMO.get(int(d["weather_code"][offset]), "날씨 정보"),
            round(d["temperature_2m_min"][offset]), round(d["temperature_2m_max"][offset]),
            round(d["precipitation_probability_max"][offset] or 0),
        )
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise netutil.NetError("날씨 응답 형식이 예상과 다릅니다.") from None
