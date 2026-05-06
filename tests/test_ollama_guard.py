"""Tests for ``ollama_guard`` VRAM heuristics and policy."""

from __future__ import annotations

import unittest
from unittest import mock

from ollama_guard.estimate import infer_params_from_name, parse_parameter_size, quant_bytes_per_param
from ollama_guard.policy import analyze_model, candidate_quant_tags


class OllamaGuardEstimateTests(unittest.TestCase):
    def test_parse_parameter_size(self) -> None:
        self.assertAlmostEqual(parse_parameter_size("8.0B"), 8.0)
        self.assertAlmostEqual(parse_parameter_size("70B"), 70.0)

    def test_infer_params_from_name(self) -> None:
        self.assertAlmostEqual(infer_params_from_name("qwen2.5:32b") or 0, 32.0)

    def test_quant_bytes_per_param(self) -> None:
        self.assertLess(quant_bytes_per_param("Q4_K_M"), quant_bytes_per_param("F16"))


class OllamaGuardPolicyTests(unittest.TestCase):
    def test_candidate_tags_shape(self) -> None:
        tags = candidate_quant_tags("some/model:7b-instruct-q8_0")
        self.assertTrue(any("q4" in t.lower() for t in tags))

    @mock.patch("ollama_guard.policy.candidate_quant_tags")
    @mock.patch("ollama_guard.policy.ollama_show")
    @mock.patch("ollama_guard.policy.sample_gpu_free_mib")
    def test_analyze_proposes_smaller_when_over_budget(
        self,
        mock_vram: mock.MagicMock,
        mock_show: mock.MagicMock,
        mock_cands: mock.MagicMock,
    ) -> None:
        mock_vram.return_value = (1500.0, {})
        mock_cands.return_value = ["repo:alt-q4"]
        mock_show.side_effect = [
            {"details": {"parameter_size": "70B", "quantization_level": "F16"}},
            {"details": {"parameter_size": "70B", "quantization_level": "Q4_K_M"}},
        ]
        d = analyze_model("repo:big-f16", base_url="http://127.0.0.1:11434")
        self.assertFalse(d.fits_without_change)
        self.assertEqual(d.proposed_model, "repo:alt-q4")

    @mock.patch("ollama_guard.policy.ollama_show")
    @mock.patch("ollama_guard.policy.sample_gpu_free_mib")
    def test_analyze_ok_when_fits(self, mock_vram: mock.MagicMock, mock_show: mock.MagicMock) -> None:
        mock_vram.return_value = (80_000.0, {})
        mock_show.return_value = {"details": {"parameter_size": "8B", "quantization_level": "Q4_K_M"}}
        d = analyze_model("smol:8b", base_url="http://127.0.0.1:11434")
        self.assertTrue(d.fits_without_change)


if __name__ == "__main__":
    unittest.main()
