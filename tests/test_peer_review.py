"""Tests for observability.peer_review gate parsing and cache labels."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class PeerReviewTests(unittest.TestCase):
    def test_parse_peer_gate_no(self) -> None:
        from observability.peer_review import parse_peer_gate

        raw = "PEER_ISSUES: NO\n\n## Summary\nAll good.\n"
        flag, body = parse_peer_gate(raw)
        self.assertIs(flag, False)
        self.assertIn("Summary", body)
        self.assertNotIn("PEER_ISSUES", body)

    def test_parse_peer_gate_yes(self) -> None:
        from observability.peer_review import parse_peer_gate

        raw = "PEER_ISSUES: YES\n\n### Bug\nBad.\n"
        flag, body = parse_peer_gate(raw)
        self.assertIs(flag, True)
        self.assertIn("Bug", body)

    def test_parse_peer_gate_missing_defaults_ambiguous(self) -> None:
        from observability.peer_review import parse_peer_gate

        raw = "Just markdown without gate.\n"
        flag, body = parse_peer_gate(raw)
        self.assertIsNone(flag)
        self.assertTrue(body)

    def test_cache_model_label(self) -> None:
        from observability.peer_review import cache_model_label

        self.assertEqual(cache_model_label("qwen32", "llama3b", False), "qwen32")
        self.assertIn("llama3b", cache_model_label("qwen32", "llama3b", True))
        self.assertIn("audit:qwen32", cache_model_label("qwen32", "llama3b", True))


if __name__ == "__main__":
    unittest.main()
