"""Tests for OpenTelemetry TracingManager (no network export in default tests)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class ObservabilityTracerTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_OTEL_DISABLED",
            "OCTO_OTEL_ENDPOINT",
            "OCTO_OTEL_MAX_BODY_CHARS",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "OTEL_SERVICE_NAME",
        ):
            os.environ.pop(k, None)

    def test_truncate_respects_max(self) -> None:
        from observability import tracer as t

        os.environ["OCTO_OTEL_MAX_BODY_CHARS"] = "100"
        long = "x" * 500
        out = t._truncate(long)
        self.assertLessEqual(len(out), 130)
        self.assertIn("truncated", out)

    def test_no_endpoint_yields_inactive_export(self) -> None:
        from observability.tracer import TracingManager, get_tracing_manager

        os.environ.pop("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", None)
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        os.environ.pop("OCTO_OTEL_ENDPOINT", None)
        m = TracingManager()
        m.configure()
        self.assertFalse(m.enabled)

    def test_trace_llm_call_records_without_crash(self) -> None:
        from observability.tracer import trace_llm_call

        def call() -> tuple[str, dict]:
            return "ok", {"prompt_eval_count": 1, "eval_count": 2}

        text, meta = trace_llm_call(
            model="m",
            provider="ollama",
            ollama_base_url="http://127.0.0.1:11434",
            prompt="hi",
            call=call,
        )
        self.assertEqual(text, "ok")
        self.assertEqual(meta.get("prompt_eval_count"), 1)

if __name__ == "__main__":
    unittest.main()
