"""Strict Claude Code tool policy + **elevate** / **restrict** for host-side agent containers.

Default tools (**read-only reconnaissance**): ``Read``, ``Grep``, ``Glob``.

Elevated session (**after terminal confirmation**): adds ``Edit`` (file edits; Claude Code's edit surface)
and ``Bash``. Maps user-facing “FileEdit” to ``Edit``.

Writes ``OCTO_CLAUDE_ALLOWED_TOOLS`` into ``<repo>/.local/claude_config/.env`` (host path) and
``docker restart`` the Claude agent so the Bun entrypoint reloads permissions.

Compose must **not** hard-code ``OCTO_CLAUDE_ALLOWED_TOOLS`` in ``environment:`` or it overrides the
mounted ``.env``. Policy defaults live in the entrypoint when the variable is unset.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from claude_bridge.model_sync import upsert_env_key

REPO_ROOT = Path(__file__).resolve().parents[2]

TOOLS_STRICT = "Read,Grep,Glob"
TOOLS_ELEVATED = "Read,Grep,Glob,Edit,Bash"

CLAUDE_CONFIG_ENV_KEY = "OCTO_CLAUDE_ALLOWED_TOOLS"
_DEFAULT_CONTAINER = "local-ai-claude-agent"


def default_claude_agent_env_path(repo_root: Path | None = None) -> Path:
    root = repo_root or REPO_ROOT
    override = (os.environ.get("CLAUDE_CONFIG_ENV_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (root / ".local" / "claude_config" / ".env").resolve()


def permission_prompt_elevate() -> bool:
    print(
        "\n*** Permission elevation ***\n"
        "Grant Claude Code **Edit** and **Bash** in addition to Read/Grep/Glob?\n"
        "This allows file modifications and shell commands until you run **restrict** "
        "and restart the agent (or reset the env key).\n",
        file=sys.stderr,
    )
    try:
        line = input("Type YES to elevate: ").strip()
    except EOFError:
        return False
    return line == "YES"


def permission_prompt_restrict() -> bool:
    print(
        "\n*** Restrict tools ***\n"
        f"Reset allowed tools to strict defaults ({TOOLS_STRICT})?\n",
        file=sys.stderr,
    )
    try:
        line = input("Type YES to restrict: ").strip()
    except EOFError:
        return False
    return line == "YES"


def docker_restart(container: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["docker", "restart", container],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError:
        return False, "docker CLI not found"
    except subprocess.TimeoutExpired:
        return False, "docker restart timed out"
    if proc.returncode == 0:
        return True, f"Restarted container {container!r}"
    err = (proc.stderr or proc.stdout or "").strip()
    return False, f"docker restart failed: {err}"


def cmd_elevate(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Elevate Claude agent tool permissions (Edit + Bash).")
    parser.add_argument("--repo", type=Path, default=None, help="Octo-spork repo root (default: inferred)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--container", default=os.environ.get("CLAUDE_AGENT_CONTAINER") or _DEFAULT_CONTAINER)
    args = parser.parse_args(argv)

    repo = (args.repo or REPO_ROOT).expanduser().resolve()
    env_file = default_claude_agent_env_path(repo)

    print(f"{CLAUDE_CONFIG_ENV_KEY}={TOOLS_ELEVATED}", file=sys.stderr)
    print(f"Env file: {env_file}", file=sys.stderr)
    if args.dry_run:
        print("Dry-run — no file write or docker restart.", file=sys.stderr)
        return 0

    if not permission_prompt_elevate():
        print("Aborted (no elevation).", file=sys.stderr)
        return 1

    upsert_env_key(env_file, CLAUDE_CONFIG_ENV_KEY, TOOLS_ELEVATED)
    ok, msg = docker_restart(args.container.strip())
    print(msg, file=sys.stderr)
    return 0 if ok else 1


def cmd_restrict(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore strict Claude agent tool permissions.")
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--container", default=os.environ.get("CLAUDE_AGENT_CONTAINER") or _DEFAULT_CONTAINER)
    args = parser.parse_args(argv)

    repo = (args.repo or REPO_ROOT).expanduser().resolve()
    env_file = default_claude_agent_env_path(repo)

    print(f"{CLAUDE_CONFIG_ENV_KEY}={TOOLS_STRICT}", file=sys.stderr)
    print(f"Env file: {env_file}", file=sys.stderr)
    if args.dry_run:
        print("Dry-run — no file write or docker restart.", file=sys.stderr)
        return 0

    if not permission_prompt_restrict():
        print("Aborted.", file=sys.stderr)
        return 1

    upsert_env_key(env_file, CLAUDE_CONFIG_ENV_KEY, TOOLS_STRICT)
    ok, msg = docker_restart(args.container.strip())
    print(msg, file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    av = sys.argv[1:]
    if not av:
        print("usage: python -m claude_bridge.permission_policy elevate|restrict [options]", file=sys.stderr)
        raise SystemExit(2)
    if av[0] == "elevate":
        raise SystemExit(cmd_elevate(av[1:]))
    if av[0] == "restrict":
        raise SystemExit(cmd_restrict(av[1:]))
    print(f"unknown command: {av[0]!r}", file=sys.stderr)
    raise SystemExit(2)
