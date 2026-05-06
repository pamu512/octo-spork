"""Tests for ``claude_bridge.permission_policy``."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claude_bridge import permission_policy as pp  # noqa: E402


class ElevateTests(unittest.TestCase):
    def test_elevate_writes_and_restarts(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            with mock.patch("claude_bridge.permission_policy.permission_prompt_elevate", return_value=True):
                with mock.patch("claude_bridge.permission_policy.docker_restart", return_value=(True, "ok")):
                    with mock.patch("claude_bridge.permission_policy.upsert_env_key") as ue:
                        rc = pp.cmd_elevate(["--repo", str(repo), "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(ue.called, False)

    def test_elevate_with_confirmation_updates_env(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            env_file = repo / ".local" / "claude_config" / ".env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text("FOO=1\n", encoding="utf-8")
            with mock.patch("claude_bridge.permission_policy.permission_prompt_elevate", return_value=True):
                with mock.patch("claude_bridge.permission_policy.docker_restart", return_value=(True, "ok")):
                    rc = pp.cmd_elevate(["--repo", str(repo)])
            self.assertEqual(rc, 0)
            text = env_file.read_text(encoding="utf-8")
            self.assertIn("OCTO_CLAUDE_ALLOWED_TOOLS=", text)
            self.assertIn("Edit", text)
            self.assertIn("Bash", text)


if __name__ == "__main__":
    unittest.main()
