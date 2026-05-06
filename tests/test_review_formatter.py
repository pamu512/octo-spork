"""Tests for ReviewFormatter (AI JSON → grouped GitHub Review markdown)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


_SAMPLE = {
    "review_summary": "Quick triage over touched files.",
    "findings": [
        {
            "file": "b/x.py",
            "line_start": 10,
            "line_end": 11,
            "issue_type": "security",
            "severity": "critical",
            "evidence_quote": "eval(user_input)",
        },
        {
            "file": "a/z.go",
            "line_start": 3,
            "line_end": 3,
            "issue_type": "correctness",
            "severity": "medium",
            "evidence_quote": "unchecked err",
        },
        {
            "file": "b/x.py",
            "line_start": 1,
            "line_end": 1,
            "issue_type": "style",
            "severity": "low",
            "evidence_quote": "TODO",
        },
    ],
}


class ReviewFormatterTests(unittest.TestCase):
    def test_groups_by_file_sorted_paths(self) -> None:
        from github_bot.review_formatter import ReviewFormatter

        out = ReviewFormatter().format(_SAMPLE).markdown
        pos_a = out.index("`a/z.go`")
        pos_b = out.index("`b/x.py`")
        self.assertLess(pos_a, pos_b)

    def test_severity_emojis_present(self) -> None:
        from github_bot.review_formatter import ReviewFormatter

        out = ReviewFormatter().format(_SAMPLE).markdown
        self.assertIn("🔴", out)
        self.assertIn("🟡", out)
        self.assertIn("🟢", out)

    def test_collapsible_long_auxiliary_log(self) -> None:
        from github_bot.review_formatter import ReviewFormatter

        long_log = "LINE\n" * 300
        payload = {"findings": [], "raw_log": long_log}
        out = ReviewFormatter(auxiliary_log_detail_threshold=100).format(payload).markdown
        self.assertIn("<details>", out)
        self.assertIn("</details>", out)

    def test_long_evidence_in_details(self) -> None:
        from github_bot.review_formatter import ReviewFormatter

        quote = "x" * 500
        payload = {
            "findings": [
                {
                    "file": "f.py",
                    "line_start": 1,
                    "line_end": 1,
                    "issue_type": "x",
                    "severity": "high",
                    "evidence_quote": quote,
                }
            ]
        }
        out = ReviewFormatter(evidence_detail_threshold=120).format(payload).markdown
        self.assertIn("<details>", out)
        self.assertIn("Full evidence excerpt", out)

    def test_explicit_summary_section(self) -> None:
        from github_bot.review_formatter import ReviewFormatter

        out = ReviewFormatter().format(_SAMPLE).markdown
        self.assertIn("## Summary", out)
        self.assertIn("Quick triage", out)

    def test_single_post_footer(self) -> None:
        from github_bot.review_formatter import ReviewFormatter

        out = ReviewFormatter().format(_SAMPLE).markdown
        self.assertIn("grouped by file", out.lower())

    def test_grounded_receipts_and_analyzed_from(self) -> None:
        from github_bot.review_formatter import ReviewFormatter

        out = ReviewFormatter().format(_SAMPLE).markdown
        self.assertIn("## Grounded Receipts", out)
        self.assertIn("**Analyzed from:** `a/z.go`", out)
        self.assertIn("_**Analyzed from:** `b/x.py`_", out)

    def test_filters_ungrounded_findings(self) -> None:
        from github_bot.review_formatter import ReviewFormatter

        mixed = {
            "findings": [
                {
                    "file": "",
                    "line_start": 1,
                    "line_end": 1,
                    "issue_type": "x",
                    "severity": "high",
                    "evidence_quote": "a",
                },
                {
                    "file": "ok.py",
                    "line_start": 1,
                    "line_end": 1,
                    "issue_type": "x",
                    "severity": "high",
                    "evidence_quote": "b",
                },
            ]
        }
        out = ReviewFormatter().format(mixed).markdown
        self.assertIn("ok.py", out)
        self.assertIn("filtered out", out.lower())
        self.assertIn("## Findings by file", out)
        self.assertNotIn("(unknown path)", out)


if __name__ == "__main__":
    unittest.main()
