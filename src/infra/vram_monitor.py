"""macOS unified-memory pressure hints from ``vm_stat`` plus swap residency from sysctl."""

from __future__ import annotations

import logging
import platform
import re
import subprocess
from typing import Final

_LOG = logging.getLogger(__name__)

_VM_STAT_TIMEOUT_SEC: Final[int] = 10


def _run_vm_stat() -> str:
    completed = subprocess.run(
        ["vm_stat"],
        capture_output=True,
        text=True,
        timeout=_VM_STAT_TIMEOUT_SEC,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"vm_stat failed with exit code {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )
    return completed.stdout or ""


def _parse_pages_line(line: str) -> int | None:
    m = re.search(r":\s*([\d,]+)\s*\.?\s*$", line.strip())
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def _parse_vm_stat_active_and_swapouts(text: str) -> tuple[int, int, int, int]:
    """Parse page size, Pages active, Swapouts (pages swapped out cumulative), and approximate % .

    Returns (page_size_bytes, pages_active, swapouts_pages, approx_pressure_percent_times_1000)
    last field stored as int per mille (0–100000) to avoid float noise in types.
    """

    size_m = re.search(r"page size of\s+(\d+)\s+bytes", text, flags=re.IGNORECASE)
    page_size = int(size_m.group(1)) if size_m else 4096

    pages_active = 0
    swapouts = 0
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith("pages active:"):
            v = _parse_pages_line(line)
            if v is not None:
                pages_active = v
        elif low.startswith("swapouts:"):
            v = _parse_pages_line(line)
            if v is not None:
                swapouts = v
        elif "pages swapped outs" in low or low.startswith("pages swapped out"):
            v = _parse_pages_line(line)
            if v is not None:
                swapouts = v

    if pages_active <= 0:
        raise ValueError("vm_stat output missing usable 'Pages active'")

    ram_bytes = int((subprocess.run(
        ["sysctl", "-n", "hw.memsize"],
        capture_output=True,
        text=True,
        timeout=_VM_STAT_TIMEOUT_SEC,
        check=True,
    ).stdout or "").strip())

    active_bytes = pages_active * page_size
    permille = min(100_000, int(100_000 * active_bytes / max(ram_bytes, 1)))

    return page_size, pages_active, swapouts, permille


def _swap_used_bytes_from_sysctl() -> int:
    """Return swap file bytes currently resident (``sysctl vm.swapusage``)."""

    completed = subprocess.run(
        ["sysctl", "-n", "vm.swapusage"],
        capture_output=True,
        text=True,
        timeout=_VM_STAT_TIMEOUT_SEC,
    )
    if completed.returncode != 0:
        return 0
    raw = (completed.stdout or "").strip()
    m = re.search(r"used\s*=\s*([\d.]+)\s*M", raw, flags=re.IGNORECASE)
    if not m:
        return 0
    mib = float(m.group(1))
    return int(mib * 1024 * 1024)


def check_memory_pressure() -> str:
    """Classify memory pressure as ``\"HIGH\"`` or ``\"NORMAL\"``.

    Runs ``vm_stat`` and parses **Pages active** plus swap-related counters (**Swapouts**, which is
    how macOS labels cumulative pages swapped out — matching the common wording "pages swapped out").
    Computes an approximate pressure percentage as ``active_pages * page_size / hw.memsize``.

    The ``vm_stat`` Swapouts counter is cumulative since boot, so it does not indicate whether swap
    is *currently* allocated. This function therefore treats **swap usage** as non-zero space used in
    the swapfile arena via ``sysctl vm.swapusage`` (``used = … M``). When that used figure is
    greater than zero, returns ``\"HIGH\"``; otherwise ``\"NORMAL\"``.
    """
    if platform.system() != "Darwin":
        return "NORMAL"

    try:
        blob = _run_vm_stat()
        page_size, pages_active, swapouts, permille = _parse_vm_stat_active_and_swapouts(blob)
        pct = permille / 1000.0
        _LOG.debug(
            "vm_stat: page_size=%s pages_active=%s swapouts(cumulative)=%s approx_pressure=%.2f%%",
            page_size,
            pages_active,
            swapouts,
            pct,
        )
        swap_used_b = _swap_used_bytes_from_sysctl()
    except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        _LOG.warning("check_memory_pressure: falling back to NORMAL (%s)", exc)
        return "NORMAL"

    if swap_used_b > 0:
        return "HIGH"
    return "NORMAL"
