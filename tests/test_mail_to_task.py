import _bootstrap  # noqa: F401
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from aide import commands, google_auth as ga, gtasks, heartbeat, inbox, netutil, rules, taskadd, telegram
from aide.__main__ import main
from aide.config import Config, ConfigError, load_config
from aide.models import Finding
from aide.state import State, StateLocked, locked

DAY = datetime(2026, 10, 14, 12, 0)
OWNER = "111"
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": OWNER}
DID = "abcd1234"
LINK = "https://mail.google.com/mail/u/0/#all/0000000a"
SECRET_TOKEN = "ya29.SECRET-ACCESS-TOKEN"
TITLE = "학부모 상담 자료 제출"


def mail(n, title=None, url=None, account="", addr=None):
    return Finding(key=f"mail:{n:08x}", source="mail", title=title or f"A: 제목{n}",
                   url=f"https://mail.google.com/mail/u/0/#all/{n:08x}" if url is None else url,
                   account=account, rule_key=f"mail:{addr}" if addr else "", label="A")


def press(data, uid=1, sender=OWNER, chat=OWNER, message_id=77):
    return {"update_id": uid, "callback_query": {
        "id": f"c{uid}", "from": {"id": int(sender), "is_bot": False}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": int(chat)}}}}


class Api:
    TelegramError = telegram.TelegramError

    def __init__(self, updates, lock_path=None):
        self.updates = updates
        self.lock_path = lock_path
        self.answers, self.sent, self.edits, self.cleared, self.set = [], [], [], [], []

    def get_updates(self, token, offset=0, timeout=0, allowed=("callback_query",)):
        return self.updates

    def answer_callback(self, token, cid, text):
        self.answers.append(text)

    def send(self, text, *, token, chat_id, buttons=None):
        if buttons:
            telegram._keyboard(buttons)
        self.sent.append((text, buttons))

    def edit_text(self, token, chat_id, message_id, text, buttons=None):
        if buttons:
            telegram._keyboard(buttons)
        self.edits.append((message_id, text, buttons))

    def clear_buttons(self, token, chat_id, message_id):
        self.cleared.append(message_id)

    def set_buttons(self, token, chat_id, message_id, buttons):
        telegram._keyboard(buttons)
        self.set.append((message_id, buttons))


class Base(unittest.TestCase):
    MAILS = [
        {"k": "mail:00000001", "t": TITLE, "u": LINK, "a": "학교"},
        {"k": "mail:00000002", "t": "급식 안내", "u": "", "a": ""},
        {"k": "mail:00000003", "t": "세번째", "u": LINK, "a": "개인"},
    ]

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.state_path = self.root / "state.json"
        self.tokens = {}
        for name in ("개인", "학교"):
            self.write_token(name, ga.TASKS_WRITE_SCOPE)
        self.cfg = self.make_cfg()
        self.seed()

    def tearDown(self):
        self.dir.cleanup()

    def write_token(self, name, *scopes):
        p = self.root / f"tok_{name}.json"
        p.write_text(json.dumps({"refresh_token": "R", "access_token": f"{SECRET_TOKEN}-{name}", "expires_at": 9e12,
                                 "scope": " ".join((ga.CAL_SCOPE, ga.GMAIL_SCOPE, *scopes))}), encoding="utf-8")
        self.tokens[name] = p

    def make_cfg(self, **kw):
        base = dict(state_path=str(self.state_path), tasks_accounts=["개인"], tasks_write=True,
                    google_accounts=[{"name": n, "token_path": str(self.root / f"tok_{n}.json")} for n in ("개인", "학교")])
        base.update(kw)
        return Config(**base)

    def seed(self, mails=None, did=DID):
        st = State(self.state_path)
        st.add_digest(did, ["k1"], DAY, None, self.MAILS if mails is None else mails)
        st.save()

    def poll(self, *datas, cfg=None, post=None, api=None, now=DAY):
        api = api or Api([press(d, uid=i + 1) for i, d in enumerate(datas)])
        post = post or mock.Mock(return_value={"id": "newtask"})
        with mock.patch.object(netutil, "post_json", post):
            inbox.poll_once(cfg or self.cfg, token="t", chat_id=OWNER, api=api, now=now)
        api.post = post
        return api


class RegexAndOffTests(Base):
    def test_the_button_pattern(self):
        for ok in ("t:abcd1234:open", "t:abcd1234:close", "t:abcd1234:p0", "t:abcd1234:p7", "t:abcd1234:p3:d0",
                   "t:abcd1234:p3:d1", "t:abcd1234:p3:dn"):
            self.assertTrue(taskadd.is_task_data(ok), ok)
            self.assertLessEqual(len(ok.encode()), 64)
        for bad in ("t:abcd123:open", "t:ABCD1234:open", "t:abcd1234:p8", "t:abcd1234:p10", "t:abcd1234:p0:d2",
                    "t:abcd1234:p0:", "t:abcd1234:open\n", "t:abcd1234:d0", "x:abcd1234:open", "", None, 5,
                    "t:abcd1234:p0:d0:d0", "t:abcd1234:open extra"):
            self.assertFalse(taskadd.is_task_data(bad), repr(bad))

    def test_the_default_is_off_and_validated(self):
        self.assertFalse(Config().tasks_write)
        self.assertFalse(load_config(Path(__file__).resolve().parents[1] / "config.example.json").tasks_write)

    def _load(self, **data):
        p = self.root / "c.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return load_config(p)

    def test_turning_it_on_needs_a_task_account_and_a_bool(self):
        with self.assertRaises(ConfigError):
            self._load(tasks_write=True)                                   # no tasks_accounts
        with self.assertRaises(ConfigError):
            self._load(tasks_write="yes", tasks_accounts=["x"], google_accounts=[{"name": "x", "token_path": "t"}])
        ok = self._load(tasks_write=True, tasks_accounts=["x"], google_accounts=[{"name": "x", "token_path": "t"}])
        self.assertTrue(ok.tasks_write)
        self.assertFalse(self._load(tasks_accounts=["x"], google_accounts=[{"name": "x", "token_path": "t"}]).tasks_write)

    def test_off_means_no_button_no_mails_key_and_buttons_expire(self):
        cfg = Config(state_path=str(self.state_path))
        sent = []
        with mock.patch.object(heartbeat, "collect", return_value=[mail(1)]), mock.patch.dict(os.environ, ENV):
            heartbeat.run(cfg, send=True, now=DAY, sender=lambda text, **k: sent.append(k.get("buttons")))
        self.assertEqual(len(sent[0]), 1)                                  # only the 확인/무시/나중에 row
        recs = [r for d, r in State(self.state_path).digests.items() if d != DID]   # DID is the seeded one
        self.assertEqual(len(recs), 1)
        self.assertNotIn("mails", recs[0])
        api = self.poll(f"t:{DID}:open", f"t:{DID}:p0:d1", cfg=Config(state_path=str(self.state_path)))
        self.assertEqual(api.answers, [taskadd.EXPIRED_OFF] * 2)
        self.assertEqual((api.sent, api.edits), ([], []))
        api.post.assert_not_called()
        self.assertEqual(State(self.state_path).task_adds, {})

    def test_off_removes_the_keyboard_task_button_too(self):
        api = self.poll(f"d:{DID}:ok", cfg=Config(state_path=str(self.state_path)))
        self.assertEqual(api.cleared, [77])
        self.assertEqual(api.set, [])


class WhoMayPressTests(Base):
    def test_a_stranger_or_another_chat_changes_nothing_and_gets_no_reply(self):
        for sender, chat in (("999", "999"), (OWNER, "999"), ("999", OWNER)):
            api = Api([press(f"t:{DID}:p0:d1", sender=sender, chat=chat)])
            post = mock.Mock()
            self.poll(cfg=self.cfg, api=api, post=post)
            self.assertEqual((api.answers, api.sent, api.edits), ([], [], []), (sender, chat))
            post.assert_not_called()
        self.assertEqual(State(self.state_path).task_adds, {})

    def test_a_group_message_is_ignored(self):
        up = press(f"t:{DID}:p0:d1", sender=OWNER, chat="-100222")
        api = self.poll(cfg=self.cfg, api=Api([up]))
        self.assertEqual((api.answers, api.edits), ([], []))
        api.post.assert_not_called()

    def test_a_bot_is_ignored(self):
        up = press(f"t:{DID}:p0:d1")
        up["callback_query"]["from"]["is_bot"] = True
        api = self.poll(cfg=self.cfg, api=Api([up]))
        api.post.assert_not_called()
        self.assertEqual(api.answers, [])


class DigestStorageTests(Base):
    def setUp(self):
        super().setUp()
        self.state_path.unlink()          # start from an empty state: the digest under test is the only one

    def hb(self, findings, cfg=None):
        sent = []
        cfg = cfg or self.make_cfg()
        with mock.patch.object(heartbeat, "collect", return_value=findings), mock.patch.dict(os.environ, ENV):
            heartbeat.run(cfg, send=True, now=DAY, sender=lambda text, **k: sent.append((text, k.get("buttons"))))
        return sent

    def test_mails_are_stored_with_the_digest_and_the_button_is_added(self):
        long_title = "가" * 300
        sent = self.hb([mail(1, title=long_title, account="학교"), mail(2, url="https://evil.example/x"),
                        Finding(key="news:1", source="news", title="뉴스")])
        text, buttons = sent[0]
        digest_id = next(iter(State(self.state_path).digests))
        self.assertEqual(buttons[1], [("📝 할 일로", f"t:{digest_id}:open")])
        mails = State(self.state_path).digests[digest_id]["mails"]
        self.assertEqual(len(mails), 2)                                    # the news item is not a mail
        self.assertEqual(mails[0]["k"], "mail:00000001")
        self.assertEqual(len(mails[0]["t"]), 100)                          # clean()ed and capped
        self.assertEqual(mails[0]["a"], "학교")
        self.assertTrue(mails[0]["u"].startswith("https://mail.google.com/"))
        self.assertEqual(mails[1]["u"], "")                                # not a Gmail link: dropped

    def test_at_most_eight_mails_and_no_button_without_mail(self):
        self.hb([mail(n) for n in range(12)])
        rec = next(iter(State(self.state_path).digests.values()))
        self.assertEqual(len(rec["mails"]), 8)
        self.state_path.unlink()
        sent = self.hb([Finding(key="news:1", source="news", title="뉴스")])
        self.assertEqual(len(sent[0][1]), 1)
        self.assertNotIn("mails", next(iter(State(self.state_path).digests.values())))

    def test_the_task_button_sits_next_to_the_clean_button(self):
        cfg = self.make_cfg(rules_enabled=True, evening_hour=20)
        sent = self.hb([mail(1, addr="a@x.kr")], cfg=cfg)
        did = next(iter(State(self.state_path).digests))
        self.assertEqual(sent[0][1][1], [("🧹 정리", f"m:{did}:open"), ("📝 할 일로", f"t:{did}:open")])
        self.assertEqual([d for _, d in sent[0][1][0]], [f"d:{did}:ok", f"d:{did}:skip", f"d:{did}:later"])

    def test_old_state_files_load_and_new_ones_roundtrip(self):
        self.state_path.write_text(json.dumps({"digests": {DID: {"keys": ["a"], "at": DAY.isoformat(), "status": "ok"}}}),
                                   encoding="utf-8")
        st = State(self.state_path)
        self.assertNotIn("mails", st.digests[DID])
        self.assertEqual(st.task_adds, {})
        st.add_digest("11112222", ["k"], DAY, None, [{"k": f"mail:{i}", "t": "T", "u": LINK, "a": "x"} for i in range(12)])
        st.save()
        got = State(self.state_path).digests["11112222"]["mails"]
        self.assertEqual(len(got), 8)
        self.assertEqual(got[0], {"k": "mail:0", "t": "T", "u": LINK, "a": "x"})

    def test_mails_are_pruned_with_their_digest_after_45_days(self):
        st = State(self.state_path)
        st.add_digest("11112222", ["k"], DAY - timedelta(days=46), None, self.MAILS)
        st.add_digest("33334444", ["k"], DAY - timedelta(days=10), None, self.MAILS)
        st.prune(DAY)
        self.assertEqual(sorted(st.digests), ["33334444"])

    def test_daily_counter_prunes_old_days(self):
        st = State(self.state_path)
        st.task_adds = {(DAY.date() - timedelta(days=30)).isoformat(): 5, DAY.date().isoformat(): 2, "junk": 1}
        self.assertEqual(st.task_adds_today(DAY), 2)
        self.assertEqual(list(st.task_adds), [DAY.date().isoformat()])


class FlowTests(Base):
    def test_open_lists_the_mails_with_a_button_each_and_a_close(self):
        api = self.poll(f"t:{DID}:open")
        text, buttons = api.sent[0]
        self.assertTrue(text.startswith("어느 메일을 할 일로 추가할까요?"))
        self.assertIn(f"1. {TITLE}", text)
        data = [d for row in buttons for _, d in row]
        self.assertEqual(data, [f"t:{DID}:p0", f"t:{DID}:p1", f"t:{DID}:p2", f"t:{DID}:close"])
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))
        api.post.assert_not_called()

    def test_choosing_a_mail_edits_the_message_into_the_due_choice(self):
        api = self.poll(f"t:{DID}:p0")
        mid, text, buttons = api.edits[0]
        self.assertEqual((mid, text), (77, f"할 일 추가: {TITLE}"))
        self.assertEqual([[t for t, _ in row] for row in buttons], [["오늘", "내일", "날짜 없음"], ["취소"]])
        self.assertEqual([[d for _, d in row] for row in buttons],
                         [[f"t:{DID}:p0:d0", f"t:{DID}:p0:d1", f"t:{DID}:p0:dn"], [f"t:{DID}:close"]])
        api.post.assert_not_called()                                       # nothing is written yet
        self.assertEqual(State(self.state_path).task_adds, {})

    def test_cancel_closes_without_writing(self):
        api = self.poll(f"t:{DID}:close")
        self.assertEqual(api.edits[0][1], "닫았어요")
        api.post.assert_not_called()

    def test_the_final_press_adds_one_task_and_rewrites_the_message(self):
        api = self.poll(f"t:{DID}:p0:d1")
        self.assertEqual(api.post.call_count, 1)
        self.assertEqual(api.answers, [taskadd.BUSY])
        self.assertEqual(api.edits[-1][:2], (77, f"✅ 할 일에 추가했어요: {TITLE} (내일)"))
        self.assertFalse(api.edits[-1][2])                                  # no buttons left
        st = State(self.state_path)
        self.assertTrue(st.is_seen(f"taskadd:{DID}:0"))
        self.assertEqual(st.task_adds_today(DAY), 1)

    def test_due_labels_in_the_final_message(self):
        for n, (d, label) in enumerate((("d0", "오늘"), ("dn", "날짜 없음"))):
            api = self.poll(f"t:{DID}:p{n}:{d}")
            self.assertTrue(api.edits[-1][1].endswith(f"({label})"), api.edits[-1][1])

    def test_pressing_again_is_idempotent(self):
        self.poll(f"t:{DID}:p0:d1")
        api = self.poll(f"t:{DID}:p0:d1", f"t:{DID}:p0:d0", f"t:{DID}:p0")
        api.post.assert_not_called()
        self.assertEqual(api.answers, [taskadd.ALREADY] * 3)
        self.assertEqual(State(self.state_path).task_adds_today(DAY), 1)
        self.assertEqual(api.edits[0][1], f"이미 추가했어요: {TITLE}")

    def test_a_double_press_in_one_batch_adds_once(self):
        api = self.poll(f"t:{DID}:p0:d1", f"t:{DID}:p0:d1")
        self.assertEqual(api.post.call_count, 1)
        self.assertEqual(State(self.state_path).task_adds_today(DAY), 1)

    def test_added_mails_are_marked_in_the_list(self):
        self.poll(f"t:{DID}:p1:dn")
        api = self.poll(f"t:{DID}:open")
        self.assertIn("2. ✅ 급식 안내", api.sent[0][0])
        self.assertIn("1. " + TITLE, api.sent[0][0])

    def test_unknown_digest_and_out_of_range_numbers_are_expired(self):
        api = self.poll("t:00000000:open", "t:00000000:p0:d1", f"t:{DID}:p3", f"t:{DID}:p7:d1", f"t:{DID}:p5")
        self.assertEqual(api.answers, [taskadd.EXPIRED] * 5)
        self.assertEqual((api.sent, api.edits), ([], []))
        api.post.assert_not_called()

    def test_a_digest_without_mails_is_expired(self):
        st = State(self.state_path)
        st.add_digest("11112222", ["k"], DAY)
        st.save()
        api = self.poll("t:11112222:open", "t:11112222:p0")
        self.assertEqual(api.answers, [taskadd.EXPIRED] * 2)

    def test_an_old_digest_without_the_mails_key_is_expired_not_a_crash(self):
        self.state_path.write_text(json.dumps({"digests": {DID: {"keys": ["a"], "at": DAY.isoformat(),
                                                                 "status": "pending"}}}), encoding="utf-8")
        api = self.poll(f"t:{DID}:open")
        self.assertEqual(api.answers, [taskadd.EXPIRED])

    def test_the_rest_of_the_batch_still_runs(self):
        api = self.poll(f"d:{DID}:ok", f"t:{DID}:p0:d1", f"t:{DID}:p1:d0")
        self.assertEqual(api.post.call_count, 2)
        self.assertEqual(api.answers[0], "확인했어요")


class OutsideTheLockTests(Base):
    def test_the_google_call_and_the_final_edit_happen_with_the_state_lock_released(self):
        lock_path = self.state_path
        seen = {"post": False, "edit": False}

        def free():
            try:
                with locked(lock_path, timeout=0.3):
                    return True
            except StateLocked:
                return False

        def post(url, payload, headers=None, timeout=15):
            seen["post"] = free()
            return {"id": "x"}

        class LockApi(Api):
            def edit_text(self_, token, chat_id, message_id, text, buttons=None):
                if text.startswith("✅"):
                    seen["edit"] = free()
                super().edit_text(token, chat_id, message_id, text, buttons)

        self.poll(f"t:{DID}:p0:d1", post=post, api=LockApi([press(f"t:{DID}:p0:d1")]))
        self.assertEqual(seen, {"post": True, "edit": True})

    def test_process_updates_itself_never_calls_google(self):
        st = State(self.state_path)
        with mock.patch.object(netutil, "post_json", side_effect=AssertionError("network under the lock")):
            handled, to_run = inbox.process_updates([press(f"t:{DID}:p0:d1")], st, token="t", allowed_chat_id=OWNER,
                                                    api=Api([]), now=DAY, tasks_write=True)
        self.assertEqual(handled, 1)
        self.assertEqual([type(x) for x in to_run], [taskadd.TaskAddRequest])
        self.assertEqual(to_run[0].title, TITLE)
        self.assertEqual(st.task_adds, {})                                  # nothing recorded before the call succeeded

    def test_it_is_recorded_only_after_success(self):
        st = State(self.state_path)
        _, to_run = inbox.process_updates([press(f"t:{DID}:p0:d1")], st, token="t", allowed_chat_id=OWNER,
                                          api=Api([]), now=DAY, tasks_write=True)
        self.assertFalse(st.is_seen(f"taskadd:{DID}:0"))
        self.assertEqual(st.task_adds_today(DAY), 0)


class FailureTests(Base):
    def test_a_network_error_says_so_records_nothing_and_can_be_retried(self):
        api = self.poll(f"t:{DID}:p0:d1", post=mock.Mock(side_effect=netutil.NetError("HTTP 401", status=401)))
        self.assertEqual(api.edits[-1][1], f"{taskadd.FAIL_TEXT}\n{TITLE}")
        self.assertIn("(로그 확인)", api.edits[-1][1])
        self.assertEqual([d for row in api.edits[-1][2] for _, d in row][:3],
                         [f"t:{DID}:p0:d0", f"t:{DID}:p0:d1", f"t:{DID}:p0:dn"])   # the same choices stay
        st = State(self.state_path)
        self.assertFalse(st.is_seen(f"taskadd:{DID}:0"))
        self.assertEqual(st.task_adds_today(DAY), 0)
        again = self.poll(f"t:{DID}:p0:d1")
        self.assertEqual(again.post.call_count, 1)
        self.assertEqual(again.edits[-1][1], f"✅ 할 일에 추가했어요: {TITLE} (내일)")

    def test_a_login_problem_is_a_failure_not_a_crash(self):
        with mock.patch.object(ga, "access_token", side_effect=ga.GoogleAuthError("로그인이 만료되었습니다.")):
            api = self.poll(f"t:{DID}:p0:d1")
        api.post.assert_not_called()
        self.assertTrue(api.edits[-1][1].startswith(taskadd.FAIL_TEXT))
        self.assertFalse(State(self.state_path).is_seen(f"taskadd:{DID}:0"))

    def test_an_unexpected_error_in_the_call_is_contained(self):
        api = self.poll(f"t:{DID}:p0:d1", f"t:{DID}:p1:d1", post=mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(State(self.state_path).task_adds, {})
        self.assertEqual(sum(1 for e in api.edits if e[1].startswith(taskadd.FAIL_TEXT)), 2)


class DailyLimitTests(Base):
    def test_at_the_limit_nothing_is_added(self):
        st = State(self.state_path)
        st.task_adds = {DAY.date().isoformat(): 20}
        st.save()
        api = self.poll(f"t:{DID}:p0:d1")
        self.assertEqual(api.answers, ["오늘은 20건까지만 추가할 수 있어요"])
        api.post.assert_not_called()
        self.assertFalse(State(self.state_path).is_seen(f"taskadd:{DID}:0"))

    def test_the_batch_counts_toward_the_limit(self):
        st = State(self.state_path)
        st.task_adds = {DAY.date().isoformat(): 18}
        st.save()
        api = self.poll(f"t:{DID}:p0:d1", f"t:{DID}:p1:d1", f"t:{DID}:p2:d1")
        self.assertEqual(api.post.call_count, 2)
        self.assertEqual(api.answers[2], taskadd.LIMIT_TEXT)
        self.assertEqual(State(self.state_path).task_adds_today(DAY), 20)

    def test_a_new_day_starts_at_zero(self):
        st = State(self.state_path)
        st.task_adds = {DAY.date().isoformat(): 20}
        st.save()
        api = self.poll(f"t:{DID}:p0:d1", now=DAY + timedelta(days=1))
        self.assertEqual(api.post.call_count, 1)


class AccountAndPermissionTests(Base):
    def token_of(self, post):
        return post.call_args.kwargs["headers"]["Authorization"]

    def test_the_mails_own_account_is_used_when_it_is_a_task_account(self):
        cfg = self.make_cfg(tasks_accounts=["개인", "학교"])
        api = self.poll(f"t:{DID}:p0:d1", cfg=cfg)                          # mail 0 belongs to 학교
        self.assertEqual(self.token_of(api.post), f"Bearer {SECRET_TOKEN}-학교")

    def test_otherwise_the_first_task_account(self):
        api = self.poll(f"t:{DID}:p0:d1")                                   # 학교 is not a task account
        self.assertEqual(self.token_of(api.post), f"Bearer {SECRET_TOKEN}-개인")
        api = self.poll(f"t:{DID}:p1:d1")                                   # mail 1 has no account
        self.assertEqual(self.token_of(api.post), f"Bearer {SECRET_TOKEN}-개인")

    def test_a_token_without_write_permission_gets_the_instruction_and_nothing_is_sent(self):
        self.write_token("개인", ga.TASKS_SCOPE)                              # read-only tasks
        api = self.poll(f"t:{DID}:p1:d1")
        api.post.assert_not_called()
        self.assertEqual(api.edits[-1][1], "할 일 쓰기 권한이 없어요 — PC에서 python -m aide google-login "
                                           "--account 개인 --tasks-write 후 토큰을 서버로 복사")
        self.assertFalse(State(self.state_path).is_seen(f"taskadd:{DID}:1"))
        self.assertEqual(State(self.state_path).task_adds, {})

    def test_a_missing_token_file_gets_the_same_instruction(self):
        self.tokens["개인"].unlink()
        api = self.poll(f"t:{DID}:p1:d1")
        api.post.assert_not_called()
        self.assertIn("--tasks-write", api.edits[-1][1])

    def test_the_request_goes_to_the_default_list_over_post_json(self):
        api = self.poll(f"t:{DID}:p0:d1")
        self.assertEqual(api.post.call_args.args[0], "https://tasks.googleapis.com/tasks/v1/lists/@default/tasks")


class PayloadTests(unittest.TestCase):
    def req(self, due, url=LINK, title=TITLE):
        return taskadd.TaskAddRequest(DID, 0, due, title, url, "")

    def test_due_dates_are_today_tomorrow_or_absent(self):
        today = date(2026, 10, 14)
        self.assertEqual(taskadd.payload(self.req("d0"), today)["due"], "2026-10-14T00:00:00.000Z")
        self.assertEqual(taskadd.payload(self.req("d1"), today)["due"], "2026-10-15T00:00:00.000Z")
        self.assertNotIn("due", taskadd.payload(self.req("dn"), today))
        self.assertEqual(taskadd.payload(self.req("d1"), date(2026, 10, 31))["due"], "2026-11-01T00:00:00.000Z")
        self.assertEqual(taskadd.payload(self.req("d1"), date(2026, 12, 31))["due"], "2027-01-01T00:00:00.000Z")
        self.assertRegex(taskadd.payload(self.req("d0"), today)["due"], r"^\d{4}-\d{2}-\d{2}T00:00:00\.000Z$")

    def test_today_is_the_local_kst_date_of_the_press(self):
        sent = {}

        def post(url, payload, headers=None, timeout=15):
            sent.update(payload)
            return {}

        with tempfile.TemporaryDirectory() as d:
            tok = Path(d) / "t.json"
            tok.write_text(json.dumps({"access_token": "A", "expires_at": 9e12, "scope": ga.TASKS_WRITE_SCOPE}))
            cfg = Config(google_accounts=[{"name": "개인", "token_path": str(tok)}], tasks_accounts=["개인"], tasks_write=True)
            late = datetime(2026, 10, 14, 23, 50)          # 23:50 KST is still the 14th, not the 15th (UTC would say 14th 14:50 anyway)
            res = taskadd.run(self.req("d0"), cfg, late, post=post)
        self.assertEqual(res.status, "ok")
        self.assertEqual(sent["due"], "2026-10-14T00:00:00.000Z")

    def test_notes_carry_only_a_gmail_link(self):
        today = date(2026, 10, 14)
        self.assertEqual(taskadd.payload(self.req("dn"), today)["notes"], LINK)
        for bad in ("", "https://evil.example/x", "http://mail.google.com/x", "https://mail.google.com.evil.example/x",
                    "javascript:alert(1)"):
            self.assertNotIn("notes", taskadd.payload(self.req("dn", url=bad), today), bad)

    def test_a_foreign_link_in_the_stored_digest_never_reaches_the_request(self):
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "s.json"
            st = State(sp)
            st.add_digest(DID, ["k"], DAY, None, [{"k": "mail:1", "t": "제목", "u": "https://evil.example/phish", "a": ""}])
            _, to_run = inbox.process_updates([press(f"t:{DID}:p0:d1")], st, token="t", allowed_chat_id=OWNER,
                                              api=Api([]), now=DAY, tasks_write=True)
        self.assertEqual(to_run[0].url, "")

    def test_title_is_cleaned_and_capped(self):
        body = taskadd.payload(self.req("dn", title="제목\x00\n  둘" + "가" * 300), date(2026, 10, 14))
        self.assertNotIn("\x00", body["title"])
        self.assertNotIn("\n", body["title"])
        self.assertLessEqual(len(body["title"]), 100)


class LogTests(Base):
    def test_logs_never_hold_the_title_the_link_or_a_token(self):
        with self.assertLogs("aide", level="DEBUG") as cm:
            self.poll(f"t:{DID}:open", f"t:{DID}:p0", f"t:{DID}:p0:d1", f"t:{DID}:p0:d1")
            self.poll(f"t:{DID}:p1:d1", post=mock.Mock(side_effect=netutil.NetError("HTTP 401", status=401)))
            self.write_token("개인", ga.TASKS_SCOPE)
            self.poll(f"t:{DID}:p2:d1")
        blob = "\n".join(cm.output)
        for secret in (TITLE, "급식 안내", "세번째", LINK, "mail.google.com", SECRET_TOKEN, "Bearer"):
            self.assertNotIn(secret, blob)
        self.assertIn("할 일 추가", blob)                                    # but it does say what it did
        self.assertIn("개인", blob)

    def test_log_when_post_raises_does_not_leak_the_message(self):
        with self.assertLogs("aide.taskadd", level="DEBUG") as cm:
            self.poll(f"t:{DID}:p0:d1", post=mock.Mock(side_effect=RuntimeError(f"{TITLE} {SECRET_TOKEN}")))
        self.assertNotIn(TITLE, "\n".join(cm.output))
        self.assertNotIn(SECRET_TOKEN, "\n".join(cm.output))


class KeyboardCoexistenceTests(Base):
    def test_confirm_keeps_only_the_task_button_when_there_is_no_clean_button(self):
        api = self.poll(f"d:{DID}:ok")
        self.assertEqual(api.set, [(77, [[("📝 할 일로", f"t:{DID}:open")]])])
        self.assertEqual(api.cleared, [])

    def test_confirm_keeps_clean_and_task_together(self):
        st = State(self.state_path)
        st.add_digest("11112222", ["k"], DAY, [{"k": "mail:a@x.kr", "l": "A", "u": LINK}], self.MAILS)
        st.save()
        cfg = self.make_cfg(rules_enabled=True, evening_hour=20)
        api = self.poll("d:11112222:skip", cfg=cfg)
        self.assertEqual(api.set, [(77, [[("🧹 정리", "m:11112222:open"), ("📝 할 일로", "t:11112222:open")]])])

    def test_the_old_clean_only_behaviour_is_unchanged(self):
        st = State(self.state_path)
        st.add_digest("11112222", ["k"], DAY, [{"k": "mail:a@x.kr", "l": "A", "u": LINK}])
        st.save()
        api = self.poll("d:11112222:ok", cfg=self.make_cfg(rules_enabled=True, evening_hour=20))
        self.assertEqual(api.set, [(77, [[("🧹 정리", "m:11112222:open")]])])

    def test_a_repeat_press_still_tidies_the_keyboard(self):
        self.poll(f"d:{DID}:ok")
        api = self.poll(f"d:{DID}:ok")
        self.assertEqual(api.set, [(77, [[("📝 할 일로", f"t:{DID}:open")]])])

    def test_button_rows_helper(self):
        self.assertEqual(len(inbox.buttons_for(DID)), 1)
        self.assertEqual(inbox.buttons_for(DID, with_task=True)[1], [("📝 할 일로", f"t:{DID}:open")])
        self.assertEqual(inbox.buttons_for(DID, with_clean=True)[1], [("🧹 정리", f"m:{DID}:open")])
        for _, data in sum(inbox.buttons_for(DID, True, True), []):
            self.assertLessEqual(len(data.encode()), 64)

    def test_clean_buttons_still_work_next_to_task_buttons(self):
        st = State(self.state_path)
        st.add_digest("11112222", ["k"], DAY, [{"k": "mail:a@x.kr", "l": "A", "u": LINK}], self.MAILS)
        st.save()
        api = self.poll("m:11112222:t0:hide", cfg=self.make_cfg(rules_enabled=True, evening_hour=20))
        self.assertEqual({k: v["action"] for k, v in rules.Rules(rules.path_for(self.state_path)).rules.items()},
                         {"mail:a@x.kr": "hide"})


class ReadCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def token(self, *scopes):
        p = self.root / "t.json"
        p.write_text(json.dumps({"refresh_token": "R", "access_token": "A", "expires_at": 9e12, "scope": " ".join(scopes)}))
        return p

    def cfg(self, p):
        return Config(google_accounts=[{"name": "개인", "token_path": str(p)}], tasks_accounts=["개인"])

    def test_a_write_token_reads_tasks_and_so_does_a_readonly_one(self):
        for scope in (ga.TASKS_WRITE_SCOPE, ga.TASKS_SCOPE):
            tasks, failed, _ = gtasks.gather(self.cfg(self.token(ga.CAL_SCOPE, scope)),
                                             lambda token: [gtasks.Task("할 일", None)])
            self.assertEqual((len(tasks), failed), (1, []), scope)

    def test_a_token_with_neither_still_fails_to_read(self):
        tasks, failed, _ = gtasks.gather(self.cfg(self.token(ga.CAL_SCOPE, ga.GMAIL_SCOPE)),
                                         lambda token: [gtasks.Task("할 일", None)])
        self.assertEqual((tasks, failed), ([], ["개인"]))

    def test_access_token_accepts_alternatives_and_a_plain_string(self):
        p = self.token(ga.TASKS_WRITE_SCOPE)
        client = self.root / "c.json"
        self.assertEqual(ga.access_token(client, p, need_scope=ga.TASKS_READ_SCOPES, now=lambda: 1), "A")
        self.assertEqual(ga.access_token(client, p, need_scope=ga.TASKS_WRITE_SCOPE, now=lambda: 1), "A")
        with self.assertRaises(ga.GoogleAuthError):
            ga.access_token(client, p, need_scope=ga.TASKS_SCOPE, now=lambda: 1)   # a plain string is still exact
        with self.assertRaises(ga.GoogleAuthError):
            ga.access_token(client, self.token(ga.TASKS_SCOPE), need_scope=ga.TASKS_WRITE_SCOPE, now=lambda: 1)

    def test_token_has_scope_takes_a_tuple(self):
        p = self.token(ga.TASKS_WRITE_SCOPE)
        self.assertTrue(ga.token_has_scope(p, ga.TASKS_READ_SCOPES))
        self.assertTrue(ga.token_has_scope(p, ga.TASKS_WRITE_SCOPE))
        self.assertFalse(ga.token_has_scope(p, ga.TASKS_SCOPE))
        self.assertFalse(ga.token_has_scope(p, (ga.CAL_SCOPE, ga.GMAIL_SCOPE)))

    def test_the_connections_line_tells_write_from_read(self):
        p = self.token(ga.CAL_SCOPE, ga.TASKS_WRITE_SCOPE)
        self.assertEqual(commands._google_line(self.cfg(p)), "구글: 개인(일정·할 일·할 일 쓰기)")
        p = self.token(ga.CAL_SCOPE, ga.TASKS_SCOPE)
        self.assertEqual(commands._google_line(self.cfg(p)), "구글: 개인(일정·할 일)")


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.cp = self.root / "client.json"
        self.cp.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "sec"}}), encoding="utf-8")
        self.tp = self.root / "tok.json"

    def tearDown(self):
        self.dir.cleanup()

    def run_login(self, granted, **kw):
        import urllib.request
        from urllib.parse import parse_qs, urlparse
        seen, out = {}, []

        def browser(url):
            q = parse_qs(urlparse(url).query)
            seen["scope"] = q["scope"][0].split()
            urllib.request.urlopen(f"{q['redirect_uri'][0]}/?code=C&state={q['state'][0]}", timeout=5).read()

        post = lambda u, f: {"access_token": "A", "refresh_token": "R", "expires_in": 3600, "scope": granted}  # noqa: E731
        ga.login(self.cp, self.tp, open_browser=browser, post=post, say=out.append, **kw)
        return seen["scope"], "\n".join(out)

    def test_tasks_write_asks_for_the_read_write_tasks_scope_on_top_of_the_usual(self):
        scope, say = self.run_login(f"{ga.SCOPE} {ga.TASKS_WRITE_SCOPE}", tasks_write=True)
        self.assertEqual(scope, [ga.CAL_SCOPE, ga.GMAIL_SCOPE, ga.TASKS_WRITE_SCOPE])
        self.assertIn("로그인 완료", say)
        self.assertIn("추가", say)

    def test_it_says_so_when_the_box_was_unticked(self):
        _, say = self.run_login(ga.SCOPE, tasks_write=True)
        self.assertIn("'할 일 쓰기' 권한이 허용되지 않았어요", say)

    def test_the_existing_flags_are_unchanged(self):
        scope, say = self.run_login(ga.SCOPE)
        self.assertEqual(scope, [ga.CAL_SCOPE, ga.GMAIL_SCOPE])
        self.assertIn("읽기 전용 권한(캘린더, Gmail)만", say)
        scope, say = self.run_login(f"{ga.SCOPE} {ga.TASKS_SCOPE}", tasks=True)
        self.assertEqual(scope, [ga.CAL_SCOPE, ga.GMAIL_SCOPE, ga.TASKS_SCOPE])
        self.assertNotIn(ga.TASKS_WRITE_SCOPE, scope)
        self.assertIn("읽기 전용 권한(캘린더, Gmail, 할 일)만", say)

    def test_the_cli_flag_reaches_login(self):
        cfgp = self.root / "config.json"
        cfgp.write_text("{}", encoding="utf-8")
        with mock.patch.object(ga, "login") as login:
            code = main(["google-login", "--tasks-write", "--config", str(cfgp)])
            self.assertEqual(code, 0)
            self.assertEqual((login.call_args.kwargs["tasks"], login.call_args.kwargs["tasks_write"]), (False, True))
            main(["google-login", "--tasks", "--config", str(cfgp)])
            self.assertEqual((login.call_args.kwargs["tasks"], login.call_args.kwargs["tasks_write"]), (True, False))

    def test_write_scope_is_the_exact_google_scope(self):
        self.assertEqual(ga.TASKS_WRITE_SCOPE, "https://www.googleapis.com/auth/tasks")


class SelfcheckTests(unittest.TestCase):
    def run_selfcheck(self, **data):
        with tempfile.TemporaryDirectory() as d:
            tok = Path(d) / "t.json"
            tok.write_text(json.dumps({"scope": data.pop("scope", "")}), encoding="utf-8")
            p = Path(d) / "config.json"
            base = {"state_path": str(Path(d) / "s.json"),
                    "google_accounts": [{"name": "개인", "token_path": str(tok)}]}
            base.update(data)
            p.write_text(json.dumps(base), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = main(["selfcheck", "--config", str(p)])
        self.assertEqual(code, 0)
        return next(l for l in out.getvalue().splitlines() if l.startswith("tasks_write"))

    def test_off_and_on_lines(self):
        self.assertIn("꺼짐", self.run_selfcheck())
        on = self.run_selfcheck(tasks_accounts=["개인"], tasks_write=True)
        self.assertIn("개인 (권한 없음: google-login --tasks-write)", on)
        on = self.run_selfcheck(tasks_accounts=["개인"], tasks_write=True, scope=ga.TASKS_WRITE_SCOPE)
        self.assertIn("개인 (권한 있음)", on)
        self.assertIn("하루 20건", on)


if __name__ == "__main__":
    unittest.main()
