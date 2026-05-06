"""Long-term conversation memory: consolidate oversized Redis thread snapshots.

When a LangGraph-style ``values["messages"]`` thread exceeds a token budget (default **10,000**),
``SessionStore.push_snapshot`` triggers **Memory Consolidation**:

1. Call a fast ~3B Ollama model to compress the transcript into **10 key bullet points**.
2. Replace ``messages`` with the summary (plus a short system note).
3. Write the **full** prior ``values`` payload to cold-storage JSON for manual auditing.

Environment:

- ``OCTO_MEMORY_CONSOLIDATION_ENABLED`` — ``1`` / ``true`` to enable (default: **enabled**).
- ``OCTO_MEMORY_TOKEN_THRESHOLD`` — estimated tokens before consolidation (default **10000**).
- ``OCTO_MEMORY_SUMMARIZER_MODEL`` — Ollama tag for the fast summarizer (default ``qwen2.5:3b``).
- ``OLLAMA_LOCAL_URL`` / ``OLLAMA_BASE_URL`` — Ollama HTTP API (default ``http://127.0.0.1:11434``).
- ``OCTO_MEMORY_COLD_STORAGE_DIR`` — override directory for JSON archives (default
  ``<repo>/.local/octo_session/cold_storage`` using ``OCTO_SPORK_REPO_ROOT`` or cwd).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

_DEFAULT_THRESHOLD = 10_000
_DEFAULT_FAST_MODEL = "qwen2.5:3b"


def _estimate_tokens_python(text: str) -> int:
    from claude_bridge.token_governor import estimate_tokens_python

    return estimate_tokens_python(text)


def consolidation_enabled() -> bool:
    raw = (os.environ.get("OCTO_MEMORY_CONSOLIDATION_ENABLED") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def token_threshold() -> int:
    raw = (os.environ.get("OCTO_MEMORY_TOKEN_THRESHOLD") or "").strip()
    if raw:
        try:
            return max(1024, int(raw))
        except ValueError:
            pass
    return _DEFAULT_THRESHOLD


def _ollama_base_url() -> str:
    return (
        (os.environ.get("OLLAMA_LOCAL_URL") or "").strip()
        or (os.environ.get("OLLAMA_BASE_URL") or "").strip()
        or "http://127.0.0.1:11434"
    ).rstrip("/")


def _summarizer_model() -> str:
    return (os.environ.get("OCTO_MEMORY_SUMMARIZER_MODEL") or _DEFAULT_FAST_MODEL).strip() or _DEFAULT_FAST_MODEL


def _cold_storage_root() -> Path:
    override = (os.environ.get("OCTO_MEMORY_COLD_STORAGE_DIR") or "").strip()
    if override:
        p = Path(override).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    repo_hint = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    root = Path(repo_hint).expanduser().resolve() if repo_hint else Path.cwd()
    d = root / ".local" / "octo_session" / "cold_storage"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_thread_slug(thread_id: str) -> str:
    s = re.sub(r"[^\w.\-]+", "_", thread_id.strip())[:120]
    return s or "thread"


def _json_default(o: Any) -> Any:
    try:
        return str(o)
    except Exception:
        return repr(o)


def _normalize_role(role: str) -> str:
    r = (role or "").strip().lower()
    if r in {"human", "user"}:
        return "user"
    if r in {"ai", "assistant"}:
        return "assistant"
    if r in {"system"}:
        return "system"
    return r or "unknown"


def _message_content(m: Any) -> str:
    if isinstance(m, dict):
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts: list[str] = []
            for block in c:
                if isinstance(block, dict):
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    elif isinstance(block.get("content"), str):
                        parts.append(block["content"])
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts)
        return str(c if c is not None else "")
    role = getattr(m, "type", None) or getattr(m, "role", None)
    content = getattr(m, "content", None)
    if content is not None:
        return _message_content({"role": str(role or ""), "content": content})
    return str(m)


def _messages_to_transcript(values: dict[str, Any]) -> str:
    msgs = values.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return json.dumps(values, default=_json_default)

    lines: list[str] = []
    for m in msgs:
        if isinstance(m, dict):
            role = _normalize_role(str(m.get("role") or ""))
        else:
            typ = getattr(m, "type", None)
            if typ is not None:
                role = _normalize_role(str(typ))
            else:
                role = _normalize_role(str(getattr(m, "role", "") or ""))
        body = _message_content(m)
        lines.append(f"{role.upper()}: {body}")
    return "\n\n".join(lines)


def estimate_conversation_tokens(values: dict[str, Any]) -> int:
    """Rough token estimate for the conversational portion (messages or whole snapshot)."""
    text = _messages_to_transcript(values)
    return _estimate_tokens_python(text)


class LongTermSummarizer:
    """Wraps threshold detection, Ollama summarization, cold storage, and message replacement."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        threshold: int | None = None,
        ollama_base_url: str | None = None,
        model: str | None = None,
        cold_storage_dir: Path | None = None,
    ) -> None:
        self._enabled = consolidation_enabled() if enabled is None else enabled
        self._threshold = token_threshold() if threshold is None else threshold
        self._base = (ollama_base_url or _ollama_base_url()).rstrip("/")
        self._model = (model or _summarizer_model()).strip()
        self._cold_dir = Path(cold_storage_dir or _cold_storage_root()).expanduser().resolve()
        self._cold_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> LongTermSummarizer:
        return cls()

    def estimate_tokens(self, values: dict[str, Any]) -> int:
        return estimate_conversation_tokens(values)

    def should_consolidate(self, values: dict[str, Any]) -> bool:
        if not self._enabled:
            return False
        return self.estimate_tokens(values) > self._threshold

    def archive_full_history(self, thread_id: str, prior_values: dict[str, Any], *, prior_tokens: int) -> Path:
        """Write full ``values`` JSON for auditing."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{_safe_thread_slug(thread_id)}_{ts}.json"
        path = self._cold_dir / name
        payload = {
            "version": 1,
            "kind": "memory_consolidation_full_history",
            "thread_id": thread_id,
            "archived_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "prior_estimated_tokens": prior_tokens,
            "values": prior_values,
        }
        path.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")
        _LOG.info("LongTermSummarizer: archived full history to %s", path)
        return path

    def summarize_transcript(self, transcript: str, *, timeout_sec: float = 120.0) -> str:
        """Ask the fast 3B (or configured) model for exactly 10 bullet points."""
        import httpx

        cap = min(240_000, max(32_000, int(os.environ.get("OCTO_MEMORY_SUMMARY_INPUT_CHARS", "120000") or "120000")))
        body = transcript if len(transcript) <= cap else transcript[:cap] + "\n\n… [truncated for summarizer input cap]\n"

        sysp = (
            "You summarize a developer/agent conversation for downstream context recovery. "
            "Reply with **exactly 10** bullet points. Each bullet must start with '- ' (markdown list). "
            "Cover goals, decisions, blockers, files/repos mentioned, and open questions. "
            "Be dense and factual. No preamble or closing."
        )
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": sysp},
                {
                    "role": "user",
                    "content": "Summarize this transcript:\n\n" + body,
                },
            ],
            "stream": False,
            "options": {"temperature": 0.15, "num_predict": 1536},
        }
        url = f"{self._base}/api/chat"
        with httpx.Client(timeout=timeout_sec) as client:
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        msg = data.get("message")
        out = ""
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            out = msg["content"].strip()
        if not out:
            out = str(data.get("response") or "").strip()
        return self._ensure_ten_bullets(out)

    def _ensure_ten_bullets(self, text: str) -> str:
        lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
        bullets = [ln for ln in lines if ln.lstrip().startswith(("-", "*", "•"))]
        if len(bullets) >= 10:
            return "\n".join(bullets[:10])
        if len(lines) >= 10:
            return "\n".join(f"- {ln.lstrip('-*• ')}" for ln in lines[:10])
        padded = list(lines)
        while len(padded) < 10:
            padded.append("See cold storage audit file for additional detail.")
        return "\n".join(f"- {ln.lstrip('-*• ')}" for ln in padded[:10])

    def replacement_messages(self, bullet_text: str, cold_path: Path) -> list[dict[str, str]]:
        sys_note = (
            "Earlier conversation was consolidated: the thread exceeded the configured token budget. "
            f"Full verbatim history is archived for audit at: {cold_path}"
        )
        return [
            {"role": "system", "content": sys_note},
            {"role": "assistant", "content": bullet_text},
        ]

    def consolidate_if_needed(self, thread_id: str, values: dict[str, Any]) -> dict[str, Any]:
        if not self.should_consolidate(values):
            return values

        prior_tokens = self.estimate_tokens(values)
        prior = copy.deepcopy(values)
        cold_path = self.archive_full_history(thread_id, prior, prior_tokens=prior_tokens)
        transcript = _messages_to_transcript(prior)

        try:
            bullets = self.summarize_transcript(transcript)
        except Exception as exc:
            _LOG.warning("LongTermSummarizer: Ollama summarize failed (%s); using fallback bullets", exc)
            fb = [
                "- Summarizer call failed; use the cold-storage JSON for the full thread.",
                f"- Cold storage path: {cold_path}",
            ]
            while len(fb) < 10:
                fb.append("- Remaining detail is only in the archived JSON file.")
            bullets = "\n".join(fb[:10])

        out = copy.deepcopy(values)
        out["messages"] = self.replacement_messages(bullets, cold_path)
        out["octo_memory_consolidation"] = {
            "consolidated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "cold_storage_path": str(cold_path),
            "prior_estimated_tokens": prior_tokens,
            "summarizer_model": self._model,
            "token_threshold": self._threshold,
        }
        _LOG.info(
            "LongTermSummarizer: consolidated thread_id=%s prior_tokens≈%s",
            thread_id[:24],
            prior_tokens,
        )
        return out


def maybe_memory_consolidate(thread_id: str, values: dict[str, Any]) -> dict[str, Any]:
    """Hook for :class:`SessionStore`: returns *values* unchanged unless consolidation applies."""
    return LongTermSummarizer.from_env().consolidate_if_needed(thread_id, values)
