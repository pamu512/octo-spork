"""Load repository-local coding rules from ``CLAUDE.md`` (KnowledgeSync).

Parses markdown structure (front matter, headings, lists, blockquotes, fenced blocks)
and exposes compact text for system prompts, narrative review blocks, and optional
developer-facing proposals when recurring patterns appear elsewhere in the pipeline.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
_LOG = logging.getLogger(__name__)

CLAUDE_MD_FILENAME = "CLAUDE.md"

_RECURRING_HEADING = "### Recurring architectural debt"

# Sections whose title suggests coding rules / conventions.
_RULE_SECTION_TITLE = re.compile(
    r"(coding\s+rules?|rules?\s+of\s+thumb|conventions?|style\s+guide|standards?|"
    r"architecture|lint|must\s+not|should\s+not|don't|avoid|preferences|guidelines|"
    r"contributing|development|engineering|repo\s+policy|project\s+policy|CLAUDE)",
    re.IGNORECASE,
)

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET_LINE = re.compile(
    r"^\s*[-*+]\s+(?:\[(?: |x|X)\]\s*)?(.+)$",
)
_ORDERED_LINE = re.compile(r"^\s*\d+[.)]\s+(.+)$")
_BLOCKQUOTE_LINE = re.compile(r"^\s*>\s?(.*)$")

_FENCE_OPEN = re.compile(r"^(\s*)(`{3,}|~{3,})\s*.*$")


def knowledge_sync_enabled() -> bool:
    return os.environ.get("OCTO_KNOWLEDGE_SYNC_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _max_chars() -> int:
    raw = (os.environ.get("OCTO_KNOWLEDGE_SYNC_MAX_CHARS") or "").strip()
    if raw.isdigit():
        return max(2048, int(raw))
    return 24_000


def _resolve_repo_root(explicit: Path | None) -> Path | None:
    if explicit is not None:
        p = explicit.expanduser().resolve()
        return p if p.is_dir() else None
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
        return p if p.is_dir() else None
    return None


def claude_md_path(repo_root: Path | None) -> Path | None:
    root = _resolve_repo_root(repo_root)
    if root is None:
        return None
    p = root / CLAUDE_MD_FILENAME
    return p if p.is_file() else None


def strip_bom(text: str) -> str:
    if text.startswith("\ufeff"):
        return text[1:]
    return text


def strip_yaml_front_matter(text: str) -> str:
    """Remove leading YAML front matter delimited by ``---`` lines (first doc block only)."""
    t = strip_bom(text.lstrip())
    if not t.startswith("---"):
        return text
    lines = t.split("\n")
    if len(lines) < 2:
        return text
    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return text
    remainder = "\n".join(lines[end_idx + 1 :])
    return remainder if remainder.endswith("\n") or not remainder else remainder + "\n"


def strip_html_comments(text: str) -> str:
    """Remove ``<!-- ... -->`` comments (non-greedy, dot matches newline)."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _fence_regions(lines: list[str]) -> list[tuple[int, int]]:
    """Return half-open line index spans [start, end) that lie inside fenced code blocks."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _FENCE_OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        fence = m.group(2)
        ch = fence[0]
        opener_len = len(fence)
        j = i + 1
        while j < n:
            s = lines[j].lstrip()
            if len(s) >= opener_len and s[0] == ch and set(s[:opener_len]) == {ch}:
                spans.append((i, j + 1))
                i = j + 1
                break
            j += 1
        else:
            spans.append((i, n))
            break
    return spans


def _line_in_spans(idx: int, spans: list[tuple[int, int]]) -> bool:
    for a, b in spans:
        if a <= idx < b:
            return True
    return False


@dataclass
class MarkdownSection:
    title: str
    level: int
    start_line: int
    raw_body: str


@dataclass
class ParsedClaudeMd:
    """Structured parse result for ``CLAUDE.md``."""

    preamble: str
    sections: list[MarkdownSection] = field(default_factory=list)
    rule_sections: list[MarkdownSection] = field(default_factory=list)
    rules_flat: list[str] = field(default_factory=list)


def _heading_level(line: str) -> tuple[int, str] | None:
    m = _ATX_HEADING.match(line.rstrip())
    if not m:
        return None
    hashes, title = m.group(1), m.group(2).strip()
    return len(hashes), title


def split_markdown_sections(text: str) -> tuple[str, list[MarkdownSection]]:
    """Split document into preamble (before first ATX heading) and titled sections.

    Headings inside fenced code blocks are ignored so ``#`` comment lines do not break structure.
    """
    lines = text.split("\n")
    spans = _fence_regions(lines)

    def heading_at(idx: int) -> tuple[int, str] | None:
        if _line_in_spans(idx, spans):
            return None
        return _heading_level(lines[idx])

    first_idx: int | None = None
    for i, _line in enumerate(lines):
        if heading_at(i):
            first_idx = i
            break
    if first_idx is None:
        return text.rstrip(), []

    preamble = "\n".join(lines[:first_idx]).rstrip()
    sections: list[MarkdownSection] = []
    i = first_idx
    while i < len(lines):
        hl = heading_at(i)
        if not hl:
            i += 1
            continue
        level, title = hl
        start_line = i
        i += 1
        body_lines: list[str] = []
        while i < len(lines):
            if heading_at(i):
                break
            body_lines.append(lines[i])
            i += 1
        sections.append(
            MarkdownSection(
                title=title,
                level=level,
                start_line=start_line,
                raw_body="\n".join(body_lines).strip(),
            )
        )
    return preamble, sections


def _section_is_rule_like(sec: MarkdownSection) -> bool:
    if _RULE_SECTION_TITLE.search(sec.title):
        return True
    body_lower = sec.raw_body.lower()
    bulletish = sum(
        1
        for ln in sec.raw_body.split("\n")
        if _BULLET_LINE.match(ln) or _ORDERED_LINE.match(ln)
    )
    if bulletish >= 2:
        return True
    if "must " in body_lower or "should " in body_lower or "do not " in body_lower:
        return True
    if re.search(r"(?m)^\s*>\s*\S", sec.raw_body):
        return True
    return False


def _extract_rules_from_text(chunk: str) -> list[str]:
    """Pull bullet, numbered, and blockquote rules from a chunk; skips fenced regions."""
    lines = chunk.split("\n")
    spans = _fence_regions(lines)
    rules: list[str] = []
    bq_acc: list[str] = []

    def flush_bq() -> None:
        nonlocal bq_acc
        if bq_acc:
            rules.append(" ".join(x.strip() for x in bq_acc if x.strip()))
            bq_acc = []

    for idx, line in enumerate(lines):
        if _line_in_spans(idx, spans):
            flush_bq()
            continue
        bm = _BULLET_LINE.match(line)
        if bm:
            flush_bq()
            t = bm.group(1).strip()
            if t:
                rules.append(t)
            continue
        om = _ORDERED_LINE.match(line)
        if om:
            flush_bq()
            t = om.group(1).strip()
            if t:
                rules.append(t)
            continue
        qm = _BLOCKQUOTE_LINE.match(line)
        if qm:
            bq_acc.append(qm.group(1))
            continue
        flush_bq()

    flush_bq()
    return rules


def parse_claude_md(text: str) -> ParsedClaudeMd:
    """Parse ``CLAUDE.md`` content: front matter, HTML comments, sections, and coding rules."""
    cleaned = strip_html_comments(strip_yaml_front_matter(text))
    preamble, sections = split_markdown_sections(cleaned)

    rule_sections = [s for s in sections if _section_is_rule_like(s)]
    rules_flat: list[str] = []

    rules_flat.extend(_extract_rules_from_text(preamble))

    for sec in rule_sections:
        rules_flat.extend(_extract_rules_from_text(sec.raw_body))

    if not rule_sections and sections:
        for sec in sections:
            rules_flat.extend(_extract_rules_from_text(sec.raw_body))

    seen: set[str] = set()
    deduped: list[str] = []
    for r in rules_flat:
        key = re.sub(r"\s+", " ", r.strip().lower())
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        deduped.append(r.strip())

    return ParsedClaudeMd(
        preamble=preamble.strip(),
        sections=sections,
        rule_sections=rule_sections,
        rules_flat=deduped,
    )


def load_parsed_claude_md(repo_root: Path | None) -> ParsedClaudeMd | None:
    path = claude_md_path(repo_root)
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _LOG.warning("knowledge_sync: cannot read %s: %s", path, exc)
        return None
    return parse_claude_md(raw)


def load_claude_md_rules_markdown(repo_root: Path | None) -> str:
    """Concatenate extracted rules as markdown suitable for prompts."""
    if not knowledge_sync_enabled():
        return ""
    parsed = load_parsed_claude_md(repo_root)
    if parsed is None or not parsed.rules_flat:
        return ""
    cap = _max_chars()
    lines = ["### Repository coding rules (`CLAUDE.md`)", ""]
    for r in parsed.rules_flat:
        lines.append(f"- {r}")
    text = "\n".join(lines) + "\n"
    if len(text) > cap:
        text = (
            text[: cap - 120]
            + "\n\n_… [truncated; raise OCTO_KNOWLEDGE_SYNC_MAX_CHARS]_\n"
        )
    return text


def knowledge_sync_system_append(repo_root: Path | None = None) -> str:
    """Compact appendix for strict JSON system prompts."""
    if not knowledge_sync_enabled():
        return ""
    body = load_claude_md_rules_markdown(repo_root)
    if not body:
        return ""
    return (
        "\n\n**Repository KnowledgeSync (`CLAUDE.md`):** Apply these project-local rules when "
        "classifying severity and issue types. `evidence_quote` must still be verbatim from the PR.\n\n"
        f"{body}"
    )


def format_knowledge_sync_block_for_review(repo_root: Path | None = None) -> str:
    """Markdown section for long-form grounded / narrative review prompts."""
    return load_claude_md_rules_markdown(repo_root).strip()


def extract_recurring_smell_hints(scanner_markdown: str) -> list[str]:
    """Extract bullet summaries from the global smell index recurring-debt section."""
    text = scanner_markdown or ""
    if _RECURRING_HEADING not in text:
        return []
    idx = text.find(_RECURRING_HEADING)
    rest = text[idx + len(_RECURRING_HEADING) :]
    stop = re.search(r"\n###\s+", rest)
    block = rest[: stop.start()] if stop else rest
    hints: list[str] = []
    for line in block.split("\n"):
        m = _BULLET_LINE.match(line)
        if m:
            hints.append(m.group(1).strip())
    return hints[:24]


def propose_claude_md_update(
    repo_root: Path | None,
    *,
    recurring_hints: list[str],
    review_excerpt: str | None = None,
) -> str:
    """Developer-facing markdown suggesting edits to ``CLAUDE.md`` when patterns recur."""
    if not recurring_hints and not (review_excerpt or "").strip():
        return ""
    path = claude_md_path(repo_root)
    path_hint = f"`{path}`" if path else f"`{CLAUDE_MD_FILENAME}` at the repository root"
    lines = [
        "### KnowledgeSync — suggested `CLAUDE.md` update",
        "",
        f"_Recurring patterns were detected. Consider codifying them under a **Coding rules** or "
        f"**Conventions** heading in {path_hint} so future reviews inherit them automatically._",
        "",
    ]
    if recurring_hints:
        lines.append("Suggested bullets to add (edit for tone and accuracy):")
        lines.append("")
        for h in recurring_hints[:12]:
            short = h.replace("\n", " ").strip()
            if len(short) > 360:
                short = short[:357] + "…"
            lines.append(f"- [ ] Address recurring drift: {short}")
        lines.append("")
    if review_excerpt and review_excerpt.strip():
        ex = review_excerpt.strip()
        if len(ex) > 800:
            ex = ex[:797] + "…"
        lines.append("Context from the latest review (for wording):")
        lines.append("")
        lines.append(f"> {ex.replace(chr(10), ' ')}")
        lines.append("")
    lines.append(
        "_After you edit `CLAUDE.md`, the next grounded review will pick up the rules via KnowledgeSync._"
    )
    return "\n".join(lines)


def maybe_knowledge_sync_proposal_for_scanners(
    repo_root: Path | None,
    *scanner_chunks: str | None,
) -> str:
    """Build a proposal block when recurring architectural debt appears in scanner markdown."""
    combined = "\n\n".join(c for c in scanner_chunks if c)
    hints = extract_recurring_smell_hints(combined)
    if not hints:
        return ""
    return propose_claude_md_update(repo_root, recurring_hints=hints)
