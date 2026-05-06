"""Tests for ChromaDB vector memory (mocked Chroma / Ollama)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class MemoryVectorStoreTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_VECTOR_MEMORY",
            "OCTO_CHROMA_HOST",
            "OCTO_CHROMA_PORT",
            "OCTO_EMBEDDING_MODEL",
            "OCTO_VECTOR_MEMORY_TOP_K",
        ):
            os.environ.pop(k, None)

    def test_vector_memory_enabled(self) -> None:
        from observability import memory_vector_store as mvs

        self.assertFalse(mvs.vector_memory_enabled())
        os.environ["OCTO_VECTOR_MEMORY"] = "1"
        self.assertTrue(mvs.vector_memory_enabled())

    def test_attach_similar_sets_snapshot_block(self) -> None:
        from observability import memory_vector_store as mvs

        os.environ["OCTO_VECTOR_MEMORY"] = "1"

        fake_store = mock.Mock()
        fake_store.query_memory.return_value = [
            {
                "repo_full": "o/r",
                "revision_sha": "abcd1234ef",
                "query_head": "scan",
                "excerpt": "SQL injection in login.",
                "distance": 0.21,
                "owner": "o",
                "repo": "r",
                "id": "x",
            }
        ]

        snap: dict = {"owner": "o", "repo": "r"}
        with mock.patch.object(mvs, "VectorMemory", return_value=fake_store):
            mvs.attach_similar_historical_findings("security review", snap, "http://localhost:11434")

        self.assertIn("vector_memory_similar_block", snap)
        self.assertIn("SQL injection", snap["vector_memory_similar_block"])
        self.assertIn("o/r", snap["vector_memory_similar_block"])

    def test_index_skips_without_snapshot(self) -> None:
        from observability import memory_vector_store as mvs

        os.environ["OCTO_VECTOR_MEMORY"] = "1"
        store_cm = mock.patch.object(mvs, "VectorMemory")
        with store_cm as ctor:
            mvs.index_successful_grounded_review(
                query="q",
                model="m",
                ollama_base_url="http://localhost:11434",
                result={"success": True, "answer": "ok"},
            )
            ctor.assert_not_called()

    def test_ollama_embed_prefers_prompt_then_input(self) -> None:
        from observability import memory_vector_store as mvs

        class Resp:
            def __init__(self, ok: bool, payload: dict | None = None, status: int = 200):
                self.status_code = status
                self._payload = payload or {}

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    raise RuntimeError("bad")

            def json(self) -> dict:
                return dict(self._payload)

        ok_emb = {"embedding": [0.1, 0.2]}

        with mock.patch("observability.memory_vector_store.httpx.Client") as client_cls:
            inst = client_cls.return_value.__enter__.return_value
            inst.post.side_effect = [
                Resp(False, {}, 400),
                Resp(True, ok_emb),
            ]
            out = mvs._ollama_embed("hi", "http://localhost:11434", "nomic-embed-text")
        self.assertEqual(out, [0.1, 0.2])

    def test_split_findings_and_fixes(self) -> None:
        from observability import memory_vector_store as mvs

        md = "## Summary\n\nHello.\n\n## Fixes\n\n- Patch auth.\n"
        f, x = mvs.split_findings_and_fixes(md)
        self.assertIn("Summary", f)
        self.assertNotIn("## Fixes", x)
        self.assertIn("Patch auth", x)

    def test_chunk_text_splits_long_body(self) -> None:
        from observability import memory_vector_store as mvs

        body = "\n\n".join(["para"] * 80)
        chunks = mvs.chunk_text(body, max_chars=50, overlap=5)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 50 for c in chunks))

    def test_query_memory_uses_verified_where_filter(self) -> None:
        from observability import memory_vector_store as mvs

        store = mvs.VectorMemory(ollama_base_url="http://127.0.0.1:11434")
        mock_coll = mock.Mock()
        mock_coll.query.return_value = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        store._collection = mock_coll
        with mock.patch.object(store, "embed", return_value=[0.0, 1.0]):
            store.query_memory("hello", k=3)
        mock_coll.query.assert_called_once()
        call_kw = mock_coll.query.call_args.kwargs
        self.assertEqual(call_kw.get("where"), {"is_verified": True})

    def test_add_memory_sets_is_verified_from_rescan_flag(self) -> None:
        from observability import memory_vector_store as mvs

        store = mvs.VectorMemory(ollama_base_url="http://127.0.0.1:11434")
        mock_coll = mock.Mock()
        store._collection = mock_coll
        with (
            mock.patch.object(store, "embed", return_value=[0.1]),
            mock.patch(
                "observability.memory_vector_store.chunk_text",
                return_value=["chunk"],
            ),
        ):
            store.add_memory(
                owner="o",
                repo="r",
                revision_sha="abc",
                query="q",
                answer_markdown="# X\n\n## Fixes\n\nfix\n",
                review_model="m",
                rescan_loop_passed=True,
            )
        mock_coll.upsert.assert_called_once()
        meta = mock_coll.upsert.call_args.kwargs["metadatas"]
        self.assertTrue(all(m.get("is_verified") is True for m in meta))


if __name__ == "__main__":
    unittest.main()
