"""Post-process Claude Code session output: verify cited paths and line numbers against the workspace."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


WARN_PREFIX = "⚠️ Grounding Mismatch"

# Dotted extensions plus extensionless repo files often cited in sessions.
_DOTTED_EXT = (
    r"(?:py|pyi|toml|yaml|yml|json|rs|go|tsx?|jsx?|sh|graphql|sql|txt|md|ini|cfg|conf"
    r"|lock|sum|cmake|plist|swift|kt|java|c|h|cpp|hpp|cc|hh|m|mm)"
)
_SPECIAL_BASE = rf"(?:[\w.-]+\.{_DOTTED_EXT}|Dockerfile|Makefile|LICENSE|Cargo\.toml|py\.typed|\.env\.example)"
# Path with at least one slash (e.g. ``src/pkg/mod.py``, ``deploy/Dockerfile``).
_PATH_WITH_SLASH = rf"(?P<p>(?:[\w.-]+/)+{_SPECIAL_BASE})"
# Top-level file: ``README.md``, ``Dockerfile``.
_TOP_LEVEL_FILE = rf"(?P<p2>{_SPECIAL_BASE})"


@dataclass(frozen=True)
class Citation:
    """A file reference extracted from agent output."""

    raw: str
    line: int | None
    span_start: int


def _normalize_raw_path(raw: str) -> str:
    s = raw.strip().strip('\'"`')
    s = s.replace("\\", "/")
    # Strip trailing junk often glued by prose / markdown.
    s = s.rstrip(").,;>]}…")
    # Ripgrep-style "-123-" suffix occasionally pasted (best-effort strip).
    if re.search(r"-\d+-\s*$", s):
        s = re.sub(r"-\d+-\s*$", "", s)
    return s.strip()


def _is_url_or_scheme(s: str) -> bool:
    sl = s.lower()
    return sl.startswith(("http://", "https://", "ftp://", "file://")) or "://" in s


def resolve_under_workspace(workspace: Path, raw: str) -> tuple[Path | None, str]:
    """Resolve string to a path confined to ``workspace``; return ``(None, reason)`` if not applicable."""
    ws = workspace.expanduser().resolve()
    p = _normalize_raw_path(raw)
    if not p or _is_url_or_scheme(p):
        return None, "url_or_empty"
    # Skip obvious globs / templates.
    if any(ch in p for ch in "*?<>|"):
        return None, "glob"

    path = Path(p)
    try:
        if path.is_absolute():
            resolved = path.resolve()
            resolved.relative_to(ws)
            return resolved, "ok"
        cand = (ws / p).resolve()
        cand.relative_to(ws)
        return cand, "ok"
    except (ValueError, OSError):
        return None, "outside_or_invalid"


def _line_count(path: Path) -> int | None:
    try:
        if not path.is_file():
            return None
        n = 0
        with path.open("rb") as fh:
            for _ in fh:
                n += 1
        return n
    except OSError:
        return None


_RE_PATH_LINE_COLON = re.compile(
    rf"(?<![\w/.]){_PATH_WITH_SLASH}:(?P<ln>\d{{1,7}})(?!\d)",
    re.MULTILINE,
)
_RE_PATH_LINE_HASH = re.compile(
    rf"(?<![\w/.]){_PATH_WITH_SLASH}#L(?P<lh>\d{{1,7}})\b",
    re.MULTILINE,
)
_RE_PATH_ONLY = re.compile(
    rf"(?<![\w/.]){_PATH_WITH_SLASH}(?![:\w#])(?=[\s)\]`\"',]|$)",
    re.MULTILINE,
)
_RE_TOP_LINE = re.compile(
    rf"(?<![\w/.]){_TOP_LEVEL_FILE}:(?P<ln2>\d{{1,7}})(?!\d)",
    re.MULTILINE,
)
_RE_TOP_ONLY = re.compile(
    rf"(?<![\w/.]){_TOP_LEVEL_FILE}(?![:\w#])(?=[\s)\]`\"',]|$)",
    re.MULTILINE,
)


def extract_citations(text: str) -> list[Citation]:
    """Pull path references (with optional line numbers) from session text."""
    out: list[Citation] = []
    seen: set[tuple[str, int | None, int]] = set()

    def add(raw_path: str, line: int | None, start: int) -> None:
        key = (raw_path, line, start)
        if key in seen:
            return
        seen.add(key)
        out.append(Citation(raw=raw_path, line=line, span_start=start))

    for m in _RE_PATH_LINE_COLON.finditer(text):
        add(m.group("p"), int(m.group("ln")), m.start("p"))
    for m in _RE_PATH_LINE_HASH.finditer(text):
        add(m.group("p"), int(m.group("lh")), m.start("p"))
    for m in _RE_TOP_LINE.finditer(text):
        add(m.group("p2"), int(m.group("ln2")), m.start("p2"))
    for m in _RE_PATH_ONLY.finditer(text):
        add(m.group("p"), None, m.start("p"))
    for m in _RE_TOP_ONLY.finditer(text):
        add(m.group("p2"), None, m.start("p2"))

    out.sort(key=lambda c: c.span_start)
    return out


def audit_transcript(text: str, workspace: Path) -> list[str]:
    """Return terminal warning lines for each grounding mismatch."""
    ws = workspace.expanduser().resolve()
    warnings: list[str] = []
    citations = extract_citations(text)

    for cite in citations:
        resolved, reason = resolve_under_workspace(ws, cite.raw)
        if resolved is None:
            if reason == "outside_or_invalid":
                warnings.append(
                    f"{WARN_PREFIX}: cited path is outside or invalid relative to workspace "
                    f"`{ws}` → {_normalize_raw_path(cite.raw)!r}"
                )
            continue

        if not resolved.exists():
            ln_note = f" (line {cite.line} cited)" if cite.line is not None else ""
            warnings.append(
                f"{WARN_PREFIX}: file does not exist → {_normalize_raw_path(cite.raw)!r}{ln_note}"
            )
            continue

        if cite.line is None:
            continue

        if resolved.is_dir():
            warnings.append(
                f"{WARN_PREFIX}: line {cite.line} cited but path is a directory → {cite.raw!r}"
            )
            continue

        nlines = _line_count(resolved)
        if nlines is None:
            continue
        if cite.line < 1 or cite.line > nlines:
            warnings.append(
                f"{WARN_PREFIX}: line {cite.line} out of range for `{resolved.relative_to(ws)}` "
                f"(file has {nlines} line(s))"
            )

    return warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify file paths and line numbers in Claude Code session output against the workspace "
            "(anti-hallucination receipt audit)."
        ),
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Transcript path (default: read stdin)",
    )
    parser.add_argument(
        "--workspace",
        "-w",
        type=Path,
        default=None,
        help="Repository root for resolving relative paths (default: current directory)",
    )
    args = parser.parse_args(argv)

    ws = (args.workspace or Path.cwd()).expanduser().resolve()

    if args.input_file:
        path = Path(args.input_file)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            sys.stderr.write(f"receipt_auditor: could not read {path}: {exc}\n")
            return 2
    else:
        text = sys.stdin.read()

    warns = audit_transcript(text, ws)
    for w in warns:
        print(w, file=sys.stderr)

    return 1 if warns else 0


if __name__ == "__main__":
    raise SystemExit(main())
