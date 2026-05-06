"""Tests for :mod:`github_bot.global_smell_index`."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _sarif_for_file(py_file: Path) -> dict:
    return {
        "runs": [
            {
                "tool": {"driver": {"rules": [{"id": "TEST/1", "shortDescription": {"text": "Bad pattern"}}]}},
                "results": [
                    {
                        "ruleId": "TEST/1",
                        "level": "warning",
                        "message": {"text": "security issue"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": py_file.as_uri()},
                                    "region": {"startLine": 1},
                                }
                            }
                        ],
                    }
                ],
            }
        ]
    }


class GlobalSmellIndexTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_GLOBAL_SMELL_DB",
            "OCTO_GLOBAL_SMELL_INDEX",
            "OCTO_SPORK_SKIP_GLOBAL_SMELL",
        ):
            os.environ.pop(k, None)

    def test_second_pr_triggers_recurring_markdown(self) -> None:
        from github_bot.global_smell_index import ingest_sarif_findings, smell_index_enabled

        self.assertTrue(smell_index_enabled())

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "x.db"
            os.environ["OCTO_GLOBAL_SMELL_DB"] = str(db)

            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            dummy = repo_root / "dummy.py"
            dummy.write_text("eval(user_input)\n", encoding="utf-8")
            sarif = _sarif_for_file(dummy)

            md1, rec1 = ingest_sarif_findings(
                "trivy",
                sarif,
                repo_root,
                repo_full_name="org/a",
                pr_html_url="https://github.com/org/a/pull/1",
            )
            self.assertEqual(rec1, [])
            self.assertEqual(md1.strip(), "")

            md2, rec2 = ingest_sarif_findings(
                "trivy",
                sarif,
                repo_root,
                repo_full_name="org/b",
                pr_html_url="https://github.com/org/b/pull/9",
            )
            self.assertTrue(rec2)
            self.assertIn("Recurring architectural debt", md2)
            self.assertIn("org/a", md2)
            self.assertIn("pull/1", md2)

    def test_disabled_skip_env(self) -> None:
        from github_bot import global_smell_index as gsi

        os.environ["OCTO_SPORK_SKIP_GLOBAL_SMELL"] = "1"
        self.assertFalse(gsi.smell_index_enabled())


if __name__ == "__main__":
    unittest.main()
