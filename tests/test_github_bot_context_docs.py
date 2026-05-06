"""Tests for ``/docs/context`` API-key gate and payload."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class _FakeRedis:
    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class ContextDocsRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_key = os.environ.get("OCTO_SYSTEM_PROMPT_VIEWER_KEY")
        os.environ["OCTO_SYSTEM_PROMPT_VIEWER_KEY"] = "unit-test-context-viewer-secret"

    def tearDown(self) -> None:
        if self._prev_key is None:
            os.environ.pop("OCTO_SYSTEM_PROMPT_VIEWER_KEY", None)
        else:
            os.environ["OCTO_SYSTEM_PROMPT_VIEWER_KEY"] = self._prev_key

    def test_docs_context_401_without_header(self) -> None:
        from github_bot.app import app

        fake = _FakeRedis()
        with mock.patch("github_bot.app.redis_async.from_url", return_value=fake):
            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                r = client.get("/docs/context")
        self.assertEqual(r.status_code, 401)

    def test_docs_context_503_when_key_unconfigured(self) -> None:
        import github_bot.app as app_module
        from fastapi.testclient import TestClient

        fake = _FakeRedis()
        with mock.patch.object(app_module.redis_async, "from_url", return_value=fake):
            with mock.patch.dict(os.environ, {"OCTO_SYSTEM_PROMPT_VIEWER_KEY": ""}):
                with TestClient(app_module.app) as client:
                    r = client.get("/docs/context", headers={"X-API-Key": "anything"})
        self.assertEqual(r.status_code, 503)

    def test_docs_context_json_empty_capture(self) -> None:
        import github_bot.app as app_module
        from fastapi.testclient import TestClient

        fake = _FakeRedis()
        with mock.patch.object(app_module.redis_async, "from_url", return_value=fake):
            with TestClient(app_module.app) as client:
                key = os.environ["OCTO_SYSTEM_PROMPT_VIEWER_KEY"]
                r = client.get(
                    "/docs/context",
                    headers={"X-API-Key": key, "Accept": "application/json"},
                )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data.get("status"), "empty")

    def test_docs_context_json_with_capture(self) -> None:
        import github_bot.app as app_module
        from fastapi.testclient import TestClient
        from observability import prompt_capture as pc

        pc.record_ollama_review_prompt(
            "hello **evidence**",
            model="m",
            ollama_base_url="http://127.0.0.1:11434",
            num_ctx=4096,
            temperature=0.1,
            timeout_seconds=60,
        )
        fake = _FakeRedis()
        with mock.patch.object(app_module.redis_async, "from_url", return_value=fake):
            with TestClient(app_module.app) as client:
                key = os.environ["OCTO_SYSTEM_PROMPT_VIEWER_KEY"]
                r = client.get(
                    "/docs/context?format=json",
                    headers={"Authorization": f"Bearer {key}"},
                )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("prompt"), "hello **evidence**")
        self.assertEqual(data.get("model"), "m")


if __name__ == "__main__":
    unittest.main()
