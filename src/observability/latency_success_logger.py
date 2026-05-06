"""Latency & success logging for remediation: **Scan Start** → **Verified Patch** (local SQLite)."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

_LOCK = threading.Lock()

# TTR above this (seconds) triggers context reset (KV-style cache clear + aggressive prune hint).
_DEFAULT_SLOW_TTR_SEC = 300.0

_STATE_NAME = "remediation_context_reset_state.json"


def _repo_root() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def latency_log_db_path() -> Path:
    override = (os.environ.get("OCTO_LATENCY_SUCCESS_DB") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _repo_root() / ".local" / "latency_success.db"


def _connect() -> sqlite3.Connection:
    path = latency_log_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remediation_latency (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_utc TEXT NOT NULL,
            scan_start_unix REAL NOT NULL,
            event_end_unix REAL NOT NULL,
            ttr_seconds REAL NOT NULL,
            success_verified_patch INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            pr_html_url TEXT,
            cve_id TEXT,
            extra_json TEXT
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_remediation_latency_created "
        "ON remediation_latency(created_at_utc);"
    )
    conn.commit()


@dataclass
class RemediationLatencyRow:
    created_at_utc: str
    scan_start_unix: float
    event_end_unix: float
    ttr_seconds: float
    success_verified_patch: bool
    outcome: str
    pr_html_url: str
    cve_id: str
    extra: dict[str, Any] = field(default_factory=dict)


def log_remediation_latency(
    row: RemediationLatencyRow,
) -> int:
    """Insert a row; returns row id."""
    payload = asdict(row)
    extra = payload.pop("extra")
    success = payload.pop("success_verified_patch")
    with _LOCK:
        conn = _connect()
        try:
            init_schema(conn)
            cur = conn.execute(
                """
                INSERT INTO remediation_latency (
                    created_at_utc, scan_start_unix, event_end_unix, ttr_seconds,
                    success_verified_patch, outcome, pr_html_url, cve_id, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["created_at_utc"],
                    payload["scan_start_unix"],
                    payload["event_end_unix"],
                    payload["ttr_seconds"],
                    1 if success else 0,
                    payload["outcome"],
                    payload["pr_html_url"],
                    payload["cve_id"],
                    json.dumps(extra, default=str) if extra else None,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()


def slow_ttr_threshold_sec() -> float:
    raw = (os.environ.get("OCTO_REMEDIATION_SLOW_TTR_SEC") or "").strip()
    if not raw:
        return _DEFAULT_SLOW_TTR_SEC
    try:
        return max(30.0, float(raw))
    except ValueError:
        return _DEFAULT_SLOW_TTR_SEC


def _ollama_base_url() -> str:
    return (
        (os.environ.get("OLLAMA_LOCAL_URL") or "").strip()
        or (os.environ.get("OLLAMA_BASE_URL") or "").strip()
        or "http://127.0.0.1:11434"
    ).rstrip("/")


def clear_ollama_runtime_cache() -> list[str]:
    """
    Best-effort unload of resident Ollama models (clears weight + KV cache in the running server).
    """
    try:
        from infra.resource_manager import VRAMManager

        mgr = VRAMManager(ollama_base_url=_ollama_base_url())
        return mgr.clear_cache(ollama_base_url=_ollama_base_url())
    except Exception as exc:
        _LOG.warning("latency_success_logger: Ollama cache clear failed: %s", exc)
        return []


def _state_path(repo_root: Path | None = None) -> Path:
    root = repo_root or _repo_root()
    return (root / ".local" / _STATE_NAME).resolve()


def set_aggressive_pruning_active(
    *,
    repo_root: Path | None = None,
    reason: str,
    ttl_sec: float | None = None,
) -> None:
    """Persist aggressive remediation context pruning for subsequent runs."""
    raw_ttl = (os.environ.get("OCTO_AGGRESSIVE_PRUNE_TTL_SEC") or "").strip()
    try:
        ttl = float(raw_ttl) if raw_ttl else (ttl_sec if ttl_sec is not None else 7200.0)
    except ValueError:
        ttl = 7200.0
    until = time.time() + max(60.0, ttl)
    path = _state_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "active": True,
        "reason": reason,
        "until_unix": until,
        "set_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _LOG.warning("Aggressive context pruning enabled until %s (%s)", until, reason)


def aggressive_prune_effective_max_chars(default_max: int, *, repo_root: Path | None = None) -> int:
    """If a context-reset flag is active, cap brief size (more aggressive pruning)."""
    path = _state_path(repo_root)
    if not path.is_file():
        return default_max
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_max
    if not data.get("active"):
        return default_max
    try:
        until = float(data.get("until_unix") or 0.0)
    except (TypeError, ValueError):
        until = 0.0
    if time.time() > until:
        try:
            path.unlink()
        except OSError:
            pass
        return default_max
    cap_raw = (os.environ.get("OCTO_FIX_CONTEXT_AGGRESSIVE_MAX_CHARS") or "").strip()
    try:
        cap = int(cap_raw) if cap_raw else max(8_000, default_max // 2)
    except ValueError:
        cap = max(8_000, default_max // 2)
    return min(default_max, cap)


def trigger_context_reset(
    *,
    ttr_seconds: float,
    repo_root: Path | None = None,
    reason: str = "slow_ttr",
) -> None:
    """
    **Context reset:** unload Ollama residents (KV/weights) and enable aggressive PR brief pruning
    for the next remediation run.
    """
    if _truthy("OCTO_CONTEXT_RESET_DISABLE"):
        _LOG.info("Context reset skipped (OCTO_CONTEXT_RESET_DISABLE=1)")
        return
    unloaded = clear_ollama_runtime_cache()
    _LOG.warning(
        "Context reset: TTR=%.1fs — unloaded Ollama models: %s",
        ttr_seconds,
        unloaded or "(none or unavailable)",
    )
    set_aggressive_pruning_active(
        repo_root=repo_root,
        reason=f"{reason}:ttr={ttr_seconds:.1f}s",
    )


def maybe_trigger_context_reset_for_ttr(
    *,
    ttr_seconds: float,
    success_verified_patch: bool,
    repo_root: Path | None = None,
) -> None:
    """If TTR exceeds the slow threshold, run :func:`trigger_context_reset`."""
    if ttr_seconds <= slow_ttr_threshold_sec():
        return
    if not success_verified_patch:
        return
    trigger_context_reset(ttr_seconds=ttr_seconds, repo_root=repo_root, reason="verified_patch_slow_ttr")


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
