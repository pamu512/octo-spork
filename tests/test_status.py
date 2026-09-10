"""Fail-closed / CLI contract for :mod:`local_ai_stack.status`.

Asserts the status command's existing env-read failure path (not an import-only
rename). Happy-path dashboard output is unchanged and not re-specified here.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from local_ai_stack.status import command_status


def test_unreadable_env_file_raises_runtime_error(tmp_path: Path) -> None:
    """Invalid UTF-8 env file → RuntimeError (existing contract in command_status)."""
    env_file = tmp_path / ".env.local"
    env_file.write_bytes(b"\xff\xfe not-utf8")
    with pytest.raises(RuntimeError, match="Could not read env file"):
        command_status(env_file)


def test_cli_unreadable_env_file_returns_1(tmp_path: Path) -> None:
    """``python -m local_ai_stack status`` maps that RuntimeError to returncode 1."""
    env_file = tmp_path / ".env.local"
    env_file.write_bytes(b"\xff\xfe not-utf8")
    sub = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "status", "--env-file", str(env_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sub.returncode == 1
    combined = f"{sub.stdout}{sub.stderr}"
    assert "Error:" in combined
    assert "Could not read env file" in combined


def test_cli_help_flags_unchanged() -> None:
    sub = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "status", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sub.returncode == 0
    assert "--env-file" in sub.stdout
    assert "Path to .env.local" in sub.stdout

    parent = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert parent.returncode == 0
    assert "status" in parent.stdout
