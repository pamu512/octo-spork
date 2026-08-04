"""Summaries of nightly audit logs and ``logs/metrics.db`` for schedulers / Hermes."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def logs_dir() -> Path:
    return _repo_root() / "logs"


def latest_nightly_audit_log() -> Path | None:
    root = logs_dir()
    if not root.is_dir():
        return None
    files = sorted(root.glob("nightly_audit_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _tail_text(path: Path, *, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def metrics_summary(*, limit: int = 20) -> dict[str, Any]:
    """Aggregate recent rows from ``logs/metrics.db`` (empty dict fields if missing)."""

    db = logs_dir() / "metrics.db"
    out: dict[str, Any] = {
        "db_path": str(db),
        "exists": db.is_file(),
        "count": 0,
        "success_count": 0,
        "avg_ttr_seconds": None,
        "recent": [],
    }
    if not db.is_file():
        return out
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            total = conn.execute("SELECT COUNT(*) AS c FROM metrics").fetchone()
            out["count"] = int(total["c"]) if total else 0
            ok = conn.execute(
                "SELECT COUNT(*) AS c FROM metrics WHERE success IN (1, '1', 'true', 'True')"
            ).fetchone()
            out["success_count"] = int(ok["c"]) if ok else 0
            avg = conn.execute("SELECT AVG(ttr_seconds) AS a FROM metrics").fetchone()
            if avg and avg["a"] is not None:
                out["avg_ttr_seconds"] = float(avg["a"])
            rows = conn.execute(
                """
                SELECT pr_name, start_time, end_time, ttr_seconds, success
                FROM metrics
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
            out["recent"] = [dict(r) for r in rows]
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return out
    return out


def audit_status_payload(*, log_tail_chars: int = 4000, metrics_limit: int = 20) -> dict[str, Any]:
    """JSON-serializable status for GET ``/octo/audit/status``."""

    latest = latest_nightly_audit_log()
    log_block: dict[str, Any] = {
        "path": str(latest) if latest else None,
        "mtime": latest.stat().st_mtime if latest else None,
        "tail": _tail_text(latest, max_chars=log_tail_chars) if latest else "",
    }
    return {
        "repo_root": str(_repo_root()),
        "nightly_audit": log_block,
        "metrics": metrics_summary(limit=metrics_limit),
    }
