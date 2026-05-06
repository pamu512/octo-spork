"""Tests for Review Refiner helper."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class ReviewRefinerTests(unittest.TestCase):
    def test_strip_markdown_fence(self) -> None:
        from github_bot.review_refiner import strip_markdown_fence

        raw = "```markdown\n## Hi\nok\n```"
        self.assertTrue(strip_markdown_fence(raw).startswith("## Hi"))

    def test_refinement_disabled_returns_draft_via_or_original(self) -> None:
        from github_bot import review_refiner as rr

        with mock.patch.dict(os.environ, {"OCTO_REVIEW_REFINER_ENABLED": ""}, clear=False):
            out = rr.refine_review_or_original("DRAFT", pr_context="ctx", workspace=Path("/tmp"))
        self.assertEqual(out, "DRAFT")

    def test_maybe_refine_integrator_pass_through_when_disabled(self) -> None:
        from github_bot import review_refiner as rr

        with mock.patch.dict(os.environ, {"OCTO_REVIEW_REFINER_ENABLED": ""}, clear=False):
            out = rr.maybe_refine_ai_section_for_integrator(
                "RAW",
                pr_context="x",
                repo_path=Path("/tmp"),
            )
        self.assertEqual(out, "RAW")

    def test_pr_html_url_from_pull_request_payload(self) -> None:
        import github_bot.review_queue as rq

        self.assertEqual(
            rq.pr_html_url_from_pull_request_payload(
                {
                    "repository": {"full_name": "acme/r"},
                    "pull_request": {"number": 5},
                }
            ),
            "https://github.com/acme/r/pull/5",
        )


if __name__ == "__main__":
    unittest.main()
