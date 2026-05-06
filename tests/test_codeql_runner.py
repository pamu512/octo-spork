"""Tests for CodeQL runner (PR comment integration)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class CodeQLRunnerTests(unittest.TestCase):
    def test_available_false_without_binary(self) -> None:
        from github_bot.codeql_runner import CodeQLRunner

        r = CodeQLRunner(codeql_executable="/nonexistent/codeql")
        self.assertFalse(r.available())

    def test_infer_language(self) -> None:
        from github_bot.codeql_runner import CodeQLRunner

        r = CodeQLRunner(codeql_executable="/x")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "requirements.txt").write_text("x", encoding="utf-8")
            self.assertEqual(r.infer_language(root), "python")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "package.json").write_text("{}", encoding="utf-8")
            self.assertEqual(r.infer_language(root), "javascript")

    def test_extract_critical_respects_error_level(self) -> None:
        from github_bot.codeql_runner import CodeQLRunner

        sarif = {
            "runs": [
                {
                    "tool": {"driver": {"rules": []}},
                    "results": [
                        {
                            "ruleId": "x",
                            "level": "error",
                            "message": {"text": "bad"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "a.py"},
                                        "region": {"startLine": 1},
                                    }
                                }
                            ],
                        },
                        {
                            "ruleId": "y",
                            "level": "warning",
                            "message": {"text": "maybe"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "b.py"},
                                        "region": {"startLine": 2},
                                    }
                                }
                            ],
                        },
                    ],
                }
            ]
        }
        found = CodeQLRunner.extract_critical_findings(sarif)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].rule_id, "x")

    def test_create_failed_yields_system_warning(self) -> None:
        from github_bot.codeql_runner import CodeQLDatabaseCreateFailed, CodeQLRunner

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "x.py").write_text("print(1)", encoding="utf-8")
            r = CodeQLRunner(codeql_executable="/fake/codeql")
            with patch.object(CodeQLRunner, "create_database", side_effect=CodeQLDatabaseCreateFailed("x", stderr="error: no member named 'foo'", stdout="")):
                res = r.run_on_source_root(root, work_dir=root / "w")
        self.assertTrue(res.build_failed)
        self.assertIn("System Warning (CodeQL)", res.markdown)
        self.assertIn("error: no member", res.markdown)

    def test_skip_env(self) -> None:
        from github_bot import codeql_runner as mod

        with patch.dict("os.environ", {"OCTO_SPORK_SKIP_CODEQL": "1"}):
            out = mod.scan_pr_branch_codeql_to_markdown(
                clone_url="https://github.com/a/b",
                branch="main",
                token="t",
            )
        self.assertIn("skipped", out.lower())
