"""VRAM / GPU memory probes for gating Claude Code launches under pressure."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from typing import Any

_LOG = logging.getLogger(__name__)

_DEFAULT_THRESHOLD_PCT = 90.0


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def vram_guard_skipped() -> bool:
    return _truthy_env("OCTO_SKIP_VRAM_GUARD")


def vram_max_util_pct() -> float:
    raw = (os.environ.get("OCTO_VRAM_MAX_UTIL_PCT") or "").strip()
    if raw:
        try:
            return max(1.0, min(100.0, float(raw)))
        except ValueError:
            pass
    return _DEFAULT_THRESHOLD_PCT


def mac_memory_proxy_enabled() -> bool:
    return _truthy_env("OCTO_VRAM_MAC_MEMORY_PROXY")


def _run_capture(argv: list[str], *, timeout: float = 15.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return proc.returncode, out
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _LOG.debug("probe command failed: %s %s", argv[0], exc)
        return 127, ""


def _nvidia_smi_max_util_pct() -> tuple[float | None, str]:
    """Per-GPU VRAM utilization; return max across GPUs (worst case)."""
    rc, out = _run_capture(
        [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        timeout=15.0,
    )
    if rc != 0 or not out.strip():
        return None, "nvidia-smi unavailable or failed"
    ratios: list[float] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            used_mib = float(parts[0].replace(" MiB", "").replace("[Not Supported]", "").strip())
            total_mib = float(parts[1].replace(" MiB", "").replace("[Not Supported]", "").strip())
        except ValueError:
            continue
        if total_mib <= 0:
            continue
        ratios.append(100.0 * used_mib / total_mib)
    if not ratios:
        return None, "nvidia-smi produced no parseable VRAM rows"
    return max(ratios), f"nvidia-smi max VRAM util across {len(ratios)} GPU(s)"


def _walk_json_for_vram_ratio(obj: Any, depth: int = 0) -> float | None:
    """Best-effort: find used/total MiB or GB pairs in system_profiler JSON."""
    if depth > 30:
        return None
    if isinstance(obj, dict):
        keys_lower = {str(k).lower(): k for k in obj}
        # Explicit used/total pairs (some AMD / classic Mac listings).
        for u_key, t_key in (
            ("spdisplays_vram_used", "spdisplays_vram_total"),
            ("vram_used", "vram_total"),
        ):
            if u_key in keys_lower and t_key in keys_lower:
                u_raw = obj[keys_lower[u_key]]
                t_raw = obj[keys_lower[t_key]]
                used = _parse_size_to_mib(u_raw)
                total = _parse_size_to_mib(t_raw)
                if used is not None and total is not None and total > 0:
                    return 100.0 * used / total
        for v in obj.values():
            r = _walk_json_for_vram_ratio(v, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _walk_json_for_vram_ratio(item, depth + 1)
            if r is not None:
                return r
    return None


def _parse_size_to_mib(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if float(raw) > 0 else None
    s = str(raw).strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB|GiB|MiB|gib|mib)?", s, re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in ("gb", "gib"):
        return val * 1024.0
    if unit in ("mb", "mib"):
        return val
    # Bare number: assume MiB for typical profiler strings.
    return val


def _system_profiler_vram_ratio_pct() -> tuple[float | None, str]:
    rc, out = _run_capture(["system_profiler", "SPDisplaysDataType", "-json"], timeout=25.0)
    if rc != 0 or not out.strip():
        return None, "system_profiler failed or empty"
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return None, f"system_profiler JSON parse error: {exc}"
    ratio = _walk_json_for_vram_ratio(data)
    if ratio is None:
        # Text fallback: lines like "VRAM: 1234 MB of 8192 MB"
        rc2, text = _run_capture(["system_profiler", "SPDisplaysDataType"], timeout=25.0)
        if rc2 == 0 and text:
            m = re.search(
                r"(?i)vram.*?\b(\d+)\s*(?:MB|MiB)\s+of\s+(\d+)\s*(?:MB|MiB)",
                text,
            )
            if m:
                u, t = float(m.group(1)), float(m.group(2))
                if t > 0:
                    return 100.0 * u / t, "system_profiler text VRAM ratio"
        return None, "system_profiler: no VRAM used/total fields (common on Apple Silicon)"
    return ratio, "system_profiler SPDisplaysDataType JSON"


def _darwin_free_pages_gb() -> tuple[float | None, int]:
    """Return (free GiB, page_size) from vm_stat."""
    try:
        page_size = 4096
        free_pages = None
        out = subprocess.check_output(["vm_stat"], text=True, timeout=10)
        for line in out.splitlines():
            ll = line.lower()
            if "page size of" in ll and "bytes" in ll:
                parts = line.split()
                for i, p in enumerate(parts):
                    if p.isdigit() and i + 1 < len(parts) and parts[i + 1].lower() == "bytes":
                        page_size = int(p)
                        break
            if line.startswith("Pages free:"):
                tok = line.split()[2].rstrip(".")
                free_pages = int(tok)
        if free_pages is None:
            return None, page_size
        return free_pages * page_size / (1024.0**3), page_size
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, OSError):
        return None, 4096


def _darwin_physmem_gb() -> float | None:
    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=5)
        n = int(out.strip())
        return n / (1024.0**3)
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError, OSError):
        return None


def _darwin_memory_pressure_proxy_pct() -> tuple[float | None, str]:
    """Unified-memory heuristic: share not covered by vm_stat 'Pages free' vs physical RAM."""
    if sys.platform != "darwin":
        return None, "not macOS"
    total_gb = _darwin_physmem_gb()
    free_gb, _ = _darwin_free_pages_gb()
    if total_gb is None or total_gb <= 0 or free_gb is None:
        return None, "could not read hw.memsize/vm_stat"
    in_use_ratio = max(0.0, min(1.0, 1.0 - (free_gb / total_gb)))
    return 100.0 * in_use_ratio, "macOS unified-memory proxy (vm_stat free vs hw.memsize)"


def probe_gpu_memory_utilization_pct() -> tuple[float | None, str]:
    """Return (max utilization %, probe description). None if unknown — caller should not block."""
    pct, src = _nvidia_smi_max_util_pct()
    if pct is not None:
        return pct, src
    pct2, src2 = _system_profiler_vram_ratio_pct()
    if pct2 is not None:
        return pct2, src2
    if mac_memory_proxy_enabled():
        pct3, src3 = _darwin_memory_pressure_proxy_pct()
        if pct3 is not None:
            return pct3, src3
    return None, "no VRAM probe (nvidia-smi / system_profiler; optional Mac proxy off)"


def remediation_hints() -> str:
    return (
        "Mitigations:\n"
        "  1) Reduce Ollama footprint — switch to a smaller embedding model or unload the large "
        "LLM (e.g. stop the 70B runner / pull a lighter tag) so embeddings can use a separate small model.\n"
        "  2) Free Docker memory — stop heavy addon workers, e.g. "
        "`docker stop local-ai-n8n` (see deploy/local-ai/docker-compose.addons.yml), "
        "then retry Claude Code.\n"
    )


def format_blocked_message(util_pct: float, source: str, threshold: float) -> str:
    return (
        f"[octo VRAM guard] GPU / VRAM utilization is about {util_pct:.1f}% "
        f"({source}); threshold is {threshold:.0f}%.\n"
        "Launch blocked — likely an oversized local model (e.g. Ollama 70B) filled GPU memory.\n\n"
        f"{remediation_hints()}"
        "Emergency override (disables this check): OCTO_SKIP_VRAM_GUARD=1\n"
    )


def vram_guard_allows_claude_launch() -> tuple[bool, str | None]:
    """(True, None) when launch is allowed; (False, stderr_message) when blocked."""
    if vram_guard_skipped():
        return True, None
    threshold = vram_max_util_pct()
    pct, source = probe_gpu_memory_utilization_pct()
    if pct is None:
        _LOG.debug("VRAM guard: no utilization probe — allowing launch (%s)", source)
        return True, None
    if pct <= threshold:
        return True, None
    return False, format_blocked_message(pct, source, threshold)


def enforce_vram_guard_before_claude() -> None:
    """Exit with code 1 when VRAM guard blocks; no-op when skipped or probe unknown."""
    ok, msg = vram_guard_allows_claude_launch()
    if ok:
        return
    assert msg is not None
    sys.stderr.write(msg)
    raise SystemExit(1)


class ResourceMonitor:
    """VRAM-aware gate for local Claude Code — use :func:`probe_gpu_memory_utilization_pct` / :func:`vram_guard_allows_claude_launch`."""

    probe_gpu_memory_utilization_pct = staticmethod(probe_gpu_memory_utilization_pct)
    vram_guard_allows_claude_launch = staticmethod(vram_guard_allows_claude_launch)
    enforce_vram_guard_before_claude = staticmethod(enforce_vram_guard_before_claude)
