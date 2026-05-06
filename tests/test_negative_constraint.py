"""Tests for github_bot.negative_constraint."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class NegativeConstraintTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("OCTO_NEGATIVE_CONSTRAINT_ENABLED", None)

    def test_disabled_returns_empty(self) -> None:
        from github_bot import negative_constraint as nc

        os.environ["OCTO_NEGATIVE_CONSTRAINT_ENABLED"] = "0"
        self.assertEqual(
            nc.build_negative_constraint_section(
                "long " * 50,
                ollama_base_url="http://127.0.0.1:11434",
                model="m",
            ),
            "",
        )

    def test_short_review_notice(self) -> None:
        from github_bot import negative_constraint as nc

        os.environ["OCTO_NEGATIVE_CONSTRAINT_ENABLED"] = "1"
        out = nc.build_negative_constraint_section(
            "short",
            ollama_base_url="http://127.0.0.1:11434",
            model="m",
        )
        self.assertIn("too short", out)

    @patch("github_bot.negative_constraint._ollama_chat")
    def test_success_formats_table(self, mock_chat: MagicMock) -> None:
        from github_bot import negative_constraint as nc

        os.environ["OCTO_NEGATIVE_CONSTRAINT_ENABLED"] = "1"
        payload = {
            "items": [
                {
                    "change_summary": "Add auth middleware",
                    "risk_score": 8,
                    "exploit_scenarios": "Bypass if ordering wrong.",
                }
            ]
        }
        mock_chat.return_value = json.dumps(payload)
        out = nc.build_negative_constraint_section(
            "x" * 200,
            ollama_base_url="http://127.0.0.1:11434",
            model="m",
        )
        self.assertIn("Negative constraint", out)
        self.assertRegex(out, r"\|\s*8\s*\|")
        self.assertIn("Add auth middleware", out)

    def test_normalize_payload_clamps_score(self) -> None:
        from github_bot.negative_constraint import _normalize_payload, format_risk_analysis_markdown

        p = _normalize_payload({"items": [{"change_summary": "a", "risk_score": 99, "exploit_scenarios": "x"}]})
        self.assertEqual(p["items"][0]["risk_score"], 10)
        md = format_risk_analysis_markdown(p)
        self.assertIn("| 10 |", md)


if __name__ == "__main__":
    unittest.main()
