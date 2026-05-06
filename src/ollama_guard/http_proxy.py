"""Reverse proxy in front of Ollama that may rewrite ``model`` on POST APIs."""

from __future__ import annotations

import json
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from ollama_guard.policy import analyze_model

_REWRITE_PATHS = frozenset({"api/generate", "api/chat", "api/embeddings"})
_SKIP_RESP_HEADERS = frozenset({"connection", "transfer-encoding", "keep-alive"})


def create_app() -> FastAPI:
    upstream = os.environ.get(
        "OLLAMA_GUARD_UPSTREAM",
        os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
    ).rstrip("/")
    analyze_url = os.environ.get("OLLAMA_GUARD_ANALYZE_URL", upstream).rstrip("/")

    app = FastAPI(title="ollama-guard")

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH", "OPTIONS"],
    )
    async def relay(full_path: str, request: Request) -> StreamingResponse:
        body = await request.body()

        if request.method == "POST" and full_path in _REWRITE_PATHS and body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                name = payload.get("model")
                if isinstance(name, str) and name.strip():
                    decision = analyze_model(name.strip(), base_url=analyze_url)
                    if decision.fits_without_change is False and decision.proposed_model:
                        payload["model"] = decision.proposed_model
                        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        url = f"{upstream}/{full_path}"
        q = request.url.query
        if q:
            url = f"{url}?{q}"

        hdrs: dict[str, str] = {}
        for k, v in request.headers.items():
            lk = k.lower()
            if lk in {"host", "connection", "content-length"}:
                continue
            hdrs[k] = v

        timeout = httpx.Timeout(None)
        client = httpx.AsyncClient(timeout=timeout)
        req = client.build_request(request.method, url, content=body or None, headers=hdrs)
        stream = await client.send(req, stream=True)

        out_headers = {
            k: v
            for k, v in stream.headers.items()
            if k.lower() not in _SKIP_RESP_HEADERS
        }

        async def gen() -> bytes:
            try:
                async for chunk in stream.aiter_raw():
                    yield chunk
            finally:
                await stream.aclose()
                await client.aclose()

        return StreamingResponse(
            gen(),
            status_code=stream.status_code,
            headers=out_headers,
        )

    return app
