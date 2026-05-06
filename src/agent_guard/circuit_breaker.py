"""Circuit breaker for LangGraph-style agent loops: cap execution depth without terminal output.

Wrap ``CompiledGraph.stream`` (or any per-step iterable). Each yielded chunk counts as one execution
step unless the chunk serializes to text containing a terminal marker (default substrings ``Response``
or ``Fix``). After ``max_steps_without_terminal`` (default **15**), the breaker writes
``logs/crash_report.json`` and terminates the process (SIGKILL / hard exit) so runaway graphs cannot
spin overnight.

Environment (optional):

- ``OCTO_CB_MAX_STEPS`` — override step budget (default ``15``).
- ``OCTO_CB_TERMINAL_MARKERS`` — comma-separated substrings that reset the depth counter.
- ``OCTO_CB_TERMINAL_REGEX`` — if set, takes precedence (one match → terminal).
- ``OCTO_CB_CASE_SENSITIVE`` — ``1`` for case-sensitive marker / regex matching.
- ``OCTO_CB_DOCKER_KILL_NAME`` — optional Docker container name for ``docker kill`` from the host
  before local SIGKILL (best-effort).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

_LOG = logging.getLogger(__name__)


def _workspace_logs_dir() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        root = Path(raw).expanduser().resolve()
    else:
        root = Path.cwd()
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _crash_report_path() -> Path:
    override = (os.environ.get("OCTO_CB_CRASH_REPORT_PATH") or "").strip()
    if override:
        p = Path(override).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    return _workspace_logs_dir() / "crash_report.json"


def _max_steps_default() -> int:
    raw = (os.environ.get("OCTO_CB_MAX_STEPS") or "15").strip()
    try:
        return max(1, min(10_000, int(raw)))
    except ValueError:
        return 15


def _default_markers() -> tuple[str, ...]:
    raw = (os.environ.get("OCTO_CB_TERMINAL_MARKERS") or "").strip()
    if raw:
        parts = tuple(s.strip() for s in raw.split(",") if s.strip())
        return parts if parts else ("Response", "Fix")
    return ("Response", "Fix")


def _serialize_chunk(chunk: Any, *, limit: int = 24_000) -> str:
    if isinstance(chunk, (bytes, bytearray)):
        return bytes(chunk[:limit]).decode("utf-8", errors="replace")
    if isinstance(chunk, str):
        return chunk[:limit]
    try:
        return json.dumps(chunk, ensure_ascii=False, default=str)[:limit]
    except TypeError:
        return repr(chunk)[:limit]


def _collect_stack_dump() -> dict[str, Any]:
    main_stack = "".join(traceback.format_stack(limit=40))
    threads_out: list[dict[str, Any]] = []
    try:
        import threading

        for th in threading.enumerate():
            frame = sys._current_frames().get(th.ident) if hasattr(sys, "_current_frames") else None
            if frame is None:
                continue
            threads_out.append(
                {
                    "name": th.name,
                    "ident": th.ident,
                    "stack": "".join(traceback.format_stack(frame, limit=24)),
                }
            )
    except Exception as exc:
        threads_out.append({"error": str(exc)})
    return {"main_stack": main_stack, "threads": threads_out}


def _docker_kill_best_effort(name: str) -> None:
    import shutil
    import subprocess

    exe = shutil.which("docker")
    if not exe:
        return
    try:
        subprocess.run(
            [exe, "kill", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _terminate_this_process() -> None:
    """Hard-stop current PID (container main process when PID 1 child)."""
    docker_name = (os.environ.get("OCTO_CB_DOCKER_KILL_NAME") or "").strip()
    if docker_name:
        _docker_kill_best_effort(docker_name)
    try:
        os.kill(os.getpid(), signal.SIGKILL)
    except (AttributeError, OSError):
        os._exit(137)


@dataclass
class CircuitBreakerConfig:
    max_steps_without_terminal: int = field(default_factory=_max_steps_default)
    terminal_markers: Sequence[str] = field(default_factory=_default_markers)
    terminal_regex: str | None = None
    case_sensitive: bool = False
    crash_report_path: Path | None = None

    def __post_init__(self) -> None:
        rx = (os.environ.get("OCTO_CB_TERMINAL_REGEX") or "").strip()
        if rx:
            self.terminal_regex = rx
        cs = (os.environ.get("OCTO_CB_CASE_SENSITIVE") or "").strip().lower()
        if cs in {"1", "true", "yes", "on"}:
            self.case_sensitive = True


class ExecutionDepthCircuitBreaker:
    """Counts streaming steps; trips when no terminal marker appears within the budget."""

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._cfg = config or CircuitBreakerConfig()
        self._compiled_rx: re.Pattern[str] | None = None
        if self._cfg.terminal_regex:
            flags = 0 if self._cfg.case_sensitive else re.IGNORECASE
            self._compiled_rx = re.compile(self._cfg.terminal_regex, flags)
        self._steps_since_terminal = 0
        self._last_chunks: list[str] = []

    @property
    def steps_since_terminal(self) -> int:
        return self._steps_since_terminal

    def reset(self) -> None:
        self._steps_since_terminal = 0
        self._last_chunks.clear()

    def _is_terminal_text(self, text: str) -> bool:
        if self._compiled_rx is not None:
            return bool(self._compiled_rx.search(text))
        hay = text if self._cfg.case_sensitive else text.lower()
        for m in self._cfg.terminal_markers:
            needle = m if self._cfg.case_sensitive else m.lower()
            if needle in hay:
                return True
        return False

    def observe_chunk(self, chunk: Any, *, preview_limit: int = 4000) -> None:
        """Record one LangGraph stream event / loop iteration."""
        preview = _serialize_chunk(chunk, limit=preview_limit)
        self._last_chunks.append(preview[:800])
        if len(self._last_chunks) > 32:
            self._last_chunks.pop(0)

        if self._is_terminal_text(preview):
            self._steps_since_terminal = 0
            return

        self._steps_since_terminal += 1
        if self._steps_since_terminal >= self._cfg.max_steps_without_terminal:
            self._trip(f"Exceeded {self._cfg.max_steps_without_terminal} steps without terminal marker")

    def _trip(self, reason: str) -> None:
        report_path = self._cfg.crash_report_path or _crash_report_path()
        payload: dict[str, Any] = {
            "version": 1,
            "reason": reason,
            "circuit": "ExecutionDepthCircuitBreaker",
            "max_steps_without_terminal": self._cfg.max_steps_without_terminal,
            "steps_since_terminal": self._steps_since_terminal,
            "terminal_markers": list(self._cfg.terminal_markers),
            "terminal_regex": self._cfg.terminal_regex,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "stack_dump": _collect_stack_dump(),
            "last_chunk_previews": list(self._last_chunks),
        }
        try:
            report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            _LOG.error("Circuit breaker tripped — wrote %s", report_path)
        except OSError as exc:
            _LOG.error("Circuit breaker could not write crash report: %s", exc)
        _terminate_this_process()


class LangGraphCircuitWrapper:
    """Thin facade around a compiled LangGraph object with guarded ``stream`` / ``invoke``."""

    def __init__(self, compiled: Any, breaker: ExecutionDepthCircuitBreaker) -> None:
        self._compiled = compiled
        self._breaker = breaker

    def stream(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        inner = getattr(self._compiled, "stream", None)
        if inner is None or not callable(inner):
            raise TypeError("wrapped object has no callable stream()")
        self._breaker.reset()
        for chunk in inner(*args, **kwargs):
            self._breaker.observe_chunk(chunk)
            yield chunk

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        """Use streaming internally so per-step depth is visible."""
        if hasattr(self._compiled, "stream"):
            last: Any = None
            for chunk in self.stream(*args, **kwargs):
                last = chunk
            return last
        invoke_fn = getattr(self._compiled, "invoke", None)
        if invoke_fn is None or not callable(invoke_fn):
            raise TypeError("wrapped object has no stream() or invoke()")
        self._breaker.reset()
        result = invoke_fn(*args, **kwargs)
        self._breaker.observe_chunk(result)
        return result


def guard_langgraph(compiled: Any, *, config: CircuitBreakerConfig | None = None) -> LangGraphCircuitWrapper:
    """Wrap a compiled LangGraph graph returned by ``builder.compile()``."""
    return LangGraphCircuitWrapper(compiled, ExecutionDepthCircuitBreaker(config))


def iter_with_circuit(
    events: Iterator[Any],
    breaker: ExecutionDepthCircuitBreaker | None = None,
) -> Iterator[Any]:
    """Wrap any iterator (e.g. ``compiled.stream(...)``) with execution-depth accounting."""
    cb = breaker or ExecutionDepthCircuitBreaker()
    cb.reset()
    for chunk in events:
        cb.observe_chunk(chunk)
        yield chunk
