"""Minimal Ollama HTTP client."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _get_json(base: str, path: str, timeout: float) -> dict[str, Any] | None:
    url = f"{base.rstrip('/')}{path}"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _post_json(base: str, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def ollama_show(base_url: str, name: str, *, timeout: float = 30.0) -> dict[str, Any] | None:
    return _post_json(base_url, "/api/show", {"name": name}, timeout)


def ollama_ps(base_url: str, *, timeout: float = 15.0) -> dict[str, Any] | None:
    """GET ``/api/ps`` — running models on the Ollama server."""
    return _get_json(base_url, "/api/ps", timeout)


def ollama_list_tags(local_models_text: str) -> set[str]:
    """Parse ``ollama list`` stdout → ``owner/name:tag`` style keys."""
    names: set[str] = set()
    for line in local_models_text.splitlines()[1:]:
        parts = line.split()
        if parts:
            names.add(parts[0].strip())
    return names
