"""Smoke tests for ``local_ai_stack build-optimized`` wiring (no Docker required)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class BuildOptimizedCommandTests(unittest.TestCase):
    def test_command_invokes_docker_compose_build(self) -> None:
        import local_ai_stack.__main__ as las

        with tempfile.TemporaryDirectory() as td:
            agentic = Path(td) / "agentic"
            agentic.mkdir()
            (agentic / "docker-compose.yml").write_text(
                "services:\n  backend: {image: x}\n  frontend: {image: x}\n",
                encoding="utf-8",
            )
            env_file = Path(td) / ".env.local"
            env_file.write_text(f"AGENTICSEEK_DIR={agentic}\n", encoding="utf-8")

            with mock.patch.object(las, "_run") as run_mock:
                with mock.patch.object(las, "_claude_agent_stack_available", return_value=False):
                    las.command_build_optimized(env_file, no_cache=True)

            args, kwargs = run_mock.call_args
            cmd = args[0]
            self.assertIn("docker", cmd[0])
            self.assertIn("compose", cmd)
            self.assertIn("build", cmd)
            self.assertIn("--no-cache", cmd)
            self.assertIn("backend", cmd)
            self.assertIn("frontend", cmd)
            self.assertIn(str(las.ROOT / "deploy/local-ai/docker-compose.build-optimized.agentic.yml"), cmd)
            env = kwargs.get("env") or {}
            self.assertEqual(env.get("OCTO_SPORK_ROOT"), str(las.ROOT))


if __name__ == "__main__":
    unittest.main()
