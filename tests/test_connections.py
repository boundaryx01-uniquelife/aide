import _bootstrap  # noqa: F401
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from aide import commands, google_auth, health, netutil
from aide.config import Config

NOW = datetime(2026, 11, 7, 9, 0)
CAL, MAIL, TASKS = google_auth.CAL_SCOPE, google_auth.GMAIL_SCOPE, google_auth.TASKS_SCOPE
HDR = health.EXPIRY_HEADER


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def token(self, name, *scopes, raw=None):
        p = self.dir / f"{name}.json"
        p.write_text(raw if raw is not None else json.dumps(
            {"access_token": "AT-SECRET", "refresh_token": "RT-SECRET", "scope": " ".join(scopes)}), encoding="utf-8")
        return str(p)

    def accounts(self, **by_name):
        """by_name: account name -> scopes tuple, or None for 'no token file'."""
        out = []
        for name, scopes in by_name.items():
            path = self.token(name, *scopes) if scopes is not None else str(self.dir / f"{name}.json")
            out.append({"name": name, "token_path": path, "email": f"{name}@example.com"})
        return out

    def render(self, cfg, get=None, env=None):
        get = get or (lambda url, headers=None, timeout=15: ({}, {}))
        return commands.render_connections(cfg, NOW, get=get, environ=env if env is not None else {})


class FormatTests(Base):
    def test_example_from_the_spec(self):
        cfg = Config(
            google_accounts=self.accounts(기본=(CAL, MAIL, TASKS), 개인=(CAL, MAIL, TASKS), 학교=(CAL, MAIL)),
            llm_enabled=True, chat_enabled=True, memory_enabled=True, commands=["오늘"],
            github_accounts=["boundaryx01-uniquelife", "foruniquelife00"],
            news_feeds=["https://n.example/rss"], news_hours=[7, 12], market_queries=["a", "b", "c", "d", "e"],
            weather_enabled=True)
        env = {"ANTHROPIC_API_KEY": "sk-ant-SECRET", "GITHUB_TOKEN_BOUNDARYX01_UNIQUELIFE": "ghp-1",
               "GITHUB_TOKEN_FORUNIQUELIFE00": "ghp-2"}

        def get(url, headers=None, timeout=15):
            if "ghp-1" in headers["Authorization"]:
                return {}, {HDR: "2027-11-07 10:00:00 UTC"}
            return {}, {}

        out = self.render(cfg, get, env)
        self.assertEqual(out, "\n".join([
            "■ 연결 상태",
            "구글: 기본(일정·메일·할 일) · 개인(일정·메일·할 일) · 학교(일정·메일)",
            "AI(Anthropic): 키 있음 · 쓰는 곳: 중요도 판단, 문장으로 묻기, 기억",
            "GitHub: boundaryx01-uniquelife(토큰 있음, 만료 2027-11-07) · foruniquelife00(토큰 있음)",
            "뉴스: 피드 1개 · 7·12시 / 장중 특징주: 검색어 5개",
            "날씨: 켜짐 / 날씨 판단: 꺼짐"]))

    def test_defaults_show_everything_off(self):
        out = self.render(Config(google_accounts=self.accounts(기본=(CAL,))))
        self.assertEqual(out.splitlines(), [
            "■ 연결 상태", "구글: 기본(일정)", "AI(Anthropic): 키 없음 · 쓰는 곳: 없음", "GitHub: 꺼짐",
            "뉴스: 피드 0개 · 매 점검 / 장중 특징주: 꺼짐", "날씨: 꺼짐 / 날씨 판단: 꺼짐"])


class GoogleTests(Base):
    def test_permission_combinations(self):
        cfg = Config(google_accounts=self.accounts(a=(CAL,), b=(MAIL,), c=(TASKS,), d=(CAL, TASKS), e=()))
        line = commands._google_line(cfg)
        self.assertEqual(line, "구글: a(일정) · b(메일) · c(할 일) · d(일정·할 일) · e(권한 없음)")

    def test_missing_token_file_is_not_logged_in(self):
        cfg = Config(google_accounts=self.accounts(a=None, b=(CAL,)))
        self.assertEqual(commands._google_line(cfg), "구글: a(로그인 안 됨) · b(일정)")

    def test_broken_token_file_is_no_permission_not_a_crash(self):
        cfg = Config(google_accounts=[{"name": "a", "token_path": self.token("a", raw="{not json")}])
        self.assertEqual(commands._google_line(cfg), "구글: a(권한 없음)")

    def test_single_unnamed_account_is_called_기본(self):
        p = self.token("g", CAL, MAIL)
        cfg = Config(google_token_path=p)
        self.assertEqual(commands._google_line(cfg), "구글: 기본(일정·메일)")

    def test_permissions_come_only_from_the_scope_field(self):
        # the config enabling tasks does not make the line claim the permission
        cfg = Config(google_accounts=self.accounts(a=(CAL,)), tasks_accounts=["a"], calendar_enabled=True,
                     mail_enabled=True)
        self.assertEqual(commands._google_line(cfg), "구글: a(일정)")


class AiTests(Base):
    def test_key_and_uses(self):
        env = {"ANTHROPIC_API_KEY": "sk"}
        cases = [
            (dict(llm_enabled=True), "AI(Anthropic): 키 있음 · 쓰는 곳: 중요도 판단"),
            (dict(llm_enabled=True, llm_highlights=["morning"]), "AI(Anthropic): 키 있음 · 쓰는 곳: 중요도 판단, 오늘의 핵심"),
            (dict(llm_enabled=True, chat_enabled=True, commands=["오늘"]),
             "AI(Anthropic): 키 있음 · 쓰는 곳: 중요도 판단, 문장으로 묻기"),
            (dict(memory_enabled=True, commands=["오늘"]), "AI(Anthropic): 키 있음 · 쓰는 곳: 기억"),
            ({}, "AI(Anthropic): 키 있음 · 쓰는 곳: 없음"),
        ]
        for kw, want in cases:
            self.assertEqual(commands._ai_line(Config(**kw), env), want)

    def test_no_key(self):
        self.assertEqual(commands._ai_line(Config(llm_enabled=True), {}), "AI(Anthropic): 키 없음 · 쓰는 곳: 중요도 판단")
        self.assertIn("키 없음", commands._ai_line(Config(llm_enabled=True), {"ANTHROPIC_API_KEY": "  "}))


class GitHubTests(Base):
    def line(self, accounts, env, get):
        return commands._github_line(Config(github_accounts=accounts), get, env)

    def test_missing_token_no_call(self):
        calls = []
        out = self.line(["alice"], {}, lambda *a, **k: calls.append(1))
        self.assertEqual((out, calls), ("GitHub: alice(토큰 없음)", []))

    def test_network_failure_says_could_not_check(self):
        def get(url, headers=None, timeout=15):
            raise netutil.NetError("연결 실패: URLError")
        with self.assertLogs("aide.health", level="WARNING"):
            out = self.line(["alice"], {"GITHUB_TOKEN_ALICE": "ghp-SECRET"}, get)
        self.assertEqual(out, "GitHub: alice(토큰 있음, 확인 못 함)")

    def test_bad_token_401_is_also_could_not_check(self):
        def get(url, headers=None, timeout=15):
            raise netutil.NetError("HTTP 401", status=401)
        with self.assertLogs("aide.health", level="WARNING"):
            self.assertIn("확인 못 함", self.line(["alice"], {"GITHUB_TOKEN_ALICE": "x"}, get))

    def test_expiry_shown_and_unlimited_not(self):
        env = {"GITHUB_TOKEN_ALICE": "a", "GITHUB_TOKEN_BOB": "b"}

        def get(url, headers=None, timeout=15):
            return {}, ({HDR: "2027-01-02 00:00:00 UTC"} if headers["Authorization"].endswith("a") else {})
        self.assertEqual(self.line(["alice", "bob"], env, get),
                         "GitHub: alice(토큰 있음, 만료 2027-01-02) · bob(토큰 있음)")

    def test_far_expiry_is_still_shown_here(self):
        # /연결 always shows the date; only the morning section limits itself to 14 days
        get = lambda url, headers=None, timeout=15: ({}, {HDR: "2030-05-06 15:30:00 UTC"})
        self.assertIn("만료 2030-05-07", self.line(["alice"], {"GITHUB_TOKEN_ALICE": "a"}, get))   # KST date

    def test_only_one_call_to_user_per_account(self):
        calls = []

        def get(url, headers=None, timeout=15):
            calls.append(url)
            return {}, {}
        self.line(["alice"], {"GITHUB_TOKEN_ALICE": "a"}, get)
        self.assertEqual(calls, ["https://api.github.com/user"])


class SecretsTests(Base):
    def test_no_secret_email_or_path_in_the_output(self):
        cfg = Config(google_accounts=self.accounts(개인=(CAL, MAIL)), llm_enabled=True, github_accounts=["alice"],
                     news_feeds=["https://secret-feed.example/rss?token=FEEDSECRET"])
        env = {"ANTHROPIC_API_KEY": "sk-ant-SECRET", "GITHUB_TOKEN_ALICE": "ghp-SECRET"}
        out = self.render(cfg, env=env)
        for bad in ("SECRET", "@example.com", str(self.dir), ".json", "https", "FEEDSECRET", "RT-", "AT-"):
            self.assertNotIn(bad, out)

    def test_github_token_only_goes_in_the_authorization_header(self):
        seen = []

        def get(url, headers=None, timeout=15):
            seen.append((url, headers))
            return {}, {}
        self.render(Config(github_accounts=["alice"]), get, {"GITHUB_TOKEN_ALICE": "ghp-SECRET"})
        self.assertEqual(seen[0][0], "https://api.github.com/user")
        self.assertEqual(seen[0][1]["Authorization"], "Bearer ghp-SECRET")


class NewsWeatherTests(Base):
    def test_news_variants(self):
        self.assertEqual(commands._news_line(Config(news_feeds=["a", "b"], news_hours=[7])),
                         "뉴스: 피드 2개 · 7시 / 장중 특징주: 꺼짐")
        self.assertEqual(commands._news_line(Config(market_queries=["x"])),
                         "뉴스: 피드 0개 · 매 점검 / 장중 특징주: 검색어 1개")

    def test_weather_variants(self):
        out = self.render(Config(weather_enabled=True, weather_alerts=True))
        self.assertTrue(out.endswith("날씨: 켜짐 / 날씨 판단: 켜짐"))


class CommandWiringTests(Base):
    def test_always_available_like_help(self):
        for word in ("/연결", "연결", "/connections", "／연결", "/연결@aide_bot"):
            self.assertEqual(commands.resolve(word, []), commands.CONNECTIONS_NAME, word)
            self.assertEqual(commands.resolve(word, ["오늘"]), commands.CONNECTIONS_NAME, word)
        self.assertIsNone(commands.resolve("/연결 상태", []))        # more than one word: not a command

    def test_not_a_configurable_command_name(self):
        from aide.config import COMMAND_NAMES, ConfigError, load_config
        self.assertNotIn("연결", COMMAND_NAMES)
        p = self.dir / "c.json"
        p.write_text('{"commands": ["연결"]}', encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(p)

    def test_registry_and_run(self):
        self.assertIs(commands.REGISTRY["연결"], commands.render_connections)
        with mock.patch.dict("os.environ", {}, clear=False):
            out = commands.run("연결", Config(google_accounts=self.accounts(a=(CAL,))), NOW)
        self.assertTrue(out.startswith("■ 연결 상태\n구글: a(일정)"))

    def test_read_only_signature_has_no_state(self):
        import inspect
        params = list(inspect.signature(commands.render_connections).parameters)
        self.assertEqual(params[:2], ["cfg", "now"])
        self.assertNotIn("state", params)

    def test_one_broken_line_does_not_hide_the_others(self):
        cfg = Config(google_accounts=self.accounts(a=(CAL,)))
        with mock.patch.object(commands, "_news_line", side_effect=RuntimeError("x")), \
                self.assertLogs("aide.commands", level="ERROR"):
            out = self.render(cfg)
        self.assertIn("(이 줄은 확인하지 못했어요)", out)
        self.assertIn("구글: a(일정)", out)
        self.assertIn("날씨:", out)

    def test_help_lists_it_only_when_commands_exist_and_exact_old_text_otherwise(self):
        self.assertEqual(commands.render_help(Config(), NOW), "사용 가능한 명령이 없어요.")
        out = commands.render_help(Config(commands=["오늘"]), NOW)
        self.assertEqual(out.splitlines()[:3], ["사용 가능한 명령:", "/오늘", "/연결"])
        self.assertNotIn("날씨", out)


if __name__ == "__main__":
    unittest.main()
