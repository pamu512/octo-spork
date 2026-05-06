"""Tests for ContextGovernor VRAM-aware summarization."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class ContextGovernorTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_CONTEXT_GOVERNOR_ENABLED",
            "OCTO_CONTEXT_GOVERNOR_FORCE",
            "OCTO_CTX_GOV_MAX_FILES",
        ):
            os.environ.pop(k, None)

    def test_low_priority_paths(self) -> None:
        from observability.context_governor import is_low_priority_path

        self.assertTrue(is_low_priority_path("tests/unit/foo.py"))
        self.assertTrue(is_low_priority_path("docs/guide.md"))
        self.assertFalse(is_low_priority_path("README.md"))
        self.assertFalse(is_low_priority_path("src/core/auth.py"))

    def test_estimate_snapshot_tokens(self) -> None:
        from observability.context_governor import _estimate_snapshot_tokens

        class RF:
            def __init__(self, path: str, content: str) -> None:
                self.path = path
                self.content = content

        snap = {"readme": "ab", "files": [RF("tests/t.py", "c" * 100)]}

        def est(t: str) -> int:
            return max(1, len(t) // 4)

        self.assertEqual(_estimate_snapshot_tokens(snap, est), est("ab") + est("c" * 100))

    def test_maybe_compress_summarizes_and_updates_tokens(self) -> None:
        from observability.context_governor import ContextGovernor

        class RF:
            def __init__(self, path: str, content: str) -> None:
                self.path = path
                self.content = content
                self.size = len(content.encode("utf-8"))

        long_body = "x" * 8000
        snap: dict = {
            "readme": "",
            "files": [
                RF("tests/unit/a.py", long_body),
                RF("src/main.py", "y" * 800),
            ],
        }

        os.environ["OCTO_CONTEXT_GOVERNOR_FORCE"] = "1"
        os.environ["OCTO_CTX_GOV_MAX_FILES"] = "4"

        def est(t: str) -> int:
            return max(1, len(t) // 4)

        abstract = "First. Second. Third."

        with patch(
            "observability.context_governor._ollama_chat_summarize",
            return_value=abstract,
        ):
            gov = ContextGovernor(ollama_base_url="http://127.0.0.1:11434")
            stats = gov.maybe_compress_snapshot(snap, estimate_tokens=est)

        self.assertIn("tokens_before", stats)
        self.assertIn("tokens_after", stats)
        self.assertLess(stats["tokens_after"], stats["tokens_before"])
        self.assertIn("tests/unit/a.py", stats["compressed_paths"])
        self.assertNotIn("src/main.py", stats["compressed_paths"])

        test_rf = snap["files"][0]
        self.assertIn("ContextGovernor", test_rf.content)
        self.assertIn(abstract, test_rf.content)

    def test_take_three_sentences(self) -> None:
        from observability.context_governor import _take_three_sentences

        s = _take_three_sentences("A. B! C? D.")
        self.assertNotIn(" D.", s)


if __name__ == "__main__":
    unittest.main()
