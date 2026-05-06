"""VRAM high-water tracking per model execution + automatic context-compression signaling.

Logs JSON lines to ``logs/performance_vram.jsonl``. On instability spikes (configurable),
sets an in-process compression flag and writes ``logs/context_compression_events.jsonl``
with evidence paths ranked by size (largest first = primary bloat contributors).

Grounded review hooks (:func:`bind_evidence_manifest`) supply paths for attribution.

Environment (defaults are conservative):

- ``OCTO_PERF_TRACKING`` — ``1`` to enable (default **on** when ``nvidia-smi`` works).
- ``OCTO_PERF_DISABLE`` — ``1`` to force-disable.
- ``OCTO_PERF_STABILITY_UTIL_PCT`` — VRAM util %% threshold (default ``92``).
- ``OCTO_PERF_SPIKE_DELTA_MIB`` — absolute growth MiB vs execution start (default ``3072``).
- ``OCTO_PERF_COMPRESSION_FILE_RATIO`` — multiply max selected files (default ``0.65``).
- ``OCTO_PERF_COMPRESSION_CONTENT_RATIO`` — retain this fraction of each file body (default ``0.72``).

For ``phase='ollama.review'``, :class:`infra.resource_reclaimer.ResourceReclaimer` may ``docker compose stop``
``n8n`` and ``searxng`` during the model call (see ``OCTO_RESOURCE_RECLAIMER_ENABLED``).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

_LOG = logging.getLogger(__name__)

_local = threading.local()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _logs_dir() -> Path:
    p = Path.cwd() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        _LOG.debug("performance_tracker log failed: %s", exc)


def sample_vram_nvidia() -> dict[str, float | None]:
    """Return ``used_mib``, ``total_mib``, ``util_pct`` (``None`` if unavailable)."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return {"used_mib": None, "total_mib": None, "util_pct": None}
        line = (proc.stdout or "").strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            return {"used_mib": None, "total_mib": None, "util_pct": None}
        used = float(parts[0].replace("[Not Supported]", "0") or 0)
        total = float(parts[1].replace("[Not Supported]", "0") or 0)
        util = float(parts[2].replace("[Not Supported]", "0") or 0)
        util_ratio = (100.0 * used / total) if total > 0 else None
        return {
            "used_mib": used,
            "total_mib": total,
            "util_pct": float(util_ratio) if util_ratio is not None else util,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        return {"used_mib": None, "total_mib": None, "util_pct": None}


def _stability_util_threshold() -> float:
    raw = (os.environ.get("OCTO_PERF_STABILITY_UTIL_PCT") or "").strip()
    if raw:
        try:
            return max(50.0, min(99.9, float(raw)))
        except ValueError:
            pass
    return 92.0


def _spike_delta_mib() -> float:
    raw = (os.environ.get("OCTO_PERF_SPIKE_DELTA_MIB") or "").strip()
    if raw:
        try:
            return max(256.0, float(raw))
        except ValueError:
            pass
    return 3072.0


def _file_ratio() -> float:
    raw = (os.environ.get("OCTO_PERF_COMPRESSION_FILE_RATIO") or "").strip()
    if raw:
        try:
            return max(0.2, min(1.0, float(raw)))
        except ValueError:
            pass
    return 0.65


def _content_ratio() -> float:
    raw = (os.environ.get("OCTO_PERF_COMPRESSION_CONTENT_RATIO") or "").strip()
    if raw:
        try:
            return max(0.25, min(1.0, float(raw)))
        except ValueError:
            pass
    return 0.72


@dataclass
class _Session:
    manifest: list[tuple[str, int]] = field(default_factory=list)
    compression_active: bool = False
    compression_reason: str = ""
    spike_count: int = 0


def _session() -> _Session:
    if not hasattr(_local, "session"):
        _local.session = _Session()
    return _local.session


def bind_evidence_manifest(snapshot: dict[str, Any]) -> None:
    """Register ranked-by-size paths from a grounded-review snapshot (thread-local)."""
    rows: list[tuple[str, int]] = []
    for f in snapshot.get("files") or []:
        path = getattr(f, "path", None)
        size = int(getattr(f, "size", 0) or 0)
        if path:
            rows.append((str(path), size))
    rows.sort(key=lambda x: -x[1])
    _session().manifest = rows


def clear_performance_session() -> None:
    """Reset thread-local session after a grounded review completes."""
    _local.session = _Session()


def should_compress_evidence() -> bool:
    return _session().compression_active


def compression_targets() -> dict[str, Any] | None:
    """Return trim parameters when compression is active."""
    if not _session().compression_active:
        return None
    raw = (os.environ.get("GROUNDED_REVIEW_MAX_FILES") or "12").strip()
    try:
        base_max = max(4, int(raw))
    except ValueError:
        base_max = 12
    fr = _file_ratio()
    cr = _content_ratio()
    return {
        "max_files": max(4, int(base_max * fr)),
        "content_ratio": cr,
        "reason": _session().compression_reason,
    }


def _ranked_bloat_paths(limit: int = 64) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path, size in _session().manifest[:limit]:
        out.append({"path": path, "bytes": size})
    return out


def _detect_instability(
    start: dict[str, float | None],
    peak_used: float,
    peak_util: float | None,
) -> tuple[bool, str]:
    total = start.get("total_mib")
    su = start.get("used_mib")
    if total and su is not None and peak_used is not None:
        peak_pct = 100.0 * peak_used / float(total)
        if peak_pct >= _stability_util_threshold():
            return True, f"peak_vram_util≈{peak_pct:.1f}% (>={_stability_util_threshold():.0f}%)"
    if peak_util is not None and peak_util >= _stability_util_threshold():
        return True, f"peak_util_sample≈{peak_util:.1f}%"
    if su is not None and peak_used is not None:
        if peak_used - float(su) >= _spike_delta_mib():
            return (
                True,
                f"delta_used≈{peak_used - float(su):.0f} MiB (>={_spike_delta_mib():.0f} MiB)",
            )
    return False, ""


def _emit_compression_event(reason: str, stats: dict[str, Any]) -> None:
    sess = _session()
    sess.compression_active = True
    sess.compression_reason = reason
    sess.spike_count += 1
    payload = {
        "ts": time.time(),
        "event": "context_compression",
        "reason": reason,
        "stats": stats,
        "bloat_paths_ranked": _ranked_bloat_paths(),
    }
    _append_jsonl(_logs_dir() / "context_compression_events.jsonl", payload)
    try:
        from observability.tui_bridge import append_trace_record

        append_trace_record(
            {
                "ts": time.time(),
                "kind": "control",
                "thought": f"Context compression: {reason}. Bloat paths (largest first): "
                + ", ".join(p["path"] for p in payload["bloat_paths_ranked"][:24]),
            }
        )
    except ImportError:
        pass
    try:
        if os.environ.get("OCTO_AUDIT_SQLITE", "").strip().lower() in {"1", "true", "yes", "on"}:
            from observability.audit_sqlite import record_event, get_audit_session

            sid = get_audit_session()
            if sid:
                record_event(sid, "system", payload)
    except ImportError:
        pass
    _LOG.warning("Performance: context compression triggered — %s", reason)


@contextmanager
def track_model_execution(
    *,
    model: str,
    phase: str = "ollama",
    poll_interval_sec: float = 0.35,
) -> Iterator[None]:
    """Poll VRAM during a blocking model call; log HW mark; optionally trigger compression."""
    reclaim_cm: Any = nullcontext()
    if phase == "ollama.review":
        try:
            from infra.resource_reclaimer import ResourceReclaimer

            reclaim_cm = ResourceReclaimer.pause_addon_services_for_inference()
        except ImportError:
            reclaim_cm = nullcontext()

    with reclaim_cm:
        if _truthy("OCTO_PERF_DISABLE"):
            yield
            return
        probe = sample_vram_nvidia()
        auto_gpu = probe["used_mib"] is not None
        if not auto_gpu and not _truthy("OCTO_PERF_TRACKING"):
            yield
            return
        if not auto_gpu and _truthy("OCTO_PERF_TRACKING"):
            _LOG.debug("OCTO_PERF_TRACKING set but nvidia-smi unavailable — logging NULL VRAM.")

        start = probe
        peak_used = float(start["used_mib"] or 0) if start.get("used_mib") is not None else 0.0
        peak_util = start["util_pct"]
        stop = threading.Event()

        def poll() -> None:
            nonlocal peak_used, peak_util
            while not stop.wait(poll_interval_sec):
                s = sample_vram_nvidia()
                u = s.get("used_mib")
                if u is not None:
                    peak_used = max(peak_used, float(u))
                v = s.get("util_pct")
                if v is not None:
                    peak_util = (
                        max(peak_util or 0, float(v)) if peak_util is not None else float(v)
                    )

        t = threading.Thread(target=poll, name="octo-vram-poll", daemon=True)
        if auto_gpu:
            t.start()
        t0 = time.perf_counter()
        err: str | None = None
        try:
            yield
        except BaseException as exc:
            err = str(exc)
            raise
        finally:
            stop.set()
            if auto_gpu:
                t.join(timeout=5.0)
            elapsed = time.perf_counter() - t0
            end = sample_vram_nvidia()
            hw_record = {
                "ts": time.time(),
                "phase": phase,
                "model": model,
                "duration_sec": round(elapsed, 3),
                "vram_start_used_mib": start.get("used_mib"),
                "vram_peak_used_mib": peak_used,
                "vram_end_used_mib": end.get("used_mib"),
                "vram_total_mib": start.get("total_mib"),
                "vram_peak_util_pct": peak_util,
                "high_water_mark_mib": peak_used,
                "error": err,
            }
            _append_jsonl(_logs_dir() / "performance_vram.jsonl", hw_record)

            unstable, why = (
                _detect_instability(start, peak_used, peak_util)
                if start.get("used_mib") is not None
                else (False, "")
            )
            if unstable:
                _emit_compression_event(
                    why,
                    {
                        "start_used_mib": start.get("used_mib"),
                        "peak_used_mib": peak_used,
                        "peak_util_pct": peak_util,
                        "total_mib": start.get("total_mib"),
                    },
                )


def tracking_enabled() -> bool:
    if _truthy("OCTO_PERF_DISABLE"):
        return False
    if _truthy("OCTO_PERF_TRACKING"):
        return True
    return sample_vram_nvidia()["used_mib"] is not None


class PerformanceTracker:
    """VRAM high-water logging + context-compression signaling (facade over module functions)."""

    bind_evidence_manifest = staticmethod(bind_evidence_manifest)
    clear_performance_session = staticmethod(clear_performance_session)
    compression_targets = staticmethod(compression_targets)
    sample_vram_nvidia = staticmethod(sample_vram_nvidia)
    should_compress_evidence = staticmethod(should_compress_evidence)
    track_model_execution = staticmethod(track_model_execution)
    tracking_enabled = staticmethod(tracking_enabled)


__all__ = [
    "PerformanceTracker",
    "bind_evidence_manifest",
    "clear_performance_session",
    "compression_targets",
    "sample_vram_nvidia",
    "should_compress_evidence",
    "track_model_execution",
    "tracking_enabled",
]
