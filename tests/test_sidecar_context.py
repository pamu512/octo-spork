"""Tests for ``claude_bridge.sidecar_context``."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claude_bridge import sidecar_context as sc  # noqa: E402


def _touch_octospork_layout(base: Path) -> None:
    (base / "local_ai_stack").mkdir(parents=True)
    (base / "local_ai_stack" / "__main__.py").write_text("# ok\n", encoding="utf-8")
    (base / "deploy" / "local-ai").mkdir(parents=True)
    (base / "patches" / "agenticseek").mkdir(parents=True)


class DiscoveryTests(unittest.TestCase):
    def test_finds_ancestor_and_add_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            octo = Path(td) / "octo-spork"
            child = octo / "packages" / "child-repo"
            child.mkdir(parents=True)
            _touch_octospork_layout(octo)
            argv = sc.claude_add_dir_argv(child)
            self.assertEqual(argv, ["--add-dir", str(octo.resolve())])

    def test_no_add_dir_when_already_at_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            octo = Path(td) / "octo-spork"
            _touch_octospork_layout(octo)
            self.assertEqual(sc.claude_add_dir_argv(octo), [])

    def test_octospork_root_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            octo = Path(td) / "octo"
            other = Path(td) / "somewhere-else" / "proj"
            other.mkdir(parents=True)
            _touch_octospork_layout(octo)
            with mock.patch.dict(os.environ, {"OCTO_SPORK_ROOT": str(octo)}, clear=False):
                argv = sc.claude_add_dir_argv(other)
            self.assertEqual(argv, ["--add-dir", str(octo.resolve())])


class CliTests(unittest.TestCase):
    def test_json_emit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            octo = Path(td) / "r"
            child = octo / "sub"
            child.mkdir(parents=True)
            _touch_octospork_layout(octo)
            raw = subprocess_run_sidecar(child)
            buf = json.loads(raw)
            self.assertEqual(buf["extra"], ["--add-dir", str(octo.resolve())])


def subprocess_run_sidecar(ws: Path) -> str:
    import subprocess

    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "claude_bridge.sidecar_context",
            "--workspace",
            str(ws),
            "--emit",
            "json",
        ],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


if __name__ == "__main__":
    unittest.main()
