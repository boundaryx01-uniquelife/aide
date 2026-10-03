import _bootstrap  # noqa: F401
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

from aide import heartbeat
from aide.checks import news_keywords as nk
from aide.config import Config, ConfigError, load_config
from aide.state import State

UTC = timezone.utc
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}


def rfc(dt):
    return format_datetime(dt.astimezone(UTC), usegmt=True)


def rss(*items):
    """items: (title, guid, published datetime or None)"""
    parts = []
    for title, guid, when in items:
        date = f"<pubDate>{rfc(when)}</pubDate>" if when else ""
        parts.append(f"<item><title>{title}</title><guid>{guid}</guid><link>http://x/{guid}</link>{date}</item>")
    return ("<rss><channel>" + "".join(parts) + "</channel></rss>").encode("utf-8")


def ago(hours):
    return NOW - timedelta(hours=hours)


class DateTests(unittest.TestCase):
    def test_rfc822_and_iso_and_garbage(self):
        self.assertEqual(nk.parse_date("Fri, 03 Oct 2026 01:00:00 GMT"), datetime(2026, 10, 3, 1, 0, tzinfo=UTC))
        self.assertEqual(nk.parse_date("2026-10-03T01:00:00Z"), datetime(2026, 10, 3, 1, 0, tzinfo=UTC))
        self.assertEqual(nk.parse_date("2026-10-03T10:00:00+09:00"), datetime(2026, 10, 3, 1, 0, tzinfo=UTC))
        self.assertIsNone(nk.parse_date("어제쯤"))
        self.assertIsNone(nk.parse_date(""))


class TitleTests(unittest.TestCase):
    def test_same_headline_from_two_outlets_is_equal(self):
        a = nk.normalize_title("발명교육개발원 공식 출범 - 정안뉴스")
        b = nk.normalize_title("발명교육개발원, 공식 출범! - 화성신문")
        self.assertEqual(a, b)

    def test_different_headlines_differ(self):
        self.assertNotEqual(nk.normalize_title("발명교육 지원 확대 - A"), nk.normalize_title("발명교육 센터 개소 - A"))

    def test_long_tail_is_not_mistaken_for_an_outlet(self):
        t = "AI 시대 - " + "가" * 40
        self.assertIn("가" * 40, nk.normalize_title(t))


class FilterTests(unittest.TestCase):
    def run_check(self, data, **kw):
        kw.setdefault("now", NOW)
        kw.setdefault("max_age_hours", 36)
        return nk.check(["u"], ["발명"], fetch=lambda u: data, **kw)

    def test_old_articles_are_dropped_recent_kept(self):
        out = self.run_check(rss(("발명 새 소식", "new", ago(10)), ("발명 옛 소식", "old", ago(50))))
        self.assertEqual([f.key for f in out], ["news:new"])

    def test_boundary(self):
        out = self.run_check(rss(("발명 A", "a", ago(35.9)), ("발명 B", "b", ago(36.1))))
        self.assertEqual([f.key for f in out], ["news:a"])

    def test_undated_and_future_dated_are_kept(self):
        out = self.run_check(rss(("발명 없음", "u1", None), ("발명 미래", "u2", NOW + timedelta(hours=3))))
        self.assertEqual(len(out), 2)

    def test_no_limit_without_now(self):
        out = nk.check(["u"], ["발명"], fetch=lambda u: rss(("발명 옛", "o", ago(999))))
        self.assertEqual(len(out), 1)

    def test_newest_first_undated_last(self):
        out = self.run_check(rss(("발명 가", "g", ago(20)), ("발명 나", "n", None), ("발명 다", "d", ago(2))))
        self.assertEqual([f.key for f in out], ["news:d", "news:g", "news:n"])

    def test_same_headline_other_outlet_reported_once(self):
        out = self.run_check(rss(("발명 대회 개최 - A일보", "g1", ago(5)), ("발명 대회 개최 - B뉴스", "g2", ago(4))))
        self.assertEqual(len(out), 1)

    def test_same_guid_twice_reported_once(self):
        out = self.run_check(rss(("발명 하나", "same", ago(5)), ("발명 둘 다른 제목", "same", ago(4))))
        self.assertEqual(len(out), 1)

    def test_alt_key_carries_normalized_headline(self):
        out = self.run_check(rss(("발명 대회 - A", "g", ago(1))))
        self.assertEqual(out[0].alt_keys, ("newstitle:" + nk.normalize_title("발명 대회 - A"),))


@unittest.skipUnless(hasattr(__import__("time"), "tzset"), "needs time.tzset (not on Windows)")
class LocalTimeTests(unittest.TestCase):
    """heartbeat passes a NAIVE local `now`; it must be read as local time, not as UTC."""

    def setUp(self):
        import time
        self.old = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Seoul"
        time.tzset()
        self.addCleanup(self.restore)

    def restore(self):
        import time
        if self.old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old
        time.tzset()

    def test_naive_local_now_is_converted_not_relabelled(self):
        naive_local_now = datetime.fromtimestamp(NOW.timestamp())  # same instant, Seoul wall clock
        data = rss(("발명 삼십시간 전", "a", ago(30)), ("발명 마흔시간 전", "b", ago(40)))
        out = nk.check(["u"], ["발명"], fetch=lambda u: data, now=naive_local_now, max_age_hours=36)
        self.assertEqual([f.key for f in out], ["news:a"])


class HeartbeatDedupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"
        self.cfg = Config(news_feeds=["u"], news_keywords=["발명"], state_path=str(self.path))
        self.now = datetime(2026, 10, 3, 12, 0)  # naive local, outside quiet hours
        self.sent = []
        self.feed = b""

    def tearDown(self):
        self.dir.cleanup()

    def fresh(self, hours=1):
        return self.now.astimezone(UTC) - timedelta(hours=hours)

    def go(self):
        def sender(text, *, token, chat_id, buttons=None):
            self.sent.append(text)

        with mock.patch.dict(os.environ, ENV):
            return heartbeat.run(self.cfg, send=True, now=self.now, sender=sender, fetch=lambda u: self.feed)

    def test_repost_by_another_outlet_is_not_sent_later(self):
        self.feed = rss(("발명 대회 개최 - A", "g1", self.fresh()))
        self.go()
        self.feed = rss(("발명 대회 개최 - B", "g2", self.fresh()))  # different id, same headline
        self.go()
        self.assertEqual(len(self.sent), 1)

    def test_legacy_state_with_only_the_old_key_still_skips(self):
        st = State(self.path)
        st.mark("news:g1", self.now)  # as written by versions before alt keys existed
        st.save()
        self.feed = rss(("발명 대회 개최 - A", "g1", self.fresh()))
        self.go()
        self.assertEqual(self.sent, [])

    def test_later_button_forgets_headline_too(self):
        self.feed = rss(("발명 대회 개최 - A", "g1", self.fresh()))
        self.go()
        st = State(self.path)
        did = next(iter(st.digests))
        self.assertEqual(st.resolve_digest(did, "later"), "later")
        st.save()
        self.go()  # reported again
        self.assertEqual(len(self.sent), 2)


class ConfigTests(unittest.TestCase):
    def test_default_and_validation(self):
        self.assertEqual(Config().news_max_age_hours, 36)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text('{"news_max_age_hours": 0}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)


if __name__ == "__main__":
    unittest.main()
