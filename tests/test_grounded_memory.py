"""Tests for ``claude_bridge.grounded_memory``."""

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

from claude_bridge.grounded_memory import (  # noqa: E402
    MANAGED_END,
    MANAGED_START,
    GroundedMemoryManager,
    USER_END,
    USER_START,
    format_sarif_hotspots_section,
    merge_claude_md,
)


def _minimal_sarif_one_finding() -> dict:
    return {
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [
                            {
                                "id": "py/test-rule",
                                "name": "Test rule",
                                "shortDescription": {"text": "Short"},
                            }
                        ]
                    }
                },
                "results": [
                    {
                        "ruleId": "py/test-rule",
                        "level": "error",
                        "message": {"text": "Something bad"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "file:///src/app.py"},
                                    "region": {"startLine": 42},
                                }
                            }
                        ],
                    }
                ],
            }
        ]
    }


class MergeClaudeMdTests(unittest.TestCase):
    def test_fresh_file_wraps_user_section(self) -> None:
        md = merge_claude_md(None, "## Managed\n\nHello.")
        self.assertIn(MANAGED_START, md)
        self.assertIn(MANAGED_END, md)
        self.assertIn(USER_START, md)
        self.assertIn(USER_END, md)
        self.assertIn("Hello.", md)

    def test_preserves_user_on_resync(self) -> None:
        first = merge_claude_md(None, "Content A.")
        second = merge_claude_md(first, "Content B.")
        self.assertIn("Content B.", second)
        self.assertIn(USER_START, second)
        between_u, _, _ = second.partition(USER_START)[2].partition(USER_END)
        self.assertIn("_Add project-specific notes", between_u)

    def test_legacy_whole_file_becomes_user(self) -> None:
        legacy = "# My notes\n\nKeep this.\n"
        merged = merge_claude_md(legacy, "Auto.")
        self.assertIn(MANAGED_START, merged)
        self.assertIn("# My notes", merged)
        self.assertIn("Keep this.", merged)
        self.assertIn("Auto.", merged)


class SarifHotspotsTests(unittest.TestCase):
    def test_format_lists_finding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "r.sarif"
            p.write_text(json.dumps(_minimal_sarif_one_finding()), encoding="utf-8")
            out = format_sarif_hotspots_section(p, heading="Trivy", limit=5)
            self.assertIn("ERROR", out)
            self.assertIn("app.py", out)
            self.assertIn("py/test-rule", out)


class GroundedMemoryManagerTests(unittest.TestCase):
    def test_sync_writes_claude_md(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".octo").mkdir(parents=True, exist_ok=True)
            (root / ".octo" / "user_summary.md").write_text(
                "- Prefer functional core.\n",
                encoding="utf-8",
            )
            sarif = root / "t.sarif"
            sarif.write_text(json.dumps(_minimal_sarif_one_finding()), encoding="utf-8")

            mgr = GroundedMemoryManager(
                repo_root=root,
                user_summary_path=root / ".octo" / "user_summary.md",
                trivy_sarif=sarif,
                codeql_sarif=None,
            )
            res = mgr.sync(dry_run=False)
            self.assertTrue(res.path.is_file())
            text = res.path.read_text(encoding="utf-8")
            self.assertIn("functional core", text)
            self.assertIn("Strict coding standards", text)
            self.assertIn(MANAGED_START, text)

    def test_user_notes_survive_second_sync(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            summary = root / "USER_SUMMARY.md"
            summary.write_text("Design: keep it small.", encoding="utf-8")

            mgr = GroundedMemoryManager(repo_root=root, user_summary_path=summary)
            mgr.sync(dry_run=False)
            p = root / "CLAUDE.md"
            v1 = p.read_text(encoding="utf-8")
            _, _, u1 = v1.partition(USER_START)
            user1, _, _ = u1.partition(USER_END)

            p.write_text(
                v1.replace(user1, "\nMY CUSTOM NOTE\n", 1),
                encoding="utf-8",
            )
            v1b = p.read_text(encoding="utf-8")
            self.assertIn("MY CUSTOM NOTE", v1b)

            mgr.sync(dry_run=False)
            v2 = p.read_text(encoding="utf-8")
            self.assertIn("MY CUSTOM NOTE", v2)
            self.assertIn("Design: keep it small.", v2)


if __name__ == "__main__":
    unittest.main()
