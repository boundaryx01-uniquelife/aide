import _bootstrap  # noqa: F401
import unittest
from datetime import datetime

from aide import heartbeat
from aide.checks import notice_pages as np
from aide.config import Config
from aide.state import State

NOW = datetime(2026, 10, 4, 14, 0)
PAGE = "https://example.go.kr/board/list.do"
KW = ["공모", "모집", "연수"]

TABLE = """
<html><body><nav><ul><li><a href="/m">공모</a></li></ul></nav>
<table>
<tr><td>3</td><td><a href="/view?id=3">[공고] 모두의 AI 챌린지 프로그램 모집 3차</a></td><td>2026-09-21~2026-10-12 접수중</td></tr>
<tr><td>2</td><td><a href="javascript:goView(2)">2026년 교원 발명교육 직무연수 안내</a></td><td>2026-10-01 ~ 2026-10-05</td></tr>
<tr><td>1</td><td><a href="/view?id=1">과학영재교육원 수행기관 모집 재공모</a></td><td>2026-09-01~2026-09-30 접수마감</td></tr>
<tr><td>0</td><td><a href="/view?id=0">시설 점검 안내 공지입니다</a></td><td>2026-10-01</td></tr>
</table></body></html>
"""


def fetch_for(html):
    return lambda url: html.encode("utf-8")


class ParseTests(unittest.TestCase):
    def test_deadline(self):
        from datetime import date
        self.assertEqual(np.deadline_of("2026-09-21~2026-10-12 접수중"), date(2026, 10, 12))
        self.assertEqual(np.deadline_of("2026.10.1 ~ 2026. 10. 5"), date(2026, 10, 5))
        self.assertIsNone(np.deadline_of("작성일 2026-10-01"))     # posting date is not a deadline
        self.assertIsNone(np.deadline_of("~2026-13-45"))            # invalid date
        self.assertIsNone(np.deadline_of(""))

    def test_decode_falls_back_to_cp949(self):
        self.assertEqual(np.decode("공모".encode("cp949")), "공모")
        self.assertEqual(np.decode("공모".encode("utf-8")), "공모")

    def test_rows_and_links(self):
        items = np.parse_items(TABLE)
        titles = [t for t, _, _ in items]
        self.assertIn("2026년 교원 발명교육 직무연수 안내", titles)
        row = [r for t, _, r in items if "AI 챌린지" in t][0]
        self.assertIn("2026-10-12", row)
        self.assertNotIn("2026-10-05", row)  # other rows' text must not leak in

    def test_broken_html_and_scripts(self):
        self.assertEqual(np.parse_items("<a href='x'>미완성 <b>공모 링크"), [])
        items = np.parse_items("<script>var a='<a href=1>공모 스크립트 안의 글</a>'</script><a href='/x'>공모 신청 안내 페이지</a>")
        self.assertEqual([t for t, _, _ in items], ["공모 신청 안내 페이지"])


class CheckTests(unittest.TestCase):
    def run_check(self, html=TABLE, now=NOW, **kw):
        return np.check([PAGE], KW, fetch_for(html), now, **kw)

    def test_filtering(self):
        f = {x.title: x for x in self.run_check()}
        titles = " | ".join(f)
        self.assertIn("모두의 AI 챌린지", titles)       # open, keyword
        self.assertIn("직무연수", titles)               # keyword, deadline 10/05
        self.assertNotIn("재공모", titles)              # 접수마감 skipped
        self.assertNotIn("시설 점검", titles)           # no keyword
        self.assertNotIn("메뉴", titles)
        self.assertEqual(len(f), 2)                     # nav "공모" too short -> skipped

    def test_deadline_stage_and_detail(self):
        f = {x.title.split("] ")[-1][:6]: x for x in self.run_check()}
        ch = [x for x in self.run_check() if "챌린지" in x.title][0]
        self.assertEqual(ch.detail, "접수 ~10/12 (D-8)")
        self.assertFalse(ch.title.startswith("마감 임박"))
        ed = [x for x in self.run_check() if "직무연수" in x.title][0]
        self.assertTrue(ed.title.startswith("마감 임박(D-1)"))
        self.assertTrue(ed.key.endswith(":d1"))

    def test_stage_keys_differ_so_reminders_fire_once_each(self):
        k = lambda d: [x.key for x in self.run_check(now=datetime(2026, 10, d, 9)) if "챌린지" in x.title][0]
        self.assertEqual(len({k(4), k(9), k(11)}), 3)         # D-8, D-3, D-1
        self.assertTrue(k(9).endswith(":d3"))
        self.assertTrue(k(11).endswith(":d1"))
        self.assertEqual(k(4), k(5))                            # same stage -> same key
        self.assertFalse([x for x in self.run_check(now=datetime(2026, 10, 13)) if "챌린지" in x.title])  # closed

    def test_links(self):
        f = {x.title: x.url for x in self.run_check()}
        self.assertEqual([u for t, u in f.items() if "챌린지" in t][0], "https://example.go.kr/view?id=3")
        self.assertEqual([u for t, u in f.items() if "직무연수" in t][0], PAGE)  # javascript: -> page url

    def test_hostile_links_never_leak_scheme(self):
        html = '<ul><li><a href="data:text/html,x">공모 악성 링크 시험용 제목</a> 2026-10-03</li></ul>'
        self.assertEqual(self.run_check(html)[0].url, PAGE)

    def test_max_per_page(self):
        rows = "".join(f'<li><a href="/{i}">공모 신청 안내 번호 {i}</a> 2026-10-03</li>' for i in range(9))
        self.assertEqual(len(self.run_check(f"<ul>{rows}</ul>", max_per_page=3)), 3)

    def test_title_is_sanitized(self):
        html = '<ul><li><a href="/x">공모 \x00제목\n' + "가" * 300 + "</a> 2026-10-03</li></ul>"
        t = self.run_check(html)[0].title
        self.assertNotIn("\x00", t)
        self.assertLessEqual(len(t), 120)

    def test_failing_page_does_not_hide_others(self):
        def fetch(url):
            if "bad" in url:
                raise OSError("down")
            return TABLE.encode()
        out = np.check(["https://bad.example/", PAGE], KW, fetch, NOW)
        self.assertEqual(len(out), 2)

    def test_keys_stable_across_runs_and_titles_normalized(self):
        a = self.run_check()
        b = self.run_check()
        self.assertEqual([x.key for x in a], [x.key for x in b])
        self.assertEqual(np._key(PAGE, "공모  신청!"), np._key(PAGE, "공모 신청"))
        self.assertNotEqual(np._key(PAGE, "공모 신청"), np._key("https://other.kr/x", "공모 신청"))


class MoreEdgeTests(unittest.TestCase):
    def test_menu_links_without_a_date_are_ignored(self):
        html = ('<ul><li><a href="/a">제안/공모/정책토론 메뉴입니다</a></li>'
                '<li><a href="/b">프로그램운영신청/자료집계</a></li>'
                '<li><a href="/c">[대회] 작품제출 바로가기</a></li></ul>'
                '<table><tr><td><a href="/d">발명 연수 공모 안내 글</a></td><td>2026.10.01</td></tr></table>')
        got = np.check([PAGE], KW + ["대회", "신청"], lambda u: html.encode(), NOW)
        self.assertEqual([x.title for x in got], ["발명 연수 공모 안내 글"])

    def test_too_short_title_ignored_even_with_date(self):
        html = '<table><tr><td><a href="/a">공모 안내</a></td><td>2026.10.01</td></tr></table>'
        self.assertEqual(np.check([PAGE], KW, lambda u: html.encode(), NOW), [])

    def test_date_inside_the_title_alone_does_not_count(self):
        html = '<ul><li><a href="/a">2026-10-03 공모 메뉴 링크 텍스트</a></li></ul>'
        self.assertEqual(np.check([PAGE], KW, lambda u: html.encode(), NOW), [])

    def test_short_date_formats_count(self):
        for d in ("10.01", "10/1", "2026-10-01"):
            html = f'<table><tr><td><a href="/a">발명 연수 공모 안내 글</a></td><td>{d}</td></tr></table>'
            self.assertEqual(len(np.check([PAGE], KW, lambda u: html.encode(), NOW)), 1, d)

    def one(self, html, now=NOW):
        return np.check([PAGE], KW, lambda u: html.encode(), now)

    def test_closed_badge_skipped_even_with_future_date(self):
        html = '<table><tr><td><a href="/a">공모 사업 안내 공고문</a></td><td>~2026-12-31 접수마감</td></tr></table>'
        self.assertEqual(self.one(html), [])

    def test_deadline_today_still_reported(self):
        html = '<table><tr><td><a href="/a">공모 사업 안내 공고문</a></td><td>~2026-10-04</td></tr></table>'
        f = self.one(html)
        self.assertEqual(len(f), 1)
        self.assertIn("D-0", f[0].detail)
        self.assertTrue(f[0].key.endswith(":d1"))

    def test_far_deadline_has_no_stage(self):
        html = '<table><tr><td><a href="/a">공모 사업 안내 공고문</a></td><td>~2026-10-08</td></tr></table>'  # D-4
        self.assertFalse(":d" in self.one(html)[0].key)
        html = html.replace("10-08", "10-07")  # D-3
        self.assertTrue(self.one(html)[0].key.endswith(":d3"))

    def test_latest_end_date_wins(self):
        html = '<table><tr><td><a href="/a">공모 사업 안내 공고문</a></td><td>1차 ~2026-10-05 / 2차 ~2026-10-20</td></tr></table>'
        self.assertEqual(self.one(html)[0].detail, "접수 ~10/20 (D-16)")

    def test_nested_list_in_row_keeps_outer_text(self):
        html = '<table><tr><td><ul><li><a href="/a">공모 사업 안내 공고문</a></li></ul></td><td>~2026-10-20</td></tr></table>'
        self.assertEqual(self.one(html)[0].detail, "접수 ~10/20 (D-16)")


class IntegrationTests(unittest.TestCase):
    def test_collect_and_digest(self):
        cfg = Config(notice_pages=[PAGE], notice_keywords=KW)
        fetch = lambda url: TABLE.encode("utf-8")
        fs = [f for f in heartbeat.collect(cfg, NOW, fetch) if f.source == "notice"]
        self.assertEqual(len(fs), 2)
        text = heartbeat.format_digest(fs, NOW, 10)
        self.assertIn("기관 공지·마감", text)
        self.assertIn("접수 ~10/12", text)

    def test_empty_by_default(self):
        self.assertEqual([f for f in heartbeat.collect(Config(), NOW, lambda u: b"") if f.source == "notice"], [])


class CliTests(unittest.TestCase):
    def test_notices_command_prints_findings(self):
        import io, json, tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from unittest import mock
        from aide import __main__ as cli
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "c.json"
            cfgp.write_text(json.dumps({"notice_keywords": ["모집", "연수"]}), encoding="utf-8")
            buf = io.StringIO()
            with mock.patch("aide.checks.news_keywords.fetch_feed", lambda u: TABLE.encode()), redirect_stdout(buf):
                rc = cli.main(["notices", PAGE, "--config", str(cfgp)])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("직무연수", out)
        self.assertIn("선택", out)

    def test_http_error_code_is_shown(self):
        import io, json, tempfile, urllib.error
        from contextlib import redirect_stdout
        from pathlib import Path
        from unittest import mock
        from aide import __main__ as cli
        from aide.checks import news_keywords as nk

        def boom(*a, **k):
            raise urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "c.json"
            cfgp.write_text("{}", encoding="utf-8")
            buf = io.StringIO()
            with mock.patch("urllib.request.urlopen", boom), redirect_stdout(buf):
                cli.main(["notices", "https://example.go.kr/x", "--config", str(cfgp)])
        self.assertIn("HTTP 403", buf.getvalue())

    def test_notices_without_pages_is_setup_error(self):
        import io, json, tempfile
        from contextlib import redirect_stdout
        from pathlib import Path
        from aide import __main__ as cli
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "c.json"
            cfgp.write_text("{}", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["notices", "--config", str(cfgp)]), 2)


if __name__ == "__main__":
    unittest.main()
