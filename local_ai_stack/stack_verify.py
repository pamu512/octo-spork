"""Verify / hook-infra health probes and shared log-tail helpers.

Pure move from ``__main__`` (after #14 stack_bootstrap): function bodies are
byte-identical to their previous definitions in ``__main__``, which re-exports
them for backward compatibility.

This module does not import ``local_ai_stack.__main__`` (even lazily).
Helpers already extracted live in ``env_validation`` / ``stack_bootstrap``.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from local_ai_stack.env_validation import _parse_env_file
from local_ai_stack.stack_bootstrap import (
    _compose_base_args,
    _http_ok,
    _merge_process_env,
    _resolve_agenticseek_path,
    _run,
    _wait_for_http,
)

ROOT = Path(__file__).resolve().parents[1]
STATUS_CONTAINER_SEARXNG = "local-ai-searxng"
STATUS_CONTAINER_REDIS = "local-ai-redis"
STATUS_CONTAINER_N8N = "local-ai-n8n"

# Pre-push hook: map verify labels to docker container names (addons compose).
HOOK_LABEL_TO_CONTAINER: dict[str, str] = {
    "agenticseek-backend": "local-ai-agentic-api",
    "agenticseek-frontend": "local-ai-agentic-ui",
    "open-webui": "local-ai-open-webui",
    "n8n": STATUS_CONTAINER_N8N,
    "searxng": STATUS_CONTAINER_SEARXNG,
}


def _print(message: str) -> None:
    print(message, flush=True)


def _docker_logs_tail(container_name: str, *, tail_lines: int = 80) -> str:
    if container_name is None or not str(container_name).strip():
        raise ValueError("container_name is required")
    name = str(container_name).strip()
    try:
        completed = subprocess.run(
            ["docker", "logs", "--tail", str(int(tail_lines)), name],
            cwd=str(ROOT),
            check=False,
            text=True,
            capture_output=True,
            timeout=90,
        )
    except FileNotFoundError:
        return "docker CLI not found; cannot fetch container logs."
    except subprocess.TimeoutExpired:
        return "docker logs timed out."
    except OSError as exc:
        return f"docker logs OS error: {exc}"
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    combined = "\n".join(part for part in (stdout, stderr) if part)
    if not combined:
        return f"(no log output; docker exit {completed.returncode})"
    max_chars = 16000
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n… (truncated)"
    return combined


def _ollama_host_log_snippet(*, tail_lines: int = 80, max_chars: int = 12000) -> str:
    log_path = Path(tempfile.gettempdir()) / "octo-spork-ollama.log"
    try:
        if not log_path.is_file():
            return (
                f"No host log file at {log_path}. "
                "Ollama runs on the host when started by this stack; check system logs or `ollama serve` output."
            )
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"Could not read Ollama host log {log_path}: {exc}"
    tail = lines[-tail_lines:] if len(lines) > tail_lines else lines
    text = "\n".join(tail)
    if len(text) > max_chars:
        text = text[-max_chars:] + "\n… (truncated)"
    return text


def _hook_probe_n8n_ollama_reachability(
    env_file: Path, env_values: dict[str, str], process_env: dict[str, str]
) -> tuple[bool, str]:
    """Same probe as ``command_verify`` (n8n container → Ollama)."""
    try:
        agenticseek_path = _resolve_agenticseek_path(env_values)
    except (RuntimeError, OSError, ValueError) as exc:
        return False, f"[infra] n8n→Ollama reachability skipped (AgenticSeek path): {exc}"

    check_script = (
        "fetch(process.env.OLLAMA_BASE_URL + '/api/tags')"
        ".then((r)=>{if(!r.ok){throw new Error('HTTP '+r.status)};return r.text();})"
        ".then((t)=>{console.log(t.slice(0, 160));})"
        ".catch((e)=>{console.error(e);process.exit(1);});"
    )
    compose_cmd = _compose_base_args(env_file, agenticseek_path) + ["exec", "-T", "n8n", "node", "-e", check_script]
    try:
        completed = subprocess.run(
            compose_cmd,
            env=process_env,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError:
        return False, "[infra] docker compose not available for n8n reachability check"
    except subprocess.TimeoutExpired:
        return False, "[infra] n8n→Ollama reachability check timed out"
    except OSError as exc:
        return False, f"[infra] n8n→Ollama reachability OS error: {exc}"

    if completed.returncode == 0:
        return True, ""

    detail = (completed.stderr or completed.stdout or "").strip()
    extra = _docker_logs_tail(STATUS_CONTAINER_N8N, tail_lines=48)
    return False, (
        f"[infra] n8n container could not reach Ollama (exit {completed.returncode}). "
        f"stderr/stdout: {detail[:2000]}\n--- docker logs {STATUS_CONTAINER_N8N} ---\n{extra}"
    )


def run_hook_infra_health_probe(env_file: Path) -> tuple[bool, list[str]]:
    """Lightweight single-shot probes mirroring ``command_verify`` (no long backoff)."""
    logs: list[str] = []
    try:
        env_values = _parse_env_file(env_file)
    except (OSError, UnicodeDecodeError) as exc:
        return True, [f"[infra] Could not read env file {env_file}: {exc}"]

    process_env = _merge_process_env(env_values)
    h = "127.0.0.1"
    ollama_local_url = env_values.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434").rstrip("/")
    probes: list[tuple[str, str]] = [
        (f"{ollama_local_url}/api/tags", "ollama"),
        (f"http://{h}:{env_values.get('AGENTIC_BACKEND_PORT', '7777')}/health", "agenticseek-backend"),
        (f"http://{h}:{env_values.get('AGENTIC_FRONTEND_PORT', '3010')}", "agenticseek-frontend"),
        (f"http://{h}:{env_values.get('OPEN_WEBUI_PORT', '3001')}", "open-webui"),
        (f"http://{h}:{env_values.get('N8N_PORT', '5678')}/healthz", "n8n"),
        (f"http://{h}:{env_values.get('SEARXNG_PORT', '8080')}", "searxng"),
    ]

    blocked = False
    for url, label in probes:
        if _http_ok(url, timeout=5):
            continue
        blocked = True
        logs.append(f"[infra] FAILED {label}: HTTP probe did not succeed ({url})")
        container = HOOK_LABEL_TO_CONTAINER.get(label)
        if container:
            logs.append(_docker_logs_tail(container, tail_lines=48))
        elif label == "ollama":
            logs.append(f"--- host Ollama log ---\n{_ollama_host_log_snippet(tail_lines=48)}")

    ok_reach, reach_detail = _hook_probe_n8n_ollama_reachability(env_file, env_values, process_env)
    if not ok_reach:
        blocked = True
        logs.append(reach_detail)

    return blocked, logs


def command_verify(env_file: Path) -> None:
    env_values = _parse_env_file(env_file)
    agenticseek_path = _resolve_agenticseek_path(env_values)
    process_env = _merge_process_env(env_values)
    # Use IPv4 loopback: on some hosts `localhost` resolves to ::1 first while Docker
    # publishes ports on IPv4 only, which yields connection reset during health checks.
    h = "127.0.0.1"

    from local_ai_stack.deep_probe import run_deep_probe_until_ready

    _print("DeepProbe: Ollama /api/tags, Postgres SELECT 1, Redis PING (60s deadline, backoff 1–8s)...")
    run_deep_probe_until_ready(env_values, logger=None, announce=_print, timeout_sec=60.0)

    _print("Checking other service endpoints...")
    _wait_for_http(f"http://{h}:{env_values.get('AGENTIC_BACKEND_PORT', '7777')}/health", "agenticseek-backend")
    _wait_for_http(f"http://{h}:{env_values.get('AGENTIC_FRONTEND_PORT', '3010')}", "agenticseek-frontend")
    _wait_for_http(f"http://{h}:{env_values.get('OPEN_WEBUI_PORT', '3001')}", "open-webui")
    _wait_for_http(f"http://{h}:{env_values.get('N8N_PORT', '5678')}/healthz", "n8n")
    _wait_for_http(f"http://{h}:{env_values.get('SEARXNG_PORT', '8080')}", "searxng")

    _print("Verifying n8n container can reach Ollama...")
    check_script = (
        "fetch(process.env.OLLAMA_BASE_URL + '/api/tags')"
        ".then((r)=>{if(!r.ok){throw new Error('HTTP '+r.status)};return r.text();})"
        ".then((t)=>{console.log(t.slice(0, 160));})"
        ".catch((e)=>{console.error(e);process.exit(1);});"
    )
    compose_cmd = _compose_base_args(env_file, agenticseek_path) + ["exec", "-T", "n8n", "node", "-e", check_script]
    _run(compose_cmd, env=process_env)
    _print("All verification checks passed.")
