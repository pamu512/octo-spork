"""Tests for Claude session receipt / grounding auditor."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class ReceiptAuditorTests(unittest.TestCase):
    def test_extract_citations_line_colon_hash(self) -> None:
        from claude_bridge.receipt_auditor import extract_citations

        text = "See src/mod.py:42 and pkg/Foo.tsx#L11 edge.\nAlso README.md:3.\n"
        cites = extract_citations(text)
        raw_paths = {(c.raw, c.line) for c in cites}
        self.assertIn(("src/mod.py", 42), raw_paths)
        self.assertIn(("pkg/Foo.tsx", 11), raw_paths)

    def test_extract_skips_url(self) -> None:
        from claude_bridge.receipt_auditor import audit_transcript

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            warns = audit_transcript(
                "Link https://example.com/foo.py is not a path.\n",
                ws,
            )
        self.assertEqual(warns, [])

    def test_flags_missing_file(self) -> None:
        from claude_bridge.receipt_auditor import WARN_PREFIX, audit_transcript

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "real.py").write_text("x\n", encoding="utf-8")
            warns = audit_transcript(
                "Edit phantom ghost/path/nope.py and done.\n",
                ws,
            )
        self.assertTrue(any(WARN_PREFIX in w for w in warns))
        self.assertTrue(any("nope.py" in w for w in warns))

    def test_flags_bad_line(self) -> None:
        from claude_bridge.receipt_auditor import WARN_PREFIX, audit_transcript

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tiny.py").write_text("a\nb\n", encoding="utf-8")
            warns = audit_transcript("tiny.py:999\n", ws)
        self.assertTrue(any("line 999" in w for w in warns))
        self.assertTrue(any(WARN_PREFIX in w for w in warns))

    def test_clean_citation_no_warnings(self) -> None:
        from claude_bridge.receipt_auditor import audit_transcript

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tiny.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
            warns = audit_transcript(
                "See tiny.py:2 for detail.\nValid tiny.py reference.\n",
                ws,
            )
        self.assertEqual(warns, [])


if __name__ == "__main__":
    unittest.main()
