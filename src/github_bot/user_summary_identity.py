"""Load ``user_summary.json`` and compose operator identity for system prompts (Octo-spork persona)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

_DEFAULT_VALUES = ("local-first compute", "fraud-infra security")
_DEFAULT_STACK = ("Python", "LangGraph")


def user_summary_identity_enabled() -> bool:
    return os.environ.get("OCTO_USER_SUMMARY_ENABLED", "true").strip().lower() in {
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


def _candidate_paths(repo_root: Path | None) -> list[Path]:
    override = (os.environ.get("OCTO_USER_SUMMARY_JSON") or "").strip()
    out: list[Path] = []
    if override:
        out.append(Path(override).expanduser().resolve())
    root = repo_root if repo_root is not None else _workspace_root()
    out.extend(
        [
            root / ".octo" / "user_summary.json",
            root / "USER_SUMMARY.json",
            root / "docs" / "user_summary.json",
        ]
    )
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _coerce_str(v: Any, default: str) -> str:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return default


def _coerce_str_list(v: Any, default: tuple[str, ...]) -> list[str]:
    if isinstance(v, list):
        out = [str(x).strip() for x in v if str(x).strip()]
        return out if out else list(default)
    if isinstance(v, str) and v.strip():
        parts = [p.strip() for p in v.replace(",", "\n").split("\n") if p.strip()]
        return parts if parts else list(default)
    return list(default)


@dataclass
class UserSummaryIdentity:
    """Identity traits merged from defaults + ``user_summary.json``."""

    agent_name: str = "Octo-spork"
    audience: str = "solo developer"
    values: list[str] = field(default_factory=lambda: list(_DEFAULT_VALUES))
    tone: str = "concise_technical"
    technical_depth: str = "high"
    stack_assumptions: list[str] = field(default_factory=lambda: list(_DEFAULT_STACK))
    voice_notes: str = ""

    def baseline_one_liner(self) -> str:
        vals = ", ".join(self.values) if self.values else ", ".join(_DEFAULT_VALUES)
        return (
            f"You are {self.agent_name}, assisting a {self.audience} who values {vals}."
        )

    def tone_instruction(self) -> str:
        t = self.tone.lower().strip()
        if t in {"concise", "concise_technical", "brief"}:
            return (
                "Keep wording tight and technical; prefer bullets and precise references over filler."
            )
        if t in {"neutral", "professional"}:
            return "Use neutral, professional tone suitable for audit logs and PR threads."
        if t in {"friendly", "warm"}:
            return "Use a friendly but still precise engineering tone."
        return f"Match tone preference `{self.tone}` while staying precise and evidence-grounded."

    def depth_instruction(self) -> str:
        d = self.technical_depth.lower().strip()
        if d in {"high", "expert", "advanced"}:
            return (
                "Assume **high technical competence**: skip introductory explanations unless necessary "
                "for severity justification; prefer architecture-level and edge-case reasoning."
            )
        if d in {"medium", "intermediate"}:
            return (
                "Assume solid engineering background; explain non-obvious security or correctness "
                "risks briefly when citing evidence."
            )
        return (
            "Calibrate explanations to technical_depth="
            f"`{self.technical_depth}` without losing precision."
        )

    def stack_instruction(self) -> str:
        if not self.stack_assumptions:
            return ""
        stacks = ", ".join(self.stack_assumptions)
        return (
            f"Assume strong familiarity with **{stacks}** when discussing patterns, imports, and risks."
        )


def _parse_identity_obj(raw: dict[str, Any]) -> UserSummaryIdentity:
    """Merge optional nested ``identity`` object with top-level keys (top-level wins)."""
    merged: dict[str, Any] = {}
    inner = raw.get("identity")
    if isinstance(inner, dict):
        merged.update(inner)
    for k, v in raw.items():
        if k != "identity":
            merged[k] = v

    base = UserSummaryIdentity()
    agent_name = _coerce_str(merged.get("agent_name"), base.agent_name)
    audience = _coerce_str(merged.get("audience"), base.audience)
    tone = _coerce_str(merged.get("tone"), base.tone)
    technical_depth = _coerce_str(merged.get("technical_depth"), base.technical_depth)
    voice_notes = _coerce_str(merged.get("voice_notes"), "")
    values = _coerce_str_list(merged.get("values"), _DEFAULT_VALUES)
    stack_assumptions = _coerce_str_list(merged.get("stack_assumptions"), _DEFAULT_STACK)

    return UserSummaryIdentity(
        agent_name=agent_name,
        audience=audience,
        values=values,
        tone=tone,
        technical_depth=technical_depth,
        stack_assumptions=stack_assumptions,
        voice_notes=voice_notes,
    )


def load_user_summary_identity(repo_root: Path | None = None) -> UserSummaryIdentity:
    """Load identity from the first existing candidate JSON path; otherwise defaults."""
    for path in _candidate_paths(repo_root):
        if not path.is_file():
            continue
        try:
            raw_text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not raw_text:
                continue
            payload = json.loads(raw_text)
            if not isinstance(payload, dict):
                continue
            ident = _parse_identity_obj(payload)
            _LOG.debug("user_summary_identity: loaded %s", path)
            return ident
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            _LOG.warning("user_summary_identity: skip %s: %s", path, exc)
            continue
    return UserSummaryIdentity()


def user_identity_system_append(repo_root: Path | None = None) -> str:
    """Compact block prepended to strict JSON / system prompts."""
    if not user_summary_identity_enabled():
        return ""
    ident = load_user_summary_identity(repo_root)
    lines = [
        "## Operator context (user_summary.json)",
        "",
        ident.baseline_one_liner(),
        "",
        "- **Tone:** " + ident.tone_instruction(),
        "- **Technical depth:** " + ident.depth_instruction(),
    ]
    si = ident.stack_instruction()
    if si:
        lines.append("- **Stack assumptions:** " + si)
    if ident.voice_notes.strip():
        lines.extend(["", "_Additional voice notes:_", ident.voice_notes.strip()])
    lines.append("")
    return "\n".join(lines)


def format_user_identity_block_for_review(repo_root: Path | None = None) -> str:
    """Narrative block for long-form grounded review prompts."""
    if not user_summary_identity_enabled():
        return ""
    ident = load_user_summary_identity(repo_root)
    parts = [
        "### Operator identity (user_summary.json)",
        "",
        ident.baseline_one_liner(),
        "",
        "Adjust **tone** and **technical depth** accordingly:",
        "",
        f"- {ident.tone_instruction()}",
        f"- {ident.depth_instruction()}",
    ]
    si = ident.stack_instruction()
    if si:
        parts.append(f"- {si}")
    if ident.voice_notes.strip():
        parts.extend(["", ident.voice_notes.strip()])
    parts.append("")
    return "\n".join(parts)
