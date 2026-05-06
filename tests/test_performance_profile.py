"""Tests for ``local_ai_stack.performance_profile``."""

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


class PerformanceProfileTests(unittest.TestCase):
    def test_compute_stability_score_prefers_fast_low_vram(self) -> None:
        from local_ai_stack.performance_profile import compute_stability_score

        high = {"success": True, "time_to_first_token_sec": 0.1, "tokens_per_second": 40.0, "peak_vram_mib": 4000.0}
        low = {"success": True, "time_to_first_token_sec": 0.5, "tokens_per_second": 10.0, "peak_vram_mib": 12000.0}
        self.assertGreater(compute_stability_score(high), compute_stability_score(low))

    def test_pick_most_stable_model(self) -> None:
        from local_ai_stack.performance_profile import pick_most_stable_model

        rows = [
            {"name": "big", "success": True, "time_to_first_token_sec": 1.0, "tokens_per_second": 5.0, "peak_vram_mib": 9000},
            {"name": "stable", "success": True, "time_to_first_token_sec": 0.2, "tokens_per_second": 35.0, "peak_vram_mib": 3500},
        ]
        name, score = pick_most_stable_model(rows)
        self.assertEqual(name, "stable")
        self.assertIsNotNone(score)

    def test_resolve_background_review_explicit_env(self) -> None:
        from local_ai_stack.performance_profile import resolve_background_review_model

        with patch.dict(os.environ, {"OCTO_BACKGROUND_REVIEW_MODEL": "custom:v1"}, clear=False):
            self.assertEqual(resolve_background_review_model(), "custom:v1")

    def test_resolve_background_review_from_profile_file(self) -> None:
        from local_ai_stack.performance_profile import resolve_background_review_model

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prof = root / "performance_profile.json"
            prof.write_text(
                json.dumps({"most_stable_model": "t:t"}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"OCTO_PERF_PROFILE_PATH": str(prof), "OLLAMA_MODEL": "fallback"}, clear=False):
                with patch(
                    "local_ai_stack.model_fallback.list_local_ollama_model_names",
                    return_value=["t:t", "other"],
                ):
                    m = resolve_background_review_model(ollama_base_url="http://x")
            self.assertEqual(m, "t:t")

    def test_build_standard_prompt_near_target_tokens(self) -> None:
        from local_ai_stack.performance_profile import build_standard_prompt

        sys.path.insert(0, str(SRC))
        from claude_bridge.token_governor import estimate_tokens_python

        p = build_standard_prompt(target_tokens=100)
        self.assertGreaterEqual(estimate_tokens_python(p), 100)

    def test_bench_one_model_stream(self) -> None:
        from local_ai_stack.performance_profile import bench_one_model

        lines = [
            '{"model":"m","created_at":"x","response":"","done":false}',
            '{"model":"m","created_at":"x","response":"Hi","done":false}',
            '{"model":"m","done":true,"eval_count":10,"eval_duration":1000000000}',
            "",
        ]
        blob = "\n".join(lines)

        class FakeStream:
            def __enter__(self) -> FakeStream:
                return self

            def __exit__(self, *a: object) -> None:
                return None

            def raise_for_status(self) -> None:
                return None

            def iter_text(self):
                yield blob

        fake_resp = FakeStream()
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.__exit__.return_value = None
        fake_client.stream.return_value = fake_resp

        with patch("httpx.Client") as mock_cls:
            mock_cls.return_value.__enter__.return_value = fake_client
            row = bench_one_model("http://127.0.0.1:11434", "m", "prompt", completion_tokens=64)
        self.assertTrue(row.get("success"))
        self.assertGreater(row.get("tokens_per_second") or 0, 0)
        self.assertIsNotNone(row.get("time_to_first_token_sec"))


if __name__ == "__main__":
    unittest.main()
