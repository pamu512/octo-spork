"""Tests for ``github_bot.http_client``."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class RateLimitDetectionTests(unittest.TestCase):
    def test_429_is_rate_limit(self) -> None:
        import httpx

        from github_bot.http_client import is_github_rate_limit_response

        r = httpx.Response(429, request=httpx.Request("GET", "https://api.github.com/x"))
        self.assertTrue(is_github_rate_limit_response(r))

    def test_403_with_zero_remaining(self) -> None:
        import httpx

        from github_bot.http_client import is_github_rate_limit_response

        r = httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9"},
            request=httpx.Request("GET", "https://api.github.com/x"),
        )
        self.assertTrue(is_github_rate_limit_response(r))


class RetryBehaviorTests(unittest.TestCase):
    def test_sleep_until_reset_from_header(self) -> None:
        import httpx

        from github_bot.http_client import retrying_github_request

        future = int(time.time()) + 2
        responses = [
            httpx.Response(
                429,
                headers={"X-RateLimit-Reset": str(future)},
                request=httpx.Request("GET", "https://api.github.com/y"),
            ),
            httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", "https://api.github.com/y")),
        ]
        stack = iter(responses)

        def execute() -> httpx.Response:
            return next(stack)

        with patch("github_bot.http_client.time.sleep") as mock_sleep:
            out = retrying_github_request(execute, max_attempts=5, base_delay=0.01, max_delay=1.0)

        self.assertEqual(out.status_code, 200)
        self.assertGreaterEqual(mock_sleep.call_count, 1)
        slept_total = sum(call.args[0] for call in mock_sleep.call_args_list if call.args)
        self.assertGreaterEqual(slept_total, 1.0)

    def test_decorator_retries_then_succeeds(self) -> None:
        import httpx

        from github_bot.http_client import github_http_retry

        calls = {"n": 0}

        @github_http_retry(max_attempts=5, base_delay=0.01, max_delay=1.0)
        def flaky() -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(
                    429,
                    headers={"X-RateLimit-Reset": str(int(time.time()) + 3600)},
                    request=httpx.Request("GET", "https://api.github.com/z"),
                )
            return httpx.Response(200, json={}, request=httpx.Request("GET", "https://api.github.com/z"))

        with patch("github_bot.http_client.time.sleep"):
            r = flaky()

        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls["n"], 2)

    def test_exponential_backoff_on_500(self) -> None:
        import httpx

        from github_bot.http_client import retrying_github_request

        responses = [
            httpx.Response(502, request=httpx.Request("GET", "https://api.github.com/a")),
            httpx.Response(200, json={}, request=httpx.Request("GET", "https://api.github.com/a")),
        ]
        it = iter(responses)

        def execute() -> httpx.Response:
            return next(it)

        with patch("github_bot.http_client.time.sleep") as mock_sleep:
            r = retrying_github_request(execute, max_attempts=5, base_delay=0.1, max_delay=10.0)

        self.assertEqual(r.status_code, 200)
        mock_sleep.assert_called()


class GitHubHttpxClientTests(unittest.TestCase):
    def test_client_wraps_request(self) -> None:
        import httpx

        from github_bot.http_client import GitHubHttpxClient

        future = int(time.time()) + 2
        seq = [
            httpx.Response(
                429,
                headers={"X-RateLimit-Reset": str(future)},
                request=httpx.Request("GET", "https://api.github.com/user"),
            ),
            httpx.Response(200, json={"login": "x"}, request=httpx.Request("GET", "https://api.github.com/user")),
        ]
        q = iter(seq)

        def handler(request: httpx.Request) -> httpx.Response:
            return next(q)

        transport = httpx.MockTransport(handler)

        with patch("github_bot.http_client.time.sleep"):
            client = GitHubHttpxClient(transport=transport, max_attempts=4)
            resp = client.get("https://api.github.com/user")

        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
