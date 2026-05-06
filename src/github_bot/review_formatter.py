"""Format structured AI JSON (findings) into one grouped GitHub Review markdown body."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

_SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")

_SEVERITY_EMOJI: dict[str, str] = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
    "info": "ℹ️",
}

_DEFAULT_UNKNOWN_EMOJI = "⚪"

_INVALID_FILE_MARKERS = frozenset(
    {
        "",
        "(unknown path)",
        "unknown path",
        "unknown",
        "n/a",
        "none",
        "null",
    }
)


def finding_has_valid_grounding(item: dict[str, Any]) -> bool:
    """Return True only if the finding cites a real path and evidence (high signal-to-noise)."""
    if not isinstance(item, dict):
        return False
    raw = str(item.get("file") or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if low in _INVALID_FILE_MARKERS:
        return False
    if "unknown" in low and raw.startswith("("):
        return False
    eq = str(item.get("evidence_quote") or "").strip()
    if not eq:
        return False
    return True


def filter_grounded_findings(findings: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """Drop findings without a usable ``file`` + ``evidence_quote``. Returns ``(kept, dropped_count)``."""
    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in findings:
        if isinstance(item, dict) and finding_has_valid_grounding(item):
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


def _normalize_severity(raw: str) -> str:
    s = str(raw or "").strip().lower()
    return s if s in _SEVERITY_EMOJI else "info"


def _severity_rank(sev: str) -> int:
    try:
        return _SEVERITY_ORDER.index(sev)
    except ValueError:
        return len(_SEVERITY_ORDER)


def _emoji_for(sev: str) -> str:
    return _SEVERITY_EMOJI.get(sev, _DEFAULT_UNKNOWN_EMOJI)


def _escape_details_body(text: str) -> str:
    """Avoid accidental closure of ``<details>`` blocks."""
    return text.replace("</details>", r"&lt;/details&gt;")


def _wrap_collapsible(summary: str, body: str) -> str:
    safe = _escape_details_body(body.strip())
    if not safe:
        return ""
    return (
        f"<details>\n<summary>{summary}</summary>\n\n"
        f"```text\n{safe}\n```\n\n"
        f"</details>\n"
    )


def _blockquote(text: str) -> str:
    return "\n".join(f"> {ln}" for ln in text.splitlines()) + "\n"


def _format_evidence_quote(quote: str, *, threshold: int) -> str:
    """Blockquote short evidence; long quotes spill into ``<details>``."""
    q = quote.strip()
    if len(q) <= threshold:
        return _blockquote(q)
    head = q[:threshold].rstrip()
    tail = q[threshold:].strip()
    collapsed = _wrap_collapsible("Full evidence excerpt", tail)
    return (
        f"{_blockquote(head)}\n"
        f"_(Truncated — expand below for the full excerpt.)_\n\n"
        f"{collapsed}"
    )


def _parse_ai_payload(ai_json: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(ai_json, str):
        return json.loads(ai_json.strip())
    return ai_json


@dataclass(frozen=True)
class FormattedGitHubReview:
    """Single markdown document: post once as a PR review body or issue comment."""

    markdown: str


class ReviewFormatter:
    """Turn AI JSON (``findings`` array + optional summary/logs) into grouped review markdown."""

    def __init__(
        self,
        *,
        evidence_detail_threshold: int = 360,
        auxiliary_log_detail_threshold: int = 800,
    ) -> None:
        self._evidence_thr = int(evidence_detail_threshold)
        self._log_thr = int(auxiliary_log_detail_threshold)

    def format(self, ai_json: dict[str, Any] | str) -> FormattedGitHubReview:
        """Build structured GitHub-flavored markdown: Summary, emoji severity, findings grouped by file."""
        root = _parse_ai_payload(ai_json)
        findings_raw = root.get("findings")
        raw_list: list[Any] = findings_raw if isinstance(findings_raw, list) else []

        findings, dropped_n = filter_grounded_findings(raw_list)

        by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in findings:
            path = str(item.get("file") or "").strip()
            by_file[path].append(item)

        grounded_receipts = self._build_grounded_receipts_section(by_file, dropped_n=dropped_n)
        summary_section = self._build_summary_section(root, by_file, dropped_n=dropped_n)
        logs_section = self._build_auxiliary_logs_section(root)
        findings_section = self._build_findings_by_file(by_file)

        parts = [
            p
            for p in (summary_section, grounded_receipts, logs_section, findings_section)
            if p.strip()
        ]
        body = "\n\n---\n\n".join(parts)
        footer = (
            "\n\n---\n\n"
            "<sub>Findings are grouped by file in this single review to reduce timeline noise. "
            "Suggestions without a valid `file` + `evidence_quote` in the JSON are omitted.</sub>\n"
        )
        return FormattedGitHubReview(markdown=body + footer)

    def _build_grounded_receipts_section(
        self,
        by_file: dict[str, list[dict[str, Any]]],
        *,
        dropped_n: int,
    ) -> str:
        lines = [
            "## Grounded Receipts",
            "",
            "_Each retained suggestion below is tied to repository evidence. Every finding includes "
            "**Analyzed from:** with the path grounded in the model JSON._",
            "",
        ]
        paths = sorted(by_file.keys())
        if not paths:
            lines.append("_No grounded file-scoped findings passed validation._")
            if dropped_n > 0:
                lines.append(f"_({dropped_n} raw entr{'ies' if dropped_n != 1 else 'y'} omitted — missing file reference or evidence.)_")
            lines.append("")
            return "\n".join(lines)

        for p in paths:
            lines.append(f"- **Analyzed from:** `{p}`")
        lines.append("")
        if dropped_n > 0:
            lines.append(
                f"_{dropped_n} suggestion(s) were **filtered out** (no valid `file` and/or "
                f"`evidence_quote` in JSON)._"
            )
            lines.append("")
        return "\n".join(lines).strip()

    def _build_summary_section(
        self,
        root: dict[str, Any],
        by_file: dict[str, list[dict[str, Any]]],
        *,
        dropped_n: int = 0,
    ) -> str:
        explicit = root.get("review_summary") or root.get("summary")
        counts: dict[str, int] = defaultdict(int)
        for items in by_file.values():
            for it in items:
                if not isinstance(it, dict):
                    continue
                sev = _normalize_severity(str(it.get("severity") or ""))
                counts[sev] += 1

        lines = ["## Summary", ""]
        if isinstance(explicit, str) and explicit.strip():
            lines.append(explicit.strip())
            lines.append("")
        else:
            total = sum(len(v) for v in by_file.values())
            if total == 0:
                if dropped_n > 0:
                    lines.append(
                        f"_No grounded findings remain after validation. "
                        f"**{dropped_n}** raw entr{'ies' if dropped_n != 1 else 'y'} "
                        f"had no valid `file` and/or `evidence_quote`._"
                    )
                else:
                    lines.append("_Automated review produced **no structured findings** for this PR._")
            else:
                bits = [f"**{total}** grounded finding(s) across **{len(by_file)}** file(s)."]
                sev_bits = [
                    f"{_emoji_for(s)} **{s}**: {counts[s]}"
                    for s in _SEVERITY_ORDER
                    if counts.get(s, 0) > 0
                ]
                if sev_bits:
                    bits.append(" · ".join(sev_bits))
                lines.append(" ".join(bits))
                if dropped_n > 0:
                    lines.append(
                        f"_({dropped_n} raw entr{'ies' if dropped_n != 1 else 'y'} omitted: "
                        "missing `file` path and/or `evidence_quote`.)_"
                    )
            lines.append("")

        raw_meta = root.get("metadata") or root.get("meta")
        if isinstance(raw_meta, (dict, list)) and raw_meta:
            meta_text = json.dumps(raw_meta, indent=2, ensure_ascii=False)
            if len(meta_text) > self._log_thr:
                lines.append(_wrap_collapsible("Structured metadata (JSON)", meta_text))
            else:
                lines.append("```json")
                lines.append(meta_text)
                lines.append("```")
                lines.append("")
        return "\n".join(lines).strip()

    def _build_auxiliary_logs_section(self, root: dict[str, Any]) -> str:
        keys = ("raw_log", "scanner_log", "build_log", "auxiliary_logs", "debug_log")
        chunks: list[str] = []
        for k in keys:
            val = root.get(k)
            if isinstance(val, str) and val.strip():
                title = k.replace("_", " ").title()
                text = val.strip()
                if len(text) > self._log_thr:
                    chunks.append(_wrap_collapsible(f"{title} (long)", text))
                else:
                    chunks.append(f"### {title}\n\n```text\n{_escape_details_body(text)}\n```\n")
        if not chunks:
            return ""
        return "## Logs & diagnostics\n\n" + "\n".join(chunks)

    def _build_findings_by_file(self, by_file: dict[str, list[dict[str, Any]]]) -> str:
        if not by_file:
            return ""

        lines: list[str] = ["## Findings by file", ""]
        for path in sorted(by_file.keys()):
            items = by_file[path]
            sorted_items = sorted(
                items,
                key=lambda it: (
                    _severity_rank(_normalize_severity(str(it.get("severity") or ""))),
                    int(it.get("line_start") or 0) if isinstance(it.get("line_start"), int) else 0,
                ),
            )
            lines.append(f"### 📄 `{path}`")
            lines.append("")
            for it in sorted_items:
                if not isinstance(it, dict):
                    continue
                sev = _normalize_severity(str(it.get("severity") or ""))
                em = _emoji_for(sev)
                issue = str(it.get("issue_type") or "finding").strip()
                ls = it.get("line_start")
                le = it.get("line_end")
                line_bits = ""
                if isinstance(ls, int) and isinstance(le, int):
                    line_bits = f"L{ls}" if ls == le else f"L{ls}–L{le}"
                elif isinstance(ls, int):
                    line_bits = f"L{ls}"
                heading = f"#### {em} **{sev.title()}** · `{issue}`"
                if line_bits:
                    heading += f" · _{line_bits}_"
                lines.append(heading)
                lines.append("")
                eq = str(it.get("evidence_quote") or "").strip()
                if eq:
                    lines.append(_format_evidence_quote(eq, threshold=self._evidence_thr))
                lines.append("")
                lines.append(f"_**Analyzed from:** `{path}`_")
                lines.append("")
            lines.append("")

        return "\n".join(lines).strip()


def format_github_review_from_ai_json(
    ai_json: dict[str, Any] | str,
    *,
    evidence_detail_threshold: int = 360,
    auxiliary_log_detail_threshold: int = 800,
) -> str:
    """Functional wrapper: same output as :class:`ReviewFormatter`."""
    return ReviewFormatter(
        evidence_detail_threshold=evidence_detail_threshold,
        auxiliary_log_detail_threshold=auxiliary_log_detail_threshold,
    ).format(ai_json).markdown
