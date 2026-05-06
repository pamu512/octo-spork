"""Tests for ``infra.vram_pressure_monitor``."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class VRAMPressureMonitorTests(unittest.TestCase):
    def test_parse_pressure_high_from_text(self) -> None:
        from infra.vram_pressure_monitor import parse_unified_memory_pressure_level

        txt = """
Graphics/Displays:

    Apple M5:

      Unified Memory: 24 GB
      Pressure: High

      Displays:
        ...
"""
        self.assertEqual(parse_unified_memory_pressure_level(txt), "high")

    def test_parse_pressure_normal(self) -> None:
        from infra.vram_pressure_monitor import parse_unified_memory_pressure_level

        txt = "Unified Memory (Foo)\nSomething\nPressure: Normal\n"
        self.assertEqual(parse_unified_memory_pressure_level(txt), "normal")

    def test_model_large_heuristic(self) -> None:
        from infra.vram_pressure_monitor import model_is_large_for_pressure_override

        self.assertTrue(model_is_large_for_pressure_override("qwen2.5:14b"))
        self.assertTrue(model_is_large_for_pressure_override("x:32b-instruct"))
        self.assertFalse(model_is_large_for_pressure_override("qwen2.5-coder:7b"))

    def test_override_when_high_pressure(self) -> None:
        from infra import vram_pressure_monitor as vpm

        with patch.object(vpm.VRAMPressureMonitor, "unified_memory_pressure_is_high", return_value=True):
            out, reason = vpm.apply_unified_memory_pressure_override(
                "qwen2.5:14b",
                ["qwen2.5-coder:7b", "qwen2.5:14b"],
            )
        self.assertEqual(out, "qwen2.5-coder:7b")
        self.assertEqual(reason, "unified_memory_pressure_high")

    def test_json_pressure_walk(self) -> None:
        from infra.vram_pressure_monitor import _pressure_from_json

        data = {"SPDisplaysDataType": [{"spdisplays_unified_memory_pressure": "High"}]}
        self.assertEqual(_pressure_from_json(data), "high")


if __name__ == "__main__":
    unittest.main()
