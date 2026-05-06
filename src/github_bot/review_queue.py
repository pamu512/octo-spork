"""Redis mutex + FIFO queue so only one heavy PR review runs at a time (local LLM friendly)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import traceback
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from redis.asyncio import Redis

from github_bot.developer_note import report_pr_processing_failure

_LOG = logging.getLogger(__name__)

# Binary semaphore: only one holder at a time (SET NX on mutex key).
DEFAULT_MUTEX_KEY = "octo-spork:bot:pr_review:mutex"
DEFAULT_QUEUE_KEY = "octo-spork:bot:pr_review:queue"

_MUTEX_KEY = os.environ.get("GITHUB_PR_REVIEW_MUTEX_KEY", DEFAULT_MUTEX_KEY)
_QUEUE_KEY = os.environ.get("GITHUB_PR_REVIEW_QUEUE_KEY", DEFAULT_QUEUE_KEY)
_MUTEX_TTL_SEC = int(os.environ.get("GITHUB_PR_REVIEW_MUTEX_TTL_SEC", "7200"))

Envelope = dict[str, Any]
ReviewWorker = Callable[[Envelope], Awaitable[None]]


async def acquire_review_mutex(redis: Redis, *, ttl_sec: int | None = None) -> bool:
    """Try to acquire the global PR-review mutex (``SET NX EX``).

    Returns ``True`` if this caller holds the mutex and may start a review.
    """
    ttl = ttl_sec if ttl_sec is not None else _MUTEX_TTL_SEC
    ok = await redis.set(_MUTEX_KEY, "1", nx=True, ex=ttl)
    return ok is True


async def extend_review_mutex(redis: Redis, *, ttl_sec: int | None = None) -> None:
    """Refresh mutex TTL while a long-running review is active."""
    ttl = ttl_sec if ttl_sec is not None else _MUTEX_TTL_SEC
    await redis.expire(_MUTEX_KEY, ttl)


async def enqueue_review(redis: Redis, envelope: Envelope) -> int:
    """Append a serialized webhook envelope to the FIFO queue; returns new queue length."""
    raw = json.dumps(envelope, sort_keys=True)
    return int(await redis.rpush(_QUEUE_KEY, raw))


async def queue_depth(redis: Redis) -> int:
    return int(await redis.llen(_QUEUE_KEY))


async def release_mutex_and_run_next(
    redis: Redis,
    *,
    worker: ReviewWorker,
    ttl_sec: int | None = None,
) -> None:
    """Release mutex after a review finishes, then run the next queued job if any.

    Chains asynchronously: each completed review schedules the next waiter with
    :func:`asyncio.create_task`, so only one review executes at a time.
    """
    await redis.delete(_MUTEX_KEY)
    raw = await redis.lpop(_QUEUE_KEY)
    if raw is None:
        return

    ttl = ttl_sec if ttl_sec is not None else _MUTEX_TTL_SEC
    acquired = await redis.set(_MUTEX_KEY, "1", nx=True, ex=ttl)
    if acquired is not True:
        await redis.lpush(_QUEUE_KEY, raw)
        _LOG.warning(
            "Could not re-acquire PR review mutex after dequeue; re-queued delivery for retry."
        )
        return

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        _LOG.error("Corrupt queue payload dropped: %s", exc)
        await redis.delete(_MUTEX_KEY)
        asyncio.create_task(release_mutex_and_run_next(redis, worker=worker, ttl_sec=ttl_sec))
        return

    asyncio.create_task(_run_review_chain(envelope, redis, worker, ttl_sec=ttl_sec))


async def _run_review_chain(
    envelope: Envelope,
    redis: Redis,
    worker: ReviewWorker,
    *,
    ttl_sec: int | None,
) -> None:
    try:
        await worker(envelope)
    except Exception as exc:
        tb_text = traceback.format_exc()
        _LOG.error(
            "PR review worker failed delivery=%s\n%s",
            envelope.get("delivery"),
            tb_text,
        )
        try:
            await asyncio.to_thread(report_pr_processing_failure, envelope, exc, tb_text)
        except Exception:
            _LOG.exception(
                "Developer-note reporter failed after worker error delivery=%s",
                envelope.get("delivery"),
            )
    finally:
        await release_mutex_and_run_next(redis, worker=worker, ttl_sec=ttl_sec)


async def schedule_pr_review_or_queue(
    redis: Redis,
    envelope: Envelope,
    worker: ReviewWorker,
    *,
    ttl_sec: int | None = None,
) -> str:
    """Acquire mutex or enqueue. If mutex acquired, start the review chain in the background.

    Returns ``\"started\"`` when this delivery begins reviewing immediately, or ``\"queued\"``
    when another review holds the mutex.
    """
    ttl = ttl_sec if ttl_sec is not None else _MUTEX_TTL_SEC
    got = await acquire_review_mutex(redis, ttl_sec=ttl)
    if not got:
        depth = await enqueue_review(redis, envelope)
        _LOG.info(
            "PR review queued (mutex busy) delivery=%s queue_depth=%s",
            envelope.get("delivery"),
            depth,
        )
        return "queued"

    _LOG.info("PR review started delivery=%s", envelope.get("delivery"))
    asyncio.create_task(_run_review_chain(envelope, redis, worker, ttl_sec=ttl))
    return "started"


def should_gate_pull_request_review(headers: dict[str, str], payload: dict[str, Any]) -> bool:
    """Return True when this hook should use the mutex + queue (heavy PR review)."""
    event = (headers.get("X-GitHub-Event") or headers.get("x-github-event") or "").strip()
    if event != "pull_request":
        return False
    action = str(payload.get("action") or "").strip().lower()
    return action in {"opened", "synchronize", "reopened", "ready_for_review"}


_OCTO_SPORK_ANALYZE_FIRST_LINE = re.compile(
    r"^/octo-spork\s+analyze(?:\s+.*)?$",
    re.IGNORECASE,
)
_OCTO_SPORK_FIX_FIRST_LINE = re.compile(
    r"^/octo-spork\s+fix(?:\s+.*)?$",
    re.IGNORECASE,
)

OctoSporkIssueCommand = Literal["analyze", "fix"]


def octo_spork_issue_comment_command(body: str | None) -> OctoSporkIssueCommand | None:
    """Parse ``/octo-spork analyze`` or ``/octo-spork fix`` from the first line of a PR comment."""
    if not body or not str(body).strip():
        return None
    first_line = str(body).strip().splitlines()[0].strip()
    if _OCTO_SPORK_ANALYZE_FIRST_LINE.match(first_line):
        return "analyze"
    if _OCTO_SPORK_FIX_FIRST_LINE.match(first_line):
        return "fix"
    return None


def comment_body_invokes_octo_spork_analyze(body: str | None) -> bool:
    """True when the first line of the comment is ``/octo-spork analyze`` (optional trailing text)."""
    return octo_spork_issue_comment_command(body) == "analyze"


def issue_comment_targets_pull_request(payload: dict[str, Any]) -> bool:
    """True when the comment is on a pull request (issue has ``pull_request`` link)."""
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return False
    return bool(issue.get("pull_request"))


def should_schedule_octo_spork_from_issue_comment(
    headers: dict[str, str], payload: dict[str, Any]
) -> bool:
    """Return True when this is a new PR comment invoking ``/octo-spork analyze`` or ``/octo-spork fix``."""
    event = (headers.get("X-GitHub-Event") or headers.get("x-github-event") or "").strip()
    if event != "issue_comment":
        return False
    if str(payload.get("action") or "").strip().lower() != "created":
        return False
    if not issue_comment_targets_pull_request(payload):
        return False
    comment_obj = payload.get("comment")
    if not isinstance(comment_obj, dict):
        return False
    body = comment_obj.get("body")
    return octo_spork_issue_comment_command(body if isinstance(body, str) else None) is not None


def pr_html_url_from_pull_request_payload(payload: dict[str, Any]) -> str | None:
    """Build PR HTML URL from a ``pull_request`` webhook JSON body."""
    repo = payload.get("repository")
    pr = payload.get("pull_request")
    if not isinstance(repo, dict) or not isinstance(pr, dict):
        return None
    full = repo.get("full_name")
    num = pr.get("number")
    if not isinstance(full, str) or not full.strip():
        return None
    if not isinstance(num, int):
        return None
    return f"https://github.com/{full.strip()}/pull/{num}"


def pr_html_url_from_issue_comment_payload(payload: dict[str, Any]) -> str | None:
    """Build ``https://github.com/{owner}/{repo}/pull/{n}`` for an ``issue_comment`` on a PR."""
    repo = payload.get("repository")
    issue = payload.get("issue")
    if not isinstance(repo, dict) or not isinstance(issue, dict):
        return None
    full = repo.get("full_name")
    num = issue.get("number")
    if not isinstance(full, str) or not full.strip():
        return None
    if not isinstance(num, int):
        return None
    return f"https://github.com/{full.strip()}/pull/{num}"


async def default_pr_review_worker(envelope: Envelope) -> None:
    """PR webhook worker: optional draft sources + Review Refiner + GitHub comment."""
    from github_bot.pr_review_runner import execute_webhook_pr_review

    await execute_webhook_pr_review(envelope)
