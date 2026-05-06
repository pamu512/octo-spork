"""Unit tests for Ollama pre-flight helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from github_bot import ollama_preflight as op  # noqa: E402


class OllamaPreflightParsingTests(unittest.TestCase):
    def test_model_present_in_tags(self) -> None:
        data = {"models": [{"name": "llama3.2:latest"}, {"name": "qwen2.5:14b"}]}
        self.assertTrue(op.model_present_in_tags(data, "llama3.2"))
        self.assertTrue(op.model_present_in_tags(data, "llama3.2:latest"))
        self.assertTrue(op.model_present_in_tags(data, "qwen2.5:14b"))
        self.assertFalse(op.model_present_in_tags(data, "mistral"))

    def test_model_loaded_in_ps(self) -> None:
        data = {"models": [{"model": "gemma2:2b", "size": 1}]}
        self.assertTrue(op.model_loaded_in_ps(data, "gemma2"))
        self.assertFalse(op.model_loaded_in_ps(data, "llama3"))


class OllamaPreflightIntegrationTests(unittest.TestCase):
    def test_unreachable_returns_false(self) -> None:
        ok, msg = op.verify_ollama_preflight("http://127.0.0.1:1", "any")
        self.assertFalse(ok)
        self.assertIn("unreachable", msg.lower())

    @patch("github_bot.ollama_preflight.urllib.request.urlopen")
    def test_happy_path(self, mock_urlopen: MagicMock) -> None:
        import json

        class Resp:
            status = 200

            def __enter__(self) -> Resp:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b""

        tags = {
            "models": [
                {"name": "qwen2.5:14b"},
            ]
        }

        def side_effect(req: object, timeout: float | None = None) -> Resp:  # noqa: ARG001
            url = getattr(req, "full_url", "") or str(req)
            if "version" in url:
                return Resp()
            if "tags" in url:
                r = Resp()

                def read_tags() -> bytes:
                    return json.dumps(tags).encode()

                r.read = read_tags  # type: ignore[method-assign]
                return r
            raise AssertionError(f"unexpected url {url!r}")

        mock_urlopen.side_effect = side_effect
        ok, msg = op.verify_ollama_preflight("http://127.0.0.1:11434", "qwen2.5:14b", timeout_sec=2.0)
        self.assertTrue(ok)
        self.assertEqual(msg, "")


if __name__ == "__main__":
    unittest.main()
