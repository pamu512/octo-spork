"""Tests for Redis-backed webhook delivery idempotency."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class FakeRedis:
    """Minimal async Redis stub matching ``SET nx ex`` semantics used by the bot."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def set(self, name: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None:
        if nx and name in self._store:
            return None
        self._store[name] = value
        return True

    async def aclose(self) -> None:
        return None


class DeliveryCacheUnitTests(unittest.TestCase):
    def test_try_claim_second_call_is_duplicate(self) -> None:
        from github_bot.delivery_cache import try_claim_delivery

        async def run() -> None:
            r = FakeRedis()
            did = "550e8400-e29b-41d4-a716-446655440000"
            self.assertTrue(await try_claim_delivery(r, did))
            self.assertFalse(await try_claim_delivery(r, did))

        asyncio.run(run())

    def test_is_valid_delivery_id(self) -> None:
        from github_bot.delivery_cache import is_valid_delivery_id

        self.assertTrue(is_valid_delivery_id("550e8400-e29b-41d4-a716-446655440000"))
        self.assertFalse(is_valid_delivery_id("not-a-uuid"))
        self.assertFalse(is_valid_delivery_id(""))


class WebhookIdempotencyRouteTests(unittest.TestCase):
    def test_duplicate_post_returns_202(self) -> None:
        from fastapi.testclient import TestClient

        import github_bot.app as app_module

        fake = FakeRedis()

        async def skip_verify() -> None:
            return None

        with patch.object(app_module.redis_async, "from_url", return_value=fake):
            app_module.app.dependency_overrides[app_module.verify_signature] = skip_verify
            try:
                with TestClient(app_module.app) as client:
                    did = "550e8400-e29b-41d4-a716-446655440000"
                    headers = {"X-GitHub-Delivery": did}
                    r1 = client.post("/webhook", json={"zen": "pong"}, headers=headers)
                    self.assertEqual(r1.status_code, 200)
                    r2 = client.post("/webhook", json={"zen": "pong"}, headers=headers)
                    self.assertEqual(r2.status_code, 202)
                    body = r2.json()
                    self.assertEqual(body.get("status"), "duplicate_delivery")
                    self.assertEqual(body.get("delivery"), did)
            finally:
                app_module.app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
