"""Tests for observability TUI bridge (stop flag + trace log)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class TuiBridgeTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in ("OCTO_TUI_TRACE_LOG", "OCTO_AGENT_STOP_FLAG"):
            os.environ.pop(k, None)

    def test_stop_request_roundtrip(self) -> None:
        from observability.tui_bridge import (
            agent_stop_requested,
            clear_agent_stop,
            request_agent_stop,
        )

        with tempfile.TemporaryDirectory() as td:
            os.environ["OCTO_AGENT_STOP_FLAG"] = str(Path(td) / "stop.json")
            self.assertFalse(agent_stop_requested())
            request_agent_stop(reason="test")
            self.assertTrue(agent_stop_requested())
            clear_agent_stop()
            self.assertFalse(agent_stop_requested())

    def test_trace_append(self) -> None:
        from observability import tui_bridge as tb

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            os.environ["OCTO_TUI_TRACE_LOG"] = str(p)
            tb.append_trace_record({"kind": "dashboard", "thought": "hello"})
            txt = p.read_text(encoding="utf-8")
            self.assertIn("hello", txt)


if __name__ == "__main__":
    unittest.main()
