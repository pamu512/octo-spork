"""TokenGovernor: throttle oversized single-shot ``claude`` invocations for slow local LLMs.

Uses the same **characters ÷ 4** heuristic as ``deploy/claude-code/src/tokenEstimation.ts`` when Bun is
available; otherwise mirrors it in Python.

If estimated tokens for the combined prompt / system content exceed the budget (default **32k**),
the governor **blocks** ``exec`` until the user chooses to proceed or abort (``Chunk`` / compress
instructions).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from claude_bridge.run_agent import assemble_claude_command

REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BUDGET = 32_000


def gather_estimation_payload(claude_argv: list[str]) -> str:
    """Collect text passed via flags that dominate context size (prompt + system append)."""
    chunks: list[str] = []
    i = 0
    while i < len(claude_argv):
        a = claude_argv[i]
        if a in ("-p", "--print") and i + 1 < len(claude_argv):
            chunks.append(claude_argv[i + 1])
            i += 2
            continue
        if a == "--append-system-prompt" and i + 1 < len(claude_argv):
            chunks.append(claude_argv[i + 1])
            i += 2
            continue
        if a == "--append-system-prompt-file" and i + 1 < len(claude_argv):
            p = Path(claude_argv[i + 1]).expanduser()
            try:
                chunks.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                chunks.append("")
            i += 2
            continue
        if a in ("-s", "--system-prompt") and i + 1 < len(claude_argv):
            chunks.append(claude_argv[i + 1])
            i += 2
            continue
        i += 1
    return "\n\n".join(chunks)


def estimate_tokens_python(text: str) -> int:
    t = text.strip()
    if not t:
        return 0
    return max(1, (len(t) + 3) // 4)


def estimate_tokens(repo_root: Path, text: str) -> int:
    """Prefer ``tokenEstimation.ts`` (Bun); fall back to Python mirror."""
    ts_dir = repo_root / "deploy" / "claude-code"
    ts_file = ts_dir / "src" / "tokenEstimation.ts"
    if ts_file.is_file() and shutil.which("bun"):
        proc = subprocess.run(
            ["bun", "run", "src/tokenEstimation.ts", "--stdin"],
            cwd=str(ts_dir),
            input=text,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            try:
                data = json.loads(proc.stdout.strip().splitlines()[-1])
                return int(data.get("estimatedTokens", 0))
            except (json.JSONDecodeError, TypeError, ValueError, IndexError):
                pass
    return estimate_tokens_python(text)


def prompt_over_budget(estimated: int, budget: int) -> str:
    """Return ``proceed`` or ``abort``."""
    print(
        f"\nTokenGovernor: single-task estimate **{estimated:,}** tokens exceeds budget **{budget:,}** "
        "(may clog a local GPU / large context).\n",
        file=sys.stderr,
    )
    print(
        "  [c] **Chunk** — split into smaller tasks / prompts and run again\n"
        "  [s] **Compress** — shorten prompt or drop files from context, then re-run the same command\n"
        "  [p] **Proceed anyway** — run claude despite the estimate\n"
        "  [a] **Abort** (default)\n",
        file=sys.stderr,
    )
    try:
        choice = input("Choice [c/s/p/a]: ").strip().lower() or "a"
    except EOFError:
        return "abort"
    if choice in {"p", "proceed", "y", "yes"}:
        return "proceed"
    return "abort"


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] in {"elevate", "restrict"}:
        print(
            "Use `python -m claude_bridge.run_agent " + argv[0] + " ...` for permission changes.",
            file=sys.stderr,
        )
        return 2

    raw = argv
    if "--" in raw:
        idx = raw.index("--")
        pre, post = raw[:idx], raw[idx + 1 :]
    else:
        pre, post = raw, []

    parser = argparse.ArgumentParser(
        description="Run claude behind a token budget check (local LLM throttle).",
    )
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--no-add-dir", action="store_true")
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help=f"Max estimated tokens per invocation (default env OCTO_TOKEN_BUDGET or {_DEFAULT_BUDGET})",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Non-interactive: proceed without prompting (CI / scripts)",
    )
    args = parser.parse_args(pre)

    ws = (args.workspace or Path.cwd()).expanduser().resolve()

    from claude_bridge.resource_monitor import enforce_vram_guard_before_claude

    enforce_vram_guard_before_claude()

    budget = args.budget
    if budget is None:
        env_b = (os.environ.get("OCTO_TOKEN_BUDGET") or "").strip()
        budget = int(env_b) if env_b.isdigit() else _DEFAULT_BUDGET
    budget = max(1, budget)

    if os.environ.get("OCTO_SKIP_TOKEN_GOVERNOR", "").strip() == "1":
        cmd = assemble_claude_command(workspace=ws, claude_argv=post, no_add_dir=args.no_add_dir)
        os.execvp(cmd[0], cmd)
    payload = gather_estimation_payload(post)
    est = estimate_tokens(REPO_ROOT, payload)

    if est > budget and not args.yes:
        decision = prompt_over_budget(est, budget)
        if decision != "proceed":
            print("Aborted.", file=sys.stderr)
            return 1

    cmd = assemble_claude_command(workspace=ws, claude_argv=post, no_add_dir=args.no_add_dir)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    raise SystemExit(main())
