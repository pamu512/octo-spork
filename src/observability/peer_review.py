"""Fast-model screen + optional large-model audit (Peer Review loop)."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

_LOG = logging.getLogger(__name__)

PEER_GATE_SUFFIX = """

---

## Mandatory peer-review gate (machine-readable first line)
Your response MUST start with **exactly one** line (plain text, uppercase keyword):

PEER_ISSUES: YES

or

PEER_ISSUES: NO

(Do not wrap this line in markdown fences.)

Use **YES** if there is **any** substantive issue worth developer attention: Critical / High / Medium severity,
security exposure, likely correctness bug, missing coverage for risky logic, or notable regression risk.

Use **NO** only when the change-set appears sound with at most trivial or stylistic nits you could mention briefly.

After that first line, output **one blank line**, then your full markdown review (sections as usual).
"""


def peer_review_enabled() -> bool:
    return os.environ.get("OCTO_PEER_REVIEW_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolve_fast_model(explicit: str | None = None) -> str:
    return (
        (explicit or os.environ.get("OCTO_PEER_FAST_MODEL") or "llama3.2:3b").strip()
        or "llama3.2:3b"
    )


def cache_model_label(primary_model: str, fast_model: str, peer_used: bool) -> str:
    """Cache key fragment when peer loop alters model usage."""
    if not peer_used:
        return primary_model
    return f"peer:{fast_model}|audit:{primary_model}"


def parse_peer_gate(raw_text: str) -> tuple[bool | None, str]:
    """Return (issues_flagged, body_without_gate). ``None`` means ambiguous → caller audits."""
    text = raw_text or ""
    lines = text.replace("\r\n", "\n").split("\n")
    gate_idx = None
    flag: bool | None = None
    for i, raw_line in enumerate(lines[:40]):
        line = raw_line.strip()
        if not line:
            continue
        line = line.strip("`").strip("*").strip()
        m = re.match(r"PEER_ISSUES:\s*(YES|NO)\s*$", line, re.IGNORECASE)
        if m:
            gate_idx = i
            flag = m.group(1).upper() == "YES"
            break

    if gate_idx is None:
        _LOG.warning("peer_review: missing PEER_ISSUES line; treating as issues present (safe default)")
        return None, text.strip()

    rest_lines = lines[gate_idx + 1 :]
    while rest_lines and not rest_lines[0].strip():
        rest_lines = rest_lines[1:]
    body = "\n".join(rest_lines).strip()
    body = re.sub(r"^\s*[`]{3}(?:markdown)?\s*\n?", "", body)
    return flag, body


def build_audit_prompt(
    *,
    query: str,
    fast_review_body: str,
    snapshot: dict[str, Any],
    map_digest: str,
) -> str:
    """Prompt for the larger model to validate and finalize findings."""
    owner = str(snapshot.get("owner") or "")
    repo = str(snapshot.get("repo") or "")
    branch = str(snapshot.get("default_branch") or "main")
    digest = (map_digest or "").strip() or "(none)"

    body_cap = int(os.environ.get("OCTO_PEER_AUDIT_FAST_BODY_CHARS", "56000"))
    clipped = fast_review_body if len(fast_review_body) <= body_cap else (
        fast_review_body[: body_cap - 80] + "\n\n… _(fast review truncated for audit prompt)_\n"
    )

    kb_block = ""
    try:
        from observability.knowledge_base import format_domain_constraints_block_for_review

        _kb = format_domain_constraints_block_for_review()
        if _kb:
            kb_block = "\n\n" + _kb + "\n\n---\n"
    except ImportError:
        pass

    return f"""You are the **senior audit reviewer** for this pull request / repository review.
{kb_block}
A smaller, faster model produced an initial review below. Your responsibilities:
1. **Validate** each claimed issue — remove false positives and hallucinations.
2. **Escalate** if evidence clearly supports a higher severity than the peer stated.
3. **Catch** serious gaps the peer missed (especially security and correctness).
4. Produce the **single final markdown review** for the developer audience.

Do **not** describe this pipeline or mention "peer model" / "audit model" in the output.

---

### User request
{query}

### Repository
- **owner/repo:** `{owner}/{repo}`
- **branch:** `{branch}`
- **Map digest (if any):** {digest}

---

### Peer review (fast model — verify against evidence contracts in full prompt context)
{clipped}

---

Respond with **complete markdown** using your usual sections (summary, severity-ranked findings, etc.).
Ground statements in the evidence that was available to the peer; if you cannot verify, say so explicitly.
"""
