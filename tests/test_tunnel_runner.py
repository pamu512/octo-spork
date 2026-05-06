"""Tests for tunnel runner utilities."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class TryCloudflareUrlTests(unittest.TestCase):
    def test_extracts_https_hostname(self) -> None:
        from github_bot.tunnel_runner import _extract_trycloudflare_url

        line = "INF https://abcd-efgh.trycloudflare.com is ready"
        self.assertEqual(_extract_trycloudflare_url(line), "https://abcd-efgh.trycloudflare.com")


class GitHubWebhookPatchTests(unittest.TestCase):
    def test_skips_github_api_without_admin_token(self) -> None:
        from github_bot.tunnel_runner import update_github_app_webhook_url

        with patch.dict(os.environ, {"GH_ADMIN_TOKEN": ""}, clear=False):
            with patch("github_bot.tunnel_runner._github_request") as mock_req:
                update_github_app_webhook_url("https://public.example")
            mock_req.assert_not_called()


if __name__ == "__main__":
    unittest.main()
