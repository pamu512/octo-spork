"""Return-code contract for :mod:`local_ai_stack.nightly_audit`.

Missing ``scripts/nightly_audit.sh`` → 2. Subprocess returncode is propagated
(no ``|| true`` in the Python wrapper).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from local_ai_stack.nightly_audit import command_nightly_audit


def test_missing_script_returns_2(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("local_ai_stack.nightly_audit.ROOT", tmp_path)
    code = command_nightly_audit(tmp_path / ".env.local", ".")
    assert code == 2


def test_subprocess_nonzero_returncode_is_propagated(
    tmp_path: Path, monkeypatch
) -> None:
    script = tmp_path / "scripts" / "nightly_audit.sh"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("", encoding="utf-8")
    monkeypatch.setattr("local_ai_stack.nightly_audit.ROOT", tmp_path)
    code = command_nightly_audit(tmp_path / ".env.local", ".")
    assert code == 7


def test_cli_help_flags_unchanged() -> None:
    sub = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "nightly-audit", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sub.returncode == 0
    assert "--env-file" in sub.stdout
    assert "--repo" in sub.stdout

    parent = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert parent.returncode == 0
    assert "nightly-audit" in parent.stdout
    assert "scripts/nightly_audit.sh" in parent.stdout
