"""Protected ``/docs/context`` viewer for the last Ollama grounded-review prompt."""

from __future__ import annotations

import html
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(tags=["developer-tools"])

_ENV_KEY = "OCTO_SYSTEM_PROMPT_VIEWER_KEY"


def _expected_api_key() -> str:
    return (os.environ.get(_ENV_KEY) or "").strip()


def verify_context_viewer_key(request: Request) -> None:
    """Require ``X-API-Key`` or ``Authorization: Bearer`` matching :envvar:`OCTO_SYSTEM_PROMPT_VIEWER_KEY`."""
    expected = _expected_api_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{_ENV_KEY} is not set — add it to deploy/local-ai/.env.local (or process env).",
        )

    got_raw = (request.headers.get("X-API-Key") or "").strip()
    auth = request.headers.get("Authorization") or ""
    if not got_raw and auth.lower().startswith("bearer "):
        got_raw = auth[7:].strip()

    if not got_raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key (use X-API-Key or Authorization: Bearer).",
        )

    exp_b = expected.encode("utf-8")
    got_b = got_raw.encode("utf-8")
    if len(got_b) != len(exp_b):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")
    if not secrets.compare_digest(got_b, exp_b):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")


def _snapshot_payload() -> dict[str, Any] | None:
    try:
        from observability.prompt_capture import get_last_prompt_snapshot
    except ImportError:
        return None
    return get_last_prompt_snapshot()


@router.get("/docs/context", response_model=None)
async def docs_context(
    request: Request,
    fmt: str | None = Query(
        default=None,
        alias="format",
        description='Use format=json for JSON (also respects Accept: application/json)',
    ),
) -> HTMLResponse | JSONResponse:
    """HTML or JSON view of the exact last prompt sent to Ollama (includes embedded Trivy/CodeQL text)."""
    verify_context_viewer_key(request)

    want_json = fmt == "json" or (
        "application/json" in (request.headers.get("accept") or "").lower()
        and fmt != "html"
    )

    snap = _snapshot_payload()
    if snap is None:
        empty = {
            "status": "empty",
            "message": "No Ollama prompt captured yet — run a grounded review first.",
            "prompt": None,
        }
        if want_json:
            return JSONResponse(empty)
        body = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>System prompt viewer</title>"
            "<meta http-equiv='refresh' content='5'></head><body>"
            "<p><strong>No prompt captured yet.</strong> Trigger a grounded review, then refresh.</p>"
            "</body></html>"
        )
        return HTMLResponse(body)

    wall = float(snap.get("captured_wall", 0))
    iso = datetime.fromtimestamp(wall, tz=timezone.utc).isoformat()
    payload = {
        "status": "ok",
        "captured_at": iso,
        "model": snap.get("model"),
        "ollama_base_url": snap.get("ollama_base_url"),
        "num_ctx": snap.get("num_ctx"),
        "temperature": snap.get("temperature"),
        "timeout_seconds": snap.get("timeout_seconds"),
        "prompt_chars": snap.get("prompt_chars"),
        "prompt": snap.get("prompt"),
    }

    if want_json:
        return JSONResponse(payload)

    prompt_esc = html.escape(str(snap.get("prompt") or ""), quote=True)
    meta_esc = html.escape(json.dumps({k: v for k, v in payload.items() if k != "prompt"}, indent=2))
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta http-equiv="refresh" content="8"/>
  <title>Octo-spork — system prompt (live)</title>
  <style>
    body {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
           margin: 1rem; background: #0d1117; color: #e6edf3; }}
    h1 {{ font-size: 1.1rem; }}
    .meta {{ background: #161b22; padding: 0.75rem; border-radius: 6px; margin-bottom: 1rem;
             white-space: pre-wrap; font-size: 0.75rem; color: #8b949e; }}
    pre {{ background: #010409; padding: 1rem; overflow: auto; border-radius: 6px;
           border: 1px solid #30363d; font-size: 0.8rem; line-height: 1.35; }}
    .hint {{ color: #8b949e; font-size: 0.85rem; margin-top: 0.5rem; }}
  </style>
</head>
<body>
  <h1>Last Ollama prompt (grounded review)</h1>
  <p class="hint">Auto-refreshes every 8s. Trivy / CodeQL / dependency blocks appear inline below when injected.</p>
  <div class="meta">{meta_esc}</div>
  <pre id="prompt">{prompt_esc}</pre>
</body>
</html>"""
    return HTMLResponse(page)
