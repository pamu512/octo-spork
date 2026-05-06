"""httpx wrapper for GitHub REST calls: exponential backoff + rate-limit reset sleeps."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import httpx

__all__ = [
    "GitHubHttpxClient",
    "github_http_retry",
    "retrying_github_request",
]

F = TypeVar("F", bound=Callable[..., httpx.Response])


def _parse_rate_limit_reset_epoch(headers: httpx.Headers) -> int | None:
    raw = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _retry_after_interval_sec(headers: httpx.Headers) -> float | None:
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        return float(text)
    except ValueError:
        return None


def exponential_backoff_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """Return sleep seconds with exponential growth capped at ``max_delay`` plus light jitter."""
    cap = min(max_delay, base_delay * (2**attempt))
    jitter = random.uniform(0.0, max(base_delay * 0.25, 0.05))
    return cap + jitter


def _sleep_until_rate_limit_reset(headers: httpx.Headers, *, buffer_sec: float = 1.0) -> float:
    """Sleep until ``X-RateLimit-Reset`` (Unix epoch seconds). Returns seconds slept."""
    epoch = _parse_rate_limit_reset_epoch(headers)
    if epoch is None:
        return 0.0
    now = time.time()
    wait = max(0.0, float(epoch) - now + buffer_sec)
    time.sleep(wait)
    return wait


def _sleep_for_github_rate_limit(
    headers: httpx.Headers,
    attempt: int,
    base_delay: float,
    max_delay: float,
) -> None:
    """Prefer GitHub's reset time, then ``Retry-After``, then exponential backoff."""
    slept = _sleep_until_rate_limit_reset(headers)
    if slept > 0.0:
        return
    ra = _retry_after_interval_sec(headers)
    if ra is not None:
        time.sleep(min(max_delay, max(ra, 0.0)))
        return
    time.sleep(exponential_backoff_delay(attempt, base_delay, max_delay))


def is_github_rate_limit_response(response: httpx.Response) -> bool:
    """True when GitHub is throttling (secondary limits may use 403 with zero remaining)."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    rem_raw = response.headers.get("X-RateLimit-Remaining") or response.headers.get(
        "x-ratelimit-remaining"
    )
    if rem_raw is not None:
        try:
            if int(str(rem_raw).strip()) == 0:
                return True
        except ValueError:
            pass
    # If GitHub included a reset timestamp, treat as quota-related throttling.
    if _parse_rate_limit_reset_epoch(response.headers) is not None:
        return True
    return False


def retrying_github_request(
    execute: Callable[[], httpx.Response],
    *,
    max_attempts: int = 10,
    base_delay: float = 1.0,
    max_delay: float = 120.0,
) -> httpx.Response:
    """Run ``execute()`` (typically ``client.get(...)``) with retries.

    * **403 / 429** when :func:`is_github_rate_limit_response` is true: sleep until
      ``X-RateLimit-Reset`` when present, else ``Retry-After``, else exponential backoff.
    * **5xx**: exponential backoff.
    * **Transport errors** (:class:`httpx.HTTPError`): exponential backoff.
    * Other status codes are returned immediately (no retry).
    """
    last_http_error: httpx.HTTPError | None = None
    for attempt in range(max_attempts):
        try:
            response = execute()
        except httpx.HTTPError as exc:
            last_http_error = exc
            if attempt >= max_attempts - 1:
                raise
            time.sleep(exponential_backoff_delay(attempt, base_delay, max_delay))
            continue

        if response.status_code < 400:
            return response

        if is_github_rate_limit_response(response):
            if attempt >= max_attempts - 1:
                return response
            _sleep_for_github_rate_limit(response.headers, attempt, base_delay, max_delay)
            continue

        if response.status_code >= 500:
            if attempt >= max_attempts - 1:
                return response
            time.sleep(exponential_backoff_delay(attempt, base_delay, max_delay))
            continue

        return response

    if last_http_error is not None:
        raise last_http_error
    raise RuntimeError("retry loop exited without response")


def github_http_retry(
    max_attempts: int = 10,
    *,
    base_delay: float = 1.0,
    max_delay: float = 120.0,
) -> Callable[[F], F]:
    """Decorator for a **nullary** callable that performs one httpx call and returns a :class:`httpx.Response`.

    Example::

        client = httpx.Client(headers={"Authorization": "Bearer …"})
        @github_http_retry(max_attempts=8)
        def fetch_me():
            return client.get("https://api.github.com/user")

        fetch_me()

    For requests with parameters, use a closure or :class:`GitHubHttpxClient`.
    """

    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> httpx.Response:
            return retrying_github_request(
                lambda: fn(*args, **kwargs),
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
            )

        return wrapper  # type: ignore[return-value]

    return decorator


class GitHubHttpxClient(httpx.Client):
    """:class:`httpx.Client` that retries GitHub rate limits and transient failures."""

    def __init__(
        self,
        *args: Any,
        max_attempts: int = 10,
        base_delay: float = 1.0,
        max_delay: float = 120.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._gr_max_attempts = max_attempts
        self._gr_base_delay = base_delay
        self._gr_max_delay = max_delay

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        parent = super()
        return retrying_github_request(
            lambda: parent.request(method, url, **kwargs),
            max_attempts=self._gr_max_attempts,
            base_delay=self._gr_base_delay,
            max_delay=self._gr_max_delay,
        )
