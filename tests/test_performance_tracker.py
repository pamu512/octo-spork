"""Tests for VRAM performance tracker."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class PerformanceTrackerTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_PERF_DISABLE",
            "OCTO_PERF_TRACKING",
            "OCTO_PERF_STABILITY_UTIL_PCT",
            "OCTO_PERF_SPIKE_DELTA_MIB",
        ):
            os.environ.pop(k, None)

    def test_compression_trigger_on_high_util(self) -> None:
        import time

        from observability import performance_tracker as pt

        with tempfile.TemporaryDirectory() as td:
            old = os.getcwd()
            try:
                os.chdir(td)
                os.environ["OCTO_PERF_TRACKING"] = "1"
                os.environ["OCTO_PERF_STABILITY_UTIL_PCT"] = "50"
                pt.clear_performance_session()
                pt.bind_evidence_manifest(
                    {
                        "files": [
                            mock.Mock(path="a.py", size=100),
                            mock.Mock(path="b.py", size=9000),
                        ]
                    }
                )
                state = {"i": 0}

                def fake_sample() -> dict:
                    state["i"] += 1
                    if state["i"] == 1:
                        return {"used_mib": 1000.0, "total_mib": 10000.0, "util_pct": 10.0}
                    return {"used_mib": 9500.0, "total_mib": 10000.0, "util_pct": 95.0}

                with mock.patch.object(pt, "sample_vram_nvidia", side_effect=fake_sample):
                    with pt.track_model_execution(model="m", phase="test", poll_interval_sec=0.01):
                        time.sleep(0.06)

                self.assertTrue(pt.should_compress_evidence())
                log = Path(td) / "logs" / "context_compression_events.jsonl"
                self.assertTrue(log.is_file())
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()
