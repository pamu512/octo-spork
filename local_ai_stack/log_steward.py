"""LogSteward: on stack shutdown, roll up ``logs/`` by day, gzip large files, prune old archives."""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

SIZE_THRESHOLD_BYTES = 10 * 1024 * 1024
ARCHIVE_MAX_AGE_SECONDS = 7 * 24 * 3600
ISO_DATE_IN_NAME = re.compile(r"(20\d{2}-\d{2}-\d{2})")


def _truthy_disable() -> bool:
    return os.environ.get("OCTO_LOG_STEWARD", "").strip().lower() in {"0", "false", "no", "off"}


def _announce(fn: Callable[[str], None] | None, msg: str) -> None:
    if fn is not None:
        fn(msg)
    else:
        _LOG.info("%s", msg)


def _iter_merge_source_files(logs_dir: Path) -> Iterator[Path]:
    """Plain log files under *logs_dir*, excluding steward workspace and ``*.gz``."""
    if not logs_dir.is_dir():
        return
    for path in logs_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(logs_dir)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == ".steward":
            continue
        if path.name.endswith(".gz"):
            continue
        yield path


def _date_key_for_file(path: Path) -> str:
    """Prefer ISO date embedded in filename; else UTC calendar date from mtime."""
    m = ISO_DATE_IN_NAME.search(path.name)
    if m:
        return m.group(1)
    try:
        ts = path.stat().st_mtime
    except OSError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _concatenate_logs_by_date(logs_dir: Path, announce: Callable[[str], None] | None) -> int:
    """Merge multiple same-day logs into ``logs/.steward/daily/<date>-combined.log``; return files removed."""
    buckets: dict[str, list[Path]] = defaultdict(list)
    for path in _iter_merge_source_files(logs_dir):
        buckets[_date_key_for_file(path)].append(path)

    out_dir = logs_dir / ".steward" / "daily"
    removed = 0
    for date_key, paths in buckets.items():
        if len(paths) < 2:
            continue
        paths.sort(key=lambda p: str(p))
        out_dir.mkdir(parents=True, exist_ok=True)
        combined = out_dir / f"{date_key}-combined.log"
        header = (
            f"\n\n{'=' * 72}\n"
            f"LogSteward merged {len(paths)} files for {date_key}\n"
            f"{'=' * 72}\n"
        )
        try:
            is_new_file = not combined.exists()
            with combined.open("a", encoding="utf-8", errors="replace") as out:
                if is_new_file:
                    out.write(f"# LogSteward daily rollup — {date_key}\n")
                out.write(header)
                for src in paths:
                    out.write(f"\n--- source: {src.relative_to(logs_dir)} ---\n")
                    try:
                        body = src.read_text(encoding="utf-8", errors="replace")
                    except OSError as exc:
                        out.write(f"<read error: {exc}>\n")
                        continue
                    out.write(body)
                    if not body.endswith("\n"):
                        out.write("\n")
            for src in paths:
                try:
                    src.unlink()
                    removed += 1
                except OSError as exc:
                    _announce(announce, f"LogSteward: could not remove merged source {src}: {exc}")
        except OSError as exc:
            _announce(announce, f"LogSteward: combine failed for {date_key}: {exc}")
    if removed:
        _announce(announce, f"LogSteward: merged and removed {removed} log file(s) into logs/.steward/daily/")
    return removed


def _compress_large_logs(logs_dir: Path, min_bytes: int, announce: Callable[[str], None] | None) -> int:
    """Gzip regular files larger than *min_bytes*; replace with ``*.gz``."""
    count = 0
    for path in logs_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".gz":
            continue
        try:
            sz = path.stat().st_size
        except OSError:
            continue
        if sz <= min_bytes:
            continue
        gz_path = path.with_name(path.name + ".gz")
        try:
            with path.open("rb") as raw:
                with gzip.open(gz_path, "wb", compresslevel=9) as gz:
                    shutil.copyfileobj(raw, gz)
            path.unlink()
            count += 1
        except OSError as exc:
            _announce(announce, f"LogSteward: gzip failed for {path}: {exc}")
    if count:
        _announce(announce, f"LogSteward: compressed {count} log file(s) > {min_bytes // (1024 * 1024)} MiB")
    return count


def _purge_old_gz_archives(logs_dir: Path, max_age_seconds: float, announce: Callable[[str], None] | None) -> int:
    """Delete ``*.gz`` under *logs_dir* with mtime older than *max_age_seconds*."""
    now = time.time()
    cutoff = now - max_age_seconds
    removed = 0
    for path in logs_dir.rglob("*.gz"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError as exc:
            _announce(announce, f"LogSteward: could not delete old archive {path}: {exc}")
    if removed:
        _announce(announce, f"LogSteward: removed {removed} gzip archive(s) older than 7 days")
    return removed


def run_log_steward(repo_root: Path, *, announce: Callable[[str], None] | None = None) -> dict[str, Any]:
    """
    Maintain ``logs/``: daily merge, gzip large logs, drop stale ``*.gz``.

    Set ``OCTO_LOG_STEWARD=0`` to disable.
    """
    if _truthy_disable():
        _announce(announce, "LogSteward: skipped (OCTO_LOG_STEWARD disables).")
        return {"skipped": True}

    logs_dir = (repo_root / "logs").resolve()
    if not logs_dir.is_dir():
        _announce(announce, f"LogSteward: no {logs_dir} directory — nothing to do.")
        return {"skipped": True, "reason": "no logs dir"}

    summary: dict[str, Any] = {"logs_dir": str(logs_dir)}
    try:
        merged_removed = _concatenate_logs_by_date(logs_dir, announce)
        summary["merged_sources_removed"] = merged_removed
        gzipped = _compress_large_logs(logs_dir, SIZE_THRESHOLD_BYTES, announce)
        summary["gzipped"] = gzipped
        purged = _purge_old_gz_archives(logs_dir, ARCHIVE_MAX_AGE_SECONDS, announce)
        summary["purged_archives"] = purged
    except OSError as exc:
        _announce(announce, f"LogSteward: error: {exc}")
        summary["error"] = str(exc)
    return summary
