"""Strict SearXNG query policy for repository review and privacy hardening.

When a grounded review is in progress, :func:`strict_repo_review_session` marks the
current task so :class:`searxSearch.searxSearch` can strip PII and repository slugs
from outgoing search queries before they reach upstream engines.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

strict_review_search: ContextVar[bool] = ContextVar("octo_strict_review_searx", default=False)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_GITHUB_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?(?:[/?#][^\s]*)?",
    re.IGNORECASE,
)
# Long lowercase hex tokens (common for git commit / blob SHAs in pasted queries)
_SHA_RE = re.compile(r"\b[a-f0-9]{12,40}\b")
# Naive phone-like clusters (10+ digits with optional separators); avoids short numeric IDs.
_PHONE_RE = re.compile(
    r"\b(?:\+\d{1,3}[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}\b|\b\d{11,16}\b"
)


def _denylist_tokens() -> list[str]:
    raw = os.getenv("OCTO_SPORK_SEARCH_DENYLIST", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


def _truthy_env(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def want_strict_sanitization() -> bool:
    """True when the outgoing query should be redacted (grounded review or OCTO_SPORK_STRICT_SEARX)."""
    if strict_review_search.get():
        return True
    return _truthy_env("OCTO_SPORK_STRICT_SEARX", "false")


def sanitize_searx_query(q: str) -> str:
    """Remove PII, GitHub paths, commit SHAs, and active/denylisted repo tokens from *q*."""
    s = str(q or "").strip()
    if not s:
        return s
    s = _EMAIL_RE.sub("[redacted-email]", s)
    s = _GITHUB_URL_RE.sub("git repository", s)
    s = _SHA_RE.sub("[commit]", s)
    s = _PHONE_RE.sub("[redacted-phone]", s)

    active = os.getenv("OCTO_SPORK_ACTIVE_REPO", "").strip()
    if "/" in active:
        parts = active.split("/", 1)
        owner, repo = parts[0].strip(), parts[1].strip()
        for token in (owner, repo):
            if len(token) >= 2:
                s = re.sub(rf"\b{re.escape(token)}\b", " ", s, flags=re.IGNORECASE)
    for token in _denylist_tokens():
        if len(token) >= 2:
            s = re.sub(rf"\b{re.escape(token)}\b", " ", s, flags=re.IGNORECASE)

    s = re.sub(r"\s+", " ", s).strip()
    return s


@contextmanager
def strict_repo_review_session(owner: str | None, repo: str | None) -> Iterator[None]:
    """Enable strict SearXNG sanitation while a grounded repository review runs."""
    tok = strict_review_search.set(True)
    prev_active = os.environ.get("OCTO_SPORK_ACTIVE_REPO")
    try:
        if owner and repo:
            os.environ["OCTO_SPORK_ACTIVE_REPO"] = f"{owner}/{repo}"
        yield
    finally:
        strict_review_search.reset(tok)
        if prev_active is None:
            os.environ.pop("OCTO_SPORK_ACTIVE_REPO", None)
        else:
            os.environ["OCTO_SPORK_ACTIVE_REPO"] = prev_active
