"""Predictive VRAM governor: probe GPU memory, estimate Ollama model footprint, gate heavy runs.

NVIDIA uses NVML via the ``pynvml`` module shipped with ``nvidia-ml-py`` (already in requirements).
Apple Silicon / macOS uses ``system_profiler SPDisplaysDataType -json`` for VRAM totals and
``psutil.virtual_memory()`` as a unified-memory availability proxy.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

_LOG = logging.getLogger(__name__)


def _min_free_ratio() -> float:
    raw = (os.environ.get("OCTO_VRAM_MIN_FREE_RATIO") or "0.20").strip()
    try:
        return max(0.01, min(0.95, float(raw)))
    except ValueError:
        return 0.20


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _post_json(base: str, path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any] | None:
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _get_json(base: str, path: str, *, timeout: float) -> dict[str, Any] | None:
    url = f"{base.rstrip('/')}{path}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


@dataclass(frozen=True)
class MemorySnapshot:
    """Unified VRAM / GPU-memory view for scheduling."""

    free_mib: float
    total_mib: float
    free_ratio: float
    backend: str
    detail: str


def _query_nvml_mib() -> MemorySnapshot | None:
    """Use NVML (``nvidia-ml-py`` → ``import pynvml``) for NVIDIA GPUs."""
    try:
        import pynvml  # type: ignore[import-untyped]
    except ImportError:
        _LOG.debug("NVML not available (install nvidia-ml-py)")
        return None

    try:
        pynvml.nvmlInit()
    except Exception as exc:
        _LOG.debug("nvmlInit failed: %s", exc)
        return None

    try:
        n = int(pynvml.nvmlDeviceGetCount())
        total_b = 0
        free_b = 0
        for i in range(n):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            info = pynvml.nvmlDeviceGetMemoryInfo(h)
            total_b += int(info.total)
            free_b += int(info.free)
        mib_free = free_b / (1024.0 * 1024.0)
        mib_total = total_b / (1024.0 * 1024.0)
        ratio = (free_b / total_b) if total_b > 0 else 0.0
        return MemorySnapshot(
            free_mib=mib_free,
            total_mib=mib_total,
            free_ratio=float(ratio),
            backend="nvml",
            detail=f"NVML {n} GPU(s)",
        )
    except Exception as exc:
        _LOG.debug("NVML query failed: %s", exc)
        return None
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass


def _parse_size_to_mib(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if float(raw) > 0 else None
    s = str(raw).strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB|GiB|MiB)?", s, re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "MB").upper()
    if unit in ("GB", "GIB"):
        return val * 1024.0
    return val


def _walk_apple_vram_total_mib(obj: Any, depth: int = 0) -> float | None:
    """Best-effort total VRAM from ``system_profiler SPDisplaysDataType -json``."""
    if depth > 40:
        return None
    if isinstance(obj, dict):
        keys_lower = {str(k).lower(): k for k in obj}
        if "spdisplays_vram" in keys_lower:
            mib = _parse_size_to_mib(obj[keys_lower["spdisplays_vram"]])
            if mib is not None and mib > 0:
                return mib
        if "vram" in keys_lower and "metal" in json.dumps(obj).lower():
            mib = _parse_size_to_mib(obj[keys_lower["vram"]])
            if mib is not None:
                return mib
        for v in obj.values():
            r = _walk_apple_vram_total_mib(v, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _walk_apple_vram_total_mib(item, depth + 1)
            if r is not None:
                return r
    return None


def _query_apple_mib() -> MemorySnapshot | None:
    """Apple Silicon / macOS: ``system_profiler SPDisplaysDataType`` + unified-memory heuristic."""
    if platform.system() != "Darwin":
        return None
    try:
        proc = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return None
        data = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError, ValueError) as exc:
        _LOG.debug("system_profiler failed: %s", exc)
        return None

    total_mib = _walk_apple_vram_total_mib(data)
    if total_mib is None or total_mib <= 0:
        return None

    free_ratio_sys = None
    try:
        import psutil

        vm = psutil.virtual_memory()
        if vm.total > 0:
            free_ratio_sys = float(vm.available) / float(vm.total)
    except Exception:
        pass

    if free_ratio_sys is None:
        return None

    free_mib = total_mib * free_ratio_sys
    ratio = free_ratio_sys
    return MemorySnapshot(
        free_mib=free_mib,
        total_mib=total_mib,
        free_ratio=float(ratio),
        backend="apple_system_profiler",
        detail="system_profiler VRAM total × psutil memory available ratio (unified memory proxy)",
    )


def query_gpu_memory_snapshot() -> MemorySnapshot:
    """Prefer NVML on NVIDIA; fall back to Apple profiler + psutil on Darwin."""
    snap = _query_nvml_mib()
    if snap is not None:
        return snap
    snap = _query_apple_mib()
    if snap is not None:
        return snap
    return MemorySnapshot(
        free_mib=0.0,
        total_mib=0.0,
        free_ratio=1.0,
        backend="unknown",
        detail="no NVML and no Apple VRAM heuristic — governor cannot measure GPU memory",
    )


class VRAMManager:
    """Estimate model VRAM, enforce minimum free ratio, unload Ollama residents when tight."""

    def __init__(self, ollama_base_url: str | None = None) -> None:
        raw = (ollama_base_url or os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_LOCAL_URL") or "").strip()
        self._ollama_base_url = raw.rstrip("/") or "http://127.0.0.1:11434"

    def query_memory_mib(self) -> MemorySnapshot:
        return query_gpu_memory_snapshot()

    def estimate_model_vram_mib(self, model_name: str) -> float:
        """Weights + KV overhead via :mod:`ollama_guard` heuristics and ``/api/show``."""
        from ollama_guard.client import ollama_show
        from ollama_guard.estimate import (
            estimate_weight_mib,
            infer_params_from_name,
            parse_parameter_size,
            quant_bytes_per_param,
        )

        timeout = float(os.environ.get("OCTO_VRAM_SHOW_TIMEOUT_SEC", "30"))
        show = ollama_show(self._ollama_base_url, model_name, timeout=timeout)
        details = (show or {}).get("details") if isinstance(show, dict) else None
        details = details if isinstance(details, dict) else {}

        qlevel = details.get("quantization_level")
        qlevel_s = str(qlevel) if qlevel is not None else None

        ps = details.get("parameter_size")
        pb = parse_parameter_size(str(ps)) if ps is not None else None
        if pb is None:
            pb = infer_params_from_name(model_name)

        bpp = quant_bytes_per_param(qlevel_s)

        kv = float(os.environ.get("OCTO_VRAM_GOVERNOR_KV_MIB", "768"))
        kv = max(128.0, kv)

        if pb is None:
            return kv + 4096.0

        return estimate_weight_mib(params_billions=pb, bpp=bpp, kv_overhead_mib=kv)

    def clear_cache(self, *, ollama_base_url: str | None = None, timeout_per_unload: float = 45.0) -> list[str]:
        """Unload resident Ollama models via ``/api/generate`` with ``keep_alive: 0`` (frees VRAM)."""
        base = (ollama_base_url or self._ollama_base_url).rstrip("/")
        ps = _get_json(base, "/api/ps", timeout=min(15.0, timeout_per_unload))
        unloaded: list[str] = []
        if not isinstance(ps, dict):
            return unloaded

        models = ps.get("models")
        if not isinstance(models, list):
            return unloaded

        for entry in models:
            if not isinstance(entry, dict):
                continue
            name = entry.get("model") or entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            payload = {"model": name, "prompt": ".", "stream": False, "keep_alive": 0}
            out = _post_json(base, "/api/generate", payload, timeout=timeout_per_unload)
            if out is not None:
                unloaded.append(name)
                _LOG.info("VRAMManager: requested unload for `%s` (keep_alive=0)", name)
            else:
                _LOG.warning("VRAMManager: unload request failed for `%s`", name)

        return unloaded

    def assert_can_run_model(self, model_name: str, *, auto_unload_if_tight: bool | None = None) -> None:
        """Raise :exc:`ResourceWarning` if free VRAM ratio is below threshold or model cannot fit.

        When ``OCTO_VRAM_AUTO_UNLOAD=1`` (default when unset is **false**), calls :meth:`clear_cache`
        once before re-checking thresholds.
        """
        if auto_unload_if_tight is None:
            auto_unload_if_tight = _truthy("OCTO_VRAM_AUTO_UNLOAD")

        min_r = _min_free_ratio()
        snap = self.query_memory_mib()
        est = self.estimate_model_vram_mib(model_name)

        def _block(msg: str) -> None:
            warnings.warn(msg, ResourceWarning, stacklevel=2)
            raise ResourceWarning(msg)

        if snap.backend == "unknown":
            if _truthy("OCTO_VRAM_GOVERNOR_STRICT_UNKNOWN"):
                _block(
                    "Predictive VRAM governor: GPU memory backend unavailable "
                    "(set OCTO_VRAM_GOVERNOR_STRICT_UNKNOWN=0 to allow)."
                )
            _LOG.debug("VRAM governor: unknown backend — skipping ratio gate")
            return

        if auto_unload_if_tight and snap.free_ratio < min_r:
            self.clear_cache()
            snap = self.query_memory_mib()

        if snap.free_ratio < min_r:
            _block(
                f"Predictive VRAM governor: available GPU memory ratio {snap.free_ratio:.1%} "
                f"is below minimum {min_r:.0%} ({snap.backend}: {snap.detail}). "
                f"Free ≈ {snap.free_mib:.0f} MiB / {snap.total_mib:.0f} MiB."
            )

        if snap.total_mib > 0 and est > snap.free_mib:
            _block(
                f"Predictive VRAM governor: estimated model footprint ≈ {est:.0f} MiB exceeds "
                f"available ≈ {snap.free_mib:.0f} MiB ({snap.backend})."
            )


def enforce_before_ollama(model_name: str, ollama_base_url: str | None = None, *, query: str | None = None) -> None:
    """Optional entry point for callers (honours ``OCTO_VRAM_PREDICTIVE_GOVERNOR``).

    When ``OCTO_SMART_SWAPPER`` is enabled and the run qualifies, retries once after
    :mod:`infra.smart_swapper` remediation (unload + Docker / page-cache reclaim).
    """
    if not _truthy("OCTO_VRAM_PREDICTIVE_GOVERNOR"):
        return
    mgr = VRAMManager(ollama_base_url=ollama_base_url)
    try:
        mgr.assert_can_run_model(model_name)
    except ResourceWarning as exc:
        try:
            from infra.smart_swapper import prepare_high_memory_model, smart_swapper_enabled
        except ImportError:
            raise exc
        if not smart_swapper_enabled():
            raise exc
        ok, _steps = prepare_high_memory_model(
            model_name,
            ollama_base_url=ollama_base_url,
            query=query,
            task_kind=None,
        )
        if not ok:
            raise exc
        mgr.assert_can_run_model(model_name)


def enforce_before_agent_task() -> None:
    """Gate non-Ollama agent tasks using the same free-ratio rule (no model estimate)."""
    if not _truthy("OCTO_VRAM_PREDICTIVE_GOVERNOR_AGENT"):
        return
    snap = query_gpu_memory_snapshot()
    min_r = _min_free_ratio()
    if snap.backend == "unknown":
        return
    if snap.free_ratio < min_r:
        msg = (
            f"Predictive VRAM governor (agent): available ratio {snap.free_ratio:.1%} "
            f"< {min_r:.0%} ({snap.detail})"
        )
        warnings.warn(msg, ResourceWarning, stacklevel=2)
        raise ResourceWarning(msg)
