import _bootstrap  # noqa: F401
import json
import logging
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from aide import chat, commands, inbox, llm, memory, telegram
from aide.config import Config, ConfigError, load_config
from aide.models import Finding
from aide.state import State, StateLocked, locked

from test_inbox import FakeApi, msg, press

NOW = datetime(2026, 10, 3, 9, 0)
OWNER = "111"
SECRET = "비밀기억문장XYZ"


class MemApi(FakeApi):
    """Records buttons and edits; optionally fails the test if anything is sent while the state lock is held."""

    def __init__(self, updates=None, state_path=None):
        super().__init__(updates)
        self.buttons, self.edits, self.state_path = [], [], state_path

    def send(self, text, *, token, chat_id, buttons=None):
        if self.state_path is not None:
            try:
                with locked(self.state_path, timeout=0.3):
                    pass
            except StateLocked:
                raise AssertionError("sent while the state lock was held") from None
        self.buttons.append(buttons)
        super().send(text, token=token, chat_id=chat_id)

    def edit_text(self, token, chat_id, message_id, text, buttons=None):
        self.edits.append((message_id, text, buttons))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "memory.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_path_is_next_to_state_file(self):
        self.assertEqual(memory.path_for(Path("data/state.json")), Path("data/memory.json"))

    def test_add_save_reload(self):
        m = memory.Memory(self.path)
        status, item = m.add("교육청 공문은 항상 중요", NOW)
        self.assertEqual(status, "ok")
        self.assertRegex(item["id"], r"^[0-9a-f]{8}$")
        m.save()
        again = memory.Memory(self.path)
        self.assertEqual([i["text"] for i in again.items], ["교육청 공문은 항상 중요"])
        self.assertEqual(again.items[0]["at"], "2026-10-03T09:00:00")
        self.assertFalse(self.path.with_name("memory.json.tmp").exists())

    def test_empty_and_control_only_text_refused(self):
        m = memory.Memory(self.path)
        for t in ("", "   ", "\x00\x07"):
            self.assertEqual(m.add(t, NOW)[0], "empty")
        self.assertEqual(m.items, [])

    def test_duplicate_not_saved_twice(self):
        m = memory.Memory(self.path)
        m.add("같은 글", NOW)
        status, item = m.add("같은   글", NOW)   # whitespace is collapsed by clean()
        self.assertEqual(status, "dup")
        self.assertEqual(item["text"], "같은 글")
        self.assertEqual(len(m.items), 1)

    def test_limit_of_50(self):
        m = memory.Memory(self.path)
        for n in range(50):
            self.assertEqual(m.add(f"항목{n}", NOW)[0], "ok")
        self.assertEqual(m.add("하나 더", NOW)[0], "full")
        self.assertEqual(len(m.items), 50)
        m.remove(m.items[0]["id"])
        self.assertEqual(m.add("하나 더", NOW)[0], "ok")

    def test_text_is_cleaned_and_capped_at_200(self):
        m = memory.Memory(self.path)
        _, item = m.add("가" * 300, NOW)
        self.assertEqual(len(item["text"]), 200)
        self.assertTrue(item["text"].endswith("…"))
        _, item = m.add("a\x00b\n\nc", NOW)
        self.assertEqual(item["text"], "ab c")

    def test_damaged_file_is_set_aside_and_start_empty(self):
        self.path.write_text("{ not json", encoding="utf-8")
        m = memory.Memory(self.path)
        self.assertEqual(m.items, [])
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(Path(self.dir.name).glob("memory.json.corrupt-*"))), 1)

    def test_prompt_reader_does_not_move_a_damaged_file(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(memory.Memory(self.path, quarantine=False).items, [])
        self.assertTrue(self.path.exists())

    def test_bad_entries_in_file_are_dropped(self):
        self.path.write_text(json.dumps({"items": [
            {"id": "abcd1234", "text": "좋음", "at": "x"},
            {"id": "abcd1234", "text": "같은 id", "at": "x"},
            {"id": "ZZ", "text": "나쁜 id"},
            {"id": "11111111", "text": "  "},
            "문자열", {"id": "22222222", "text": "다음"}]}), encoding="utf-8")
        self.assertEqual([i["text"] for i in memory.Memory(self.path).items], ["좋음", "다음"])

    def test_save_is_atomic_and_private(self):
        m = memory.Memory(self.path)
        m.add("글", NOW)
        with mock.patch("aide.memory.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                m.save()
        self.assertFalse(self.path.exists())   # the real file was never half-written
        m.save()
        self.assertTrue(self.path.exists())
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_nothing_written_when_unchanged(self):
        memory.Memory(self.path).save()
        self.assertFalse(self.path.exists())


class ParseCommandTests(unittest.TestCase):
    def test_list_and_add(self):
        self.assertEqual(memory.parse_command("/기억"), ("list", ""))
        self.assertEqual(memory.parse_command("/기억   "), ("list", ""))
        self.assertEqual(memory.parse_command("/기억 교육청 공문"), ("add", "교육청 공문"))
        self.assertEqual(memory.parse_command("/기억\n여러 줄"), ("add", "여러 줄"))

    def test_full_width_slash_and_spaces(self):
        self.assertEqual(memory.parse_command("／기억 글"), ("add", "글"))
        self.assertEqual(memory.parse_command("／기억　전각공백 글"), ("add", "전각공백 글"))

    def test_english_alias_any_case(self):
        self.assertEqual(memory.parse_command("/remember x"), ("add", "x"))
        self.assertEqual(memory.parse_command("/Remember"), ("list", ""))

    def test_bot_suffix(self):
        self.assertEqual(memory.parse_command("/기억@mybot 글"), ("add", "글"))

    def test_not_this_command(self):
        for t in ("기억 글", "/기억해줘", "/기억글", "/일정", "내일 일정 기억해", "", None, 5, "x /기억 y"):
            self.assertIsNone(memory.parse_command(t), t)

    def test_long_text_still_parses_the_caller_enforces_230(self):
        self.assertEqual(memory.parse_command("/기억 " + "가" * 300)[0], "add")
        self.assertEqual(memory.MAX_MESSAGE, 230)

    def test_callback_data_pattern(self):
        for ok in ("k:abcd1234:undo", "k:00000000:del"):
            self.assertTrue(memory.is_data(ok))
        for bad in ("k:abcd123:del", "k:ABCD1234:del", "k:abcd1234:rm", "k:abcd1234:del ", "x:abcd1234:del", None, 5):
            self.assertFalse(memory.is_data(bad), bad)


class InboxMemoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"
        self.mpath = memory.path_for(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def cfg(self, **kw):
        base = dict(state_path=str(self.path), commands=["오늘"], memory_enabled=True)
        base.update(kw)
        return Config(**base)

    def poll(self, updates, cfg=None, api=None, now=NOW):
        api = api or MemApi(updates, self.path)
        n = inbox.poll_once(cfg or self.cfg(), token="t", chat_id=OWNER, api=api, now=now)
        return n, api

    def items(self):
        return memory.Memory(self.mpath).items

    # --- off ---------------------------------------------------------------
    def test_off_means_unknown_command_and_nothing_stored(self):
        n, api = self.poll([msg("/기억 비밀")], cfg=self.cfg(memory_enabled=False))
        self.assertEqual(api.sent, [(OWNER, "모르는 명령이에요. /도움")])
        self.assertFalse(self.mpath.exists())
        self.assertEqual(State(self.path).commands_in_last_hour(NOW), 0)

    def test_off_buttons_just_expire(self):
        memory_file = memory.Memory(self.mpath)
        _, it = memory_file.add("글", NOW)
        memory_file.save()
        api = MemApi([press(f"k:{it['id']}:del")])
        self.poll(None, cfg=self.cfg(memory_enabled=False), api=api)
        self.assertEqual(api.answers, [("c1", memory.EXPIRED)])
        self.assertEqual(len(self.items()), 1)

    # --- add / list --------------------------------------------------------
    def test_add_replies_with_text_and_undo_button(self):
        n, api = self.poll([msg("/기억 교육청 공문은 항상 중요")])
        self.assertEqual(n, 1)
        item = self.items()[0]
        self.assertEqual(item["text"], "교육청 공문은 항상 중요")
        self.assertEqual(api.sent, [(OWNER, "기억했어요: 교육청 공문은 항상 중요")])
        self.assertEqual(api.buttons, [[[("↩ 취소", f"k:{item['id']}:undo")]]])
        self.assertEqual(State(self.path).commands_in_last_hour(NOW), 1)

    def test_duplicate_and_full_and_empty_replies(self):
        self.poll([msg("/기억 글")])
        _, api = self.poll([msg("/기억 글", uid=2)])
        self.assertEqual(api.sent, [(OWNER, "이미 기억하고 있어요: 글")])
        self.assertEqual(api.buttons, [None])
        self.assertEqual(len(self.items()), 1)
        m = memory.Memory(self.mpath)
        for n in range(49):
            m.add(f"항목{n}", NOW)
        m.save()
        _, api = self.poll([msg("/기억 하나 더", uid=3)])
        self.assertEqual(api.sent, [(OWNER, "기억은 50개까지예요. /기억 에서 지워 주세요")])
        self.assertEqual(len(self.items()), 50)
        _, api = self.poll([msg("/기억 \x00\x07", uid=4)])   # only control characters
        self.assertEqual(api.sent, [(OWNER, memory.EMPTY_BODY)])

    def test_list_reply_numbered_with_delete_buttons_four_per_row(self):
        m = memory.Memory(self.mpath)
        for n in range(6):
            m.add(f"항목{n}", NOW)
        m.save()
        _, api = self.poll([msg("/기억")])
        text = api.sent[0][1]
        self.assertTrue(text.startswith("■ 기억 (6/50)\n1. 항목0\n2. 항목1"))
        rows = api.buttons[0]
        self.assertEqual([len(r) for r in rows], [4, 2])
        ids = [i["id"] for i in self.items()]
        self.assertEqual(rows[0][0], ("🗑 1", f"k:{ids[0]}:del"))
        self.assertEqual(rows[1][1], ("🗑 6", f"k:{ids[5]}:del"))

    def test_fifty_item_list_has_fifty_buttons(self):
        m = memory.Memory(self.mpath)
        for n in range(50):
            m.add(f"항목{n}", NOW)
        m.save()
        _, api = self.poll([msg("/기억")])
        self.assertEqual(sum(len(r) for r in api.buttons[0]), 50)

    def test_empty_list_hint(self):
        _, api = self.poll([msg("/기억")])
        self.assertEqual(api.sent, [(OWNER, "기억해 둔 것이 없어요. 예: /기억 교육청 공문은 항상 중요")])
        self.assertEqual(api.buttons, [None])

    def test_too_long_message_rejected_once_with_hint(self):
        _, api = self.poll([msg("/기억 " + "가" * 230), msg("/기억 " + "나" * 300, uid=2)])
        self.assertEqual(api.sent, [(OWNER, memory.TOO_LONG)])   # one notice per batch
        self.assertEqual(self.items(), [])
        _, api = self.poll([msg("/기억 " + "다" * 200, uid=3)])
        self.assertEqual(len(self.items()), 1)

    def test_english_alias_and_full_width_slash_work(self):
        self.poll([msg("/remember a"), msg("／기억 b", uid=2)])
        self.assertEqual([i["text"] for i in self.items()], ["a", "b"])

    # --- who / when --------------------------------------------------------
    def test_not_the_owner_group_forwarded_bot_ignored_silently(self):
        updates = [msg("/기억 x", uid=1, sender="999"),
                   msg("/기억 x", uid=2, chat="999"),
                   msg("/기억 x", uid=3, chat_type="group"),
                   msg("/기억 x", uid=4, forwarded=True),
                   msg("/기억 x", uid=5, is_bot=True)]
        n, api = self.poll(updates)
        self.assertEqual((n, api.sent), (0, []))
        self.assertFalse(self.mpath.exists())

    def test_stale_message_skipped_with_one_notice(self):
        n, api = self.poll([msg("/기억 오래됨", ts=int(NOW.timestamp()) - 3600)])
        self.assertEqual(n, 0)
        self.assertEqual(api.sent, [(OWNER, "늦게 받은 명령은 건너뛰었어요.")])
        self.assertEqual(self.items(), [])

    def test_redelivered_message_skipped(self):
        st = State(self.path)
        st.offset = 10
        st.save()
        n, api = self.poll([msg("/기억 다시", uid=5)])
        self.assertEqual((n, api.sent, self.items()), (0, [], []))

    # --- limits shared with /명령 ------------------------------------------
    def test_batch_limit_shared_with_commands(self):
        updates = [msg("오늘", uid=1), msg("/기억 하나", uid=2), msg("/기억 둘", uid=3), msg("/기억 셋", uid=4)]
        with mock.patch.object(commands, "run", return_value="OK"):
            n, api = self.poll(updates)
        self.assertEqual(n, 3)
        self.assertEqual([i["text"] for i in self.items()], ["하나", "둘"])
        self.assertIn("요청이 많아 일부 명령은 건너뛰었어요", api.sent[-1][1])

    def test_hourly_limit_shared_and_counted(self):
        st = State(self.path)
        for _ in range(commands.MAX_PER_HOUR):
            st.record_command(NOW)
        st.save()
        n, api = self.poll([msg("/기억 막힘")])
        self.assertEqual(n, 0)
        self.assertEqual(self.items(), [])
        self.assertIn("요청이 많아", api.sent[0][1])

    # --- buttons -----------------------------------------------------------
    def seed(self, *texts):
        m = memory.Memory(self.mpath)
        for t in texts:
            m.add(t, NOW)
        m.save()
        return [i["id"] for i in m.items]

    def test_undo_button_removes_and_is_idempotent(self):
        (rid,) = self.seed("취소할 것")
        _, api = self.poll([press(f"k:{rid}:undo", uid=1)])
        self.assertEqual(self.items(), [])
        self.assertEqual(api.answers, [("c1", memory.DELETED)])
        self.assertEqual(api.edits[0][1], "취소했어요: 취소할 것")
        _, api = self.poll([press(f"k:{rid}:undo", uid=2)])
        self.assertEqual(api.answers, [("c1", memory.GONE)])
        self.assertEqual(api.edits[0][1], memory.GONE)

    def test_delete_button_removes_one_and_redraws_list(self):
        a, b, c = self.seed("가", "나", "다")
        _, api = self.poll([press(f"k:{b}:del")])
        self.assertEqual([i["text"] for i in self.items()], ["가", "다"])
        self.assertEqual(api.answers, [("c1", memory.DELETED)])
        mid, text, buttons = api.edits[0]
        self.assertEqual(mid, 77)
        self.assertEqual(text, "■ 기억 (2/50)\n1. 가\n2. 다")
        self.assertEqual(buttons, [[("🗑 1", f"k:{a}:del"), ("🗑 2", f"k:{c}:del")]])
        _, api = self.poll([press(f"k:{b}:del", uid=2)])
        self.assertEqual(api.answers, [("c1", memory.GONE)])
        self.assertEqual(len(self.items()), 2)

    def test_delete_last_item_shows_empty_hint_without_buttons(self):
        (rid,) = self.seed("하나뿐")
        _, api = self.poll([press(f"k:{rid}:del")])
        self.assertEqual(api.edits[0], (77, memory.EMPTY_LIST, None))

    def test_buttons_from_someone_else_do_nothing(self):
        (rid,) = self.seed("지키기")
        api = MemApi([press(f"k:{rid}:del", sender="999"), press(f"k:{rid}:del", chat="999", uid=2),
                      press(f"k:{rid}:del", is_bot=True, uid=3)], self.path)
        self.poll(None, api=api)
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(api.answers, [])

    def test_malformed_button_data_is_not_a_memory_button(self):
        (rid,) = self.seed("지키기")
        for bad in (f"k:{rid}:wipe", f"k:{rid.upper()}:del", f"k:{rid[:7]}:del", "k:*:del", f"k:{rid}:del;x"):
            api = MemApi([press(bad)], self.path)
            self.poll(None, api=api)
            self.assertEqual(len(self.items()), 1, bad)
            self.assertEqual(api.answers, [("c1", "알 수 없는 버튼이에요")], bad)

    def test_unknown_id_is_already_gone(self):
        self.seed("남음")
        _, api = self.poll([press("k:deadbeef:del")])
        self.assertEqual(api.answers, [("c1", memory.GONE)])
        self.assertEqual(len(self.items()), 1)

    # --- lock and logs -----------------------------------------------------
    def test_replies_are_sent_outside_the_state_lock(self):
        # MemApi.send raises AssertionError if the lock is still held; poll() uses it by default.
        _, api = self.poll([msg("/기억 글"), msg("/기억", uid=2)])
        self.assertEqual(len(api.sent), 2)

    def test_log_never_contains_memory_text(self):
        with self.assertLogs("aide", level=logging.DEBUG) as cm:
            logging.getLogger("aide.inbox").info("start")
            self.poll([msg(f"/기억 {SECRET}"), msg("/기억", uid=2)])
            rid = self.items()[0]["id"]
            self.poll([press(f"k:{rid}:del", uid=3)])
        self.assertNotIn(SECRET, "\n".join(cm.output))
        self.assertTrue(any("기억" in line for line in cm.output))

    def test_other_commands_unchanged_when_memory_on(self):
        with mock.patch.object(commands, "run", return_value="OK") as run:
            _, api = self.poll([msg("오늘")])
        run.assert_called_once()
        self.assertEqual(api.sent, [(OWNER, "OK")])


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"

    def tearDown(self):
        self.dir.cleanup()

    def cfg(self, **kw):
        base = dict(state_path=str(self.path), commands=["오늘", "일정"], llm_enabled=True, llm_profile="교사",
                    chat_enabled=True)
        base.update(kw)
        return Config(**base)

    def write(self, *texts):
        m = memory.Memory(memory.path_for(self.path))
        for t in texts:
            m.add(t, NOW)
        m.save()

    FINDINGS = [Finding(key="a", source="mail", title="제목", detail="d")]
    ITEMS = [{"kind": "공지", "when": "10/05", "text": "행사"}]

    def bodies(self, cfg):
        return (llm._build_request(self.FINDINGS, [0], cfg),
                llm._build_pick_request(self.ITEMS, cfg, "맑음"),
                chat._build_request("내일 일정", cfg, date(2026, 10, 3)))

    def test_off_or_empty_requests_are_identical_to_before(self):
        base = self.bodies(self.cfg())
        self.write("이건 꺼져 있으면 안 보여야 함")
        self.assertEqual(self.bodies(self.cfg()), base)                         # off, file exists
        os.remove(memory.path_for(self.path))
        self.assertEqual(self.bodies(self.cfg(memory_enabled=True)), base)      # on, no file
        self.write()                                                            # on, empty
        self.assertEqual(self.bodies(self.cfg(memory_enabled=True)), base)

    def test_damaged_file_gives_the_plain_request(self):
        base = self.bodies(self.cfg())
        memory.path_for(self.path).write_text("{ nope", encoding="utf-8")
        self.assertEqual(self.bodies(self.cfg(memory_enabled=True)), base)

    def test_on_adds_numbered_block_after_profile(self):
        self.write("교육청 공문은 항상 중요", "광고는 낮음")
        req, pick, ask = self.bodies(self.cfg(memory_enabled=True))
        block = memory.PROMPT_HEADER + "\n1. 교육청 공문은 항상 중요\n2. 광고는 낮음"
        for body in (req, pick):
            sysmsg = body["system"]
            self.assertTrue(sysmsg.endswith("사용자가 적어 둔 설명(참고용): 교사\n\n" + block), sysmsg)
        self.assertTrue(ask["system"].endswith("\n\n" + block))
        # only the system text changed; the user message and schema are untouched
        base = self.bodies(self.cfg())
        for got, want in zip((req, pick, ask), base):
            self.assertEqual(got["messages"], want["messages"])
            self.assertEqual(got["output_config"], want["output_config"])

    def test_cap_keeps_newest_items_and_stays_under_limit(self):
        self.write(*[f"{n:02d}" + "가" * 190 for n in range(40)])
        block = memory.prompt_block(self.cfg(memory_enabled=True))
        self.assertLessEqual(len(block), memory.PROMPT_LIMIT)
        self.assertIn("39" + "가" * 190, block)
        self.assertNotIn("00" + "가" * 190, block)
        lines = block.split("\n")[1:]
        self.assertTrue(lines[0].startswith("1. "))
        self.assertTrue(lines[-1].endswith("가" * 190))

    def test_prompt_block_is_off_without_memory_enabled(self):
        self.write("x")
        self.assertEqual(memory.prompt_block(self.cfg()), "")


class ConfigAndHelpTests(unittest.TestCase):
    def load(self, **extra):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps(extra), encoding="utf-8")
            return load_config(p)

    def test_default_off(self):
        self.assertFalse(Config().memory_enabled)
        self.assertFalse(self.load().memory_enabled)

    def test_on_requires_commands(self):
        with self.assertRaises(ConfigError):
            self.load(memory_enabled=True)
        self.assertTrue(self.load(memory_enabled=True, commands=["오늘"]).memory_enabled)

    def test_must_be_a_bool(self):
        with self.assertRaises(ConfigError):
            self.load(memory_enabled="yes", commands=["오늘"])

    def test_example_config_lists_it_off(self):
        data = json.loads((Path(__file__).resolve().parent.parent / "config.example.json").read_text(encoding="utf-8"))
        self.assertIs(data["memory_enabled"], False)

    def test_help_mentions_memory_and_anthropic_only_when_on(self):
        on = commands.render_help(Config(commands=["오늘"], memory_enabled=True), NOW)
        self.assertIn("/기억", on)
        self.assertIn("Anthropic", on)
        off = commands.render_help(Config(commands=["오늘"]), NOW)
        self.assertNotIn("/기억", off)
        self.assertNotIn("Anthropic", off)

    def test_gitignore_excludes_memory_files(self):
        text = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/memory.json", text)
        self.assertIn("data/memory.json.tmp", text)


class SelfcheckTests(unittest.TestCase):
    def run_selfcheck(self, **cfg):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        from aide.__main__ import main
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.json"
            cfg_path = Path(d) / "config.json"
            cfg_path.write_text(json.dumps({"state_path": str(state), **cfg}), encoding="utf-8")
            m = memory.Memory(memory.path_for(state))
            m.add(SECRET, NOW)
            m.add("둘째", NOW)
            m.save()
            out = StringIO()
            with redirect_stdout(out), redirect_stderr(StringIO()):
                code = main(["selfcheck", "--config", str(cfg_path)])
            return code, out.getvalue()

    def test_shows_count_but_never_content(self):
        code, out = self.run_selfcheck(memory_enabled=True, commands=["오늘"])
        self.assertEqual(code, 0)
        self.assertIn("memory           : 켜짐 (2개)", out)
        self.assertNotIn(SECRET, out)

    def test_off(self):
        _, out = self.run_selfcheck()
        self.assertIn("memory           : 꺼짐", out)


if __name__ == "__main__":
    unittest.main()
