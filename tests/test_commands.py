import _bootstrap  # noqa: F401
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from aide import commands, gcal, gmail, google_auth, weather
from aide.config import Config, ConfigError, load_config
from aide.models import Finding

NOW = datetime(2026, 10, 3, 9, 0)
ENABLED = ["오늘", "일정", "마감", "메일"]


class NormalizeTests(unittest.TestCase):
    def test_strips_slash_and_botname_and_lowercases(self):
        self.assertEqual(commands.normalize("/Today@my_bot"), "today")

    def test_fullwidth_variant_folds_to_ascii(self):
        fullwidth_today = "ｔｏｄａｙ"  # fullwidth "today"
        self.assertEqual(commands.normalize("/" + fullwidth_today), "today")

    def test_whitespace_or_multiword_is_not_a_command(self):
        self.assertEqual(commands.normalize("오늘 뭐해"), "")
        self.assertEqual(commands.normalize("오늘\n일정"), "")

    def test_too_long_is_rejected(self):
        self.assertEqual(commands.normalize("오늘" * 20), "")

    def test_empty_and_none(self):
        self.assertEqual(commands.normalize(""), "")
        self.assertEqual(commands.normalize(None), "")


class ResolveTests(unittest.TestCase):
    def test_korean_and_english_alias_both_resolve(self):
        self.assertEqual(commands.resolve("/오늘", ENABLED), "오늘")
        self.assertEqual(commands.resolve("/today", ENABLED), "오늘")

    def test_not_enabled_is_none(self):
        self.assertIsNone(commands.resolve("/오늘", ["일정"]))

    def test_unknown_text_is_none(self):
        self.assertIsNone(commands.resolve("/부엉이", ENABLED))
        self.assertIsNone(commands.resolve("rm -rf /", ENABLED))

    def test_help_resolves_even_when_nothing_else_enabled(self):
        self.assertEqual(commands.resolve("/도움", []), commands.HELP_NAME)
        self.assertEqual(commands.resolve("/start", []), commands.HELP_NAME)


def message(text, uid=1, sender="111", chat="111", chat_type="private", is_bot=False, ts=None, forwarded=False,
            has_text=True):
    m = {
        "message_id": 5,
        "from": {"id": int(sender), "is_bot": is_bot},
        "chat": {"id": int(chat), "type": chat_type},
        "date": ts if ts is not None else int(NOW.timestamp()),
    }
    if has_text:
        m["text"] = text
    if forwarded:
        m["forward_origin"] = {"type": "user"}
    return {"update_id": uid, "message": m}


class IsCommandMessageTests(unittest.TestCase):
    OWNER = "111"

    def test_owner_private_text_is_allowed(self):
        self.assertTrue(commands.is_command_message(message("/오늘"), self.OWNER))

    def test_stranger_is_rejected(self):
        self.assertFalse(commands.is_command_message(message("/오늘", sender="999", chat="999"), self.OWNER))

    def test_group_chat_is_rejected_even_from_owner(self):
        self.assertFalse(commands.is_command_message(message("/오늘", chat="-100", chat_type="group"), self.OWNER))

    def test_bot_sender_is_rejected(self):
        self.assertFalse(commands.is_command_message(message("/오늘", is_bot=True), self.OWNER))

    def test_forwarded_is_rejected(self):
        self.assertFalse(commands.is_command_message(message("/오늘", forwarded=True), self.OWNER))

    def test_non_text_message_is_rejected(self):
        self.assertFalse(commands.is_command_message(message(None, has_text=False), self.OWNER))

    def test_no_allowed_chat_id_configured_rejects_everyone(self):
        self.assertFalse(commands.is_command_message(message("/오늘"), ""))

    def test_malformed_update_is_rejected(self):
        self.assertFalse(commands.is_command_message({}, self.OWNER))
        self.assertFalse(commands.is_command_message({"message": "not a dict"}, self.OWNER))


class StaleTests(unittest.TestCase):
    def test_fresh_is_not_stale(self):
        now = 1_000_000.0
        self.assertFalse(commands.is_stale(message("/오늘", ts=int(now) - 10), now))

    def test_old_is_stale(self):
        now = 1_000_000.0
        self.assertTrue(commands.is_stale(message("/오늘", ts=int(now) - commands.STALE_SECONDS - 1), now))

    def test_missing_date_is_stale(self):
        u = message("/오늘")
        del u["message"]["date"]
        self.assertTrue(commands.is_stale(u, 1_000_000.0))


class RenderTests(unittest.TestCase):
    def cfg(self, **kw):
        return Config(**kw)

    def test_today_off_by_default(self):
        out = commands.render_today(self.cfg(), NOW)
        self.assertIn("꺼져 있어요", out)

    def test_today_weather_and_calendar(self):
        w = weather.Weather("맑음", 12, 20, 10)
        evs = [gcal.Event("14:00", "회의")]
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            out = commands.render_today(
                self.cfg(weather_enabled=True, calendar_enabled=True), NOW,
                fetch_weather=lambda la, lo: w, fetch_events=lambda t, n: evs)
        self.assertIn("맑음", out)
        self.assertIn("14:00 회의", out)

    def test_today_degrades_on_failure(self):
        with mock.patch.object(google_auth, "access_token", side_effect=google_auth.GoogleAuthError("x")):
            out = commands.render_today(self.cfg(calendar_enabled=True), NOW)
        self.assertIn("불러오지 못했어요", out)

    def test_week_off_by_default(self):
        self.assertIn("꺼져 있어요", commands.render_week(self.cfg(), NOW))

    def test_week_groups_by_day(self):
        from datetime import date
        evs = [gcal.Event("09:00", "월요 회의", day=date(2026, 10, 5))]
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            out = commands.render_week(self.cfg(calendar_enabled=True), NOW, fetch_week=lambda t, f, d: evs)
        self.assertIn("월요 회의", out)
        self.assertIn("10/05", out)

    def test_week_no_events(self):
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            out = commands.render_week(self.cfg(calendar_enabled=True), NOW, fetch_week=lambda t, f, d: [])
        self.assertIn("없어요", out)

    def test_due_off_without_notice_pages(self):
        self.assertIn("꺼져 있어요", commands.render_due(self.cfg(), NOW))

    def test_due_lists_upcoming(self):
        html = '<ul><li><a href="/n/1">연수 신청 공고 안내</a> ~2026-10-05</li></ul>'.encode()
        out = commands.render_due(
            self.cfg(notice_pages=["http://x"], notice_keywords=["연수"]), NOW, fetch=lambda url: html)
        self.assertIn("연수 신청 공고 안내", out)
        self.assertIn("D-", out)

    def test_mail_off_by_default(self):
        self.assertIn("꺼져 있어요", commands.render_mail(self.cfg(), NOW))

    def test_mail_lists_and_degrades_per_account(self):
        accts = [
            {"name": "개인", "token_path": "data/t1.json", "email": "a@b.com"},
            {"name": "학교", "token_path": "data/t2.json", "email": "c@d.com"},
        ]
        cfg = self.cfg(mail_enabled=True, mail_recent_hours=[8], google_accounts=accts)

        def access(client, path, need_scope=None):
            if "t2" in str(path):
                raise google_auth.GoogleAuthError("만료")
            return "T"

        def recent(token, max_items, window_hours, blocked, now):
            return [Finding(key="mail:1", source="mail", title="보낸이: 제목")]

        with mock.patch.object(google_auth, "access_token", side_effect=access), \
                mock.patch.object(gmail, "recent_unread", side_effect=recent):
            out = commands.render_mail(cfg, NOW)
        self.assertIn("[개인]", out)
        self.assertIn("불러오지 못한 계정", out)
        self.assertIn("학교", out)

    def test_help_lists_only_enabled(self):
        out = commands.render_help(self.cfg(commands=["오늘", "마감"]), NOW)
        self.assertIn("/오늘", out)
        self.assertIn("/마감", out)
        self.assertNotIn("/일정", out)

    def test_help_with_nothing_enabled(self):
        self.assertIn("없어요", commands.render_help(self.cfg(), NOW))


class RunTests(unittest.TestCase):
    def test_handler_exception_never_escapes(self):
        with mock.patch.dict(commands.REGISTRY, {"오늘": mock.Mock(side_effect=RuntimeError("boom"))}):
            out = commands.run("오늘", Config(), NOW)
        self.assertIn("오류", out)

    def test_known_command_runs(self):
        out = commands.run(commands.HELP_NAME, Config(commands=["일정"]), NOW)
        self.assertIn("/일정", out)


class ConfigValidationTests(unittest.TestCase):
    def write(self, data):
        d = tempfile.mkdtemp()
        p = Path(d) / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_default_is_empty_and_feature_off(self):
        self.assertEqual(Config().commands, [])

    def test_known_names_accepted(self):
        cfg = load_config(self.write({"commands": ["오늘", "마감"]}))
        self.assertEqual(cfg.commands, ["오늘", "마감"])

    def test_unknown_name_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write({"commands": ["오늘", "삭제"]}))

    def test_duplicate_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write({"commands": ["오늘", "오늘"]}))

    def test_non_list_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write({"commands": "오늘"}))

    def test_non_string_entries_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self.write({"commands": [1, 2]}))


if __name__ == "__main__":
    unittest.main()
