"""DeepProbe: verify Ollama (``/api/tags``), Postgres (``SELECT 1``), and Redis (``PING``).

Used by ``local_ai_stack up`` after ``docker compose up`` and by ``verify`` for parity.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable

CONTAINER_POSTGRES = "local-ai-postgres"
CONTAINER_REDIS = "local-ai-redis"

BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0)
DEFAULT_TIMEOUT_SEC = 60.0


def _timeout_from_env() -> float:
    raw = (os.environ.get("OCTO_DEEP_PROBE_TIMEOUT_SEC") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SEC
    try:
        v = float(raw)
        return max(5.0, min(600.0, v))
    except ValueError:
        return DEFAULT_TIMEOUT_SEC


def probe_ollama_api_tags(ollama_base_url: str, *, timeout: float = 10.0) -> tuple[bool, str]:
    """GET ``/api/tags``; success when HTTP 200 and JSON contains a ``models`` array."""
    raw = (ollama_base_url or "").strip() or "http://127.0.0.1:11434"
    url = raw.rstrip("/") + "/api/tags"
    try:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "local-ai-stack-deep-probe/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
            code = getattr(response, "status", None) or response.getcode()
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return False, f"connection failed: {exc.reason}"
    except (TimeoutError, OSError, ValueError) as exc:
        return False, str(exc)
    if code != 200:
        return False, f"HTTP {code}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return False, "response is not a JSON object"
    models = data.get("models")
    if not isinstance(models, list):
        return False, "response missing 'models' array"
    return True, f"/api/tags OK ({len(models)} model tag(s))"


def probe_postgres_select1(
    *,
    container: str,
    user: str,
    password: str,
    database: str,
    timeout: float = 25.0,
) -> tuple[bool, str]:
    """Run ``SELECT 1`` inside the Postgres container via ``psql``."""
    try:
        completed = subprocess.run(
            [
                "docker",
                "exec",
                "-e",
                f"PGPASSWORD={password}",
                container,
                "psql",
                "-U",
                user,
                "-d",
                database,
                "-tAc",
                "SELECT 1",
            ],
            cwd=None,
            capture_output=True,
            text=True,
            timeout=float(timeout),
            check=False,
        )
    except FileNotFoundError:
        return False, "docker CLI not found"
    except subprocess.TimeoutExpired:
        return False, "docker exec / psql timed out"
    except OSError as exc:
        return False, str(exc)
    out = (completed.stdout or "").strip()
    err = (completed.stderr or "").strip()
    if completed.returncode == 0 and out == "1":
        return True, "SELECT 1 ok"
    detail = err or out or f"exit {completed.returncode}"
    return False, detail


def probe_redis_ping(container: str, *, timeout: float = 25.0) -> tuple[bool, str]:
    """``redis-cli ping`` inside the Redis/Valkey container; success when response is ``PONG``."""
    try:
        completed = subprocess.run(
            ["docker", "exec", container, "redis-cli", "ping"],
            capture_output=True,
            text=True,
            timeout=float(timeout),
            check=False,
        )
    except FileNotFoundError:
        return False, "docker CLI not found"
    except subprocess.TimeoutExpired:
        return False, "docker exec timed out"
    except OSError as exc:
        return False, str(exc)
    out = (completed.stdout or "").strip()
    err = (completed.stderr or "").strip()
    if completed.returncode == 0 and out.upper() == "PONG":
        return True, "PONG"
    detail = err or out or f"exit {completed.returncode}"
    return False, detail


def deep_probe_once(env_values: dict[str, str]) -> tuple[bool, dict[str, tuple[bool, str]]]:
    """Run all three probes once; returns (all_ok, component -> (ok, detail))."""
    ollama_base = env_values.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434").strip()
    if not ollama_base:
        ollama_base = "http://127.0.0.1:11434"

    user = (env_values.get("POSTGRES_USER") or "octo").strip()
    password = (env_values.get("POSTGRES_PASSWORD") or "changeme").strip()
    database = (env_values.get("POSTGRES_DB") or "octo").strip()

    results: dict[str, tuple[bool, str]] = {}
    o_ok, o_msg = probe_ollama_api_tags(ollama_base)
    results["ollama"] = (o_ok, o_msg)
    p_ok, p_msg = probe_postgres_select1(
        container=CONTAINER_POSTGRES,
        user=user,
        password=password,
        database=database,
    )
    results["postgres"] = (p_ok, p_msg)
    r_ok, r_msg = probe_redis_ping(CONTAINER_REDIS)
    results["redis"] = (r_ok, r_msg)
    return o_ok and p_ok and r_ok, results


def run_deep_probe_until_ready(
    env_values: dict[str, str],
    *,
    logger: logging.Logger | None = None,
    timeout_sec: float | None = None,
    announce: Callable[[str], None] | None = None,
) -> None:
    """
    Retry until all probes succeed or *timeout_sec* elapses.

    Uses exponential backoff **between rounds**: 1s, 2s, 4s, 8s (then stays at 8s).
    """
    limit_sec = float(timeout_sec if timeout_sec is not None else _timeout_from_env())
    deadline = time.monotonic() + limit_sec
    backoff_idx = 0
    last_results: dict[str, tuple[bool, str]] | None = None

    def _say(text: str) -> None:
        if announce is not None:
            announce(text)
        else:
            print(text, flush=True)

    while True:
        now = time.monotonic()
        if now >= deadline:
            parts = []
            if last_results:
                for name, (ok, detail) in last_results.items():
                    parts.append(f"{name}: {'ok' if ok else 'fail'} ({detail})")
            detail_txt = "; ".join(parts) if parts else "no probe results"
            raise RuntimeError(
                f"DeepProbe timed out after {limit_sec:.0f}s (last round: {detail_txt})."
            )

        all_ok, last_results = deep_probe_once(env_values)
        if all_ok and last_results:
            if logger is not None:
                logger.info("DeepProbe: all checks passed (ollama, postgres, redis)")
            _say(
                "Stack Ready: Ollama /api/tags, Postgres SELECT 1, "
                "Redis PING — all succeeded."
            )
            return

        for name, (ok, detail) in (last_results or {}).items():
            if logger is not None:
                if ok:
                    logger.debug("DeepProbe round: %s ok (%s)", name, detail)
                else:
                    logger.warning("DeepProbe round: %s failed (%s)", name, detail)

        delay = BACKOFF_SECONDS[min(backoff_idx, len(BACKOFF_SECONDS) - 1)]
        backoff_idx += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        sleep_s = min(delay, remaining)
        if logger is not None:
            logger.info(
                "DeepProbe: sleeping %.2fs before next round (%.1fs left on deadline)",
                sleep_s,
                remaining,
            )
        time.sleep(sleep_s)
