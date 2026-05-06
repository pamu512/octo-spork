"""Regex-based PII / secret redaction before LLM calls, with symmetric un-redaction on responses.

Disable entirely with ``OCTO_PRIVACY_FILTER=0``. Placeholders look like ``<REDACTED_SECRET_1>``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Pattern


def is_enabled() -> bool:
    return (os.environ.get("OCTO_PRIVACY_FILTER") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _compile_patterns() -> list[tuple[str, Pattern[str]]]:
    return [
        (
            "pem",
            re.compile(
                r"-----BEGIN [A-Z0-9 ]+-----\r?\n[\s\S]{1,64000}?-----END [A-Z0-9 ]+-----",
                re.MULTILINE,
            ),
        ),
        ("email", re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")),
        (
            "ipv4",
            re.compile(
                r"(?<![0-9])(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?![0-9])"
            ),
        ),
        ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
        ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
        ("github_oauth", re.compile(r"\bgho_[A-Za-z0-9]{36,}\b")),
        ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
        ("openai_sk", re.compile(r"\bsk-[a-zA-Z0-9_-]{20,}\b")),
        ("bearer", re.compile(r"(?i)Bearer\s+\S+")),
        (
            "assignment_secret",
            re.compile(
                r"(?i)(?:api[_-]?key|secret|token|password|passwd|pwd)\s*=\s*\S+"
            ),
        ),
    ]


_PATTERNS = _compile_patterns()


def _merge_matches(text: str) -> list[tuple[int, int, str]]:
    """Merge overlapping regex hits into disjoint spans (document order), slice from *text*."""
    intervals: list[tuple[int, int]] = []
    for _name, rx in _PATTERNS:
        for m in rx.finditer(text):
            intervals.append((m.start(), m.end()))
    if not intervals:
        return []
    intervals.sort(key=lambda t: t[0])
    merged_iv: list[tuple[int, int]] = []
    for s, e in intervals:
        if not merged_iv:
            merged_iv.append((s, e))
            continue
        ps, pe = merged_iv[-1]
        if s <= pe:
            merged_iv[-1] = (ps, max(pe, e))
        else:
            merged_iv.append((s, e))
    return [(s, e, text[s:e]) for s, e in merged_iv]


def redact_for_llm(text: str) -> tuple[str, dict[str, str]]:
    """Replace sensitive substrings with ``<REDACTED_SECRET_N>``; return mapping token → original."""
    if not is_enabled() or not text:
        return text, {}
    spans = _merge_matches(text)
    if not spans:
        return text, {}
    mapping: dict[str, str] = {}
    out = text
    for i in range(len(spans) - 1, -1, -1):
        s, e, orig = spans[i]
        token = f"<REDACTED_SECRET_{i + 1}>"
        mapping[token] = orig
        out = out[:s] + token + out[e:]
    return out, mapping


def unredact_response(text: str, mapping: dict[str, str]) -> str:
    """Restore placeholders produced by :func:`redact_for_llm` when they appear verbatim in the model output."""
    if not mapping or not text:
        return text
    out = text
    for token in sorted(mapping.keys(), key=len, reverse=True):
        out = out.replace(token, mapping[token])
    return out


@dataclass
class PrivacyFilter:
    """Middleware-style helper: redact outbound text, un-redact inbound LLM text."""

    enabled: bool = field(default_factory=is_enabled)

    def filter_request(self, text: str) -> tuple[str, dict[str, str]]:
        if not self.enabled or not text:
            return text, {}
        return redact_for_llm(text)

    def filter_response(self, text: str, mapping: dict[str, str]) -> str:
        if not self.enabled or not mapping:
            return text
        return unredact_response(text, mapping)
