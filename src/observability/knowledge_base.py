"""Load Markdown domain rules from ``grounding/rules/`` for CTI/fraud (and related) review constraints."""

from __future__ import annotations

import logging
import os
from pathlib import Path

_LOG = logging.getLogger(__name__)

_REL_RULES = Path("grounding") / "rules"


def domain_constraints_enabled() -> bool:
    return os.environ.get("OCTO_DOMAIN_CONSTRAINTS_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _workspace_root() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def grounding_rules_dir(repo_root: Path | None = None) -> Path:
    """Directory containing ``*.md`` rule files (override with :envvar:`OCTO_GROUNDING_RULES_DIR`)."""
    override = (os.environ.get("OCTO_GROUNDING_RULES_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    root = repo_root if repo_root is not None else _workspace_root()
    return (root / _REL_RULES).resolve()


def _max_chars() -> int:
    raw = (os.environ.get("OCTO_GROUNDING_RULES_MAX_CHARS") or "").strip()
    if raw.isdigit():
        return max(4096, int(raw))
    return 48_000


def load_domain_constraints_markdown(
    *,
    repo_root: Path | None = None,
    max_total_chars: int | None = None,
) -> str:
    """Concatenate all ``grounding/rules/*.md`` into one markdown document (sorted by filename).

    Returns empty string when disabled, when the directory is missing, or when no ``*.md`` files exist.
    """
    if not domain_constraints_enabled():
        return ""
    rules_dir = grounding_rules_dir(repo_root)
    if not rules_dir.is_dir():
        return ""

    paths = sorted(
        p for p in rules_dir.iterdir() if p.is_file() and p.suffix.lower() == ".md"
    )
    if not paths:
        return ""

    cap = max_total_chars if max_total_chars is not None else _max_chars()
    intro = (
        "## Domain Constraints\n\n"
        "_The following repository-local knowledge defines fraud-infrastructure, CTI, and policy "
        "expectations. Treat it as hard requirements for triage and severity — still ground claims in "
        "the evidence sections of this prompt._\n\n"
    )
    parts: list[str] = [intro]
    for p in paths:
        try:
            raw_file = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            _LOG.warning("knowledge_base: skip %s: %s", p, exc)
            continue
        parts.append(f"### Rules file: `{p.name}`\n\n{raw_file}\n\n")
    text = "".join(parts).strip() + "\n"
    if len(text) > cap:
        text = (
            text[: cap - 140]
            + "\n\n_… [truncated; raise OCTO_GROUNDING_RULES_MAX_CHARS to include more]_\n"
        )
    return text


def format_domain_constraints_block_for_review() -> str:
    """Markdown section for long-form grounded / narrative review prompts."""
    body = load_domain_constraints_markdown()
    return body.strip()


def domain_constraints_system_append() -> str:
    """Compact appendix for strict JSON system prompts (PR findings agent)."""
    if not domain_constraints_enabled():
        return ""
    body = load_domain_constraints_markdown()
    if not body:
        return ""
    return (
        "\n\n**Domain policy:** Apply the **Domain Constraints** below when classifying `severity` and "
        "`issue_type`. "
        "`evidence_quote` must remain verbatim from the PR/diff only.\n\n"
        f"{body}"
    )
