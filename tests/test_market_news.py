import _bootstrap  # noqa: F401
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

from aide import heartbeat
from aide.checks import market_news as mn
from aide.checks import news_keywords as nk
from aide.config import Config, ConfigError, load_config
from aide.models import Finding

MON = datetime(2026, 11, 2, 10, 7)        # a Monday, local time
UTC_NOW = MON.astimezone(timezone.utc)


def item(title, link, pub=None, summary="", guid=""):
    date = f"<pubDate>{format_datetime(pub)}</pubDate>" if pub else ""
    return (f"<item><title>{title}</title><link>{link}</link><guid>{guid or link}</guid>{date}"
            f"<description>{summary}</description></item>")


def rss(*items):
    return ("<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>" + "".join(items)
            + "</channel></rss>").encode("utf-8")


def minutes_ago(m):
    return UTC_NOW - timedelta(minutes=m)


class HoursAndUrlTests(unittest.TestCase):
    def test_market_hours_are_weekdays_9_to_16(self):
        at = lambda d, h, m: datetime(2026, 11, d, h, m)    # 11/2 Mon ... 11/7 Sat
        self.assertTrue(mn.in_market_hours(at(2, 9, 0)))
        self.assertTrue(mn.in_market_hours(at(6, 15, 37)))
        self.assertTrue(mn.in_market_hours(at(2, 16, 0)))
        self.assertFalse(mn.in_market_hours(at(2, 8, 59)))
        self.assertFalse(mn.in_market_hours(at(2, 16, 1)))
        self.assertFalse(mn.in_market_hours(at(7, 10, 0)))      # Saturday
        self.assertFalse(mn.in_market_hours(at(8, 10, 0)))      # Sunday

    def test_feed_url_is_https_google_news_last_day(self):
        url = mn.feed_url("특징주")
        self.assertTrue(url.startswith("https://news.google.com/rss/search?q="))
        self.assertIn("when%3A1d", url)
        self.assertNotIn("특징주", url)       # percent-encoded
        self.assertIn("hl=ko", url)


class StockNameTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(mn.stock_name("[특징주] 한국전력, 실적 호조에 19% 급등"), "한국전력")
        self.assertEqual(mn.stock_name("[ET특징주] 삼성전자·SK하이닉스, 반도체 강세"), "삼성전자·SK하이닉스")
        self.assertEqual(mn.stock_name("[홍길동 특징주] 현대차, 신차 효과"), "현대차")
        self.assertEqual(mn.stock_name("코스피 5000 돌파 [시황]"), "")
        self.assertEqual(mn.stock_name("[특징주] 쉼표가 없는 제목"), "")
        self.assertEqual(mn.stock_name(""), "")


class CheckTests(unittest.TestCase):
    def run_check(self, feeds, queries=("특징주",), exclude=(), max_age=3, now=MON):
        calls = []

        def fetch(url):
            calls.append(url)
            return feeds

        return mn.check(list(queries), exclude, fetch, now=now, max_age_hours=max_age), calls

    def test_same_stock_from_several_outlets_is_one_line_with_a_count(self):
        data = rss(
            item("[특징주] 한국전력, 실적 호조에 19% '급등' - 서울경제", "https://x/1", minutes_ago(50)),
            item("[특징주] 한국전력, 호실적에 상한가 근접 - 이데일리", "https://x/2", minutes_ago(10)),
            item("[특징주] 한국전력, 외국인 순매수 - 머니투데이", "https://x/3", minutes_ago(30)),
            item("[특징주] 삼성전자, 반도체 강세 - 연합뉴스", "https://x/4", minutes_ago(20)),
        )
        found, _ = self.run_check(data)
        self.assertEqual(len(found), 2)
        kepco = next(f for f in found if "한국전력" in f.title)
        self.assertEqual(kepco.title, "한국전력 · 호실적에 상한가 근접 (기사 3건)")   # newest headline, outlet stripped
        self.assertEqual(kepco.url, "https://x/2")
        self.assertEqual(kepco.source, "market")
        self.assertEqual(kepco.key, "market:2026-11-02:name:한국전력")
        self.assertEqual([f.title.split(" · ")[0] for f in found], ["한국전력", "삼성전자"])   # newest group first

    def test_single_article_has_no_count_and_headline_without_name_stays_whole(self):
        data = rss(item("[특징주] 현대차, 신차 효과 - 한국경제", "https://x/1", minutes_ago(5)),
                   item("코스피 5000 돌파 [시황] - 아시아경제", "https://x/2", minutes_ago(6)))
        found, _ = self.run_check(data, queries=("특징주", "시황"))
        titles = [f.title for f in found]
        self.assertIn("현대차 · 신차 효과", titles)
        self.assertIn("코스피 5000 돌파 [시황]", titles)

    def test_headline_without_a_stock_is_deduplicated_across_outlets(self):
        data = rss(item("상한가 7개 종목 속출 - A신문", "https://x/1", minutes_ago(5)),
                   item("상한가 7개 종목 속출 - B신문", "https://x/2", minutes_ago(6)))
        found, _ = self.run_check(data, queries=("상한가",))
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].title.endswith("(기사 2건)"))

    def test_excluded_words_drop_the_article_wherever_they_appear(self):
        data = rss(item("[특징주] 가나다, 무료 추천 리딩방 홍보", "https://x/1", minutes_ago(5)),
                   item("[특징주] 라마바, 정상 기사", "https://x/2", minutes_ago(5), summary="내용에 카톡방 안내"),
                   item("[특징주] 사아자, 정상 기사", "https://x/3", minutes_ago(5)))
        found, _ = self.run_check(data, exclude=("리딩방", "카톡방"))
        self.assertEqual([f.title.split(" · ")[0] for f in found], ["사아자"])

    def test_old_articles_are_dropped_undated_are_kept(self):
        data = rss(item("[특징주] 오래된, 어제 기사", "https://x/1", minutes_ago(300)),
                   item("[특징주] 최근, 방금 기사", "https://x/2", minutes_ago(30)),
                   item("[특징주] 날짜없음, 날짜 모름", "https://x/3"))
        found, _ = self.run_check(data, max_age=3)
        self.assertEqual(sorted(f.title.split(" · ")[0] for f in found), ["날짜없음", "최근"])

    def test_the_query_word_must_be_in_the_article(self):
        data = rss(item("[특징주] 가나다, 급등", "https://x/1", minutes_ago(5)),
                   item("관련 없는 제목", "https://x/2", minutes_ago(5), summary="본문 없음"))
        found, _ = self.run_check(data, queries=("특징주",))
        self.assertEqual(len(found), 1)

    def test_a_new_day_gets_new_keys_so_a_stock_is_reported_once_per_day(self):
        data = rss(item("[특징주] 한국전력, 급등", "https://x/1"))          # undated, so the next day's age limit cannot hide it
        today, _ = self.run_check(data)
        tomorrow, _ = self.run_check(data, now=MON + timedelta(days=1))
        self.assertNotEqual(today[0].key, tomorrow[0].key)

    def test_broken_feed_is_skipped_and_no_queries_means_nothing(self):
        def boom(url):
            raise nk.FeedError("x")
        self.assertEqual(mn.check(["특징주"], [], boom, now=MON), [])
        self.assertEqual(mn.check([], [], boom, now=MON), [])
        self.assertEqual(mn.check(["", None], [], boom, now=MON), [])

    def test_untrusted_text_is_cleaned_and_capped(self):
        data = rss(item("[특징주] 가나다, " + "가" * 300, "https://x/1", minutes_ago(5)),
                   item("[특징주] " + "나" * 100 + ", 이름이 너무 김", "https://x/2", minutes_ago(5)))
        found, _ = self.run_check(data)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(len(f.title) <= 90 for f in found))
        self.assertEqual(sum(1 for f in found if " · " in f.title), 1)      # only the real name is split off

    def test_each_query_is_fetched_once_and_at_most_ten(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return rss()
        mn.check([f"q{i}" for i in range(25)], [], fetch, now=MON)
        self.assertEqual(len(calls), mn.MAX_QUERIES)
        self.assertTrue(all(u.startswith("https://") for u in calls))


class CollectTests(unittest.TestCase):
    """News hours and the market window inside heartbeat.collect()."""

    def cfg(self, **kw):
        base = dict(news_feeds=["http://general"], news_keywords=["키워드"], market_queries=["특징주"])
        base.update(kw)
        return Config(**base)

    @staticmethod
    def fetch(url):
        if url == "http://general":
            return rss(item("키워드 일반 기사", "https://g/1"))
        return rss(item("[특징주] 한국전력, 급등", "https://m/1"))      # undated: never dropped for age

    def sources(self, cfg, now, **kw):
        return sorted({f.source for f in heartbeat.collect(cfg, now, self.fetch, **kw)})

    def test_default_checks_general_news_every_time_and_market_only_in_market_hours(self):
        self.assertEqual(self.sources(self.cfg(), MON), ["market", "news"])
        self.assertEqual(self.sources(self.cfg(), datetime(2026, 11, 2, 18, 7)), ["news"])
        self.assertEqual(self.sources(self.cfg(), datetime(2026, 11, 7, 10, 7)), ["news"])          # Saturday

    def test_news_hours_limit_general_news_but_not_the_market_lines(self):
        cfg = self.cfg(news_hours=[7, 12])
        self.assertEqual(self.sources(cfg, MON), ["market"])                                        # 10:07
        self.assertEqual(self.sources(cfg, datetime(2026, 11, 2, 12, 7)), ["market", "news"])
        self.assertEqual(self.sources(cfg, datetime(2026, 11, 2, 7, 37)), ["news"])                 # before the open

    def test_market_is_off_without_queries(self):
        self.assertEqual(self.sources(self.cfg(market_queries=[]), MON), ["news"])

    def test_preview_ignores_both_gates(self):
        cfg = self.cfg(news_hours=[7, 12])
        sat = datetime(2026, 11, 7, 22, 0)
        self.assertEqual(self.sources(cfg, sat), [])
        self.assertEqual(self.sources(cfg, sat, preview=True), ["market", "news"])


class DigestAndRunTests(unittest.TestCase):
    def test_market_section_comes_first_with_its_own_heading(self):
        fs = [Finding(key="n", source="news", title="일반"), Finding(key="m", source="market", title="한국전력 · 급등"),
              Finding(key="g", source="mail", title="메일")]
        text = heartbeat.format_digest(fs, MON, 10)
        self.assertLess(text.index("■ 장중 특징주"), text.index("■ 메일"))
        self.assertLess(text.index("■ 메일"), text.index("■ 뉴스 키워드"))
        self.assertIn("• 한국전력 · 급등", text)

    def test_market_lines_are_sent_once_and_never_to_the_llm(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(market_queries=["특징주"], state_path=str(Path(d) / "s.json"), llm_enabled=True)
            sent = []
            fetch = lambda url: rss(item("[특징주] 한국전력, 급등", "https://m/1", minutes_ago(5)))
            env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
            with mock.patch.dict("os.environ", env), mock.patch("aide.llm.rank", return_value=None) as rank:
                for _ in range(2):
                    heartbeat.run(cfg, send=True, now=MON, fetch=fetch,
                                  sender=lambda m, **k: sent.append(m))
            self.assertEqual(len(sent), 1)                           # the second check finds nothing new
            self.assertIn("한국전력", sent[0])
            ranked = rank.call_args.args[0] if rank.call_args else []
            self.assertFalse(any(f.source == "market" for f in ranked) and cfg.llm_sources.count("market"))
            self.assertNotIn("market", cfg.llm_sources)


class ConfigTests(unittest.TestCase):
    def write(self, data):
        p = Path(tempfile.mkdtemp()) / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_defaults_keep_everything_as_before(self):
        cfg = Config()
        self.assertEqual((cfg.news_hours, cfg.market_queries, cfg.market_max_age_hours), ([], [], 3))
        self.assertIn("리딩방", cfg.market_exclude)

    def test_valid_values(self):
        cfg = load_config(self.write({"news_hours": [7, 12], "market_queries": ["특징주", "상한가"]}))
        self.assertEqual((cfg.news_hours, cfg.market_queries), ([7, 12], ["특징주", "상한가"]))

    def test_rejects_bad_values(self):
        for data in ({"news_hours": [24]}, {"news_hours": [7, 7]}, {"news_hours": ["7"]}, {"news_hours": [True]},
                     {"news_hours": 7}, {"market_queries": "특징주"}, {"market_queries": [""]},
                     {"market_queries": ["가" * 31]}, {"market_queries": [f"q{i}" for i in range(11)]},
                     {"market_exclude": [1]}, {"market_max_age_hours": 0}, {"market_max_age_hours": 25},
                     {"market_max_age_hours": True}):
            with self.assertRaises(ConfigError, msg=str(data)):
                load_config(self.write(data))


if __name__ == "__main__":
    unittest.main()
