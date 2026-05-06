"""Tests for PR failure developer notes."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from github_bot.developer_note import (  # noqa: E402
    parse_developer_note_issue,
    report_pr_processing_failure,
)


class DeveloperNoteParsingTests(unittest.TestCase):
    def test_parse_issue_spec(self) -> None:
        self.assertEqual(
            parse_developer_note_issue("acme/widget#42"),
            ("acme", "widget", 42),
        )
        self.assertIsNone(parse_developer_note_issue("bad"))
        self.assertIsNone(parse_developer_note_issue(""))


class DeveloperNoteReportingTests(unittest.TestCase):
    def test_writes_failure_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "failures.log"
            env = {"OCTO_SPORK_FAILURE_LOG": str(log)}
            with patch.dict(os.environ, env, clear=False):
                report_pr_processing_failure(
                    {"delivery": "d1", "trigger": "pull_request", "event": "pull_request"},
                    ValueError("boom"),
                    "Traceback (most recent call last):\n  ValueError: boom\n",
                )
            text = log.read_text(encoding="utf-8")
            self.assertIn("Developer Note", text)
            self.assertIn("ValueError: boom", text)
            self.assertIn("d1", text)


if __name__ == "__main__":
    unittest.main()
