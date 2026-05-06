"""Tests for negative reinforcement prompts after RescanLoop failure."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class NegativeReinforcementTests(unittest.TestCase):
    def test_prompt_embeds_error_and_instructions(self) -> None:
        from github_bot.negative_reinforcement import negative_reinforcement_prompt

        text = negative_reinforcement_prompt(validation_error="CVE still present in go.mod")
        self.assertIn("Your previous attempt failed validation with error:", text)
        self.assertIn("CVE still present in go.mod", text)
        self.assertIn("This fix is insecure.", text)
        self.assertIn("Do not repeat the previous logic.", text)

    def test_markdown_section_is_fenced(self) -> None:
        from github_bot.negative_reinforcement import negative_reinforcement_markdown_section

        md = negative_reinforcement_markdown_section(validation_error="x")
        self.assertIn("```text", md)
        self.assertIn("Do not repeat the previous logic.", md)


if __name__ == "__main__":
    unittest.main()
