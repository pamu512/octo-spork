"""Python Claude launcher: injects Octo-spork ``--add-dir`` sidecar context, then execs ``claude``.

Subcommands (before ``--``)::

    PYTHONPATH=src python -m claude_bridge.run_agent elevate [--repo ROOT] [--dry-run]
    PYTHONPATH=src python -m claude_bridge.run_agent restrict [--repo ROOT] [--dry-run]

Interactive confirmation updates ``.local/claude_config/.env`` and restarts the Claude agent container.

Example::

    PYTHONPATH=src python -m claude_bridge.run_agent --workspace /path/to/child -- -- -p \"Review submodule\"

Everything after ``--`` is forwarded to ``claude`` unchanged (after ``--add-dir`` insertion).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from claude_bridge.sidecar_context import claude_add_dir_argv


def assemble_claude_command(
    *,
    workspace: Path,
    claude_argv: list[str],
    no_add_dir: bool = False,
) -> list[str]:
    """Build ``claude`` argv with Octo allowlist and ``--add-dir`` sidecar (same as :func:`main`)."""
    extra = [] if no_add_dir else claude_add_dir_argv(workspace)
    allowed: list[str] = []
    if os.environ.get("OCTO_SKIP_ALLOWED_TOOLS", "").strip() != "1":
        spec = (os.environ.get("OCTO_CLAUDE_ALLOWED_TOOLS") or "Read,Grep,Glob").strip()
        tools = [t.strip() for t in spec.split(",") if t.strip()]
        if tools:
            allowed = ["--allowedTools", ",".join(tools)]
    return ["claude", *allowed, *extra, *claude_argv]


def main() -> None:
    raw = sys.argv[1:]
    if raw and raw[0] == "elevate":
        from claude_bridge.permission_policy import cmd_elevate

        raise SystemExit(cmd_elevate(raw[1:]))
    if raw and raw[0] == "restrict":
        from claude_bridge.permission_policy import cmd_restrict

        raise SystemExit(cmd_restrict(raw[1:]))
    if "--" in raw:
        idx = raw.index("--")
        pre, post = raw[:idx], raw[idx + 1 :]
    else:
        pre, post = raw, []

    parser = argparse.ArgumentParser(
        description="Exec claude with Octo-spork parent stack added via --add-dir.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Working tree used for sidecar discovery (default: cwd)",
    )
    parser.add_argument(
        "--no-add-dir",
        action="store_true",
        help="Skip automatic --add-dir injection",
    )
    args = parser.parse_args(pre)

    ws = (args.workspace or Path.cwd()).expanduser().resolve()

    from claude_bridge.resource_monitor import enforce_vram_guard_before_claude

    enforce_vram_guard_before_claude()

    cmd = assemble_claude_command(workspace=ws, claude_argv=post, no_add_dir=args.no_add_dir)

    from claude_bridge.session_store import (
        record_session_enabled,
        run_claude_capture_and_record,
        run_claude_relay_stderr_and_record,
        should_use_capture_mode,
    )

    if record_session_enabled():
        if should_use_capture_mode(post):
            raise SystemExit(run_claude_capture_and_record(cmd, workspace=ws))
        raise SystemExit(run_claude_relay_stderr_and_record(cmd, workspace=ws))

    os.execvp("claude", cmd)


if __name__ == "__main__":
    main()
