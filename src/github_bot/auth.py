"""GitHub App authentication: JWT minting and installation access token exchange."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt

# Installation tokens are valid for ~1 hour; refresh when less than this remains.
_REFRESH_MARGIN = timedelta(minutes=5)

# GitHub requires App JWTs to expire within 10 minutes (we use the maximum window).
_JWT_TTL_SECONDS = 600


@dataclass(frozen=True)
class _CachedInstallationToken:
    token: str
    expires_at: datetime


class GitHubAuth:
    """Manage RS256 JWTs for a GitHub App and exchange them for installation tokens."""

    def __init__(
        self,
        *,
        app_id: str | int,
        private_key_path: str | Path,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self._app_id = str(app_id)
        self._private_key_path = Path(private_key_path)
        self._api_base = api_base_url.rstrip("/")
        self._pem_text: str | None = None
        self._installation_cache: dict[int, _CachedInstallationToken] = {}
        self._cache_lock = threading.Lock()

    def _load_private_key_pem(self) -> str:
        if self._pem_text is None:
            try:
                self._pem_text = self._private_key_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(
                    f"Could not read GitHub App private key from {self._private_key_path}"
                ) from exc
            if "BEGIN" not in self._pem_text or "PRIVATE KEY" not in self._pem_text:
                raise ValueError(
                    f"File does not look like a PEM private key: {self._private_key_path}"
                )
        return self._pem_text

    def create_jwt(self) -> str:
        """Build a new GitHub App JWT (RS256) valid for 10 minutes."""
        pem = self._load_private_key_pem()
        now = int(time.time())
        payload = {
            "iat": now,
            "exp": now + _JWT_TTL_SECONDS,
            "iss": self._app_id,
        }
        encoded: str = jwt.encode(payload, pem, algorithm="RS256")
        return encoded

    def _should_refresh(self, expires_at: datetime) -> bool:
        """True if the token expires within the refresh margin (or is already expired)."""
        now = datetime.now(timezone.utc)
        remaining = expires_at - now
        return remaining <= _REFRESH_MARGIN

    def _request_installation_token(
        self,
        installation_id: int,
        app_jwt: str,
    ) -> _CachedInstallationToken:
        url = f"{self._api_base}/app/installations/{installation_id}/access_tokens"
        body = b"{}"
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "octo-spork-github-bot",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub installation token request failed ({exc.code}): {detail[:2000]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub installation token request failed: {exc}") from exc

        data = json.loads(raw)
        token = data.get("token")
        expires_raw = data.get("expires_at")
        if not token or not expires_raw:
            raise RuntimeError("GitHub API response missing token or expires_at")
        expires_at = _parse_github_datetime(str(expires_raw))
        return _CachedInstallationToken(token=str(token), expires_at=expires_at)

    def get_installation_access_token(self, installation_id: int) -> str:
        """Return an installation access token, using cache unless expiry is within 5 minutes."""
        with self._cache_lock:
            cached = self._installation_cache.get(installation_id)
            if cached is not None and not self._should_refresh(cached.expires_at):
                return cached.token

        app_jwt = self.create_jwt()
        fresh = self._request_installation_token(installation_id, app_jwt)

        with self._cache_lock:
            self._installation_cache[installation_id] = fresh
            return fresh.token

    @staticmethod
    def installation_id_from_payload(payload: dict[str, Any]) -> int | None:
        """Extract ``installation.id`` from a webhook JSON body (many event types)."""
        inst = payload.get("installation")
        if not isinstance(inst, dict):
            return None
        raw = inst.get("id")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None


def _parse_github_datetime(value: str) -> datetime:
    """Parse GitHub API ISO-8601 timestamps (``...Z``) to aware UTC :class:`datetime`."""
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
