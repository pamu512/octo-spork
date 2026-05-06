"""Tests for agent_guard circuit breaker (no real SIGKILL)."""

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


class CircuitBreakerTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in ("OCTO_CB_MAX_STEPS", "OCTO_CB_TERMINAL_MARKERS"):
            os.environ.pop(k, None)

    def test_terminal_resets_depth(self) -> None:
        from agent_guard.circuit_breaker import CircuitBreakerConfig, ExecutionDepthCircuitBreaker

        cfg = CircuitBreakerConfig(max_steps_without_terminal=5, terminal_markers=("OK",))
        cb = ExecutionDepthCircuitBreaker(cfg)
        cb.observe_chunk({"x": 1})
        cb.observe_chunk({"msg": "OK terminal"})
        self.assertEqual(cb.steps_since_terminal, 0)
        cb.observe_chunk({"x": 2})
        self.assertEqual(cb.steps_since_terminal, 1)

    def test_trip_writes_report_and_calls_kill(self) -> None:
        from agent_guard import circuit_breaker as cb_mod
        from agent_guard.circuit_breaker import CircuitBreakerConfig, ExecutionDepthCircuitBreaker

        fd, tmpp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        report = Path(tmpp)
        cfg = CircuitBreakerConfig(max_steps_without_terminal=2, crash_report_path=report)
        br = ExecutionDepthCircuitBreaker(cfg)

        with patch.object(cb_mod, "_terminate_this_process") as kill_mock:
            br.observe_chunk({"n": 1})
            br.observe_chunk({"n": 2})
            kill_mock.assert_called_once()

        self.assertTrue(report.is_file())
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertIn("Exceeded 2 steps without terminal marker", data.get("reason", ""))
        self.assertIn("stack_dump", data)
        report.unlink(missing_ok=True)

    def test_guard_langgraph_wraps_stream(self) -> None:
        from agent_guard.circuit_breaker import CircuitBreakerConfig, guard_langgraph

        inner = MagicMock()
        inner.stream.return_value = iter([{"a": 1}, {"Response": "done"}])

        cfg = CircuitBreakerConfig(max_steps_without_terminal=10)
        wrapped = guard_langgraph(inner, config=cfg)

        out = list(wrapped.stream({"x": 1}))
        self.assertEqual(len(out), 2)

    def test_iter_with_circuit(self) -> None:
        from agent_guard.circuit_breaker import CircuitBreakerConfig, ExecutionDepthCircuitBreaker, iter_with_circuit

        cfg = CircuitBreakerConfig(max_steps_without_terminal=100)
        br = ExecutionDepthCircuitBreaker(cfg)
        gen = iter_with_circuit(iter(range(3)), breaker=br)
        self.assertEqual(list(gen), [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
