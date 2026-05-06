"""Tests for ``claude_bridge.issue_to_task``."""

from __future__ import annotations

import json
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claude_bridge import issue_to_task as itt  # noqa: E402


def _sample_sarif_two_findings() -> dict:
    return {
        "runs": [
            {
                "tool": {"driver": {"name": "CodeQL", "rules": [{"id": "py/sql-injection", "name": "SQL query built from user-controlled input"}]}},
                "results": [
                    {
                        "ruleId": "py/sql-injection",
                        "level": "error",
                        "message": {"text": "User data flows into query."},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "file:///work/db.py"},
                                    "region": {"startLine": 45},
                                }
                            }
                        ],
                    }
                ],
            },
            {
                "tool": {"driver": {"name": "Trivy", "rules": []}},
                "results": [
                    {
                        "ruleId": "DS002",
                        "level": "warning",
                        "message": {"text": "Misconfiguration example."},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "file:///work/Dockerfile"},
                                    "region": {"startLine": 3},
                                }
                            }
                        ],
                    }
                ],
            },
        ]
    }


class ParseSarifTests(unittest.TestCase):
    def test_parses_codeql_and_trivy_runs(self) -> None:
        items = itt.parse_sarif_findings(_sample_sarif_two_findings())
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].tool, "CodeQL")
        self.assertIn("db.py", items[0].file_path)
        self.assertEqual(items[0].line, 45)


class BatchTests(unittest.TestCase):
    def test_batch_size_splits_prompts(self) -> None:
        results = []
        for i in range(5):
            results.append(
                {
                    "ruleId": f"rule-{i}",
                    "level": "warning",
                    "message": {"text": f"Issue {i}"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": f"file:///work/f{i}.py"},
                                "region": {"startLine": i + 1},
                            }
                        }
                    ],
                }
            )
        payload = {
            "runs": [
                {
                    "tool": {"driver": {"name": "Trivy", "rules": []}},
                    "results": results,
                }
            ]
        }
        findings = itt.parse_sarif_findings(payload)
        self.assertEqual(len(findings), 5)
        prompts = itt.build_batched_prompts(findings, batch_size=2, max_chars_per_batch=100_000)
        self.assertEqual(len(prompts), 3)

    def test_shell_contains_claude_p(self) -> None:
        findings = itt.parse_sarif_findings(_sample_sarif_two_findings())
        prompts = itt.build_batched_prompts(findings, batch_size=10)
        out = itt.format_claude_commands(prompts, claude_bin="claude", emit="shell")
        self.assertIn("claude -p ", out)
        self.assertIn("db.py", out)


class CliTests(unittest.TestCase):
    def test_json_emit(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".sarif", delete=False) as f:
            json.dump(_sample_sarif_two_findings(), f)
            path = Path(f.name)
        buf = io.StringIO()
        old = sys.stdout
        try:
            sys.stdout = buf
            rc = itt.run_cli(
                sarif=path,
                batch_size=2,
                max_chars=None,
                claude_bin="claude",
                emit="json",
                max_findings=None,
            )
        finally:
            sys.stdout = old
            path.unlink(missing_ok=True)
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["finding_count"], 2)
        self.assertIn("shell", data["commands"][0])


if __name__ == "__main__":
    unittest.main()
