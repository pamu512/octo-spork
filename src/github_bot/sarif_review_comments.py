"""Map SARIF (Trivy/CodeQL) locations to ``POST .../pulls/{n}/comments`` JSON bodies.

GitHub review comments anchor to a **blob line** in the PR diff view:

- ``side=RIGHT`` + ``commit_id`` = PR head: ``line`` is a 1-based line number in the **head** version
  of ``path`` (additions + unchanged/context lines as shown in green or white).
- ``side=LEFT`` + ``commit_id`` must match the **parent** side when commenting only on deletions (red):
  ``line`` is a 1-based line number in the **base** version of ``path``.

Scanners run on the checked-out PR head tree, so findings almost always target **RIGHT**. When a
unified diff is supplied, this module classifies each head line as *added* vs *context* and picks
``LEFT`` only when a finding clearly refers to **base-only** deleted lines (rare for head scans).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

Side = Literal["LEFT", "RIGHT"]


@dataclass(frozen=True)
class PullRequestReviewCommentInput:
    """JSON body for ``POST /repos/{{owner}}/{{repo}}/pulls/{{pull_number}}/comments``."""

    body: str
    commit_id: str
    path: str
    line: int
    side: Side
    start_line: int | None = None
    start_side: Side | None = None

    def as_api_dict(self) -> dict[str, Any]:
        """Serialize for GitHub REST (omit null multi-line keys on single-line comments)."""
        out: dict[str, Any] = {
            "body": self.body,
            "commit_id": self.commit_id,
            "path": self.path,
            "line": self.line,
            "side": self.side,
        }
        if self.start_line is not None:
            out["start_line"] = self.start_line
        if self.start_side is not None:
            out["start_side"] = self.start_side
        return out


@dataclass(frozen=True)
class _HeadLineClass:
    """How a 1-based head line appears in the unified diff for ``path``."""

    kind: Literal["added", "context"]


@dataclass(frozen=True)
class _BaseDeletion:
    """A deletion-only row (``-``) maps to this base line number (LEFT comment target)."""

    base_line: int


_HUNK_HDR = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@",
)


def _uri_to_repo_relative(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path or "")
        return path.lstrip("/")
    return uri.replace("\\", "/")


def _normalize_diff_path(raw: str) -> str:
    s = raw.strip()
    for prefix in ("a/", "b/", "./"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    return s.replace("\\", "/")


def _split_unified_diff(diff_text: str) -> dict[str, str]:
    """Split full unified diff into per-file hunks (keys = normalized ``path`` as in ``+++ b/``)."""
    raw = diff_text or ""
    if not raw.strip():
        return {}
    parts = re.split(r"(?=^diff --git )", raw.lstrip("\n"), flags=re.MULTILINE)
    chunks: dict[str, list[str]] = {}
    for part in parts:
        if not part.strip():
            continue
        lines = part.splitlines()
        if not lines:
            continue
        head = lines[0]
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", head)
        path_key: str | None = None
        if m:
            path_key = _normalize_diff_path(m.group(2))
        else:
            # Fallback: parse ``+++ b/path``
            for ln in lines[:12]:
                if ln.startswith("+++ b/"):
                    path_key = _normalize_diff_path(ln[6:])
                    break
        if path_key is None:
            continue
        chunks.setdefault(path_key, []).extend(lines)
    return {p: "\n".join(lines) for p, lines in chunks.items() if lines}


def _parse_file_patch(file_patch: str) -> tuple[dict[int, _HeadLineClass], dict[int, _BaseDeletion]]:
    """Return head-line classification and base-only deletion markers for one file's patch."""
    head_map: dict[int, _HeadLineClass] = {}
    base_deletions: dict[int, _BaseDeletion] = {}

    lines = file_patch.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("@@"):
            m = _HUNK_HDR.match(line)
            if not m:
                i += 1
                continue
            old_line = int(m.group(1))
            new_line = int(m.group(3))
            i += 1
            while i < len(lines) and not lines[i].startswith("@@"):
                row = lines[i]
                if not row:
                    i += 1
                    continue
                if row.startswith("\\"):
                    # e.g. ``\ No newline at end of file``
                    i += 1
                    continue
                prefix = row[0]
                if prefix not in "-+ ":
                    i += 1
                    continue
                if prefix == " ":
                    head_map[new_line] = _HeadLineClass("context")
                    old_line += 1
                    new_line += 1
                elif prefix == "+":
                    head_map[new_line] = _HeadLineClass("added")
                    new_line += 1
                else:  # deletion (base-only — does not advance head line)
                    base_deletions[old_line] = _BaseDeletion(old_line)
                    old_line += 1
                i += 1
            continue
        i += 1

    return head_map, base_deletions


def build_pr_diff_index(diff_text: str) -> dict[str, tuple[dict[int, _HeadLineClass], dict[int, _BaseDeletion]]]:
    """Parse a unified PR diff into per-file head/base maps."""
    out: dict[str, tuple[dict[int, _HeadLineClass], dict[int, _BaseDeletion]]] = {}
    for path, patch in _split_unified_diff(diff_text).items():
        out[path] = _parse_file_patch(patch)
    return out


def _resolve_path_aliases(sarif_path: str, index_keys: set[str]) -> str | None:
    """Match SARIF path to a key returned by :func:`build_pr_diff_index`."""
    sp = sarif_path.replace("\\", "/").strip()
    if sp in index_keys:
        return sp
    for k in index_keys:
        if sp.endswith(k) or k.endswith(sp):
            return k
        if sp.split("/")[-1] == k.split("/")[-1] and sp.split("/")[-1]:
            return k
    return None


def _choose_side_and_lines(
    *,
    start_line: int,
    end_line: int,
    head_commit: str,
) -> tuple[Side, str, int, int | None, Side | None]:
    """Pick GitHub ``side``, ``commit_id``, and ``line`` / ``start_line`` for the REST API.

    Trivy/CodeQL SARIF locations describe artifacts on the **PR head** tree; coordinates are
    **head blob line numbers** → ``side=RIGHT`` and ``commit_id`` = PR head SHA.

    GitHub expects ``line`` as the **last** line of a multi-line span; ``start_line`` / ``start_side``
    define the start when ``end_line > start_line``.
    """
    if end_line > start_line:
        return "RIGHT", head_commit, end_line, start_line, "RIGHT"
    return "RIGHT", head_commit, end_line, None, None


def diff_note_for_finding(
    path: str,
    line: int,
    diff_index: dict[str, tuple[dict[int, _HeadLineClass], dict[int, _BaseDeletion]]] | None,
) -> str:
    """Optional footnote: whether the head line appeared as an addition or context in the PR diff."""
    if not diff_index:
        return ""
    key = _resolve_path_aliases(path.replace("\\", "/").lstrip("./"), set(diff_index.keys()))
    if key is None:
        return ""
    hm, _ = diff_index[key]
    meta = hm.get(line)
    if meta is None:
        return "_Diff context: line not inside a shown hunk (unchanged region)._"
    if meta.kind == "added":
        return "_Diff context: line is part of an **addition** in this PR._"
    return "_Diff context: line is **context** (unchanged vs base in the shown hunk)._"


def sarif_to_pull_request_review_comments(
    sarif_path: Path | str,
    *,
    commit_id: str,
    unified_pr_diff: str | None = None,
    tool_label: str = "scanner",
    max_comments: int = 60,
) -> list[PullRequestReviewCommentInput]:
    """Convert SARIF results into GitHub pull-request review comment payloads.

    Parameters
    ----------
    sarif_path:
        Path to ``results.sarif`` (Trivy or CodeQL ``sarifv2.1.0``).
    commit_id:
        SHA of the PR **head** commit that matches the scanned tree (required).
    unified_pr_diff:
        Full unified diff for the PR (e.g. ``Accept: application/vnd.github.diff``). When omitted,
        every finding uses ``side=RIGHT`` and blob line numbers from SARIF as produced on HEAD.
    """
    path = Path(sarif_path)
    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))

    diff_index = build_pr_diff_index(unified_pr_diff) if unified_pr_diff else None

    rules_map: dict[str, dict[str, Any]] = {}
    out: list[PullRequestReviewCommentInput] = []

    for run in payload.get("runs") or []:
        if not isinstance(run, dict):
            continue
        driver = ((run.get("tool") or {}).get("driver")) or {}
        for rule in driver.get("rules") or []:
            if isinstance(rule, dict) and rule.get("id"):
                rules_map[str(rule["id"])] = rule

        for result in run.get("results") or []:
            if not isinstance(result, dict):
                continue
            if len(out) >= max_comments:
                return out

            rule_id = str(result.get("ruleId") or "")
            rule_meta = rules_map.get(rule_id) or {}
            msg_obj = result.get("message")
            if isinstance(msg_obj, dict):
                message = str(msg_obj.get("text") or rule_id or "")
            else:
                message = str(msg_obj or "")
            message = message.strip() or "(no message)"

            locations = result.get("locations") or []
            if not isinstance(locations, list) or not locations:
                continue

            loc0 = locations[0] if isinstance(locations[0], dict) else {}
            phys = loc0.get("physicalLocation") if isinstance(loc0.get("physicalLocation"), dict) else {}
            region = phys.get("region") if isinstance(phys.get("region"), dict) else {}
            al = phys.get("artifactLocation") if isinstance(phys.get("artifactLocation"), dict) else {}
            uri = str(al.get("uri") or "")
            rel_path = _uri_to_repo_relative(uri) or uri
            rel_path = rel_path.replace("\\", "/").lstrip("./")

            try:
                start_line = int(region.get("startLine") or 1)
            except (TypeError, ValueError):
                start_line = 1
            end_line_val = region.get("endLine")
            try:
                end_line = int(end_line_val) if end_line_val is not None else start_line
            except (TypeError, ValueError):
                end_line = start_line
            if end_line < start_line:
                end_line = start_line

            side, anchor_commit, line, multi_start, multi_side = _choose_side_and_lines(
                start_line=start_line,
                end_line=end_line,
                head_commit=commit_id,
            )

            title_bits = [f"**[{tool_label}]** `{rule_id}`"] if rule_id else [f"**[{tool_label}]**"]
            short_desc = rule_meta.get("shortDescription")
            short_rule = (
                str(short_desc.get("text") or "").strip() if isinstance(short_desc, dict) else ""
            )
            if short_rule and len(short_rule) < 120:
                title_bits.append(f" — {short_rule}")
            body = "".join(title_bits) + "\n\n" + message
            note = diff_note_for_finding(rel_path, end_line, diff_index)
            if note:
                body = body + "\n\n" + note
            if len(body) > 65_000:
                body = body[:64_500] + "\n\n_(truncated)_"

            out.append(
                PullRequestReviewCommentInput(
                    body=body,
                    commit_id=anchor_commit,
                    path=rel_path,
                    line=line,
                    side=side,
                    start_line=multi_start,
                    start_side=multi_side,
                )
            )

    return out
