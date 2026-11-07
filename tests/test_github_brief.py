import _bootstrap  # noqa: F401
import io
import json
import logging
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from aide import github_brief as gb, morning, netutil
from aide.config import Config, ConfigError, load_config

NOW = datetime(2026, 11, 7, 8, 0)            # Saturday, KST; yesterday = 11/06
ENV = {"GITHUB_TOKEN_ALICE": "tok-alice", "GITHUB_TOKEN_BOB_X": "tok-bob"}
START = "2026-11-05T15:00:00Z"               # 11/06 00:00 KST
END = "2026-11-06T15:00:00Z"                 # 11/07 00:00 KST


def repo(name, pushed="2026-11-06T05:00:00Z", archived=False):
    return {"name": name, "archived": archived, "pushed_at": pushed}


class FakeGitHub:
    """Fake `get`: routes by path; records (url, headers). Values may be data or a NetError to raise."""

    def __init__(self, repos=None, runs=None, commits=None, closed=None, opened=None, fail_list=None,
                 fail_on=()):
        self.repos = repos or {}      # account token -> list(repo) (or list of pages)
        self.runs, self.commits, self.closed, self.opened = runs or {}, commits or {}, closed or {}, opened or {}
        self.fail_list = fail_list
        self.fail_on = fail_on
        self.calls = []

    def __call__(self, url, headers=None, timeout=15):
        self.calls.append((url, headers))
        parsed = urllib.parse.urlparse(url)
        assert parsed.scheme == "https" and parsed.netloc == "api.github.com", url
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        path = urllib.parse.unquote(parsed.path)
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        if self.fail_list is not None and path == "/user/repos":
            raise self.fail_list
        for frag in self.fail_on:
            if frag in path:
                raise netutil.NetError("HTTP 500", status=500)
        if path == "/user/repos":
            pages = self.repos.get(token, [])
            page = int(q.get("page", "1"))
            return pages[page - 1] if page <= len(pages) else []
        parts = path.split("/")          # '', repos, owner, name, kind...
        name, kind = parts[3], "/".join(parts[4:])
        if kind == "actions/runs":
            runs = self.runs.get(name, [])
            return {"total_count": len(runs), "workflow_runs": runs}
        if kind == "commits":
            return self.commits.get(name, [])
        if kind == "pulls":
            return (self.closed if q["state"] == "closed" else self.opened).get(name, [])
        raise AssertionError(path)

    def paths(self, frag):
        return [u for u, _ in self.calls if frag in u]


def cfg_of(*accounts):
    return Config(github_accounts=list(accounts))


def fetch(fake, *accounts, env=ENV, now=NOW):
    return gb.fetch(cfg_of(*accounts), now, get=fake, environ=env)


class OffTests(unittest.TestCase):
    def test_default_off_no_calls_no_change(self):
        fake = FakeGitHub()
        self.assertEqual(Config().github_accounts, [])
        self.assertEqual(gb.fetch(Config(), NOW, get=fake, environ=ENV), [])
        self.assertEqual(fake.calls, [])
        with mock.patch.object(gb, "fetch", side_effect=AssertionError("must not be called")):
            text = morning.build_message(Config(), NOW)
        self.assertNotIn("GitHub", text)


class NamingAndTimeTests(unittest.TestCase):
    def test_token_env_name(self):
        self.assertEqual(gb.token_env_name("boundaryx01-uniquelife"), "GITHUB_TOKEN_BOUNDARYX01_UNIQUELIFE")
        self.assertEqual(gb.token_env_name("foruniquelife00"), "GITHUB_TOKEN_FORUNIQUELIFE00")

    def test_yesterday_range_is_kst_converted_to_utc(self):
        day, start, end = gb.yesterday_range(datetime(2026, 11, 7, 8, 0))
        self.assertEqual(str(day), "2026-11-06")
        self.assertEqual(start, datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc))
        # month boundary
        day, start, _ = gb.yesterday_range(datetime(2026, 12, 1, 0, 5))
        self.assertEqual(str(day), "2026-11-30")
        self.assertEqual(start, datetime(2026, 11, 29, 15, 0, tzinfo=timezone.utc))

    def test_api_gets_utc_iso_range(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("a")]]})
        fetch(fake, "alice")
        runs = urllib.parse.parse_qs(urllib.parse.urlparse(fake.paths("actions/runs")[0]).query)
        self.assertEqual(runs["created"], [f"{START}..{END}"])
        self.assertEqual(runs["status"], ["failure"])
        commits = urllib.parse.parse_qs(urllib.parse.urlparse(fake.paths("/commits")[0]).query)
        self.assertEqual((commits["since"], commits["until"]), ([START], [END]))

    def test_merged_pr_range_boundaries(self):
        closed = {"a": [
            {"merged_at": "2026-11-05T14:59:59Z"},   # 11/05 23:59:59 KST -> out
            {"merged_at": "2026-11-05T15:00:00Z"},   # 11/06 00:00:00 KST -> in
            {"merged_at": "2026-11-06T14:59:59Z"},   # 11/06 23:59:59 KST -> in
            {"merged_at": "2026-11-06T15:00:00Z"},   # 11/07 00:00 KST -> out
            {"merged_at": None},                      # closed unmerged -> out
        ]}
        fake = FakeGitHub(repos={"tok-alice": [[repo("a")]]}, closed=closed)
        lines = fetch(fake, "alice")
        self.assertIn("• a: PR 병합 2", lines)


class AccountTests(unittest.TestCase):
    def test_missing_token_line_and_no_call(self):
        fake = FakeGitHub()
        lines = fetch(fake, "carol")
        self.assertEqual(lines, ["■ GitHub (어제 11/06)", "GitHub(carol): 토큰 없음", ""])
        self.assertEqual(fake.calls, [])

    def test_empty_token_counts_as_missing(self):
        lines = fetch(FakeGitHub(), "alice", env={"GITHUB_TOKEN_ALICE": "  "})
        self.assertIn("GitHub(alice): 토큰 없음", lines)

    def test_401_403_and_network_error(self):
        for err in (netutil.NetError("HTTP 401", status=401), netutil.NetError("HTTP 403", status=403),
                    netutil.NetError("연결 실패: URLError")):
            lines = fetch(FakeGitHub(fail_list=err), "alice")
            self.assertIn("GitHub(alice): 불러오지 못함 (토큰 확인)", lines)

    def test_one_bad_account_does_not_hide_another(self):
        fake = FakeGitHub(repos={"tok-bob": [[repo("b")]]}, commits={"b": [{}, {}]})
        lines = fetch(fake, "alice", "bob-x")
        self.assertIn("• b: 커밋 2", lines)

    def test_header_has_token_and_api_version(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("a")]]})
        fetch(fake, "alice")
        for _, h in fake.calls:
            self.assertEqual(h["Authorization"], "Bearer tok-alice")
            self.assertEqual(h["Accept"], "application/vnd.github+json")
            self.assertEqual(h["X-GitHub-Api-Version"], "2022-11-28")

    def test_each_account_uses_its_own_token(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("a")]], "tok-bob": [[repo("b")]]})
        fetch(fake, "alice", "bob-x")
        for url, h in fake.calls:
            if "/repos/alice/" in url:
                self.assertEqual(h["Authorization"], "Bearer tok-alice")
            if "/repos/bob-x/" in url:
                self.assertEqual(h["Authorization"], "Bearer tok-bob")

    def test_token_and_url_never_logged(self):
        fake = FakeGitHub(fail_list=netutil.NetError("HTTP 401", status=401),
                          repos={"tok-bob": [[repo("b")]]}, fail_on=("/commits",))
        with self.assertLogs("aide.github", level=logging.DEBUG) as cm:
            fetch(fake, "alice", "bob-x")
        joined = "\n".join(cm.output)
        self.assertNotIn("tok-", joined)
        self.assertNotIn("api.github.com", joined)
        self.assertNotIn("Bearer", joined)
        self.assertNotIn("per_page", joined)


class RepoListTests(unittest.TestCase):
    def test_archived_excluded_everywhere(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("old", archived=True), repo("live")]]})
        fetch(fake, "alice")
        self.assertFalse([u for u in fake.paths("/repos/") if "/repos/alice/old/" in u])
        self.assertTrue([u for u in fake.paths("/repos/") if "/repos/alice/live/" in u])

    def test_pagination_up_to_three_pages(self):
        pages = [[repo(f"p{p}r{i}", pushed="2020-01-01T00:00:00Z") for i in range(100)] for p in range(5)]
        fake = FakeGitHub(repos={"tok-alice": pages})
        fetch(fake, "alice")
        self.assertEqual(len(fake.paths("/user/repos")), 3)
        self.assertEqual(len(fake.paths("actions/runs")), 300)

    def test_short_page_stops(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("a")], [repo("never")]]})
        fetch(fake, "alice")
        self.assertEqual(len(fake.paths("/user/repos")), 1)

    def test_repo_list_query(self):
        fake = FakeGitHub(repos={"tok-alice": [[]]})
        fetch(fake, "alice")
        q = urllib.parse.parse_qs(urllib.parse.urlparse(fake.paths("/user/repos")[0]).query)
        self.assertEqual((q["affiliation"], q["per_page"], q["sort"], q["page"]), (["owner"], ["100"], ["pushed"], ["1"]))

    def test_repo_name_validation_and_encoding(self):
        bad = ["a b", "x/../y", "..", ".", "a?b", "é", "x" * 101, ""]
        repos = [repo(n) for n in bad] + [repo("ok.name_1-x"), repo(".github"), {"name": 5}, "junk"]
        fake = FakeGitHub(repos={"tok-alice": [repos]})
        fetch(fake, "alice")
        repo_urls = [u for u in fake.paths("/repos/")]
        names = {urllib.parse.urlparse(u).path.split("/")[3] for u in repo_urls}
        self.assertEqual(names, {"ok.name_1-x", ".github"})

    def test_non_list_response_is_a_failed_account(self):
        fake = FakeGitHub(repos={"tok-alice": [{"message": "x"}]})
        self.assertIn("GitHub(alice): 불러오지 못함 (토큰 확인)", fetch(fake, "alice"))


class ActivityTests(unittest.TestCase):
    def test_commits_merged_open_line(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("aide")]]}, commits={"aide": [{}] * 5},
                          closed={"aide": [{"merged_at": "2026-11-06T01:00:00Z"}] * 2},
                          opened={"aide": [{}]})
        lines = fetch(fake, "alice")
        self.assertEqual(lines, ["■ GitHub (어제 11/06)", "• aide: 커밋 5 · PR 병합 2 · 열린 PR 1", ""])

    def test_zero_parts_are_left_out(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("a")]]}, commits={"a": [{}] * 3})
        self.assertEqual(fetch(fake, "alice")[1], "• a: 커밋 3")

    def test_commits_100_plus(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("a")]]}, commits={"a": [{}] * 100})
        self.assertIn("• a: 커밋 100+", fetch(fake, "alice"))

    def test_only_recently_pushed_repos_get_commit_and_pr_calls(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("old", pushed="2026-11-05T14:59:59Z"),
                                                 repo("edge", pushed="2026-11-05T15:00:00Z"),
                                                 repo("nopush", pushed=None)]]})
        fetch(fake, "alice")
        self.assertEqual(len(fake.paths("/repos/alice/old/")), 1)      # runs only
        self.assertEqual(len(fake.paths("/repos/alice/nopush/")), 1)
        self.assertEqual(len(fake.paths("/repos/alice/edge/")), 4)     # runs, commits, closed, open

    def test_nothing_yesterday_omits_section(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("a", pushed="2026-10-01T00:00:00Z")]]})
        self.assertEqual(fetch(fake, "alice"), [])

    def test_failed_run_found_in_repo_without_push(self):
        runs = {"briefing-bot": [{"name": "Daily 3D News Bot"}]}
        fake = FakeGitHub(repos={"tok-alice": [[repo("briefing-bot", pushed="2026-01-01T00:00:00Z")]]}, runs=runs)
        lines = fetch(fake, "alice")
        self.assertEqual(lines, ["■ GitHub (어제 11/06)",
                                 "⚠ briefing-bot: 자동 작업 실패 1 (Daily 3D News Bot)", ""])
        self.assertEqual(len(fake.paths("/commits")), 0)

    def test_failure_name_is_cleaned_and_capped(self):
        nasty = "Run\x07\n  " + "x" * 100
        fake = FakeGitHub(repos={"tok-alice": [[repo("a", pushed=None)]]}, runs={"a": [{"name": nasty}]})
        line = fetch(fake, "alice")[1]
        self.assertNotIn("\x07", line)
        self.assertNotIn("\n", line)
        self.assertLessEqual(len(line.split("(", 1)[1]), 42)

    def test_failure_count_uses_total_count(self):
        class G(FakeGitHub):
            def __call__(self, url, headers=None, timeout=15):
                if "actions/runs" in url:
                    return {"total_count": 12, "workflow_runs": [{"name": "W"}] * 5}
                return super().__call__(url, headers=headers)
        self.assertIn("⚠ a: 자동 작업 실패 12 (W)",
                      fetch(G(repos={"tok-alice": [[repo("a", pushed=None)]]}), "alice"))

    def test_one_repo_failing_only_skips_that_repo(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("bad"), repo("good")]]}, commits={"good": [{}]},
                          fail_on=("/alice/bad/",))
        lines = fetch(fake, "alice")
        self.assertEqual(lines[1:-1], ["• good: 커밋 1"])

    def test_commit_messages_and_pr_titles_never_shown(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("a")]]},
                          commits={"a": [{"commit": {"message": "SECRET-MSG"}}]},
                          closed={"a": [{"title": "SECRET-TITLE", "merged_at": "2026-11-06T01:00:00Z"}]})
        self.assertNotIn("SECRET", "\n".join(fetch(fake, "alice")))


class OrderingTests(unittest.TestCase):
    def test_failures_first_then_activity_by_commits(self):
        fake = FakeGitHub(
            repos={"tok-alice": [[repo("few"), repo("many"), repo("bot", pushed=None)]]},
            commits={"few": [{}], "many": [{}] * 9},
            runs={"bot": [{"name": "W"}]})
        body = fetch(fake, "alice")[1:-1]
        self.assertEqual(body, ["⚠ bot: 자동 작업 실패 1 (W)", "• many: 커밋 9", "• few: 커밋 1"])

    def test_ten_line_limit(self):
        names = [f"r{i:02d}" for i in range(14)]
        fake = FakeGitHub(repos={"tok-alice": [[repo(n) for n in names]]},
                          commits={n: [{}] * (20 - i) for i, n in enumerate(names)},
                          runs={"r13": [{"name": "W"}]})
        lines = fetch(fake, "alice")
        body = lines[1:-1]
        self.assertEqual(len(body), 11)
        self.assertTrue(body[0].startswith("⚠ r13"))
        self.assertEqual(body[-1], "…외 5개")      # 15 lines in total, 10 shown
        self.assertEqual(body[1], "• r00: 커밋 20")

    def test_same_repo_name_in_two_accounts_shows_account(self):
        fake = FakeGitHub(repos={"tok-alice": [[repo("shared"), repo("solo")]], "tok-bob": [[repo("shared")]]},
                          commits={"shared": [{}], "solo": [{}]})
        body = fetch(fake, "alice", "bob-x")[1:-1]
        self.assertIn("• alice/shared: 커밋 1", body)
        self.assertIn("• bob-x/shared: 커밋 1", body)
        self.assertIn("• solo: 커밋 1", body)

    def test_time_budget_skips_remaining_repos(self):
        ticks = iter([0, 0, 1000, 1000, 1000, 1000, 1000, 1000])
        fake = FakeGitHub(repos={"tok-alice": [[repo("a"), repo("b"), repo("c")]]}, commits={"a": [{}]})
        lines = gb.fetch(cfg_of("alice"), NOW, get=fake, environ=ENV, clock=lambda: next(ticks))
        self.assertIn("• a: 커밋 1", lines)
        self.assertEqual(len(fake.paths("/repos/alice/b/")), 0)

    def test_fetch_never_raises(self):
        def boom(url, headers=None, timeout=15):
            raise RuntimeError("boom")
        self.assertEqual(gb.fetch(cfg_of("alice"), NOW, get=boom, environ=ENV), [])


class ConfigTests(unittest.TestCase):
    def load(self, value):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"github_accounts": value}), encoding="utf-8")
            return load_config(p)

    def test_valid(self):
        self.assertEqual(self.load(["boundaryx01-uniquelife", "foruniquelife00"]).github_accounts,
                         ["boundaryx01-uniquelife", "foruniquelife00"])
        self.assertEqual(self.load([]).github_accounts, [])

    def test_invalid(self):
        for bad in ["alice", [1], ["a b"], ["a_b"], ["a/b"], [""], ["x" * 40], ["a", "A"], ["a", "a"],
                    [f"u{i}" for i in range(6)], ["a\nb"], [None]]:
            with self.assertRaises(ConfigError, msg=repr(bad)):
                self.load(bad)

    def test_max_five_and_39_chars_ok(self):
        self.assertEqual(len(self.load([f"u{i}" for i in range(5)]).github_accounts), 5)
        self.assertEqual(len(self.load(["x" * 39]).github_accounts[0]), 39)

    def test_example_config_loads_with_empty_list(self):
        cfg = load_config(Path(__file__).resolve().parents[1] / "config.example.json")
        self.assertEqual(cfg.github_accounts, [])


LINES = ["■ GitHub (어제 11/06)", "• aide: 커밋 5", ""]


class MorningTests(unittest.TestCase):
    def cfg(self):
        return Config(github_accounts=["alice"], watch_repos=[], weather_enabled=False, calendar_enabled=False)

    def test_dry_run_and_send_have_the_same_section(self):
        cfg = self.cfg()
        calls = []

        def fg(c, now):
            calls.append(now)
            return list(LINES)

        preview = morning.build_message(cfg, NOW, fetch_github=fg)
        self.assertIn("■ GitHub (어제 11/06)\n• aide: 커밋 5", preview)

        sent = []
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", env):
            cfg.state_path = str(Path(d) / "s.json")
            rc = morning.run(cfg, send=True, now=NOW, sender=lambda m, **k: sent.append(m), fetch_github=fg)
            self.assertEqual(rc, 0)
            cfg.state_path = str(Path(d) / "s2.json")
            out = io.StringIO()
            with redirect_stdout(out):
                morning.run(cfg, send=False, now=NOW, fetch_github=fg)
        self.assertEqual(sent[0], preview)
        self.assertEqual(out.getvalue().strip(), preview)
        self.assertEqual(len(calls), 3)    # once per path, never twice in one run

    def test_section_comes_after_tasks_and_before_work(self):
        cfg = self.cfg()
        cfg.watch_repos = []
        text = morning.build_message(cfg, NOW, fetch_github=lambda c, n: list(LINES))
        self.assertTrue(text.startswith("좋은 아침이에요"))
        self.assertIn("• aide: 커밋 5", text)

    def test_exception_drops_only_the_section(self):
        cfg = self.cfg()

        def boom(c, now):
            raise RuntimeError("x")

        text = morning.build_message(cfg, NOW, fetch_github=boom)
        self.assertTrue(text.startswith("좋은 아침이에요"))
        self.assertNotIn("GitHub", text)
        sent = []
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", env):
            cfg.state_path = str(Path(d) / "s.json")
            rc = morning.run(cfg, send=True, now=NOW, sender=lambda m, **k: sent.append(m), fetch_github=boom)
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1)

    def test_fetch_runs_outside_state_lock(self):
        cfg = self.cfg()
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", env):
            cfg.state_path = str(Path(d) / "s.json")
            lock_seen = []

            def fg(c, now):
                from aide.state import locked
                try:
                    with locked(Path(cfg.state_path)):   # would raise StateLocked if morning held it
                        lock_seen.append(False)
                except Exception:  # noqa: BLE001
                    lock_seen.append(True)
                return []

            morning.run(cfg, send=True, now=NOW, sender=lambda m, **k: None, fetch_github=fg)
        self.assertEqual(lock_seen, [False])


if __name__ == "__main__":
    unittest.main()
