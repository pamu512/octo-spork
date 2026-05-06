"""Generate ``deploy/local-ai/docker-compose.override.yaml`` from host RAM and CPU.

Caps SearXNG and n8n so heavy crawls / workflows are less likely to contend with host Ollama.

Disable by setting ``OCTO_RESOURCE_HARDENER=0``. Optional overrides:
``OCTO_SEARXNG_MEM_LIMIT_MIB``, ``OCTO_N8N_MEM_LIMIT_MIB``, ``OCTO_SEARXNG_CPUS``, ``OCTO_N8N_CPUS``
(non-empty values replace computed limits; reservations scale down from limits).
"""

from __future__ import annotations

import logging
import math
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[misc, assignment]

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[misc, assignment]


_OVERRIDE_REL = Path("deploy") / "local-ai" / "docker-compose.override.yaml"


def _env_float(name: str) -> float | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _detect_ram_mib_psutil() -> float | None:
    if psutil is None:
        return None
    try:
        return float(psutil.virtual_memory().total) / (1024.0 * 1024.0)
    except Exception:
        return None


def _detect_ram_mib_sysctl_hw_memsize() -> float | None:
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return None
        return float(proc.stdout.strip()) / (1024.0 * 1024.0)
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        return None


def _detect_ram_mib_proc_meminfo() -> float | None:
    try:
        path = Path("/proc/meminfo")
        if not path.is_file():
            return None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2:
                    # MemTotal is in kB
                    return float(parts[1]) / 1024.0
        return None
    except (OSError, ValueError):
        return None


def detect_total_ram_mib() -> float:
    """Best-effort total physical RAM (MiB)."""
    for fn in (_detect_ram_mib_psutil, _detect_ram_mib_sysctl_hw_memsize, _detect_ram_mib_proc_meminfo):
        v = fn()
        if v is not None and v > 64.0:
            return v
    return 8192.0


def detect_logical_cpus() -> int:
    if psutil is not None:
        try:
            n = psutil.cpu_count(logical=True)
            if n is not None and n >= 1:
                return int(n)
        except Exception:
            pass
    try:
        return max(1, os.cpu_count() or 1)
    except Exception:
        return 4


@dataclass(frozen=True)
class ServiceResources:
    mem_limit_mib: float
    cpus_limit: float
    mem_reservation_mib: float
    cpus_reservation: float


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_resources(total_ram_mib: float, logical_cpus: int) -> tuple[ServiceResources, ServiceResources]:
    """Derive SearXNG / n8n limits leaving most RAM for the host LLM and OS."""
    t = max(512.0, total_ram_mib)
    c = max(1, logical_cpus)

    # Defaults: bounded share of RAM; SearXNG can spike on scans.
    searx_mem = _clamp(t * 0.11, 384.0, 3072.0)
    n8n_mem = _clamp(t * 0.07, 512.0, 2048.0)

    cap_sum = t * 0.20
    if searx_mem + n8n_mem > cap_sum > 0:
        scale = cap_sum / (searx_mem + n8n_mem)
        searx_mem = max(384.0, searx_mem * scale)
        n8n_mem = max(384.0, n8n_mem * scale)

    searx_cpus = _clamp(c * 0.30, 0.35, 2.0)
    n8n_cpus = _clamp(c * 0.22, 0.35, 1.5)

    searx_mem_override = _env_float("OCTO_SEARXNG_MEM_LIMIT_MIB")
    n8n_mem_override = _env_float("OCTO_N8N_MEM_LIMIT_MIB")
    searx_cpu_override = _env_float("OCTO_SEARXNG_CPUS")
    n8n_cpu_override = _env_float("OCTO_N8N_CPUS")

    if searx_mem_override is not None:
        searx_mem = _clamp(searx_mem_override, 256.0, t * 0.25)
    if n8n_mem_override is not None:
        n8n_mem = _clamp(n8n_mem_override, 256.0, t * 0.20)
    if searx_cpu_override is not None:
        searx_cpus = _clamp(searx_cpu_override, 0.1, float(c))
    if n8n_cpu_override is not None:
        n8n_cpus = _clamp(n8n_cpu_override, 0.1, float(c))

    searx_res_m = _clamp(searx_mem * 0.22, 128.0, min(searx_mem * 0.85, 768.0))
    n8n_res_m = _clamp(n8n_mem * 0.22, 128.0, min(n8n_mem * 0.85, 512.0))
    searx_res_c = _clamp(searx_cpus * 0.35, 0.1, searx_cpus)
    n8n_res_c = _clamp(n8n_cpus * 0.35, 0.1, n8n_cpus)

    searx = ServiceResources(
        mem_limit_mib=searx_mem,
        cpus_limit=searx_cpus,
        mem_reservation_mib=searx_res_m,
        cpus_reservation=searx_res_c,
    )
    n8n = ServiceResources(
        mem_limit_mib=n8n_mem,
        cpus_limit=n8n_cpus,
        mem_reservation_mib=n8n_res_m,
        cpus_reservation=n8n_res_c,
    )
    return searx, n8n


def _fmt_cpus(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s if s else "0.1"


def _fmt_mem_mib(v: float) -> str:
    return f"{max(32, int(math.ceil(v)))}m"


def build_override_document(searx: ServiceResources, n8n: ServiceResources) -> dict[str, Any]:
    """Compose fragment merged over ``searxng`` / ``n8n`` (``mem_limit`` + CPU reservations)."""
    return {
        "services": {
            "searxng": {
                "mem_limit": _fmt_mem_mib(searx.mem_limit_mib),
                "deploy": {
                    "resources": {
                        "limits": {"cpus": _fmt_cpus(searx.cpus_limit)},
                        "reservations": {
                            "cpus": _fmt_cpus(searx.cpus_reservation),
                            "memory": _fmt_mem_mib(searx.mem_reservation_mib),
                        },
                    }
                },
            },
            "n8n": {
                "mem_limit": _fmt_mem_mib(n8n.mem_limit_mib),
                "deploy": {
                    "resources": {
                        "limits": {"cpus": _fmt_cpus(n8n.cpus_limit)},
                        "reservations": {
                            "cpus": _fmt_cpus(n8n.cpus_reservation),
                            "memory": _fmt_mem_mib(n8n.mem_reservation_mib),
                        },
                    }
                },
            },
        }
    }


def render_override_yaml(
    doc: dict[str, Any],
    *,
    ram_mib: float,
    logical_cpus: int,
) -> str:
    if yaml is None:
        raise RuntimeError("PyYAML is required to write docker-compose.override.yaml")
    body = yaml.safe_dump(
        doc,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    meta = (
        "# Generated by ResourceHardener (local_ai_stack). Do not edit by hand — "
        "it is rewritten on each `local_ai_stack up`.\n"
        f"# Host: {platform.system()} ram_mib≈{ram_mib:.0f} cpus={logical_cpus}\n"
        "# Disable: OCTO_RESOURCE_HARDENER=0\n"
        "\n"
    )
    return meta + body


def override_path_for_repo(repo_root: Path) -> Path:
    return (repo_root / _OVERRIDE_REL).resolve()


def ensure_compose_resource_override(
    repo_root: Path,
    *,
    logger: logging.Logger | None = None,
) -> Path | None:
    """Write ``docker-compose.override.yaml`` unless disabled. Returns path written, or None."""
    if (os.environ.get("OCTO_RESOURCE_HARDENER") or "1").strip() == "0":
        if logger:
            logger.info("ResourceHardener skipped (OCTO_RESOURCE_HARDENER=0)")
        return None

    ram = detect_total_ram_mib()
    cpus = detect_logical_cpus()
    searx, n8n = compute_resources(ram, cpus)
    doc = build_override_document(searx, n8n)
    path = override_path_for_repo(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_override_yaml(doc, ram_mib=ram, logical_cpus=cpus)
    path.write_text(text, encoding="utf-8")
    if logger:
        logger.info(
            "ResourceHardener wrote %s (searxng mem=%s cpus≈%s, n8n mem=%s cpus≈%s)",
            path,
            _fmt_mem_mib(searx.mem_limit_mib),
            _fmt_cpus(searx.cpus_limit),
            _fmt_mem_mib(n8n.mem_limit_mib),
            _fmt_cpus(n8n.cpus_limit),
        )
    return path


def compose_files_should_include_override(repo_root: Path) -> Path | None:
    """Path to include as ``-f`` if present (generated or hand-maintained)."""
    path = override_path_for_repo(repo_root)
    return path if path.is_file() else None
