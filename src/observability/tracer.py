"""TracingManager: OpenTelemetry spans for LLM calls and tools (Phoenix / LangSmith / OTLP collector).

Enable when ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` or
``OCTO_OTEL_ENDPOINT`` is set (unless ``OCTO_OTEL_DISABLED=1``).

Examples:

- **Arize Phoenix** (local): ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:6006/v1/traces``
- **OTEL Collector** (Docker): ``OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces``
- **LangSmith OTLP** (when exposed): set the HTTP OTLP traces URL from LangSmith docs.

Raw prompts/responses are truncated (``OCTO_OTEL_MAX_BODY_CHARS``, default 16384) and stored as span events.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

_LOG = logging.getLogger(__name__)

_HAS_OTEL = False
try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import Status, StatusCode

    _HAS_OTEL = True
except ImportError:
    otel_trace = None  # type: ignore[assignment]
    Status = None  # type: ignore[assignment]
    StatusCode = None  # type: ignore[assignment]


def _truthy_disabled() -> bool:
    return os.environ.get("OCTO_OTEL_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _max_body_chars() -> int:
    raw = (os.environ.get("OCTO_OTEL_MAX_BODY_CHARS") or "").strip()
    if raw.isdigit():
        return max(64, min(1_000_000, int(raw)))
    return 16_384


def _truncate(text: str | None) -> str:
    if not text:
        return ""
    max_c = _max_body_chars()
    t = str(text)
    if len(t) <= max_c:
        return t
    return t[:max_c] + "\n… [truncated for OTEL export]"


def _resolve_otlp_traces_endpoint() -> str | None:
    """Return full HTTP OTLP traces URL (…/v1/traces)."""
    explicit = (os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or "").strip()
    if explicit:
        return explicit
    base = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    if base:
        b = base.rstrip("/")
        if "/v1/traces" in b:
            return b
        return b + "/v1/traces"
    octo = (os.environ.get("OCTO_OTEL_ENDPOINT") or "").strip()
    if octo:
        o = octo.rstrip("/")
        if "/v1/traces" in o:
            return o
        return o + "/v1/traces"
    return None


def _install_exporter(endpoint: str) -> None:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider = otel_trace.get_tracer_provider()
    if hasattr(provider, "add_span_processor"):
        provider.add_span_processor(BatchSpanProcessor(exporter))


class TracingManager:
    """Configure OTLP HTTP export and emit spans for LLM / tool calls."""

    def __init__(self) -> None:
        self._configured = False
        self._active = False
        self._tracer: Any = None

    def configure(self) -> None:
        if self._configured:
            return
        self._configured = True
        if not _HAS_OTEL:
            _LOG.debug("OpenTelemetry SDK not installed; tracing is a no-op.")
            return
        if _truthy_disabled():
            _LOG.debug("OCTO_OTEL_DISABLED set; tracing skipped.")
            self._tracer = otel_trace.get_tracer(__name__)
            return
        endpoint = _resolve_otlp_traces_endpoint()
        if not endpoint:
            _LOG.debug("No OTLP traces endpoint env set; tracing skipped.")
            self._tracer = otel_trace.get_tracer(__name__)
            return
        try:
            service_name = (os.environ.get("OTEL_SERVICE_NAME") or "octo-spork").strip() or "octo-spork"
            resource = Resource.create({"service.name": service_name})
            provider = TracerProvider(resource=resource)
            otel_trace.set_tracer_provider(provider)
            _install_exporter(endpoint)
            self._tracer = otel_trace.get_tracer(
                "octo-spork",
                schema_url="https://opentelemetry.io/schemas/1.27.0",
            )
            self._active = True
            _LOG.info("OpenTelemetry traces export to %s", endpoint)
        except Exception as exc:
            _LOG.warning("Could not configure OpenTelemetry export: %s", exc)
            self._tracer = otel_trace.get_tracer(__name__)

    @property
    def enabled(self) -> bool:
        self.configure()
        return bool(self._active and self._tracer)

    @contextmanager
    def llm_span(
        self,
        *,
        model: str,
        provider: str = "ollama",
        endpoint_url: str | None = None,
        prompt: str | None = None,
        span_name: str = "gen_ai.chat",
    ) -> Iterator[Any]:
        """Span around an LLM generate/chat call; record latency, tokens, prompt/response, exceptions."""
        self.configure()
        if self._tracer is None:
            yield None
            return
        attrs: dict[str, Any] = {
            "gen_ai.system": provider,
            "gen_ai.request.model": model,
        }
        if endpoint_url:
            attrs["octo.llm.endpoint"] = endpoint_url[:512]
        start = time.perf_counter()
        with self._tracer.start_as_current_span(span_name, attributes=attrs) as span:
            if prompt and span is not None:
                span.add_event("gen_ai.user.message", {"content": _truncate(prompt)})
            try:
                yield span
            except BaseException as exc:
                if span is not None and Status is not None:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                if span is not None:
                    elapsed_ms = (time.perf_counter() - start) * 1000.0
                    span.set_attribute("octo.latency_ms", round(elapsed_ms, 3))

    def record_llm_result(
        self,
        span: Any,
        *,
        completion: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        extra_attrs: Mapping[str, Any] | None = None,
    ) -> None:
        if span is None:
            return
        if completion is not None:
            span.add_event("gen_ai.assistant.message", {"content": _truncate(completion)})
        if prompt_tokens is not None:
            span.set_attribute("gen_ai.usage.prompt_tokens", int(prompt_tokens))
        if completion_tokens is not None:
            span.set_attribute("gen_ai.usage.completion_tokens", int(completion_tokens))
        if prompt_tokens is not None and completion_tokens is not None:
            span.set_attribute(
                "gen_ai.usage.total_tokens",
                int(prompt_tokens) + int(completion_tokens),
            )
        if extra_attrs:
            for k, v in extra_attrs.items():
                if v is not None:
                    span.set_attribute(str(k)[:256], str(v)[:4096])

    @contextmanager
    def tool_span(
        self,
        tool_name: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        span_name: str | None = None,
    ) -> Iterator[Any]:
        """Span around a tool (SearXNG, git, etc.)."""
        self.configure()
        nm = span_name or f"tool.{tool_name}"
        attrs: dict[str, Any] = {"octo.tool.name": tool_name}
        if attributes:
            for k, v in attributes.items():
                if v is not None:
                    attrs[f"octo.tool.{k}"] = str(v)[:4096]
        start = time.perf_counter()
        err_note: str | None = None
        try:
            if self._tracer is None:
                try:
                    yield None
                except BaseException as exc:
                    err_note = str(exc)
                    raise
            else:
                with self._tracer.start_as_current_span(nm, attributes=attrs) as span:
                    try:
                        yield span
                    except BaseException as exc:
                        err_note = str(exc)
                        if span is not None and Status is not None:
                            span.record_exception(exc)
                            span.set_status(Status(StatusCode.ERROR, str(exc)))
                        raise
                    finally:
                        if span is not None:
                            elapsed_ms = (time.perf_counter() - start) * 1000.0
                            span.set_attribute("octo.latency_ms", round(elapsed_ms, 3))
        except BaseException as exc:
            if err_note is None:
                err_note = str(exc)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            try:
                from observability.tui_bridge import log_tool_step

                detail_parts: list[str] = []
                if attributes:
                    for k, v in list(attributes.items())[:12]:
                        detail_parts.append(f"{k}={str(v)[:160]}")
                log_tool_step(
                    tool=tool_name,
                    latency_ms=elapsed_ms,
                    detail="; ".join(detail_parts) if detail_parts else None,
                    error=err_note,
                )
            except ImportError:
                pass


_mgr: TracingManager | None = None


def get_tracing_manager() -> TracingManager:
    global _mgr
    if _mgr is None:
        _mgr = TracingManager()
    return _mgr


def trace_llm_call(
    *,
    model: str,
    provider: str,
    ollama_base_url: str,
    prompt: str,
    call: Callable[[], tuple[str, dict[str, Any]]],
) -> tuple[str, dict[str, Any]]:
    """Run ``call()`` inside an LLM span; ``call`` must return ``(text, meta_dict)`` like Ollama."""
    mgr = get_tracing_manager()
    endpoint = ollama_base_url.rstrip("/") + "/api/generate"
    t0 = time.perf_counter()
    try:
        with mgr.llm_span(
            model=model,
            provider=provider,
            endpoint_url=endpoint,
            prompt=prompt,
        ) as span:
            text, meta = call()
            pe = meta.get("prompt_eval_count")
            ev = meta.get("eval_count")
            mgr.record_llm_result(
                span,
                completion=text,
                prompt_tokens=int(pe) if pe is not None else None,
                completion_tokens=int(ev) if ev is not None else None,
                extra_attrs={
                    "ollama.total_duration_ns": meta.get("total_duration"),
                    "ollama.load_duration_ns": meta.get("load_duration"),
                },
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        try:
            from observability.tui_bridge import log_llm_step

            pe2 = meta.get("prompt_eval_count")
            ev2 = meta.get("eval_count")
            log_llm_step(
                model=model,
                prompt_tokens=int(pe2) if pe2 is not None else None,
                completion_tokens=int(ev2) if ev2 is not None else None,
                latency_ms=elapsed_ms,
                preview=str(text)[:1200],
            )
        except ImportError:
            pass
        return text, meta
    except BaseException as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        try:
            from observability.tui_bridge import log_llm_step

            log_llm_step(
                model=model,
                prompt_tokens=None,
                completion_tokens=None,
                latency_ms=elapsed_ms,
                preview=None,
                error=str(exc),
            )
        except ImportError:
            pass
        raise


def trace_tool(
    tool_name: str,
    fn: Callable[[], Any],
    *,
    attributes: Mapping[str, Any] | None = None,
) -> Any:
    """Execute ``fn`` inside a tool span (latency + exceptions)."""
    mgr = get_tracing_manager()
    with mgr.tool_span(tool_name, attributes=attributes):
        return fn()


__all__ = [
    "TracingManager",
    "get_tracing_manager",
    "trace_llm_call",
    "trace_tool",
]
