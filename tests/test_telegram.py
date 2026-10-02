import _bootstrap  # noqa: F401
import unittest

from aide import telegram


class SplitTests(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(telegram.split_message("hello"), ["hello"])

    def test_long_text_split_on_lines_within_limit(self):
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
        chunks = telegram.split_message(text, limit=500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("\n".join(chunks), text)

    def test_single_giant_line_is_cut(self):
        chunks = telegram.split_message("y" * 1000, limit=300)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        self.assertEqual("".join(chunks), "y" * 1000)

    def test_send_requires_credentials(self):
        with self.assertRaises(telegram.TelegramError):
            telegram.send("hi", token="", chat_id="")


if __name__ == "__main__":
    unittest.main()
