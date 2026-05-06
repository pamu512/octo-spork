"""Tests for ``github_bot.git_manager`` (URL parsing and formatting; HTTP mocked)."""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class ParseWebhookTests(unittest.TestCase):
    def test_parse_owner_repo(self) -> None:
        from github_bot.git_manager import parse_repository_owner_repo

        o, r = parse_repository_owner_repo(
            {
                "repository": {
                    "full_name": "acme/widgets",
                },
            }
        )
        self.assertEqual((o, r), ("acme", "widgets"))

    def test_parse_shas(self) -> None:
        from github_bot.git_manager import parse_pull_request_refs

        n, h, b = parse_pull_request_refs(
            {
                "pull_request": {
                    "number": 7,
                    "head": {"sha": "abc"},
                    "base": {"sha": "def"},
                },
            }
        )
        self.assertEqual(n, 7)
        self.assertEqual(h, "abc")
        self.assertEqual(b, "def")

    def test_action_filter_rejects(self) -> None:
        from github_bot.git_manager import grounded_context_from_pull_request_event

        with self.assertRaises(ValueError):
            grounded_context_from_pull_request_event(
                {"action": "labeled", "repository": {"full_name": "a/b"}, "pull_request": {"number": 1, "head": {"sha": "1"}, "base": {"sha": "2"}}},
                "t",
            )


class FormatLlmTests(unittest.TestCase):
    def test_removed_uses_patch_not_empty_text(self) -> None:
        from github_bot.git_manager import GroundedFile, GroundedPullRequest

        g = GroundedPullRequest(
            owner="o",
            repo="r",
            number=1,
            head_sha="h",
            base_sha="b",
            unified_diff="diff",
            files=[
                GroundedFile(
                    path="x.txt",
                    status="removed",
                    patch_hunk="@@ -1 +0,0 @@\n-a",
                    full_text=None,
                )
            ],
        )
        out = g.format_for_llm()
        self.assertIn("deleted", out)
        self.assertIn("```diff", out)
        self.assertNotIn("```text\n\n```", out)


class BuildGroundedMockedTests(unittest.TestCase):
    @patch("github_bot.git_manager._get_json")
    @patch("github_bot.git_manager._request")
    def test_fetches_diff_and_file_contents(
        self,
        mock_request: MagicMock,
        mock_get_json: MagicMock,
    ) -> None:
        from github_bot.git_manager import build_grounded_pull_request

        diff_bytes = b"diff --git a/foo.py b/foo.py\n"
        mock_request.side_effect = [(200, diff_bytes)]

        file_row = {
            "filename": "foo.py",
            "status": "modified",
            "patch": "@@ ...",
        }
        mock_get_json.side_effect = [
            [file_row],
            {"type": "file", "encoding": "base64", "content": base64.b64encode(b"x=1\n").decode("ascii")},
        ]

        ctx = build_grounded_pull_request(
            "o",
            "r",
            3,
            head_sha="HEADSHA",
            base_sha="BASESHA",
            token="tok",
        )

        self.assertEqual(ctx.unified_diff, diff_bytes.decode())
        self.assertEqual(len(ctx.files), 1)
        self.assertEqual(ctx.files[0].path, "foo.py")
        self.assertEqual(ctx.files[0].full_text, "x=1\n")
        self.assertFalse(ctx.files[0].fetch_error)


if __name__ == "__main__":
    unittest.main()
