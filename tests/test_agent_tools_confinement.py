"""Thin confinement tests for LangGraph TerminalTool and FileWriteTool."""

from __future__ import annotations

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

from agent.tools.file_write import AtomicWriteFailed, FileWriteTool  # noqa: E402
from agent.tools.terminal import TerminalTool  # noqa: E402


class TerminalToolSecurityTests(unittest.TestCase):
    def test_blocked_rm_rf_root_never_reaches_subprocess(self) -> None:
        tool = TerminalTool()
        with mock.patch("agent.tools.terminal.subprocess.run") as run:
            result = tool.execute("rm -rf /")

        run.assert_not_called()
        self.assertIsInstance(result, dict)
        self.assertIn("exit_code", result)
        self.assertIn("stdout", result)
        self.assertIn("stderr", result)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("blocked", result["stderr"].lower())


class FileWriteToolWorkspaceTests(unittest.TestCase):
    def test_write_outside_workspace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workspace = base / "workspace"
            workspace.mkdir()
            outside = base / "escape.txt"
            tool = FileWriteTool(workspace_root=workspace)

            with self.assertRaises(AtomicWriteFailed):
                tool.write_content(str(outside), "pwned")
            with self.assertRaises(AtomicWriteFailed):
                tool.write_content(str(workspace / ".." / "escape.txt"), "pwned")

            self.assertFalse(outside.exists())

    def test_write_inside_workspace_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            dest = workspace / "nested" / "ok.txt"
            tool = FileWriteTool(workspace_root=workspace)

            tool.write_content(str(dest), "hello")

            self.assertEqual(dest.read_text(encoding="utf-8"), "hello")

    def test_write_without_workspace_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "anywhere.txt"
            tool = FileWriteTool()
            env = {k: v for k, v in os.environ.items() if k != "OCTO_WORKSPACE"}
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(AtomicWriteFailed):
                    tool.write_content(str(dest), "nope")
            self.assertFalse(dest.exists())

    def test_write_respects_octo_workspace_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            inside = workspace / "in.txt"
            outside = Path(tmp) / "out.txt"
            tool = FileWriteTool()
            with mock.patch.dict(os.environ, {"OCTO_WORKSPACE": str(workspace)}, clear=False):
                tool.write_content(str(inside), "ok")
                with self.assertRaises(AtomicWriteFailed):
                    tool.write_content(str(outside), "nope")
            self.assertEqual(inside.read_text(encoding="utf-8"), "ok")
            self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
