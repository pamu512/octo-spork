"""Tests for remediation Doctor TUI helpers (no Textual runtime required for logic tests)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class RemediationTuiLogicTests(unittest.TestCase):
    def test_extract_edit_target_explicit_wins(self) -> None:
        from claude_bridge.remediation_tui import extract_edit_target

        t = extract_edit_target("# OCTO_EDIT_TARGET: a.py\nx", "b.py")
        self.assertEqual(t, "b.py")

    def test_extract_from_directive(self) -> None:
        from claude_bridge.remediation_tui import extract_edit_target

        t = extract_edit_target("# OCTO_EDIT_TARGET: src/x.py\npass\n", None)
        self.assertEqual(t, "src/x.py")

    def test_strip_leading_directives(self) -> None:
        from claude_bridge.remediation_tui import strip_leading_edit_directives

        raw = "# OCTO_EDIT_TARGET: foo.py\n\nprint(1)\n"
        self.assertEqual(strip_leading_edit_directives(raw).strip(), "print(1)")

    def test_apply_file_edit_writes(self) -> None:
        from claude_bridge.remediation_tui import apply_file_edit

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pkg").mkdir()
            ok, msg = apply_file_edit(root, "pkg/hi.txt", "hello")
            self.assertTrue(ok)
            self.assertTrue(Path(msg).is_file())
            self.assertEqual(Path(msg).read_text(), "hello")

    def test_apply_blocked_github_bot(self) -> None:
        from claude_bridge.remediation_tui import apply_file_edit

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src" / "github_bot").mkdir(parents=True)
            ok, msg = apply_file_edit(root, "src/github_bot/x.py", "nope")
            self.assertFalse(ok)
            self.assertIn("blocked", msg.lower())


if __name__ == "__main__":
    unittest.main()
