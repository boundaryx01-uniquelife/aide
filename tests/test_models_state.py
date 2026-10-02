import _bootstrap  # noqa: F401
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from aide.models import clean
from aide.state import State


class CleanTests(unittest.TestCase):
    def test_strips_control_chars_and_collapses_space(self):
        self.assertEqual(clean("a\x00b\x1b[31m  c\n\td"), "ab[31m c d")

    def test_truncates(self):
        out = clean("x" * 500, 50)
        self.assertEqual(len(out), 50)
        self.assertTrue(out.endswith("…"))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "sub" / "state.json"
        self.now = datetime(2026, 10, 2, 12, 0)

    def tearDown(self):
        self.dir.cleanup()

    def test_roundtrip(self):
        s = State(self.path)
        s.mark("k1", self.now)
        s.save()
        self.assertTrue(State(self.path).is_seen("k1"))
        self.assertFalse(State(self.path).is_seen("k2"))

    def test_corrupt_file_is_set_aside_not_crash(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        s = State(self.path)
        self.assertEqual(s.seen, {})
        self.assertTrue(any(p.name.startswith("state.json.corrupt-") for p in self.path.parent.iterdir()))

    def test_prune_old_and_bad_entries(self):
        s = State(self.path)
        s.mark("old", self.now - timedelta(days=60))
        s.mark("new", self.now - timedelta(days=1))
        s.seen["bad"] = "garbage"
        removed = s.prune(self.now, days=45)
        self.assertEqual(removed, 2)
        self.assertEqual(set(s.seen), {"new"})

    def test_save_leaves_no_tmp_file(self):
        s = State(self.path)
        s.mark("k", self.now)
        s.save()
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["state.json"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["seen"].keys(), {"k"})


if __name__ == "__main__":
    unittest.main()
