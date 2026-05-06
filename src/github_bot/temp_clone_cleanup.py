"""Hourly maintenance: remove stale directories under ``.temp_clones`` (ephemeral git clones)."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)


def resolve_temp_clones_dir() -> Path:
    """Directory scanned for subfolders to prune (default: ``./.temp_clones`` under cwd)."""
    raw = (os.environ.get("OCTO_SPORK_TEMP_CLONES_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / ".temp_clones").resolve()


def cleanup_stale_temp_clones(
    base_dir: Path | None = None,
    *,
    max_age_seconds: float | None = None,
) -> dict[str, Any]:
    """Delete immediate child **directories** of ``base_dir`` older than ``max_age_seconds``.

    Uses :func:`shutil.rmtree` with explicit handling for permission errors on Linux/macOS/Windows
    (``PermissionError`` / ``EACCES`` / ``EPERM``) so one stuck clone does not abort the sweep.

    Returns counts under keys ``removed``, ``skipped_young``, ``errors``.
    """
    base = base_dir if base_dir is not None else resolve_temp_clones_dir()
    if max_age_seconds is None:
        max_age_seconds = float(os.environ.get("OCTO_SPORK_TEMP_CLONE_MAX_AGE_SEC", str(2 * 3600)))

    removed = 0
    skipped_young = 0
    errors = 0

    if not base.is_dir():
        return {
            "base": str(base),
            "removed": 0,
            "skipped_young": 0,
            "errors": 0,
            "note": "base_missing_or_not_dir",
        }

    now = time.time()
    try:
        entries = list(base.iterdir())
    except OSError as exc:
        _LOG.warning("Cannot list temp clones dir %s: %s", base, exc)
        return {
            "base": str(base),
            "removed": 0,
            "skipped_young": 0,
            "errors": 1,
            "note": f"list_failed:{exc}",
        }

    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            st = entry.stat()
            mtime = float(st.st_mtime)
        except OSError as exc:
            _LOG.warning("Cannot stat %s: %s", entry, exc)
            errors += 1
            continue

        age_sec = now - mtime
        if age_sec <= max_age_seconds:
            skipped_young += 1
            continue

        try:
            shutil.rmtree(entry)
            removed += 1
            _LOG.info(
                "Removed stale temp clone dir (age %.0fs): %s",
                age_sec,
                entry,
            )
        except PermissionError as exc:
            _LOG.warning(
                "Permission denied removing temp clone %s (%s); skipping — disk may need manual cleanup",
                entry,
                exc,
            )
            errors += 1
        except OSError as exc:
            en = getattr(exc, "errno", None)
            if en in {errno.EACCES, errno.EPERM}:
                _LOG.warning(
                    "Permission error removing temp clone %s (errno=%s): %s",
                    entry,
                    en,
                    exc,
                )
                errors += 1
            else:
                _LOG.warning("Failed to remove temp clone %s: %s", entry, exc)
                errors += 1

    return {
        "base": str(base),
        "max_age_seconds": max_age_seconds,
        "removed": removed,
        "skipped_young": skipped_young,
        "errors": errors,
    }
