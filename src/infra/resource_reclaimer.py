"""Pause heavy Docker addon services during local LLM inference to free unified memory (Apple Silicon)."""

from __future__ import annotations

import logging
import os
import platform
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_LOG = logging.getLogger(__name__)

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]


def reclaim_enabled() -> bool:
    """Honor ``OCTO_RESOURCE_RECLAIMER_ENABLED``; default **on** on Darwin arm64 only."""
    raw = os.environ.get("OCTO_RESOURCE_RECLAIMER_ENABLED", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _repo_root() -> Path:
    hint = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if hint:
        return Path(hint).expanduser().resolve()
    return _DEFAULT_REPO_ROOT


def _compose_base_argv(repo_root: Path) -> list[str] | None:
    """Reuse the same compose invocation as fix-it / stack (project, env file, profiles)."""
    try:
        from github_bot.fix_it_worker import _compose_base_cmd, resolve_compose_paths

        env_file, agenticseek_path = resolve_compose_paths(repo_root)
        return _compose_base_cmd(repo_root, env_file, agenticseek_path)
    except Exception as exc:  # noqa: BLE001 — optional stack; log and skip reclaim
        _LOG.debug("ResourceReclaimer: compose argv unavailable (%s)", exc)
        return None


def _compose_service_action(repo_root: Path, action: str, services: tuple[str, ...]) -> tuple[int, str]:
    base = _compose_base_argv(repo_root)
    if not base:
        return -1, ""
    cmd = base + [action, *services]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("OCTO_RESOURCE_RECLAIMER_COMPOSE_TIMEOUT_SEC", "180")),
        check=False,
    )
    tail = "\n".join(x for x in ((proc.stderr or "").strip(), (proc.stdout or "").strip()) if x)
    return proc.returncode, tail


class ResourceReclaimer:
    """Temporarily ``docker compose stop`` selected addon services around LLM inference."""

    ADDON_SERVICES: tuple[str, ...] = ("n8n", "searxng")

    @staticmethod
    @contextmanager
    def pause_addon_services_for_inference() -> Iterator[None]:
        """``docker compose stop`` **n8n** and **searxng**, run inference body, then ``docker compose start``.

        Controlled by :func:`reclaim_enabled`. Failures are logged; inference still runs if stop fails.
        """
        if not reclaim_enabled():
            yield
            return

        services = ResourceReclaimer.ADDON_SERVICES
        root = _repo_root()
        rc, detail = _compose_service_action(root, "stop", services)
        if rc != 0:
            _LOG.warning(
                "ResourceReclaimer: compose stop %s failed rc=%s: %s",
                ",".join(services),
                rc,
                detail[:800],
            )
        try:
            yield
        finally:
            rc2, detail2 = _compose_service_action(root, "start", services)
            if rc2 != 0:
                _LOG.warning(
                    "ResourceReclaimer: compose start %s failed rc=%s: %s — restart manually if needed",
                    ",".join(services),
                    rc2,
                    detail2[:800],
                )


# Back-compat alias for callers that prefer a function-style context manager name.
inference_memory_reclaim = ResourceReclaimer.pause_addon_services_for_inference
