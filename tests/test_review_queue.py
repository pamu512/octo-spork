"""Tests for Redis-backed PR review mutex and FIFO queue."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class QueueFakeRedis:
    """Async Redis stub for mutex + list queue + delivery idempotency ``SET NX``."""

    def __init__(self) -> None:
        self._strings: dict[str, str] = {}
        self._lists: dict[str, list[str]] = {}

    async def ping(self) -> bool:
        return True

    async def set(
        self, name: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and name in self._strings:
            return None
        self._strings[name] = value
        return True

    async def delete(self, *names: str) -> int:
        n = 0
        for name in names:
            if name in self._strings:
                del self._strings[name]
                n += 1
        return n

    async def expire(self, name: str, time: int) -> bool:  # noqa: ARG002
        return name in self._strings

    async def rpush(self, key: str, value: str) -> int:
        self._lists.setdefault(key, []).append(value)
        return len(self._lists[key])

    async def lpop(self, key: str) -> str | None:
        lst = self._lists.get(key, [])
        if not lst:
            return None
        return lst.pop(0)

    async def lpush(self, key: str, value: str) -> int:
        self._lists.setdefault(key, []).insert(0, value)
        return len(self._lists[key])

    async def llen(self, key: str) -> int:
        return len(self._lists.get(key, []))

    async def aclose(self) -> None:
        return None


class ReviewQueueUnitTests(unittest.TestCase):
    def test_should_gate_pull_request_review(self) -> None:
        import github_bot.review_queue as rq

        self.assertTrue(
            rq.should_gate_pull_request_review(
                {"X-GitHub-Event": "pull_request"},
                {"action": "opened"},
            )
        )
        self.assertFalse(
            rq.should_gate_pull_request_review(
                {"X-GitHub-Event": "pull_request"},
                {"action": "labeled"},
            )
        )
        self.assertFalse(
            rq.should_gate_pull_request_review(
                {"x-github-event": "issue_comment"},
                {"action": "created"},
            )
        )

    def test_should_schedule_octo_spork_from_issue_comment(self) -> None:
        import github_bot.review_queue as rq

        payload_ok = {
            "action": "created",
            "comment": {"body": "/octo-spork analyze"},
            "issue": {"number": 1, "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/1"}},
        }
        payload_fix = {
            **payload_ok,
            "comment": {"body": "/octo-spork fix"},
        }
        self.assertTrue(
            rq.should_schedule_octo_spork_from_issue_comment(
                {"X-GitHub-Event": "issue_comment"},
                payload_ok,
            )
        )
        self.assertTrue(
            rq.should_schedule_octo_spork_from_issue_comment(
                {"X-GitHub-Event": "issue_comment"},
                payload_fix,
            )
        )
        self.assertFalse(
            rq.should_schedule_octo_spork_from_issue_comment(
                {"X-GitHub-Event": "issue_comment"},
                {**payload_ok, "action": "edited"},
            )
        )
        self.assertFalse(
            rq.should_schedule_octo_spork_from_issue_comment(
                {"X-GitHub-Event": "pull_request"},
                payload_ok,
            )
        )
        issue_only = {
            "action": "created",
            "comment": {"body": "/octo-spork analyze"},
            "issue": {"number": 2},
        }
        self.assertFalse(
            rq.should_schedule_octo_spork_from_issue_comment(
                {"X-GitHub-Event": "issue_comment"},
                issue_only,
            )
        )

    def test_comment_body_invokes_octo_spork_analyze(self) -> None:
        import github_bot.review_queue as rq

        self.assertTrue(rq.comment_body_invokes_octo_spork_analyze("/octo-spork analyze"))
        self.assertTrue(rq.comment_body_invokes_octo_spork_analyze("/Octo-Spork  analyze  extra"))
        self.assertFalse(rq.comment_body_invokes_octo_spork_analyze("please /octo-spork analyze"))
        self.assertFalse(rq.comment_body_invokes_octo_spork_analyze("not first line\n/octo-spork analyze"))

    def test_octo_spork_issue_comment_command(self) -> None:
        import github_bot.review_queue as rq

        self.assertEqual(rq.octo_spork_issue_comment_command("/octo-spork analyze"), "analyze")
        self.assertEqual(rq.octo_spork_issue_comment_command("/octo-spork fix"), "fix")
        self.assertEqual(rq.octo_spork_issue_comment_command("/Octo-Spork  fix  extra"), "fix")
        self.assertIsNone(rq.octo_spork_issue_comment_command("/octo-spork unknown"))
        self.assertIsNone(rq.octo_spork_issue_comment_command("preface\n/octo-spork fix"))

    def test_pr_html_url_from_issue_comment_payload(self) -> None:
        import github_bot.review_queue as rq

        self.assertEqual(
            rq.pr_html_url_from_issue_comment_payload(
                {
                    "repository": {"full_name": "acme/widget"},
                    "issue": {"number": 99},
                }
            ),
            "https://github.com/acme/widget/pull/99",
        )
        self.assertIsNone(rq.pr_html_url_from_issue_comment_payload({"repository": {}, "issue": {}}))

    def test_worker_exception_invokes_developer_note_and_releases_mutex(self) -> None:
        import github_bot.review_queue as rq

        reported: list[tuple[str, str, bool]] = []

        def capture(env: dict, exc: Exception, tb: str) -> None:
            reported.append(
                (str(env.get("delivery")), type(exc).__name__, "Traceback" in tb)
            )

        async def run() -> None:
            r = QueueFakeRedis()
            with (
                patch.object(rq, "report_pr_processing_failure", side_effect=capture),
                patch.object(rq, "_MUTEX_KEY", "unit:wm_ex"),
                patch.object(rq, "_QUEUE_KEY", "unit:wm_q"),
            ):

                async def boom(envelope: dict) -> None:
                    raise RuntimeError("planned worker failure")

                await rq.schedule_pr_review_or_queue(r, {"delivery": "wm1"}, boom)
                await asyncio.sleep(0.12)
                # Mutex must be released after failure so another acquire succeeds.
                ok = await rq.acquire_review_mutex(r, ttl_sec=300)
                self.assertTrue(ok)

        asyncio.run(run())
        self.assertEqual(reported, [("wm1", "RuntimeError", True)])

    def test_schedule_second_is_queued_then_chained(self) -> None:
        import github_bot.review_queue as rq

        async def run() -> None:
            r = QueueFakeRedis()
            runs: list[str] = []

            async def worker(envelope: dict) -> None:
                runs.append(str(envelope.get("delivery")))
                await asyncio.sleep(0.02)

            with patch.object(rq, "_MUTEX_KEY", "unit:mutex"), patch.object(rq, "_QUEUE_KEY", "unit:queue"):
                s1 = await rq.schedule_pr_review_or_queue(r, {"delivery": "a"}, worker)
                s2 = await rq.schedule_pr_review_or_queue(r, {"delivery": "b"}, worker)
                self.assertEqual(s1, "started")
                self.assertEqual(s2, "queued")
                self.assertEqual(await rq.queue_depth(r), 1)
                await asyncio.sleep(0.15)
                self.assertEqual(runs, ["a", "b"])

        asyncio.run(run())

    def test_queue_depth_empty(self) -> None:
        import github_bot.review_queue as rq

        async def run() -> None:
            r = QueueFakeRedis()
            with patch.object(rq, "_QUEUE_KEY", "unit:depth"):
                self.assertEqual(await rq.queue_depth(r), 0)

        asyncio.run(run())


class WebhookPRReviewRouteTests(unittest.TestCase):
    def test_pull_request_opened_returns_202_and_started(self) -> None:
        from fastapi.testclient import TestClient

        import github_bot.app as app_module

        fake = QueueFakeRedis()

        async def skip_verify() -> None:
            return None

        with patch.object(app_module.redis_async, "from_url", return_value=fake):
            app_module.app.dependency_overrides[app_module.verify_signature] = skip_verify
            try:
                with TestClient(app_module.app) as client:
                    did = "550e8400-e29b-41d4-a716-446655440001"
                    headers = {
                        "X-GitHub-Delivery": did,
                        "X-GitHub-Event": "pull_request",
                    }
                    r = client.post(
                        "/webhook",
                        json={
                            "action": "opened",
                            "installation": {"id": 1},
                            "pull_request": {"id": 99},
                        },
                        headers=headers,
                    )
                    self.assertEqual(r.status_code, 202)
                    body = r.json()
                    self.assertEqual(body.get("pr_review"), "started")
                    self.assertEqual(body.get("delivery"), did)
            finally:
                app_module.app.dependency_overrides.clear()

    def test_second_pr_while_first_slow_is_queued(self) -> None:
        from fastapi.testclient import TestClient

        import github_bot.app as app_module

        fake = QueueFakeRedis()
        gate = threading.Event()

        async def slow_worker(envelope: dict) -> None:
            await asyncio.to_thread(gate.wait)

        async def skip_verify() -> None:
            return None

        with patch.object(app_module.redis_async, "from_url", return_value=fake):
            app_module.app.dependency_overrides[app_module.verify_signature] = skip_verify
            try:
                with patch.object(app_module, "_pr_review_worker", slow_worker):
                    with TestClient(app_module.app) as client:
                        did1 = "550e8400-e29b-41d4-a716-446655440010"
                        did2 = "550e8400-e29b-41d4-a716-446655440011"
                        h1 = {
                            "X-GitHub-Delivery": did1,
                            "X-GitHub-Event": "pull_request",
                        }
                        h2 = {
                            "X-GitHub-Delivery": did2,
                            "X-GitHub-Event": "pull_request",
                        }
                        pr_body = {
                            "action": "opened",
                            "installation": {"id": 1},
                            "pull_request": {"id": 1},
                        }
                        r1 = client.post("/webhook", json=pr_body, headers=h1)
                        self.assertEqual(r1.status_code, 202)
                        self.assertEqual(r1.json().get("pr_review"), "started")

                        r2 = client.post("/webhook", json=pr_body, headers=h2)
                        self.assertEqual(r2.status_code, 202)
                        body2 = r2.json()
                        self.assertEqual(body2.get("pr_review"), "queued")
                        self.assertEqual(body2.get("queue_depth"), 1)
                        # Unblock the slow worker before TestClient shutdown waits on background tasks.
                        gate.set()
            finally:
                app_module.app.dependency_overrides.clear()

    def test_issue_comment_unauthorized_returns_403(self) -> None:
        from fastapi.testclient import TestClient

        import github_bot.app as app_module

        fake = QueueFakeRedis()

        async def skip_verify() -> None:
            return None

        with patch.object(app_module.redis_async, "from_url", return_value=fake):
            app_module.app.dependency_overrides[app_module.verify_signature] = skip_verify
            try:
                with patch.dict(
                    os.environ,
                    {"ALLOWED_USERS": "someone-else"},
                    clear=False,
                ):
                    with TestClient(app_module.app) as client:
                        did = "550e8400-e29b-41d4-a716-446655440100"
                        r = client.post(
                            "/webhook",
                            json={
                                "action": "created",
                                "comment": {
                                    "body": "/octo-spork analyze",
                                    "user": {"login": "alice"},
                                },
                                "issue": {
                                    "number": 3,
                                    "pull_request": {
                                        "url": "https://api.github.com/repos/o/r/pulls/3"
                                    },
                                },
                                "repository": {"full_name": "o/r"},
                                "installation": {"id": 1},
                            },
                            headers={
                                "X-GitHub-Delivery": did,
                                "X-GitHub-Event": "issue_comment",
                            },
                        )
                self.assertEqual(r.status_code, 403)
                body = r.json()
                self.assertEqual(body.get("error"), "permission_denied")
            finally:
                app_module.app.dependency_overrides.clear()

    def test_issue_comment_authorized_returns_202(self) -> None:
        from fastapi.testclient import TestClient

        import github_bot.app as app_module

        fake = QueueFakeRedis()

        async def skip_verify() -> None:
            return None

        with patch.object(app_module.redis_async, "from_url", return_value=fake):
            app_module.app.dependency_overrides[app_module.verify_signature] = skip_verify
            try:
                with patch.dict(
                    os.environ,
                    {"ALLOWED_USERS": "alice, bob"},
                    clear=False,
                ):
                    with TestClient(app_module.app) as client:
                        did = "550e8400-e29b-41d4-a716-446655440101"
                        r = client.post(
                            "/webhook",
                            json={
                                "action": "created",
                                "comment": {
                                    "body": "/octo-spork analyze",
                                    "user": {"login": "Alice"},
                                },
                                "issue": {
                                    "number": 3,
                                    "pull_request": {
                                        "url": "https://api.github.com/repos/o/r/pulls/3"
                                    },
                                },
                                "repository": {"full_name": "o/r"},
                                "installation": {"id": 1},
                            },
                            headers={
                                "X-GitHub-Delivery": did,
                                "X-GitHub-Event": "issue_comment",
                            },
                        )
                self.assertEqual(r.status_code, 202)
                j = r.json()
                self.assertEqual(j.get("pr_review"), "started")
                self.assertEqual(
                    j.get("pr_html_url"), "https://github.com/o/r/pull/3"
                )
            finally:
                app_module.app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
