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
    calendars: tuple = ()   # names of the calendars to read; () = only the main one


_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MAX_ACCOUNTS = 8
MAX_CALENDARS_PER_ACCOUNT = 8
# Stage 3b command names. Fixed set on purpose: `commands` in config.json can only
# pick a subset of these, never introduce a new one.
COMMAND_NAMES = ("오늘", "일정", "마감", "메일", "날씨", "할일")


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
    # Stage 2b: an "오늘의 핵심" highlight section (closed-choice: index + reason code
    # only, never free text) on top of the morning/evening message. Off by default;
    # pick which messages get it: "morning" and/or "evening" (the latter also covers
    # the Sunday weekly brief). See ARCHITECTURE.md "LLM 판단 (2b)".
    llm_highlights: List[str] = field(default_factory=list)
    # Preference learning L1 (ARCHITECTURE.md "선호 학습 L1"): the owner's own per-sender mail
    # rules and a "정리" button. Off by default; off also stops every stored rule from applying.
    rules_enabled: bool = False
    # With rules on: fold bulk/list mail (headers say so) that has no rule into one line.
    mail_bulk_fold: bool = False
    # Stage C1 (ARCHITECTURE.md "대화 C1"): free-text questions in the bot's private chat. The model
    # only turns the owner's sentence into ONE closed query (tool + dates + account + keyword) that
    # the fixed read-only command code then runs. Off by default; needs llm_enabled and commands.
    # Turning it on is the owner's consent to send the typed sentence (nothing else) to the LLM.
    chat_enabled: bool = False
    chat_max_per_day: int = 20   # own daily budget, separate from llm_max_calls_per_day
    # Google Tasks (read-only, tasks.readonly): names from google_accounts whose task lists are read.
    # Empty = off. Only these accounts need the extra permission (`google-login --tasks`).
    tasks_accounts: List[str] = field(default_factory=list)

    def resolved_state_path(self) -> Path:
        return _resolve(self.state_path)

    def resolved_google_client(self) -> Path:
        return _resolve(self.google_client_path)

    def resolved_google_token(self) -> Path:
        return _resolve(self.google_token_path)

    def task_accounts(self) -> List[Account]:
        """The accounts whose Google Tasks are read ([] = off). With one unnamed account any entry turns it on."""
        if not self.tasks_accounts:
            return []
        if not self.google_accounts:
            return self.accounts()
        return [a for a in self.accounts() if a.name in self.tasks_accounts]

    def accounts(self) -> List[Account]:
        if not self.google_accounts:
            return [Account("", self.resolved_google_token())]
        return [Account(a["name"], _resolve(a["token_path"]), a.get("email", ""), tuple(a.get("calendars", ())))
                for a in self.google_accounts]


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
    if not isinstance(cfg.rules_enabled, bool) or not isinstance(cfg.mail_bulk_fold, bool):
        raise ConfigError("rules_enabled / mail_bulk_fold 는 true/false 여야 합니다.")
    _check_chat(cfg)
    _check_tasks(cfg)
    if cfg.rules_enabled and cfg.evening_hour is None:
        # The weekly "걸러낸 것" summary in the evening wrap-up is the only place a rule that hides
        # too much shows up, and it cannot be switched off on its own -- so rules need it.
        raise ConfigError("rules_enabled 를 켜려면 evening_hour 도 필요합니다 "
                          "(일요일 저녁 정리의 '걸러낸 것' 요약이 규칙의 안전망입니다).")
    return cfg


def _check_chat(cfg: "Config") -> None:
    if not isinstance(cfg.chat_enabled, bool):
        raise ConfigError("chat_enabled 는 true/false 여야 합니다.")
    if (not isinstance(cfg.chat_max_per_day, int) or isinstance(cfg.chat_max_per_day, bool)
            or not 1 <= cfg.chat_max_per_day <= 200):
        raise ConfigError("chat_max_per_day 는 1~200 사이의 정수여야 합니다.")
    if cfg.chat_enabled and not (cfg.llm_enabled and cfg.commands):
        raise ConfigError("chat_enabled 를 켜려면 llm_enabled 와 commands 도 필요합니다 "
                          "(대화는 /명령과 같은 읽기 전용 조회만 실행합니다).")


def _check_tasks(cfg: "Config") -> None:
    t = cfg.tasks_accounts
    if not isinstance(t, list) or any(not isinstance(n, str) or not n.strip() for n in t) or len(set(t)) != len(t):
        raise ConfigError("tasks_accounts 는 서로 다른 계정 이름의 목록이어야 합니다.")
    names = {a["name"] for a in cfg.google_accounts}
    if cfg.google_accounts:
        unknown = sorted(set(t) - names)
        if unknown:
            raise ConfigError(f"tasks_accounts 에 google_accounts 에 없는 이름이 있습니다: {unknown}")
    elif len(t) > 1:
        raise ConfigError("계정이 하나뿐일 때 tasks_accounts 는 항목 하나만 쓸 수 있습니다.")


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
# Stage 2b: which messages may carry an "오늘의 핵심" section.
LLM_HIGHLIGHT_SCOPES = ("morning", "evening")


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
    if not isinstance(cfg.llm_highlights, list) or any(s not in LLM_HIGHLIGHT_SCOPES for s in cfg.llm_highlights):
        raise ConfigError(f"llm_highlights 는 {list(LLM_HIGHLIGHT_SCOPES)} 중에서만 고를 수 있습니다.")
    if len(set(cfg.llm_highlights)) != len(cfg.llm_highlights):
        raise ConfigError("llm_highlights 에 같은 값이 중복되어 있습니다.")


def _check_accounts(accounts) -> None:
    if not isinstance(accounts, list) or len(accounts) > MAX_ACCOUNTS:
        raise ConfigError(f"google_accounts 는 계정 목록(최대 {MAX_ACCOUNTS}개)이어야 합니다.")
    names, paths = set(), set()
    for a in accounts:
        if not isinstance(a, dict) or set(a) - {"name", "token_path", "email", "calendars"}:
            raise ConfigError('google_accounts 항목은 {"name", "token_path", "email"(선택), "calendars"(선택)} 만 쓸 수 있습니다.')
        name, path, email = a.get("name"), a.get("token_path"), a.get("email", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 20 or "\n" in name:
            raise ConfigError("google_accounts 의 name 은 20자 이하의 비어 있지 않은 한 줄 글자여야 합니다.")
        if not isinstance(path, str) or not path.strip():
            raise ConfigError(f"google_accounts[{name}] 의 token_path 가 필요합니다.")
        if not isinstance(email, str) or (email and not _EMAIL.match(email)):
            raise ConfigError(f"google_accounts[{name}] 의 email 형식이 올바르지 않습니다.")
        cals = a.get("calendars", [])
        if (not isinstance(cals, list) or len(cals) > MAX_CALENDARS_PER_ACCOUNT or len(set(cals)) != len(cals)
                or any(not isinstance(c, str) or not c.strip() or len(c) > 60 or "\n" in c for c in cals)):
            raise ConfigError(f"google_accounts[{name}] 의 calendars 는 서로 다른 캘린더 이름(60자 이하) 목록"
                              f"(최대 {MAX_CALENDARS_PER_ACCOUNT}개)이어야 합니다.")
        if name in names or path in paths:
            raise ConfigError("google_accounts 의 name 과 token_path 는 서로 달라야 합니다.")
        names.add(name)
        paths.add(path)
