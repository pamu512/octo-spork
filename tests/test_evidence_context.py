"""Tests for ``claude_bridge.evidence_context``."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claude_bridge import evidence_context as ec  # noqa: E402


class PytestLogsTests(unittest.TestCase):
    def test_lists_newest_logs(self) -> None:
        import os

        with tempfile_dir() as td:
            root = Path(td)
            d = root / ".octo" / "evidence" / "pytest_failures"
            d.mkdir(parents=True)
            old_p = d / "old.log"
            new_p = d / "new.log"
            old_p.write_text("old", encoding="utf-8")
            new_p.write_text("new", encoding="utf-8")
            os.utime(old_p, (1, 100))
            os.utime(new_p, (1, 200))
            logs = ec._sorted_failure_logs(root)
            self.assertEqual([p.name for p in logs], ["new.log", "old.log"])


class BuildMarkdownTests(unittest.TestCase):
    def test_includes_failure_logs_and_skips_ruff_when_missing(self) -> None:
        with tempfile_dir() as td:
            root = Path(td)
            d = root / ".octo" / "evidence" / "pytest_failures"
            d.mkdir(parents=True)
            (d / "one.log").write_text("FAILED test_x\nAssertionError\n", encoding="utf-8")
            with mock.patch("claude_bridge.evidence_context.subprocess.run") as run:
                run.side_effect = FileNotFoundError()
                md = ec.build_grounded_evidence_markdown(root, pytest_tail=3, ruff_top=5)
            self.assertIn("Grounded Evidence", md)
            self.assertIn("FAILED test_x", md)
            self.assertIn("Ruff not installed", md)

    def test_ruff_json_table(self) -> None:
        sample = [
            {
                "code": "F401",
                "message": "unused",
                "severity": "warning",
                "filename": "a.py",
                "location": {"row": 1, "column": 1},
            },
            {
                "code": "E999",
                "message": "syntax",
                "severity": "error",
                "filename": "b.py",
                "location": {"row": 2, "column": 3},
            },
        ]
        with tempfile_dir() as td:
            root = Path(td)
            with mock.patch("claude_bridge.evidence_context.subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout=json.dumps(sample))
                md = ec.build_grounded_evidence_markdown(root, pytest_tail=0, ruff_top=5)
            self.assertIn("E999", md)
            self.assertIn("b.py", md)


def tempfile_dir():
    import tempfile

    return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
