"""SelfHeal: pytest → Claude fix loop with a grounded failure report when fixes exhaust."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PytestAttempt:
    """One ``pytest`` invocation."""

    round_index: int
    exit_code: int
    combined_output: str


@dataclass
class ClaudeAttempt:
    """One Claude fix invocation after a failed pytest."""

    round_index: int
    exit_code: int
    stdout: str
    stderr: str
    prompt_excerpt: str


@dataclass
class SelfHealOutcome:
    workspace: Path
    max_fix_attempts: int
    pytest_attempts: list[PytestAttempt] = field(default_factory=list)
    claude_attempts: list[ClaudeAttempt] = field(default_factory=list)
    success: bool = False


def _truncate(text: str, max_chars: int) -> str:
    t = text.strip()
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    head = max_chars // 2
    tail = max_chars - head - 50
    return (
        t[:head]
        + "\n\n… [truncated "
        + str(len(t) - head - tail)
        + " chars] …\n\n"
        + t[-tail:]
    )


def build_fix_prompt(pytest_output: str) -> str:
    """Prompt passed to ``claude -p`` for the next fix attempt."""
    max_chars = int(os.environ.get("OCTO_SELF_HEAL_MAX_FAILURE_CHARS", "120000"))
    body = _truncate(pytest_output.strip(), max_chars)
    return (
        "Fix these test failures.\n\n"
        "Analyze the pytest output below, edit the codebase to resolve failures, "
        "and keep changes minimal and consistent with existing style.\n\n"
        "--- pytest output ---\n"
        f"{body}\n"
    )


def run_pytest(
    workspace: Path,
    pytest_argv: list[str],
    *,
    timeout_sec: float | None = None,
) -> tuple[int, str]:
    """Run pytest under ``workspace``; return exit code and merged stdout/stderr."""
    if timeout_sec is None:
        timeout_sec = float(os.environ.get("OCTO_SELF_HEAL_PYTEST_TIMEOUT_SEC", "0")) or None
    cmd = [sys.executable, "-m", "pytest", *pytest_argv]
    proc = subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    out_parts = []
    if proc.stdout:
        out_parts.append(proc.stdout)
    if proc.stderr:
        out_parts.append(proc.stderr)
    combined = "\n".join(out_parts)
    return proc.returncode, combined


def run_claude_fix(
    workspace: Path,
    prompt: str,
    *,
    no_add_dir: bool = False,
    timeout_sec: float | None = None,
) -> tuple[int, str, str]:
    """Invoke Claude via :func:`claude_bridge.run_agent.assemble_claude_command`."""
    from claude_bridge.run_agent import assemble_claude_command

    if timeout_sec is None:
        raw = os.environ.get("OCTO_SELF_HEAL_CLAUDE_TIMEOUT_SEC", "").strip()
        timeout_sec = float(raw) if raw else None

    cmd = assemble_claude_command(
        workspace=workspace,
        claude_argv=["-p", prompt],
        no_add_dir=no_add_dir,
    )
    from claude_bridge.resource_monitor import vram_guard_allows_claude_launch

    ok_guard, guard_msg = vram_guard_allows_claude_launch()
    if not ok_guard:
        if guard_msg:
            sys.stderr.write(guard_msg)
        return 1, "", ""

    proc = subprocess.run(
        cmd,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    merged = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if merged.strip():
        from claude_bridge.session_store import (
            extract_session_id,
            record_session_enabled,
            save_last_session_id,
        )

        if record_session_enabled():
            sid = extract_session_id(merged)
            if sid:
                save_last_session_id(workspace, sid)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def render_grounded_failure_report(outcome: SelfHealOutcome) -> str:
    """Markdown report: timeline of pytest / Claude attempts and final failure context."""
    lines: list[str] = [
        "# Grounded Failure Report (SelfHeal)",
        "",
        f"- **Workspace:** `{outcome.workspace}`",
        f"- **Max fix attempts:** {outcome.max_fix_attempts}",
        f"- **Outcome:** tests still failing after exhausting the SelfHeal loop.",
        "",
        "## What ran",
        "",
    ]

    for pa in outcome.pytest_attempts:
        lines.append(f"### Pytest (round {pa.round_index}) — exit `{pa.exit_code}`")
        lines.append("")
        lines.append("```text")
        lines.append(_truncate(pa.combined_output, 24000))
        lines.append("```")
        lines.append("")

    if outcome.claude_attempts:
        lines.append("## What the agent tried (Claude)")
        lines.append("")
        for ca in outcome.claude_attempts:
            lines.append(f"### Claude attempt {ca.round_index} — exit `{ca.exit_code}`")
            lines.append("")
            lines.append("<details><summary>Prompt excerpt</summary>")
            lines.append("")
            lines.append("```text")
            lines.append(_truncate(ca.prompt_excerpt, 8000))
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")
            if ca.stdout.strip():
                lines.append("**stdout (excerpt)**")
                lines.append("")
                lines.append("```text")
                lines.append(_truncate(ca.stdout, 12000))
                lines.append("```")
                lines.append("")
            if ca.stderr.strip():
                lines.append("**stderr (excerpt)**")
                lines.append("")
                lines.append("```text")
                lines.append(_truncate(ca.stderr, 12000))
                lines.append("```")
                lines.append("")
    else:
        lines.append("## What the agent tried (Claude)")
        lines.append("")
        lines.append("_No Claude fix attempts were recorded._")
        lines.append("")

    lines.append("## Where this got stuck")
    lines.append("")
    if outcome.pytest_attempts:
        last = outcome.pytest_attempts[-1]
        lines.append(
            f"- Last **pytest** exited with code **{last.exit_code}** "
            f"(see final pytest block above)."
        )
    else:
        lines.append("- No pytest output was captured.")
    if outcome.claude_attempts:
        lc = outcome.claude_attempts[-1]
        lines.append(
            f"- Last **Claude** invocation exited with code **{lc.exit_code}**."
        )
    lines.append(
        "- Review the tracebacks and failing tests in the **last pytest** section, "
        "then fix manually or narrow the failing scope (e.g. single test file)."
    )
    lines.append("")
    return "\n".join(lines)


def run_self_heal(
    workspace: Path,
    pytest_argv: list[str],
    *,
    max_fix_attempts: int = 3,
    skip_claude: bool = False,
    no_add_dir: bool = False,
) -> SelfHealOutcome:
    """Run pytest up to ``max_fix_attempts`` Claude repair rounds; collect outcomes."""
    ws = workspace.expanduser().resolve()
    outcome = SelfHealOutcome(
        workspace=ws,
        max_fix_attempts=max_fix_attempts,
    )

    fix_round = 0
    while True:
        rc, combined = run_pytest(ws, pytest_argv)
        outcome.pytest_attempts.append(
            PytestAttempt(round_index=len(outcome.pytest_attempts) + 1, exit_code=rc, combined_output=combined)
        )
        if rc == 0:
            outcome.success = True
            return outcome

        if skip_claude:
            return outcome

        if fix_round >= max_fix_attempts:
            return outcome

        prompt = build_fix_prompt(combined)
        excerpt = _truncate(prompt, 4000)

        if skip_claude:
            outcome.claude_attempts.append(
                ClaudeAttempt(
                    round_index=fix_round + 1,
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    prompt_excerpt=excerpt + "\n[skipped: OCTO_SELF_HEAL_SKIP_CLAUDE or --skip-claude]",
                )
            )
        else:
            cc, out, err = run_claude_fix(ws, prompt, no_add_dir=no_add_dir)
            outcome.claude_attempts.append(
                ClaudeAttempt(
                    round_index=fix_round + 1,
                    exit_code=cc,
                    stdout=out,
                    stderr=err,
                    prompt_excerpt=excerpt,
                )
            )

        fix_round += 1


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--" in argv:
        idx = argv.index("--")
        pre, post = argv[:idx], argv[idx + 1 :]
    else:
        pre, post = argv, []

    parser = argparse.ArgumentParser(
        description="Run pytest; on failure, invoke Claude to fix (up to N rounds), then re-test.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Repository root for pytest and Claude (default: cwd)",
    )
    parser.add_argument(
        "--max-fix-attempts",
        type=int,
        default=int(os.environ.get("OCTO_SELF_HEAL_MAX_FIX_ATTEMPTS", "3")),
        help="Max Claude fix rounds after failed pytest (default: 3)",
    )
    parser.add_argument(
        "--skip-claude",
        action="store_true",
        help="Do not invoke Claude (for CI / debugging the loop)",
    )
    parser.add_argument(
        "--no-add-dir",
        action="store_true",
        help="Skip run_agent --add-dir injection for Claude",
    )
    args = parser.parse_args(pre)

    ws = (args.workspace or Path.cwd()).expanduser().resolve()
    skip = args.skip_claude or os.environ.get("OCTO_SELF_HEAL_SKIP_CLAUDE", "").strip() == "1"

    outcome = run_self_heal(
        ws,
        post,
        max_fix_attempts=max(0, args.max_fix_attempts),
        skip_claude=skip,
        no_add_dir=args.no_add_dir,
    )

    if outcome.success:
        sys.stderr.write("[SelfHeal] All tests passed.\n")
        return 0

    report = render_grounded_failure_report(outcome)
    sys.stdout.write(report)
    if not report.endswith("\n"):
        sys.stdout.write("\n")
    sys.stderr.write("[SelfHeal] Tests still failing — see Grounded Failure Report on stdout.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
