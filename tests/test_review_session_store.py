"""Tests for observability.review_session_store."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class ReviewSessionStoreTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("OCTO_SPORK_REPO_ROOT", None)

    def test_roundtrip_with_repo_root(self) -> None:
        from observability.review_session_store import (
            load_last_review_session,
            persist_last_review_session,
            review_session_path,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            os.environ["OCTO_SPORK_REPO_ROOT"] = str(root)
            payload = {
                "version": 1,
                "query": "q",
                "answer": "a",
                "prompt": "p",
                "model": "m",
                "ollama_base_url": "http://x",
                "meta": {},
                "extras": {},
            }
            p = persist_last_review_session(payload, repo_root=root)
            self.assertIsNotNone(p)
            self.assertTrue(review_session_path(root).is_file())
            loaded = load_last_review_session(root)
            self.assertEqual(loaded.get("prompt"), "p")
            self.assertEqual(loaded.get("answer"), "a")


if __name__ == "__main__":
    unittest.main()
