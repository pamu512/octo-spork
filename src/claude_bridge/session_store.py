"""Redis-backed store for the last Claude Code session ID per workspace (resume / grounded continuity)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
KEY_PREFIX = os.environ.get("OCTO_SESSION_REDIS_PREFIX", "octo-spork:claude:last_session")
TTL_SEC = int(os.environ.get("OCTO_SESSION_REDIS_TTL_SEC", str(28 * 24 * 3600)))

_UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)


def workspace_cache_key(workspace: Path) -> str:
    """Stable Redis field component for a resolved workspace path."""
    resolved = workspace.expanduser().resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return f"{KEY_PREFIX}:{digest}"


def redis_client() -> Any:
    import redis

    url = (os.environ.get("REDIS_URL") or "").strip() or DEFAULT_REDIS_URL
    return redis.Redis.from_url(url, decode_responses=True)


def save_last_session_id(workspace: Path, session_id: str) -> None:
    sid = (session_id or "").strip()
    if not sid:
        return
    key = workspace_cache_key(workspace)
    try:
        r = redis_client()
        r.set(key, sid, ex=TTL_SEC if TTL_SEC > 0 else None)
        _LOG.info("Recorded Claude session %s for workspace key %s", sid[:12] + "…", key)
    except Exception as exc:
        _LOG.warning("Could not persist session id to Redis: %s", exc)


def get_last_session_id(workspace: Path) -> str | None:
    key = workspace_cache_key(workspace)
    try:
        r = redis_client()
        raw = r.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        if raw:
            return str(raw).strip()
    except Exception as exc:
        _LOG.warning("Could not read session id from Redis: %s", exc)
    return None


def extract_session_id(combined_output: str) -> str | None:
    """Best-effort parse of Claude Code session id from captured CLI output."""
    text = combined_output or ""
    m_sess = re.search(r"\b(sess_[a-zA-Z0-9_-]{10,})\b", text)
    if m_sess:
        return m_sess.group(1).strip()
    m_labeled = re.search(
        r"(?:session(?:\s+id)?|conversation)[:\s]+([a-zA-Z0-9_.:-]{12,})",
        text,
        re.IGNORECASE,
    )
    if m_labeled:
        return m_labeled.group(1).strip()
    uuid_matches = list(_UUID_RE.finditer(text))
    if uuid_matches:
        return uuid_matches[-1].group(1).strip()
    return None


def record_session_enabled() -> bool:
    return os.environ.get("OCTO_RECORD_CLAUDE_SESSION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def run_claude_relay_stderr_and_record(
    cmd: list[str],
    *,
    workspace: Path,
) -> int:
    """Run ``claude`` with stdin/stdout inherited; tee stderr for session-id extraction."""
    proc = subprocess.Popen(
        cmd,
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    buf: list[str] = []

    def relay() -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                buf.append(line)
                sys.stderr.write(line)
                sys.stderr.flush()
        except BrokenPipeError:
            pass

    t = threading.Thread(target=relay, daemon=True)
    t.start()
    rc = proc.wait()
    t.join(timeout=30)
    merged = "".join(buf)
    sid = extract_session_id(merged)
    if sid:
        save_last_session_id(workspace, sid)
    elif record_session_enabled():
        _LOG.debug(
            "OCTO_RECORD_CLAUDE_SESSION set but no session id matched stderr (%d chars)",
            len(merged),
        )
    return rc


def run_claude_capture_and_record(
    cmd: list[str],
    *,
    workspace: Path,
) -> int:
    """Non-interactive capture (stdout/stderr) then persist session id."""
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    sid = extract_session_id(out)
    if sid:
        save_last_session_id(workspace, sid)
    return proc.returncode


def should_use_capture_mode(claude_argv: list[str]) -> bool:
    """Heuristic: ``-p`` / ``--print`` runs are non-interactive; safe to capture_output."""
    for i, a in enumerate(claude_argv):
        if a in ("-p", "--print"):
            return True
        if a == "--output-format":
            return True
    return False
