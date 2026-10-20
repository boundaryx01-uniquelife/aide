import _bootstrap  # noqa: F401
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from aide import accounts, chat, commands, gcal, gmail, google_auth, inbox, netutil, weather
from aide.config import Config, ConfigError, load_config
from aide.models import Finding
from aide.state import State, StateLocked, locked

from test_inbox import FakeApi, msg

NOW = datetime(2026, 10, 3, 9, 0)   # a Saturday
TODAY = NOW.date()
OWNER = "111"
TWO = [{"name": "개인", "token_path": "data/t1.json"}, {"name": "학교", "token_path": "data/t2.json"}]


def make_cfg(**kw):
    base = dict(llm_enabled=True, chat_enabled=True, calendar_enabled=True, weather_enabled=True,
                mail_enabled=True, notice_pages=["http://x"], commands=["오늘", "일정", "마감", "메일", "날씨"],
                google_accounts=TWO)
    base.update(kw)
    return Config(**base)


def reply(**fields):
    obj = {"tool": "일정", "start": "2026-10-06", "days": 1, "account": "", "keyword": ""}
    obj.update(fields)
    return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}]}


class ParseQueryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def parse(self, data, cfg=None):
        return chat.parse_query(data, cfg or self.cfg, TODAY)

    def reason(self, data, cfg=None):
        with self.assertRaises(chat.Reject) as cm:
            chat._parse(data, cfg or self.cfg, TODAY)
        return cm.exception.reason

    def test_valid_query(self):
        q = self.parse(reply(tool="일정", start="2026-10-06", days=7, account="개인", keyword="치과"))
        self.assertEqual(q, chat.Query("일정", date(2026, 10, 6), 7, "개인", "치과"))

    def test_extra_or_missing_field_rejected(self):
        obj = json.loads(reply()["content"][0]["text"])
        for broken in ({**obj, "extra": 1}, {k: v for k, v in obj.items() if k != "days"}):
            data = {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(broken)}]}
            self.assertEqual(self.reason(data), "invalid")

    def test_unknown_tool_rejected(self):
        self.assertEqual(self.reason(reply(tool="삭제")), "invalid")
        self.assertEqual(self.reason(reply(tool="")), "invalid")

    def test_tool_not_enabled_in_commands_is_reported_as_off(self):
        cfg = make_cfg(commands=["일정"])
        self.assertEqual(self.reason(reply(tool="메일"), cfg), "off")
        self.assertIsNotNone(self.parse(reply(tool="일정"), cfg))

    def test_help_and_unknown_always_allowed(self):
        cfg = make_cfg(commands=["일정"])
        self.assertEqual(self.parse(reply(tool="도움"), cfg).tool, "도움")
        self.assertEqual(self.parse(reply(tool="모름"), cfg).tool, "모름")

    def test_refusal_or_truncated_rejected(self):
        for reason in ("refusal", "max_tokens", None):
            data = reply()
            data["stop_reason"] = reason
            self.assertEqual(self.reason(data), "invalid")

    def test_malformed_body_rejected(self):
        for data in (None, [], {}, {"stop_reason": "end_turn"}, {"stop_reason": "end_turn", "content": []},
                     {"stop_reason": "end_turn", "content": [{"type": "text", "text": "not json"}]},
                     {"stop_reason": "end_turn", "content": [{"type": "text", "text": "[1]"}]},
                     {"stop_reason": "end_turn", "content": [{"type": "text", "text": "[" * 100000}]}):
            self.assertIsNone(self.parse(data))

    def test_calendar_start_date_window(self):
        past, future = chat.PAST_DAYS, chat.FUTURE_DAYS
        self.assertIsNotNone(self.parse(reply(start=(TODAY - timedelta(days=past)).isoformat())))
        self.assertEqual(self.reason(reply(start=(TODAY - timedelta(days=past + 1)).isoformat())), "range")
        self.assertIsNotNone(self.parse(reply(start=(TODAY + timedelta(days=future)).isoformat())))
        self.assertEqual(self.reason(reply(start=(TODAY + timedelta(days=future + 1)).isoformat())), "range")

    def test_other_tools_never_reject_over_the_date(self):
        for tool in ("마감", "메일", "오늘", "도움", "모름"):
            q = self.parse(reply(tool=tool, start="2026-01-01"))
            self.assertIsNotNone(q, tool)
            self.assertEqual(q.start, TODAY, tool)       # the date means nothing there

    def test_start_must_be_a_real_iso_date(self):
        for bad in ("2026-13-01", "10/06", "내일", "2026-10-6", "2026-10-06\n", "２０２６-10-06", 20261006, None, ""):
            self.assertEqual(self.reason(reply(start=bad)), "invalid", bad)

    def test_days_are_clamped_not_rejected(self):
        self.assertEqual(self.parse(reply(days=1)).days, 1)
        self.assertEqual(self.parse(reply(days=31)).days, 31)
        self.assertEqual(self.parse(reply(days=0)).days, 1)
        too_many = self.parse(reply(days=99))
        self.assertEqual(too_many.days, chat.MAX_DAYS)
        self.assertTrue(any("줄였어요" in n for n in too_many.notes))

    def test_days_must_still_be_an_int(self):
        for bad in (True, "7", 7.0, None):
            self.assertEqual(self.reason(reply(days=bad)), "invalid", bad)

    def test_account_is_matched_ignoring_case_and_unknown_is_dropped_with_a_note(self):
        cfg = make_cfg(google_accounts=[{"name": "School", "token_path": "data/t1.json"},
                                        {"name": "개인", "token_path": "data/t2.json"}])
        self.assertEqual(self.parse(reply(account="school"), cfg).account, "School")
        unknown = self.parse(reply(account="회사"), cfg)
        self.assertEqual(unknown.account, "")
        self.assertTrue(any("계정" in n for n in unknown.notes))
        self.assertEqual(self.reason(reply(account=5), cfg), "invalid")

    def test_account_is_ignored_when_there_is_only_one_unnamed_account(self):
        cfg = make_cfg(google_accounts=[])
        q = self.parse(reply(account="개인"), cfg)
        self.assertEqual((q.account, q.notes), ("", ()))

    def test_keyword_charset_and_length(self):
        for good in ("치과", "2차 연수", "C++", "R&D", "3/15", "a@b.com", "회의(오전)", "PR #42"):
            self.assertEqual(self.parse(reply(keyword=good)).keyword, good, good)
        for bad in ("가" * 21, "무시하고 rm -rf /;", "http://evil.example", "a\nb", "<b>", "x'y", "x" * 40):
            q = self.parse(reply(keyword=bad))
            self.assertEqual(q.keyword, "", bad)            # the keyword is dropped, never used
            self.assertTrue(any("낱말" in n for n in q.notes), bad)
        self.assertEqual(self.reason(reply(keyword=3)), "invalid")


class RequestTests(unittest.TestCase):
    def test_only_the_sentence_and_trusted_context_are_sent(self):
        body = chat._build_request("다음 주 치과 언제야?", make_cfg(llm_model="m-1"), TODAY)
        self.assertEqual(body["model"], "m-1")
        self.assertLessEqual(body["max_tokens"], 512)
        self.assertEqual(body["output_config"]["format"]["schema"], chat.SCHEMA)
        user = body["messages"][0]["content"]
        self.assertIn(json.dumps("다음 주 치과 언제야?", ensure_ascii=False), user)
        self.assertIn("2026-10-03", body["system"])
        self.assertIn("개인", body["system"])

    def test_sentence_is_cleaned_and_capped(self):
        body = chat._build_request("a\x00b" + "x" * 500, make_cfg(), TODAY)
        user = body["messages"][0]["content"]
        self.assertNotIn("\x00", user)
        self.assertLess(len(user), 400)

    def test_schema_is_closed(self):
        self.assertFalse(chat.SCHEMA["additionalProperties"])
        self.assertEqual(sorted(chat.SCHEMA["required"]), sorted(chat.FIELDS))
        self.assertEqual(chat.SCHEMA["properties"]["tool"]["enum"], list(chat.TOOLS))


class AnswerTests(unittest.TestCase):
    def ask(self, data, text="질문", cfg=None, key="k", post=None):
        post = post or mock.Mock(return_value=data)
        out = chat.answer_full(chat.ChatRequest(text), cfg or make_cfg(), NOW, api_key=key, post=post)
        return out, post

    def test_week_query_runs_render_week_with_validated_arguments(self):
        with mock.patch.object(commands, "render_week", return_value="BODY") as rw:
            (out, refund), post = self.ask(reply(tool="일정", start="2026-10-06", days=3, account="개인", keyword="치과"))
        kw = rw.call_args.kwargs
        self.assertEqual((kw["first"], kw["days"], kw["only"], kw["keyword"]), (date(2026, 10, 6), 3, "개인", "치과"))
        self.assertTrue(out.startswith("▸ 찾아본 것: 10/06(화)~10/08(목) 일정 · 개인 · '치과'"))
        self.assertIn("BODY", out)
        self.assertFalse(refund)
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "k")

    def test_due_query(self):
        with mock.patch.object(commands, "render_due", return_value="BODY") as rd:
            (out, _), _ = self.ask(reply(tool="마감", days=14, keyword="연수"))
        self.assertEqual((rd.call_args.kwargs["days"], rd.call_args.kwargs["keyword"]), (14, "연수"))
        self.assertIn("오늘부터 14일 이내 마감", out)

    def test_mail_days_are_capped_at_a_week_and_echo_says_so(self):
        with mock.patch.object(commands, "render_mail", return_value="BODY") as rm:
            (out, _), _ = self.ask(reply(tool="메일", days=30, account="학교"))
        self.assertEqual((rm.call_args.kwargs["days"], rm.call_args.kwargs["only"]), (7, "학교"))
        self.assertIn("최근 7일 안 읽은 메일 · 학교", out)

    def test_weather_tomorrow_says_which_place(self):
        with mock.patch.object(commands, "render_weather", return_value="BODY") as rw:
            (out, _), _ = self.ask(reply(tool="날씨", start="2026-10-04"))
        self.assertEqual(rw.call_args.kwargs["offset"], 1)
        self.assertIn("10/04(일) 날씨 (설정된 위치 기준)", out)

    def test_weather_outside_the_forecast_window_is_refused_locally(self):
        with mock.patch.object(commands, "render_weather") as rw:
            (far, _), _ = self.ask(reply(tool="날씨", start="2026-10-20"))
            (past, _), _ = self.ask(reply(tool="날씨", start="2026-09-30"))
        rw.assert_not_called()
        self.assertIn(f"{weather.MAX_OFFSET}일 뒤까지", far)
        self.assertIn(f"{weather.MAX_OFFSET}일 뒤까지", past)
        self.assertNotIn("7일 이내", far)

    def test_today_summary(self):
        with mock.patch.object(commands, "render_today", return_value="BODY"):
            (out, _), _ = self.ask(reply(tool="오늘"))
        self.assertIn("오늘 요약", out)

    def test_help(self):
        (out, _), _ = self.ask(reply(tool="도움"))
        self.assertIn("/일정", out)
        self.assertIn("문장으로 물어봐도", out)

    def test_unknown_request_says_what_it_can_and_cannot_do(self):
        (out, refund), _ = self.ask(reply(tool="모름"))
        self.assertEqual(out, chat.cant_do(make_cfg()))
        self.assertIn("못 해요", out)
        self.assertFalse(refund)

    def test_each_failure_has_its_own_message_and_only_unbilled_ones_refund(self):
        cfg = make_cfg()
        (invalid, r1), _ = self.ask(reply(days="x"))
        (rng, r2), _ = self.ask(reply(start="2030-01-01"))
        (off, r3), _ = self.ask(reply(tool="메일"), cfg=make_cfg(commands=["일정"]))
        self.assertEqual(invalid, chat.not_understood(cfg))
        self.assertEqual(rng, chat.RANGE_TEXT)
        self.assertEqual(off, chat.off_text(make_cfg(commands=["일정"])))
        self.assertEqual((r1, r2, r3), (False, False, False))       # these answers were generated and billed

    def test_invalid_answer_is_never_used(self):
        for bad in (reply(days=None), reply(tool="삭제"), {"stop_reason": "refusal", "content": []}):
            with mock.patch.object(commands, "render_week") as rw:
                (out, _), _ = self.ask(bad)
            rw.assert_not_called()
            self.assertEqual(out, chat.not_understood(make_cfg()))

    def test_no_api_key_means_no_request_and_a_refund(self):
        (out, refund), post = self.ask(reply(), key="")
        post.assert_not_called()
        self.assertEqual((out, refund), (chat.UNAVAILABLE, True))

    def test_a_key_with_a_newline_never_reaches_the_http_layer(self):
        for key in ("sk-abc\ndef", "sk-abc def", "sk-한글", "sk-abc\r"):
            (out, refund), post = self.ask(reply(), key=key)
            post.assert_not_called()
            self.assertEqual((out, refund), (chat.UNAVAILABLE, True))

    def test_network_failure_says_so_and_refunds(self):
        (out, refund), _ = self.ask(None, post=mock.Mock(side_effect=netutil.NetError("down")))
        self.assertEqual((out, refund), (chat.CONNECTION, True))

    def test_unexpected_error_is_caught_and_its_text_not_logged(self):
        with self.assertLogs("aide", level="DEBUG") as logs:
            (out, refund), _ = self.ask(None, post=mock.Mock(side_effect=RuntimeError("SECRET-KEY-TEXT")))
        self.assertEqual((out, refund), (chat.UNAVAILABLE, True))
        self.assertNotIn("SECRET-KEY-TEXT", "\n".join(logs.output))

    def test_renderer_error_is_caught(self):
        with mock.patch.object(commands, "render_week", side_effect=RuntimeError("boom")):
            (out, _), _ = self.ask(reply())
        self.assertIn("오류", out)

    def test_sentence_and_keyword_never_reach_the_log(self):
        with mock.patch.object(commands, "render_week", return_value="BODY"), \
                self.assertLogs("aide", level="DEBUG") as logs:
            self.ask(reply(keyword="비밀낱말"), text="비밀문장 치과 언제야")
        text = "\n".join(logs.output)
        self.assertNotIn("비밀문장", text)
        self.assertNotIn("비밀낱말", text)

    def test_the_only_model_text_on_screen_is_the_validated_keyword(self):
        with mock.patch.object(commands, "render_week", return_value="BODY"):
            (out, _), _ = self.ask(reply(keyword="치과"))
        self.assertEqual(out.splitlines()[0], "▸ 찾아본 것: 10/06(화) 일정 · '치과'")

    def test_echo_has_no_particle_to_get_wrong(self):
        with mock.patch.object(commands, "render_week", return_value="BODY"), \
                mock.patch.object(commands, "render_due", return_value="BODY"), \
                mock.patch.object(commands, "render_today", return_value="BODY"):
            for tool in ("일정", "마감", "오늘"):
                (out, _), _ = self.ask(reply(tool=tool))
                first = out.splitlines()[0]
                self.assertNotIn("로 찾아봤어요", first)
                self.assertTrue(first.startswith("▸ 찾아본 것: "))

    def test_repairs_are_told_to_the_owner(self):
        with mock.patch.object(commands, "render_week", return_value="BODY") as rw:
            (out, _), _ = self.ask(reply(days=99, account="회사", keyword="가" * 30))
        self.assertEqual((rw.call_args.kwargs["days"], rw.call_args.kwargs["only"], rw.call_args.kwargs["keyword"]),
                         (31, "", ""))
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("※ ")), 3)

    def test_part_of_day_is_admitted_to_be_unsupported(self):
        with mock.patch.object(commands, "render_week", return_value="BODY"):
            (out, _), _ = self.ask(reply(), text="화요일 오후 일정")
        self.assertIn("오전/오후는 구분하지 못해", out)
        with mock.patch.object(commands, "render_week", return_value="BODY"):
            (plain, _), _ = self.ask(reply(), text="화요일 일정")
        self.assertNotIn("오전/오후", plain)

    def test_keyword_is_admitted_to_be_unused_for_weather_and_today(self):
        with mock.patch.object(commands, "render_weather", return_value="BODY"):
            (out, _), _ = self.ask(reply(tool="날씨", start="2026-10-04", keyword="소풍"))
        self.assertIn("낱말 찾기는 일정·마감·메일에서만", out)

    def test_empty_keyword_search_says_which_dates_it_looked_at(self):
        with mock.patch.object(commands, "render_week", return_value="■ 일정\n없어요."):
            (out, _), _ = self.ask(reply(keyword="치과"))
        self.assertIn(chat.NO_RESULT_HINT, out)
        with mock.patch.object(commands, "render_week", return_value="■ 일정\n• 치과"):
            (found, _), _ = self.ask(reply(keyword="치과"))
        self.assertNotIn(chat.NO_RESULT_HINT, found)
        with mock.patch.object(commands, "render_week", return_value="■ 일정\n없어요."):
            (nokw, _), _ = self.ask(reply())
        self.assertNotIn(chat.NO_RESULT_HINT, nokw)

    def test_answer_wrapper_returns_just_the_text(self):
        out = chat.answer(chat.ChatRequest("x"), make_cfg(), NOW, api_key="", post=mock.Mock())
        self.assertEqual(out, chat.UNAVAILABLE)


class ExamplesAndWordingTests(unittest.TestCase):
    def test_examples_only_advertise_what_is_switched_on(self):
        cfg = make_cfg(commands=["일정"])
        for text in (chat.not_understood(cfg), chat.cant_do(cfg), commands.render_help(cfg, NOW)):
            self.assertIn("일정", text)
            self.assertNotIn("날씨", text)
            self.assertNotIn("메일", text)

    def test_help_explains_bare_words_and_warns_about_what_leaves_the_machine(self):
        out = commands.render_help(make_cfg(), NOW)
        self.assertIn("한 단어만 써도", out)
        self.assertIn("Anthropic", out)
        self.assertIn("개인정보", out)
        self.assertNotIn("Anthropic", commands.render_help(make_cfg(chat_enabled=False), NOW))

    def test_limit_messages_use_plain_words(self):
        for text in (chat.OUT_OF_BUDGET, chat.TOO_LONG, chat.CHAT_BUSY, chat.CONNECTION):
            self.assertNotIn("/명령", text)
            self.assertNotIn("대화 한도", text)


class LooksLikeChatTests(unittest.TestCase):
    def test_sentences_yes_slash_no(self):
        self.assertTrue(chat.looks_like_chat("내일 일정 뭐야?"))
        self.assertFalse(chat.looks_like_chat("/일정 내일"))
        self.assertFalse(chat.looks_like_chat("  /rm"))
        self.assertFalse(chat.looks_like_chat("   "))
        self.assertFalse(chat.looks_like_chat(None))

    def test_full_width_slash_and_invisible_prefixes_are_commands_too(self):
        self.assertFalse(chat.looks_like_chat("／삭제 전부"))
        self.assertFalse(chat.looks_like_chat("​/일정 알려줘"))      # zero-width space first
        self.assertFalse(chat.looks_like_chat("‮/x"))               # right-to-left override first
        self.assertTrue(chat.looks_like_chat("일정 / 메일"))


class FakeClockApi(FakeApi):
    """A Telegram double that moves the clock while 'long polling', like a 25 s wait across midnight."""

    def __init__(self, updates, clock, after):
        super().__init__(updates)
        self.clock, self.after = clock, after

    def get_updates(self, token, offset=0, timeout=0, allowed=("callback_query",)):
        got = super().get_updates(token, offset, timeout, allowed)
        self.clock["now"] = self.after
        return got


class LockCheckingApi(FakeApi):
    """Fails the test if anything is sent to Telegram while the state lock is held."""

    def __init__(self, updates, path):
        super().__init__(updates)
        self.path = path

    def send(self, text, *, token, chat_id, buttons=None):
        try:
            with locked(self.path, timeout=0.3):
                pass
        except StateLocked:
            raise AssertionError("sent while the state lock was held") from None
        return super().send(text, token=token, chat_id=chat_id, buttons=buttons)


class InboxChatTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"

    def tearDown(self):
        self.dir.cleanup()

    def cfg(self, **kw):
        base = dict(state_path=str(self.path), commands=["오늘", "일정"], llm_enabled=True, chat_enabled=True)
        base.update(kw)
        return Config(**base)

    def poll(self, cfg, updates, answer=("ANSWER", False), api=None, now=NOW):
        api = api or FakeApi(updates)
        with mock.patch.object(chat, "answer_full", return_value=answer) as ans:
            n = inbox.poll_once(cfg, token="t", chat_id=OWNER, api=api, now=now)
        return n, api, ans

    def test_off_by_default_a_sentence_is_just_unknown(self):
        n, api, ans = self.poll(self.cfg(chat_enabled=False), [msg("내일 일정 뭐야?")])
        ans.assert_not_called()
        self.assertEqual(api.sent, [(OWNER, "모르는 명령이에요. /도움")])
        self.assertEqual(State(self.path).chat_calls_today(NOW), 0)

    def test_sentence_is_answered_and_budget_spent(self):
        n, api, ans = self.poll(self.cfg(), [msg("내일 일정 뭐야?")])
        self.assertEqual(n, 1)
        self.assertEqual(api.sent, [(OWNER, "ANSWER")])
        self.assertEqual(ans.call_args.args[0], chat.ChatRequest("내일 일정 뭐야?"))
        st = State(self.path)
        self.assertEqual(st.chat_calls_today(NOW), 1)
        self.assertEqual(st.llm_calls_today(NOW), 0)               # the 2a/2b budget is untouched
        self.assertEqual(st.commands_in_last_hour(NOW), 0)         # and so is the /명령 allowance

    def test_api_key_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            _, _, ans = self.poll(self.cfg(), [msg("내일 일정 뭐야?")])
        self.assertEqual(ans.call_args.kwargs["api_key"], "sk-test")

    def test_command_words_still_use_the_fixed_path(self):
        with mock.patch.dict(commands.REGISTRY, {"오늘": lambda cfg, now: "TODAY"}):
            n, api, ans = self.poll(self.cfg(), [msg("/오늘"), msg("오늘", uid=2)])
        ans.assert_not_called()
        self.assertEqual([t for _, t in api.sent], ["TODAY", "TODAY"])
        self.assertEqual(State(self.path).chat_calls_today(NOW), 0)

    def test_slash_text_is_never_sent_to_the_model(self):
        n, api, ans = self.poll(self.cfg(), [msg("/삭제 전부"), msg("/메일", uid=2), msg("／삭제", uid=3)])
        ans.assert_not_called()
        self.assertEqual([t for _, t in api.sent], ["모르는 명령이에요. /도움"])    # once, however many

    def test_too_long_is_refused_once_without_spending(self):
        n, api, ans = self.poll(self.cfg(), [msg("가" * (chat.MAX_CHAT_LEN + 1)), msg("나" * 300, uid=2)])
        ans.assert_not_called()
        self.assertEqual(api.sent, [(OWNER, chat.TOO_LONG)])
        self.assertEqual(State(self.path).chat_calls_today(NOW), 0)

    def test_exactly_max_length_is_allowed(self):
        _, _, ans = self.poll(self.cfg(), [msg("가" * chat.MAX_CHAT_LEN)])
        ans.assert_called_once()

    def test_daily_budget_stops_chat_but_not_commands(self):
        st = State(self.path)
        for _ in range(3):
            st.record_chat_call(NOW - timedelta(hours=5))
        st.save()
        with mock.patch.dict(commands.REGISTRY, {"오늘": lambda cfg, now: "TODAY"}):
            n, api, ans = self.poll(self.cfg(chat_max_per_day=3),
                                    [msg("질문1"), msg("질문2", uid=2), msg("/오늘", uid=3)])
        ans.assert_not_called()
        texts = [t for _, t in api.sent]
        self.assertEqual(texts.count(chat.OUT_OF_BUDGET), 1)     # one notice, not one per sentence
        self.assertIn("TODAY", texts)

    def test_budget_is_kept_when_the_answer_was_billed(self):
        self.poll(self.cfg(), [msg("질문")], answer=(chat.not_understood(make_cfg()), False))
        self.assertEqual(State(self.path).chat_calls_today(NOW), 1)

    def test_budget_is_handed_back_when_no_answer_was_generated(self):
        n, api, ans = self.poll(self.cfg(), [msg("질문1"), msg("질문2", uid=2)], answer=(chat.CONNECTION, True))
        st = State(self.path)
        self.assertEqual((st.chat_calls_today(NOW), st.chats_in_last_hour(NOW)), (0, 0))
        self.assertEqual([t for _, t in api.sent], [chat.CONNECTION, chat.CONNECTION])

    def test_budget_is_per_day(self):
        st = State(self.path)
        for _ in range(20):
            st.record_chat_call(datetime(2026, 10, 2, 9, 0))
        st.save()
        _, _, ans = self.poll(self.cfg(), [msg("질문")])
        ans.assert_called_once()

    def test_stranger_group_forwarded_and_stale_are_not_chat(self):
        old = int(NOW.timestamp()) - commands.STALE_SECONDS - 5
        n, api, ans = self.poll(self.cfg(), [
            msg("질문", sender="999", chat="999"),
            msg("질문", uid=2, chat="-100", chat_type="group"),
            msg("질문", uid=3, forwarded=True),
            msg("질문", uid=4, is_bot=True),
        ])
        ans.assert_not_called()
        self.assertEqual(api.sent, [])
        n, api, ans = self.poll(self.cfg(), [msg("질문", uid=5, ts=old)])
        ans.assert_not_called()
        self.assertEqual(api.sent, [(OWNER, "늦게 받은 명령은 건너뛰었어요.")])
        self.assertEqual(State(self.path).chat_calls_today(NOW), 0)

    def test_chat_shares_the_per_batch_limit_with_commands(self):
        updates = [msg(f"질문{i}", uid=i) for i in range(1, commands.MAX_PER_BATCH + 3)]
        n, api, ans = self.poll(self.cfg(), updates)
        self.assertEqual(ans.call_count, commands.MAX_PER_BATCH)
        self.assertEqual(State(self.path).chat_calls_today(NOW), commands.MAX_PER_BATCH)
        self.assertEqual([t for _, t in api.sent].count(chat.CHAT_BUSY), 1)

    def test_chat_has_its_own_hourly_limit_that_never_blocks_commands(self):
        st = State(self.path)
        for _ in range(chat.MAX_PER_HOUR):
            st.record_chat_call(NOW - timedelta(minutes=10))
        st.save()
        with mock.patch.dict(commands.REGISTRY, {"오늘": lambda cfg, now: "TODAY"}):
            n, api, ans = self.poll(self.cfg(), [msg("질문"), msg("/오늘", uid=2)])
        ans.assert_not_called()
        texts = [t for _, t in api.sent]
        self.assertIn("TODAY", texts)
        self.assertEqual(texts.count(chat.CHAT_BUSY), 1)

    def test_a_chat_flood_leaves_the_command_allowance_untouched(self):
        st = State(self.path)
        for _ in range(commands.MAX_PER_HOUR - 1):
            st.record_chat_call(NOW - timedelta(hours=2))      # old, outside the hour
        st.save()
        self.poll(self.cfg(chat_max_per_day=200), [msg(f"질문{i}", uid=i) for i in range(1, 4)])
        self.assertEqual(State(self.path).commands_in_last_hour(NOW), 0)

    def test_redelivered_messages_are_not_run_billed_or_answered_twice(self):
        st = State(self.path)
        st.offset = 50
        st.save()
        n, api, ans = self.poll(self.cfg(), [msg("질문", uid=49), msg("/오늘", uid=48)])
        ans.assert_not_called()
        self.assertEqual((n, api.sent), (0, []))
        self.assertEqual(State(self.path).chat_calls_today(NOW), 0)
        n, api, ans = self.poll(self.cfg(), [msg("질문", uid=50)])
        ans.assert_called_once()

    def test_a_flood_of_junk_sends_at_most_one_notice_per_kind(self):
        updates = [msg("/x%d" % i, uid=i) for i in range(1, 40)]
        n, api, ans = self.poll(self.cfg(), updates)
        self.assertEqual(len(api.sent), 1)

    def test_nothing_is_sent_to_telegram_while_the_state_lock_is_held(self):
        api = LockCheckingApi([msg("/x", uid=1), msg("가" * 300, uid=2), msg("질문", uid=3)], self.path)
        n, api, ans = self.poll(self.cfg(), None, api=api)
        self.assertEqual(len(api.sent), 3)      # unknown notice, too-long notice, the answer

    def test_the_clock_is_read_after_the_long_wait(self):
        clock = {"now": datetime(2026, 10, 3, 23, 59, 50)}

        class Clock:
            @staticmethod
            def now():
                return clock["now"]

        sent_at = int(datetime(2026, 10, 3, 23, 59, 55).timestamp())
        api = FakeClockApi([msg("질문", ts=sent_at)], clock, datetime(2026, 10, 4, 0, 0, 10))
        with mock.patch.object(inbox, "datetime", Clock):
            _, _, ans = self.poll(self.cfg(), None, api=api, now=None)
        self.assertEqual(ans.call_args.args[2].date(), date(2026, 10, 4))        # "tomorrow" is not "today"
        self.assertEqual(State(self.path).chat_calls_today(datetime(2026, 10, 4, 0, 5)), 1)

    def test_messages_are_requested_from_telegram(self):
        _, api, _ = self.poll(self.cfg(), [])
        self.assertEqual(api.allowed_seen, [("callback_query", "message")])


class NetutilKeyLeakTests(unittest.TestCase):
    def test_a_bad_header_value_becomes_a_neutral_error_without_the_value(self):
        with self.assertRaises(netutil.NetError) as cm:
            netutil.post_json("https://api.example.invalid/x", {}, headers={"x-api-key": "sk-SECRET\nmore"})
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertTrue(cm.exception.__suppress_context__)


class StateBudgetTests(unittest.TestCase):
    def test_chat_budget_is_separate_persisted_and_pruned(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            st = State(p)
            st.record_chat_call(NOW)
            st.record_chat_call(NOW)
            st.record_llm_call(NOW)
            st.chat_calls["2026-01-01"] = 5
            st.save()
            again = State(p)
            self.assertEqual(again.chat_calls_today(NOW), 2)
            self.assertEqual(again.llm_calls_today(NOW), 1)
            self.assertEqual(again.chats_in_last_hour(NOW), 2)
            self.assertNotIn("2026-01-01", again.chat_calls)

    def test_refund_gives_one_back_and_never_goes_below_zero(self):
        with tempfile.TemporaryDirectory() as d:
            st = State(Path(d) / "s.json")
            st.refund_chat_call(NOW)
            self.assertEqual(st.chat_calls_today(NOW), 0)
            st.record_chat_call(NOW)
            st.record_chat_call(NOW)
            st.refund_chat_call(NOW)
            self.assertEqual((st.chat_calls_today(NOW), st.chats_in_last_hour(NOW)), (1, 1))

    def test_hourly_window_forgets_old_sentences(self):
        with tempfile.TemporaryDirectory() as d:
            st = State(Path(d) / "s.json")
            st.record_chat_call(NOW - timedelta(minutes=61))
            st.record_chat_call(NOW - timedelta(minutes=5))
            self.assertEqual(st.chats_in_last_hour(NOW), 1)

    def test_old_state_files_without_the_keys_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps({"seen": {}, "offset": 3}), encoding="utf-8")
            st = State(p)
            self.assertEqual((st.offset, st.chat_calls_today(NOW), st.chats_in_last_hour(NOW)), (3, 0, 0))


class ConfigTests(unittest.TestCase):
    def write(self, data):
        p = Path(tempfile.mkdtemp()) / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_default_is_off(self):
        cfg = Config()
        self.assertFalse(cfg.chat_enabled)
        self.assertEqual(cfg.chat_max_per_day, 20)

    def test_chat_needs_llm_and_commands(self):
        for data in ({"chat_enabled": True},
                     {"chat_enabled": True, "llm_enabled": True},
                     {"chat_enabled": True, "commands": ["오늘"]}):
            with self.assertRaises(ConfigError):
                load_config(self.write(data))
        cfg = load_config(self.write({"chat_enabled": True, "llm_enabled": True, "commands": ["오늘"]}))
        self.assertTrue(cfg.chat_enabled)

    def test_budget_range(self):
        for bad in (0, 201, True, "5", 2.5):
            with self.assertRaises(ConfigError):
                load_config(self.write({"chat_max_per_day": bad}))
        self.assertEqual(load_config(self.write({"chat_max_per_day": 5})).chat_max_per_day, 5)

    def test_chat_flag_must_be_boolean(self):
        with self.assertRaises(ConfigError):
            load_config(self.write({"chat_enabled": "yes"}))

    def test_weather_command_name_is_accepted(self):
        self.assertEqual(load_config(self.write({"commands": ["날씨"]})).commands, ["날씨"])


class ReusedRendererTests(unittest.TestCase):
    """The parameters chat adds to the fixed renderers, and that the defaults did not change."""

    def test_weather_alias_resolves(self):
        self.assertEqual(commands.resolve("/날씨", ["날씨"]), "날씨")
        self.assertEqual(commands.resolve("weather", ["날씨"]), "날씨")
        self.assertIsNone(commands.resolve("/날씨", ["오늘"]))
        self.assertEqual(commands.resolve("／날씨", ["날씨"]), "날씨")      # full-width slash

    def test_render_weather(self):
        w = weather.Weather("맑음", 12, 20, 10)
        seen = []
        out = commands.render_weather(make_cfg(), NOW, fetch=lambda la, lo, off: seen.append(off) or w, offset=2)
        self.assertEqual(seen, [2])
        self.assertIn("10/05, 월", out)
        self.assertIn("맑음", out)
        self.assertIn("꺼져 있어요", commands.render_weather(Config(), NOW))

    def test_render_weather_failure_degrades(self):
        def boom(la, lo, off):
            raise netutil.NetError("x")
        self.assertIn("불러오지 못했어요", commands.render_weather(make_cfg(), NOW, fetch=boom))

    def test_on_day_picks_the_right_day_and_asks_for_enough_days(self):
        urls = []
        data = {"daily": {"weather_code": [0, 3, 61], "temperature_2m_min": [1, 2, 3],
                          "temperature_2m_max": [10, 11, 12], "precipitation_probability_max": [0, 20, 70]}}
        w = weather.on_day(35.2, 129.08, 2, get=lambda u: urls.append(u) or data)
        self.assertEqual((w.low, w.high, w.rain_pct), (3, 12, 70))
        self.assertIn("forecast_days=3", urls[0])

    def test_on_day_rejects_out_of_range_offsets(self):
        for bad in (-1, 7, True, "1"):
            with self.assertRaises(netutil.NetError):
                weather.on_day(0, 0, bad, get=lambda u: {})

    def test_today_is_unchanged(self):
        data = {"daily": {"weather_code": [0], "temperature_2m_min": [1],
                          "temperature_2m_max": [10], "precipitation_probability_max": [0]}}
        self.assertEqual(weather.today(0, 0, get=lambda u: data).high, 10)

    def test_week_with_range_keyword_and_account(self):
        calls = []
        evs = [gcal.Event("10:00", "치과 예약", day=date(2026, 10, 6)),
               gcal.Event("11:00", "회의", day=date(2026, 10, 6))]

        def fetch(token, first, days):
            calls.append((token, first, days))
            return evs

        seen = []
        with mock.patch.object(google_auth, "access_token", side_effect=lambda c, p, **k: seen.append(p.name) or "T"):
            out = commands.render_week(make_cfg(), NOW, fetch_week=fetch, first=date(2026, 10, 6), days=3,
                                       only="학교", keyword="치과")
        self.assertEqual(seen, ["t2.json"])                 # only the chosen account was read
        self.assertEqual(calls[0][1:], (date(2026, 10, 6), 3))
        self.assertIn("10/06~10/08", out)
        self.assertIn("[학교] 치과 예약", out)              # labels as in the many-account view
        self.assertNotIn("회의", out)

    def test_week_defaults_unchanged(self):
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            out = commands.render_week(Config(calendar_enabled=True), NOW, fetch_week=lambda t, f, d: [])
        self.assertIn("10/03~10/09", out)

    def test_one_day_header_has_no_doubled_date(self):
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            out = commands.render_week(Config(calendar_enabled=True), NOW, fetch_week=lambda t, f, d: [],
                                       first=date(2026, 10, 6), days=1)
        self.assertIn("■ 일정 (10/06)", out)
        self.assertNotIn("~", out.splitlines()[0])

    def test_due_with_days_and_keyword(self):
        html = ('<ul><li><a href="/n/1">연수 신청 공고</a> ~2026-10-20</li>'
                '<li><a href="/n/2">대회 공모</a> ~2026-10-20</li></ul>').encode()
        cfg = Config(notice_pages=["http://x"], notice_keywords=["연수", "대회"])
        week = commands.render_due(cfg, NOW, fetch=lambda u: html)
        self.assertNotIn("연수 신청", week)                # 17 days away: outside the default 7
        out = commands.render_due(cfg, NOW, fetch=lambda u: html, days=20, keyword="연수")
        self.assertIn("20일 이내", out)
        self.assertIn("연수 신청 공고", out)
        self.assertNotIn("대회", out)

    def test_mail_with_days_account_and_keyword(self):
        cfg = make_cfg(mail_recent_hours=[8])
        seen = []

        def recent(token, max_items, window_hours, blocked, now):
            seen.append((max_items, window_hours))
            return [Finding(key="mail:1", source="mail", title="학교: 치과 안내", detail="…이 외에 3건 더"),
                    Finding(key="mail:2", source="mail", title="광고: 세일")]

        with mock.patch.object(google_auth, "access_token", return_value="T") as tok, \
                mock.patch.object(gmail, "recent_unread", side_effect=recent):
            out = commands.render_mail(cfg, NOW, days=2, only="학교", keyword="치과")
        self.assertEqual(tok.call_count, 1)
        self.assertEqual(seen, [(commands.KEYWORD_MAIL_MAX, 48)])
        self.assertIn("치과 안내", out)
        self.assertNotIn("세일", out)
        self.assertNotIn("이 외에", out)                   # the "more mail" note is wrong for a filtered list
        self.assertIn(f"최신 {commands.KEYWORD_MAIL_MAX}건", out)

    def test_mail_keyword_does_not_match_the_account_label(self):
        cfg = make_cfg()
        mails = [Finding(key="mail:1", source="mail", title="광고: 세일")]
        with mock.patch.object(google_auth, "access_token", return_value="T"), \
                mock.patch.object(gmail, "recent_unread", return_value=mails):
            out = commands.render_mail(cfg, NOW, keyword="학교")      # "[학교]" is only a label we add
            allmail = commands.render_mail(cfg, NOW)
        self.assertNotIn("세일", out)
        self.assertIn("[학교] 광고: 세일", allmail)

    def test_mail_other_account_keeps_unique_keys_when_filtered(self):
        cfg = make_cfg()
        with mock.patch.object(google_auth, "access_token", return_value="T"), \
                mock.patch.object(gmail, "recent_unread",
                                  return_value=[Finding(key="mail:1", source="mail", title="x", url="")]):
            out = commands.render_mail(cfg, NOW, only="학교")
        self.assertIn("[학교]", out)

    def test_gather_events_only_unknown_account_reads_nothing(self):
        called = []
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            ev, failed, total = accounts.gather_events(make_cfg(), lambda t: called.append(t) or [], lambda e: e.when,
                                                       only="없는계정")
        self.assertEqual((ev, failed, total, called), ([], [], 0, []))

    def test_help_mentions_chat_only_when_enabled(self):
        on = commands.render_help(make_cfg(), NOW)
        off = commands.render_help(make_cfg(chat_enabled=False), NOW)
        self.assertIn("문장으로", on)
        self.assertNotIn("문장으로", off)
        self.assertIn("/날씨", on)


if __name__ == "__main__":
    unittest.main()
