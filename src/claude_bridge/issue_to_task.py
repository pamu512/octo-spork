#!/usr/bin/env python3
"""Convert SARIF (Trivy / CodeQL) into batched ``claude -p`` remediation commands.

Groups findings into compact batches so each Claude Code invocation stays within a reasonable
context size. Use ``--batch-size`` to tune how many vulnerabilities are addressed per command.

Example::

    PYTHONPATH=src python -m claude_bridge.issue_to_task \\
        --sarif .octo-spork-trivy-run/results.sarif \\
        --batch-size 3 \\
        --emit shell
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_DEFAULT_BATCH_SIZE = 4
_MAX_PROMPT_CHARS_SOFT = 14_000


def _sarif_level_rank(level: str | None) -> int:
    normalized = str(level or "warning").lower()
    return {"error": 4, "warning": 3, "note": 2, "none": 1}.get(normalized, 2)


def _uri_to_repo_relative(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path or "")
        return path.lstrip("/")
    return uri.replace("\\", "/")


def _tool_name_from_run(run: dict[str, Any]) -> str:
    driver = (run.get("tool") or {}).get("driver") or {}
    name = str(driver.get("name") or "").strip()
    if name:
        return name
    return "SARIF"


@dataclass(frozen=True)
class RemediationFinding:
    """One SARIF result normalized for remediation prompts."""

    tool: str
    severity: str
    rule_id: str
    rule_name: str
    message: str
    file_path: str
    line: int
    rank: int

    def short_title(self) -> str:
        if self.rule_name and self.rule_name not in {self.rule_id, ""}:
            return self.rule_name
        return self.rule_id or "finding"

    def one_line_summary(self, *, max_msg: int = 220) -> str:
        msg = " ".join(self.message.split())
        if len(msg) > max_msg:
            msg = msg[: max_msg - 3] + "..."
        loc = f"{self.file_path}:{self.line}" if self.line else self.file_path
        return f"{self.severity} — {self.short_title()} @ `{loc}`"


def parse_sarif_findings(payload: dict[str, Any]) -> list[RemediationFinding]:
    """Parse SARIF 2.1 JSON into findings, sorted by severity then document order."""
    items: list[RemediationFinding] = []
    for run in payload.get("runs") or []:
        if not isinstance(run, dict):
            continue
        tool = _tool_name_from_run(run)
        rules_map: dict[str, dict[str, Any]] = {}
        driver = ((run.get("tool") or {}).get("driver")) or {}
        for rule in driver.get("rules") or []:
            if isinstance(rule, dict) and rule.get("id"):
                rules_map[str(rule["id"])] = rule

        for result in run.get("results") or []:
            if not isinstance(result, dict):
                continue
            level = str(result.get("level") or "warning")
            rank = _sarif_level_rank(level)
            rule_id = str(result.get("ruleId") or "")
            rule_meta = rules_map.get(rule_id) or {}
            short = rule_meta.get("shortDescription")
            short_txt = str(short.get("text") or "") if isinstance(short, dict) else ""
            rule_name = str(rule_meta.get("name") or short_txt or "").strip()

            msg_obj = result.get("message")
            if isinstance(msg_obj, dict):
                message = str(msg_obj.get("text") or rule_id or "")
            else:
                message = str(msg_obj or "")
            message = message.strip()

            rel_path = "(unknown file)"
            line = 0
            locations = result.get("locations") or []
            if isinstance(locations, list) and locations:
                loc0 = locations[0] if isinstance(locations[0], dict) else {}
                phys = loc0.get("physicalLocation") if isinstance(loc0.get("physicalLocation"), dict) else {}
                region = phys.get("region") if isinstance(phys.get("region"), dict) else {}
                al = phys.get("artifactLocation") if isinstance(phys.get("artifactLocation"), dict) else {}
                uri = str(al.get("uri") or "")
                rel_path = _uri_to_repo_relative(uri) or uri or rel_path
                try:
                    line = int(region.get("startLine") or 0)
                except (TypeError, ValueError):
                    line = 0

            items.append(
                RemediationFinding(
                    tool=tool,
                    severity=level.upper(),
                    rule_id=rule_id or "(rule)",
                    rule_name=rule_name,
                    message=message,
                    file_path=rel_path,
                    line=line,
                    rank=rank,
                )
            )

    items.sort(key=lambda f: (-f.rank, f.file_path, f.line, f.rule_id))
    return items


def load_sarif_path(path: Path) -> list[RemediationFinding]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("SARIF root must be a JSON object")
    return parse_sarif_findings(payload)


def _remediation_instruction(f: RemediationFinding) -> str:
    """Single-sentence task text suitable for inclusion in a batched ``-p`` prompt."""
    title = f.short_title()
    where = f"in `{f.file_path}` at line {f.line}" if f.line else f"in `{f.file_path}`"
    detail = f.message if f.message else "Address per rule documentation."
    return (
        f"Fix the finding `{title}` {where} as identified by {f.tool} "
        f"(rule `{f.rule_id}`, {f.severity}). {detail} "
        "Prefer minimal, idiomatic fixes (e.g. parameterized queries for SQL injection); add tests if appropriate."
    )


def _compose_batch_prompt(batch: list[RemediationFinding], *, batch_index: int, batch_total: int) -> str:
    lines = [
        "Remediation tasks from static analysis (SARIF). Complete each item fully before moving to the next.",
        f"This is batch {batch_index + 1} of {batch_total}.",
        "",
    ]
    for j, f in enumerate(batch, start=1):
        lines.append(f"{j}. {_remediation_instruction(f)}")
    return "\n".join(lines)


def build_batched_prompts(
    findings: list[RemediationFinding],
    *,
    batch_size: int,
    max_chars_per_batch: int | None = None,
) -> list[str]:
    """Build one natural-language prompt string per batch (character-capped)."""
    if not findings:
        return []
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    cap = max_chars_per_batch or _MAX_PROMPT_CHARS_SOFT

    raw_batches: list[list[RemediationFinding]] = []
    i = 0
    n = len(findings)
    est_batches = max(1, (n + batch_size - 1) // batch_size)
    while i < n:
        end = min(i + batch_size, n)
        chunk = list(findings[i:end])
        prompt = _compose_batch_prompt(chunk, batch_index=len(raw_batches), batch_total=est_batches)
        while len(chunk) > 1 and len(prompt) > cap:
            chunk = chunk[:-1]
            prompt = _compose_batch_prompt(chunk, batch_index=len(raw_batches), batch_total=est_batches)
        if not chunk:
            chunk = [findings[i]]
        raw_batches.append(chunk)
        i += len(chunk)

    total = len(raw_batches)
    return [_compose_batch_prompt(b, batch_index=k, batch_total=total) for k, b in enumerate(raw_batches)]


def format_claude_commands(
    prompts: list[str],
    *,
    claude_bin: str,
    emit: str,
) -> str:
    """Render prompts as ``claude -p`` lines or plain text."""
    parts: list[str] = []
    for i, prompt in enumerate(prompts):
        if emit == "text":
            parts.append(f"--- batch {i + 1} ---\n{prompt}\n")
        else:
            quoted = shlex.quote(prompt)
            parts.append(f"{claude_bin} -p {quoted}")
    return "\n".join(parts).rstrip() + "\n"


def run_cli(
    *,
    sarif: Path,
    batch_size: int,
    max_chars: int | None,
    claude_bin: str,
    emit: str,
    max_findings: int | None,
) -> int:
    path = sarif.expanduser().resolve()
    if not path.is_file():
        print(f"error: SARIF file not found: {path}", file=sys.stderr)
        return 2
    try:
        findings = load_sarif_path(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: could not parse SARIF: {exc}", file=sys.stderr)
        return 1

    if max_findings is not None and max_findings >= 0:
        findings = findings[:max_findings]

    prompts = build_batched_prompts(findings, batch_size=batch_size, max_chars_per_batch=max_chars)

    if emit == "json":
        obj = {
            "sarif": str(path),
            "finding_count": len(findings),
            "batch_count": len(prompts),
            "batches": [{"index": i + 1, "prompt": p} for i, p in enumerate(prompts)],
            "commands": [
                {"index": i + 1, "shell": f"{claude_bin} -p {shlex.quote(p)}"}
                for i, p in enumerate(prompts)
            ],
        }
        print(json.dumps(obj, indent=2))
        return 0

    text = format_claude_commands(prompts, claude_bin=claude_bin, emit=emit)
    sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit batched Claude Code remediation commands from a SARIF file.",
    )
    parser.add_argument("--sarif", type=Path, required=True, help="Path to results.sarif (Trivy or CodeQL)")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"Max findings per claude invocation (default: {_DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--max-chars-per-batch",
        type=int,
        default=None,
        help=f"Soft limit for prompt body; batches may shrink (default: {_MAX_PROMPT_CHARS_SOFT})",
    )
    parser.add_argument("--claude-bin", default="claude", help="Executable name for Claude Code CLI")
    parser.add_argument(
        "--emit",
        choices=("shell", "text", "json"),
        default="shell",
        help="shell: claude -p lines; text: prompts only; json: structured output",
    )
    parser.add_argument(
        "--max-findings",
        type=int,
        default=None,
        help="Process only the first N findings (after severity sort)",
    )
    args = parser.parse_args(argv)

    return run_cli(
        sarif=args.sarif,
        batch_size=max(1, args.batch_size),
        max_chars=args.max_chars_per_batch,
        claude_bin=args.claude_bin.strip() or "claude",
        emit=args.emit,
        max_findings=args.max_findings,
    )


if __name__ == "__main__":
    raise SystemExit(main())
