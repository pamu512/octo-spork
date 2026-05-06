"""Persist the last grounded review (prompt + answer) for follow-up REPL (``local_ai_stack chat``)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_REL_PATH = Path(".octo") / "review_session" / "last_review.json"
_MAX_JSON_BYTES = int(os.environ.get("OCTO_REVIEW_SESSION_MAX_BYTES", "4000000"))
_MAX_FIELD_CHARS = 1_800_000


def _cap_field(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 120] + "\n… [truncated for session store]\n"


def _workspace_root() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def review_session_path(repo_root: Path | None = None) -> Path:
    root = repo_root if repo_root is not None else _workspace_root()
    return (root / _REL_PATH).resolve()


def persist_last_review_session(
    payload: dict[str, Any],
    *,
    repo_root: Path | None = None,
) -> Path | None:
    """Atomically write session JSON. Returns path or None if payload empty / error."""
    try:
        path = review_session_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        lim = _MAX_FIELD_CHARS
        out = dict(payload)
        for key in ("prompt", "answer"):
            if isinstance(out.get(key), str):
                out[key] = _cap_field(out[key], lim)
        raw = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
        while len(raw.encode("utf-8")) > _MAX_JSON_BYTES and lim > 50_000:
            lim = max(50_000, lim // 2)
            out = dict(payload)
            for key in ("prompt", "answer"):
                if isinstance(out.get(key), str):
                    out[key] = _cap_field(out[key], lim)
            raw = json.dumps(out, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(
            prefix="last_review_",
            suffix=".json",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            Path(tmp).replace(path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path
    except OSError:
        return None


def load_last_review_session(repo_root: Path | None = None) -> dict[str, Any] | None:
    """Load persisted session, or None if missing."""
    path = review_session_path(repo_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data
