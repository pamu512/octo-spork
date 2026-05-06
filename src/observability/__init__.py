"""OpenTelemetry-backed tracing for LLM and tool execution (Phoenix / LangSmith / OTLP collector)."""

from observability.tracer import TracingManager, get_tracing_manager

__all__ = ["TracingManager", "get_tracing_manager"]
