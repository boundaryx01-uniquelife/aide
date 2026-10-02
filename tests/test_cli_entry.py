import _bootstrap  # noqa: F401
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def run_cli(*args, cwd=None):
    # Empty strings (not "unset"): load_dotenv never overrides an existing variable, so the
    # real project .env (with a real token) can never leak into this test.
    env = dict(os.environ, PYTHONPATH=SRC, PYTHONUTF8="1", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
    return subprocess.run(
        [sys.executable, "-m", "aide", *args], capture_output=True, text=True, encoding="utf-8", env=env, cwd=cwd, timeout=60
    )


class EntryPointTests(unittest.TestCase):
    """`python -m aide` is what the scheduler really runs; calling functions directly
    in other tests cannot notice a missing `if __name__ == "__main__"` block."""

    def test_version_prints_and_exits_zero(self):
        p = run_cli("--version")
        self.assertEqual(p.returncode, 0)
        self.assertIn("0.1.0", p.stdout + p.stderr)

    def test_missing_subcommand_is_an_error_not_silence(self):
        p = run_cli()
        self.assertNotEqual(p.returncode, 0)

    def test_poll_without_credentials_reports_and_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text("{}", encoding="utf-8")
            p = run_cli("poll", "--config", str(cfg), cwd=d)
        self.assertEqual(p.returncode, 2)
        self.assertIn("TELEGRAM_BOT_TOKEN", p.stderr)


if __name__ == "__main__":
    unittest.main()
