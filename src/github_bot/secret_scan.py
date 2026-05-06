"""Fast regex-based secret detection on PR diff text (runs before heavy AI review)."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

_SKIP_ENV = "OCTO_SPORK_SKIP_SECRET_SCAN"


def _skip_scan() -> bool:
    return os.environ.get(_SKIP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _redact(raw: str, *, head: int = 4, tail: int = 4) -> str:
    s = raw.strip()
    if len(s) <= head + tail + 3:
        return "***"
    return f"{s[:head]}…{s[-tail:]}"


@dataclass(frozen=True)
class SecretFinding:
    """One suspected credential span (values are redacted for GitHub display)."""

    category: str
    redacted_preview: str
    pattern_name: str


# Compiled once — tuned for speed on typical PR diffs.
_AWS_ACCESS_KEY_ID = re.compile(
    r"\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}\b",
)
# 40-char AWS secret key material often adjacent to assignment or JSON keys.
_AWS_SECRET_ASSIGN = re.compile(
    r"(?i)(?:aws_secret_access_key|AWS_SECRET_ACCESS_KEY|secret_access_key)\s*[=:]\s*"
    r"['\"]?([A-Za-z0-9/+=]{40})\b",
)
_STRIPE_SK = re.compile(r"\bsk_(?:live|test)_[0-9a-zA-Z]{8,}\b")
_STRIPE_RK = re.compile(r"\brk_(?:live|test)_[0-9a-zA-Z]{8,}\b")
_GITHUB_CLASSIC = re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")
_GITHUB_FG = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")
_GITHUB_APP = re.compile(r"\b(?:gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")

_SCAN_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("AWS", "access key ID", _AWS_ACCESS_KEY_ID),
    ("AWS", "secret access key (assignment)", _AWS_SECRET_ASSIGN),
    ("Stripe", "secret API key (sk_*)", _STRIPE_SK),
    ("Stripe", "restricted key (rk_*)", _STRIPE_RK),
    ("GitHub", "personal access token", _GITHUB_CLASSIC),
    ("GitHub", "fine-grained PAT", _GITHUB_FG),
    ("GitHub", "OAuth / GitHub App token", _GITHUB_APP),
)


def scan_diff_text(diff_text: str, *, max_findings: int = 24) -> list[SecretFinding]:
    """Scan unified diff text for high-risk credential patterns.

    Returns de-duplicated findings (by span position). Values are never returned verbatim —
    only redacted previews suitable for PR comments.
    """
    if _skip_scan():
        return []

    text = diff_text or ""
    if not text.strip():
        return []

    spans_seen: set[tuple[int, int]] = set()
    out: list[SecretFinding] = []

    for vendor, pname, rx in _SCAN_RULES:
        for m in rx.finditer(text):
            start, end = m.span()
            if (start, end) in spans_seen:
                continue
            spans_seen.add((start, end))

            if m.lastindex:
                raw = str(m.group(m.lastindex) or "")
            else:
                raw = str(m.group(0) or "")
            preview = _redact(raw)
            digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
            out.append(
                SecretFinding(
                    category=f"{vendor} ({pname})",
                    redacted_preview=f"{preview} _(id:{digest})_",
                    pattern_name=pname,
                )
            )
            if len(out) >= max_findings:
                return out

    return out


def scan_text_for_pattern_names(
    diff_text: str,
    pattern_names: set[str],
    *,
    max_findings: int = 48,
) -> list[SecretFinding]:
    """Run only credential rules whose ``pattern_name`` is in ``pattern_names`` (fleet / Sovereign Intel)."""
    if _skip_scan():
        return []
    names = {n.strip() for n in pattern_names if str(n).strip()}
    if not names:
        return []

    text = diff_text or ""
    if not text.strip():
        return []

    rules = [r for r in _SCAN_RULES if r[1] in names]
    if not rules:
        return []

    spans_seen: set[tuple[int, int]] = set()
    out: list[SecretFinding] = []

    for vendor, pname, rx in rules:
        for m in rx.finditer(text):
            start, end = m.span()
            if (start, end) in spans_seen:
                continue
            spans_seen.add((start, end))

            if m.lastindex:
                raw = str(m.group(m.lastindex) or "")
            else:
                raw = str(m.group(0) or "")
            preview = _redact(raw)
            digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
            out.append(
                SecretFinding(
                    category=f"{vendor} ({pname})",
                    redacted_preview=f"{preview} _(id:{digest})_",
                    pattern_name=pname,
                )
            )
            if len(out) >= max_findings:
                return out

    return out


def format_critical_alert_comment(
    *,
    html_url: str,
    title: str,
    findings: list[SecretFinding],
    skipped_ai: bool = True,
) -> str:
    """Markdown for issue/review comment — never includes raw secrets."""
    lines = [
        "## Critical Alert — suspected credentials in PR diff",
        "",
        f"**PR:** [{title}]({html_url})",
        "",
        "Automated regex scan matched patterns consistent with **AWS keys**, **Stripe secrets**, "
        "or **GitHub tokens** in the unified diff.",
        "",
    ]
    if skipped_ai:
        lines.extend(
            [
                "**Heavy AI review was skipped** to save compute after this alert.",
                "",
            ]
        )
    lines.extend(
        [
            "| Kind | Redacted preview |",
            "| --- | --- |",
        ]
    )
    for f in findings:
        cat = f.category.replace("|", "\\|")
        prev = f.redacted_preview.replace("|", "\\|")
        lines.append(f"| {cat} | {prev} |")
    lines.extend(
        [
            "",
            "**Action:** Rotate any exposed credentials immediately; treat this branch as compromised.",
            "",
            "_This check is heuristic — verify each hit in context._",
        ]
    )
    return "\n".join(lines)
