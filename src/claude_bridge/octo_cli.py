"""Octo-spork CLI helpers: resume last grounded Claude Code session from Redis."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from claude_bridge.run_agent import assemble_claude_command


def cmd_resume(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="Resume the last Claude Code session recorded for this workspace (via Redis).",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Same workspace used for run_agent / sidecar (default: cwd)",
    )
    parser.add_argument(
        "--no-add-dir",
        action="store_true",
        help="Skip automatic --add-dir injection (match run_agent --no-add-dir)",
    )
    parser.add_argument(
        "forward",
        nargs="*",
        help="Extra args appended after claude -r <id> (e.g. extra flags)",
    )
    args, unknown = parser.parse_known_args(argv)
    forward = list(args.forward)
    if unknown:
        forward = unknown + forward

    ws = (args.workspace or Path.cwd()).expanduser().resolve()

    from claude_bridge.session_store import get_last_session_id

    sid = get_last_session_id(ws)
    if not sid:
        sys.stderr.write(
            "[octo resume] No session id in Redis for this workspace.\n"
            "Set REDIS_URL if needed, run a tracked Claude task with OCTO_RECORD_CLAUDE_SESSION=1,\n"
            "or complete a run_agent invocation that emits a session id on stderr.\n"
        )
        return 1

    from claude_bridge.resource_monitor import enforce_vram_guard_before_claude

    enforce_vram_guard_before_claude()

    resume_argv = ["-r", sid, *forward]
    cmd = assemble_claude_command(
        workspace=ws,
        claude_argv=resume_argv,
        no_add_dir=args.no_add_dir,
    )
    sys.stderr.write(f"[octo resume] Resuming session {sid[:16]}… ({len(resume_argv)} claude args)\n")
    os.execvp(cmd[0], cmd)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "resume":
        return cmd_resume(sys.argv[2:])
    sys.stderr.write("Usage: python -m claude_bridge.octo_cli resume [--workspace DIR] [--no-add-dir]\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
