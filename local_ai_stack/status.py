"""Health dashboard for the local AI stack (SearXNG, Redis, n8n, host Ollama).

CLI path stays ``python -m local_ai_stack status`` — this module is dispatch-only.
Shared probes (env parse, HTTP, docker logs) stay in ``__main__``.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _format_byte_size(num_bytes: int) -> str:
    if num_bytes < 0:
        raise ValueError("num_bytes must be non-negative")
    if num_bytes < 1024:
        return f"{num_bytes} B"
    value = float(num_bytes)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.2f} {unit}"
    return f"{value:.2f} PiB"


def _probe_redis_via_docker_exec(container_name: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["docker", "exec", container_name, "redis-cli", "ping"],
            cwd=str(ROOT),
            check=False,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError:
        return False, "docker CLI not found"
    except subprocess.TimeoutExpired:
        return False, "docker exec timed out"
    except OSError as exc:
        return False, f"OS error: {exc}"
    out = (completed.stdout or "").strip()
    err = (completed.stderr or "").strip()
    if completed.returncode == 0 and out.upper() == "PONG":
        return True, "PONG"
    detail = err or out or f"exit {completed.returncode}"
    return False, detail


def _fetch_ollama_models_and_error(
    base_url: str, *, timeout: float = 12.0
) -> tuple[list[dict[str, object]], str | None]:
    raw_base = (base_url or "").strip() or "http://127.0.0.1:11434"
    url = raw_base.rstrip("/") + "/api/tags"
    try:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "local-ai-stack-status/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return [], f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return [], f"connection failed: {exc.reason}"
    except (TimeoutError, OSError, ValueError, TypeError) as exc:
        return [], str(exc)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return [], f"invalid JSON: {exc}"
    models = data.get("models")
    if not isinstance(models, list):
        return [], "response missing 'models' array"
    typed: list[dict[str, object]] = []
    for item in models:
        if isinstance(item, dict):
            typed.append(item)
    return typed, None


def command_status(env_file: Path) -> None:
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
    except ImportError as exc:
        raise RuntimeError(
            "The status command requires the 'rich' package. Install with: python3 -m pip install rich"
        ) from exc

    # Shared helpers stay in __main__ (hooks / orchestrator). Import after load to
    # avoid a module-level cycle with dispatch.
    from local_ai_stack.__main__ import (
        STATUS_CONTAINER_N8N,
        STATUS_CONTAINER_REDIS,
        STATUS_CONTAINER_SEARXNG,
        _docker_logs_tail,
        _http_get_status,
        _ollama_host_log_snippet,
        _parse_env_file,
        _print,
    )

    try:
        env_values = _parse_env_file(env_file)
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read env file {env_file}: {exc}") from exc

    loopback = "127.0.0.1"
    try:
        searx_port = str(env_values.get("SEARXNG_PORT", "8080")).strip() or "8080"
        n8n_port = str(env_values.get("N8N_PORT", "5678")).strip() or "5678"
        ollama_base = env_values.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434").strip()
        if not ollama_base:
            ollama_base = "http://127.0.0.1:11434"
    except (TypeError, AttributeError) as exc:
        raise RuntimeError(f"Invalid port configuration in env: {exc}") from exc

    searx_url = f"http://{loopback}:{searx_port}/healthz"
    n8n_url = f"http://{loopback}:{n8n_port}/healthz"
    ollama_tags_url = ollama_base.rstrip("/") + "/api/tags"

    table = Table(title="Local AI stack — health", show_lines=True, header_style="bold")
    table.add_column("Service", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center")
    table.add_column("Probe", overflow="fold")
    table.add_column("Detail", overflow="fold")

    failure_logs: list[tuple[str, str, str]] = []

    # SearXNG
    try:
        st = _http_get_status(searx_url, timeout=6.0)
    except (ValueError, TypeError) as exc:
        st = None
        searx_detail = str(exc)
    else:
        searx_detail = f"HTTP {st}" if st is not None else "no response"
    searx_ok = st == 200
    if searx_ok:
        table.add_row("SearXNG", Text("UP", style="bold green"), searx_url, searx_detail)
    else:
        table.add_row("SearXNG", Text("DOWN", style="bold red"), searx_url, searx_detail)
        try:
            logs = _docker_logs_tail(STATUS_CONTAINER_SEARXNG, tail_lines=80)
        except (OSError, ValueError, TypeError) as exc:
            logs = str(exc)
        failure_logs.append(("SearXNG", STATUS_CONTAINER_SEARXNG, logs))

    # Redis (redis-cli ping inside container)
    try:
        redis_ok, redis_detail = _probe_redis_via_docker_exec(STATUS_CONTAINER_REDIS)
    except (OSError, ValueError, TypeError) as exc:
        redis_ok = False
        redis_detail = str(exc)
    probe_redis = f"docker exec {STATUS_CONTAINER_REDIS} redis-cli ping"
    if redis_ok:
        table.add_row("Redis", Text("UP", style="bold green"), probe_redis, redis_detail)
    else:
        table.add_row("Redis", Text("DOWN", style="bold red"), probe_redis, redis_detail)
        try:
            logs = _docker_logs_tail(STATUS_CONTAINER_REDIS, tail_lines=80)
        except (OSError, ValueError, TypeError) as exc:
            logs = str(exc)
        failure_logs.append(("Redis", STATUS_CONTAINER_REDIS, logs))

    # n8n
    try:
        n8_st = _http_get_status(n8n_url, timeout=8.0)
    except (ValueError, TypeError) as exc:
        n8_st = None
        n8_detail = str(exc)
    else:
        n8_detail = f"HTTP {n8_st}" if n8_st is not None else "no response"
    n8_ok = n8_st == 200
    if n8_ok:
        table.add_row("n8n", Text("UP", style="bold green"), n8n_url, n8_detail)
    else:
        table.add_row("n8n", Text("DOWN", style="bold red"), n8n_url, n8_detail)
        try:
            logs = _docker_logs_tail(STATUS_CONTAINER_N8N, tail_lines=80)
        except (OSError, ValueError, TypeError) as exc:
            logs = str(exc)
        failure_logs.append(("n8n", STATUS_CONTAINER_N8N, logs))

    # Ollama (host): /api/tags + model list with sizes
    models_raw: list[dict[str, object]] = []
    ollama_err: str | None = None
    try:
        models_raw, ollama_err = _fetch_ollama_models_and_error(ollama_base, timeout=12.0)
    except (RuntimeError, OSError, TypeError, ValueError) as exc:
        ollama_err = str(exc)
        models_raw = []

    if ollama_err is None:
        table.add_row(
            "Ollama",
            Text("UP", style="bold green"),
            ollama_tags_url,
            f"{len(models_raw)} model(s) listed",
        )
    else:
        detail = ollama_err or "unknown error"
        table.add_row(
            "Ollama",
            Text("DOWN", style="bold red"),
            ollama_tags_url,
            detail,
        )
        try:
            host_logs = _ollama_host_log_snippet(tail_lines=80)
        except (OSError, ValueError, TypeError) as exc:
            host_logs = str(exc)
        failure_logs.append(("Ollama", "(host process)", host_logs))

    console = Console()
    console.print(table)

    if ollama_err is None:
        mt = Table(title="Ollama — pulled models", show_lines=True, header_style="bold")
        mt.add_column("Model", style="cyan")
        mt.add_column("Size", justify="right")
        mt.add_column("Modified", overflow="fold")
        if models_raw:
            for m in models_raw:
                name = str(m.get("name", "") or "?")
                size_val = m.get("size")
                try:
                    size_int = int(size_val) if size_val is not None else 0
                except (TypeError, ValueError):
                    size_int = 0
                size_txt = _format_byte_size(size_int) if size_int >= 0 else "?"
                mod = m.get("modified_at") or m.get("modifiedAt") or ""
                mt.add_row(name, size_txt, str(mod))
        else:
            mt.add_row("(none)", "—", "No models installed yet; run `ollama pull <name>`.")
        console.print(mt)

    for svc_label, cname, snippet in failure_logs:
        title = f"{svc_label} — diagnostics ({cname})"
        try:
            console.print(Panel(snippet, title=title, style="yellow"))
        except (OSError, ValueError, TypeError) as exc:
            _print(f"Warning: could not render log panel for {svc_label}: {exc}")
