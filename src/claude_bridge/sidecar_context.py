"""Resolve Octo-spork **parent stack** paths for Claude Code ``--add-dir`` sidecar context.

When editing a child checkout or subfolder, walking upward may reach the Octo-spork monorepo root
(containing ``local_ai_stack``, ``deploy/local-ai``, etc.). That directory is injected so agents see
compose, patches, and stack conventions alongside the active workspace.

Override: set ``OCTO_SPORK_ROOT`` to an explicit Octo-spork root when the workspace is outside the
parent tree (e.g. standalone submodule clone).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def is_octo_spork_root(path: Path) -> bool:
    """Heuristic: directory looks like the octo-spork repository root."""
    p = path.resolve()
    return (
        (p / "local_ai_stack" / "__main__.py").is_file()
        and (p / "deploy" / "local-ai").is_dir()
        and (p / "patches" / "agenticseek").is_dir()
    )


def find_octo_spork_root(start: Path) -> Path | None:
    """Walk ``start`` and ancestors; return first directory that matches :func:`is_octo_spork_root`."""
    cur = start.expanduser().resolve()
    if not cur.exists():
        cur = cur.parent
    for candidate in [cur, *cur.parents]:
        try:
            if candidate.is_dir() and is_octo_spork_root(candidate):
                return candidate
        except OSError:
            continue
    return None


def octo_spork_root_for_workspace(workspace: Path) -> Path | None:
    """Resolve Octo-spork root for ``workspace`` (discovery or ``OCTO_SPORK_ROOT``)."""
    env_root = (os.environ.get("OCTO_SPORK_ROOT") or "").strip()
    if env_root:
        p = Path(env_root).expanduser().resolve()
        if is_octo_spork_root(p):
            return p
    return find_octo_spork_root(workspace)


def claude_add_dir_argv(workspace: Path) -> list[str]:
    """Return argv fragment ``["--add-dir", "<root>"]`` when a distinct Octo parent exists."""
    ws = workspace.expanduser().resolve()
    octo = octo_spork_root_for_workspace(ws)
    if octo is None:
        return []
    if ws.resolve() == octo.resolve():
        return []
    return ["--add-dir", str(octo)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit Claude Code extra argv for Octo-spork sidecar (--add-dir).",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Active repository / working tree (default: cwd)",
    )
    parser.add_argument(
        "--emit",
        choices=("json", "argv"),
        default="json",
        help="json: one JSON object; argv: NUL-separated tokens for scripting",
    )
    args = parser.parse_args(argv)

    extra = claude_add_dir_argv(args.workspace)
    if args.emit == "json":
        print(json.dumps({"extra": extra, "octo_root": extra[1] if len(extra) > 1 else None}))
    else:
        sys.stdout.write("\0".join(extra))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
