"""Tests for VRAM guard / ResourceMonitor."""

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


class ResourceMonitorTests(unittest.TestCase):
    def tearDown(self) -> None:
        for key in (
            "OCTO_SKIP_VRAM_GUARD",
            "OCTO_VRAM_MAX_UTIL_PCT",
            "OCTO_VRAM_MAC_MEMORY_PROXY",
        ):
            os.environ.pop(key, None)

    def test_skip_env_allows_high_util(self) -> None:
        from claude_bridge.resource_monitor import vram_guard_allows_claude_launch

        os.environ["OCTO_SKIP_VRAM_GUARD"] = "1"
        ok, msg = vram_guard_allows_claude_launch()
        self.assertTrue(ok)
        self.assertIsNone(msg)

    def test_nvidia_smi_blocks_over_threshold(self) -> None:
        from claude_bridge import resource_monitor as rm

        os.environ.pop("OCTO_SKIP_VRAM_GUARD", None)

        fake_out = "92000, 100000\n"  # 92 MiB / 100 MiB

        def fake_capture(argv: list[str], *, timeout: float = 15.0) -> tuple[int, str]:
            if argv[:2] == ["nvidia-smi", "--query-gpu=memory.used,memory.total"]:
                return 0, fake_out + "\n"
            if argv[:2] == ["system_profiler", "SPDisplaysDataType"]:
                return 0, "{}\n"
            return 127, ""

        with mock.patch.object(rm, "_run_capture", side_effect=fake_capture):
            ok, msg = rm.vram_guard_allows_claude_launch()
        self.assertFalse(ok)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertIn("92", msg)
        self.assertIn("nvidia-smi", msg)

    def test_nvidia_smi_allows_under_threshold(self) -> None:
        from claude_bridge import resource_monitor as rm

        fake_out = "1000, 100000\n"

        def fake_capture(argv: list[str], *, timeout: float = 15.0) -> tuple[int, str]:
            if argv[:2] == ["nvidia-smi", "--query-gpu=memory.used,memory.total"]:
                return 0, fake_out + "\n"
            return 127, ""

        with mock.patch.object(rm, "_run_capture", side_effect=fake_capture):
            ok, msg = rm.vram_guard_allows_claude_launch()
        self.assertTrue(ok)
        self.assertIsNone(msg)

    def test_custom_threshold(self) -> None:
        from claude_bridge import resource_monitor as rm

        os.environ["OCTO_VRAM_MAX_UTIL_PCT"] = "50"
        fake_out = "60000, 100000\n"  # 60%

        def fake_capture(argv: list[str], *, timeout: float = 15.0) -> tuple[int, str]:
            if argv[:2] == ["nvidia-smi", "--query-gpu=memory.used,memory.total"]:
                return 0, fake_out + "\n"
            return 127, ""

        with mock.patch.object(rm, "_run_capture", side_effect=fake_capture):
            ok, msg = rm.vram_guard_allows_claude_launch()
        self.assertFalse(ok)
        self.assertIsNotNone(msg)


if __name__ == "__main__":
    unittest.main()
