"""Tests for Correction Ledger (negative examples)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class CorrectionLedgerUnitTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_CORRECTION_LEDGER",
            "OLLAMA_BASE_URL",
            "OCTO_CHROMA_DATA_DIR",
            "OCTO_EMBEDDING_MODEL",
        ):
            os.environ.pop(k, None)

    def test_format_lessons_learned_section(self) -> None:
        from github_bot.correction_ledger import format_lessons_learned_section

        md = format_lessons_learned_section(
            [
                {
                    "rejected_preview": "Use eval for flexibility",
                    "corrected_preview": "Use a safe parser",
                    "repo_full": "acme/api",
                }
            ]
        )
        self.assertIn("## Lessons Learned", md)
        self.assertIn("Avoid **", md)
        self.assertIn("corrected it to **", md)
        self.assertIn("acme/api", md)

    def test_correction_ledger_enabled(self) -> None:
        from github_bot import correction_ledger as cl

        self.assertFalse(cl.correction_ledger_enabled())
        os.environ["OCTO_CORRECTION_LEDGER"] = "1"
        self.assertTrue(cl.correction_ledger_enabled())

    @patch("github_bot.correction_ledger.CorrectionLedger")
    def test_cli_record_success(self, mock_ctor: MagicMock) -> None:
        from github_bot.correction_ledger import cli_record

        os.environ["OCTO_CORRECTION_LEDGER"] = "1"
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        inst = MagicMock()
        inst.record_negative_example.return_value = "neg_deadbeef"
        mock_ctor.return_value = inst
        code = cli_record(
            rejected="bad",
            corrected="good",
            repo="o/r",
            editor="t",
            source="cli",
        )
        self.assertEqual(code, 0)
        inst.record_negative_example.assert_called_once()


class StylePrefsLedgerGateTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in ("OCTO_STYLE_LEARN_ENABLED", "OCTO_CORRECTION_LEDGER"):
            os.environ.pop(k, None)

    def test_should_learn_true_when_only_correction_ledger_on(self) -> None:
        from github_bot.style_prefs import should_learn_style_from_issue_comment

        os.environ["OCTO_STYLE_LEARN_ENABLED"] = "false"
        os.environ["OCTO_CORRECTION_LEDGER"] = "1"
        headers = {"X-GitHub-Event": "issue_comment"}
        payload = {
            "action": "edited",
            "issue": {"number": 1, "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/1"}},
            "changes": {"body": {"from": "old"}},
            "comment": {"body": "new"},
        }
        self.assertTrue(should_learn_style_from_issue_comment(headers, payload))


if __name__ == "__main__":
    unittest.main()
