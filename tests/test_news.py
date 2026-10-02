import _bootstrap  # noqa: F401
import unittest

from aide.checks import news_keywords as nk

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>발명교육 센터, 신규 프로그램 운영</title><link>https://ex.com/a</link><guid>g-1</guid>
<description>&lt;b&gt;초등&lt;/b&gt; 학생 대상</description></item>
<item><title>오늘의 날씨</title><link>https://ex.com/b</link><guid>g-2</guid><description>맑음</description></item>
<item><title>3D프린터 활용 수업 사례</title><link>https://ex.com/c</link><description>메이커교육 현장</description></item>
</channel></rss>""".encode("utf-8")

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>f</title>
<entry><title>Maker education news</title><link href="https://ex.com/atom1"/><id>atom-1</id><summary>maker</summary></entry>
</feed>""".encode("utf-8")

EVIL = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'


class ParseTests(unittest.TestCase):
    def test_rss_items(self):
        items = nk.parse_feed(RSS)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].guid, "g-1")
        self.assertNotIn("<b>", items[0].summary)  # html stripped

    def test_atom_items(self):
        items = nk.parse_feed(ATOM)
        self.assertEqual(items[0].link, "https://ex.com/atom1")
        self.assertEqual(items[0].guid, "atom-1")

    def test_doctype_entity_rejected(self):
        with self.assertRaises(nk.FeedError):
            nk.parse_feed(EVIL)

    def test_garbage_rejected(self):
        with self.assertRaises(nk.FeedError):
            nk.parse_feed(b"<rss><oops>")

    def test_non_http_url_rejected(self):
        with self.assertRaises(nk.FeedError):
            nk.fetch_feed("file:///etc/passwd")


class CheckTests(unittest.TestCase):
    def test_matches_title_or_summary_case_insensitive(self):
        f = nk.check(["u"], ["발명교육", "메이커교육", "MAKER"], lambda url: RSS)
        titles = [x.title for x in f]
        self.assertEqual(len(f), 2)  # item 1 (title) and item 3 (summary), not the weather one
        self.assertIn("발명교육 센터, 신규 프로그램 운영", titles)
        self.assertNotIn("오늘의 날씨", titles)

    def test_key_prefers_guid_then_link(self):
        f = nk.check(["u"], ["발명교육", "3D프린터"], lambda url: RSS)
        self.assertEqual({x.key for x in f}, {"news:g-1", "news:https://ex.com/c"})

    def test_no_keywords_no_findings(self):
        self.assertEqual(nk.check(["u"], [], lambda url: RSS), [])

    def test_bad_feed_is_skipped_others_continue(self):
        def fetch(url):
            if url == "bad":
                raise nk.FeedError("boom")
            return RSS

        f = nk.check(["bad", "good"], ["발명교육"], fetch)
        self.assertEqual(len(f), 1)


if __name__ == "__main__":
    unittest.main()
