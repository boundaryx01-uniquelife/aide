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


class ButtonSendTests(unittest.TestCase):
    def test_keyboard_only_on_last_chunk(self):
        from unittest import mock

        calls = []
        text = "\n".join("z" * 100 for _ in range(100))  # forces several chunks
        with mock.patch.object(telegram, "_call", lambda m, t, p, timeout=15: calls.append(p)):
            telegram.send(text, token="t", chat_id="1", buttons=[[("a", "d:abcd1234:ok")]])
        self.assertGreater(len(calls), 1)
        self.assertTrue(all("reply_markup" not in c for c in calls[:-1]))
        self.assertIn("reply_markup", calls[-1])

    def test_get_updates_asks_only_for_callbacks(self):
        from unittest import mock

        seen = {}

        def fake(m, t, p, timeout=15):
            seen.update(p, method=m)
            return []

        with mock.patch.object(telegram, "_call", fake):
            telegram.get_updates("t", 5, 0)
        self.assertEqual(seen["allowed_updates"], '["callback_query"]')
        self.assertEqual(seen["offset"], 5)


if __name__ == "__main__":
    unittest.main()
