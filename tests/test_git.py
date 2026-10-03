import _bootstrap  # noqa: F401
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from aide.checks import git_dirty


def make_repo(root: Path, files: int) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for i in range(files):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")
    return root


class GitDirtyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_reports_when_at_or_over_threshold(self):
        repo = make_repo(self.base / "proj", 6)
        f = git_dirty.check([str(repo)], 5, date(2026, 10, 2))
        self.assertEqual(len(f), 1)
        self.assertIn("6개", f[0].title)
        self.assertTrue(f[0].key.endswith(":2026-10-02"))

    def test_silent_under_threshold(self):
        repo = make_repo(self.base / "proj", 3)
        self.assertEqual(git_dirty.check([str(repo)], 5, date(2026, 10, 2)), [])

    def test_non_repo_and_missing_path_are_skipped(self):
        plain = self.base / "plain"
        plain.mkdir()
        out = git_dirty.check([str(plain), str(self.base / "nope")], 1, date(2026, 10, 2))
        self.assertEqual(out, [])

    def test_key_changes_next_day(self):
        repo = make_repo(self.base / "proj", 6)
        a = git_dirty.check([str(repo)], 5, date(2026, 10, 2))[0].key
        b = git_dirty.check([str(repo)], 5, date(2026, 10, 3))[0].key
        self.assertNotEqual(a, b)


class NoLockTests(unittest.TestCase):
    def test_status_does_not_take_the_index_lock(self):
        from unittest import mock

        seen = {}
        real = subprocess.run

        def spy(cmd, *a, **k):
            seen["cmd"] = cmd
            return real(cmd, *a, **k)

        with tempfile.TemporaryDirectory() as d:
            repo = make_repo(Path(d) / "r", 1)
            with mock.patch.object(git_dirty.subprocess, "run", spy):
                git_dirty.count_changes(repo)
        self.assertIn("--no-optional-locks", seen["cmd"])


if __name__ == "__main__":
    unittest.main()
