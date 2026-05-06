"""Tests for ``src.utils.git_utils``."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from utils.git_utils import IOThrottle


def test_ensure_disk_space_raises_when_low(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dest = tmp_path / "repo"

    class U:
        free = 100 * 1024 * 1024
        total = 10 * 1024**3
        used = total - free

    monkeypatch.setattr("utils.git_utils.shutil.disk_usage", lambda _p: U())
    with pytest.raises(OSError, match="IOThrottle: insufficient disk space"):
        IOThrottle.ensure_disk_space(dest, min_free_bytes=500 * 1024 * 1024)


def test_approx_tree_bytes_parses_ls_tree() -> None:
    stdout = (
        "100644 blob abcd1234abcd1234abcd1234abcd1234abcd1234      100\tREADME.md\n"
        "100644 blob deadbeefdeadbeefdeadbeefdeadbeefdeadbeef     9000\tsrc/main.py\n"
    )
    proc = MagicMock(returncode=0, stderr="", stdout=stdout)
    with patch.object(IOThrottle, "_run_git", return_value=proc):
        n = IOThrottle._approx_tree_bytes_at_head(Path("/fake/repo"))
    assert n == 9100


def test_existing_selective_paths() -> None:
    proc = MagicMock(returncode=0, stderr="", stdout="README\nsrc\nconfig\n")
    with patch.object(IOThrottle, "_run_git", return_value=proc):
        got = IOThrottle._existing_selective_paths(Path("/r"), ("src", "config", "missing"))
    assert got == ["src", "config"]
