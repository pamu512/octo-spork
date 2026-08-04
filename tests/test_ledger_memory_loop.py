"""Tests for verified ledger upsert / query helpers (mocked Chroma + embed)."""

from __future__ import annotations

import unittest
from unittest import mock


class TestUpsertVerifiedPattern(unittest.TestCase):
    def test_upsert_calls_chroma(self) -> None:
        from memory import vector_store as vs

        coll = mock.Mock()
        with (
            mock.patch.object(vs, "_get_or_create_ledger_collection", return_value=coll),
            mock.patch(
                "observability.memory_vector_store._ollama_embed",
                return_value=[0.1, 0.2],
            ),
        ):
            rid = vs.upsert_verified_pattern(
                cve_id="CVE-2024-1",
                file_path="src/a.py",
                document="fixed by upgrading dep",
            )
        self.assertTrue(rid and rid.startswith("ledger_"))
        coll.upsert.assert_called_once()
        kwargs = coll.upsert.call_args.kwargs
        self.assertEqual(kwargs["metadatas"][0]["is_verified"], True)
        self.assertEqual(kwargs["metadatas"][0]["cve_id"], "CVE-2024-1")

    def test_query_falls_back_when_ledger_empty(self) -> None:
        from memory import vector_store as vs

        with (
            mock.patch.object(vs, "_get_ledger_collection", return_value=None),
            mock.patch.object(
                vs,
                "_query_review_memory_fallback",
                return_value=[
                    {
                        "id": "mem_1",
                        "document": "excerpt",
                        "cve_id": "",
                        "file_path": "fixes",
                        "is_verified": True,
                        "distance": 0.1,
                    }
                ],
            ) as fb,
        ):
            rows = vs.query_verified_patterns("CVE-2024-1")
        fb.assert_called_once()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_verified"])


if __name__ == "__main__":
    unittest.main()
