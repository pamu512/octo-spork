"""Dispatch context pruning by file type: Python uses ``ast``; other languages use tree-sitter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from context_pruner import python_prune
from context_pruner import tree_sitter_prune
from context_pruner.comments import omitted_comment


@dataclass(frozen=True)
class ContextPruneResult:
    """Pruned source suitable for LLM context."""

    text: str
    path: Path
    line: int
    engine: Literal["python-ast", "tree-sitter", "fallback"]
    note: str | None = None


def prune_file_for_llm(path: Path | str, line: int, *, encoding: str = "utf-8") -> ContextPruneResult:
    """
    Keep the enclosing function/class, supporting imports and same-file symbols it uses.

    Other contiguous regions are replaced by a single omission comment per gap.
    ``line`` is 1-based (as in SARIF / many scanners).
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding=encoding, errors="replace")
    except OSError as exc:
        return ContextPruneResult(
            text="",
            path=p,
            line=line,
            engine="fallback",
            note=f"read_error:{exc}",
        )

    suf = p.suffix.lower()
    if suf == ".py":
        text, note = python_prune.prune_python_source(raw, line)
        return ContextPruneResult(text=text, path=p, line=line, engine="python-ast", note=note)

    if suf in tree_sitter_prune.SUPPORTED_SUFFIXES:
        text, note = tree_sitter_prune.prune_with_tree_sitter(p, raw, line)
        return ContextPruneResult(text=text, path=p, line=line, engine="tree-sitter", note=note)

    text, note = _fallback_window(raw, line)
    return ContextPruneResult(text=text, path=p, line=line, engine="fallback", note=note)


def _fallback_window(source: str, line: int, *, window: int = 40) -> tuple[str, str | None]:
    if line < 1:
        return source, "invalid_line"
    lines = source.splitlines()
    if not lines:
        return "", "empty"
    lo = max(1, line - window // 2)
    hi = min(len(lines), lo + window - 1)
    chunk = "\n".join(lines[lo - 1 : hi])
    om = omitted_comment()
    head = f"{om}\n" if lo > 1 else ""
    tail = f"\n{om}" if hi < len(lines) else ""
    return f"{head}{chunk}{tail}", "unsupported_extension_window"
