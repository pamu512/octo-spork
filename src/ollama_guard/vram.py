"""GPU memory sampling (no dependency on observability package)."""

from __future__ import annotations

import subprocess
from typing import Any


def sample_gpu_free_mib() -> tuple[float | None, dict[str, Any]]:
    """Return (free_mib, raw_meta) from ``nvidia-smi``, or (None, {}) if unavailable."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return None, {}
        line = (proc.stdout or "").strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            return None, {}
        used = float(parts[0].replace("[Not Supported]", "0") or 0)
        total = float(parts[1].replace("[Not Supported]", "0") or 0)
        free = float(parts[2].replace("[Not Supported]", "0") or 0)
        meta = {"used_mib": used, "total_mib": total, "free_mib": free}
        return free, meta
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError):
        return None, {}
