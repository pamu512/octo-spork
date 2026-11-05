"""Stack bootstrap / up helpers for ``local_ai_stack up`` and ``bootstrap``.

Pure move from ``__main__`` (after #13 stack_teardown): function bodies are
byte-identical to their previous definitions in ``__main__``, which re-exports
them for backward compatibility.

This module does not import ``local_ai_stack.__main__`` (even lazily).
Helpers that ``stack_orchestrator`` / ``stack_teardown`` still call are bound
onto those modules here (those modules do not import this file or ``__main__``).
"""
from __future__ import annotations

import configparser
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from local_ai_stack.env_validation import (
    _ensure_env_file,
    _parse_env_file,
    _seed_env_secrets,
)
import local_ai_stack.stack_orchestrator as _stack_orchestrator
from local_ai_stack.stack_orchestrator import StackOrchestrator
import local_ai_stack.stack_teardown as _stack_teardown


ROOT = Path(__file__).resolve().parents[1]
REPO_LOCAL_DATA_DIR = ROOT / ".local" / "data"
COMPOSE_PROJECT_NAME = "octo-spork-local-ai"
CLAUDE_CODE_DIR = ROOT / "deploy" / "claude-code"
CLAUDE_AGENT_COMPOSE_FILE = ROOT / "deploy" / "local-ai" / "docker-compose.claude-agent.yml"
PORT_SENTINEL_COMPOSE_FILE = ROOT / "deploy" / "local-ai" / "docker-compose.port-sentinel.override.yml"
PROJECT_LABEL_COMPOSE_FILE = ROOT / "deploy" / "local-ai" / "docker-compose.project-labels.yml"
PATCH_BUNDLE_AGENTICSEEK = ROOT / "patches" / "agenticseek"


def _print(message: str) -> None:
    print(message, flush=True)


def _run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    _print(f"+ {' '.join(args)}")
    return subprocess.run(
        args,
        env=env,
        cwd=str(cwd or ROOT),
        check=check,
        text=True,
    )

def _resolve_agenticseek_path(env: dict[str, str]) -> Path:
    raw = env.get("AGENTICSEEK_DIR", "")
    if not raw:
        raise RuntimeError("AGENTICSEEK_DIR is empty in env file")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path

def _normalize_dockerfile_backend(agenticseek_path: Path, use_native_arm64: bool) -> None:
    dockerfile = agenticseek_path / "Dockerfile.backend"
    if not dockerfile.exists():
        return
    content = dockerfile.read_text(encoding="utf-8")
    updated = re.sub(
        r"^FROM\s+--platform=linux/amd64\s+(.+)$",
        r"FROM \1",
        content,
        count=1,
        flags=re.MULTILINE,
    )

    host_arch = platform.machine().lower()
    if host_arch in {"arm64", "aarch64"} and use_native_arm64:
        replacements = {
            "/linux64/chrome-linux64.zip": "/linux-arm64/chrome-linux-arm64.zip",
            "/opt/chrome-linux64/chrome": "/opt/chrome-linux-arm64/chrome",
            "/linux64/chromedriver-linux64.zip": "/linux-arm64/chromedriver-linux-arm64.zip",
            "/tmp/chromedriver-linux64/chromedriver": "/tmp/chromedriver-linux-arm64/chromedriver",
        }
    else:
        replacements = {
            "/linux-arm64/chrome-linux-arm64.zip": "/linux64/chrome-linux64.zip",
            "/opt/chrome-linux-arm64/chrome": "/opt/chrome-linux64/chrome",
            "/linux-arm64/chromedriver-linux-arm64.zip": "/linux64/chromedriver-linux64.zip",
            "/tmp/chromedriver-linux-arm64/chromedriver": "/tmp/chromedriver-linux64/chromedriver",
        }

    for old, new in replacements.items():
        updated = updated.replace(old, new)

    if updated != content:
        dockerfile.write_text(updated, encoding="utf-8")

def ensure_repo_local_data_dirs() -> None:
    """Create host paths for Docker bind mounts (``<repo>/.local/data``)."""
    for sub in ("redis", "postgres", "open-webui", "n8n"):
        (REPO_LOCAL_DATA_DIR / sub).mkdir(parents=True, exist_ok=True)

def _claude_agent_stack_available() -> bool:
    """True when the Claude Agent compose fragment and ``claude-code`` Dockerfile exist."""
    return (
        (CLAUDE_CODE_DIR / "Dockerfile").is_file()
        and CLAUDE_AGENT_COMPOSE_FILE.is_file()
    )

def _ensure_claude_config_dir() -> None:
    """Host directory mounted into the Claude Agent container for ``.env`` / config."""
    cfg = ROOT / ".local" / "claude_config"
    cfg.mkdir(parents=True, exist_ok=True)
    env_file = cfg / ".env"
    if not env_file.is_file():
        env_file.write_text(
            "# Octo-spork Claude Code agent — strict tools by default (see claude_bridge.permission_policy).\n"
            "OCTO_CLAUDE_ALLOWED_TOOLS=Read,Grep,Glob\n",
            encoding="utf-8",
        )

def _configure_agenticseek_ini(agenticseek_path: Path, env: dict[str, str]) -> None:
    config_path = agenticseek_path / "config.ini"
    if not config_path.exists():
        return
    ollama_base = env.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    ollama_model = env.get("OLLAMA_MODEL", "qwen2.5:14b")
    provider_address = ollama_base.replace("http://", "").replace("https://", "").rstrip("/")

    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8")
    if "MAIN" not in config:
        config["MAIN"] = {}
    if "BROWSER" not in config:
        config["BROWSER"] = {}
    config["MAIN"]["is_local"] = "True"
    config["MAIN"]["provider_name"] = "ollama"
    config["MAIN"]["provider_model"] = ollama_model
    config["MAIN"]["provider_server_address"] = provider_address
    config["BROWSER"]["headless_browser"] = "True"

    with config_path.open("w", encoding="utf-8") as handle:
        config.write(handle)

def _compose_base_args(env_file: Path, agenticseek_path: Path) -> list[str]:
    args: list[str] = [
        "docker",
        "compose",
        "--project-name",
        COMPOSE_PROJECT_NAME,
        "--env-file",
        str(env_file),
        "-f",
        str(agenticseek_path / "docker-compose.yml"),
        "-f",
        str(ROOT / "deploy" / "local-ai" / "docker-compose.addons.yml"),
    ]
    if PROJECT_LABEL_COMPOSE_FILE.is_file():
        args.extend(["-f", str(PROJECT_LABEL_COMPOSE_FILE)])
    if _claude_agent_stack_available():
        args.extend(["-f", str(CLAUDE_AGENT_COMPOSE_FILE)])
    try:
        from local_ai_stack.resource_hardener import compose_files_should_include_override

        op = compose_files_should_include_override(ROOT)
    except Exception:
        op = None
    if op is not None:
        args.extend(["-f", str(op)])
    if PORT_SENTINEL_COMPOSE_FILE.is_file():
        args.extend(["-f", str(PORT_SENTINEL_COMPOSE_FILE)])
    return args

def _merge_process_env(env_file_values: dict[str, str]) -> dict[str, str]:
    merged = os.environ.copy()
    merged.update(env_file_values)
    # Do not set DOCKER_DEFAULT_PLATFORM here: it applies to every compose service and can
    # break pulls (e.g. Valkey) when a wrong-arch image is cached. AgenticSeek backend +
    # frontend use `platform: linux/amd64` in docker-compose.addons.yml on Apple Silicon.
    return merged

def _orchestrator_log_path(root: Path) -> Path:
    return root / "logs" / "orchestrator.log"

def _configure_orchestrator_file_logger(root: Path) -> logging.Logger:
    log_name = "local_ai_stack.orchestrator"
    logger = logging.getLogger(log_name)
    logger.setLevel(logging.DEBUG)
    for existing in list(logger.handlers):
        try:
            logger.removeHandler(existing)
        except (OSError, ValueError) as exc:
            print(f"Warning: could not remove log handler: {exc}", file=sys.stderr, flush=True)
    log_path = _orchestrator_log_path(root)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8", mode="a")
    except OSError as exc:
        print(f"Error: could not open orchestrator log file: {exc}", file=sys.stderr, flush=True)
        raise RuntimeError(f"Failed to create orchestrator log at {log_path.parent}") from exc
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    try:
        logger.addHandler(file_handler)
    except (OSError, TypeError) as exc:
        try:
            file_handler.close()
        except OSError:
            pass
        print(f"Error: could not attach log handler: {exc}", file=sys.stderr, flush=True)
        raise RuntimeError("Failed to configure orchestrator logger") from exc
    logger.propagate = False
    return logger

def _http_ok(url: str, timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return 200 <= response.status < 400
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False

def _start_ollama_if_needed(env: dict[str, str], process_env: dict[str, str]) -> None:
    ollama_local_url = env.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434").rstrip("/")
    probe_url = f"{ollama_local_url}/api/tags"
    if _http_ok(probe_url, timeout=3):
        return

    ollama_host = env.get("OLLAMA_HOST", "0.0.0.0:11434")
    _print(f"Starting Ollama on host ({ollama_host})...")
    start_env = process_env.copy()
    start_env["OLLAMA_HOST"] = ollama_host
    log_path = Path(tempfile.gettempdir()) / "octo-spork-ollama.log"
    with log_path.open("a", encoding="utf-8") as log_handle:
        subprocess.Popen(
            ["ollama", "serve"],
            env=start_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    time.sleep(3)
    if not _http_ok(probe_url, timeout=5):
        raise RuntimeError("Ollama did not become healthy after startup attempt")

def _wait_for_http(url: str, label: str, attempts: int = 60, sleep_seconds: int = 2) -> None:
    for _ in range(attempts):
        if _http_ok(url, timeout=5):
            _print(f"{label}: ok")
            return
        time.sleep(sleep_seconds)
    raise RuntimeError(f"{label}: failed after {attempts} attempts ({url})")

def bootstrap(env_file: Path) -> tuple[dict[str, str], Path]:
    _ensure_env_file(env_file)
    _seed_env_secrets(env_file)
    env_values = _parse_env_file(env_file)

    agenticseek_path = _resolve_agenticseek_path(env_values)
    agenticseek_path.parent.mkdir(parents=True, exist_ok=True)
    ref = env_values.get("AGENTICSEEK_REF", "main")
    if (agenticseek_path / ".git").is_dir():
        _run(["git", "-C", str(agenticseek_path), "fetch", "--depth", "1", "origin", ref])
        _run(["git", "-C", str(agenticseek_path), "checkout", "-f", ref])
        _run(["git", "-C", str(agenticseek_path), "pull", "--ff-only", "origin", ref])
    else:
        _run(["git", "clone", "--depth", "1", "--branch", ref, "https://github.com/Fosowl/agenticSeek.git", str(agenticseek_path)])

    ensure_repo_local_data_dirs()
    if _claude_agent_stack_available():
        _ensure_claude_config_dir()

    try:
        from local_ai_stack.patch_manager import PatchConflictError, PatchManager

        pm = PatchManager(PATCH_BUNDLE_AGENTICSEEK, ROOT)
        pm.apply(agenticseek_path, progress=_print)
    except PatchConflictError as exc:
        raise RuntimeError(
            "AgenticSeek overlay patches failed to apply (upstream repo likely changed). "
            "See patches/agenticseek/, regenerate unified diffs from the new upstream files, "
            "or pin AGENTICSEEK_REF to a matching revision.\n"
            + str(exc)
        ) from exc

    use_native_arm64 = env_values.get("AGENTICSEEK_NATIVE_ARM64", "false").lower() == "true"
    _normalize_dockerfile_backend(agenticseek_path, use_native_arm64)
    _configure_agenticseek_ini(agenticseek_path, env_values)

    _print(f"AgenticSeek is ready at: {agenticseek_path}")
    _print(f"Environment file: {env_file}")
    return env_values, agenticseek_path


# Bind CLI helpers the extracted class still calls. No back-import from
# stack_orchestrator / stack_teardown into this module or into __main__
# (same fence as #11 port_plan / #12 / #13).
_stack_orchestrator.bootstrap = bootstrap
_stack_orchestrator._run = _run
_stack_orchestrator._compose_base_args = _compose_base_args
_stack_orchestrator._merge_process_env = _merge_process_env
_stack_orchestrator._start_ollama_if_needed = _start_ollama_if_needed
_stack_orchestrator._claude_agent_stack_available = _claude_agent_stack_available

# Same fence for stack_teardown: no back-import; bind helpers it still calls.
_stack_teardown._compose_base_args = _compose_base_args
_stack_teardown._merge_process_env = _merge_process_env
_stack_teardown._resolve_agenticseek_path = _resolve_agenticseek_path


def command_up(env_file: Path, *, rewrite_conflicting_ports: bool = True) -> None:
    try:
        orchestrator_logger = _configure_orchestrator_file_logger(ROOT)
    except RuntimeError:
        raise
    except Exception as exc:
        print(f"Error: failed to initialize orchestrator logging: {exc}", file=sys.stderr, flush=True)
        raise RuntimeError("Orchestrator logging initialization failed") from exc
    try:
        StackOrchestrator(
            ROOT,
            env_file,
            orchestrator_logger,
            rewrite_conflicting_ports=rewrite_conflicting_ports,
        ).execute()
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError):
        raise
    except Exception as exc:
        try:
            orchestrator_logger.exception("Unexpected failure in StackOrchestrator.execute")
        except (OSError, RuntimeError, AttributeError) as log_exc:
            print(f"Error: could not write orchestrator log: {log_exc}", file=sys.stderr, flush=True)
        raise RuntimeError("Stack orchestration failed due to an unexpected error.") from exc
