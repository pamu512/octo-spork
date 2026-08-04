"""PR latency metrics stored in SQLite (``logs/metrics.db``)."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

_LOG = logging.getLogger(__name__)


def _repo_root() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _metrics_db_path() -> Path:
    return _repo_root() / "logs" / "metrics.db"


def init_db() -> None:
    """Create ``logs/metrics.db`` and the ``metrics`` table if missing. Closes the connection when done."""
    path = _metrics_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY,
                pr_name TEXT,
                start_time REAL,
                end_time REAL,
                ttr_seconds REAL,
                success BOOLEAN
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_metric(
    *,
    pr_name: str,
    start_time: float,
    end_time: float,
    success: bool,
) -> None:
    """Insert one latency row. Ensures schema exists. Never raises to callers (logs on failure)."""

    ttr = max(0.0, float(end_time) - float(start_time))
    try:
        init_db()
        path = _metrics_db_path()
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(
                """
                INSERT INTO metrics (pr_name, start_time, end_time, ttr_seconds, success)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (pr_name or "")[:512],
                    float(start_time),
                    float(end_time),
                    ttr,
                    1 if success else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except OSError as exc:
        _LOG.debug("metrics.db write skipped: %s", exc)
