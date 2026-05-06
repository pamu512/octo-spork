"""Orchestrate patch apply + Trivy filesystem rescan to confirm CVE remediation."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .exceptions import VerificationFailedError
from .validator import PatchValidationResult, PatchValidator

_LOG = logging.getLogger(__name__)


def _collect_vuln_records_with_id(obj: Any, cve_id: str) -> list[dict[str, Any]]:
    """Depth-first collect dict nodes whose ``VulnerabilityID`` matches *cve_id* (case-insensitive)."""
    needle = (cve_id or "").strip().upper()
    if not needle:
        return []
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            vid = node.get("VulnerabilityID")
            if isinstance(vid, str) and vid.strip().upper() == needle:
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(obj)
    return found


def _run_trivy_fs_json(clone_root: Path, *, trivy_bin: str, timeout_sec: float) -> dict[str, Any]:
    proc = subprocess.run(
        [trivy_bin, "fs", "--format", "json", "--quiet", str(clone_root)],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
        cwd=str(clone_root),
    )
    raw_out = (proc.stdout or "").strip()
    raw_err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(
            f"trivy fs exited {proc.returncode}: {raw_err or raw_out or '(no output)'}",
        )
    if not raw_out:
        return {}
    try:
        data = json.loads(raw_out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"trivy JSON parse failed: {exc}: {raw_out[:2000]}") from exc
    return data if isinstance(data, dict) else {}


class RescanLoop:
    """After a patch applies cleanly, run ``trivy fs --format json`` on the clone and verify the CVE is gone.

    If the scan still reports the same **CVE-ID**, raises :exc:`VerificationFailedError` with a JSON
    snippet of the matching finding(s).
    """

    def __init__(
        self,
        patch_validator: PatchValidator,
        cve_id: str,
        *,
        trivy_executable: str | None = None,
        trivy_timeout_sec: float = 600.0,
    ) -> None:
        self._validator = patch_validator
        self._cve_id = (cve_id or "").strip()
        raw = trivy_executable if trivy_executable is not None else os.environ.get("TRIVY_PATH")
        self._trivy = (raw or "").strip() or shutil.which("trivy") or ""
        self._trivy_timeout = float(trivy_timeout_sec)

    def _verify_trivy(self, clone_root: Path) -> None:
        if not self._trivy:
            raise RuntimeError(
                "trivy executable not found; install Trivy or set TRIVY_PATH / PATH.",
            )
        if not self._cve_id:
            raise ValueError("cve_id must be non-empty for RescanLoop verification")

        data = _run_trivy_fs_json(clone_root, trivy_bin=self._trivy, timeout_sec=self._trivy_timeout)
        matches = _collect_vuln_records_with_id(data, self._cve_id)
        if matches:
            snippet = json.dumps(matches, indent=2, default=str)
            if len(snippet) > 12000:
                snippet = snippet[:11900] + "\n… [truncated]"
            raise VerificationFailedError(
                f"Trivy still reports {self._cve_id} after patch; remediation verification failed.",
                snippet=snippet,
            )

    def run(self, diff_text: str) -> PatchValidationResult:
        """Apply *diff_text* via :class:`PatchValidator`, then run the Trivy rescan when apply succeeds."""
        hook: Callable[[Path], None] = self._verify_trivy
        return self._validator.validate(diff_text, on_apply_success=hook)
