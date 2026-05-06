"""Redis-backed SessionStore for LangGraph-style agent state (thread_id + values snapshot).

Uses the same ``REDIS_URL`` as the stack (local Redis / Valkey container). Serialized JSON is stored
at ``{OCTO_LANGGRAPH_SESSION_PREFIX}:latest`` and ``...:thread:{thread_id}``.

Example with a compiled LangGraph app and checkpoint config::

    store = SessionStore()
    config = {"configurable": {"thread_id": "review-42"}}
    handle = store.start_periodic_save(
        300.0,
        lambda: (
            config["configurable"]["thread_id"],
            dict(app.get_state(config).values),
        ),
    )
    # ... run agent ...
    handle.stop()

Resume via ``python -m local_ai_stack resume`` (writes ``.local/octo_session/langgraph_resume.json``
and sets ``OCTO_LANGGRAPH_*`` for a child process).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_LOG = logging.getLogger(__name__)

_DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
_DEFAULT_PREFIX = "octo-spork:langgraph:session"
_DEFAULT_INTERVAL_SEC = 300
_DEFAULT_TTL_SEC = 7 * 24 * 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json_default(o: Any) -> Any:
    try:
        return str(o)
    except Exception:
        return repr(o)


class SessionStore:
    """Serialize ``thread_id`` and checkpoint ``values`` to JSON in local Redis."""

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        key_prefix: str | None = None,
    ) -> None:
        url = (redis_url or os.environ.get("REDIS_URL") or "").strip() or _DEFAULT_REDIS_URL
        self._redis_url = url
        self._prefix = (key_prefix or os.environ.get("OCTO_LANGGRAPH_SESSION_PREFIX") or "").strip() or _DEFAULT_PREFIX
        self._ttl_sec = int(os.environ.get("OCTO_LANGGRAPH_SESSION_TTL_SEC", str(_DEFAULT_TTL_SEC)))

    def _client(self) -> Any:
        import redis

        return redis.Redis.from_url(self._redis_url, decode_responses=True)

    def push_snapshot(self, thread_id: str, values: dict[str, Any]) -> None:
        """Store latest state under ``{prefix}:latest`` and per-thread key."""
        tid = (thread_id or "").strip()
        if not tid:
            raise ValueError("thread_id is required")
        if values is None:
            raise ValueError("values must not be None")

        vals: dict[str, Any] = dict(values)
        try:
            from agent_guard.long_term_summarizer import maybe_memory_consolidate

            vals = maybe_memory_consolidate(tid, vals)
        except Exception:
            _LOG.exception("SessionStore: memory consolidation failed; storing snapshot without consolidation")

        payload: dict[str, Any] = {
            "version": 1,
            "thread_id": tid,
            "values": vals,
            "updated_at": _now_iso(),
        }
        raw = json.dumps(payload, default=_json_default)
        r = self._client()
        latest_key = f"{self._prefix}:latest"
        thread_key = f"{self._prefix}:thread:{tid}"
        r.set(latest_key, raw)
        r.set(thread_key, raw)
        if self._ttl_sec > 0:
            r.expire(latest_key, self._ttl_sec)
            r.expire(thread_key, self._ttl_sec)
        _LOG.debug("SessionStore: pushed snapshot thread_id=%s keys=%s", tid[:16], len(values))

    def get_latest_snapshot(self) -> dict[str, Any] | None:
        """Return parsed JSON for the latest saved session, or ``None``."""
        r = self._client()
        raw = r.get(f"{self._prefix}:latest")
        if raw is None or raw == "":
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            _LOG.warning("SessionStore: corrupt snapshot in Redis: %s", exc)
            return None
        if not isinstance(obj, dict):
            return None
        return obj

    def get_snapshot_for_thread(self, thread_id: str) -> dict[str, Any] | None:
        tid = (thread_id or "").strip()
        if not tid:
            return None
        r = self._client()
        raw = r.get(f"{self._prefix}:thread:{tid}")
        if raw is None or raw == "":
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    def start_periodic_save(
        self,
        interval_sec: float | None,
        get_state: Callable[[], tuple[str, dict[str, Any]] | None],
    ) -> PeriodicSaveHandle:
        """Background loop: every *interval_sec* (default 300), push ``thread_id`` and ``values``.

        *get_state* should return ``(thread_id, values)`` or ``None`` to skip a tick.
        """
        raw_iv = interval_sec
        if raw_iv is None:
            raw_iv = float(os.environ.get("OCTO_SESSION_SAVE_INTERVAL_SEC", str(_DEFAULT_INTERVAL_SEC)))
        iv = max(30.0, float(raw_iv))

        stop = threading.Event()

        def loop() -> None:
            while True:
                try:
                    got = get_state()
                    if got is not None:
                        tid, vals = got
                        if (tid or "").strip() and vals is not None and isinstance(vals, dict):
                            self.push_snapshot(tid.strip(), vals)
                        elif vals is not None and not isinstance(vals, dict):
                            _LOG.warning("SessionStore: values must be a dict; skipping tick")
                except Exception:
                    _LOG.exception("SessionStore: periodic save failed")
                if stop.wait(timeout=iv):
                    break

        t = threading.Thread(target=loop, name="session-store-periodic", daemon=True)
        t.start()
        return PeriodicSaveHandle(stop_event=stop, thread=t)


@dataclass
class PeriodicSaveHandle:
    """Stop background autosave (waits briefly for the worker thread)."""

    stop_event: threading.Event
    thread: threading.Thread

    def stop(self, *, join_timeout: float = 5.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout=join_timeout)


def autosave_enabled() -> bool:
    return os.environ.get("OCTO_SESSION_AUTOSAVE", "").strip().lower() in {"1", "true", "yes", "on"}
