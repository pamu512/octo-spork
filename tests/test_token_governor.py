"""Tests for ``claude_bridge.token_governor``."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claude_bridge import token_governor as tg  # noqa: E402


class GatherTests(unittest.TestCase):
    def test_gather_p_and_append(self) -> None:
        p = tg.gather_estimation_payload(["-p", "hello", "--append-system-prompt", "sys"])
        self.assertIn("hello", p)
        self.assertIn("sys", p)


class EstimateTests(unittest.TestCase):
    def test_python_mirror_matches_chars_div_4(self) -> None:
        self.assertEqual(tg.estimate_tokens_python("abcd"), 1)
        self.assertEqual(tg.estimate_tokens_python("abcde"), 2)


class GovernorMainTests(unittest.TestCase):
    def test_abort_when_over_budget(self) -> None:
        argv = ["--budget", "5", "--", "-p", "x" * 400]
        with mock.patch.object(tg.os, "execvp") as ex:
            with mock.patch("builtins.input", return_value="a"):
                rc = tg.main(argv)
        self.assertEqual(rc, 1)
        self.assertFalse(ex.called)

    def test_yes_proceeds_to_execvp(self) -> None:
        argv = ["--yes", "--budget", "10", "--", "-p", "x" * 400]
        with mock.patch.object(tg.os, "execvp", side_effect=OSError("stub")) as ex:
            with self.assertRaises(OSError):
                tg.main(argv)
        self.assertTrue(ex.called)


if __name__ == "__main__":
    unittest.main()
