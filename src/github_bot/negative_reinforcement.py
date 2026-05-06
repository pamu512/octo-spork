"""Agent-facing **negative reinforcement** copy when CVE :class:`remediation.rescan_loop.RescanLoop` fails."""

from __future__ import annotations

import os


def _max_error_chars() -> int:
    raw = (os.environ.get("OCTO_NEGATIVE_REINFORCEMENT_ERROR_MAX_CHARS") or "6000").strip()
    try:
        return max(200, min(50_000, int(raw)))
    except ValueError:
        return 6000


def negative_reinforcement_prompt(
    *,
    validation_error: str,
    max_error_chars: int | None = None,
) -> str:
    """
    Prompt instructing the remediation agent to discard the failed approach after validation failure.

    Use the verification error text (Trivy snippet, ``VerificationFailedError``, apply stderr, etc.)
    as *validation_error*.
    """
    limit = max_error_chars if max_error_chars is not None else _max_error_chars()
    err = (validation_error or "").strip()
    if len(err) > limit:
        err = err[: max(0, limit - 40)].rstrip() + "\n… _(truncated)_"

    return (
        "Your previous attempt failed validation with error: "
        f"{err}\n\n"
        "This fix is insecure. Analyze the error and provide a different approach. "
        "Do not repeat the previous logic."
    )


def negative_reinforcement_markdown_section(*, validation_error: str) -> str:
    """Markdown block suitable for PR comments (fenced body + short heading)."""
    body = negative_reinforcement_prompt(validation_error=validation_error)
    return (
        "### Negative reinforcement — next agent instruction\n\n"
        "Use the following as **user** or **system** context on the next remediation attempt:\n\n"
        "```text\n"
        f"{body}\n"
        "```\n"
    )
