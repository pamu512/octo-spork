"""Apple Silicon unified-memory pressure probe via ``system_profiler SPDisplaysDataType``.

When Graphics reports **Unified Memory** pressure **High**, downstream Ollama callers should prefer a
small quantized coder (default ``qwen2.5-coder:7b``) instead of 14B/32B-class tags to reduce swap.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
from typing import Any, Callable

_LOG = logging.getLogger(__name__)

_DEFAULT_PRESSURE_MODEL = "qwen2.5-coder:7b"

_UNIFIED_LINE = re.compile(r"Unified\s+Memory", re.IGNORECASE)
_PRESSURE_LINE = re.compile(r"Pressure\s*:\s*(High|Normal|Fair|Low)", re.IGNORECASE)


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_tag(tag: str) -> str:
    return (tag or "").strip()


def _model_available_locally(model_name: str, local_tags: list[str]) -> bool:
    want = _normalize_tag(model_name)
    if not want:
        return False
    local = [_normalize_tag(x) for x in local_tags if x]
    if want in local:
        return True
    base = want.split(":")[0].lower()
    for t in local:
        if t.split(":")[0].lower() == base:
            return True
    return False


def parse_unified_memory_pressure_level(spdisplays_text: str) -> str | None:
    """
    Extract Unified Memory pressure level from ``system_profiler SPDisplaysDataType`` text.

    Returns lower-case ``high`` / ``normal`` / ``fair`` / ``low``, or ``None`` if not present.
    """
    lines = (spdisplays_text or "").replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if not _UNIFIED_LINE.search(line):
            continue
        window = "\n".join(lines[i : i + 40])
        m = _PRESSURE_LINE.search(window)
        if m:
            return m.group(1).lower()
        one = _PRESSURE_LINE.search(line)
        if one:
            return one.group(1).lower()
    return None


def _pressure_from_json(data: Any) -> str | None:
    """Best-effort: future / alternate macOS JSON keys for unified-memory pressure."""

    def walk(obj: Any, depth: int = 0) -> str | None:
        if depth > 48:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                ks = str(k).lower()
                if "unified" in ks and "pressure" in ks and isinstance(v, str):
                    lv = v.strip().lower()
                    if lv in {"high", "normal", "fair", "low"}:
                        return lv
                r = walk(v, depth + 1)
                if r:
                    return r
        elif isinstance(obj, list):
            for it in obj:
                r = walk(it, depth + 1)
                if r:
                    return r
        return None

    return walk(data)


def sample_spdisplays_text(
    *,
    subprocess_run: Callable[..., Any] | None = None,
) -> str:
    run = subprocess_run or subprocess.run
    try:
        proc = run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("OCTO_SYSTEM_PROFILER_TIMEOUT_SEC", "25")),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        _LOG.debug("VRAMPressureMonitor: system_profiler text failed: %s", exc)
        return ""
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def sample_spdisplays_json(
    *,
    subprocess_run: Callable[..., Any] | None = None,
) -> Any | None:
    run = subprocess_run or subprocess.run
    try:
        proc = run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=float(os.environ.get("OCTO_SYSTEM_PROFILER_TIMEOUT_SEC", "25")),
            check=False,
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return None
        return json.loads(proc.stdout)
    except (
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ) as exc:
        _LOG.debug("VRAMPressureMonitor: system_profiler -json failed: %s", exc)
        return None


class VRAMPressureMonitor:
    """Reads ``SPDisplaysDataType`` and reports whether unified-memory pressure is **High**."""

    def unified_memory_pressure_is_high(
        self,
        *,
        subprocess_run: Callable[..., Any] | None = None,
    ) -> bool:
        if platform.system() != "Darwin":
            return False
        if _truthy("OCTO_VRAM_PRESSURE_MONITOR_DISABLE"):
            return False

        text = sample_spdisplays_text(subprocess_run=subprocess_run)
        level = parse_unified_memory_pressure_level(text)
        if level == "high":
            return True
        if level is not None:
            return False

        data = sample_spdisplays_json(subprocess_run=subprocess_run)
        if data is not None:
            jl = _pressure_from_json(data)
            if jl == "high":
                return True
            if jl is not None:
                return False

        if _truthy("OCTO_VRAM_PRESSURE_ASSUME_HIGH_WHEN_UNKNOWN"):
            _LOG.warning(
                "VRAMPressureMonitor: no Unified Memory pressure in SPDisplaysDataType; "
                "assuming High (OCTO_VRAM_PRESSURE_ASSUME_HIGH_WHEN_UNKNOWN=1)"
            )
            return True
        return False


def model_is_large_for_pressure_override(model_name: str) -> bool:
    """True for ~14B+ class tags (rough heuristic — matches ``ollama_guard.estimate`` patterns)."""
    try:
        from ollama_guard.estimate import infer_params_from_name

        p = infer_params_from_name(model_name)
        if p is not None:
            return p >= 9.0
    except ImportError:
        pass
    low = model_name.lower()
    m = re.search(r"[:\-](\d+)b\b", low)
    if m:
        try:
            return int(m.group(1)) >= 9
        except ValueError:
            pass
    return any(x in low for x in (":14b", ":32b", ":70b", ":72b", "-14b", "-32b"))


def resolve_pressure_fallback_model(local_tags: list[str]) -> str | None:
    """Pick ``OCTO_UNIFIED_MEMORY_PRESSURE_MODEL`` or closest local ``qwen2.5-coder`` 7B tag."""
    preferred = (
        (os.environ.get("OCTO_UNIFIED_MEMORY_PRESSURE_MODEL") or _DEFAULT_PRESSURE_MODEL).strip()
        or _DEFAULT_PRESSURE_MODEL
    )
    if _model_available_locally(preferred, local_tags):
        return preferred
    base = preferred.split(":")[0].lower()
    best: str | None = None
    for t in local_tags:
        tl = t.lower()
        if not tl.startswith(base + ":"):
            continue
        if re.search(r"7", tl):
            best = t
            break
        if best is None:
            best = t
    if best:
        return best
    try:
        from local_ai_stack.model_fallback import pick_small_coder_fallback

        return pick_small_coder_fallback(local_tags)
    except ImportError:
        return None


def apply_unified_memory_pressure_override(
    candidate_model: str,
    local_tags: list[str],
) -> tuple[str, str | None]:
    """
    If unified-memory pressure is **High** and *candidate_model* is large-class, return a small coder tag.

    Returns ``(model, reason)`` where *reason* explains the swap for logs.
    """
    if not candidate_model.strip():
        return candidate_model, None
    if not model_is_large_for_pressure_override(candidate_model):
        return candidate_model, None

    mon = VRAMPressureMonitor()
    if not mon.unified_memory_pressure_is_high():
        return candidate_model, None

    fb = resolve_pressure_fallback_model(local_tags)
    if not fb:
        _LOG.warning(
            "VRAMPressureMonitor: unified memory pressure High but no small fallback in "
            "`ollama list` (want %r); keeping %r",
            (os.environ.get("OCTO_UNIFIED_MEMORY_PRESSURE_MODEL") or _DEFAULT_PRESSURE_MODEL),
            candidate_model,
        )
        return candidate_model, None

    if fb == candidate_model.strip():
        return candidate_model, None

    _LOG.warning(
        "VRAMPressureMonitor: Unified Memory pressure High — using %r instead of large model %r",
        fb,
        candidate_model,
    )
    return fb, "unified_memory_pressure_high"


monitor = VRAMPressureMonitor()
