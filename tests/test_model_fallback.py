"""Tests for :mod:`local_ai_stack.model_fallback`."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from local_ai_stack.model_fallback import (
    DEGRADED_INSTRUCTION,
    pick_small_coder_fallback,
    run_ollama_pull_with_model_fallback,
    _looks_like_memory_or_vram_failure,
)


class ModelFallbackTests(unittest.TestCase):
    def test_vram_heuristic(self) -> None:
        self.assertTrue(_looks_like_memory_or_vram_failure("CUDA out of memory"))
        self.assertTrue(_looks_like_memory_or_vram_failure("insufficient VRAM to load"))
        self.assertFalse(_looks_like_memory_or_vram_failure("connection refused"))

    def test_pick_prefers_8b_coder(self) -> None:
        names = [
            "llama3.1:70b",
            "qwen2.5-coder:7b",
            "qwen2.5-coder:3b",
        ]
        self.assertEqual(pick_small_coder_fallback(names), "qwen2.5-coder:7b")

    def test_pick_3b_when_no_8b(self) -> None:
        names = ["mistral:7b", "phi3.5:3.8b-coder", "big-model:70b"]
        self.assertEqual(pick_small_coder_fallback(names), "phi3.5:3.8b-coder")

    @patch("local_ai_stack.model_fallback.subprocess.run")
    def test_fallback_on_vram_pull_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stderr="CUDA out of memory", stdout="")

        env_file = Path("/tmp/.env.local")
        agentic = Path("/tmp/agentic")
        root = Path("/tmp/repo")
        env_vals = {"OLLAMA_MODEL": "qwen2.5:72b", "OLLAMA_LOCAL_URL": "http://127.0.0.1:11434"}
        log = MagicMock()

        with patch("local_ai_stack.model_fallback.list_local_ollama_model_names") as mock_tags:
            mock_tags.return_value = ["qwen2.5-coder:7b"]
            with patch("local_ai_stack.__main__._rewrite_env_file_string_values"):
                with patch("local_ai_stack.__main__._configure_agenticseek_ini"):
                    with patch("local_ai_stack.__main__._print"):
                        import local_ai_stack.__main__ as main_mod

                        out = run_ollama_pull_with_model_fallback(
                            root,
                            env_file,
                            env_vals,
                            {},
                            log,
                            agentic,
                        )
        self.assertEqual(out["OLLAMA_MODEL"], "qwen2.5-coder:7b")
        self.assertEqual(out["OCTO_DEGRADED_TASK_INSTRUCTION"], DEGRADED_INSTRUCTION)
