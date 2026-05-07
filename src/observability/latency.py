"""PR latency metrics stored in SQLite (``logs/metrics.db``)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


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
