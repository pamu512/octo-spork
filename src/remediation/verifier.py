"""Pytest-based verification for remediation workflows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _repository_root() -> Path:
    """Directory containing ``src/remediation`` (repository checkout root)."""

    return Path(__file__).resolve().parents[2]


def _pytest_binary_paths(repo_root: Path) -> list[Path]:
    """Prefer project-local virtualenv interpreters (POSIX + Windows layouts)."""

    if sys.platform == "win32":
        return [
            repo_root / ".venv" / "Scripts" / "pytest.exe",
            repo_root / "venv" / "Scripts" / "pytest.exe",
        ]
    return [
        repo_root / ".venv" / "bin" / "pytest",
        repo_root / "venv" / "bin" / "pytest",
    ]


def _resolve_pytest_executable(repo_root: Path) -> Path | None:
    for candidate in _pytest_binary_paths(repo_root):
        resolved = candidate.resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    which = shutil.which("pytest")
    if which:
        return Path(which).resolve()
    return None


def _format_logs(completed: subprocess.CompletedProcess[str]) -> str:
    chunks: list[str] = []
    if completed.stdout:
        chunks.append(completed.stdout.strip())
    if completed.stderr:
        chunks.append(completed.stderr.strip())
    if not chunks:
        return "(pytest produced no stdout or stderr)"
    return "\n\n--- stderr ---\n".join(chunks) if len(chunks) == 2 else chunks[0]


def run_test_suite(filepath: str) -> dict[str, bool | str]:
    """Run pytest against ``filepath`` (file or directory) and summarize the outcome.

    Locates pytest under ``<repo>/.venv/bin/pytest`` (or ``venv``, or Windows ``Scripts``), falling
    back to ``pytest`` on ``PATH``. Uses :func:`subprocess.run` with ``capture_output=True`` and
    text mode so stdout/stderr are merged into ``logs``.

    Pytest uses exit code ``0`` when all tests pass and ``1`` (among others) when tests fail;
    ``passed`` is ``True`` only when the process exits with code ``0``.
    """
    repo_root = _repository_root()
    pytest_exe = _resolve_pytest_executable(repo_root)
    target = Path(filepath).expanduser().resolve()

    if pytest_exe is None:
        return {
            "passed": False,
            "logs": (
                "ERROR: could not resolve pytest executable "
                "(looked under .venv and venv, then PATH)."
            ),
        }

    cmd = [str(pytest_exe), str(target)]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return {
            "passed": False,
            "logs": f"ERROR: failed to execute pytest at {pytest_exe}: {exc}",
        }

    logs = _format_logs(completed)
    passed = completed.returncode == 0
    return {"passed": passed, "logs": logs}
