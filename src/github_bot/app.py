"""FastAPI server for GitHub webhooks with HMAC signature verification."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import redis.asyncio as redis_async
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from github_bot.auth import GitHubAuth
from github_bot.context_docs import router as context_docs_router
from github_bot.delivery_cache import is_valid_delivery_id, try_claim_delivery
from github_bot.temp_clone_cleanup import cleanup_stale_temp_clones
from github_bot.review_queue import (
    default_pr_review_worker,
    octo_spork_issue_comment_command,
    pr_html_url_from_issue_comment_payload,
    pr_html_url_from_pull_request_payload,
    queue_depth,
    schedule_pr_review_or_queue,
    should_gate_pull_request_review,
    should_schedule_octo_spork_from_issue_comment,
)
from github_bot.style_prefs import (
    process_issue_comment_edited_payload,
    sender_login,
    should_learn_style_from_issue_comment,
)

load_dotenv()
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_ENV = _REPO_ROOT / "deploy" / "local-ai" / ".env.local"
if _LOCAL_ENV.is_file():
    load_dotenv(_LOCAL_ENV, override=True)

_LOG = logging.getLogger(__name__)


async def _style_learn_background(payload: dict[str, Any]) -> None:
    try:
        await asyncio.to_thread(process_issue_comment_edited_payload, payload)
    except Exception:
        _LOG.exception("style_learn: background merge failed")


def _temp_clone_cleanup_enabled() -> bool:
    return os.environ.get("OCTO_SPORK_TEMP_CLONE_CLEANUP_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Open a shared Redis connection for delivery de-duplication."""
    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    client = redis_async.from_url(url, decode_responses=True)
    await client.ping()
    app.state.redis = client

    cleanup_task: asyncio.Task[None] | None = None
    if _temp_clone_cleanup_enabled():
        interval_sec = max(60, int(os.environ.get("OCTO_SPORK_TEMP_CLONE_CLEANUP_INTERVAL_SEC", "3600")))

        async def _temp_clone_maintenance_loop() -> None:
            while True:
                try:
                    summary = await asyncio.to_thread(cleanup_stale_temp_clones)
                    if summary.get("removed") or summary.get("errors"):
                        _LOG.info("temp_clone_cleanup: %s", summary)
                except Exception:
                    _LOG.exception("temp_clone_cleanup sweep failed")
                await asyncio.sleep(interval_sec)

        cleanup_task = asyncio.create_task(_temp_clone_maintenance_loop())

    try:
        yield
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        await client.aclose()


app = FastAPI(
    title="GitHub Webhook Bot",
    description=(
        "Receives GitHub webhooks with HMAC-SHA256 signature verification "
        "and Redis-backed idempotency (X-GitHub-Delivery)."
    ),
    lifespan=_lifespan,
)

app.include_router(context_docs_router)


async def _pr_review_worker(envelope: dict[str, Any]) -> None:
    """Installation token (when configured) then heavy review (LLM) pipeline."""
    payload = envelope.get("payload")
    auth = _github_auth_from_env()
    if auth is not None and isinstance(payload, dict):
        installation_id = GitHubAuth.installation_id_from_payload(payload)
        if installation_id is not None:
            await asyncio.to_thread(auth.get_installation_access_token, installation_id)
    if envelope.get("octo_command") == "fix":
        from github_bot.fix_it_worker import run_fix_remediation_pr

        await run_fix_remediation_pr(envelope)
        return
    await default_pr_review_worker(envelope)


def _github_auth_from_env() -> GitHubAuth | None:
    """Construct :class:`GitHubAuth` when ``GITHUB_APP_ID`` and ``GITHUB_APP_PRIVATE_KEY_PATH`` are set."""
    app_id = os.environ.get("GITHUB_APP_ID")
    key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if not app_id or not str(app_id).strip() or not key_path or not str(key_path).strip():
        return None
    return GitHubAuth(app_id=str(app_id).strip(), private_key_path=str(key_path).strip())


def _allowed_github_logins() -> frozenset[str]:
    """Comma-separated GitHub usernames in ``ALLOWED_USERS`` (case-insensitive; whitespace trimmed)."""
    raw = os.environ.get("ALLOWED_USERS", "")
    if not str(raw).strip():
        return frozenset()
    return frozenset(
        p.strip().lower() for p in str(raw).split(",") if p.strip()
    )


def _issue_comment_author_login(payload: dict[str, Any]) -> str | None:
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return None
    user = comment.get("user")
    if not isinstance(user, dict):
        return None
    login = user.get("login")
    if not isinstance(login, str) or not login.strip():
        return None
    return login.strip()


async def verify_signature(request: Request) -> None:
    """Verify ``X-Hub-Signature-256`` against HMAC-SHA256 of the raw body.

    Uses ``GITHUB_WEBHOOK_SECRET`` from the environment (loaded from ``.env`` via
    :func:`dotenv.load_dotenv`). Comparison uses :func:`hmac.compare_digest`,
    which provides the same timing-attack resistance as Node.js
    ``crypto.timingSafeEqual`` when comparing the computed digest bytes to the
    digest parsed from the header.
    """
    secret_raw = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if secret_raw is None or not str(secret_raw).strip():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GITHUB_WEBHOOK_SECRET is not configured",
        )
    secret_key = str(secret_raw).encode("utf-8")

    header_value = request.headers.get("X-GitHub-Signature-256")
    if header_value is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Hub-Signature-256 header",
        )

    header_value = header_value.strip()
    prefix = "sha256="
    if not header_value.startswith(prefix):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-Hub-Signature-256 format (expected sha256=<hex>)",
        )

    provided_hex = header_value[len(prefix) :].strip()
    if not provided_hex:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty signature digest in X-Hub-Signature-256",
        )

    raw_body = await request.body()

    expected_digest = hmac.new(secret_key, raw_body, hashlib.sha256).digest()

    try:
        provided_digest = bytes.fromhex(provided_hex)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Signature is not valid hexadecimal",
        ) from exc

    digest_size = hashlib.sha256().digest_size
    if len(provided_digest) != digest_size:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature digest length",
        )

    if not hmac.compare_digest(expected_digest, provided_digest):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Signature verification failed",
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook", dependencies=[Depends(verify_signature)], response_model=None)
async def github_webhook(request: Request) -> JSONResponse | dict[str, Any]:
    """Handle verified GitHub webhooks (payload is JSON).

    Idempotent per ``X-GitHub-Delivery``: duplicate deliveries receive ``202 Accepted``
    without re-running installation token / downstream AI work.
    """
    raw_delivery = request.headers.get("X-GitHub-Delivery")
    if not raw_delivery or not str(raw_delivery).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-GitHub-Delivery header",
        )
    delivery_id = str(raw_delivery).strip()
    if not is_valid_delivery_id(delivery_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-GitHub-Delivery (expected UUID)",
        )

    redis_client: redis_async.Redis = request.app.state.redis
    claim_ok = await try_claim_delivery(redis_client, delivery_id)
    if not claim_ok:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "duplicate_delivery",
                "delivery": delivery_id,
                "message": "Already processed; skipping downstream handling.",
            },
        )

    payload = await request.json()
    headers_map = {str(k): str(v) for k, v in request.headers.items()}
    out: dict[str, Any] = {
        "ok": True,
        "event": request.headers.get("X-GitHub-Event"),
        "delivery": delivery_id,
        "zen": payload.get("zen") if isinstance(payload, dict) else None,
    }
    if isinstance(payload, dict) and should_learn_style_from_issue_comment(headers_map, payload):
        allowed = _allowed_github_logins()
        editor = sender_login(payload)
        editor_l = editor.lower() if editor else None
        if not allowed or not editor_l or editor_l not in allowed:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "ok": False,
                    "error": "permission_denied",
                    "message": "User is not authorized to record style preferences (ALLOWED_USERS).",
                    "delivery": delivery_id,
                },
            )
        asyncio.create_task(_style_learn_background(payload))
        out["style_learn"] = "scheduled"
        out["trigger"] = "issue_comment_style_edit"
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=out)

    if isinstance(payload, dict) and should_schedule_octo_spork_from_issue_comment(
        headers_map, payload
    ):
        allowed = _allowed_github_logins()
        author = _issue_comment_author_login(payload)
        author_l = author.lower() if author else None
        if not author_l or author_l not in allowed:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "ok": False,
                    "error": "permission_denied",
                    "message": "User is not authorized to trigger Octo-spork PR commands (/octo-spork analyze or /octo-spork fix).",
                    "delivery": delivery_id,
                },
            )
        pr_url = pr_html_url_from_issue_comment_payload(payload)
        if not pr_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not determine pull request URL from issue_comment payload",
            )
        comment_obj = payload.get("comment")
        comment_body = (
            comment_obj.get("body")
            if isinstance(comment_obj, dict) and isinstance(comment_obj.get("body"), str)
            else None
        )
        envelope: dict[str, Any] = {
            "payload": payload,
            "delivery": delivery_id,
            "event": request.headers.get("X-GitHub-Event"),
            "headers": headers_map,
            "trigger": "issue_comment",
            "pr_html_url": pr_url,
            "octo_command": octo_spork_issue_comment_command(comment_body),
        }
        pr_status = await schedule_pr_review_or_queue(redis_client, envelope, _pr_review_worker)
        out["pr_review"] = pr_status
        out["trigger"] = "issue_comment"
        out["pr_html_url"] = pr_url
        out["octo_command"] = envelope.get("octo_command")
        if pr_status == "queued":
            out["queue_depth"] = await queue_depth(redis_client)
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=out)

    if isinstance(payload, dict) and should_gate_pull_request_review(headers_map, payload):
        pr_pull_url = pr_html_url_from_pull_request_payload(payload)
        envelope = {
            "payload": payload,
            "delivery": delivery_id,
            "event": request.headers.get("X-GitHub-Event"),
            "headers": headers_map,
            "trigger": "pull_request",
            "pr_html_url": pr_pull_url,
        }
        pr_status = await schedule_pr_review_or_queue(redis_client, envelope, _pr_review_worker)
        out["pr_review"] = pr_status
        out["trigger"] = "pull_request"
        out["pr_html_url"] = pr_pull_url
        if pr_status == "queued":
            out["queue_depth"] = await queue_depth(redis_client)
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=out)

    auth = _github_auth_from_env()
    if auth is not None and isinstance(payload, dict):
        installation_id = GitHubAuth.installation_id_from_payload(payload)
        if installation_id is not None:
            auth.get_installation_access_token(installation_id)
            out["installation_token_ready"] = True
    return out


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
