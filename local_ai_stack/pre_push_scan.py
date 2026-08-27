"""Pre-push scan: Trivy CRITICAL filesystem scan + optional infra health probes.

Fail-closed by default: a missing ``trivy`` binary is treated as a scan failure
unless the caller explicitly passes ``--skip-trivy``.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _lines_from_trivy_critical_report(report: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for result in report.get("Results") or []:
        if not isinstance(result, dict):
            continue
        target = str(result.get("Target") or "")
        for vuln in result.get("Vulnerabilities") or []:
            if not isinstance(vuln, dict):
                continue
            if str(vuln.get("Severity") or "").upper() != "CRITICAL":
                continue
            vid = str(vuln.get("VulnerabilityID") or vuln.get("ID") or "?")
            pkg = str(vuln.get("PkgName") or vuln.get("PkgID") or "?")
            title = str(vuln.get("Title") or "").strip()
            tail = f": {title}" if title else ""
            lines.append(f"[security] CRITICAL vuln {vid} in `{pkg}` ({target}){tail}")
        for mc in result.get("Misconfigurations") or []:
            if not isinstance(mc, dict):
                continue
            if str(mc.get("Severity") or "").upper() != "CRITICAL":
                continue
            mid = str(mc.get("ID") or "?")
            title = str(mc.get("Title") or "").strip()
            tail = f": {title}" if title else ""
            lines.append(f"[security] CRITICAL misconfiguration {mid} ({target}){tail}")
    return lines[:40]


def collect_trivy_critical_evidence(
    scan_root: Path, *, timeout: int = 180
) -> tuple[bool, list[str], bool]:
    """Return (critical_found, messages, skipped_no_binary).

    Mirrors grounded-review scope: CRITICAL filesystem vulns/misconfigs only.
    """
    exe = shutil.which("trivy")
    if not exe:
        return False, ["[octo-spork] Trivy not on PATH; Critical filesystem scan skipped."], True
    scan_root = scan_root.expanduser().resolve()
    if not scan_root.is_dir():
        return False, [f"[octo-spork] Scan root is not a directory: {scan_root}"], False
    cmd = [
        exe,
        "fs",
        "--severity",
        "CRITICAL",
        "--format",
        "json",
        "--quiet",
        str(scan_root),
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(scan_root),
            capture_output=True,
            text=True,
            timeout=max(30, int(timeout)),
            check=False,
        )
    except FileNotFoundError:
        return False, ["[octo-spork] Trivy executable vanished mid-invocation."], True
    except subprocess.TimeoutExpired:
        return True, [f"[octo-spork] Trivy timed out after {timeout}s"], False
    except OSError as exc:
        return True, [f"[octo-spork] Trivy OS error: {exc}"], False

    raw = (completed.stdout or "").strip()
    if not raw:
        err = (completed.stderr or "").strip()
        return True, [f"[octo-spork] Trivy produced empty stdout (exit {completed.returncode}): {err[:1500]}"], False

    try:
        report = json.loads(raw)
    except json.JSONDecodeError as exc:
        return True, [f"[octo-spork] Could not parse Trivy JSON: {exc}; stderr={(completed.stderr or '')[:800]}"], False

    if not isinstance(report, dict):
        return True, ["[octo-spork] Trivy JSON root was not an object."], False

    critical_lines = _lines_from_trivy_critical_report(report)
    if completed.returncode != 0:
        # ponytail: broken scan ≠ clean — empty/missing Results must not mask a nonzero exit.
        return True, critical_lines + [
            f"[octo-spork] Trivy exited {completed.returncode} (broken scan ≠ clean)."
        ], False
    if critical_lines:
        return True, critical_lines, False
    return False, [], False


def run_pre_push_scan(
    env_file: Path,
    repo_root: Path,
    *,
    skip_health: bool,
    skip_trivy: bool,
) -> int:
    """Return 0 when push should proceed, 1 when blocked.

    Fail-closed: if ``trivy`` is not on PATH the scan fails unless
    *skip_trivy* is ``True``.
    """
    failures: list[str] = []
    repo_root = repo_root.expanduser().resolve()

    if not skip_health:
        from local_ai_stack.__main__ import run_hook_infra_health_probe

        bad_health, health_logs = run_hook_infra_health_probe(env_file)
        if bad_health:
            failures.extend(health_logs)

    if not skip_trivy:
        critical, tri_logs, skipped = collect_trivy_critical_evidence(repo_root)
        if skipped:
            # ponytail: fail-closed — missing binary cannot masquerade as a clean scan.
            failures.append(
                "[security] `trivy` not found on PATH. "
                "Install Trivy or pass --skip-trivy to suppress this check."
            )
        elif critical:
            failures.extend(tri_logs)

    for msg in failures:
        print(msg, file=sys.stderr)
    if failures:
        print(
            "\n[octo-spork] pre-push scan failed: resolve infra health issues and/or CRITICAL Trivy findings.\n",
            file=sys.stderr,
        )
        return 1
    return 0


def command_pre_push_scan(
    env_file: Path,
    repo: str,
    skip_health: bool,
    skip_trivy: bool,
) -> None:
    from local_ai_stack.__main__ import _repo_path_from_arg

    code = run_pre_push_scan(
        env_file,
        _repo_path_from_arg(repo),
        skip_health=skip_health,
        skip_trivy=skip_trivy,
    )
    if code != 0:
        raise RuntimeError("Octo-spork pre-push scan did not pass.")


def command_install_hook(repo: str, env_file: Path, *, force: bool) -> None:
    """Write a pre-push hook that runs ``pre-push-scan`` for this worktree."""
    from local_ai_stack.__main__ import ROOT, _print, _repo_path_from_arg

    repo_path = _repo_path_from_arg(repo)
    git_dir = repo_path / ".git"
    if not git_dir.is_dir():
        raise RuntimeError(f"Not a git repository: {repo_path}")
    hook_path = git_dir / "hooks" / "pre-push"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    if hook_path.exists() and not force:
        raise RuntimeError(
            f"{hook_path} already exists. Re-run with --force to overwrite, or remove the file first."
        )

    env_resolved = env_file.resolve()
    py = sys.executable
    octo = str(ROOT)
    q_octo = shlex.quote(octo)
    q_py = shlex.quote(py)
    q_env = shlex.quote(str(env_resolved))
    body = f"""#!/usr/bin/env bash
# Generated by: python -m local_ai_stack install-hook
# Octo-spork pre-push: lightweight stack health (verify-style) + Trivy CRITICAL on the repo root.
# Fail-closed: missing trivy binary blocks the push. Pass --skip-trivy to override.
set -euo pipefail
OCTO_ROOT={q_octo}
PYTHON={q_py}
ENV_FILE={q_env}
export PYTHONPATH="${{OCTO_ROOT}}${{PYTHONPATH:+:$PYTHONPATH}}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
echo "[octo-spork] pre-push scan (repo: $REPO_ROOT)…" >&2
exec "$PYTHON" -m local_ai_stack pre-push-scan --env-file "$ENV_FILE" --repo "$REPO_ROOT"
"""
    hook_path.write_text(body, encoding="utf-8")
    try:
        hook_path.chmod(0o755)
    except OSError as exc:
        raise RuntimeError(f"Failed to make pre-push hook executable: {hook_path}: {exc}") from exc
    if not os.access(hook_path, os.X_OK):
        raise RuntimeError(f"Pre-push hook is not executable: {hook_path}")
    _print(f"Wrote {hook_path}")
