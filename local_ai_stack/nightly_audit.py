"""Thin wrapper around ``scripts/nightly_audit.sh`` for cron / Hermes.

Propagates the script returncode. Missing script → 2.
Does not swallow failures (``|| true`` stays in the shell script, parked).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command_nightly_audit(env_file: Path, repo: str) -> int:
    """Run the unattended audit script (doctor/status/pre-push-scan) for cron or Hermes."""

    script = ROOT / "scripts" / "nightly_audit.sh"
    if not script.is_file():
        print(f"Error: missing {script}", file=sys.stderr, flush=True)
        return 2
    env = os.environ.copy()
    env["OCTO_SPORK_REPO_ROOT"] = str(ROOT)
    env["OCTO_ENV_FILE"] = str(Path(env_file).expanduser().resolve())
    env["OCTO_AUDIT_REPO"] = str(repo)
    print(f"+ {script} (OCTO_AUDIT_REPO={repo})", flush=True)
    completed = subprocess.run(
        ["bash", str(script)],
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    return int(completed.returncode)
