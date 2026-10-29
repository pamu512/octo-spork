"""Tests for ``force-clean`` helpers in ``local_ai_stack.stack_teardown``.

Patches target the extracted module so they hit the live helpers (not a
``__main__`` wrapper that would leave ``subprocess.run`` patches missing).
"""

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
def teardown_mod():
    return importlib.import_module("local_ai_stack.stack_teardown")


@pytest.fixture
def main_mod():
    return importlib.import_module("local_ai_stack.__main__")


def test_octospork_labeled_container_ids(teardown_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(teardown_mod, "ROOT", Path("/tmp/octo-root"))
    proc = MagicMock(returncode=0, stdout="abc123\ndef456\n", stderr="")
    with patch("local_ai_stack.stack_teardown.subprocess.run", return_value=proc) as m:
        ids = teardown_mod._octospork_labeled_container_ids()
    assert ids == ["abc123", "def456"]
    args = m.call_args[0][0]
    assert "label=com.octospork.project=octo-spork-local-ai" in args


def test_remove_pid_lock_retries_after_chmod(tmp_path: Path, teardown_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    ff = tmp_path / "x.pid"
    ff.write_text("123", encoding="utf-8")
    os.chmod(ff, stat.S_IREAD)

    calls = {"n": 0}

    def fake_remove(p: str | os.PathLike[str]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("denied")
        Path(p).unlink()

    monkeypatch.setattr(teardown_mod.os, "remove", fake_remove)
    assert teardown_mod._remove_pid_lock_file(ff) is True


def test_main_reexport_hits_teardown_subprocess(main_mod) -> None:
    """Calling the re-exported helper still uses ``stack_teardown.subprocess.run``."""
    proc = MagicMock(returncode=0, stdout="deadbeef\n", stderr="")
    with patch("local_ai_stack.stack_teardown.subprocess.run", return_value=proc) as m:
        ids = main_mod._octospork_labeled_container_ids()
    assert ids == ["deadbeef"]
    assert m.called


def test_command_force_clean_uses_module_helpers(
    teardown_mod, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(teardown_mod, "ROOT", tmp_path)
    monkeypatch.setattr(teardown_mod, "_octospork_labeled_container_ids", lambda: ["c1"])
    rm_calls: list[list[str]] = []
    net_calls: list[str] = []
    stray_calls: list[str] = []
    lock_roots: list[Path] = []
    monkeypatch.setattr(teardown_mod, "_docker_rm_force_container_ids", lambda ids: rm_calls.append(list(ids)))
    monkeypatch.setattr(
        teardown_mod, "_docker_network_remove_labeled_project", lambda name: net_calls.append(name)
    )
    monkeypatch.setattr(
        teardown_mod, "_docker_network_remove_strays_by_name_prefix", lambda name: stray_calls.append(name)
    )
    monkeypatch.setattr(
        teardown_mod, "_remove_dangling_pid_lock_files_under", lambda p: lock_roots.append(Path(p))
    )
    teardown_mod.command_force_clean(tmp_path / ".env")
    assert rm_calls == [["c1"]]
    assert net_calls == ["octo-spork-local-ai"]
    assert stray_calls == ["octo-spork-local-ai"]
    assert lock_roots == [tmp_path / ".local"]
