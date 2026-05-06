"""Webhook PR review delivery: resolve AI draft, optionally refine via Claude Code, post to GitHub."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)


def _github_token() -> str | None:
    return (
        (os.environ.get("GITHUB_TOKEN") or "").strip()
        or (os.environ.get("GH_TOKEN") or "").strip()
        or None
    )


def _resolve_pr_html_url(envelope: dict[str, Any]) -> str | None:
    direct = envelope.get("pr_html_url")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    from github_bot.review_queue import (
        pr_html_url_from_issue_comment_payload,
        pr_html_url_from_pull_request_payload,
    )

    trigger = envelope.get("trigger")
    if trigger == "issue_comment":
        return pr_html_url_from_issue_comment_payload(payload)
    if trigger == "pull_request":
        return pr_html_url_from_pull_request_payload(payload)
    return None


def _parse_pr_url(pr_html_url: str) -> tuple[str, str, int]:
    from github_bot.fix_it_worker import parse_pr_html_url

    return parse_pr_html_url(pr_html_url)


def _resolve_review_draft(envelope: dict[str, Any]) -> tuple[str | None, str]:
    """Return ``(draft_markdown, source_label)`` for refinement/posting."""
    raw_env = (os.environ.get("OCTO_PR_REVIEW_DRAFT") or "").strip()
    if raw_env:
        return raw_env, "OCTO_PR_REVIEW_DRAFT"

    path_raw = (os.environ.get("OCTO_AI_FINDINGS_JSON_FILE") or "").strip()
    if path_raw:
        p = Path(path_raw).expanduser()
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            from github_bot.review_formatter import ReviewFormatter

            return ReviewFormatter().format(data).markdown, str(p)

    return None, ""


def _post_issue_comment(owner: str, repo: str, pr_number: int, body: str, token: str) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
    payload = json.dumps({"body": body}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "octo-spork-pr-review-runner",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        if getattr(resp, "status", 200) not in (200, 201):
            raw = resp.read()[:2000].decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub comment unexpected status {resp.status}: {raw}")


def run_webhook_pr_review_sync(envelope: dict[str, Any]) -> None:
    """Sync worker: build draft, refine if enabled, post refined-only when strict."""
    delivery = envelope.get("delivery")
    trigger = envelope.get("trigger")
    pr_url = _resolve_pr_html_url(envelope)
    if not pr_url:
        _LOG.info(
            "pr_review_runner: no PR URL for delivery=%s trigger=%s; skipping.",
            delivery,
            trigger,
        )
        return

    draft, src_label = _resolve_review_draft(envelope)
    if not draft:
        _LOG.info(
            "pr_review_runner: no review draft (set OCTO_PR_REVIEW_DRAFT or OCTO_AI_FINDINGS_JSON_FILE). "
            "delivery=%s trigger=%s pr=%s",
            delivery,
            trigger,
            pr_url,
        )
        return

    token = _github_token()
    if not token:
        _LOG.warning("pr_review_runner: GITHUB_TOKEN/GH_TOKEN missing; cannot post comment.")
        return

    owner, repo_name, pr_number = _parse_pr_url(pr_url)

    ctx_parts = [
        f"- PR: {pr_url}",
        f"- Draft source: {src_label or 'unknown'}",
        f"- Webhook trigger: {trigger}",
        f"- Delivery: {delivery}",
    ]
    pr_context = "\n".join(ctx_parts)

    from github_bot.review_refiner import refinement_enabled, refine_review_or_original

    ws = Path(os.environ.get("OCTO_SPORK_REPO_ROOT") or "").expanduser()
    if str(ws).strip():
        workspace = ws.resolve()
    else:
        workspace = Path(__file__).resolve().parents[2]

    body_to_post = refine_review_or_original(
        draft,
        pr_context=pr_context,
        workspace=workspace,
    )

    try:
        from github_bot.negative_constraint import build_negative_constraint_section

        ollama_url = (
            (os.environ.get("OLLAMA_LOCAL_URL") or os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434")
            .strip()
            .rstrip("/")
        )
        nc_model = (
            (os.environ.get("OCTO_NEGATIVE_CONSTRAINT_MODEL") or os.environ.get("OLLAMA_MODEL") or "llama3.2")
            .strip()
            or "llama3.2"
        )
        nc_block = build_negative_constraint_section(
            body_to_post,
            pr_context=pr_context,
            ollama_base_url=ollama_url,
            model=nc_model,
        )
        if nc_block.strip():
            body_to_post = nc_block.strip() + "\n\n---\n\n" + body_to_post
    except Exception as exc:
        _LOG.warning("pr_review_runner: negative constraint skipped: %s", exc)

    if refinement_enabled() and "Review refinement unavailable" in body_to_post:
        _LOG.warning(
            "pr_review_runner: refinement failed for delivery=%s; posting fallback notice only.",
            delivery,
        )

    refine_note = (
        "This comment was passed through the **Review Refiner** (local Claude Code) before posting."
        if refinement_enabled()
        else "Review Refiner is off (`OCTO_REVIEW_REFINER_ENABLED`); raw draft was posted."
    )
    footer = f"\n\n---\n\n<sub>Posted by octo-spork webhook — {refine_note}</sub>\n"
    final_body = body_to_post + footer
    max_chars = int(os.environ.get("GITHUB_COMMENT_MAX_CHARS", "62000"))
    if len(final_body) > max_chars:
        final_body = (
            final_body[: max_chars - 120]
            + "\n\n_(Comment truncated to size limit.)_\n"
            + footer
        )

    try:
        _post_issue_comment(owner, repo_name, pr_number, final_body, token)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        _LOG.error("pr_review_runner: GitHub HTTP %s: %s", exc.code, detail)
        raise
    except urllib.error.URLError as exc:
        _LOG.error("pr_review_runner: network error posting comment: %s", exc)
        raise

    _LOG.info(
        "pr_review_runner: posted comment on %s/%s#%s delivery=%s refined=%s",
        owner,
        repo_name,
        pr_number,
        delivery,
        refinement_enabled(),
    )


async def execute_webhook_pr_review(envelope: dict[str, Any]) -> None:
    await asyncio.to_thread(run_webhook_pr_review_sync, envelope)
