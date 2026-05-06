"""Assemble LLM prompts with PR description, before/after file snapshots, and strict JSON findings."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from github_bot.git_manager import (
    GroundedFile,
    GroundedPullRequest,
    fetch_file_at_ref,
)

try:
    from observability.memory_vector_store import VectorMemory, vector_memory_enabled
except ImportError:
    VectorMemory = None  # type: ignore[misc, assignment]

    def vector_memory_enabled() -> bool:  # type: ignore[misc]
        return False

try:
    from github_bot.correction_ledger import correction_ledger_enabled, lessons_learned_markdown
except ImportError:
    correction_ledger_enabled = lambda: False  # type: ignore[misc, assignment]

    def lessons_learned_markdown(*_a: Any, **_k: Any) -> str:  # type: ignore[misc]
        return ""


# --- Strict structured output (system prompt + validation helpers) ---

FINDINGS_JSON_SCHEMA_HINT = """{
  "findings": [
    {
      "file": "<string — repository-relative path>",
      "line_start": <integer >= 1>,
      "line_end": <integer >= line_start>,
      "issue_type": "<string — e.g. security, correctness, regression, performance, style, maintainability>",
      "severity": "<one of: critical, high, medium, low, info>",
      "evidence_quote": "<verbatim excerpt from the Before/After sections; must appear exactly>"
    }
  ]
}"""

STRICT_FINDINGS_SYSTEM_PROMPT = f"""You operate as **Octo-spork**'s structured-review agent for this repository. You MUST respond with exactly one JSON object and nothing else before or after it (no markdown fences, no commentary).

The JSON MUST match this shape (keys are mandatory for every finding):
{FINDINGS_JSON_SCHEMA_HINT}

Rules:
- Output valid UTF-8 JSON only.
- `findings` is an array (use an empty array if there are no issues).
- `line_start` and `line_end` refer to lines in the **After** state when the file still exists; if the issue applies only to deleted lines, use line numbers from the **Before** state and state that clearly in `evidence_quote`.
- `evidence_quote` must be copied verbatim from the provided Before/After file contents (or from the unified diff if the file could not be loaded).
- Do not invent paths or line numbers; if uncertain, omit the finding or widen `evidence_quote` and explain inside it.
- `severity` must be one of: critical, high, medium, low, info.
"""


REQUIRED_FINDING_KEYS = frozenset({"file", "line_start", "line_end", "issue_type", "severity", "evidence_quote"})


def validate_findings_json(text: str) -> tuple[bool, str]:
    """Return ``(ok, error_message)`` for a model response that should be JSON findings only."""
    text = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON: {exc}"
    if not isinstance(obj, dict):
        return False, "root must be a JSON object"
    findings = obj.get("findings")
    if findings is None:
        return False, "missing 'findings' array"
    if not isinstance(findings, list):
        return False, "'findings' must be an array"
    for i, item in enumerate(findings):
        if not isinstance(item, dict):
            return False, f"findings[{i}] must be an object"
        missing = REQUIRED_FINDING_KEYS - item.keys()
        if missing:
            return False, f"findings[{i}] missing keys: {sorted(missing)}"
        for k in ("line_start", "line_end"):
            if not isinstance(item.get(k), int):
                return False, f"findings[{i}].{k} must be an integer"
        if item["line_end"] < item["line_start"]:
            return False, f"findings[{i}]: line_end < line_start"
        for k in ("file", "issue_type", "severity", "evidence_quote"):
            if not isinstance(item.get(k), str):
                return False, f"findings[{i}].{k} must be a string"
    return True, ""


@dataclass
class FileSnapshot:
    """One PR file with optional Before (base) and After (head) full text."""

    path: str
    status: str
    before: str | None
    after: str | None
    is_binary_before: bool = False
    is_binary_after: bool = False
    notes: list[str] = field(default_factory=list)


class PromptGenerator:
    """Build system + user messages for grounded PR review with structured JSON output."""

    def __init__(
        self,
        *,
        pr_title: str,
        pr_body: str,
        files: list[FileSnapshot],
        unified_diff: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        number: int | None = None,
        base_sha: str | None = None,
        head_sha: str | None = None,
        ollama_base_url: str | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.pr_title = pr_title.strip() or "(no title)"
        self.pr_body = (pr_body or "").strip() or "(no description)"
        self.files = files
        self.unified_diff = unified_diff
        self.owner = owner
        self.repo = repo
        self.number = number
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.ollama_base_url = (ollama_base_url or "").strip() or None
        self.repo_root = repo_root

    @classmethod
    def from_grounded_pull_request(
        cls,
        ctx: GroundedPullRequest,
        *,
        pr_title: str,
        pr_body: str,
        token: str,
        api_base: str | None = None,
        include_unified_diff: bool = True,
        max_chars_per_file: int | None = 400_000,
        ollama_base_url: str | None = None,
        repo_root: Path | None = None,
    ) -> PromptGenerator:
        """Build snapshots by fetching **base** (`before`) and using ctx **head** (`after`) content."""
        base = api_base or os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

        snapshots: list[FileSnapshot] = []
        for gf in ctx.files:
            snap = _file_snapshot_from_grounded(gf, ctx, token, api_base=base, max_chars=max_chars_per_file)
            snapshots.append(snap)

        return cls(
            pr_title=pr_title,
            pr_body=pr_body,
            files=snapshots,
            unified_diff=ctx.unified_diff if include_unified_diff else None,
            owner=ctx.owner,
            repo=ctx.repo,
            number=ctx.number,
            base_sha=ctx.base_sha,
            head_sha=ctx.head_sha,
            ollama_base_url=ollama_base_url,
            repo_root=repo_root,
        )

    def system_prompt(self) -> str:
        """Strict system instructions (JSON-only findings)."""
        identity = ""
        try:
            from github_bot.user_summary_identity import user_identity_system_append

            identity = user_identity_system_append(self.repo_root)
        except ImportError:
            pass
        kb = ""
        try:
            from observability.knowledge_base import domain_constraints_system_append

            kb = domain_constraints_system_append()
        except ImportError:
            pass
        ks = ""
        try:
            from github_bot.knowledge_sync import knowledge_sync_system_append

            ks = knowledge_sync_system_append(self.repo_root)
        except ImportError:
            pass
        try:
            from github_bot.style_prefs import style_guide_system_prompt_suffix

            extra = style_guide_system_prompt_suffix()
        except ImportError:
            extra = ""
        return identity + STRICT_FINDINGS_SYSTEM_PROMPT + kb + ks + extra

    def _similarity_query_text(self) -> str:
        """Text used to retrieve historically similar review chunks from VectorMemory."""
        chunks: list[str] = [self.pr_title, self.pr_body]
        if self.owner and self.repo:
            chunks.append(f"Repository context: {self.owner}/{self.repo}")
        return "\n\n".join(c for c in chunks if c)

    def _historical_context_block(self) -> str:
        """Top similar past issues from VectorMemory; empty if disabled or unavailable."""
        if VectorMemory is None or not vector_memory_enabled():
            return ""
        base = self.ollama_base_url
        if not base:
            base = (
                (os.environ.get("OLLAMA_LOCAL_URL") or os.environ.get("OLLAMA_BASE_URL") or "")
                .strip()
                .rstrip("/")
            )
        if not base:
            return ""
        try:
            store = VectorMemory(ollama_base_url=base)
            hits = store.similar_findings(self._similarity_query_text(), k=3)
        except Exception:
            return ""
        if not hits:
            return ""
        lines: list[str] = [
            "## Historical Context",
            "",
        ]
        for h in hits[:3]:
            repo_full = str(h.get("repo_full") or "").strip() or "unknown/unknown"
            lines.append(
                f"In the past, you found similar issues in **{repo_full}**. "
                "Ensure this review checks for those patterns again."
            )
            excerpt = str(h.get("excerpt") or "").strip().replace("\r\n", "\n")
            if excerpt:
                if len(excerpt) > 600:
                    excerpt = excerpt[:600] + "…"
                lines.append(f"_Pattern hint (from prior review):_ {excerpt}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n\n"

    def _lessons_learned_block(self) -> str:
        """Correction Ledger: developer edits to AI comments (negative examples)."""
        if not correction_ledger_enabled():
            return ""
        base = self.ollama_base_url
        if not base:
            base = (
                (os.environ.get("OLLAMA_LOCAL_URL") or os.environ.get("OLLAMA_BASE_URL") or "")
                .strip()
                .rstrip("/")
            )
        if not base:
            return ""
        try:
            return lessons_learned_markdown(self._similarity_query_text(), base)
        except Exception:
            return ""

    def user_prompt(self) -> str:
        """Human-readable PR context: description + diff + per-file Before/After."""
        meta_lines = []
        if self.owner and self.repo and self.number is not None:
            meta_lines.append(f"Repository: `{self.owner}/{self.repo}` — PR #{self.number}")
        if self.base_sha:
            meta_lines.append(f"Base ref SHA: `{self.base_sha}`")
        if self.head_sha:
            meta_lines.append(f"Head ref SHA: `{self.head_sha}`")

        hist = self._historical_context_block()
        lessons = self._lessons_learned_block()

        parts: list[str] = [
            "\n".join(meta_lines) if meta_lines else "",
            "## PR title",
            self.pr_title,
            "",
            "## PR description",
            self.pr_body,
            "",
        ]
        if hist:
            parts.append(hist)
        if lessons:
            parts.append(lessons)
        if self.unified_diff:
            parts.extend(
                [
                    "## Unified diff (entire PR)",
                    "```diff",
                    self.unified_diff.strip(),
                    "```",
                    "",
                ]
            )

        parts.append("## Modified files — Before (base) vs After (head)")
        parts.append(
            "Each section lists the **Before** snapshot at the merge base and the **After** snapshot "
            "at the PR head. Use these for grounded citations."
        )
        parts.append("")

        for snap in self.files:
            parts.append(f"### `{snap.path}` — status: `{snap.status}`")
            if snap.notes:
                for n in snap.notes:
                    parts.append(f"_Note: {n}_")
            before_block = _format_snapshot_body(snap.before, snap.is_binary_before)
            after_block = _format_snapshot_body(snap.after, snap.is_binary_after)
            parts.append("**Before (base)**")
            parts.append("```text")
            parts.append(before_block)
            parts.append("```")
            parts.append("")
            parts.append("**After (head)**")
            parts.append("```text")
            parts.append(after_block)
            parts.append("```")
            parts.append("")

        parts.append(
            "Respond with the JSON object only, following the system schema. "
            "Ground every finding in the Before/After text or unified diff."
        )
        return "\n".join(parts).strip() + "\n"

    def messages(self) -> list[dict[str, str]]:
        """OpenAI/Ollama-style chat messages: system + user."""
        return [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": self.user_prompt()},
        ]


def _truncate(text: str | None, max_chars: int | None) -> str | None:
    if text is None:
        return None
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n… [truncated]"


def _format_snapshot_body(text: str | None, is_binary: bool) -> str:
    if is_binary:
        return "[binary file — contents omitted]"
    if text is None:
        return "(not applicable — e.g. new file has no Before, or deleted file has no After)"
    if not text.strip():
        return "(empty file)"
    return text.rstrip()


def _file_snapshot_from_grounded(
    gf: GroundedFile,
    ctx: GroundedPullRequest,
    token: str,
    *,
    api_base: str,
    max_chars: int | None,
) -> FileSnapshot:
    """Resolve Before (base ref) and After (head ref) text for one `GroundedFile`."""
    notes: list[str] = []
    owner, repo = ctx.owner, ctx.repo
    base_sha, head_sha = ctx.base_sha, ctx.head_sha

    before: str | None = None
    after: str | None = None
    bin_b = False
    bin_a = False

    st = (gf.status or "modified").lower()

    if st == "added":
        before = None
        after = gf.full_text
        bin_a = gf.is_binary
        if gf.fetch_error:
            notes.append(f"After load: {gf.fetch_error}")
    elif st == "removed":
        bt, bin_b, eb = fetch_file_at_ref(owner, repo, gf.path, base_sha, token, api_base=api_base)
        before = bt
        after = None
        if eb:
            notes.append(f"Before load: {eb}")
        if gf.patch_hunk and not (before or "").strip():
            notes.append("Per-file patch available in unified diff section.")
    elif st == "renamed":
        old_path = gf.previous_path or ""
        if old_path:
            bt, bin_b, eb = fetch_file_at_ref(owner, repo, old_path, base_sha, token, api_base=api_base)
            before = bt
            if eb:
                notes.append(f"Before (old path) load: {eb}")
        else:
            notes.append("Rename without previous_filename; Before unavailable.")
        after = gf.full_text
        bin_a = gf.is_binary
        if gf.fetch_error:
            notes.append(f"After load: {gf.fetch_error}")
    else:
        # modified, copied, changed, etc.
        bt, bin_b, eb = fetch_file_at_ref(owner, repo, gf.path, base_sha, token, api_base=api_base)
        before = bt
        if eb:
            notes.append(f"Before load: {eb}")
        after = gf.full_text
        bin_a = gf.is_binary
        if gf.fetch_error:
            notes.append(f"After load: {gf.fetch_error}")

    before = _truncate(before, max_chars)
    after = _truncate(after, max_chars)

    return FileSnapshot(
        path=gf.path,
        status=gf.status,
        before=before,
        after=after,
        is_binary_before=bin_b,
        is_binary_after=bin_a,
        notes=notes,
    )


def parse_pull_request_description(payload: dict[str, Any]) -> tuple[str, str]:
    """Extract PR title and body from a ``pull_request`` webhook payload."""
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        raise ValueError("payload.pull_request missing")
    title = pr.get("title")
    body = pr.get("body")
    t = str(title).strip() if isinstance(title, str) else "(no title)"
    b = str(body).strip() if isinstance(body, str) else ""
    return t, b
