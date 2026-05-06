"""Dynamic context scaling: when VRAM pressure is high, summarize low-priority files via Ollama."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from typing import Any

from observability.privacy_filter import redact_for_llm, unredact_response

_LOG = logging.getLogger(__name__)


def governor_enabled() -> bool:
    return os.environ.get("OCTO_CONTEXT_GOVERNOR_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _force_compress() -> bool:
    return os.environ.get("OCTO_CONTEXT_GOVERNOR_FORCE", "").lower() in {"1", "true", "yes", "on"}


def _min_free_mib() -> float:
    raw = (os.environ.get("OCTO_CTX_GOV_MIN_FREE_MIB") or "").strip()
    if raw:
        try:
            return max(128.0, float(raw))
        except ValueError:
            pass
    return 2048.0


def _util_threshold_pct() -> float:
    raw = (os.environ.get("OCTO_CTX_GOV_UTIL_PCT") or "").strip()
    if raw:
        try:
            return max(50.0, min(99.9, float(raw)))
        except ValueError:
            pass
    return 88.0


def _max_files() -> int:
    raw = (os.environ.get("OCTO_CTX_GOV_MAX_FILES") or "").strip()
    if raw:
        try:
            return max(1, min(64, int(raw)))
        except ValueError:
            pass
    return 12


def _min_chars_to_compress() -> int:
    raw = (os.environ.get("OCTO_CTX_GOV_MIN_CHARS_TO_COMPRESS") or "").strip()
    if raw:
        try:
            return max(120, int(raw))
        except ValueError:
            pass
    return 400


def _summarizer_input_cap() -> int:
    raw = (os.environ.get("OCTO_CTX_GOV_SUMMARIZER_INPUT_CHARS") or "").strip()
    if raw:
        try:
            return max(4000, min(120_000, int(raw)))
        except ValueError:
            pass
    return 32_000


def is_low_priority_path(path: str) -> bool:
    """Tests, fixtures, and docs — safe to abstract before heavy review tokens."""
    p = path.replace("\\", "/").strip()
    low = p.lower()
    base = low.split("/")[-1]

    if base in ("readme.md", "readme.rst", "readme.txt"):
        return False

    if "/test/" in f"/{low}/" or "/tests/" in f"/{low}/":
        return True
    if "/__tests__/" in f"/{low}/" or "/__test__/" in f"/{low}/":
        return True
    if low.startswith("test/") or low.startswith("tests/"):
        return True
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    if ".spec." in base or ".test." in base or base.endswith("_spec.rb"):
        return True
    if "/fixtures/" in f"/{low}/" or "/testdata/" in f"/{low}/":
        return True

    if low.endswith(".md"):
        if "docs/" in low or low.startswith("docs/"):
            return True
        if "/documentation/" in f"/{low}/":
            return True

    if "/docs/" in f"/{low}/" or low.startswith("docs/"):
        return True
    if low.endswith((".rst", ".adoc")):
        return True

    return False


def vram_pressure_high() -> bool:
    """True when GPU memory is constrained (nvidia-smi) or ``OCTO_CONTEXT_GOVERNOR_FORCE``."""
    if _force_compress():
        return True
    try:
        from observability.performance_tracker import sample_vram_nvidia
    except ImportError:
        return False

    s = sample_vram_nvidia()
    used = s.get("used_mib")
    total = s.get("total_mib")
    util = s.get("util_pct")

    if used is not None and total is not None and total > 0:
        free = float(total) - float(used)
        if free < _min_free_mib():
            _LOG.info(
                "context_governor: VRAM pressure (free %.1f MiB < %.1f MiB)",
                free,
                _min_free_mib(),
            )
            return True

    if util is not None and float(util) >= _util_threshold_pct():
        _LOG.info(
            "context_governor: VRAM pressure (util %.1f%% >= %.1f%%)",
            float(util),
            _util_threshold_pct(),
        )
        return True

    return False


def _take_three_sentences(text: str) -> str:
    t = text.strip()
    if not t:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", t)
    sentences = [p.strip() for p in parts if p.strip()]
    if len(sentences) <= 3:
        return " ".join(sentences)
    return " ".join(sentences[:3])


def _ollama_chat_summarize(text: str, *, base_url: str, model: str, timeout_sec: float = 90.0) -> str:
    import httpx

    cap = _summarizer_input_cap()
    body = text if len(text) <= cap else text[:cap] + "\n\n… [truncated for summarizer context]"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You compress source files for downstream code review. "
                    "Reply with exactly three sentences in plain prose. "
                    "No bullets, numbering, markdown headings, or preamble."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Summarize the following file for a senior engineer in exactly three sentences. "
                    "Capture purpose, structure, and notable behaviors.\n\n"
                    f"{body}"
                ),
            },
        ],
        "stream": False,
        "options": {"temperature": 0.15, "num_predict": 512},
    }
    url = f"{base_url.rstrip('/')}/api/chat"
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
    if priv_map:
        out = unredact_response(out, priv_map)
    return _take_three_sentences(out)


def _estimate_snapshot_tokens(snapshot: dict[str, Any], estimate_tokens: Callable[[str], int]) -> int:
    readme = str(snapshot.get("readme") or "")
    total = estimate_tokens(readme)
    for rf in snapshot.get("files") or []:
        c = getattr(rf, "content", None)
        if c is None and isinstance(rf, dict):
            c = rf.get("content")
        total += estimate_tokens(str(c or ""))
    return total


def _set_entry_content(entry: Any, new_content: str) -> None:
    enc = len(new_content.encode("utf-8", errors="replace"))
    if hasattr(entry, "content"):
        entry.content = new_content  # type: ignore[attr-defined]
        if hasattr(entry, "size"):
            entry.size = enc  # type: ignore[attr-defined]
    elif isinstance(entry, dict):
        entry["content"] = new_content
        entry["size"] = enc


class ContextGovernor:
    """Checks VRAM and replaces low-priority file bodies with 3-sentence Ollama abstracts."""

    def __init__(
        self,
        *,
        ollama_base_url: str,
        summarize_model: str | None = None,
    ) -> None:
        self._base = ollama_base_url.rstrip("/")
        self._model = (
            summarize_model
            or os.environ.get("OCTO_CTX_SUMMARIZER_MODEL")
            or os.environ.get("OCTO_SUMMARIZER_MODEL")
            or "llama3.2"
        ).strip()

    def maybe_compress_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        estimate_tokens: Callable[[str], int],
    ) -> dict[str, Any]:
        """Compress eligible files in-place; return stats (including token estimates)."""
        if not governor_enabled():
            return {}

        if not vram_pressure_high():
            return {}

        files = list(snapshot.get("files") or [])
        if not files:
            return {}

        tokens_before = _estimate_snapshot_tokens(snapshot, estimate_tokens)

        candidates: list[tuple[int, Any]] = []
        min_chars = _min_chars_to_compress()
        for rf in files:
            path = getattr(rf, "path", None)
            if path is None and isinstance(rf, dict):
                path = rf.get("path")
            path_s = str(path or "")
            content = getattr(rf, "content", None)
            if content is None and isinstance(rf, dict):
                content = rf.get("content")
            body = str(content or "")
            if not is_low_priority_path(path_s):
                continue
            if len(body) < min_chars:
                continue
            candidates.append((len(body), rf))

        candidates.sort(key=lambda x: x[0], reverse=True)

        compressed_paths: list[str] = []
        max_n = _max_files()
        for size, rf in candidates[:max_n]:
            path_s = str(getattr(rf, "path", "") or (rf.get("path") if isinstance(rf, dict) else "") or "")
            body = getattr(rf, "content", None)
            if body is None and isinstance(rf, dict):
                body = rf.get("content")
            body_s = str(body or "")
            try:
                abstract = _ollama_chat_summarize(
                    body_s,
                    base_url=self._base,
                    model=self._model,
                )
            except Exception as exc:
                _LOG.warning("context_governor: summarize failed for %s: %s", path_s, exc)
                continue
            if not abstract.strip():
                _LOG.warning("context_governor: empty abstract for %s; skipping", path_s)
                continue

            wrapped = (
                "[Abstract — three sentences; ContextGovernor compressed this low-priority path due to VRAM pressure]\n\n"
                f"{abstract}\n\n"
                f"_Original length: {len(body_s)} characters; summarizer model `{self._model}`._"
            )
            _set_entry_content(rf, wrapped)
            compressed_paths.append(path_s)
            _LOG.info(
                "context_governor: compressed `%s` (%d chars -> abstract)",
                path_s,
                size,
            )

        tokens_after = _estimate_snapshot_tokens(snapshot, estimate_tokens)

        stats = {
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "compressed_paths": compressed_paths,
            "summarizer_model": self._model,
        }
        snapshot["_context_governor"] = stats
        return stats
