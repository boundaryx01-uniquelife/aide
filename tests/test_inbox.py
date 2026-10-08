import _bootstrap  # noqa: F401
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from aide import commands, inbox, telegram
from aide.config import Config
from aide.state import State, StateLocked, locked

NOW = datetime(2026, 10, 3, 9, 0)
OWNER = "111"
DID = "abcd1234"


class FakeApi:
    """Stands in for the telegram module; records calls, can be told to fail."""

    TelegramError = telegram.TelegramError

    def __init__(self, updates=None, fail=False):
        self.updates, self.fail = updates or [], fail
        self.answers, self.cleared, self.offsets, self.sent, self.allowed_seen = [], [], [], [], []

    def get_updates(self, token, offset=0, timeout=0, allowed=("callback_query",)):
        self.offsets.append(offset)
        self.allowed_seen.append(tuple(allowed))
        return self.updates

    def answer_callback(self, token, cid, text):
        if self.fail:
            raise telegram.TelegramError("down")
        self.answers.append((cid, text))

    def clear_buttons(self, token, chat_id, message_id):
        self.cleared.append((chat_id, message_id))

    def send(self, text, *, token, chat_id, buttons=None):
        if self.fail:
            raise telegram.TelegramError("down")
        self.sent.append((chat_id, text))


def press(data, uid=1, sender=OWNER, chat=OWNER, is_bot=False, cid="c1"):
    return {
        "update_id": uid,
        "callback_query": {
            "id": cid,
            "from": {"id": int(sender), "is_bot": is_bot},
            "data": data,
            "message": {"message_id": 77, "chat": {"id": int(chat)}},
        },
    }


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"
        self.cfg = Config(state_path=str(self.path))
        st = State(self.path)
        st.mark("k1", NOW)
        st.mark("k2", NOW)
        st.add_digest(DID, ["k1", "k2"], NOW)
        st.save()

    def tearDown(self):
        self.dir.cleanup()

    def poll(self, updates, **kw):
        api = FakeApi(updates, **kw)
        n = inbox.poll_once(self.cfg, token="t", chat_id=OWNER, api=api)
        return n, api

    def test_ok_is_recorded_and_buttons_removed(self):
        n, api = self.poll([press(f"d:{DID}:ok")])
        self.assertEqual(n, 1)
        st = State(self.path)
        self.assertEqual(st.digests[DID]["status"], "ok")
        self.assertIn("k1", st.seen)  # acknowledged: stays "seen"
        self.assertEqual(api.cleared, [(OWNER, 77)])
        self.assertEqual(api.answers[0][1], inbox.REPLIES["ok"])

    def test_later_makes_items_reportable_again(self):
        self.poll([press(f"d:{DID}:later")])
        st = State(self.path)
        self.assertNotIn("k1", st.seen)
        self.assertNotIn("k2", st.seen)

    def test_stranger_is_ignored_silently_but_consumed(self):
        n, api = self.poll([press(f"d:{DID}:ok", uid=5, sender="999", chat="999")])
        self.assertEqual(n, 0)
        self.assertEqual(api.answers, [])      # no reply at all
        self.assertEqual(api.cleared, [])
        st = State(self.path)
        self.assertEqual(st.digests[DID]["status"], "pending")
        self.assertEqual(st.offset, 6)         # but the update is not re-read forever

    def test_other_sender_in_owners_chat_is_ignored(self):
        n, api = self.poll([press(f"d:{DID}:ok", sender="999", chat=OWNER)])
        self.assertEqual(n, 0)
        self.assertEqual(api.answers, [])
        self.assertEqual(State(self.path).digests[DID]["status"], "pending")

    def test_owner_pressing_in_a_group_or_as_bot_is_ignored(self):
        n, api = self.poll([
            press(f"d:{DID}:ok", uid=1, chat="-100"),
            press(f"d:{DID}:ok", uid=2, is_bot=True),
        ])
        self.assertEqual(n, 0)
        self.assertEqual(State(self.path).digests[DID]["status"], "pending")

    def test_bad_data_unknown_and_double_press(self):
        _, api = self.poll([
            press("rm -rf /", uid=1),
            press("d:00000000:ok", uid=2),
            press(f"d:{DID}:ok", uid=3),
            press(f"d:{DID}:skip", uid=4),   # second answer must not override
        ])
        texts = [t for _, t in api.answers]
        self.assertEqual(texts[0], "알 수 없는 버튼이에요")
        self.assertEqual(texts[1], "만료된 알림이에요")
        self.assertEqual(texts[2], inbox.REPLIES["ok"])
        self.assertEqual(texts[3], "이미 처리됐어요")
        self.assertEqual(State(self.path).digests[DID]["status"], "ok")

    def test_redelivery_is_harmless(self):
        u = [press(f"d:{DID}:later")]
        self.poll(u)
        State(self.path)  # state after first press
        st = State(self.path)
        st.mark("k1", NOW)  # heartbeat re-reported k1 meanwhile
        st.save()
        self.poll(u)        # same update delivered again
        self.assertIn("k1", State(self.path).seen)  # not forgotten a second time

    def test_display_failure_does_not_lose_the_record(self):
        n, _ = self.poll([press(f"d:{DID}:skip")], fail=True)
        self.assertEqual(n, 1)
        self.assertEqual(State(self.path).digests[DID]["status"], "skip")

    def test_buttons_still_cleared_when_toast_fails(self):
        _, api = self.poll([press(f"d:{DID}:ok")], fail=True)
        self.assertEqual(api.cleared, [(OWNER, 77)])

    def test_repeat_press_still_removes_stuck_buttons(self):
        self.poll([press(f"d:{DID}:ok", uid=1)], fail=True)
        _, api = self.poll([press(f"d:{DID}:ok", uid=2)])
        self.assertEqual(api.cleared, [(OWNER, 77)])
        self.assertEqual(api.answers[0][1], "이미 처리됐어요")

    def test_offset_is_persisted_and_reused(self):
        self.poll([press(f"d:{DID}:ok", uid=41)])
        _, api = self.poll([])
        self.assertEqual(api.offsets[-1], 42)

    def test_no_updates_does_not_touch_state_file(self):
        before = self.path.read_text(encoding="utf-8")
        self.poll([])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)


class StateExtrasTests(unittest.TestCase):
    def test_old_state_file_without_new_keys_loads(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            p.write_text('{"seen": {"a": "2026-10-01T00:00:00"}}', encoding="utf-8")
            st = State(p)
            self.assertTrue(st.is_seen("a"))
            self.assertEqual((st.digests, st.offset), ({}, 0))

    def test_digests_are_pruned_with_seen(self):
        with tempfile.TemporaryDirectory() as d:
            st = State(Path(d) / "s.json")
            st.add_digest("00000001", ["x"], datetime(2026, 1, 1))
            st.prune(datetime(2026, 10, 3))
            self.assertEqual(st.digests, {})

    def test_lock_excludes_second_holder_and_releases(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            with locked(p):
                with self.assertRaises(StateLocked):
                    with locked(p, timeout=0.3):
                        pass
            with locked(p, timeout=0.3):  # free again after release
                pass

    def test_stale_lock_is_cleared(self):
        import os, time
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            lock = Path(str(p) + ".lock")
            lock.write_text("", encoding="utf-8")
            old = time.time() - 1000
            os.utime(lock, (old, old))
            with locked(p, timeout=0.3, stale=120):
                pass


class WatcherLockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = Config(state_path=str(Path(self.dir.name) / "state.json"))

    def tearDown(self):
        self.dir.cleanup()

    def test_inactive_until_touched_then_released(self):
        self.assertFalse(inbox.watcher_active(self.cfg))
        inbox.touch_watcher(self.cfg)
        self.assertTrue(inbox.watcher_active(self.cfg))
        inbox.release_watcher(self.cfg)
        self.assertFalse(inbox.watcher_active(self.cfg))

    def test_stale_lock_means_watcher_died(self):
        import os, time
        inbox.touch_watcher(self.cfg)
        lock = inbox._watch_lock(self.cfg)
        old = time.time() - inbox.WATCH_FRESH - 10
        os.utime(lock, (old, old))
        self.assertFalse(inbox.watcher_active(self.cfg))

    def test_one_shot_poll_stands_down_while_watching(self):
        from unittest import mock
        import os
        import aide.__main__ as cli

        inbox.touch_watcher(self.cfg)
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch.object(inbox, "poll_once", side_effect=AssertionError("must not poll")):
            self.assertEqual(cli._poll(self.cfg, watch=False), 0)

    def test_second_watcher_exits_immediately(self):
        from unittest import mock
        import os
        import aide.__main__ as cli

        inbox.touch_watcher(self.cfg)
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch.object(inbox, "poll_once", side_effect=AssertionError("must not poll")):
            self.assertEqual(cli._poll(self.cfg, watch=True), 0)
        self.assertTrue(inbox.watcher_active(self.cfg))  # the running one keeps its lock

    def test_watch_loop_keeps_lock_fresh_while_polling(self):
        from unittest import mock
        import os
        import aide.__main__ as cli

        seen = []

        def poll(*a, **k):
            seen.append(inbox.watcher_active(self.cfg))  # lock must exist DURING the wait
            raise KeyboardInterrupt

        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch.object(inbox, "poll_once", side_effect=poll):
            cli._poll(self.cfg, watch=True)
        self.assertEqual(seen, [True])

    def test_watch_loop_releases_lock_on_exit(self):
        from unittest import mock
        import os
        import aide.__main__ as cli

        def stop(*a, **k):
            raise KeyboardInterrupt

        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch.object(inbox, "poll_once", side_effect=stop):
            self.assertEqual(cli._poll(self.cfg, watch=True), 0)
        self.assertFalse(inbox.watcher_active(self.cfg))


class SigtermTests(unittest.TestCase):
    """systemd restarts the watcher with SIGTERM. If that skipped the `finally`, the stale lock
    made the new watcher think another one was running and exit quietly (found on the server)."""

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_sigterm_releases_lock_and_restores_handler(self):
        from unittest import mock
        import signal
        import aide.__main__ as cli

        with tempfile.TemporaryDirectory() as d:
            cfg = Config(state_path=str(Path(d) / "state.json"))
            before = signal.getsignal(signal.SIGTERM)

            def term(*a, **k):
                self.assertTrue(inbox.watcher_active(cfg))
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(1)           # the handler fires here and raises KeyboardInterrupt

            with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
                    mock.patch.object(inbox, "poll_once", side_effect=term):
                self.assertEqual(cli._poll(cfg, watch=True), 0)
            self.assertFalse(inbox.watcher_active(cfg))                 # lock gone -> a restarted watcher can start
            self.assertEqual(signal.getsignal(signal.SIGTERM), before)  # handler restored


class ButtonTests(unittest.TestCase):
    def test_callback_data_fits_telegram_limit_and_matches_parser(self):
        for _, data in inbox.buttons_for("abcd1234")[0]:
            self.assertLessEqual(len(data.encode()), 64)
            self.assertTrue(inbox.CALLBACK_RE.match(data))


def msg(text, uid=1, sender=OWNER, chat=OWNER, chat_type="private", is_bot=False, ts=None, forwarded=False):
    m = {
        "message_id": 9,
        "from": {"id": int(sender), "is_bot": is_bot},
        "chat": {"id": int(chat), "type": chat_type},
        "text": text,
        "date": ts if ts is not None else int(NOW.timestamp()),
    }
    if forwarded:
        m["forward_origin"] = {"type": "user"}
    return {"update_id": uid, "message": m}


class CommandTests(unittest.TestCase):
    """Stage 3b wiring inside inbox.py. commands.py's own logic (normalize, resolve,
    renderers) is covered in test_commands.py; this checks who/what/how-often gating
    and that network-calling handlers run only AFTER the state lock is released."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"

    def tearDown(self):
        self.dir.cleanup()

    def cfg(self, enabled=("오늘",)):
        return Config(state_path=str(self.path), commands=list(enabled))

    def poll(self, cfg, updates, **kw):
        api = FakeApi(updates, **kw)
        n = inbox.poll_once(cfg, token="t", chat_id=OWNER, api=api, now=NOW)
        return n, api

    def test_disabled_by_default_requests_only_callback_query(self):
        _, api = self.poll(Config(state_path=str(self.path)), [])
        self.assertEqual(api.allowed_seen, [("callback_query",)])

    def test_enabled_requests_messages_too(self):
        _, api = self.poll(self.cfg(), [])
        self.assertEqual(api.allowed_seen, [("callback_query", "message")])

    def test_owner_command_executes_and_replies_after_lock_released(self):
        with mock.patch.dict(commands.REGISTRY, {"오늘": lambda cfg, now: "TODAY-REPLY"}):
            n, api = self.poll(self.cfg(), [msg("/오늘")])
        self.assertEqual(n, 1)
        self.assertEqual(api.sent, [(OWNER, "TODAY-REPLY")])

    def test_stranger_message_is_ignored_silently(self):
        n, api = self.poll(self.cfg(), [msg("/오늘", sender="999", chat="999")])
        self.assertEqual(n, 0)
        self.assertEqual(api.sent, [])

    def test_group_chat_from_owner_is_ignored(self):
        n, api = self.poll(self.cfg(), [msg("/오늘", chat="-100", chat_type="group")])
        self.assertEqual(n, 0)
        self.assertEqual(api.sent, [])

    def test_forwarded_message_is_ignored(self):
        n, api = self.poll(self.cfg(), [msg("/오늘", forwarded=True)])
        self.assertEqual(n, 0)
        self.assertEqual(api.sent, [])

    def test_unknown_text_gets_a_reply_but_does_not_execute(self):
        n, api = self.poll(self.cfg(), [msg("아무말")])
        self.assertEqual(n, 0)
        self.assertEqual(api.sent, [(OWNER, "모르는 명령이에요. /도움")])

    def test_command_not_in_enabled_list_is_treated_as_unknown(self):
        n, api = self.poll(self.cfg(enabled=["일정"]), [msg("/오늘")])
        self.assertEqual(n, 0)
        self.assertEqual(api.sent, [(OWNER, "모르는 명령이에요. /도움")])

    def test_stale_command_is_skipped_with_one_notice(self):
        old_ts = int(NOW.timestamp()) - commands.STALE_SECONDS - 10
        with mock.patch.dict(commands.REGISTRY, {"오늘": lambda cfg, now: "TODAY-REPLY"}):
            n, api = self.poll(self.cfg(), [msg("/오늘", ts=old_ts)])
        self.assertEqual(n, 0)
        self.assertEqual(api.sent, [(OWNER, "늦게 받은 명령은 건너뛰었어요.")])

    def test_batch_limit_caps_execution_and_notifies_once(self):
        updates = [msg("/오늘", uid=i) for i in range(1, commands.MAX_PER_BATCH + 3)]
        with mock.patch.dict(commands.REGISTRY, {"오늘": lambda cfg, now: "R"}):
            n, api = self.poll(self.cfg(), updates)
        self.assertEqual(n, commands.MAX_PER_BATCH)
        self.assertEqual(api.sent.count((OWNER, "R")), commands.MAX_PER_BATCH)
        self.assertEqual(sum(1 for c, t in api.sent if "많아" in t), 1)

    def test_hour_limit_blocks_once_reached(self):
        state = State(self.path)
        for _ in range(commands.MAX_PER_HOUR):
            state.record_command(NOW)
        state.save()
        with mock.patch.dict(commands.REGISTRY, {"오늘": lambda cfg, now: "R"}):
            n, api = self.poll(self.cfg(), [msg("/오늘")])
        self.assertEqual(n, 0)
        self.assertEqual([t for _, t in api.sent], [f"요청이 많아 일부 명령은 건너뛰었어요 (최대 {commands.MAX_PER_HOUR}개/시간)."])

    def test_offset_advances_past_message_updates(self):
        self.poll(self.cfg(), [msg("/도움", uid=41)])
        _, api = self.poll(self.cfg(), [])
        self.assertEqual(api.offsets[-1], 42)

    def test_handler_exception_still_gets_a_reply(self):
        with mock.patch.dict(commands.REGISTRY, {"오늘": mock.Mock(side_effect=RuntimeError("boom"))}):
            n, api = self.poll(self.cfg(), [msg("/오늘")])
        self.assertEqual(n, 1)
        self.assertIn("오류", api.sent[0][1])


if __name__ == "__main__":
    unittest.main()
