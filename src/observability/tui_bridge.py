"""Bridge OpenTelemetry / agent steps to the Fopoon Textual dashboard (trace log + cooperative stop).

Environment:

- ``OCTO_TUI_TRACE_LOG`` — append-only JSONL file for dashboard tail (default ``.local/octo_fopoon_trace.log`` under cwd).
- ``OCTO_AGENT_STOP_FLAG`` — path touched when the dashboard requests a graceful stop (default ``.local/octo_agent_stop_request``).

Cooperating code should call :func:`agent_stop_requested` before starting heavy work (e.g. each LLM call).
Stopping via flag avoids sending SIGKILL to Redis clients and lets writers flush session keys.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


_LOG_LOCK = threading.Lock()
_DEFAULT_REL_LOG = Path(".local/octo_fopoon_trace.jsonl")
_DEFAULT_REL_STOP = Path(".local/octo_agent_stop_request")


class AgentStopRequested(RuntimeError):
    """Raised when the operator requested a graceful stop from the Fopoon dashboard."""


def _repo_root_guess() -> Path:
    return Path.cwd()


def trace_log_path() -> Path:
    raw = (os.environ.get("OCTO_TUI_TRACE_LOG") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (_repo_root_guess() / _DEFAULT_REL_LOG).resolve()


def stop_flag_path() -> Path:
    raw = (os.environ.get("OCTO_AGENT_STOP_FLAG") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (_repo_root_guess() / _DEFAULT_REL_STOP).resolve()


def ensure_trace_log_parent() -> None:
    p = trace_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)


def append_trace_record(record: dict[str, Any]) -> None:
    """Append one JSON line for the dashboard log (thread-safe)."""
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    path = trace_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass
    try:
        from observability.audit_sqlite import mirror_from_trace_record

        mirror_from_trace_record(record)
    except ImportError:
        pass


def log_llm_step(
    *,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    latency_ms: float,
    preview: str | None = None,
    error: str | None = None,
) -> None:
    append_trace_record(
        {
            "ts": time.time(),
            "kind": "llm",
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": round(latency_ms, 2),
            "thought": (preview or "")[:4000],
            "error": error,
        }
    )


def log_tool_step(
    *,
    tool: str,
    latency_ms: float,
    detail: str | None = None,
    error: str | None = None,
) -> None:
    append_trace_record(
        {
            "ts": time.time(),
            "kind": "tool",
            "tool": tool,
            "latency_ms": round(latency_ms, 2),
            "thought": (detail or "")[:4000],
            "error": error,
        }
    )


def log_dashboard_message(message: str) -> None:
    append_trace_record({"ts": time.time(), "kind": "dashboard", "thought": message[:4000]})


def agent_stop_requested() -> bool:
    return stop_flag_path().is_file()


def request_agent_stop(reason: str = "dashboard_kill") -> Path:
    """Create the stop flag file (idempotent). Parent dirs created."""
    path = stop_flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": time.time(), "reason": reason}
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass
    append_trace_record(
        {
            "ts": time.time(),
            "kind": "control",
            "thought": f"Graceful stop requested ({reason}). Cooperating loops should exit cleanly.",
        }
    )
    return path


def clear_agent_stop() -> None:
    """Remove stop flag so new agent runs can proceed."""
    path = stop_flag_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def check_agent_stop_or_raise() -> None:
    if agent_stop_requested():
        raise AgentStopRequested(
            "Operator requested stop via Fopoon dashboard (stop flag present)."
        )
