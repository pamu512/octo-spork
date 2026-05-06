"""Tests for SARIF → GitHub pull request review comment mapping."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_SAMPLE_DIFF = """
diff --git a/pkg/app.py b/pkg/app.py
index 111..222 100644
--- a/pkg/app.py
+++ b/pkg/app.py
@@ -1,4 +1,5 @@
 def main():
+    # risky
     print("hi")
     return 0
""".lstrip()

_SAMPLE_SARIF = {
    "runs": [
        {
            "tool": {"driver": {"rules": [{"id": "test/rule", "shortDescription": {"text": "Test"}}]}},
            "results": [
                {
                    "ruleId": "test/rule",
                    "message": {"text": "Found issue"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "pkg/app.py"},
                                "region": {"startLine": 2, "endLine": 3},
                            }
                        }
                    ],
                }
            ],
        }
    ]
}


class SarifReviewCommentsTests(unittest.TestCase):
    def test_multiline_uses_end_line_and_start_line(self) -> None:
        from github_bot.sarif_review_comments import sarif_to_pull_request_review_comments

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "r.sarif"
            p.write_text(json.dumps(_SAMPLE_SARIF), encoding="utf-8")
            out = sarif_to_pull_request_review_comments(
                p,
                commit_id="abc123" * 7,
                unified_pr_diff=None,
                tool_label="Trivy",
            )
        self.assertEqual(len(out), 1)
        c = out[0]
        d = c.as_api_dict()
        self.assertEqual(d["side"], "RIGHT")
        self.assertEqual(d["commit_id"], "abc123" * 7)
        self.assertEqual(d["path"], "pkg/app.py")
        self.assertEqual(d["line"], 3)
        self.assertEqual(d["start_line"], 2)
        self.assertEqual(d["start_side"], "RIGHT")

    def test_diff_note_detects_addition(self) -> None:
        from github_bot.sarif_review_comments import sarif_to_pull_request_review_comments

        sarif = {
            "runs": [
                {
                    "tool": {"driver": {"rules": []}},
                    "results": [
                        {
                            "ruleId": "r1",
                            "message": {"text": "x"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "pkg/app.py"},
                                        "region": {"startLine": 2, "endLine": 2},
                                    }
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "r.sarif"
            p.write_text(json.dumps(sarif), encoding="utf-8")
            out = sarif_to_pull_request_review_comments(
                p,
                commit_id="def456" * 7,
                unified_pr_diff=_SAMPLE_DIFF,
            )
        self.assertIn("addition", out[0].body.lower())

    def test_splits_diff_by_file(self) -> None:
        from github_bot.sarif_review_comments import build_pr_diff_index

        idx = build_pr_diff_index(_SAMPLE_DIFF)
        self.assertIn("pkg/app.py", idx)

    def test_as_api_dict_omits_start_line_for_single(self) -> None:
        from github_bot.sarif_review_comments import PullRequestReviewCommentInput

        c = PullRequestReviewCommentInput(
            body="b",
            commit_id="a",
            path="p",
            line=1,
            side="RIGHT",
        )
        d = c.as_api_dict()
        self.assertNotIn("start_line", d)


if __name__ == "__main__":
    unittest.main()
