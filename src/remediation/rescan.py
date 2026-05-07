"""Trivy filesystem rescans for remediation verification loops."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

_LOG = logging.getLogger(__name__)


def run_trivy_scan(target_dir: str) -> dict:
    """Run ``trivy fs --format json`` against ``target_dir`` and return parsed JSON as a dict.

    Resolves ``target_dir`` to an absolute path. Uses the ``trivy`` executable from ``TRIVY_PATH``
    or :func:`shutil.which`. Captures stdout/stderr from :func:`subprocess.run`.

    On success, returns the object produced by :func:`json.loads` (Trivy's JSON report schema).

    If stdout is not valid JSON (for example Trivy crashed and printed a stack trace), catches
    :exc:`json.JSONDecodeError`, logs a warning, and returns a dictionary describing the failure
    that includes the raw stdout and stderr plus the subprocess return code—never raises.
    """
    root = Path(target_dir).expanduser().resolve()
    if not root.is_dir():
        return {
            "error": "not_a_directory",
            "path": str(root),
        }

    trivy_exe = os.environ.get("TRIVY_PATH") or shutil.which("trivy")
    if not trivy_exe:
        return {"error": "trivy_not_found"}

    cmd = [trivy_exe, "fs", "--format", "json", str(root)]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        _LOG.error("run_trivy_scan: failed to execute %s: %s", cmd, exc)
        return {
            "error": "subprocess_failed",
            "detail": str(exc),
        }

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        _LOG.warning(
            "trivy fs emitted non-JSON stdout (JSONDecodeError: %s); stderr=%r",
            exc,
            stderr[:2000],
        )
        return {
            "json_decode_error": True,
            "message": str(exc),
            "raw_stdout": stdout,
            "stderr": stderr,
            "returncode": completed.returncode,
        }

    if not isinstance(parsed, dict):
        _LOG.warning(
            "trivy fs JSON root is %s, expected object",
            type(parsed).__name__,
        )
        return {
            "json_decode_error": True,
            "message": "trivy JSON root is not an object",
            "raw_stdout": stdout,
            "stderr": stderr,
            "returncode": completed.returncode,
        }

    return parsed


def _cve_listed_in_payload(payload: object, needle_upper: str) -> bool:
    """Return ``True`` if any ``VulnerabilityID`` field equals ``needle_upper`` (already uppercased)."""

    if isinstance(payload, dict):
        raw_id = payload.get("VulnerabilityID")
        if isinstance(raw_id, str) and raw_id.strip().upper() == needle_upper:
            return True
        for child in payload.values():
            if _cve_listed_in_payload(child, needle_upper):
                return True
    elif isinstance(payload, list):
        for item in payload:
            if _cve_listed_in_payload(item, needle_upper):
                return True
    return False


def verify_cve_resolved(scan_results: dict, target_cve: str) -> bool:
    """Return ``True`` if ``target_cve`` does **not** appear under any ``VulnerabilityID`` entry.

    Walks the full Trivy JSON tree starting from ``scan_results``, focusing on the usual
    ``Results`` → ``Vulnerabilities`` → ``VulnerabilityID`` shape while still descending into every
    nested dict and list so identifiers are not missed if Trivy nests them differently.

    Missing or empty ``Results``, absent ``Vulnerabilities`` lists, or malformed branches are
    treated as having no matching vulnerability (returns ``True``) because traversal uses
    :meth:`dict.get` and isinstance checks—never ``dict['Results']`` indexing—so empty scans do not
    raise :exc:`KeyError`.
    """
    needle = target_cve.strip()
    if not needle:
        return True

    needle_upper = needle.upper()
    return not _cve_listed_in_payload(scan_results, needle_upper)
