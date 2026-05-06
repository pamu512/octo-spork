"""Build a **Grounded Evidence** markdown block for Claude Code ``--append-system-prompt``.

Sources:

- Raw output from the **last N** failed test runs (default 3), read from
  ``<repo>/.octo/evidence/pytest_failures/*.{log,txt}`` (newest by mtime).
- **Top M** Ruff diagnostics (default 5), preferring error-severity / high-priority codes when JSON
  exposes them.

Tests can append failure transcripts to that directory (CI, local pytest/unittest wrappers).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_DEFAULT_REPO = Path.cwd()
_DEFAULT_PYTEST_TAIL = 3
_DEFAULT_RUFF_TOP = 5
_DEFAULT_LOG_MAX = 12_000

_PYTEST_DIR_REL = Path(".octo/evidence/pytest_failures")


def _sorted_failure_logs(repo: Path) -> list[Path]:
    base = repo / _PYTEST_DIR_REL
    if not base.is_dir():
        return []
    logs: list[tuple[float, Path]] = []
    for name in os.listdir(base):
        if not name.endswith((".log", ".txt")):
            continue
        p = base / name
        if not p.is_file():
            continue
        try:
            logs.append((p.stat().st_mtime, p))
        except OSError:
            continue
    logs.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in logs]


def _truncate_block(text: str, limit: int) -> str:
    text = text.strip("\n")
    if len(text) <= limit:
        return text
    return text[: limit - 80] + "\n\n… [truncated by Octo evidence builder]\n"


def section_pytest_failures(repo: Path, *, tail: int, log_max: int) -> str:
    logs = _sorted_failure_logs(repo)[:tail]
    if not logs:
        return (
            "_No failure transcripts found._ Drop pytest/unittest stderr under "
            f"`{_PYTEST_DIR_REL}/` (``*.log`` / ``*.txt``), newest files used first."
        )

    parts: list[str] = []
    for i, path in enumerate(logs, start=1):
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raw = f"[could not read {path}: {exc}]"
        parts.append(f"#### Failure log {i}: `{path.name}`\n\n```text\n{_truncate_block(raw, log_max)}\n```")
    return "\n\n".join(parts)


def _rank_ruff_diag(d: dict[str, object]) -> tuple[int, str]:
    """Lower tuple sorts earlier = higher priority."""
    sev = str(d.get("severity") or "").lower()
    if sev in {"error", "fatal"}:
        tier = 0
    elif sev == "warning":
        tier = 1
    else:
        code = str(d.get("code") or "")
        tier = 2 if code[:1] in {"E", "F"} else 3
    loc = str(d.get("filename") or d.get("path") or "")
    code = str(d.get("code") or "")
    return (tier, loc, code)


def section_ruff_critical(repo: Path, *, top: int, timeout_sec: float = 120.0) -> str:
    """Run ``ruff check`` JSON and format up to ``top`` diagnostics (critical-first)."""
    try:
        proc = subprocess.run(
            [
                "ruff",
                "check",
                str(repo),
                "--output-format",
                "json",
                "--exit-zero",
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError:
        return "_Ruff not installed or not on PATH — install ``ruff`` to populate lint evidence._"
    except subprocess.TimeoutExpired:
        return "_Ruff check timed out — skipping lint evidence._"

    raw = (proc.stdout or "").strip()
    if not raw:
        return "_Ruff produced no JSON output — skipping lint evidence._"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "_Could not parse Ruff JSON output._"

    rows: list[dict[str, object]] = []
    if isinstance(payload, list):
        rows = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        inner = payload.get("violations") or payload.get("diagnostics") or payload.get("messages")
        if isinstance(inner, list):
            rows = [x for x in inner if isinstance(x, dict)]

    if not rows:
        return "_No Ruff diagnostics returned (clean tree or unsupported JSON shape)._"

    rows.sort(key=_rank_ruff_diag)
    chosen = rows[:top]

    lines = ["| Code | Location | Message |", "| --- | --- | --- |"]
    for d in chosen:
        code = str(d.get("code") or "")
        msg = str(d.get("message") or "")
        msg = re.sub(r"\s+", " ", msg).strip()
        if len(msg) > 160:
            msg = msg[:157] + "..."
        loc_obj = d.get("location") if isinstance(d.get("location"), dict) else None
        fn = str(d.get("filename") or d.get("path") or "")
        row = 0
        col = 0
        if isinstance(loc_obj, dict):
            row = int(loc_obj.get("row") or loc_obj.get("line") or 0)
            col = int(loc_obj.get("column") or 0)
        elif "lineno" in d:
            try:
                row = int(d["lineno"])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                row = 0
        loc = f"`{fn}`:{row}" if fn else f":{row}"
        if col:
            loc += f":{col}"
        lines.append(f"| {code} | {loc} | {msg} |")
    return "\n".join(lines) + "\n"


def build_grounded_evidence_markdown(
    repo_root: Path,
    *,
    pytest_tail: int = _DEFAULT_PYTEST_TAIL,
    ruff_top: int = _DEFAULT_RUFF_TOP,
    log_max: int = _DEFAULT_LOG_MAX,
) -> str:
    repo = repo_root.expanduser().resolve()
    parts = [
        "## Grounded Evidence",
        "",
        "_Octo-spork evidence injection — failing tests + critical Ruff diagnostics._",
        "",
        "### Last failed test runs (raw transcripts)",
        "",
        section_pytest_failures(repo, tail=pytest_tail, log_max=log_max),
        "",
        "### Critical lint (Ruff)",
        "",
        section_ruff_critical(repo, top=ruff_top),
        "",
    ]
    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit Grounded Evidence markdown for Claude Code.")
    parser.add_argument("--repo", type=Path, default=_DEFAULT_REPO, help="Repository root")
    parser.add_argument("--pytest-tail", type=int, default=_DEFAULT_PYTEST_TAIL)
    parser.add_argument("--ruff-top", type=int, default=_DEFAULT_RUFF_TOP)
    parser.add_argument("--log-max", type=int, default=_DEFAULT_LOG_MAX)
    parser.add_argument(
        "--write",
        type=Path,
        default=None,
        help="Write markdown to this file instead of stdout",
    )
    args = parser.parse_args(argv)

    md = build_grounded_evidence_markdown(
        args.repo,
        pytest_tail=max(0, args.pytest_tail),
        ruff_top=max(0, args.ruff_top),
        log_max=max(1000, args.log_max),
    )
    if args.write is not None:
        out = args.write.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(out, file=sys.stdout)
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
