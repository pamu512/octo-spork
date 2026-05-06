"""Tests for ``claude_bridge.model_sync``."""

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

from claude_bridge import model_sync as ms


class ModelPreferenceTests(unittest.TestCase):
    def test_model_matches_exact_tag(self) -> None:
        self.assertTrue(ms.model_matches_preference("qwen3-coder:latest", "qwen3-coder"))

    def test_model_matches_base_only_preference(self) -> None:
        self.assertTrue(ms.model_matches_preference("llama3.1:70b-instruct-q4_0", "llama3.1"))

    def test_select_best_respects_order(self) -> None:
        tags = ["phi3:latest", "llama3.1:70b", "qwen3-coder:latest"]
        pref = ["qwen3-coder", "llama3.1:70b"]
        self.assertEqual(ms.select_best_model(tags, pref), "qwen3-coder:latest")

    def test_select_best_second_choice(self) -> None:
        tags = ["mistral:latest", "llama3.1:8b"]
        pref = ["qwen3-coder", "llama3.1"]
        self.assertEqual(ms.select_best_model(tags, pref), "llama3.1:8b")


class UpsertEnvTests(unittest.TestCase):
    def test_upsert_overwrites_and_preserves_other_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env"
            p.write_text("FOO=bar\nCLAUDE_CODE_MODEL=old\n# comment\n", encoding="utf-8")
            ms.upsert_env_key(p, "CLAUDE_CODE_MODEL", "new-model")
            text = p.read_text(encoding="utf-8")
            self.assertIn("CLAUDE_CODE_MODEL=new-model", text)
            self.assertIn("FOO=bar", text)
            self.assertIn("# comment", text)

    def test_upsert_appends_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env"
            ms.upsert_env_key(p, "CLAUDE_CODE_MODEL", "x:y")
            self.assertEqual(p.read_text(encoding="utf-8").strip(), "CLAUDE_CODE_MODEL=x:y")


class FetchTagsTests(unittest.TestCase):
    def test_fetch_parses_models_array(self) -> None:
        payload = '{"models":[{"name":"a:latest"},{"name":"b"}]}'
        mock_resp = mock.Mock()
        mock_resp.read.return_value = payload.encode("utf-8")
        mock_resp.__enter__ = mock.Mock(return_value=mock_resp)
        mock_resp.__exit__ = mock.Mock(return_value=False)

        with mock.patch.object(ms.urllib.request, "urlopen", return_value=mock_resp):
            tags = ms.fetch_ollama_model_tags("http://ollama:11434")
        self.assertEqual(tags, ["a:latest", "b"])


class RunSyncTests(unittest.TestCase):
    def test_run_sync_dry_run_no_network_effects(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            with mock.patch.object(
                ms,
                "fetch_ollama_model_tags",
                return_value=["foo:latest", "qwen3-coder:latest"],
            ):
                rc = ms.run_sync(
                    ollama_base="http://x",
                    preferred=["qwen3-coder", "foo"],
                    env_file=env_path,
                    dry_run=True,
                    restart_container=False,
                    container_name=None,
                )
            self.assertEqual(rc, 0)
            self.assertFalse(env_path.exists())

    def test_run_sync_writes_and_skips_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            with mock.patch.object(
                ms,
                "fetch_ollama_model_tags",
                return_value=["qwen3-coder:latest"],
            ):
                rc = ms.run_sync(
                    ollama_base="http://x",
                    preferred=["qwen3-coder"],
                    env_file=env_path,
                    dry_run=False,
                    restart_container=False,
                    container_name=None,
                )
            self.assertEqual(rc, 0)
            self.assertIn("CLAUDE_CODE_MODEL=qwen3-coder:latest", env_path.read_text(encoding="utf-8"))

    def test_run_sync_restart_failure_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env_path = Path(td) / ".env"
            with mock.patch.object(
                ms,
                "fetch_ollama_model_tags",
                return_value=["qwen3-coder:latest"],
            ):
                with mock.patch.object(ms.subprocess, "run", return_value=mock.Mock(returncode=1, stderr="no")):
                    rc = ms.run_sync(
                        ollama_base="http://x",
                        preferred=["qwen3-coder"],
                        env_file=env_path,
                        dry_run=False,
                        restart_container=True,
                        container_name="fake-container",
                    )
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
