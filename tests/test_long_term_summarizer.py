"""Tests for :mod:`agent_guard.long_term_summarizer`."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class LongTermSummarizerTests(unittest.TestCase):
    def test_estimate_tokens_small_thread(self) -> None:
        from agent_guard.long_term_summarizer import estimate_conversation_tokens

        v = {"messages": [{"role": "user", "content": "hello"}]}
        self.assertGreaterEqual(estimate_conversation_tokens(v), 1)

    def test_consolidate_archives_and_replaces_messages(self) -> None:
        from agent_guard.long_term_summarizer import LongTermSummarizer

        with tempfile.TemporaryDirectory() as tmp:
            cold = Path(tmp) / "cold"
            big = "word " * 8000
            values = {
                "messages": [
                    {"role": "user", "content": big},
                    {"role": "assistant", "content": "reply " * 8000},
                ],
                "other_state": 42,
            }
            summ = LongTermSummarizer(
                enabled=True,
                threshold=500,
                ollama_base_url="http://127.0.0.1:9",
                model="qwen2.5:3b",
                cold_storage_dir=cold,
            )
            ten = "\n".join(f"- point {i}" for i in range(1, 11))
            with patch.object(LongTermSummarizer, "summarize_transcript", return_value=ten):
                out = summ.consolidate_if_needed("thread-xyz", values)

            self.assertIn("octo_memory_consolidation", out)
            self.assertEqual(out["other_state"], 42)
            self.assertEqual(len(out["messages"]), 2)
            self.assertIn("token budget", out["messages"][0]["content"])
            files = list(cold.glob("*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(data["thread_id"], "thread-xyz")
            self.assertEqual(data["values"]["other_state"], 42)
            self.assertGreater(len(data["values"]["messages"]), 0)

    def test_push_snapshot_triggers_consolidation_when_threshold_low(self) -> None:
        from agent_guard.session_store import SessionStore

        store_data: dict[str, str] = {}

        def fake_get(key: str) -> str | None:
            return store_data.get(key)

        def fake_set(key: str, val: str, *a: object, **kw: object) -> bool:
            store_data[key] = val
            return True

        fake = MagicMock()
        fake.get.side_effect = fake_get
        fake.set.side_effect = fake_set
        fake.expire.return_value = True

        big = "tokenish " * 6000
        vals = {"messages": [{"role": "user", "content": big}]}

        with tempfile.TemporaryDirectory() as tmp:
            cold = Path(tmp) / "c"
            env = {
                "OCTO_MEMORY_CONSOLIDATION_ENABLED": "true",
                "OCTO_MEMORY_TOKEN_THRESHOLD": "100",
                "OCTO_MEMORY_COLD_STORAGE_DIR": str(cold),
            }
            ten = "\n".join(f"- b{i}" for i in range(10))
            with patch.dict(os.environ, env, clear=False):
                with patch("redis.Redis.from_url", return_value=fake):
                    with patch(
                        "agent_guard.long_term_summarizer.LongTermSummarizer.summarize_transcript",
                        return_value=ten,
                    ):
                        s = SessionStore(redis_url="redis://localhost:9999/0", key_prefix="test:lt")
                        s.push_snapshot("tid-merge", vals)

            raw = store_data.get("test:lt:latest")
            self.assertIsNotNone(raw)
            obj = json.loads(str(raw))
            self.assertIn("octo_memory_consolidation", obj["values"])
            self.assertTrue(any(cold.glob("*.json")))


if __name__ == "__main__":
    unittest.main()
