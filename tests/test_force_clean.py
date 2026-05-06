"""Tests for ``force-clean`` helpers in ``local_ai_stack.__main__``."""

from __future__ import annotations

import importlib
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


@pytest.fixture
def main_mod():
    return importlib.import_module("local_ai_stack.__main__")


def test_octospork_labeled_container_ids(main_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "ROOT", Path("/tmp/octo-root"))
    proc = MagicMock(returncode=0, stdout="abc123\ndef456\n", stderr="")
    with patch("local_ai_stack.__main__.subprocess.run", return_value=proc) as m:
        ids = main_mod._octospork_labeled_container_ids()
    assert ids == ["abc123", "def456"]
    args = m.call_args[0][0]
    assert "label=com.octospork.project=octo-spork-local-ai" in args


def test_remove_pid_lock_retries_after_chmod(tmp_path: Path, main_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    ff = tmp_path / "x.pid"
    ff.write_text("123", encoding="utf-8")
    os.chmod(ff, stat.S_IREAD)

    calls = {"n": 0}

    def fake_remove(p: str | os.PathLike[str]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("denied")
        Path(p).unlink()

    monkeypatch.setattr(main_mod.os, "remove", fake_remove)
    assert main_mod._remove_pid_lock_file(ff) is True
