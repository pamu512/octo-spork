"""Memory-aware preparation for large Ollama models (30B+): offload support stacks, reclaim VRAM/RAM."""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

_LOG = logging.getLogger(__name__)


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _truthy_default(name: str, *, default_true: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default_true
    return _truthy(name)


def smart_swapper_enabled() -> bool:
    """Off unless ``OCTO_SMART_SWAPPER`` is set truthy (Docker / cache actions are invasive)."""
    raw = os.environ.get("OCTO_SMART_SWAPPER")
    if raw is None or not str(raw).strip():
        return False
    return _truthy("OCTO_SMART_SWAPPER")


def min_params_billions_for_large() -> float:
    raw = (os.environ.get("OCTO_SMART_SWAP_MIN_PARAMS_B", "30") or "30").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 30.0


def _complex_reasoning_keywords() -> frozenset[str]:
    raw = (os.environ.get("OCTO_SMART_SWAP_COMPLEX_KEYWORDS") or "").strip()
    if raw:
        return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())
    return frozenset(
        [
            "refactor",
            "architecture",
            "architectural",
            "redesign",
            "migrate",
            "rewrite",
            "multi-service",
            "dependency graph",
            "coupling",
            "layering",
            "system design",
            "complex reasoning",
            "deep dive",
            "root cause",
            "full codebase",
        ]
    )


def task_requires_complex_reasoning(query: str | None, task_kind: str | None = None) -> bool:
    """Heuristic: user query / task label implies heavy reasoning suitable for a large model."""
    parts: list[str] = []
    if query:
        parts.append(query.lower())
    if task_kind:
        parts.append(task_kind.lower())
    blob = " ".join(parts)
    if not blob.strip():
        return False
    pattern = (os.environ.get("OCTO_SMART_SWAP_COMPLEX_REGEX") or "").strip()
    if pattern:
        try:
            if re.search(pattern, blob, re.IGNORECASE | re.DOTALL):
                return True
        except re.error as exc:
            _LOG.warning("OCTO_SMART_SWAP_COMPLEX_REGEX invalid: %s", exc)
    return any(k in blob for k in _complex_reasoning_keywords())


def estimate_parameter_count_billions(model_name: str, ollama_base_url: str) -> float | None:
    """Resolve parameter count (billions) from :mod:`ollama_guard` + ``/api/show``."""
    try:
        from ollama_guard.client import ollama_show
        from ollama_guard.estimate import infer_params_from_name, parse_parameter_size

        timeout = float(os.environ.get("OCTO_VRAM_SHOW_TIMEOUT_SEC", "30"))
        show = ollama_show(ollama_base_url.rstrip("/"), model_name, timeout=timeout)
        details = (show or {}).get("details") if isinstance(show, dict) else None
        details = details if isinstance(details, dict) else {}
        ps = details.get("parameter_size")
        pb = parse_parameter_size(str(ps)) if ps is not None else None
        if pb is None:
            pb = infer_params_from_name(model_name)
        return pb
    except Exception as exc:
        _LOG.debug("estimate_parameter_count_billions: %s", exc)
        return None


def should_consider_smart_swap(
    model_name: str,
    ollama_base_url: str,
    *,
    query: str | None,
    task_kind: str | None = None,
) -> bool:
    """True when swap logic should run (complex task and/or >= threshold parameter model)."""
    floor = min_params_billions_for_large()
    pb = estimate_parameter_count_billions(model_name, ollama_base_url)
    if pb is not None and pb >= floor:
        return True
    if task_requires_complex_reasoning(query, task_kind):
        return True
    return False


def _support_container_names() -> list[str]:
    raw = (os.environ.get("OCTO_SMART_SWAP_CONTAINERS") or "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return ["local-ai-n8n", "local-ai-redis"]


def _docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _container_exists(name: str) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", name],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return proc.returncode == 0


def offload_support_containers_cpu_only(steps: list[str]) -> list[str]:
    """Reduce contention from support services: pin CPUs / optionally stop containers.

    **CPU-only mode** here means constraining Docker CPU quota (and optionally stopping services
    when ``OCTO_SMART_SWAP_STOP_SERVICES=1``) so host RAM / scheduler bias favors Ollama.
    """
    if not _truthy_default("OCTO_SMART_SWAP_MANAGE_DOCKER", default_true=True):
        steps.append("docker management skipped (OCTO_SMART_SWAP_MANAGE_DOCKER=0)")
        return []

    if not _docker_available():
        steps.append("docker CLI unavailable; skip container offload")
        return []

    stopped: list[str] = []
    cpus = (os.environ.get("OCTO_SMART_SWAP_CPU_QUOTA") or "1.0").strip() or "1.0"
    use_stop = _truthy("OCTO_SMART_SWAP_STOP_SERVICES")

    for name in _support_container_names():
        if not _container_exists(name):
            steps.append(f"container `{name}` not present — skip")
            continue
        if use_stop:
            proc = subprocess.run(
                ["docker", "stop", "-t", "10", name],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if proc.returncode == 0:
                stopped.append(name)
                steps.append(f"stopped `{name}` to reclaim host resources")
            else:
                err = (proc.stderr or proc.stdout or "").strip()
                steps.append(f"docker stop `{name}` failed: {err[:200]}")
            continue

        proc = subprocess.run(
            ["docker", "update", f"--cpus={cpus}", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode == 0:
            steps.append(f"docker update --cpus={cpus} `{name}` (CPU-only bias)")
        else:
            err = (proc.stderr or proc.stdout or "").strip()
            steps.append(f"docker update `{name}` failed: {err[:200]}")

    return stopped


def _truthy_default(name: str, *, default_true: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default_true
    return _truthy(name)


def clear_system_disk_cache_best_effort(steps: list[str]) -> None:
    """Linux page cache drop (requires privileges); macOS ``purge`` optional."""
    if not _truthy("OCTO_SMART_SWAP_DROP_CACHES"):
        return

    sysname = platform.system()
    if sysname == "Linux":
        drop = Path("/proc/sys/vm/drop_caches")
        if drop.is_file():
            try:
                drop.write_text("3")
                steps.append("cleared Linux page cache via /proc/sys/vm/drop_caches")
                return
            except OSError as exc:
                steps.append(f"Linux drop_caches not permitted ({exc}); try sudo or sysctl")
        proc = subprocess.run(
            ["sudo", "-n", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode == 0:
            steps.append("cleared Linux page cache via sudo")
        else:
            steps.append("Linux page cache drop skipped (no permission)")
        return

    if sysname == "Darwin":
        proc = subprocess.run(
            ["sudo", "-n", "purge"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode == 0:
            steps.append("ran macOS purge (disk cache pressure)")
        else:
            steps.append("macOS purge skipped (sudo unavailable)")
        return

    steps.append(f"disk cache drop not implemented for {sysname}")


def restore_support_containers(names: list[str], steps: list[str]) -> None:
    if not names or not _docker_available():
        return
    for name in names:
        proc = subprocess.run(
            ["docker", "start", name],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode == 0:
            steps.append(f"started `{name}`")
        else:
            err = (proc.stderr or proc.stdout or "").strip()
            steps.append(f"docker start `{name}` failed: {err[:200]}")


@dataclass
class SmartSwapSession:
    """Tracks containers stopped during swap so they can be restarted."""

    ollama_base_url: str
    model_name: str
    query: str | None
    steps: list[str] = field(default_factory=list)
    stopped_containers: list[str] = field(default_factory=list)

    def model_fits(self) -> bool:
        from infra.resource_manager import VRAMManager

        mgr = VRAMManager(ollama_base_url=self.ollama_base_url)
        snap = mgr.query_memory_mib()
        est = mgr.estimate_model_vram_mib(self.model_name)
        headroom = float(os.environ.get("OCTO_SMART_SWAP_VRAM_HEADROOM", "1.0") or "1.0")
        headroom = max(0.5, headroom)
        if snap.backend == "unknown":
            return True
        if snap.total_mib <= 0:
            return True
        return est <= snap.free_mib * headroom

    def attempt_recovery(self, *, aggressive: bool = True) -> bool:
        """Run offload + cache clears + Ollama unload; return True if model now fits.

        ``aggressive=False`` skips Docker container changes (for preflight retry); use full offload
        inside :func:`smart_swap_context` so :meth:`restore_support_containers` runs in ``finally``.
        """
        from infra.resource_manager import VRAMManager

        mgr = VRAMManager(ollama_base_url=self.ollama_base_url)
        if aggressive:
            self.stopped_containers.extend(offload_support_containers_cpu_only(self.steps))
        clear_system_disk_cache_best_effort(self.steps)
        try:
            unloaded = mgr.clear_cache(ollama_base_url=self.ollama_base_url)
            if unloaded:
                self.steps.append(f"requested Ollama unload for: {', '.join(unloaded)}")
        except Exception as exc:
            self.steps.append(f"Ollama clear_cache warning: {exc}")

        return self.model_fits()


@contextlib.contextmanager
def smart_swap_context(
    model_name: str,
    ollama_base_url: str,
    query: str | None,
    *,
    task_kind: str | None = None,
) -> Iterator[None]:
    """If enabled and the run qualifies, reclaim memory before Ollama; restore Docker on exit."""
    if not smart_swapper_enabled():
        yield
        return

    if not should_consider_smart_swap(model_name, ollama_base_url, query=query, task_kind=task_kind):
        yield
        return

    sess = SmartSwapSession(
        ollama_base_url=ollama_base_url,
        model_name=model_name,
        query=query,
    )

    if sess.model_fits():
        _LOG.debug("SmartSwapper: VRAM sufficient without remediation")
        yield
        return

    ok = sess.attempt_recovery()
    for line in sess.steps:
        _LOG.info("SmartSwapper: %s", line)

    if not ok:
        _LOG.warning(
            "SmartSwapper: remediation did not free enough VRAM for `%s` — proceeding anyway "
            "(caller may OOM); steps=%s",
            model_name,
            sess.steps,
        )

    try:
        yield
    finally:
        restore_steps: list[str] = []
        restore_support_containers(sess.stopped_containers, restore_steps)
        for line in restore_steps:
            _LOG.info("SmartSwapper restore: %s", line)


def prepare_high_memory_model(
    model_name: str,
    *,
    ollama_base_url: str | None = None,
    query: str | None = None,
    task_kind: str | None = None,
) -> tuple[bool, list[str]]:
    """Explicit API: returns (fits_after_prep, step_log).

    Uses **light** remediation only (Ollama unload + optional page-cache drop): no Docker stop/update,
    so callers like preflight do not strand support containers without a paired restore.
    """
    base = (ollama_base_url or os.environ.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_LOCAL_URL") or "").strip()
    base = base.rstrip("/") or "http://127.0.0.1:11434"
    if not smart_swapper_enabled():
        return True, []
    if not should_consider_smart_swap(model_name, base, query=query, task_kind=task_kind):
        return True, []

    sess = SmartSwapSession(ollama_base_url=base, model_name=model_name, query=query)
    if sess.model_fits():
        return True, ["vram sufficient without SmartSwapper remediation"]

    ok = sess.attempt_recovery(aggressive=False)
    return ok, sess.steps
