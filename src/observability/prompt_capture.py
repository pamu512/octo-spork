"""Capture the latest full Ollama prompt (grounded review system prompt + evidence).

Updated on each :func:`run_ollama_review` call so operators can inspect injected Trivy/CodeQL blocks.
"""

from __future__ import annotations

import threading
import time
from typing import Any


_lock = threading.Lock()
_record: dict[str, Any] | None = None


def record_ollama_review_prompt(
    prompt: str,
    *,
    model: str,
    ollama_base_url: str,
    num_ctx: int,
    temperature: float,
    timeout_seconds: int,
) -> None:
    """Store the exact prompt string sent to Ollama ``/api/generate`` (thread-safe)."""
    global _record
    wall = time.time()
    rec = {
        "prompt": str(prompt),
        "model": str(model),
        "ollama_base_url": str(ollama_base_url).rstrip("/"),
        "num_ctx": int(num_ctx),
        "temperature": float(temperature),
        "timeout_seconds": int(timeout_seconds),
        "prompt_chars": len(str(prompt)),
        "captured_wall": wall,
    }
    with _lock:
        _record = rec


def get_last_prompt_snapshot() -> dict[str, Any] | None:
    """Return a shallow copy of the last capture, or ``None`` if nothing recorded yet."""
    with _lock:
        if _record is None:
            return None
        return dict(_record)
