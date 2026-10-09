from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Account:
    """One Google account whose calendar/mail is read. `name` is the label shown in messages
    ("" when only one account is configured); `email` is only used to build mail links."""

    name: str
    token_path: Path
    email: str = ""


_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MAX_ACCOUNTS = 8
# Stage 3b command names. Fixed set on purpose: `commands` in config.json can only
# pick a subset of these, never introduce a new one.
COMMAND_NAMES = ("오늘", "일정", "마감", "메일")


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
    # Several Google accounts: [{"name": "개인", "token_path": "data/google_token.json", "email": "me@gmail.com"}, ...]
    # Empty = the single account at google_token_path (nothing changes for existing setups).
    google_accounts: List[dict] = field(default_factory=list)
    # Stage 3b: read-only commands the owner may type in the bot's own private chat
    # (e.g. ["오늘", "일정", "마감", "메일"]). Empty (default) = feature off, behaviour
    # unchanged from stage 3a (the bot never asks Telegram for plain chat messages at all).
    commands: List[str] = field(default_factory=list)
    # Stage 2a: LLM importance ranking (high/normal/low) for the notification digest.
    # Off by default; `llm_enabled: false` (or no ANTHROPIC_API_KEY) means the digest
    # is built exactly as before. See ARCHITECTURE.md "LLM 판단 (2a)".
    llm_enabled: bool = False
    llm_model: str = "claude-haiku-5-5"
    llm_profile: str = ""   # trusted one-line description the owner writes, e.g. role + what matters
    llm_sources: List[str] = field(default_factory=lambda: ["mail", "notice", "news"])
    llm_exclude_accounts: List[str] = field(default_factory=list)   # Google account names never sent to the LLM
    llm_max_calls_per_day: int = 30

    def resolved_state_path(self) -> Path:
        return _resolve(self.state_path)

    def resolved_google_client(self) -> Path:
        return _resolve(self.google_client_path)

    def resolved_google_token(self) -> Path:
        return _resolve(self.google_token_path)

    def accounts(self) -> List[Account]:
        if not self.google_accounts:
            return [Account("", self.resolved_google_token())]
        return [Account(a["name"], _resolve(a["token_path"]), a.get("email", "")) for a in self.google_accounts]


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
    _check_accounts(cfg.google_accounts)
    _check_commands(cfg.commands)
    _check_llm(cfg)
    return cfg


def _check_commands(commands) -> None:
    if not isinstance(commands, list) or any(not isinstance(c, str) for c in commands):
        raise ConfigError("commands 는 문자열 목록이어야 합니다.")
    unknown = sorted(set(commands) - set(COMMAND_NAMES))
    if unknown:
        raise ConfigError(f"commands 에 알 수 없는 명령이 있습니다: {unknown} (허용: {list(COMMAND_NAMES)})")
    if len(set(commands)) != len(commands):
        raise ConfigError("commands 에 같은 명령이 중복되어 있습니다.")


# Stage 2a sources: "git" is accepted but excluded from the default (git commit text is
# not useful for importance ranking and never leaves the machine unless asked for).
LLM_SOURCES = ("git", "news", "notice", "mail")


def _check_llm(cfg: "Config") -> None:
    if not isinstance(cfg.llm_model, str) or not cfg.llm_model.strip():
        raise ConfigError("llm_model 은 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(cfg.llm_profile, str) or len(cfg.llm_profile) > 500:
        raise ConfigError("llm_profile 은 500자 이하의 문자열이어야 합니다.")
    if not isinstance(cfg.llm_sources, list) or any(s not in LLM_SOURCES for s in cfg.llm_sources):
        raise ConfigError(f"llm_sources 는 {list(LLM_SOURCES)} 중에서만 고를 수 있습니다.")
    if not isinstance(cfg.llm_exclude_accounts, list) or any(not isinstance(a, str) for a in cfg.llm_exclude_accounts):
        raise ConfigError("llm_exclude_accounts 는 문자열 목록이어야 합니다.")
    if (not isinstance(cfg.llm_max_calls_per_day, int) or isinstance(cfg.llm_max_calls_per_day, bool)
            or cfg.llm_max_calls_per_day < 1):
        raise ConfigError("llm_max_calls_per_day 는 1 이상의 정수여야 합니다.")


def _check_accounts(accounts) -> None:
    if not isinstance(accounts, list) or len(accounts) > MAX_ACCOUNTS:
        raise ConfigError(f"google_accounts 는 계정 목록(최대 {MAX_ACCOUNTS}개)이어야 합니다.")
    names, paths = set(), set()
    for a in accounts:
        if not isinstance(a, dict) or set(a) - {"name", "token_path", "email"}:
            raise ConfigError('google_accounts 항목은 {"name", "token_path", "email"(선택)} 만 쓸 수 있습니다.')
        name, path, email = a.get("name"), a.get("token_path"), a.get("email", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 20 or "\n" in name:
            raise ConfigError("google_accounts 의 name 은 20자 이하의 비어 있지 않은 한 줄 글자여야 합니다.")
        if not isinstance(path, str) or not path.strip():
            raise ConfigError(f"google_accounts[{name}] 의 token_path 가 필요합니다.")
        if not isinstance(email, str) or (email and not _EMAIL.match(email)):
            raise ConfigError(f"google_accounts[{name}] 의 email 형식이 올바르지 않습니다.")
        if name in names or path in paths:
            raise ConfigError("google_accounts 의 name 과 token_path 는 서로 달라야 합니다.")
        names.add(name)
        paths.add(path)
