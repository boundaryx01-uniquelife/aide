import _bootstrap  # noqa: F401
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from aide import cleanup, evening, gmail, heartbeat, inbox, rules, telegram
from aide.__main__ import main
from aide.checks import git_dirty, yesterday
from aide.config import Config, ConfigError, load_config
from aide.models import Finding
from aide.state import State

DAY = datetime(2026, 10, 14, 12, 0)
SUN = datetime(2026, 10, 18, 20, 5)   # a Sunday evening
OWNER = "111"
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": OWNER}
DID = "abcd1234"
LINK = "https://mail.google.com/mail/u/0/#all/0000000a"


def mail(n, addr="a@x.kr", label="A", bulk=False, title=None, priority=1):
    return Finding(key=f"mail:{n:08x}", source="mail", title=title or f"{label}: 제목{n}",
                   url=f"https://mail.google.com/mail/u/0/#all/{n:08x}", rule_key=f"mail:{addr}" if addr else "",
                   label=label, bulk=bulk, priority=priority)


def news(n):
    return Finding(key=f"news:{n}", source="news", title=f"뉴스{n}")


class RuleStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "rules.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_key_must_be_one_full_lowercase_address(self):
        for ok in ("mail:a@x.kr", "mail:first.last+tag@sub.pen.go.kr"):
            self.assertTrue(rules.valid_key(ok), ok)
        for bad in ("mail:pen.go.kr", "mail:@x.kr", "mail:A@x.kr", "news:abc", "a@x.kr", "mail:a@x", "",
                    "mail:a b@x.kr", None, 5):
            self.assertFalse(rules.valid_key(bad), bad)

    def test_set_save_reload_and_remove(self):
        r = rules.Rules(self.path)
        self.assertTrue(r.set("mail:a@x.kr", "hide", "A\x00사", DAY))
        r.save()
        again = rules.Rules(self.path)
        self.assertEqual(again.action("mail:a@x.kr"), "hide")
        self.assertEqual(again.rules["mail:a@x.kr"]["label"], "A사")   # cleaned
        self.assertTrue(again.remove("mail:a@x.kr"))
        again.save()
        self.assertIsNone(rules.Rules(self.path).action("mail:a@x.kr"))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["rules.json"])   # no tmp left behind

    def test_bad_key_or_action_changes_nothing(self):
        r = rules.Rules(self.path)
        self.assertFalse(r.set("mail:x.kr", "hide", "", DAY))
        self.assertFalse(r.set("mail:a@x.kr", "delete", "", DAY))
        self.assertEqual(r.rules, {})
        self.assertFalse(r.dirty)

    def test_cap_of_200(self):
        r = rules.Rules(self.path)
        for i in range(rules.MAX_RULES):
            self.assertTrue(r.set(f"mail:u{i}@x.kr", "hide", "", DAY))
        self.assertFalse(r.set("mail:extra@x.kr", "hide", "", DAY))
        self.assertTrue(r.set("mail:u0@x.kr", "pin", "", DAY))   # changing an existing one is still fine

    def test_damaged_file_means_no_rules_not_a_crash(self):
        self.path.write_text("{not json", encoding="utf-8")
        r = rules.Rules(self.path)
        self.assertEqual(r.rules, {})
        self.assertTrue(any(p.name.startswith("rules.json.corrupt-") for p in self.path.parent.iterdir()))

    def test_hand_edited_garbage_entries_are_dropped(self):
        self.path.write_text(json.dumps({"rules": {
            "mail:ok@x.kr": {"action": "hide", "label": "OK", "at": "t"},
            "mail:evil.kr": {"action": "hide"},                    # domain, not an address
            "mail:b@x.kr": {"action": "forward"},                  # not an allowed action
            "mail:c@x.kr": "oops",
        }}), encoding="utf-8")
        self.assertEqual(set(rules.Rules(self.path).rules), {"mail:ok@x.kr"})

    def test_rule_id_lookup(self):
        r = rules.Rules(self.path)
        r.set("mail:a@x.kr", "hide", "A", DAY)
        rid = rules.rule_id("mail:a@x.kr")
        self.assertRegex(rid, r"^[0-9a-f]{8}$")
        self.assertEqual(r.find_by_id(rid), "mail:a@x.kr")
        self.assertIsNone(r.find_by_id("00000000"))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.r = rules.Rules(Path(self.dir.name) / "rules.json")
        self.r.set("mail:hide@x.kr", "hide", "H", DAY)
        self.r.set("mail:pin@x.kr", "pin", "P", DAY)

    def tearDown(self):
        self.dir.cleanup()

    def test_split(self):
        fs = [mail(1, "hide@x.kr"), mail(2, "pin@x.kr"), mail(3, "other@x.kr"), news(1), mail(4, "")]
        shown, hidden, folded = rules.apply(fs, self.r)
        self.assertEqual([f.key for f in hidden], [fs[0].key])
        self.assertEqual({f.key: f.priority for f in shown}, {fs[1].key: 3, fs[2].key: 1, fs[3].key: 1, fs[4].key: 1})
        self.assertEqual(folded, [])

    def test_fold_only_bulk_mail_without_a_rule_and_only_when_asked(self):
        fs = [mail(1, "n@x.kr", bulk=True), mail(2, "hide@x.kr", bulk=True), mail(3, "pin@x.kr", bulk=True),
              mail(4, "p@x.kr", bulk=False), mail(5, "", bulk=True)]
        shown, hidden, folded = rules.apply(fs, self.r, fold_bulk=True)
        self.assertEqual([f.key for f in folded], [fs[0].key])
        self.assertEqual([f.key for f in hidden], [fs[1].key])
        self.assertIn(fs[2].key, [f.key for f in shown])          # a pin rule beats the bulk heuristic
        self.assertIn(fs[4].key, [f.key for f in shown])          # no usable address -> never touched
        shown2, _, folded2 = rules.apply(fs, self.r, fold_bulk=False)
        self.assertEqual(folded2, [])
        self.assertIn(fs[0].key, [f.key for f in shown2])

    def test_mail_senders_allowlist_beats_hide(self):
        fs = [mail(1, "hide@x.kr")]
        _, hidden, _ = rules.apply(fs, self.r, allowed_senders=["x.kr"])
        self.assertEqual(hidden, [])
        _, hidden, _ = rules.apply(fs, self.r, allowed_senders=["hide@x.kr"])
        self.assertEqual(hidden, [])
        _, hidden, _ = rules.apply(fs, self.r, allowed_senders=["other.kr"])
        self.assertEqual(len(hidden), 1)

    def test_finding_without_rule_key_is_never_hidden(self):
        shown, hidden, _ = rules.apply([news(1)], self.r, fold_bulk=True)
        self.assertEqual((len(shown), hidden), (1, []))


class GmailHeaderTests(unittest.TestCase):
    def get_for(self, headers, calls):
        def get(url, headers_=None, **kw):
            calls.append(url)
            if "/messages?" in url:
                return {"messages": [{"id": "0000000a"}]}
            return {"id": "0000000a", "payload": {"headers": [{"name": k, "value": v} for k, v in headers.items()]}}
        return lambda url, headers=None: get(url)

    def test_bulk_flag_is_presence_only_and_value_is_never_kept(self):
        calls = []
        secret = "<https://unsubscribe.evil.example/u/SECRET123>"
        get = self.get_for({"From": "Shop <Promo@Shop.KR>", "Subject": "세일", "List-Unsubscribe": secret}, calls)
        out = gmail.unread_from("T", ["shop.kr"], 5, get=get)
        f = out[0]
        self.assertTrue(f.bulk)
        self.assertEqual(f.rule_key, "mail:promo@shop.kr")        # lowercased full address
        self.assertEqual(f.label, "Shop")
        for field in (f.key, f.title, f.detail, f.url, f.label, f.rule_key):
            self.assertNotIn("SECRET123", field)
        self.assertNotIn("SECRET123", repr(f))

    def test_plain_mail_is_not_bulk_and_headers_requested_are_metadata_only(self):
        calls = []
        get = self.get_for({"From": "a@pen.go.kr", "Subject": "안내"}, calls)
        f = gmail.unread_from("T", ["pen.go.kr"], 5, get=get)[0]
        self.assertFalse(f.bulk)
        msg_url = [c for c in calls if "/messages/" in c][0]
        self.assertIn("format=metadata", msg_url)
        for h in ("List-Unsubscribe", "List-Id", "Precedence"):
            self.assertIn(f"metadataHeaders={h}", msg_url)
        self.assertNotIn("snippet", msg_url)

    def test_precedence_bulk_also_counts(self):
        get = self.get_for({"From": "a@pen.go.kr", "Subject": "x", "Precedence": "bulk"}, [])
        self.assertTrue(gmail.unread_from("T", ["pen.go.kr"], 5, get=get)[0].bulk)

    def test_odd_sender_has_no_rule_key(self):
        get = self.get_for({"From": "undisclosed-recipients:;", "Subject": "x"}, [])
        out = gmail.unread_from("T", [], 5, get=get)  # no senders -> nothing; use recent path instead
        self.assertEqual(out, [])
        get = self.get_for({"From": "weird", "Subject": "x"}, [])
        f = gmail.recent_unread("T", 5, 12, [], DAY, get=get)[0]
        self.assertEqual(f.rule_key, "")

    def test_last_item_keeps_its_rule_fields_when_more_mail_exists(self):
        ids = [f"{i:08x}" for i in range(1, 9)]

        def get(url, headers=None):
            if "/messages?" in url:
                return {"messages": [{"id": i} for i in ids]}
            return {"id": "x", "payload": {"headers": [{"name": "From", "value": "Bulk <b@x.kr>"},
                                                         {"name": "Subject", "value": "s"},
                                                         {"name": "List-Id", "value": "<l.x.kr>"}]}}
        out = gmail.recent_unread("T", 3, 12, [], DAY, get=get)
        self.assertIn("더", out[-1].detail)
        self.assertEqual((out[-1].rule_key, out[-1].label, out[-1].bulk), ("mail:b@x.kr", "Bulk", True))


class StateExtrasTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_targets_roundtrip_and_cap(self):
        s = State(self.path)
        s.add_digest(DID, ["k"], DAY, [{"k": f"mail:u{i}@x.kr", "l": "L", "u": LINK} for i in range(12)])
        s.save()
        got = State(self.path).digests[DID]["targets"]
        self.assertEqual(len(got), 8)
        self.assertEqual(got[0], {"k": "mail:u0@x.kr", "l": "L", "u": LINK})

    def test_old_digest_without_targets_loads(self):
        self.path.write_text(json.dumps({"digests": {DID: {"keys": ["a"], "at": DAY.isoformat(), "status": "ok"}}}),
                             encoding="utf-8")
        self.assertEqual(State(self.path).digests[DID]["targets"], [])

    def test_hidden_counts_summary_and_pruning(self):
        s = State(self.path)
        s.add_hidden("mail:a@x.kr", DAY)
        s.add_hidden("mail:a@x.kr", DAY)
        s.add_hidden("mail:b@x.kr", DAY - timedelta(days=3))
        s.add_hidden("mail:old@x.kr", DAY - timedelta(days=20))
        self.assertEqual(s.hidden_summary(DAY), {"mail:a@x.kr": 2, "mail:b@x.kr": 1})
        s.prune(DAY)
        self.assertNotIn((DAY - timedelta(days=20)).date().isoformat(), s.hidden)
        s.save()
        self.assertEqual(State(self.path).hidden_summary(DAY)["mail:a@x.kr"], 2)

    def test_old_state_file_loads_with_empty_hidden(self):
        self.path.write_text('{"seen": {}}', encoding="utf-8")
        self.assertEqual(State(self.path).hidden, {})


class TelegramButtonTests(unittest.TestCase):
    def test_only_gmail_urls_become_url_buttons(self):
        kb = json.loads(telegram._keyboard([[("열기", "https://mail.google.com/mail/u/0/#all/1"), ("확인", "d:abcd1234:ok")]]))
        self.assertEqual(kb["inline_keyboard"][0][0], {"text": "열기", "url": "https://mail.google.com/mail/u/0/#all/1"})
        self.assertEqual(kb["inline_keyboard"][0][1], {"text": "확인", "callback_data": "d:abcd1234:ok"})
        for evil in ("https://evil.example/u", "http://mail.google.com/x", "https://mail.google.com.evil.example/x"):
            with self.assertRaises(telegram.TelegramError, msg=evil):
                telegram._keyboard([[("x", evil)]])

    def test_edit_text_and_set_buttons_calls(self):
        calls = []
        with mock.patch.object(telegram, "_call", side_effect=lambda m, t, p, timeout=15: calls.append((m, p))):
            telegram.edit_text("t", "1", 5, "본문", [[("a", "m:abcd1234:close")]])
            telegram.edit_text("t", "1", 5, "본문")
            telegram.set_buttons("t", "1", 5, [[("정리", "m:abcd1234:open")]])
        self.assertEqual([c[0] for c in calls], ["editMessageText", "editMessageText", "editMessageReplyMarkup"])
        self.assertNotIn("parse_mode", calls[0][1])
        self.assertEqual(json.loads(calls[1][1]["reply_markup"]), {"inline_keyboard": []})

    def test_clean_button_row_only_when_asked(self):
        self.assertEqual(len(inbox.buttons_for(DID)), 1)
        rows = inbox.buttons_for(DID, with_clean=True)
        self.assertEqual(rows[1], [("🧹 정리", f"m:{DID}:open")])
        for _, data in rows[0] + rows[1]:
            self.assertLessEqual(len(data.encode()), 64)


class HeartbeatRulesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.dir.name) / "state.json"
        self.rules_path = rules.path_for(self.state_path)
        self.cfg = Config(state_path=str(self.state_path), rules_enabled=True)
        self.sent = []

    def tearDown(self):
        self.dir.cleanup()

    def sender(self, text, *, token, chat_id, buttons=None):
        self.sent.append((text, buttons))

    def make_rule(self, key, action):
        r = rules.Rules(self.rules_path)
        r.set(key, action, "라벨", DAY)
        r.save()

    def hb(self, findings, send=True, **kw):
        kw.setdefault("now", DAY)
        kw.setdefault("sender", self.sender)
        with mock.patch.object(heartbeat, "collect", return_value=findings), mock.patch.dict(os.environ, ENV):
            return heartbeat.run(self.cfg, send=send, **kw)

    def test_switch_off_ignores_stored_rules_and_buttons(self):
        self.make_rule("mail:a@x.kr", "hide")
        self.cfg.rules_enabled = False
        self.hb([mail(1, "a@x.kr", "숨길메일")])
        text, buttons = self.sent[0]
        self.assertIn("숨길메일", text)
        self.assertEqual(len(buttons), 1)                         # no 정리 row
        self.assertEqual(State(self.state_path).hidden, {})

    def test_hide_is_counted_marked_seen_and_not_listed(self):
        self.make_rule("mail:a@x.kr", "hide")
        self.hb([mail(1, "a@x.kr", "숨길메일"), mail(2, "b@x.kr", "보일메일")])
        text, _ = self.sent[0]
        self.assertNotIn("숨길메일", text)
        self.assertIn("보일메일", text)
        st = State(self.state_path)
        self.assertTrue(st.is_seen(mail(1).key))
        self.assertEqual(st.hidden_summary(DAY), {"mail:a@x.kr": 1})

    def test_only_hidden_mail_sends_nothing_but_still_records(self):
        self.make_rule("mail:a@x.kr", "hide")
        self.assertEqual(self.hb([mail(1, "a@x.kr")]), 0)
        self.assertEqual(self.sent, [])
        st = State(self.state_path)
        self.assertEqual(st.hidden_summary(DAY), {"mail:a@x.kr": 1})
        self.assertTrue(st.is_seen(mail(1).key))
        self.hb([mail(1, "a@x.kr")])                              # same mail again: no double count
        self.assertEqual(State(self.state_path).hidden_summary(DAY), {"mail:a@x.kr": 1})

    def test_failed_send_does_not_persist_hidden_counts(self):
        self.make_rule("mail:a@x.kr", "hide")

        def boom(text, *, token, chat_id, buttons=None):
            raise telegram.TelegramError("down")
        self.assertEqual(self.hb([mail(1, "a@x.kr"), mail(2, "b@x.kr")], sender=boom), 1)
        self.assertFalse(self.state_path.exists())

    def test_pin_goes_to_its_own_top_section_above_ai(self):
        self.make_rule("mail:p@x.kr", "pin")
        fs = [mail(1, "n@x.kr", "일반"), mail(2, "p@x.kr", "고정"), mail(3, "ai@x.kr", "AI선택")]
        self.cfg.llm_enabled = True
        seen = {}

        def fake_rank(findings, cfg, *, now, calls_today=0, api_key="", post=None, call_status=None):
            seen["keys"] = [f.key for f in findings]
            return ["high" if f.label == "AI선택" else "normal" for f in findings]
        with mock.patch.object(heartbeat.llm, "rank", side_effect=fake_rank):
            self.hb(fs)
        text = self.sent[0][0]
        self.assertLess(text.index("중요 (내 규칙)"), text.index("고정"))
        self.assertLess(text.index("고정"), text.index("중요 (AI 판단)"))
        self.assertLess(text.index("중요 (AI 판단)"), text.index("AI선택"))
        self.assertNotIn(mail(2).key, seen["keys"])               # a pinned mail never goes to the LLM

    def test_hidden_mail_never_reaches_the_llm(self):
        self.make_rule("mail:a@x.kr", "hide")
        self.cfg.llm_enabled = True
        seen = {}

        def fake_rank(findings, cfg, *, now, calls_today=0, api_key="", post=None, call_status=None):
            seen["keys"] = [f.key for f in findings]
            return None
        with mock.patch.object(heartbeat.llm, "rank", side_effect=fake_rank):
            self.hb([mail(1, "a@x.kr"), mail(2, "b@x.kr")])
        self.assertEqual(seen["keys"], [mail(2).key])

    def test_bulk_fold_one_line_and_marked_seen_with_a_real_digest(self):
        self.cfg.mail_bulk_fold = True
        fs = [mail(1, "ad1@x.kr", "광고A", bulk=True), mail(2, "ad2@x.kr", "광고B", bulk=True),
              mail(3, "real@x.kr", "진짜", bulk=False)]
        self.hb(fs)
        text = self.sent[0][0]
        self.assertIn("광고·정기 메일 2건: 광고A, 광고B", text)
        self.assertNotIn("광고A: 제목1", text)
        st = State(self.state_path)
        self.assertTrue(st.is_seen(mail(1).key) and st.is_seen(mail(2).key))
        targets = next(iter(st.digests.values()))["targets"]
        self.assertEqual({t["k"] for t in targets}, {"mail:ad1@x.kr", "mail:ad2@x.kr", "mail:real@x.kr"})

    def test_folded_mail_alone_is_not_sent_and_stays_unseen(self):
        self.cfg.mail_bulk_fold = True
        self.assertEqual(self.hb([mail(1, "ad@x.kr", "광고", bulk=True)]), 0)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.state_path.exists() and State(self.state_path).is_seen(mail(1).key))
        self.hb([mail(1, "ad@x.kr", "광고", bulk=True), mail(2, "real@x.kr", "진짜")])   # rides along later
        self.assertIn("광고·정기 메일 1건", self.sent[0][0])

    def test_bulk_without_fold_switch_is_listed_normally(self):
        self.hb([mail(1, "ad@x.kr", "광고", bulk=True)])
        self.assertIn("광고: 제목1", self.sent[0][0])
        self.assertNotIn("광고·정기", self.sent[0][0])

    def test_clean_button_row_present_only_with_mail_targets(self):
        self.hb([mail(1, "a@x.kr")])
        buttons = self.sent[0][1]
        self.assertEqual(buttons[1][0][1], f"m:{next(iter(State(self.state_path).digests))}:open")
        self.sent.clear()
        self.hb([news(1)])
        self.assertEqual(len(self.sent[0][1]), 1)

    def test_targets_are_deduplicated_per_address_and_capped(self):
        fs = [mail(i, f"u{i}@x.kr") for i in range(1, 12)] + [mail(99, "u1@x.kr")]
        self.hb(fs)
        t = next(iter(State(self.state_path).digests.values()))["targets"]
        self.assertEqual(len(t), 8)
        self.assertEqual(len({x["k"] for x in t}), 8)

    def test_preview_applies_rules_but_changes_nothing(self):
        self.make_rule("mail:a@x.kr", "hide")
        self.cfg.mail_bulk_fold = True
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.hb([mail(1, "a@x.kr", "숨길"), mail(2, "ad@x.kr", "광고", bulk=True), mail(3, "b@x.kr", "보일")],
                    send=False)
        out = buf.getvalue()
        self.assertIn("보일", out)
        self.assertNotIn("숨길", out)
        self.assertIn("광고·정기 메일 1건", out)
        self.assertFalse(self.state_path.exists())

    def test_explicit_mail_senders_beat_a_hide_rule(self):
        self.make_rule("mail:a@x.kr", "hide")
        self.cfg.mail_senders = ["x.kr"]
        self.hb([mail(1, "a@x.kr", "허용발신자")])
        self.assertIn("허용발신자", self.sent[0][0])

    def test_damaged_rules_file_hides_nothing(self):
        self.rules_path.write_text("garbage", encoding="utf-8")
        self.hb([mail(1, "a@x.kr", "그대로")])
        self.assertIn("그대로", self.sent[0][0])


class Api:
    TelegramError = telegram.TelegramError

    def __init__(self, updates):
        self.updates = updates
        self.answers, self.sent, self.edits, self.cleared, self.set = [], [], [], [], []

    def get_updates(self, token, offset=0, timeout=0, allowed=("callback_query",)):
        return self.updates

    def answer_callback(self, token, cid, text):
        self.answers.append(text)

    def send(self, text, *, token, chat_id, buttons=None):
        if buttons:
            telegram._keyboard(buttons)    # would the real API accept these buttons?
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


def press(data, uid=1, sender=OWNER, chat=OWNER):
    return {"update_id": uid, "callback_query": {
        "id": f"c{uid}", "from": {"id": int(sender), "is_bot": False}, "data": data,
        "message": {"message_id": 77, "chat": {"id": int(chat)}}}}


class CleanupFlowTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.dir.name) / "state.json"
        self.rules_path = rules.path_for(self.state_path)
        self.cfg = Config(state_path=str(self.state_path), rules_enabled=True)
        st = State(self.state_path)
        st.add_digest(DID, ["k1"], DAY, [
            {"k": "mail:a@x.kr", "l": "에이사", "u": LINK},
            {"k": "mail:b@x.kr", "l": "비사", "u": "https://evil.example/phish"},
        ])
        st.save()

    def tearDown(self):
        self.dir.cleanup()

    def poll(self, *datas, **kw):
        cfg = kw.pop("cfg", self.cfg)
        ups = [press(d, uid=i + 1) for i, d in enumerate(datas)]
        api = Api(ups)
        inbox.poll_once(cfg, token="t", chat_id=OWNER, api=api, now=DAY)
        return api

    def rule_actions(self):
        return {k: v["action"] for k, v in rules.Rules(self.rules_path).rules.items()}

    def test_open_lists_senders_with_addresses_and_choice_buttons(self):
        api = self.poll(f"m:{DID}:open")
        text, buttons = api.sent[0]
        self.assertIn("<a@x.kr>", text)
        self.assertIn("<b@x.kr>", text)
        data = [d for row in buttons for _, d in row]
        self.assertIn(f"m:{DID}:t0", data)
        self.assertIn("r:list", data)
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))

    def test_choose_sender_shows_the_four_choices(self):
        api = self.poll(f"m:{DID}:t0")
        _, text, buttons = api.edits[0]
        self.assertIn("a@x.kr", text)
        labels = [t for row in buttons for t, _ in row]
        self.assertEqual(labels, ["알림제외", "유지", "중요", "수신거부"])

    def test_hide_pin_keep_change_only_the_rules_file(self):
        self.poll(f"m:{DID}:t0:hide")
        self.assertEqual(self.rule_actions(), {"mail:a@x.kr": "hide"})
        self.poll(f"m:{DID}:t0:pin")
        self.assertEqual(self.rule_actions(), {"mail:a@x.kr": "pin"})
        self.poll(f"m:{DID}:t0:keep")
        self.assertEqual(self.rule_actions(), {})

    def test_pressing_twice_gives_the_same_result(self):
        self.poll(f"m:{DID}:t0:hide", f"m:{DID}:t0:hide")
        self.assertEqual(self.rule_actions(), {"mail:a@x.kr": "hide"})
        self.poll(f"m:{DID}:t0:keep", f"m:{DID}:t0:keep")
        self.assertEqual(self.rule_actions(), {})

    def test_unsubscribe_sends_nothing_and_offers_only_gmail_link(self):
        api = self.poll(f"m:{DID}:t0:unsub")
        _, text, buttons = api.edits[0]
        self.assertIn("수신거부", text)
        flat = [d for row in buttons for _, d in row]
        self.assertIn(LINK, flat)
        self.assertEqual(self.rule_actions(), {})                 # nothing decided yet
        self.assertEqual(api.sent, [])

    def test_a_non_gmail_link_is_never_offered(self):
        api = self.poll(f"m:{DID}:t1:unsub")
        flat = [d for row in api.edits[0][2] for _, d in row]
        self.assertFalse([d for d in flat if d.startswith("http")])

    def test_stranger_changes_nothing_and_gets_no_reply(self):
        api = Api([press(f"m:{DID}:t0:hide", sender="999", chat="999")])
        inbox.poll_once(self.cfg, token="t", chat_id=OWNER, api=api, now=DAY)
        self.assertEqual((api.answers, api.sent, api.edits), ([], [], []))
        self.assertEqual(self.rule_actions(), {})

    def test_bad_shapes_expired_and_out_of_range_change_nothing(self):
        api = self.poll("m:zzzz:t0:hide", f"m:{DID}:t9:hide", f"m:{DID}:t0:delete", "m:00000000:open",
                        f"m:{DID}:t5", "r:ffffffff:del")
        self.assertEqual(self.rule_actions(), {})
        self.assertEqual(api.sent, [])
        self.assertEqual(api.edits, [])                           # nothing was redrawn or changed
        self.assertEqual(api.answers[:3], ["알 수 없는 버튼이에요"] * 3)   # wrong shape / digit 9 / unknown action
        self.assertEqual(api.answers[3:5], [cleanup.EXPIRED] * 2)         # unknown digest / index past the targets
        self.assertEqual(api.answers[5], "이미 없어요")                    # deleting a rule that is not there

    def test_rules_off_means_expired_and_nothing_written(self):
        self.cfg.rules_enabled = False
        api = self.poll(f"m:{DID}:t0:hide", f"m:{DID}:open")
        self.assertEqual(api.answers, [cleanup.EXPIRED, cleanup.EXPIRED])
        self.assertEqual((api.sent, api.edits), ([], []))
        self.assertFalse(self.rules_path.exists())

    def test_list_and_delete(self):
        r = rules.Rules(self.rules_path)
        r.set("mail:a@x.kr", "hide", "에이사", DAY)
        r.set("mail:b@x.kr", "pin", "비사", DAY + timedelta(minutes=1))
        r.save()
        api = self.poll("r:list")
        text, buttons = api.sent[0]
        self.assertIn("알림제외 · 에이사 <a@x.kr>", text)
        self.assertIn("중요 · 비사 <b@x.kr>", text)
        rid = rules.rule_id("mail:a@x.kr")
        self.assertIn(f"r:{rid}:del", [d for row in buttons for _, d in row])
        api2 = self.poll(f"r:{rid}:del")
        self.assertEqual(self.rule_actions(), {"mail:b@x.kr": "pin"})
        self.assertNotIn("a@x.kr", api2.edits[0][1])

    def test_full_rule_file_refuses_politely(self):
        r = rules.Rules(self.rules_path)
        for i in range(rules.MAX_RULES):
            r.set(f"mail:u{i}@x.kr", "hide", "", DAY)
        r.save()
        api = self.poll(f"m:{DID}:t0:hide")
        self.assertEqual(len(rules.Rules(self.rules_path).rules), rules.MAX_RULES)
        self.assertEqual(api.answers, ["규칙이 가득 찼어요"])

    def test_confirm_button_keeps_the_clean_button(self):
        api = self.poll(f"d:{DID}:ok")
        self.assertEqual(api.set, [(77, [[("🧹 정리", f"m:{DID}:open")]])])
        self.assertEqual(api.cleared, [])

    def test_confirm_without_targets_clears_everything_as_before(self):
        st = State(self.state_path)
        st.add_digest("11112222", ["k2"], DAY)
        st.save()
        api = self.poll("d:11112222:ok")
        self.assertEqual(api.cleared, [77])
        self.assertEqual(api.set, [])

    def test_close_dismisses(self):
        api = self.poll(f"m:{DID}:close")
        self.assertEqual(api.edits[0][1], "닫았어요")


class EveningSummaryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.dir.name) / "state.json"
        self.cfg = Config(state_path=str(self.state_path), rules_enabled=True, evening_hour=20)
        r = rules.Rules(rules.path_for(self.state_path))
        r.set("mail:a@x.kr", "hide", "에이사", DAY)
        r.save()
        st = State(self.state_path)
        st.add_hidden("mail:a@x.kr", SUN - timedelta(days=2))
        st.add_hidden("mail:a@x.kr", SUN - timedelta(days=1))
        st.save()
        self.sent = []
        self.p = [mock.patch.object(yesterday, "commits_on", lambda r, d: []),
                  mock.patch.object(git_dirty, "count_changes", lambda r: None),
                  mock.patch.dict(os.environ, ENV)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.dir.cleanup()

    def send(self, now):
        return evening.run(self.cfg, send=True, now=now,
                           sender=lambda text, **k: self.sent.append((text, k.get("buttons"))))

    def test_sunday_shows_counts_and_the_rule_list_button(self):
        self.assertEqual(self.send(SUN), 0)
        text, buttons = self.sent[0]
        self.assertIn("■ 걸러낸 것 (지난 7일)", text)
        self.assertIn("• 에이사 <a@x.kr>: 2건", text)
        self.assertEqual(buttons, [[("📋 규칙 목록", "r:list")]])

    def test_weekday_has_no_summary(self):
        self.send(SUN - timedelta(days=1))
        self.assertNotIn("걸러낸 것", self.sent[0][0])
        self.assertIsNone(self.sent[0][1])

    def test_rules_off_has_no_summary(self):
        self.cfg.rules_enabled = False
        self.send(SUN)
        self.assertNotIn("걸러낸 것", self.sent[0][0])

    def test_nothing_hidden_means_no_summary(self):
        State(self.state_path).hidden.clear()
        st = State(self.state_path)
        st.hidden = {}
        st.save()
        self.send(SUN)
        self.assertNotIn("걸러낸 것", self.sent[0][0])


class CliAndConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.dir.name) / "state.json"
        self.cfg_path = Path(self.dir.name) / "config.json"
        self.cfg_path.write_text(
            json.dumps({"state_path": str(self.state_path), "rules_enabled": True, "evening_hour": 20}),
            encoding="utf-8")
        r = rules.Rules(rules.path_for(self.state_path))
        r.set("mail:a@x.kr", "hide", "에이사", DAY)
        r.save()

    def tearDown(self):
        self.dir.cleanup()

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*argv, "--config", str(self.cfg_path)])
        return code, out.getvalue(), err.getvalue()

    def test_list_then_remove_by_id_and_by_key(self):
        code, out, _ = self.run_main("rules")
        self.assertEqual(code, 0)
        self.assertIn("a@x.kr", out)
        rid = rules.rule_id("mail:a@x.kr")
        self.assertIn(rid, out)
        self.assertEqual(self.run_main("rules", "--remove", rid)[0], 0)
        self.assertEqual(rules.Rules(rules.path_for(self.state_path)).rules, {})
        self.assertEqual(self.run_main("rules", "--remove", rid)[0], 1)

    def test_remove_by_full_key(self):
        self.assertEqual(self.run_main("rules", "--remove", "mail:a@x.kr")[0], 0)

    def test_empty_list_message(self):
        self.run_main("rules", "--remove", "mail:a@x.kr")
        code, out, _ = self.run_main("rules")
        self.assertEqual(code, 0)
        self.assertIn("저장된 규칙이 없습니다", out)

    def test_selfcheck_shows_rules_status(self):
        code, out, _ = self.run_main("selfcheck")
        self.assertEqual(code, 0)
        self.assertIn("rules", out)

    def test_config_defaults_and_validation(self):
        self.assertEqual((Config().rules_enabled, Config().mail_bulk_fold), (False, False))
        for bad in ({"rules_enabled": "yes"}, {"mail_bulk_fold": 1}):
            p = Path(self.dir.name) / "bad.json"
            p.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_rules_need_the_weekly_summary_so_evening_must_be_on(self):
        p = Path(self.dir.name) / "norule.json"
        p.write_text(json.dumps({"rules_enabled": True}), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(p)
        p.write_text(json.dumps({"rules_enabled": True, "evening_hour": 20}), encoding="utf-8")
        self.assertTrue(load_config(p).rules_enabled)
        p.write_text(json.dumps({"rules_enabled": False}), encoding="utf-8")
        self.assertFalse(load_config(p).rules_enabled)

    def test_rules_file_is_git_ignored(self):
        text = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/rules.json", text)


if __name__ == "__main__":
    unittest.main()
