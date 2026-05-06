"""Tests for compose log coloring helpers."""

from __future__ import annotations

import unittest

from local_ai_stack.compose_logs import format_compose_log_line, highlight_severity_keywords


class ComposeLogsFormatTests(unittest.TestCase):
    def test_keyword_highlight_is_case_insensitive(self) -> None:
        self.assertIn("\033[91m", highlight_severity_keywords("something ERROR happened"))
        self.assertIn("\033[91m", highlight_severity_keywords("critical failure"))

    def test_prefix_colored_and_keyword_in_body(self) -> None:
        line = "local-ai-n8n  | workflow TIMEOUT"
        out = format_compose_log_line(line)
        self.assertIn("\033[92m", out)  # n8n green prefix coloring
        self.assertIn("\033[91mTIMEOUT", out)

    def test_backend_blue_family_for_agentic_api_prefix(self) -> None:
        line = "local-ai-agentic-api  | INFO message"
        out = format_compose_log_line(line)
        self.assertIn("\033[94m", out)


if __name__ == "__main__":
    unittest.main()
