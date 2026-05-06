"""Unit tests for Ollama model swap helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_ai_stack import ollama_swap as oswap


class OllamaSwapHelpersTests(unittest.TestCase):
    def test_registry_library_url_encodes_colon(self) -> None:
        u = oswap.registry_library_url("qwen2.5:14b")
        self.assertIn("%3A", u)
        self.assertTrue(u.startswith("https://ollama.com/library/"))

    def test_maybe_rewrite_env_model_updates_and_appends(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env.local"
            p.write_text("FOO=1\n", encoding="utf-8")
            oswap.maybe_rewrite_env_model(p, "llama3:8b")
            text = p.read_text(encoding="utf-8")
            self.assertIn("OLLAMA_MODEL=llama3:8b", text)
            self.assertIn("FOO=1", text)

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env.local"
            p.write_text("", encoding="utf-8")
            oswap.maybe_rewrite_env_model(p, "phi:latest")
            self.assertIn("OLLAMA_MODEL=phi:latest", p.read_text(encoding="utf-8"))

    def test_verify_model_on_registry_404(self) -> None:
        from io import BytesIO

        err = oswap.urllib.error.HTTPError(
            "https://example.invalid/",
            404,
            "nf",
            {},
            BytesIO(),
        )
        with mock.patch.object(oswap.urllib.request, "urlopen", side_effect=err):
            ok, msg = oswap.verify_model_on_registry("bogus-model-never-exists-xyz")
            self.assertFalse(ok)
            self.assertIn("404", msg)


if __name__ == "__main__":
    unittest.main()
