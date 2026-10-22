import _bootstrap  # noqa: F401
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from aide import chat, commands, evening, google_auth, gtasks, morning, netutil
from aide.config import Config, ConfigError, load_config

NOW = datetime(2026, 10, 22, 9, 0)    # a Thursday
TODAY = NOW.date()
SUNDAY = datetime(2026, 10, 25, 20, 0)
TWO = [{"name": "개인", "token_path": "data/t1.json"}, {"name": "학교", "token_path": "data/t2.json"}]


def make_cfg(**kw):
    base = dict(google_accounts=TWO, tasks_accounts=["개인"])
    base.update(kw)
    return Config(**base)


def lists_body(*ids):
    return {"items": [{"id": i, "title": f"목록{i}"} for i in ids]}


def tasks_body(*items):
    return {"items": list(items)}


def fake_get(routes):
    """get(url, headers=...) answering from {list id or 'lists': body}; records the urls."""
    seen = []

    def get(url, headers=None):
        seen.append(url)
        if "/users/@me/lists" in url:
            body = routes["lists"]
        else:
            key = url.split("/lists/")[1].split("/")[0]
            body = routes[key]
        if isinstance(body, Exception):
            raise body
        return body

    get.seen = seen
    return get


class FetchTests(unittest.TestCase):
    def test_parses_open_tasks_of_every_list(self):
        get = fake_get({
            "lists": lists_body("a", "b"),
            "a": tasks_body({"title": "보고서", "due": "2026-10-23T00:00:00.000Z", "status": "needsAction"},
                            {"title": "끝낸 일", "status": "completed"},
                            {"title": "날짜 없음", "status": "needsAction"}),
            "b": tasks_body({"title": "장보기", "due": "2026-10-20T00:00:00.000Z"}),
        })
        tasks = gtasks.fetch("T", get=get)
        self.assertEqual([(t.title, t.due, t.list_title) for t in tasks], [
            ("보고서", date(2026, 10, 23), "목록a"), ("날짜 없음", None, "목록a"), ("장보기", date(2026, 10, 20), "목록b")])

    def test_only_titles_dates_and_status_are_requested_never_notes_or_links(self):
        get = fake_get({"lists": lists_body("a"), "a": tasks_body()})
        gtasks.fetch("T", get=get)
        for url in get.seen:
            self.assertNotIn("notes", url)
            self.assertNotIn("links", url)
            self.assertTrue(url.startswith("https://tasks.googleapis.com/"))
        self.assertIn("showCompleted=false", get.seen[1])
        self.assertIn("fields=items%28title%2Cdue%2Cstatus%29", get.seen[1])

    def test_list_ids_are_validated_and_encoded(self):
        get = fake_get({"lists": {"items": [{"id": "../../x", "title": "나쁨"}, {"id": "ok_1-A", "title": "좋음"},
                                            {"id": 5, "title": "숫자"}, "str", {"title": "id 없음"}]},
                        "ok_1-A": tasks_body({"title": "t"})})
        tasks = gtasks.fetch("T", get=get)
        self.assertEqual([t.title for t in tasks], ["t"])
        self.assertEqual(len(get.seen), 2)

    def test_at_most_ten_lists(self):
        ids = [f"l{i}" for i in range(15)]
        routes = {"lists": lists_body(*ids), **{i: tasks_body() for i in ids}}
        get = fake_get(routes)
        gtasks.fetch("T", get=get)
        self.assertEqual(len(get.seen), 1 + gtasks.MAX_LISTS)

    def test_one_broken_list_is_skipped_all_broken_raises(self):
        get = fake_get({"lists": lists_body("a", "b"), "a": netutil.NetError("x"), "b": tasks_body({"title": "ok"})})
        self.assertEqual([t.title for t in gtasks.fetch("T", get=get)], ["ok"])
        get = fake_get({"lists": lists_body("a", "b"), "a": netutil.NetError("x"), "b": netutil.NetError("y")})
        with self.assertRaises(netutil.NetError):
            gtasks.fetch("T", get=get)

    def test_no_lists_is_just_empty(self):
        self.assertEqual(gtasks.fetch("T", get=fake_get({"lists": {}})), [])
        self.assertEqual(gtasks.fetch("T", get=fake_get({"lists": []})), [])

    def test_titles_are_cleaned_and_capped_and_garbage_due_is_ignored(self):
        long = "가" * 200 + "\x00"
        get = fake_get({"lists": lists_body("a"), "a": tasks_body(
            {"title": long, "due": "not a date"}, {"title": "", "due": 5}, {"title": "x", "due": "2026-13-45T00:00:00Z"},
            "junk", None)})
        tasks = gtasks.fetch("T", get=get)
        self.assertEqual(len(tasks), 3)
        self.assertLessEqual(len(tasks[0].title), 80)
        self.assertNotIn("\x00", tasks[0].title)
        self.assertEqual(tasks[1].title, "(제목 없음)")
        self.assertTrue(all(t.due is None for t in tasks))

    def test_due_is_the_date_part_with_no_timezone_shift(self):
        self.assertEqual(gtasks._parse_due("2026-10-22T00:00:00.000Z"), date(2026, 10, 22))
        self.assertEqual(gtasks._parse_due("2026-10-22"), date(2026, 10, 22))
        self.assertIsNone(gtasks._parse_due(None))


class GatherTests(unittest.TestCase):
    def gather(self, cfg=None, fetch_fn=None, only="", access=None):
        cfg = cfg or make_cfg()
        fetch_fn = fetch_fn or (lambda token: [gtasks.Task("할 일", TODAY)])
        with mock.patch.object(google_auth, "access_token", side_effect=access or (lambda *a, **k: "T")) as tok:
            return gtasks.gather(cfg, fetch_fn, only), tok

    def test_only_the_chosen_account_is_read_with_the_tasks_scope(self):
        ((tasks, failed, total), tok) = self.gather()
        self.assertEqual((len(tasks), failed, total), (1, [], 1))
        self.assertEqual(tok.call_count, 1)
        self.assertEqual(tok.call_args.args[1].name, "t1.json")
        self.assertEqual(tok.call_args.kwargs["need_scope"], google_auth.TASKS_SCOPE)

    def test_off_when_no_tasks_accounts(self):
        ((tasks, failed, total), tok) = self.gather(make_cfg(tasks_accounts=[]))
        self.assertEqual((tasks, failed, total, tok.call_count), ([], [], 0, 0))

    def test_missing_permission_is_a_failed_account_not_a_crash(self):
        def refuse(*a, **k):
            raise google_auth.GoogleAuthError("권한 없음")
        ((tasks, failed, total), _) = self.gather(access=refuse)
        self.assertEqual((tasks, failed, total), ([], ["개인"], 1))

    def test_several_task_accounts_label_titles_and_survive_one_failure(self):
        cfg = make_cfg(tasks_accounts=["개인", "학교"])

        def access(client, path, **k):
            if path.name == "t2.json":
                raise google_auth.GoogleAuthError("x")
            return "T"
        ((tasks, failed, total), _) = self.gather(cfg, access=access)
        self.assertEqual((failed, total), (["학교"], 2))
        self.assertEqual(tasks[0].account, "개인")

    def test_single_task_account_has_no_label(self):
        ((tasks, _, _), _) = self.gather()
        self.assertEqual(tasks[0].account, "")

    def test_net_error_is_a_failed_account(self):
        ((_, failed, _), _) = self.gather(fetch_fn=mock.Mock(side_effect=netutil.NetError("down")))
        self.assertEqual(failed, ["개인"])

    def test_one_unnamed_account(self):
        cfg = Config(tasks_accounts=["기본"])
        self.assertEqual(len(cfg.task_accounts()), 1)
        ((tasks, _, _), _) = self.gather(cfg)
        self.assertEqual(len(tasks), 1)


class FormatTests(unittest.TestCase):
    def test_when_text(self):
        self.assertEqual(gtasks.when_text(date(2026, 10, 20), TODAY), "지남 10/20")
        self.assertEqual(gtasks.when_text(TODAY, TODAY), "오늘")
        self.assertEqual(gtasks.when_text(date(2026, 10, 23), TODAY), "내일")
        self.assertEqual(gtasks.when_text(date(2026, 10, 26), TODAY), "10/26(월)")

    def test_split_orders_oldest_first_and_separates_undated(self):
        ts = [gtasks.Task("c", date(2026, 10, 24)), gtasks.Task("a", date(2026, 10, 20)),
              gtasks.Task("b", date(2026, 10, 22)), gtasks.Task("u", None), gtasks.Task("far", date(2026, 12, 1))]
        dated, undated = gtasks.split(ts, TODAY, 1)
        self.assertEqual([t.title for t in dated], ["a", "b"])
        self.assertEqual([t.title for t in undated], ["u"])

    def test_lines_section(self):
        ts = [gtasks.Task("보고서", date(2026, 10, 20)), gtasks.Task("장보기", TODAY), gtasks.Task("메모", None)]
        out = gtasks.lines(ts, [], 1, TODAY, 0, title="할 일 (오늘까지)", show_undated=True)
        self.assertEqual(out, ["■ 할 일 (오늘까지)", "• 지남 10/20 보고서", "• 오늘 장보기", "날짜 없는 할 일 1개", ""])

    def test_nothing_to_say_is_no_section(self):
        self.assertEqual(gtasks.lines([], [], 1, TODAY, 0, title="x", show_undated=True), [])
        later = [gtasks.Task("나중", date(2026, 12, 1))]
        self.assertEqual(gtasks.lines(later, [], 1, TODAY, 0, title="x", show_undated=True), [])

    def test_overflow_is_counted(self):
        ts = [gtasks.Task(f"t{i}", TODAY) for i in range(gtasks.MAX_SHOWN + 4)]
        out = gtasks.lines(ts, [], 1, TODAY, 0, title="x", show_undated=False)
        self.assertEqual(out[-2], "…외 4개")
        self.assertEqual(len(out), 1 + gtasks.MAX_SHOWN + 1 + 1)

    def test_failure_only_still_tells_the_owner(self):
        out = gtasks.lines([], ["개인"], 1, TODAY, 0, title="x", show_undated=True)
        self.assertEqual(out, ["■ x", "할 일을 불러오지 못했어요 (로그 확인)", ""])
        many = gtasks.failure_line(["학교"], 2)
        self.assertIn("학교", many[0])

    def test_labels_show_when_several_accounts(self):
        t = gtasks.Task("보고서", TODAY, account="개인")
        self.assertEqual(gtasks.task_line(t, TODAY), "• 오늘 [개인] 보고서")

    def test_section_is_empty_when_off_and_never_raises(self):
        self.assertEqual(gtasks.section(make_cfg(tasks_accounts=[]), NOW, kind="morning"), [])
        with mock.patch.object(gtasks, "gather", side_effect=RuntimeError("boom")):
            self.assertEqual(gtasks.section(make_cfg(), NOW, kind="morning"), [])

    def test_section_kinds(self):
        ts = [gtasks.Task("오늘것", TODAY), gtasks.Task("내일것", date(2026, 10, 23)),
              gtasks.Task("다음주것", date(2026, 10, 28)), gtasks.Task("메모", None)]
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            morn = gtasks.section(make_cfg(), NOW, kind="morning", fetch_fn=lambda t: ts)
            eve = gtasks.section(make_cfg(), NOW, kind="evening", fetch_fn=lambda t: ts)
            week = gtasks.section(make_cfg(), SUNDAY, kind="weekly", fetch_fn=lambda t: ts)
        self.assertEqual(morn[0], "■ 할 일 (오늘까지)")
        self.assertIn("날짜 없는 할 일 1개", morn)
        self.assertNotIn("• 내일 내일것", morn)
        self.assertEqual(eve[0], "■ 내일까지 할 일")
        self.assertIn("• 내일 내일것", eve)
        self.assertFalse(any("날짜 없는" in l for l in eve))
        self.assertEqual(week[0], "■ 다음 주까지 할 일")


class MessageTests(unittest.TestCase):
    def tasks(self, token):
        return [gtasks.Task("보고서 제출", date(2026, 10, 21)), gtasks.Task("메모", None)]

    def test_morning_message_has_the_section_after_the_calendar(self):
        cfg = make_cfg(calendar_enabled=False)
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            text = morning.build_message(cfg, NOW, fetch_tasks=self.tasks)
        self.assertIn("■ 할 일 (오늘까지)", text)
        self.assertIn("• 지남 10/21 보고서 제출", text)

    def test_morning_unchanged_when_off(self):
        cfg = make_cfg(tasks_accounts=[], calendar_enabled=False)
        text = morning.build_message(cfg, NOW, fetch_tasks=mock.Mock(side_effect=AssertionError("must not be called")))
        self.assertNotIn("할 일", text)

    def test_morning_survives_a_tasks_failure(self):
        cfg = make_cfg(calendar_enabled=False)
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            text = morning.build_message(cfg, NOW, fetch_tasks=mock.Mock(side_effect=netutil.NetError("down")))
        self.assertIn("좋은 아침이에요", text)
        self.assertIn("할 일을 불러오지 못했어요", text)

    def test_evening_message_uses_the_weekday_and_the_sunday_titles(self):
        cfg = make_cfg(calendar_enabled=False, evening_hour=20)
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            weekday = evening.build_message(cfg, datetime(2026, 10, 22, 20, 0), fetch_tasks=self.tasks)
            sunday = evening.build_message(cfg, SUNDAY, fetch_tasks=self.tasks)
        self.assertIn("■ 내일까지 할 일", weekday)
        self.assertIn("■ 다음 주까지 할 일", sunday)

    def test_evening_unchanged_when_off(self):
        cfg = make_cfg(tasks_accounts=[], calendar_enabled=False, evening_hour=20)
        text = evening.build_message(cfg, NOW, fetch_tasks=mock.Mock(side_effect=AssertionError("no")))
        self.assertNotIn("할 일", text)

    def test_the_run_paths_pass_tasks_outside_the_lock(self):
        cfg = make_cfg(calendar_enabled=False)
        sent = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(google_auth, "access_token", return_value="T"), \
                mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}):
            cfg.state_path = str(Path(d) / "s.json")
            seen_lock = []

            def fetch(token):
                seen_lock.append((Path(d) / "s.json.lock").exists())
                return self.tasks(token)

            rc = morning.run(cfg, send=True, force=True, now=NOW, sender=lambda m, **k: sent.append(m),
                             fetch_tasks=fetch)
        self.assertEqual(rc, 0)
        self.assertIn("■ 할 일 (오늘까지)", sent[0])
        self.assertEqual(seen_lock, [False])


class CommandTests(unittest.TestCase):
    def tasks(self, token):
        return [gtasks.Task("보고서 제출", date(2026, 10, 21)), gtasks.Task("장보기", date(2026, 10, 24)),
                gtasks.Task("한참 뒤", date(2026, 12, 1)), gtasks.Task("보고서 메모", None), gtasks.Task("정리", None)]

    def render(self, cfg=None, **kw):
        with mock.patch.object(google_auth, "access_token", return_value="T"):
            return commands.render_tasks(cfg or make_cfg(), NOW, fetch=self.tasks, **kw)

    def test_default_shows_overdue_and_the_week_and_counts_the_undated(self):
        out = self.render()
        self.assertIn("• 지남 10/21 보고서 제출", out)
        self.assertIn("• 10/24(토) 장보기", out)
        self.assertNotIn("한참 뒤", out)
        self.assertIn("날짜 없는 할 일 2개", out)

    def test_keyword_filters_and_lists_matching_undated_titles(self):
        out = self.render(keyword="보고서")
        self.assertIn("보고서 제출", out)
        self.assertIn("• 보고서 메모", out)
        self.assertNotIn("장보기", out)
        self.assertNotIn("날짜 없는 할 일", out)

    def test_nothing_found(self):
        self.assertIn("없어요.", self.render(keyword="없는낱말"))

    def test_off_message_and_alias_and_registry(self):
        self.assertEqual(commands.render_tasks(make_cfg(tasks_accounts=[]), NOW), "할 일 기능이 꺼져 있어요.")
        for word in ("/할일", "todo", "/tasks"):
            self.assertEqual(commands.resolve(word, ["할일"]), "할일")
        self.assertIsNone(commands.resolve("/할일", ["오늘"]))
        self.assertIs(commands.REGISTRY["할일"], commands.render_tasks)

    def test_only_selects_one_account(self):
        cfg = make_cfg(tasks_accounts=["개인", "학교"])
        seen = []
        with mock.patch.object(google_auth, "access_token", side_effect=lambda c, p, **k: seen.append(p.name) or "T"):
            commands.render_tasks(cfg, NOW, fetch=self.tasks, only="학교")
        self.assertEqual(seen, ["t2.json"])

    def test_help_lists_the_command(self):
        self.assertIn("/할일", commands.render_help(make_cfg(commands=["할일"]), NOW))

    def test_failure_is_reported_not_raised(self):
        with mock.patch.object(google_auth, "access_token", side_effect=google_auth.GoogleAuthError("x")):
            out = commands.render_tasks(make_cfg(), NOW, fetch=self.tasks)
        self.assertIn("할 일을 불러오지 못했어요", out)


class ChatToolTests(unittest.TestCase):
    def reply(self, **fields):
        obj = {"tool": "할일", "start": "2026-10-22", "days": 7, "account": "", "keyword": ""}
        obj.update(fields)
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}]}

    def cfg(self, **kw):
        base = dict(llm_enabled=True, chat_enabled=True, commands=["할일", "일정"], tasks_accounts=["개인"],
                    google_accounts=TWO)
        base.update(kw)
        return Config(**base)

    def ask(self, data, cfg=None):
        return chat.answer_full(chat.ChatRequest("이번 주 할 일"), cfg or self.cfg(), NOW, api_key="k",
                                post=mock.Mock(return_value=data))

    def test_the_tool_is_in_the_closed_schema(self):
        self.assertIn("할일", chat.SCHEMA["properties"]["tool"]["enum"])

    def test_routes_to_render_tasks_with_validated_arguments(self):
        with mock.patch.object(commands, "render_tasks", return_value="BODY") as rt:
            out, refund = self.ask(self.reply(days=14, account="개인", keyword="보고서"))
        kw = rt.call_args.kwargs
        self.assertEqual((kw["days"], kw["only"], kw["keyword"]), (14, "개인", "보고서"))
        self.assertEqual(out.splitlines()[0], "▸ 찾아본 것: 지난 것과 오늘부터 14일 이내 할 일 · 개인 · '보고서'")
        self.assertIn("BODY", out)

    def test_switched_off_command_is_reported_as_off(self):
        out, _ = self.ask(self.reply(), cfg=self.cfg(commands=["일정"]))
        self.assertEqual(out, chat.off_text(self.cfg(commands=["일정"])))

    def test_empty_keyword_search_gets_the_range_hint(self):
        with mock.patch.object(commands, "render_tasks", return_value="■ 할 일\n없어요."):
            out, _ = self.ask(self.reply(keyword="보고서"))
        self.assertIn(chat.NO_RESULT_HINT, out)

    def test_task_titles_never_go_to_the_model(self):
        body = chat._build_request("이번 주 할 일", self.cfg(), TODAY)
        self.assertNotIn("보고서", json.dumps(body, ensure_ascii=False))


class ConfigTests(unittest.TestCase):
    def write(self, data):
        p = Path(tempfile.mkdtemp()) / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_default_off(self):
        self.assertEqual(Config().tasks_accounts, [])
        self.assertEqual(Config().task_accounts(), [])

    def test_accepts_known_account_names(self):
        cfg = load_config(self.write({"google_accounts": TWO, "tasks_accounts": ["개인"], "commands": ["할일"]}))
        self.assertEqual([a.name for a in cfg.task_accounts()], ["개인"])

    def test_rejects_unknown_duplicate_or_malformed(self):
        for bad in (["없는계정"], ["개인", "개인"], "개인", [1], [""], ["개인", "학교", "x"]):
            with self.assertRaises(ConfigError, msg=str(bad)):
                load_config(self.write({"google_accounts": TWO, "tasks_accounts": bad}))

    def test_single_unnamed_account_allows_one_entry(self):
        self.assertEqual(len(load_config(self.write({"tasks_accounts": ["기본"]})).task_accounts()), 1)
        with self.assertRaises(ConfigError):
            load_config(self.write({"tasks_accounts": ["a", "b"]}))

    def test_task_command_name_is_accepted(self):
        self.assertEqual(load_config(self.write({"commands": ["할일"]})).commands, ["할일"])


class GoogleAuthTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cp = Path(self.dir.name) / "client.json"
        self.cp.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "sec"}}), encoding="utf-8")
        self.tp = Path(self.dir.name) / "token.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_default_scope_is_unchanged_and_never_includes_tasks(self):
        self.assertNotIn("tasks", google_auth.SCOPE)
        url = google_auth.build_auth_url(google_auth.load_client(self.cp), "http://127.0.0.1:5", "ST", "CH")
        self.assertNotIn("tasks", url)

    def test_tasks_login_asks_for_the_extra_scope_only_when_requested(self):
        seen = []

        def post(url, form):
            return {"access_token": "A", "refresh_token": "R", "expires_in": 3600,
                    "scope": f"{google_auth.SCOPE} {google_auth.TASKS_SCOPE}"}

        def run(tasks):
            said = []
            with mock.patch.object(google_auth, "_pkce", return_value=("V", "CH")), \
                    mock.patch.object(google_auth.HTTPServer, "handle_request", autospec=True) as hr:
                def serve(server):
                    handler = server.RequestHandlerClass
                    handler.result = "CODE"
                hr.side_effect = serve
                google_auth.login(self.cp, self.tp, open_browser=lambda u: seen.append(u), post=post, say=said.append,
                                  tasks=tasks)
            return said

        said = run(False)
        self.assertTrue(any("(캘린더, Gmail)" in s for s in said))
        said = run(True)
        self.assertTrue(any("할 일)" in s for s in said))
        self.assertTrue(google_auth.token_has_scope(self.tp, google_auth.TASKS_SCOPE))

    def test_login_says_so_when_the_box_was_not_ticked(self):
        def post(url, form):
            return {"access_token": "A", "refresh_token": "R", "expires_in": 3600, "scope": google_auth.SCOPE}

        said = []
        with mock.patch.object(google_auth, "_pkce", return_value=("V", "CH")), \
                mock.patch.object(google_auth.HTTPServer, "handle_request", autospec=True) as hr:
            hr.side_effect = lambda server: setattr(server.RequestHandlerClass, "result", "CODE")
            google_auth.login(self.cp, self.tp, open_browser=lambda u: None, post=post, say=said.append, tasks=True)
        self.assertTrue(any("허용되지 않았어요" in s for s in said))
        self.assertFalse(google_auth.token_has_scope(self.tp, google_auth.TASKS_SCOPE))

    def test_exchange_code_returns_the_granted_scope(self):
        got = google_auth.exchange_code(google_auth.load_client(self.cp), "C", "r", "v", self.tp,
                                        post=lambda u, f: {"access_token": "A", "refresh_token": "R", "scope": "x y"})
        self.assertEqual(got, "x y")

    def test_token_has_scope_reveals_nothing_and_survives_bad_files(self):
        self.assertFalse(google_auth.token_has_scope(self.tp, google_auth.TASKS_SCOPE))        # missing
        self.tp.write_text("not json", encoding="utf-8")
        self.assertFalse(google_auth.token_has_scope(self.tp, google_auth.TASKS_SCOPE))
        self.tp.write_text(json.dumps({"scope": google_auth.TASKS_SCOPE}), encoding="utf-8")
        self.assertTrue(google_auth.token_has_scope(self.tp, google_auth.TASKS_SCOPE))

    def test_old_tokens_without_the_tasks_scope_are_refused_for_tasks(self):
        self.tp.write_text(json.dumps({"refresh_token": "R", "access_token": "A", "expires_at": 9e12,
                                       "scope": google_auth.SCOPE}), encoding="utf-8")
        with self.assertRaises(google_auth.GoogleAuthError):
            google_auth.access_token(self.cp, self.tp, need_scope=google_auth.TASKS_SCOPE)
        self.assertEqual(google_auth.access_token(self.cp, self.tp, need_scope=google_auth.CAL_SCOPE), "A")


if __name__ == "__main__":
    unittest.main()
