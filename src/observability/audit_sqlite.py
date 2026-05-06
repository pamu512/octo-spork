"""SQLite audit trail for agent thoughts, actions, and decision-tree snapshots.

Database path: ``logs/audit.db`` (override with ``OCTO_AUDIT_DB``).

Bind the active session with :func:`set_audit_session` or env ``OCTO_AUDIT_SESSION_ID``, then record
events. Optionally call :func:`update_decision_tree` after each planner step with the full JSON tree.

See :func:`export_summary` for a Markdown narrative with correction / mind-change highlights.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

_DB_LOCK = threading.Lock()
_local = threading.local()

Kind = Literal["thought", "action", "system"]

_DEFAULT_DB = Path("logs/audit.db")


def audit_db_path() -> Path:
    raw = (os.environ.get("OCTO_AUDIT_DB") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / _DEFAULT_DB).resolve()


def set_audit_session(session_id: str | None) -> None:
    """Thread-local active session for mirrored trace rows (``session_id`` or ``None`` to clear)."""
    if session_id:
        _local.session_id = session_id.strip()
    else:
        _local.session_id = None


def get_audit_session() -> str | None:
    sid = getattr(_local, "session_id", None)
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    env = (os.environ.get("OCTO_AUDIT_SESSION_ID") or "").strip()
    return env or None


def _connect() -> sqlite3.Connection:
    path = audit_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit_sessions (
            id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            decision_tree_json TEXT NOT NULL DEFAULT '{}',
            meta_json TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            parent_event_id INTEGER,
            FOREIGN KEY (session_id) REFERENCES audit_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_event_id) REFERENCES audit_events(id) ON DELETE SET NULL,
            UNIQUE(session_id, seq)
        );

        CREATE INDEX IF NOT EXISTS idx_audit_events_session_seq
            ON audit_events(session_id, seq);
        CREATE INDEX IF NOT EXISTS idx_audit_events_kind ON audit_events(session_id, kind);
        """
    )
    conn.commit()


def init_db() -> None:
    with _DB_LOCK:
        conn = _connect()
        try:
            init_schema(conn)
        finally:
            conn.close()


def start_session(*, session_id: str | None = None, meta: dict[str, Any] | None = None) -> str:
    """Create a session row; return ``session_id`` (new UUID if omitted)."""
    sid = session_id or str(uuid.uuid4())
    now = time.time()
    meta_json = json.dumps(meta or {}, ensure_ascii=False, default=str)
    with _DB_LOCK:
        conn = _connect()
        try:
            init_schema(conn)
            conn.execute(
                """
                INSERT INTO audit_sessions (id, created_at, updated_at, decision_tree_json, meta_json)
                VALUES (?, ?, ?, '{}', ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    meta_json = COALESCE(excluded.meta_json, audit_sessions.meta_json)
                """,
                (sid, now, now, meta_json),
            )
            conn.commit()
        finally:
            conn.close()
    set_audit_session(sid)
    return sid


def _next_seq(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM audit_events WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row[0]) if row else 1


def record_event(
    session_id: str,
    kind: Kind,
    payload: dict[str, Any],
    *,
    parent_event_id: int | None = None,
) -> int:
    """Append one thought/action/system event; returns ``event`` row id."""
    now = time.time()
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    with _DB_LOCK:
        conn = _connect()
        try:
            init_schema(conn)
            conn.execute(
                "INSERT INTO audit_sessions (id, created_at, updated_at, decision_tree_json, meta_json) "
                "VALUES (?, ?, ?, '{}', NULL) ON CONFLICT(id) DO NOTHING",
                (session_id, now, now),
            )
            seq = _next_seq(conn, session_id)
            cur = conn.execute(
                """
                INSERT INTO audit_events (session_id, seq, kind, payload_json, parent_event_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, seq, kind, payload_json, parent_event_id),
            )
            conn.execute(
                "UPDATE audit_sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()


def record_thought(session_id: str, payload: dict[str, Any], **kw: Any) -> int:
    return record_event(session_id, "thought", payload, **kw)


def record_action(session_id: str, payload: dict[str, Any], **kw: Any) -> int:
    return record_event(session_id, "action", payload, **kw)


def update_decision_tree(session_id: str, tree: dict[str, Any]) -> None:
    """Persist the full JSON decision tree for this session (overwrites previous snapshot)."""
    now = time.time()
    blob = json.dumps(tree, ensure_ascii=False, default=str)
    with _DB_LOCK:
        conn = _connect()
        try:
            init_schema(conn)
            conn.execute(
                "INSERT INTO audit_sessions (id, created_at, updated_at, decision_tree_json, meta_json) "
                "VALUES (?, ?, ?, ?, NULL) ON CONFLICT(id) DO UPDATE SET "
                "updated_at = excluded.updated_at, decision_tree_json = excluded.decision_tree_json",
                (session_id, now, now, blob),
            )
            conn.commit()
        finally:
            conn.close()


def mirror_from_trace_record(record: dict[str, Any]) -> None:
    """Map a dashboard/trace JSON line into audit rows (needs :func:`get_audit_session`)."""
    if os.environ.get("OCTO_AUDIT_SQLITE", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    sid = get_audit_session()
    if not sid:
        return
    kind_raw = str(record.get("kind") or "").lower()
    if kind_raw in {"llm"}:
        record_event(sid, "thought", dict(record))
    elif kind_raw in {"tool"}:
        record_event(sid, "action", dict(record))
    elif kind_raw in {"control", "dashboard"}:
        record_event(sid, "system", dict(record))
    else:
        record_event(sid, "system", dict(record))


_MIND_CHANGE_RE = re.compile(
    r"\b(actually|instead|on second thought|revising|changed my mind|better approach|wait,?)\b",
    re.I,
)
_CORRECTION_RE = re.compile(
    r"\b(incorrect|mistake|wrong|i erred|failed because|error was|fixed|correction|apologies)\b",
    re.I,
)


def _thought_text(payload: dict[str, Any]) -> str:
    for key in ("thought", "preview", "message", "content", "text"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return json.dumps(payload, ensure_ascii=False, default=str)[:8000]


def _analyze_timeline(rows: list[sqlite3.Row]) -> list[tuple[int, str, str]]:
    """Return list of (event_id, category, one_line_summary)."""
    highlights: list[tuple[int, str, str]] = []
    prev_thought: str | None = None
    prev_tool_err: str | None = None
    prev_tool_name: str | None = None

    for row in rows:
        eid = int(row["id"])
        kind = row["kind"]
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = {}
        if kind == "thought":
            text = _thought_text(payload)
            if prev_thought and _MIND_CHANGE_RE.search(text):
                highlights.append((eid, "mind_change", "Language suggests revising an earlier plan."))
            if prev_thought and _CORRECTION_RE.search(text) and not _CORRECTION_RE.search(prev_thought):
                highlights.append((eid, "correction", "Possible correction or acknowledgment of error."))
            prev_thought = text
        elif kind == "action":
            tool = str(payload.get("tool") or payload.get("kind") or "action")
            err = payload.get("error")
            if err:
                prev_tool_err = str(err)
                prev_tool_name = tool
            elif prev_tool_err and prev_tool_name == tool:
                highlights.append((eid, "recovery", "Action repeated after prior failure — likely retry/recovery."))
                prev_tool_err = None
            else:
                prev_tool_err = None
    return highlights


def export_summary(session_id: str) -> str:
    """Markdown report: timeline, decision tree, and heuristic highlights."""
    init_db()
    with _DB_LOCK:
        conn = _connect()
        try:
            sess = conn.execute(
                "SELECT * FROM audit_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not sess:
                return f"# Audit summary\n\n_No session `{session_id}` found._\n"
            events = conn.execute(
                "SELECT * FROM audit_events WHERE session_id = ? ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()

    lines: list[str] = []
    lines.append("# Agent audit summary")
    lines.append("")
    lines.append(f"- **Session:** `{session_id}`")
    lines.append(f"- **Created:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(sess['created_at']))}")
    lines.append(f"- **Updated:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(sess['updated_at']))}")
    meta = sess["meta_json"]
    if meta:
        lines.append(f"- **Meta:** `{meta}`")
    lines.append("")

    tree_raw = sess["decision_tree_json"] or "{}"
    lines.append("## Decision tree (latest snapshot)")
    lines.append("")
    try:
        tree_obj = json.loads(tree_raw)
        lines.append("```json")
        lines.append(json.dumps(tree_obj, indent=2, ensure_ascii=False, default=str)[:120000])
        lines.append("```")
    except json.JSONDecodeError:
        lines.append(f"_Could not parse stored tree ({len(tree_raw)} chars)._")
    lines.append("")

    lines.append("## Timeline")
    lines.append("")
    lines.append("| # | kind | summary |")
    lines.append("|---|------|---------|")
    for row in events:
        seq = row["seq"]
        kind = row["kind"]
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = {}
        summ = _thought_text(payload) if kind == "thought" else json.dumps(payload, default=str)[:180]
        summ = summ.replace("|", "\\|").replace("\n", " ")[:200]
        lines.append(f"| {seq} | {kind} | {summ} |")
    lines.append("")

    hl = _analyze_timeline(list(events))
    lines.append("## Highlights (mind-change & correction heuristics)")
    lines.append("")
    if not hl:
        lines.append("_No strong heuristic matches (tighten instrumentation or add explicit flags in payloads)._")
    else:
        seen: set[tuple[int, str]] = set()
        for eid, cat, note in hl:
            key = (eid, cat)
            if key in seen:
                continue
            seen.add(key)
            label = {
                "mind_change": "Possible change of mind",
                "correction": "Possible correction",
                "recovery": "Retry after failure",
            }.get(cat, cat)
            lines.append(f"- **Event `{eid}` — {label}:** {note}")
    lines.append("")
    lines.append("---")
    lines.append("_Generated by `observability.audit_sqlite.export_summary`._")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "audit_db_path",
    "export_summary",
    "get_audit_session",
    "init_db",
    "mirror_from_trace_record",
    "record_action",
    "record_event",
    "record_thought",
    "set_audit_session",
    "start_session",
    "update_decision_tree",
]
