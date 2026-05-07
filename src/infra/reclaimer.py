"""Docker-oriented resource reclamation for constrained inference hosts."""

from __future__ import annotations

import logging
import subprocess
from typing import Final

_LOG = logging.getLogger(__name__)

_NON_ESSENTIAL_CONTAINER_NAMES: Final[list[str]] = ["n8n", "searxng", "redis"]

_DOCKER_TIMEOUT_SEC: Final[int] = 120


def _docker_cli_text(result: subprocess.CompletedProcess[str]) -> str:
    """Merge docker CLI streams for error classification."""

    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(result.stderr)
    return "\n".join(parts).strip()


def _pause_failure_expected(text: str) -> bool:
    """True when ``docker pause`` failed because the container is absent or already paused."""

    lower = text.lower()
    if "no such container" in lower or "no such object" in lower:
        return True
    if "already paused" in lower:
        return True
    return False


def _unpause_failure_expected(text: str) -> bool:
    """True when ``docker unpause`` failed because the container is absent or not paused."""

    lower = text.lower()
    if "no such container" in lower or "no such object" in lower:
        return True
    if "is not paused" in lower:
        return True
    return False


class ResourceGovernor:
    """Suspend and resume non-critical containers to reclaim CPU and memory via ``docker pause`` / ``docker unpause``."""

    __slots__ = ("_non_essential_containers",)

    def __init__(self) -> None:
        self._non_essential_containers: list[str] = list(_NON_ESSENTIAL_CONTAINER_NAMES)
        _LOG.debug(
            "ResourceGovernor initialized with non-essential container names=%r",
            self._non_essential_containers,
        )

    def suspend_non_essential_containers(self) -> None:
        """Pause configured containers with ``docker pause`` (idempotent for missing/already-paused)."""
        _LOG.info(
            "ResourceGovernor.suspend_non_essential_containers: pausing %d container name(s)",
            len(self._non_essential_containers),
        )
        for idx, name in enumerate(self._non_essential_containers, start=1):
            _LOG.debug(
                "suspend_non_essential_containers: [%s/%s] docker pause %r",
                idx,
                len(self._non_essential_containers),
                name,
            )
            try:
                completed = subprocess.run(
                    ["docker", "pause", name],
                    capture_output=True,
                    text=True,
                    timeout=_DOCKER_TIMEOUT_SEC,
                )
            except FileNotFoundError:
                _LOG.error(
                    "docker CLI not found on PATH; cannot pause %r — install Docker or ensure docker is on PATH",
                    name,
                )
                continue
            except subprocess.TimeoutExpired:
                _LOG.error(
                    "docker pause timed out after %ss for container %r",
                    _DOCKER_TIMEOUT_SEC,
                    name,
                )
                continue
            except OSError as exc:
                _LOG.error("failed to execute docker pause for %r: %s", name, exc)
                continue

            out = _docker_cli_text(completed)
            if completed.returncode == 0:
                _LOG.info("paused container %r", name)
                continue
            if _pause_failure_expected(out):
                _LOG.info(
                    "docker pause skipped for %r (container missing or already paused): %s",
                    name,
                    out or "(no output)",
                )
                continue
            _LOG.error(
                "docker pause failed for %r with exit code %s: %s",
                name,
                completed.returncode,
                out or "(no output)",
            )

    def resume_containers(self) -> None:
        """Unpause configured containers with ``docker unpause`` (idempotent for missing/already-running)."""
        _LOG.info(
            "ResourceGovernor.resume_containers: unpausing %d container name(s)",
            len(self._non_essential_containers),
        )
        for idx, name in enumerate(self._non_essential_containers, start=1):
            _LOG.debug(
                "resume_containers: [%s/%s] docker unpause %r",
                idx,
                len(self._non_essential_containers),
                name,
            )
            try:
                completed = subprocess.run(
                    ["docker", "unpause", name],
                    capture_output=True,
                    text=True,
                    timeout=_DOCKER_TIMEOUT_SEC,
                )
            except FileNotFoundError:
                _LOG.error(
                    "docker CLI not found on PATH; cannot unpause %r — install Docker or ensure docker is on PATH",
                    name,
                )
                continue
            except subprocess.TimeoutExpired:
                _LOG.error(
                    "docker unpause timed out after %ss for container %r",
                    _DOCKER_TIMEOUT_SEC,
                    name,
                )
                continue
            except OSError as exc:
                _LOG.error("failed to execute docker unpause for %r: %s", name, exc)
                continue

            out = _docker_cli_text(completed)
            if completed.returncode == 0:
                _LOG.info("unpaused container %r", name)
                continue
            if _unpause_failure_expected(out):
                _LOG.info(
                    "docker unpause skipped for %r (container missing or not paused): %s",
                    name,
                    out or "(no output)",
                )
                continue
            _LOG.error(
                "docker unpause failed for %r with exit code %s: %s",
                name,
                completed.returncode,
                out or "(no output)",
            )
