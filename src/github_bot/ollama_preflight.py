"""Pre-flight: verify local Ollama is reachable and the configured model is available before LLM work."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urljoin


def _normalize_base(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _model_base_name(name: str) -> str:
    """Strip Ollama tag (``:latest``, ``:14b``, …) for comparison."""
    s = str(name or "").strip().lower()
    if ":" in s:
        return s.split(":", 1)[0].strip()
    return s


def _requested_matches_tag(requested: str, tag_name: str) -> bool:
    """True if ``tag_name`` from Ollama API refers to the same model as ``requested``."""
    req = str(requested or "").strip().lower()
    tag = str(tag_name or "").strip().lower()
    if not req or not tag:
        return False
    if tag == req or tag.startswith(req + ":"):
        return True
    return _model_base_name(tag) == _model_base_name(req)


def model_present_in_tags(tag_json: dict[str, object], requested: str) -> bool:
    models = tag_json.get("models")
    if not isinstance(models, list):
        return False
    for item in models:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and _requested_matches_tag(requested, name):
            return True
    return False


def model_loaded_in_ps(ps_json: dict[str, object], requested: str) -> bool:
    models = ps_json.get("models")
    if not isinstance(models, list):
        return False
    for item in models:
        if not isinstance(item, dict):
            continue
        name = item.get("model") or item.get("name")
        if isinstance(name, str) and _requested_matches_tag(requested, name):
            return True
    return False


def verify_ollama_preflight(
    ollama_base_url: str,
    model: str,
    *,
    timeout_sec: float | None = None,
) -> tuple[bool, str]:
    """Ping Ollama and ensure ``model`` exists in ``/api/tags``.

    If ``OLLAMA_PREFLIGHT_REQUIRE_LOADED`` is true, also require the model in ``/api/ps``
    (currently resident). Returns ``(True, "")`` on success, or ``(False, human-readable reason)``.
    """
    base = _normalize_base(ollama_base_url)
    if not base:
        return False, "Ollama base URL is empty."

    if timeout_sec is None:
        timeout_sec = float(os.environ.get("OLLAMA_PREFLIGHT_TIMEOUT_SEC", "8"))

    require_loaded = os.environ.get("OLLAMA_PREFLIGHT_REQUIRE_LOADED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # 1) Daemon reachable
    version_url = urljoin(base + "/", "api/version")
    try:
        req = urllib.request.Request(version_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if getattr(resp, "status", 200) != 200:
                return False, f"Ollama `/api/version` returned HTTP {getattr(resp, 'status', 'unknown')}."
    except urllib.error.HTTPError as exc:
        return False, f"Ollama unreachable (`/api/version` HTTP {exc.code}). Is the daemon running?"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"Ollama unreachable at `{base}`: {reason}"
    except TimeoutError:
        return False, f"Ollama timed out after {timeout_sec:.0f}s at `{base}`."
    except OSError as exc:
        return False, f"Ollama connection error at `{base}`: {exc}"

    # 2) Tags — model installed
    tags_url = urljoin(base + "/", "api/tags")
    try:
        req = urllib.request.Request(tags_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        tags_data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return False, f"Could not read Ollama `/api/tags`: {exc}"

    if not isinstance(tags_data, dict):
        return False, "Ollama `/api/tags` returned an unexpected payload."

    if not model_present_in_tags(tags_data, model):
        names: list[str] = []
        for item in tags_data.get("models") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
        preview = ", ".join(names[:12])
        if len(names) > 12:
            preview += ", …"
        hint = f" Available models (sample): {preview}" if preview else " No models reported."
        return (
            False,
            f"Model `{model}` is not installed in Ollama (`ollama pull {model}`).{hint}",
        )

    # 3) Optional — must appear in /api/ps (VRAM / loaded)
    if require_loaded:
        ps_url = urljoin(base + "/", "api/ps")
        try:
            req = urllib.request.Request(ps_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            ps_data = json.loads(raw)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return False, f"Could not read Ollama `/api/ps`: {exc}"

        if isinstance(ps_data, dict) and not model_loaded_in_ps(ps_data, model):
            return (
                False,
                f"Model `{model}` is installed but not loaded (`OLLAMA_PREFLIGHT_REQUIRE_LOADED=true`). "
                f"Load it with `ollama run {model}` or trigger a short inference, then re-run the review.",
            )

    try:
        from infra.resource_manager import enforce_before_ollama

        enforce_before_ollama(model, ollama_base_url)
    except ResourceWarning as exc:
        return False, str(exc)

    return True, ""
