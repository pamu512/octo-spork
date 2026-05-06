"""ModelFallback: when a large primary model fails to pull/load under VRAM pressure, use a local small Coder model."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ENV_FALLBACK_ACTIVE = "OCTO_MODEL_FALLBACK_ACTIVE"
ENV_DEGRADED_INSTRUCTION = "OCTO_DEGRADED_TASK_INSTRUCTION"

DEGRADED_INSTRUCTION = (
    "You are in emergency degraded mode; keep responses concise and focused "
    "only on the critical fix"
)

# stderr/stdout snippets suggesting GPU / host memory pressure (Ollama + CUDA + OS).
_VRAM_OR_MEMORY_MARKERS: tuple[str, ...] = (
    "out of memory",
    "cuda error",
    "cuda runtime",
    "resource exhausted",
    "cannot allocate",
    "insufficient memory",
    "insufficient vram",
    "vram",
    "oom",
    "memory pressure",
    "failed to load model",
    "model requires",
    "mmap",
    "nvidia",
)


def _looks_like_memory_or_vram_failure(combined_output: str) -> bool:
    text = (combined_output or "").lower()
    return any(m in text for m in _VRAM_OR_MEMORY_MARKERS)


def _is_coder_family(name_lower: str) -> bool:
    if any(
        k in name_lower
        for k in (
            "coder",
            "codellama",
            "deepseek-coder",
            "starcoder",
            "qwen2.5-coder",
            "qwen3-coder",
        )
    ):
        return True
    if "code" in name_lower and any(
        x in name_lower for x in ("llama", "qwen", "gemma", "phi", "mistral", "deepseek")
    ):
        return True
    return False


def _small_size_score(name_lower: str) -> int | None:
    """Higher = better match for requested 8B / 3B small coders; None = skip (too large or unknown)."""
    if any(b in name_lower for b in ("70b", "72b", "65b", "40b", "34b", "32b", "30b")):
        return None
    if re.search(r"\b8b\b|:8b|-8b", name_lower):
        return 300
    if re.search(r"\b7b\b|:7b|-7b", name_lower):
        return 295
    if re.search(r"3\.8b|\b3b\b|:3b|-3b", name_lower):
        return 280
    if re.search(r"\b14b\b|:14b", name_lower):
        return 50
    if re.search(r"\b13b\b|:13b", name_lower):
        return 45
    if _is_coder_family(name_lower):
        return 120
    return None


def pick_small_coder_fallback(local_model_names: list[str]) -> str | None:
    """Prefer an 8B or 3B-class Coder tag present in ``ollama list`` / ``/api/tags``."""
    candidates: list[tuple[int, str]] = []
    for raw in local_model_names:
        name = (raw or "").strip()
        if not name:
            continue
        lower = name.lower()
        if not _is_coder_family(lower):
            continue
        score = _small_size_score(lower)
        if score is None:
            continue
        candidates.append((score, name))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][1]


def list_local_ollama_model_names(ollama_base_url: str, *, timeout: float = 15.0) -> list[str]:
    raw = (ollama_base_url or "").strip() or "http://127.0.0.1:11434"
    url = raw.rstrip("/") + "/api/tags"
    try:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "local-ai-stack-model-fallback/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("ModelFallback: could not list /api/tags: %s", exc)
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    names: list[str] = []
    for item in models:
        if isinstance(item, dict):
            n = item.get("name")
            if isinstance(n, str) and n.strip():
                names.append(n.strip())
    return names


def run_ollama_pull_with_model_fallback(
    root: Path,
    env_file: Path,
    env_values: dict[str, str],
    process_env: dict[str, str],
    logger: logging.Logger,
    agenticseek_path: Path,
) -> dict[str, str]:
    """
    Run ``ollama pull`` for ``OLLAMA_MODEL``. On failure that looks VRAM/memory-related,
    pick a local small Coder model, rewrite the env file, refresh AgenticSeek ``config.ini``,
    and set degraded-task instructions for downstream consumers.
    """
    import local_ai_stack.__main__ as main_mod

    primary = (env_values.get("OLLAMA_MODEL") or "qwen2.5:14b").strip() or "qwen2.5:14b"
    cmd = ["ollama", "pull", primary]
    main_mod._print(f"+ {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        env=process_env,
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return env_values

    combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
    logger.error(
        "ModelFallback: primary model pull failed model=%r rc=%s",
        primary,
        proc.returncode,
    )
    logger.debug("ModelFallback: ollama pull output (truncated): %s", combined[:4000])

    if not _looks_like_memory_or_vram_failure(combined):
        raise RuntimeError(
            f"ollama pull failed for {primary!r} (exit {proc.returncode}). Output:\n{combined[:2500]}"
        )

    logger.warning(
        "ModelFallback: treating failure as possible VRAM/memory pressure for model %r",
        primary,
    )

    ollama_url = (env_values.get("OLLAMA_LOCAL_URL") or "http://127.0.0.1:11434").strip()
    local_names = list_local_ollama_model_names(ollama_url)
    fallback = pick_small_coder_fallback(local_names)
    if not fallback:
        raise RuntimeError(
            "Primary model failed under suspected VRAM/memory pressure and no suitable local "
            "8B/7B/3B Coder-class model was found in Ollama `/api/tags`. "
            "Install e.g. `qwen2.5-coder:7b` while online, then retry `up`.\n"
            f"Pull failure output:\n{combined[:2000]}"
        )

    logger.warning(
        "ModelFallback: rerouting stack from %r to local model %r (degraded task mode)",
        primary,
        fallback,
    )
    main_mod._print(
        f"ModelFallback: primary {primary!r} failed under memory pressure — "
        f"using local {fallback!r} with degraded-task instructions "
        f"(see {ENV_DEGRADED_INSTRUCTION} in {env_file})."
    )

    updates = {
        "OLLAMA_MODEL": fallback,
        ENV_FALLBACK_ACTIVE: "1",
        ENV_DEGRADED_INSTRUCTION: DEGRADED_INSTRUCTION,
    }
    try:
        main_mod._rewrite_env_file_string_values(env_file, updates)
    except RuntimeError as exc:
        raise RuntimeError(f"ModelFallback: could not update env file {env_file}") from exc

    merged = dict(env_values)
    merged.update(updates)
    try:
        main_mod._configure_agenticseek_ini(agenticseek_path, merged)
    except OSError as exc:
        logger.warning("ModelFallback: could not rewrite AgenticSeek config.ini: %s", exc)

    return merged
