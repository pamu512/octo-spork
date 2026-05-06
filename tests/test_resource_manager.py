"""Tests for infra.resource_manager VRAM governor."""

from __future__ import annotations

import os
import sys
import unittest
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class VRAMManagerTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_VRAM_MIN_FREE_RATIO",
            "OCTO_VRAM_PREDICTIVE_GOVERNOR",
            "OCTO_VRAM_GOVERNOR_STRICT_UNKNOWN",
            "OCTO_VRAM_AUTO_UNLOAD",
        ):
            os.environ.pop(k, None)

    def test_assert_blocks_low_free_ratio(self) -> None:
        from infra.resource_manager import MemorySnapshot, VRAMManager

        snap = MemorySnapshot(
            free_mib=100.0,
            total_mib=10000.0,
            free_ratio=0.01,
            backend="nvml",
            detail="test",
        )
        with patch("infra.resource_manager.query_gpu_memory_snapshot", return_value=snap):
            with patch.object(VRAMManager, "estimate_model_vram_mib", return_value=10.0):
                mgr = VRAMManager("http://127.0.0.1:11434")
                with warnings.catch_warnings(record=True) as wrec:
                    warnings.simplefilter("always")
                    with self.assertRaises(ResourceWarning):
                        mgr.assert_can_run_model("llama3.2", auto_unload_if_tight=False)
                self.assertTrue(any(isinstance(w.message, ResourceWarning) for w in wrec))

    def test_clear_cache_calls_generate(self) -> None:
        from infra.resource_manager import VRAMManager

        mgr = VRAMManager("http://127.0.0.1:11434")
        with patch("infra.resource_manager._get_json", return_value={"models": [{"model": "m:latest"}]}):
            with patch("infra.resource_manager._post_json", return_value={"done": True}) as pm:
                out = mgr.clear_cache()
                self.assertEqual(out, ["m:latest"])
                pm.assert_called_once()
                args = pm.call_args[0]
                self.assertEqual(args[2].get("keep_alive"), 0)

    def test_enforce_before_ollama_noop_when_disabled(self) -> None:
        from infra.resource_manager import enforce_before_ollama

        os.environ.pop("OCTO_VRAM_PREDICTIVE_GOVERNOR", None)
        enforce_before_ollama("x", "http://127.0.0.1:11434")
