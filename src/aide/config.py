from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    pass


@dataclass
class Config:
    watch_repos: List[str] = field(default_factory=list)
    git_dirty_threshold: int = 5
    news_feeds: List[str] = field(default_factory=list)
    news_keywords: List[str] = field(default_factory=list)
    quiet_start: str = "22:30"
    quiet_end: str = "07:00"
    state_path: str = "data/state.json"
    max_items_per_digest: int = 15
    news_max_age_hours: int = 36
    notice_pages: List[str] = field(default_factory=list)
    notice_keywords: List[str] = field(default_factory=lambda: [
        "공모", "모집", "신청", "접수", "연수", "공고", "대회", "전시회", "지원사업", "발명", "메이커"])
    notice_exclude: List[str] = field(default_factory=list)   # title words to drop (e.g. 임용, 수능)
    notice_max_per_page: int = 5
    mail_enabled: bool = False
    mail_senders: List[str] = field(default_factory=list)   # domains (pen.go.kr) or addresses
    mail_max_items: int = 5
    mail_recent_hours: List[int] = field(default_factory=list)  # e.g. [8, 18]: digest of new Primary mail at those hours
    mail_recent_window_hours: int = 12
    mail_recent_max: int = 5
    mail_block: List[str] = field(default_factory=list)         # senders to leave out of the digest
    evening_hour: Optional[int] = None   # e.g. 20: one evening wrap-up at/after 20:00
    weather_enabled: bool = False
    latitude: float = 35.2      # Busan Dongnae (approx.)
    longitude: float = 129.08
    calendar_enabled: bool = False
    google_client_path: str = "data/google_client.json"
    google_token_path: str = "data/google_token.json"

    def resolved_state_path(self) -> Path:
        return _resolve(self.state_path)

    def resolved_google_client(self) -> Path:
        return _resolve(self.google_client_path)

    def resolved_google_token(self) -> Path:
        return _resolve(self.google_token_path)


def _resolve(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def load_dotenv(path: Optional[Path] = None) -> None:
    """Minimal .env reader (KEY=VALUE per line). Existing environment wins."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: Optional[Path] = None) -> Config:
    path = Path(path) if path else ROOT / "config.json"
    if not path.exists():
        raise ConfigError(
            f"설정 파일이 없습니다: {path}\n"
            "config.example.json 을 config.json 으로 복사한 뒤 수정하세요."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"config.json 형식 오류: {e}") from e
    known = set(Config.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"알 수 없는 설정 키: {sorted(unknown)}")
    cfg = Config(**data)
    if cfg.git_dirty_threshold < 1 or cfg.max_items_per_digest < 1 or cfg.news_max_age_hours < 1 or cfg.notice_max_per_page < 1 or cfg.mail_max_items < 1 or cfg.mail_recent_max < 1 or cfg.mail_recent_window_hours < 1:
        raise ConfigError("git_dirty_threshold / max_items_per_digest / news_max_age_hours 는 1 이상이어야 합니다.")
    if not (-90 <= cfg.latitude <= 90 and -180 <= cfg.longitude <= 180):
        raise ConfigError("latitude / longitude 범위가 올바르지 않습니다.")
    if cfg.evening_hour is not None and (
        not isinstance(cfg.evening_hour, int) or isinstance(cfg.evening_hour, bool) or not 0 <= cfg.evening_hour <= 23
    ):
        raise ConfigError("evening_hour 는 0~23 사이 정수여야 합니다 (예: 20).")
    if any(not isinstance(h, int) or isinstance(h, bool) or not 0 <= h <= 23 for h in cfg.mail_recent_hours):
        raise ConfigError("mail_recent_hours 는 0~23 사이 정수 목록이어야 합니다 (예: [8, 18]).")
    return cfg
