"""Tests for :class:`github_bot.auth.GitHubAuth`."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from github_bot.auth import GitHubAuth, _parse_github_datetime  # noqa: E402


MINIMAL_PEM = """-----BEGIN RSA PRIVATE KEY-----
MIIBOgIBAAJBALRiMLAHudeSA1iohZFnISrWTDwtPDMAzMJDIgJTIrYTSySoRWF40
vEtJWX405wJHUmFwqvI62ledoZXupTZCdBMkCAwEAAQJBAIUzTe59sRMvwtWViSgf
uQQvhxtylVzSOWpYgVvXyXwWvXyXwWvXyXwWvXyXwWvXyXwWvXyXwWvXyXwECIQDY
-----END RSA PRIVATE KEY-----
"""


class ParseGithubTimeTests(unittest.TestCase):
    def test_z_suffix(self) -> None:
        dt = _parse_github_datetime("2026-05-06T12:30:45Z")
        self.assertEqual(dt.tzinfo, timezone.utc)


class GitHubAuthCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._pem = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
        self._pem.write(MINIMAL_PEM)
        self._pem.close()
        self.pem_path = Path(self._pem.name)

    def tearDown(self) -> None:
        self.pem_path.unlink(missing_ok=True)

    @patch("github_bot.auth.jwt.encode")
    def test_reuses_installation_token_until_within_five_minutes(self, mock_encode: MagicMock) -> None:
        mock_encode.return_value = "signed-jwt"
        calls = {"n": 0}

        def urlopen_side_effect(req: object, timeout: float = 60) -> io.BytesIO:
            calls["n"] += 1
            exp = "2099-01-01T00:00:00Z"
            payload = {"token": f"tok_{calls['n']}", "expires_at": exp}
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        with patch("github_bot.auth.urllib.request.urlopen", side_effect=urlopen_side_effect):
            auth = GitHubAuth(app_id=12345, private_key_path=self.pem_path)
            t1 = auth.get_installation_access_token(99)
            t2 = auth.get_installation_access_token(99)

        self.assertEqual(t1, t2)
        self.assertEqual(calls["n"], 1)

    @patch("github_bot.auth.jwt.encode")
    def test_refreshes_when_expiry_within_margin(self, mock_encode: MagicMock) -> None:
        mock_encode.return_value = "signed-jwt"
        calls = {"n": 0}

        def urlopen_side_effect(req: object, timeout: float = 60) -> io.BytesIO:
            calls["n"] += 1
            if calls["n"] == 1:
                exp_dt = datetime.now(timezone.utc) + timedelta(minutes=3)
                exp = exp_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                exp = "2099-06-01T00:00:00Z"
            payload = {"token": f"tok_{calls['n']}", "expires_at": exp}
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        with patch("github_bot.auth.urllib.request.urlopen", side_effect=urlopen_side_effect):
            auth = GitHubAuth(app_id=12345, private_key_path=self.pem_path)
            auth.get_installation_access_token(42)
            auth.get_installation_access_token(42)

        self.assertEqual(calls["n"], 2)

    def test_installation_id_from_payload(self) -> None:
        self.assertIsNone(GitHubAuth.installation_id_from_payload({}))
        self.assertEqual(
            GitHubAuth.installation_id_from_payload({"installation": {"id": 77}}),
            77,
        )


if __name__ == "__main__":
    unittest.main()
