"""Tests for ``local_ai_stack.doctor_fix``."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class DoctorFixTests(unittest.TestCase):
    def test_upsert_env_key_replace_and_insert(self) -> None:
        from local_ai_stack.doctor_fix import upsert_env_key

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "deploy" / "local-ai" / ".env.local"
            p.parent.mkdir(parents=True)
            p.write_text("FOO=1\nOLLAMA_NUM_GPU=9\nBAR=2\n", encoding="utf-8")
            upsert_env_key(p, "OLLAMA_NUM_GPU", "2")
            text = p.read_text(encoding="utf-8")
            self.assertIn("OLLAMA_NUM_GPU=2", text)
            self.assertNotIn("OLLAMA_NUM_GPU=9", text)

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env.local"
            upsert_env_key(p, "OLLAMA_NUM_GPU", "0")
            self.assertIn("OLLAMA_NUM_GPU=0", p.read_text(encoding="utf-8"))

    @patch("local_ai_stack.doctor_fix._run", return_value=(0, "GPU 0: x\nGPU 1: y\n", ""))
    def test_detect_two_gpus(self, _m) -> None:
        from local_ai_stack.doctor_fix import detect_ollama_num_gpu

        self.assertEqual(detect_ollama_num_gpu(), 2)

    @patch("local_ai_stack.doctor_fix._run", return_value=(127, "", "not found"))
    def test_detect_zero_without_nvidia(self, _m) -> None:
        from local_ai_stack.doctor_fix import detect_ollama_num_gpu

        self.assertEqual(detect_ollama_num_gpu(), 0)


if __name__ == "__main__":
    unittest.main()
