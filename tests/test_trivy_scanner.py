"""Tests for Trivy SARIF scanner helpers."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class TrivyScannerTests(unittest.TestCase):
    def test_authenticated_clone_url(self) -> None:
        from github_bot.trivy_scanner import authenticated_clone_url

        u = authenticated_clone_url("https://github.com/acme/r.git", "tok|en")
        self.assertTrue(u.startswith("https://x-access-token:"))
        self.assertIn("github.com/acme/r.git", u)
        self.assertNotIn("|", u.split("@", 1)[0])

    def test_parse_sarif_to_markdown_table_orders_severity(self) -> None:
        from github_bot.trivy_scanner import parse_sarif_to_markdown_table

        sarif = {
            "runs": [
                {
                    "tool": {"driver": {"rules": [{"id": "r/warn", "name": "Warn"}]}},
                    "results": [
                        {
                            "ruleId": "r/warn",
                            "level": "warning",
                            "message": {"text": "later"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "b.py"},
                                        "region": {"startLine": 2},
                                    }
                                }
                            ],
                        },
                        {
                            "ruleId": "r/err",
                            "level": "error",
                            "message": {"text": "first"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "a.py"},
                                        "region": {"startLine": 10},
                                    }
                                }
                            ],
                        },
                    ],
                }
            ]
        }
        md = parse_sarif_to_markdown_table(sarif, limit=10)
        self.assertIn("| Severity | Rule | Location | Message |", md)
        pos_err = md.index("r/err")
        pos_warn = md.index("r/warn")
        self.assertLess(pos_err, pos_warn)

    def test_parse_sarif_empty(self) -> None:
        from github_bot.trivy_scanner import parse_sarif_to_markdown_table

        md = parse_sarif_to_markdown_table({"runs": []})
        self.assertIn("No SARIF results", md)

    def test_run_fs_sarif_invokes_trivy_with_parent_target(self) -> None:
        from github_bot.trivy_scanner import TrivyScanner, TrivyScanResult

        captured: dict[str, object] = {}

        def fake_run(cmd, cwd, capture_output, text, timeout, check):  # noqa: ARG001
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            Path(cwd).joinpath("results.sarif").write_text('{"runs": []}', encoding="utf-8")
            return MagicMock(returncode=0, stderr="", stdout="")

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sc = TrivyScanner(trivy_executable="/fake/trivy", timeout_sec=120)
            with patch("github_bot.trivy_scanner.subprocess.run", side_effect=fake_run):
                out = sc.run_fs_sarif(tmp)

        self.assertIsInstance(out, TrivyScanResult)
        cmd = captured["cmd"]
        assert isinstance(cmd, list)
        self.assertEqual(cmd[-1], "..")
        self.assertIn("sarif", cmd)

    def test_scan_pr_branch_respects_skip_env(self) -> None:
        from github_bot import trivy_scanner as mod

        with patch.dict("os.environ", {"OCTO_SPORK_SKIP_TRIVY": "1"}):
            md = mod.scan_pr_branch_to_markdown(
                clone_url="https://github.com/a/b.git",
                branch="main",
                token="x",
            )
        self.assertIn("skipped", md.lower())


if __name__ == "__main__":
    unittest.main()
