from __future__ import annotations

import argparse
import configparser
import importlib.util
import ipaddress
import json
import logging
import shutil
import os
import platform
import re
import secrets
import shlex
import stat
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


ROOT = Path(__file__).resolve().parents[1]
REPO_LOCAL_DATA_DIR = ROOT / ".local" / "data"
DEFAULT_ENV_FILE = ROOT / "deploy" / "local-ai" / ".env.local"
EXAMPLE_ENV_FILE = ROOT / "deploy" / "local-ai" / ".env.example"
COMPOSE_PROJECT_NAME = "octo-spork-local-ai"
# Applied via docker-compose.project-labels.yml for ``force-clean`` discovery.
OCTO_SPORK_PROJECT_LABEL_KEY = "com.octospork.project"
GROUNDED_REVIEW_TEMP_CLONES_DIR = ROOT / ".local" / "temp_clones"
CLAUDE_CODE_DIR = ROOT / "deploy" / "claude-code"
CLAUDE_AGENT_COMPOSE_FILE = ROOT / "deploy" / "local-ai" / "docker-compose.claude-agent.yml"
PORT_SENTINEL_COMPOSE_FILE = ROOT / "deploy" / "local-ai" / "docker-compose.port-sentinel.override.yml"
PROJECT_LABEL_COMPOSE_FILE = ROOT / "deploy" / "local-ai" / "docker-compose.project-labels.yml"
STATUS_CONTAINER_SEARXNG = "local-ai-searxng"
STATUS_CONTAINER_REDIS = "local-ai-redis"
STATUS_CONTAINER_N8N = "local-ai-n8n"
OVERLAY_SOURCE = ROOT / "overlays" / "agenticseek" / "sources" / "grounded_review.py"
REVIEW_TICKET_EXPORT_SOURCE = ROOT / "overlays" / "agenticseek" / "sources" / "review_ticket_export.py"
PATCH_BUNDLE_AGENTICSEEK = ROOT / "patches" / "agenticseek"

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


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _pytest_available() -> bool:
    """True if ``pytest`` is on PATH or ``python -m pytest`` works."""
    if shutil.which("pytest"):
        return True
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _codeql_cli_available() -> bool:
    return bool(shutil.which("codeql"))


def _trivy_cli_available() -> bool:
    return bool(shutil.which("trivy"))


def _missing_scan_dev_dependencies(*, require_trivy: bool) -> list[str]:
    """Return human-readable labels for missing tools (pytest, trivy, codeql)."""
    missing: list[str] = []
    if not _pytest_available():
        missing.append("pytest (`pip install pytest` or ensure `python -m pytest --version` works)")
    if require_trivy and not _trivy_cli_available():
        missing.append("trivy CLI (https://aquasecurity.github.io/trivy/latest/getting-started/installation/)")
    if not _codeql_cli_available():
        missing.append("codeql CLI (https://github.com/github/codeql-cli-binaries)")
    return missing


def _print_environment_incomplete(missing: list[str]) -> None:
    lines = [
        "",
        "Environment Incomplete",
        "",
        "Required dev dependencies are missing:",
        *[f"  - {item}" for item in missing],
        "",
        "Install the missing packages, ensure they are on PATH, then retry.",
        "To bypass this check (not recommended): export OCTO_SKIP_DEV_DEP_CHECK=1",
        "",
    ]
    print("\n".join(lines), file=sys.stderr)


def _ensure_scan_dev_dependencies(args: argparse.Namespace) -> None:
    """
    Pre-flight for scan / remediation commands: require pytest, CodeQL CLI, and usually Trivy.

    Honors ``OCTO_SKIP_DEV_DEP_CHECK=1``. For ``pre-push-scan``, Trivy is not required when
    ``--skip-trivy`` is set.
    """
    if _truthy_env("OCTO_SKIP_DEV_DEP_CHECK"):
        return
    cmd = getattr(args, "command", None)
    if cmd not in {"pre-push-scan", "review-diff", "remediation-ui", "benchmark"}:
        return
    require_trivy = True
    if cmd == "pre-push-scan":
        require_trivy = not bool(getattr(args, "skip_trivy", False))
    missing = _missing_scan_dev_dependencies(require_trivy=require_trivy)
    if missing:
        _print_environment_incomplete(missing)
        sys.exit(2)


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


def _parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key] = value
    return env


def _ensure_env_file(env_file: Path) -> None:
    if env_file.exists():
        return
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(EXAMPLE_ENV_FILE.read_text(encoding="utf-8"), encoding="utf-8")


def _parse_env_example_keys_ordered(example_path: Path) -> list[str]:
    if example_path is None:
        raise ValueError("example_path is required")
    try:
        lines = example_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Could not read example env file: {example_path}") from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Example env file is not valid UTF-8: {example_path}") from exc
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key_part, sep, _rest = raw_line.partition("=")
        key_name = key_part.strip()
        if not key_name or key_name in seen:
            continue
        seen.add(key_name)
        ordered.append(key_name)
    return ordered


def _env_url_keys() -> frozenset[str]:
    return frozenset(
        {
            "OLLAMA_BASE_URL",
            "OLLAMA_LOCAL_URL",
            "REACT_APP_BACKEND_URL",
        }
    )


def _env_boolean_keys() -> frozenset[str]:
    return frozenset(
        {
            "AGENTICSEEK_NATIVE_ARM64",
            "GROUNDED_REVIEW_ENABLE_TWO_PASS",
            "GROUNDED_REVIEW_STRICT_COVERAGE",
        }
    )


def _env_unsigned_int_keys() -> frozenset[str]:
    return frozenset(
        {
            "BROWSER_COMMAND_TIMEOUT",
            "GROUNDED_REVIEW_CACHE_TTL_SECONDS",
            "GROUNDED_REVIEW_ANSWER_CACHE_TTL_SECONDS",
            "GROUNDED_REVIEW_MAX_FILES",
            "GROUNDED_REVIEW_MAX_TOTAL_BYTES",
            "GROUNDED_REVIEW_MAX_FILE_BYTES",
            "GROUNDED_REVIEW_NUM_CTX",
            "GROUNDED_REVIEW_NUM_CTX_TWO_PASS",
        }
    )


def _env_port_suffix_key(key: str) -> bool:
    if not key:
        return False
    return key.endswith("_PORT") or key == "REDIS_PORT"


def _validate_http_url_field(key: str, value: str) -> str | None:
    if value is None:
        return f"{key} value is missing"
    raw = str(value).strip()
    if not raw:
        return f"{key} is empty (expected an http(s) URL)"
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        return f"{key} is not a valid URL: {exc}"
    if parsed.scheme not in {"http", "https"}:
        return f"{key} must use http or https scheme, got {parsed.scheme!r}"
    if not parsed.netloc:
        return f"{key} URL has no host component: {raw!r}"
    return None


def _validate_ollama_host_binding(key: str, value: str) -> str | None:
    if value is None:
        return f"{key} value is missing"
    raw = str(value).strip()
    if not raw:
        return f"{key} is empty (expected host:port, e.g. 0.0.0.0:11434)"
    host_part: str
    port_part: str
    try:
        if raw.startswith("["):
            end_bracket = raw.find("]")
            if end_bracket < 0:
                return f"{key} has invalid IPv6 bracket syntax: {raw!r}"
            host_inside = raw[1:end_bracket].strip()
            rest = raw[end_bracket + 1 :].strip()
            if not rest.startswith(":"):
                return f"{key} must use [ipv6]:port form: {raw!r}"
            port_part = rest[1:].strip()
            host_part = host_inside
        else:
            if ":" not in raw:
                return f"{key} must be host:port (got {raw!r})"
            host_part, port_part = raw.rsplit(":", 1)
            host_part = host_part.strip()
            port_part = port_part.strip()
        if not host_part:
            return f"{key} host part is empty: {raw!r}"
        try:
            port_int = int(port_part, 10)
        except ValueError:
            return f"{key} port is not an integer: {port_part!r}"
        if port_int < 1 or port_int > 65535:
            return f"{key} port must be between 1 and 65535, got {port_int}"
        try:
            ipaddress.ip_address(host_part)
        except ValueError:
            allowed = re.compile(r"^[A-Za-z0-9._-]+$")
            if not allowed.match(host_part):
                return f"{key} host {host_part!r} is not a valid IP or hostname pattern"
    except (TypeError, AttributeError) as exc:
        return f"{key} could not be parsed: {exc}"
    return None


def _validate_positive_int_field(key: str, value: str, *, allow_zero: bool = False) -> str | None:
    if value is None:
        return f"{key} value is missing"
    raw = str(value).strip()
    if not raw:
        return f"{key} is empty (expected an integer)"
    try:
        n = int(raw, 10)
    except ValueError:
        return f"{key} must be an integer, got {value!r}"
    if allow_zero:
        if n < 0:
            return f"{key} must be a non-negative integer, got {n}"
    else:
        if n < 1:
            return f"{key} must be a positive integer, got {n}"
    return None


def _validate_boolean_field(key: str, value: str) -> str | None:
    if value is None:
        return f"{key} value is missing"
    raw = str(value).strip().lower()
    if raw not in {"true", "false", "1", "0", "yes", "no"}:
        return f"{key} must be true/false (or 1/0, yes/no), got {value!r}"
    return None


def _stack_managed_secret_keys() -> frozenset[str]:
    """Keys for which we offer random generation (stack-local secrets, not third-party API tokens)."""
    return frozenset({"SEARXNG_SECRET_KEY", "N8N_ENCRYPTION_KEY"})


def _value_is_placeholder_secret(key: str, value: str) -> bool:
    raw = str(value).strip() if value is not None else ""
    lower = raw.lower()
    if "replace-me" in lower or "changeme" in lower or lower == "todo":
        return True
    if key in _stack_managed_secret_keys():
        return not raw
    return False


def _generate_secret_hex(byte_length: int = 24) -> str:
    if byte_length < 8:
        raise ValueError("byte_length must be at least 8")
    return secrets.token_hex(int(byte_length))


def _coerce_non_placeholder_stack_secret(key_name: str, candidate: str) -> str:
    if key_name not in _stack_managed_secret_keys():
        return candidate
    if _value_is_placeholder_secret(key_name, candidate):
        fresh = _generate_secret_hex(24)
        _print(f"{key_name}: still empty or placeholder after input; generated a new random value.")
        return fresh
    return candidate


def _merge_values_into_env_file(env_file: Path, merged: dict[str, str]) -> None:
    if env_file is None:
        raise ValueError("env_file is required")
    path = Path(env_file).resolve()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read env file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Env file is not valid UTF-8: {path}") from exc
    lines = raw.splitlines()
    seen_keys: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key_name = line.split("=", 1)[0].strip()
            if key_name in merged:
                new_lines.append(f"{key_name}={merged[key_name]}")
                seen_keys.add(key_name)
                continue
        new_lines.append(line)
    for key_name in sorted(merged.keys()):
        if key_name not in seen_keys:
            new_lines.append(f"{key_name}={merged[key_name]}")
    out = "\n".join(new_lines) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(out, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not write env file: {path}") from exc


def _prompt_line(message: str, default: str | None = None) -> str:
    try:
        if default is not None and str(default).strip() != "":
            raw = input(f"{message} [{default}]: ").strip()
            return raw if raw else str(default)
        raw = input(f"{message}: ").strip()
        return raw
    except EOFError as exc:
        raise RuntimeError(
            "Interactive input ended unexpectedly (EOF). Use --no-interactive in CI or "
            "provide a complete .env.local."
        ) from exc


def _prompt_secret_value(key: str, example_default: str) -> str:
    try:
        _print(
            f"Secret or sensitive value for {key}. "
            "[Enter]=generate a random value, or paste your own value."
        )
        raw = input(f"{key} [{example_default}]: ").strip()
    except EOFError as exc:
        raise RuntimeError(
            "Interactive input ended while prompting for a secret. "
            "Use --no-interactive or set the variable in the env file."
        ) from exc
    if not raw:
        return _generate_secret_hex(24)
    return raw


def _collect_env_validation_errors(env_map: dict[str, str]) -> list[str]:
    if env_map is None:
        raise ValueError("env_map is required")
    errors: list[str] = []
    url_keys = _env_url_keys()
    bool_keys = _env_boolean_keys()
    uint_keys = _env_unsigned_int_keys()
    for key, value in env_map.items():
        try:
            if key in url_keys:
                msg = _validate_http_url_field(key, value)
                if msg:
                    errors.append(msg)
            elif key == "OLLAMA_HOST":
                msg = _validate_ollama_host_binding(key, value)
                if msg:
                    errors.append(msg)
            elif key in bool_keys:
                msg = _validate_boolean_field(key, value)
                if msg:
                    errors.append(msg)
            elif key in uint_keys:
                msg = _validate_positive_int_field(key, value, allow_zero=True)
                if msg:
                    errors.append(msg)
            elif _env_port_suffix_key(key):
                msg = _validate_positive_int_field(key, value, allow_zero=False)
                if msg:
                    errors.append(msg)
        except (TypeError, ValueError) as exc:
            errors.append(f"{key}: validation error: {exc}")
    return errors


def validate_config(
    env_file: Path | None = None,
    example_file: Path | None = None,
    *,
    interactive: bool = True,
) -> dict[str, str]:
    """
    Validate ``.env.local`` against ``.env.example``: required keys, URL/bind/port formats,
    and optional interactive prompts to fill missing keys or rotate placeholder secrets.
    """
    target_env = Path(env_file).resolve() if env_file is not None else DEFAULT_ENV_FILE.resolve()
    target_example = Path(example_file).resolve() if example_file is not None else EXAMPLE_ENV_FILE.resolve()

    if not target_example.is_file():
        raise RuntimeError(f"Example env file not found: {target_example}")

    try:
        _ensure_env_file(target_env)
    except OSError as exc:
        raise RuntimeError(f"Could not ensure env file exists at {target_env}") from exc

    try:
        example_ordered_keys = _parse_env_example_keys_ordered(target_example)
    except RuntimeError:
        raise
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Failed to parse example env keys") from exc

    try:
        example_map = _parse_env_file(target_example)
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read example env file: {target_example}") from exc

    max_passes = 32
    merged: dict[str, str] = {}
    for _ in range(max_passes):
        try:
            merged = _parse_env_file(target_env)
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"Could not read env file: {target_env}") from exc

        example_key_set = set(example_ordered_keys)
        missing_keys = [key for key in example_ordered_keys if key not in merged]
        placeholder_keys = [
            key
            for key, value in merged.items()
            if key in example_key_set and _value_is_placeholder_secret(key, value)
        ]

        updates_made = False
        if interactive and missing_keys:
            _print(f"Configuration sync: {len(missing_keys)} key(s) from {target_example.name} are missing in {target_env.name}.")
            for key_name in missing_keys:
                example_val = example_map.get(key_name, "")
                if key_name in _stack_managed_secret_keys():
                    try:
                        new_val = _prompt_secret_value(key_name, "<random>")
                        new_val = _coerce_non_placeholder_stack_secret(key_name, new_val)
                    except RuntimeError:
                        raise
                    except (OSError, TypeError) as exc:
                        raise RuntimeError(f"Could not collect secret for {key_name}") from exc
                else:
                    try:
                        new_val = _prompt_line(
                            f"Enter value for {key_name}",
                            default=example_val if example_val is not None else "",
                        )
                    except RuntimeError:
                        raise
                    except (OSError, TypeError) as exc:
                        raise RuntimeError(f"Could not read input for {key_name}") from exc
                merged[key_name] = new_val
                updates_made = True

        if interactive and placeholder_keys:
            _print(f"Placeholder secrets detected for: {', '.join(sorted(placeholder_keys))}")
            for key_name in sorted(set(placeholder_keys)):
                try:
                    _print(f"Replace placeholder for {key_name}? [Enter]=generate / Or type new value")
                    new_val = _prompt_secret_value(key_name, "<random>")
                    new_val = _coerce_non_placeholder_stack_secret(key_name, new_val)
                except RuntimeError:
                    raise
                except (OSError, TypeError) as exc:
                    raise RuntimeError(f"Could not update placeholder for {key_name}") from exc
                merged[key_name] = new_val
                updates_made = True

        if updates_made:
            try:
                _merge_values_into_env_file(target_env, merged)
            except RuntimeError:
                raise
            except OSError as exc:
                raise RuntimeError(f"Could not persist env updates to {target_env}") from exc
            _print(f"Updated {target_env}")
            continue

        blocking: list[str] = []
        if missing_keys:
            blocking.append(
                f"Missing keys relative to {target_example.name}: {', '.join(missing_keys)}"
            )
        if placeholder_keys:
            blocking.append(
                "Unresolved placeholder or empty stack secrets: "
                f"{', '.join(sorted(set(placeholder_keys)))}"
            )

        field_errors = _collect_env_validation_errors(merged)
        all_errors = blocking + field_errors
        if all_errors:
            detail = "\n".join(f"  - {item}" for item in all_errors)
            raise RuntimeError(f"Configuration validation failed:\n{detail}")

        _print(f"Configuration OK: {target_env} matches {target_example.name} and validation rules.")
        return merged

    raise RuntimeError(
        "validate_config stopped after too many interactive passes; "
        "fix .env.local manually and retry."
    )


def _seed_env_secrets(env_file: Path) -> None:
    lines = env_file.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    for line in lines:
        if line.startswith("SEARXNG_SECRET_KEY=") and line.endswith("replace-me-with-a-random-secret"):
            updated.append(f"SEARXNG_SECRET_KEY={secrets.token_hex(24)}")
        elif line.startswith("N8N_ENCRYPTION_KEY=") and line.endswith("replace-me-with-a-random-secret"):
            updated.append(f"N8N_ENCRYPTION_KEY={secrets.token_hex(24)}")
        else:
            updated.append(line)
    env_file.write_text("\n".join(updated) + "\n", encoding="utf-8")


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


def _run_subprocess_allow_fail(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> int:
    try:
        _print(f"+ {' '.join(args)}")
        completed = subprocess.run(
            args,
            cwd=str(cwd or ROOT),
            env=env,
            check=False,
            text=True,
            capture_output=True,
            timeout=int(timeout),
        )
    except FileNotFoundError:
        _print("Warning: executable not found while running: " + " ".join(args))
        return 127
    except subprocess.TimeoutExpired:
        _print(f"Warning: command timed out after {timeout}s: {' '.join(args)}")
        return 124
    except OSError as exc:
        _print(f"Warning: OS error running {' '.join(args)}: {exc}")
        return 1
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        _print(f"Warning: exit {completed.returncode} from {' '.join(args)} — {err}")
    return completed.returncode


def _docker_volume_prune_project(project_name: str) -> None:
    label_filter = f"label=com.docker.compose.project={project_name}"
    args = ["docker", "volume", "prune", "-f", "--filter", label_filter]
    try:
        rc = _run_subprocess_allow_fail(args, timeout=600)
        if rc == 0:
            _print(f"Pruned unused Docker volumes labeled com.docker.compose.project={project_name!r}.")
    except (OSError, ValueError, TypeError) as exc:
        _print(f"Warning: volume prune failed unexpectedly: {exc}")


def _docker_network_ids_with_project_label(project_name: str) -> list[str]:
    args = [
        "docker",
        "network",
        "ls",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={project_name}",
    ]
    try:
        completed = subprocess.run(
            args,
            cwd=str(ROOT),
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _print(f"Warning: could not list labeled Docker networks: {exc}")
        return []
    if completed.returncode != 0:
        err = (completed.stderr or "").strip()
        _print(f"Warning: docker network ls failed: {err}")
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _docker_network_remove_by_id(network_id: str, human_name: str | None = None) -> None:
    if not network_id or not str(network_id).strip():
        raise ValueError("network_id is empty")
    nid = str(network_id).strip()
    label = human_name or nid
    try:
        rm = subprocess.run(
            ["docker", "network", "rm", nid],
            cwd=str(ROOT),
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _print(f"Warning: could not remove Docker network {label!r}: {exc}")
        return
    if rm.returncode == 0:
        _print(f"Removed Docker network {label!r} ({nid}).")
        return
    combined = ((rm.stderr or "") + (rm.stdout or "")).lower()
    if "no such network" in combined or "not found" in combined:
        return
    err = (rm.stderr or rm.stdout or "").strip()
    _print(f"Warning: docker network rm failed for {label!r}: {err}")


def _docker_network_remove_labeled_project(project_name: str) -> None:
    ids = _docker_network_ids_with_project_label(project_name)
    if not ids:
        _print("No Docker networks found with compose project label (already clean or none created).")
        return
    _print(f"Removing {len(ids)} Docker network(s) labeled for project {project_name!r}...")
    for nid in ids:
        try:
            _docker_network_remove_by_id(nid, human_name=f"<labeled:{nid[:12]}>")
        except ValueError as exc:
            _print(f"Warning: invalid network id skipped: {exc}")


def _docker_network_map_name_by_id() -> dict[str, str]:
    try:
        completed = subprocess.run(
            ["docker", "network", "ls", "--format", "{{.ID}}\t{{.Name}}"],
            cwd=str(ROOT),
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _print(f"Warning: could not list Docker networks for stray cleanup: {exc}")
        return {}
    if completed.returncode != 0:
        return {}
    result: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        nid, name = parts[0].strip(), parts[1].strip()
        if nid and name:
            result[nid] = name
    return result


def _docker_network_remove_strays_by_name_prefix(project_name: str) -> None:
    prefix = f"{project_name}_"
    network_map = _docker_network_map_name_by_id()
    if not network_map:
        return
    bridge_builtin = {"bridge", "host", "none"}
    _print(f"Checking for stray Docker networks named {prefix!r}* ...")
    for nid, net_name in network_map.items():
        try:
            if net_name in bridge_builtin:
                continue
            if not net_name.startswith(prefix):
                continue
            _docker_network_remove_by_id(nid, human_name=net_name)
        except (OSError, ValueError) as exc:
            _print(f"Warning: stray network cleanup step failed for {net_name!r}: {exc}")


def _cleanup_grounded_review_temp_clones(base_dir: Path | None = None) -> None:
    root_dir = Path(base_dir).resolve() if base_dir is not None else GROUNDED_REVIEW_TEMP_CLONES_DIR.resolve()
    try:
        if not root_dir.exists():
            _print(f"No grounded-review temp dir at {root_dir} (nothing to clean).")
            return
        if not root_dir.is_dir():
            _print(f"Warning: expected directory at {root_dir}; skipping temp clone cleanup.")
            return
    except OSError as exc:
        _print(f"Warning: could not stat temp clones dir {root_dir}: {exc}")
        return
    removed_items = 0
    try:
        for child in sorted(root_dir.iterdir(), key=lambda p: str(p)):
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed_items += 1
            except OSError as exc:
                _print(f"Warning: could not remove {child}: {exc}")
    except OSError as exc:
        _print(f"Warning: could not iterate temp clones dir {root_dir}: {exc}")
        return
    if removed_items:
        _print(f"Removed {removed_items} path(s) under {root_dir}.")
    else:
        _print(f"Grounded-review temp dir is empty: {root_dir}")


def _compose_down_stack(
    env_file: Path,
    agenticseek_path: Path,
    process_env: dict[str, str],
) -> None:
    compose_cmd = _compose_base_args(env_file, agenticseek_path) + [
        "--profile",
        "full",
        "--profile",
        "addons",
        "down",
        "--remove-orphans",
    ]
    try:
        _print(f"+ {' '.join(compose_cmd)}")
        completed = subprocess.run(
            compose_cmd,
            cwd=str(ROOT),
            env=process_env,
            check=False,
            text=True,
            capture_output=True,
            timeout=600,
        )
    except FileNotFoundError:
        _print("Warning: docker compose not found; skipping compose down.")
        return
    except subprocess.TimeoutExpired:
        _print("Warning: compose down timed out; continuing with volume/network cleanup.")
        return
    except OSError as exc:
        _print(f"Warning: compose down failed ({exc}); continuing with cleanup.")
        return
    if completed.returncode == 0:
        _print(f"Compose stack {COMPOSE_PROJECT_NAME!r} stopped (containers for declared compose files).")
        return
    err_out = (completed.stderr or completed.stdout or "").strip()
    _print(
        f"Warning: compose down exited with {completed.returncode}; continuing with volume/network cleanup. "
        f"{err_out}"
    )


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


def _parse_ollama_local_url(url: str) -> tuple[str, int]:
    if not url or not str(url).strip():
        raise ValueError("OLLAMA_LOCAL_URL is empty")
    raw = str(url).strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid Ollama URL: {url!r}") from exc
    if not parsed.hostname:
        raise ValueError(f"OLLAMA_LOCAL_URL has no host: {url!r}")
    if parsed.port is not None:
        return parsed.hostname, int(parsed.port)
    try:
        return parsed.hostname, 11434
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Could not determine Ollama port from: {url!r}") from exc


def _parse_positive_int_from_env(value: str | None, key_name: str, default: int) -> int:
    if value is None or not str(value).strip():
        return int(default)
    try:
        n = int(str(value).strip(), 10)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key_name} must be a positive integer, got {value!r}") from exc
    if n < 1 or n > 65535:
        raise ValueError(f"{key_name} must be between 1 and 65535, got {n}")
    return n


def _tcp_port_is_in_use(host: str, port: int, connect_timeout: float = 0.4) -> bool:
    if port < 1 or port > 65535:
        raise ValueError(f"port out of range: {port}")
    if not host or not str(host).strip():
        raise ValueError("host is empty")
    host_s = str(host).strip()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(float(connect_timeout))
        except (TypeError, ValueError) as exc:
            raise ValueError("connect_timeout must be a finite number") from exc
        try:
            result = sock.connect_ex((host_s, int(port)))
        except OSError as exc:
            return False
        return result == 0
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _http_get_status(url: str, timeout: float = 5.0) -> int | None:
    if not url or not str(url).strip():
        raise ValueError("url is empty")
    try:
        request = urllib.request.Request(
            str(url).strip(),
            method="GET",
            headers={"User-Agent": "local-ai-stack-orchestrator/1.0"},
        )
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:  # noqa: S310
            code = getattr(response, "status", None)
            if code is None:
                code = response.getcode()
            return int(code)
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code)
        except (TypeError, ValueError):
            return None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, TypeError):
        return None


def _find_alternative_host_port(host: str, start_candidate: int, max_attempts: int = 96) -> int:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    end = min(65535, int(start_candidate) + int(max_attempts))
    for candidate in range(int(start_candidate), end + 1):
        if candidate < 1 or candidate > 65535:
            continue
        if not _tcp_port_is_in_use(host, candidate):
            return candidate
    raise RuntimeError(f"No free TCP port found on {host!r} starting near {start_candidate}")


def _stack_port_plan(env_values: dict[str, str]) -> list[tuple[str, int]]:
    fe = _parse_positive_int_from_env(
        env_values.get("AGENTIC_FRONTEND_PORT"),
        "AGENTIC_FRONTEND_PORT",
        3010,
    )
    be = _parse_positive_int_from_env(
        env_values.get("AGENTIC_BACKEND_PORT"),
        "AGENTIC_BACKEND_PORT",
        7777,
    )
    sx = _parse_positive_int_from_env(env_values.get("SEARXNG_PORT"), "SEARXNG_PORT", 8080)
    ow = _parse_positive_int_from_env(
        env_values.get("OPEN_WEBUI_PORT"),
        "OPEN_WEBUI_PORT",
        3001,
    )
    n8 = _parse_positive_int_from_env(env_values.get("N8N_PORT"), "N8N_PORT", 5678)
    _, ollama_port = _parse_ollama_local_url(
        env_values.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434"),
    )
    return [
        ("AGENTIC_FRONTEND_PORT", fe),
        ("AGENTIC_BACKEND_PORT", be),
        ("SEARXNG_PORT", sx),
        ("OPEN_WEBUI_PORT", ow),
        ("N8N_PORT", n8),
        ("OLLAMA_LOCAL_URL port", ollama_port),
    ]


def _ollama_tags_probe_url_from_env(env_values: dict[str, str]) -> str:
    base = env_values.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434").strip()
    if not base:
        base = "http://127.0.0.1:11434"
    return base.rstrip("/") + "/api/tags"


def _format_ollama_local_url_with_port(previous_raw: str | None, new_port: int) -> str:
    if new_port < 1 or new_port > 65535:
        raise ValueError(f"new_port out of range: {new_port}")
    raw = (previous_raw or "").strip() or "http://127.0.0.1:11434"
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid OLLAMA_LOCAL_URL: {previous_raw!r}") from exc
    scheme = parsed.scheme or "http"
    host = parsed.hostname
    if host is None:
        host = "127.0.0.1"
    host_norm = host.strip("[]")
    if ":" in host_norm:
        netloc = f"[{host_norm}]:{int(new_port)}"
    else:
        netloc = f"{host_norm}:{int(new_port)}"
    return urlunparse((scheme, netloc, parsed.path or "", parsed.params, parsed.query, parsed.fragment))


def _pick_first_free_tcp_port(host: str, start_candidate: int, reserved_tcp_ports: set[int]) -> int:
    if not host or not str(host).strip():
        raise ValueError("host is empty")
    begin = max(1, int(start_candidate))
    if begin > 65535:
        raise RuntimeError(f"No TCP ports left to try starting from {start_candidate}")
    for candidate in range(begin, 65536):
        if candidate in reserved_tcp_ports:
            continue
        try:
            in_use = _tcp_port_is_in_use(str(host).strip(), int(candidate))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"TCP probe failed for port {candidate} on {host!r}") from exc
        if not in_use:
            return int(candidate)
    raise RuntimeError(f"No free TCP port found on {host!r} starting from {start_candidate}")


def _rewrite_env_file_string_values(env_file: Path, updates: dict[str, str]) -> None:
    if env_file is None:
        raise ValueError("env_file is required")
    if not updates:
        return
    for key in updates:
        if not key or not str(key).strip():
            raise ValueError("update keys must be non-empty")
    path = Path(env_file).resolve()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read env file for rewrite: {path}") from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Env file is not valid UTF-8: {path}") from exc
    lines = raw.splitlines()
    keys_remaining = dict(updates)
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key_part, sep, _value_part = line.partition("=")
        key_name = key_part.strip()
        if key_name in keys_remaining:
            replacement = keys_remaining[key_name]
            new_lines.append(f"{key_name}={replacement}")
            try:
                del keys_remaining[key_name]
            except KeyError:
                pass
        else:
            new_lines.append(line)
    for key_name in sorted(keys_remaining.keys()):
        new_lines.append(f"{key_name}={keys_remaining[key_name]}")
    out = "\n".join(new_lines) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(out, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not write env file: {path}") from exc


def _compute_port_fix_updates(env_values: dict[str, str]) -> dict[str, str]:
    if env_values is None:
        raise ValueError("env_values is required")
    work: dict[str, str] = dict(env_values)
    accumulated: dict[str, str] = {}
    bind_host = "127.0.0.1"
    max_inner_passes = 96
    for _ in range(max_inner_passes):
        try:
            plan = _stack_port_plan(work)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid port configuration while computing fixes: {exc}") from exc
        reserved_tcp_ports = {port for _label, port in plan}
        fixed_one = False
        for label, port in plan:
            try:
                in_use = _tcp_port_is_in_use(bind_host, port)
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"Could not check TCP port {port} ({label}) while computing fixes."
                ) from exc
            if not in_use:
                continue
            if label == "OLLAMA_LOCAL_URL port":
                probe_url = _ollama_tags_probe_url_from_env(work)
                try:
                    status = _http_get_status(probe_url, timeout=5.0)
                except ValueError as exc:
                    raise RuntimeError(f"Invalid Ollama probe URL {probe_url!r}") from exc
                if status == 200:
                    continue
                try:
                    new_port = _pick_first_free_tcp_port(bind_host, port + 1, reserved_tcp_ports)
                except RuntimeError as exc:
                    raise RuntimeError(
                        "Could not allocate a free port for Ollama; free a port manually or "
                        "edit OLLAMA_LOCAL_URL / OLLAMA_HOST."
                    ) from exc
                reserved_tcp_ports.discard(port)
                reserved_tcp_ports.add(new_port)
                previous_ollama_url = work.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434")
                try:
                    new_url = _format_ollama_local_url_with_port(previous_ollama_url, new_port)
                except ValueError as exc:
                    raise RuntimeError("Could not rebuild OLLAMA_LOCAL_URL with new port") from exc
                new_host_binding = f"0.0.0.0:{new_port}"
                work["OLLAMA_LOCAL_URL"] = new_url
                work["OLLAMA_HOST"] = new_host_binding
                accumulated["OLLAMA_LOCAL_URL"] = new_url
                accumulated["OLLAMA_HOST"] = new_host_binding
                fixed_one = True
                break
            env_key = label
            try:
                new_port = _pick_first_free_tcp_port(bind_host, port + 1, reserved_tcp_ports)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"Could not allocate a free port for {env_key}; free a port or edit {env_key} manually."
                ) from exc
            reserved_tcp_ports.discard(port)
            reserved_tcp_ports.add(new_port)
            new_val = str(new_port)
            work[env_key] = new_val
            accumulated[env_key] = new_val
            if env_key == "AGENTIC_BACKEND_PORT":
                backend_url = f"http://localhost:{new_val}"
                work["BACKEND_PORT"] = new_val
                accumulated["BACKEND_PORT"] = new_val
                work["REACT_APP_BACKEND_URL"] = backend_url
                accumulated["REACT_APP_BACKEND_URL"] = backend_url
            fixed_one = True
            break
        if not fixed_one:
            break
    return accumulated


class StackOrchestrator:
    """Staged rollout for `local_ai_stack up`: Docker, ports, bootstrap, compose, DeepProbe readiness."""

    def __init__(
        self,
        root: Path,
        env_file: Path,
        logger: logging.Logger,
        *,
        rewrite_conflicting_ports: bool = True,
    ) -> None:
        if root is None:
            raise ValueError("root is required")
        if env_file is None:
            raise ValueError("env_file is required")
        self._root = Path(root).resolve()
        self._env_file = Path(env_file).resolve()
        self._logger = logger
        self._rewrite_conflicting_ports = bool(rewrite_conflicting_ports)

    def _verify_docker_daemon(self) -> None:
        self._logger.info("Stage 1: verifying Docker daemon is active")
        try:
            completed = subprocess.run(
                ["docker", "info"],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except FileNotFoundError as exc:
            self._logger.exception("Docker CLI not found on PATH")
            raise RuntimeError(
                "Docker CLI was not found. Install Docker Desktop or the Docker Engine "
                "and ensure `docker` is on your PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            self._logger.exception("Docker daemon did not respond to `docker info` in time")
            raise RuntimeError(
                "Docker daemon did not respond in time. Start Docker and retry."
            ) from exc
        except OSError as exc:
            self._logger.exception("OS error while invoking docker info")
            raise RuntimeError(f"Could not execute Docker: {exc}") from exc
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            self._logger.error(
                "Docker daemon check failed rc=%s stderr=%s stdout=%s",
                completed.returncode,
                stderr,
                stdout,
            )
            raise RuntimeError(
                "Docker daemon is not running or `docker info` failed. "
                "Start Docker Desktop (or the Docker service) and retry."
            )

    def _ollama_tags_probe_url(self, env_values: dict[str, str]) -> str:
        return _ollama_tags_probe_url_from_env(env_values)

    def _verify_stack_ports_raise_on_conflict(self, env_values: dict[str, str]) -> None:
        self._logger.info("Stage 2: checking host ports for compose services")
        bind_host = "127.0.0.1"
        probe_url = _ollama_tags_probe_url_from_env(env_values)
        lines: list[str] = []
        try:
            entries = _stack_port_plan(env_values)
        except (TypeError, ValueError) as exc:
            self._logger.exception("Invalid port configuration in env")
            raise RuntimeError(f"Invalid port configuration: {exc}") from exc
        for label, port in entries:
            try:
                in_use = _tcp_port_is_in_use(bind_host, port)
            except (OSError, ValueError) as exc:
                self._logger.exception("TCP check failed for %s:%s", label, port)
                raise RuntimeError(f"Could not check whether port {port} ({label}) is in use.") from exc
            if not in_use:
                continue
            if label == "OLLAMA_LOCAL_URL port":
                try:
                    status = _http_get_status(probe_url, timeout=5.0)
                except ValueError as exc:
                    self._logger.exception("Invalid probe URL for Ollama")
                    raise RuntimeError(f"Invalid Ollama probe URL built from env: {exc}") from exc
                if status == 200:
                    self._logger.info(
                        "Port %s is in use but responds with HTTP %s for Ollama probe; continuing",
                        port,
                        status,
                    )
                    continue
                try:
                    alt = _find_alternative_host_port(bind_host, port + 1)
                except RuntimeError as exc:
                    self._logger.exception("Could not find alternative Ollama port")
                    raise RuntimeError(
                        "Ollama port is occupied by a non-Ollama service and no alternative "
                        "port was found. Free the port or adjust OLLAMA_LOCAL_URL / OLLAMA_HOST."
                    ) from exc
                lines.append(
                    f"- Port {port} (Ollama): in use and Ollama probe returned HTTP {status} "
                    f"instead of 200. Consider stopping the conflicting process or using a free "
                    f"port such as {alt} (update OLLAMA_LOCAL_URL and OLLAMA_HOST accordingly)."
                )
                self._logger.error(
                    "Ollama port conflict: probe %s returned status=%s",
                    probe_url,
                    status,
                )
                continue
            try:
                alt = _find_alternative_host_port(bind_host, port + 1)
            except RuntimeError as exc:
                self._logger.exception("Could not find alternative port for %s", label)
                raise RuntimeError(
                    f"Port {port} ({label}) is in use and no alternative port was found nearby."
                ) from exc
            lines.append(
                f"- Port {port} ({label}) is already bound on {bind_host}. "
                f"Set {label}={alt} in {self._env_file} (or another free port) and retry."
            )
            self._logger.error("Port conflict: %s=%s is in use; suggested alternative %s", label, port, alt)
        if lines:
            msg = "Host port conflicts detected:\n" + "\n".join(lines)
            self._logger.error(msg)
            raise RuntimeError(msg)

    def _rewrite_stack_ports_with_auto_fix(self, env_preview: dict[str, str]) -> dict[str, str]:
        current: dict[str, str] = dict(env_preview)
        max_outer_rounds = 24
        for round_idx in range(max_outer_rounds):
            try:
                updates = _compute_port_fix_updates(current)
            except RuntimeError:
                raise
            except Exception as exc:
                self._logger.exception("Unexpected error while computing port fix updates")
                raise RuntimeError("Automatic port conflict resolution failed unexpectedly.") from exc
            if not updates:
                try:
                    self._verify_stack_ports_raise_on_conflict(current)
                except RuntimeError:
                    raise
                return current
            try:
                _rewrite_env_file_string_values(self._env_file, updates)
            except RuntimeError:
                raise
            except OSError as exc:
                self._logger.exception("Failed to rewrite env file with adjusted ports")
                raise RuntimeError(f"Could not rewrite env file: {self._env_file}") from exc
            summary = ", ".join(f"{key}={value}" for key, value in sorted(updates.items()))
            self._logger.warning(
                "Rewrote %s to resolve host port conflicts (round %s): %s",
                self._env_file,
                round_idx + 1,
                summary,
            )
            _print(f"Resolved host port conflicts by updating {self._env_file}: {summary}")
            try:
                current = _parse_env_file(self._env_file)
            except (OSError, UnicodeDecodeError) as exc:
                self._logger.exception("Could not re-parse env file after automatic port rewrite")
                raise RuntimeError(
                    f"Could not re-read env file after port rewrite: {self._env_file}"
                ) from exc
        try:
            self._verify_stack_ports_raise_on_conflict(current)
        except RuntimeError:
            raise
        return current

    def _run_compose_up(
        self,
        env_file: Path,
        agenticseek_path: Path,
        process_env: dict[str, str],
    ) -> None:
        self._logger.info("Stage 4: starting AgenticSeek docker compose stack")
        try:
            from local_ai_stack.resource_hardener import ensure_compose_resource_override

            ensure_compose_resource_override(self._root, logger=self._logger)
        except Exception as exc:
            self._logger.warning("ResourceHardener could not refresh compose override: %s", exc)
        compose_cmd = _compose_base_args(env_file, agenticseek_path) + [
            "--profile",
            "full",
            "--profile",
            "addons",
            "up",
            "-d",
            "--build",
        ]
        try:
            _run(compose_cmd, env=process_env)
        except subprocess.CalledProcessError as exc:
            self._logger.exception(
                "docker compose up failed with exit code %s",
                getattr(exc, "returncode", "unknown"),
            )
            raise

    def execute(self) -> None:
        try:
            _ensure_env_file(self._env_file)
        except OSError as exc:
            self._logger.exception("Could not ensure env file exists")
            raise RuntimeError(f"Could not prepare env file at {self._env_file}") from exc
        try:
            env_preview = _parse_env_file(self._env_file)
        except (OSError, UnicodeDecodeError) as exc:
            self._logger.exception("Could not read env file")
            raise RuntimeError(f"Could not read env file: {self._env_file}") from exc
        try:
            self._verify_docker_daemon()
        except RuntimeError:
            raise
        except Exception as exc:
            self._logger.exception("Unexpected error during Docker daemon verification")
            raise RuntimeError("Docker verification failed due to an unexpected error.") from exc
        try:
            from local_ai_stack.port_sentinel import run_port_sentinel

            self._logger.info("Stage 1.5: PortSentinel (TCP 11434, 5432, 6379, 8080)")
            env_preview = run_port_sentinel(
                self._root,
                self._env_file,
                env_preview,
                logger=self._logger,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            self._logger.exception("Unexpected error during PortSentinel")
            raise RuntimeError("Port sentinel failed due to an unexpected error.") from exc
        try:
            if self._rewrite_conflicting_ports:
                env_preview = self._rewrite_stack_ports_with_auto_fix(env_preview)
            else:
                self._verify_stack_ports_raise_on_conflict(env_preview)
        except RuntimeError:
            raise
        except Exception as exc:
            self._logger.exception("Unexpected error during port conflict checks")
            raise RuntimeError("Port conflict checking failed due to an unexpected error.") from exc
        try:
            env_values, agenticseek_path = bootstrap(self._env_file)
        except Exception as exc:
            self._logger.exception("Bootstrap failed")
            raise
        process_env = _merge_process_env(env_values)
        try:
            _start_ollama_if_needed(env_values, process_env)
        except Exception as exc:
            self._logger.exception("Starting Ollama on the host failed")
            raise
        try:
            from local_ai_stack.model_fallback import run_ollama_pull_with_model_fallback

            env_values = run_ollama_pull_with_model_fallback(
                self._root,
                self._env_file,
                env_values,
                process_env,
                self._logger,
                agenticseek_path,
            )
            process_env = _merge_process_env(env_values)
        except RuntimeError:
            raise
        except Exception as exc:
            self._logger.exception("ollama pull / ModelFallback failed")
            raise RuntimeError("Model pull or fallback handling failed unexpectedly.") from exc
        if _claude_agent_stack_available():
            self._logger.info("Including Claude Agent service (deploy/claude-code present)")
            _print(
                "Claude Agent: building and starting ``claude-agent`` "
                f"(image from {CLAUDE_CODE_DIR.relative_to(ROOT)})…"
            )
        try:
            self._run_compose_up(self._env_file, agenticseek_path, process_env)
        except subprocess.CalledProcessError:
            raise
        try:
            from local_ai_stack.deep_probe import run_deep_probe_until_ready

            self._logger.info(
                "Stage 5: DeepProbe — Ollama /api/tags, Postgres SELECT 1, Redis PING "
                "(backoff 1–8s, deadline 60s)"
            )
            run_deep_probe_until_ready(
                env_values,
                logger=self._logger,
                announce=_print,
                timeout_sec=60.0,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            self._logger.exception("Unexpected error during DeepProbe")
            raise RuntimeError("DeepProbe readiness check failed unexpectedly.") from exc
        try:
            from local_ai_stack import privacy_monitor as _privacy_monitor

            _privacy_monitor.maybe_arm_after_up(self._env_file, self._logger)
        except Exception as exc:
            self._logger.warning("Privacy monitor could not arm: %s", exc)
        _print("Stack started.")
        _print(f"AgenticSeek UI: http://localhost:{env_values.get('AGENTIC_FRONTEND_PORT', '3010')}")
        _print(f"AgenticSeek API: http://localhost:{env_values.get('AGENTIC_BACKEND_PORT', '7777')}/health")
        _print(f"Open WebUI: http://localhost:{env_values.get('OPEN_WEBUI_PORT', '3001')}")
        _print(f"n8n: http://localhost:{env_values.get('N8N_PORT', '5678')}")
        _print(f"SearXNG: http://localhost:{env_values.get('SEARXNG_PORT', '8080')}")
        if _claude_agent_stack_available():
            _print(
                "Claude Agent: container ``local-ai-claude-agent`` on ``agentic-seek-net`` "
                "(ANTHROPIC_BASE_URL=http://ollama:11434 → host Ollama via extra_hosts). "
                "Mount config at ./.local/claude_config"
            )


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


def _lines_from_trivy_critical_report(report: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for result in report.get("Results") or []:
        if not isinstance(result, dict):
            continue
        target = str(result.get("Target") or "")
        for vuln in result.get("Vulnerabilities") or []:
            if not isinstance(vuln, dict):
                continue
            if str(vuln.get("Severity") or "").upper() != "CRITICAL":
                continue
            vid = str(vuln.get("VulnerabilityID") or vuln.get("ID") or "?")
            pkg = str(vuln.get("PkgName") or vuln.get("PkgID") or "?")
            title = str(vuln.get("Title") or "").strip()
            tail = f": {title}" if title else ""
            lines.append(f"[security] CRITICAL vuln {vid} in `{pkg}` ({target}){tail}")
        for mc in result.get("Misconfigurations") or []:
            if not isinstance(mc, dict):
                continue
            if str(mc.get("Severity") or "").upper() != "CRITICAL":
                continue
            mid = str(mc.get("ID") or "?")
            title = str(mc.get("Title") or "").strip()
            tail = f": {title}" if title else ""
            lines.append(f"[security] CRITICAL misconfiguration {mid} ({target}){tail}")
    return lines[:40]


def collect_trivy_critical_evidence(
    scan_root: Path, *, timeout: int = 180
) -> tuple[bool, list[str], bool]:
    """Return (critical_found, messages, skipped_no_binary).

    Mirrors grounded-review scope: CRITICAL filesystem vulns/misconfigs only.
    """
    exe = shutil.which("trivy")
    if not exe:
        return False, ["[octo-spork] Trivy not on PATH; Critical filesystem scan skipped."], True
    scan_root = scan_root.expanduser().resolve()
    if not scan_root.is_dir():
        return False, [f"[octo-spork] Scan root is not a directory: {scan_root}"], False
    cmd = [
        exe,
        "fs",
        "--severity",
        "CRITICAL",
        "--format",
        "json",
        "--quiet",
        str(scan_root),
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(scan_root),
            capture_output=True,
            text=True,
            timeout=max(30, int(timeout)),
            check=False,
        )
    except FileNotFoundError:
        return False, ["[octo-spork] Trivy executable vanished mid-invocation."], True
    except subprocess.TimeoutExpired:
        return True, [f"[octo-spork] Trivy timed out after {timeout}s"], False
    except OSError as exc:
        return True, [f"[octo-spork] Trivy OS error: {exc}"], False

    raw = (completed.stdout or "").strip()
    if not raw:
        err = (completed.stderr or "").strip()
        return True, [f"[octo-spork] Trivy produced empty stdout (exit {completed.returncode}): {err[:1500]}"], False

    try:
        report = json.loads(raw)
    except json.JSONDecodeError as exc:
        return True, [f"[octo-spork] Could not parse Trivy JSON: {exc}; stderr={(completed.stderr or '')[:800]}"], False

    if not isinstance(report, dict):
        return True, ["[octo-spork] Trivy JSON root was not an object."], False

    critical_lines = _lines_from_trivy_critical_report(report)
    if critical_lines:
        return True, critical_lines, False
    return False, [], False


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


def run_pre_push_scan(
    env_file: Path,
    repo_root: Path,
    *,
    skip_health: bool,
    skip_trivy: bool,
    require_trivy: bool,
) -> int:
    """Return 0 when push should proceed, 1 when blocked."""
    failures: list[str] = []
    repo_root = repo_root.expanduser().resolve()

    if not skip_health:
        bad_health, health_logs = run_hook_infra_health_probe(env_file)
        if bad_health:
            failures.extend(health_logs)

    if not skip_trivy:
        critical, tri_logs, skipped = collect_trivy_critical_evidence(repo_root)
        if skipped and require_trivy:
            failures.append("[security] --require-trivy set but `trivy` was not found on PATH.")
        elif critical:
            failures.extend(tri_logs)
        elif skipped and tri_logs:
            for msg in tri_logs:
                print(msg, file=sys.stderr)

    for msg in failures:
        print(msg, file=sys.stderr)
    if failures:
        print(
            "\n[octo-spork] pre-push scan failed: resolve infra health issues and/or CRITICAL Trivy findings.\n",
            file=sys.stderr,
        )
        return 1
    return 0


def command_pre_push_scan(
    env_file: Path,
    repo: str,
    skip_health: bool,
    skip_trivy: bool,
    require_trivy: bool,
) -> None:
    code = run_pre_push_scan(
        env_file,
        _repo_path_from_arg(repo),
        skip_health=skip_health,
        skip_trivy=skip_trivy,
        require_trivy=require_trivy,
    )
    if code != 0:
        raise RuntimeError("Octo-spork pre-push scan did not pass.")


def command_install_hook(repo: str, env_file: Path, *, force: bool) -> None:
    """Write a pre-push hook that runs ``pre-push-scan`` for this worktree."""
    repo_path = _repo_path_from_arg(repo)
    git_dir = repo_path / ".git"
    if not git_dir.is_dir():
        raise RuntimeError(f"Not a git repository: {repo_path}")
    hook_path = git_dir / "hooks" / "pre-push"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    if hook_path.exists() and not force:
        raise RuntimeError(
            f"{hook_path} already exists. Re-run with --force to overwrite, or remove the file first."
        )

    env_resolved = env_file.resolve()
    py = sys.executable
    octo = str(ROOT)
    # Hook runs with cwd = git toplevel; keep absolute paths.
    q_octo = shlex.quote(octo)
    q_py = shlex.quote(py)
    q_env = shlex.quote(str(env_resolved))
    body = f"""#!/usr/bin/env bash
# Generated by: python -m local_ai_stack install-hook
# Octo-spork pre-push: lightweight stack health (verify-style) + Trivy CRITICAL on the repo root.
set -euo pipefail
OCTO_ROOT={q_octo}
PYTHON={q_py}
ENV_FILE={q_env}
export PYTHONPATH="${{OCTO_ROOT}}${{PYTHONPATH:+:$PYTHONPATH}}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
echo "[octo-spork] pre-push scan (repo: $REPO_ROOT)…" >&2
exec "$PYTHON" -m local_ai_stack pre-push-scan --env-file "$ENV_FILE" --repo "$REPO_ROOT"
"""
    hook_path.write_text(body, encoding="utf-8")
    try:
        hook_path.chmod(0o755)
    except OSError:
        pass
    _print(f"Wrote {hook_path}")


def command_install_verify_logic_hook(repo: str, env_file: Path, *, force: bool) -> None:
    """Write a pre-commit hook that runs ``scripts/verify_logic.py`` (no auto ``up``; respects OCTO_SKIP_VERIFY_LOGIC)."""
    repo_path = _repo_path_from_arg(repo)
    git_dir = repo_path / ".git"
    if not git_dir.is_dir():
        raise RuntimeError(f"Not a git repository: {repo_path}")
    hook_path = git_dir / "hooks" / "pre-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    if hook_path.exists() and not force:
        raise RuntimeError(
            f"{hook_path} already exists. Re-run with --force to overwrite, or remove the file first."
        )

    env_resolved = env_file.resolve()
    py = sys.executable
    octo = str(ROOT)
    q_octo = shlex.quote(octo)
    q_py = shlex.quote(py)
    q_env = shlex.quote(str(env_resolved))
    body = f"""#!/usr/bin/env bash
# Generated by: python -m local_ai_stack install-verify-logic-hook
# Golden-path: Ollama grounded diff review on a dummy vuln repo + receipt audit.
# Emergency bypass: OCTO_SKIP_VERIFY_LOGIC=1 git commit ...
# Full stack bring-up (slow) is NOT run here; use: python3 scripts/verify_logic.py --bring-up
set -euo pipefail
if [ -n "${{OCTO_SKIP_VERIFY_LOGIC:-}}" ]; then
  echo "[octo-spork] pre-commit: OCTO_SKIP_VERIFY_LOGIC set — skipping verify_logic." >&2
  exit 0
fi
OCTO_ROOT={q_octo}
PYTHON={q_py}
ENV_FILE={q_env}
export PYTHONPATH="${{OCTO_ROOT}}:${{OCTO_ROOT}}/src${{PYTHONPATH:+:$PYTHONPATH}}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
echo "[octo-spork] pre-commit: verify_logic (repo: $REPO_ROOT)…" >&2
exec "$PYTHON" "$OCTO_ROOT/scripts/verify_logic.py" --repo-root "$REPO_ROOT" --env-file "$ENV_FILE" --no-bring-up
"""
    hook_path.write_text(body, encoding="utf-8")
    try:
        hook_path.chmod(0o755)
    except OSError:
        pass
    _print(f"Wrote {hook_path}")


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


def _fetch_ollama_models_and_error(base_url: str, *, timeout: float = 12.0) -> tuple[list[dict[str, object]], str | None]:
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


def command_logs(
    env_file: Path,
    *,
    log_all: bool,
    tail: int,
    follow: bool,
    timestamps: bool,
    services: tuple[str, ...],
) -> int:
    """Stream ``docker compose logs`` with per-service colors and red severity keywords."""
    from local_ai_stack.compose_logs import run_follow_logs

    env_values = _parse_env_file(env_file)
    agenticseek_path = _resolve_agenticseek_path(env_values)
    process_env = _merge_process_env(env_values)
    cmd = _compose_base_args(env_file, agenticseek_path) + [
        "--profile",
        "full",
        "--profile",
        "addons",
    ]
    tail_str = "all" if log_all else str(tail)
    return run_follow_logs(
        cmd,
        tail=tail_str,
        follow=follow,
        timestamps=timestamps,
        services=services,
        env=process_env,
    )


def command_doctor(
    env_file: Path,
    repo: str,
    *,
    strict: bool,
    fix: bool = False,
    accept_prune: bool = False,
) -> int:
    """Print the developer environment checklist (CPU/GPU, Docker, disk, tools, stack, Claude Code)."""
    if fix:
        from local_ai_stack.doctor_fix import run_stability_fix

        repo_root = Path(repo).expanduser()
        if not repo_root.is_absolute():
            repo_root = Path.cwd() / repo_root
        return run_stability_fix(repo_root.resolve(), env_file, accept_prune=accept_prune)

    from local_ai_stack.doctor import format_doctor_report, run_doctor

    repo_root = Path(repo).expanduser()
    if not repo_root.is_absolute():
        repo_root = Path.cwd() / repo_root
    repo_root = repo_root.resolve()
    items = run_doctor(env_file=env_file, repo_root=repo_root)
    _print(format_doctor_report(items))
    if strict and any(it.status == "red" for it in items):
        return 1
    return 0


def command_benchmark(
    env_file: Path,
    *,
    git_url: str,
    work_parent: Path,
    clone_depth: int,
    output_csv: Path,
    model: str | None,
    ollama_url: str | None,
    base: str | None,
    head: str | None,
    skip_clone: bool,
    repo_dir: Path | None,
    show_review: bool,
) -> int:
    """Clone Spoon-Knife (unless skipped), run grounded diff review, append metrics to performance.csv."""
    _ensure_env_file(env_file)
    from local_ai_stack.benchmark import run_benchmark

    return run_benchmark(
        env_file=env_file,
        git_url=git_url,
        work_parent=work_parent,
        clone_depth=clone_depth,
        output_csv=output_csv,
        model_override=model,
        ollama_url_override=ollama_url,
        base_ref=base,
        head_ref=head,
        skip_clone=skip_clone,
        repo_dir=repo_dir,
        show_review=show_review,
    )


def command_swap_model(
    env_file: Path,
    model: str,
    *,
    update_env: bool,
    skip_registry: bool,
    ignore_vram: bool,
    vram_headroom: float,
) -> None:
    """Pull a model through Ollama's HTTP API; optionally set ``OLLAMA_MODEL`` in the env file."""
    from local_ai_stack.ollama_swap import run_swap

    try:
        env_values = _parse_env_file(env_file)
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read env file {env_file}: {exc}") from exc
    base = str(env_values.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434")).strip().rstrip("/")
    run_swap(
        model,
        ollama_base_url=base,
        env_file=env_file if update_env else None,
        update_env=update_env,
        skip_registry=skip_registry,
        ignore_vram=ignore_vram,
        vram_headroom=vram_headroom,
    )


def command_data_wipe(*, assume_yes: bool, passes: int) -> None:
    """Overwrite bind-mounted database files under ``.local/data`` then delete them."""
    from local_ai_stack.data_wipe import wipe_directory_tree

    target = REPO_LOCAL_DATA_DIR.resolve()
    if not assume_yes:
        _print(
            "Stop the stack first so Docker releases file locks "
            "(e.g. `python -m local_ai_stack down --env-file deploy/local-ai/.env.local`)."
        )
        _print(f"This will overwrite then delete all files under:\n  {target}")
        _print("Type exactly YES to continue:")
        line = sys.stdin.readline()
        if (line or "").strip() != "YES":
            raise RuntimeError("data-wipe aborted (confirmation did not match YES).")
    if not target.exists():
        _print(f"No data directory at {target}; creating empty layout.")
        ensure_repo_local_data_dirs()
        return
    wipe_directory_tree(target, passes=max(1, passes))
    ensure_repo_local_data_dirs()
    _print(f"Wiped data and recreated empty directories under {target}")


def command_down(env_file: Path) -> None:
    _print(f"Shutting down stack project {COMPOSE_PROJECT_NAME!r} ...")
    try:
        from local_ai_stack import privacy_monitor as _privacy_monitor

        _privacy_monitor.teardown_from_down()
    except Exception as exc:
        _print(f"Warning: privacy monitor teardown: {exc}")
    env_values: dict[str, str] = {}
    try:
        env_values = _parse_env_file(env_file)
    except (OSError, UnicodeDecodeError) as exc:
        _print(f"Warning: could not read env file {env_file}: {exc}")

    process_env = _merge_process_env(env_values)
    agenticseek_path: Path | None = None
    try:
        agenticseek_path = _resolve_agenticseek_path(env_values)
    except (RuntimeError, OSError, ValueError) as exc:
        _print(
            f"Warning: could not resolve AGENTICSEEK_DIR ({exc}). "
            "Skipping compose down; still pruning Docker resources for this project name."
        )

    if agenticseek_path is not None:
        dc_yml = agenticseek_path / "docker-compose.yml"
        if not dc_yml.is_file():
            _print(
                f"Warning: {dc_yml} not found; skipping compose down. "
                "Run bootstrap or clone AgenticSeek so compose files exist."
            )
        else:
            try:
                ef = Path(env_file).resolve()
                _compose_down_stack(ef, agenticseek_path, process_env)
            except (RuntimeError, OSError, ValueError, TypeError) as exc:
                _print(f"Warning: unexpected error during compose down: {exc}")

    try:
        _docker_volume_prune_project(COMPOSE_PROJECT_NAME)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: volume prune failed unexpectedly: {exc}")

    try:
        _docker_network_remove_labeled_project(COMPOSE_PROJECT_NAME)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: labeled network removal failed unexpectedly: {exc}")

    try:
        _docker_network_remove_strays_by_name_prefix(COMPOSE_PROJECT_NAME)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: stray network cleanup failed unexpectedly: {exc}")

    try:
        _cleanup_grounded_review_temp_clones(GROUNDED_REVIEW_TEMP_CLONES_DIR)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: temp clone cleanup failed unexpectedly: {exc}")

    try:
        from local_ai_stack.log_steward import run_log_steward

        run_log_steward(ROOT, announce=_print)
    except Exception as exc:
        _print(f"Warning: LogSteward failed: {exc}")

    _print("Down cleanup finished.")


def _octospork_labeled_container_ids() -> list[str]:
    """Container IDs with ``com.octospork.project=<COMPOSE_PROJECT_NAME>`` (running or stopped)."""
    filt = f"label={OCTO_SPORK_PROJECT_LABEL_KEY}={COMPOSE_PROJECT_NAME}"
    try:
        completed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", filt],
            cwd=str(ROOT),
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _print(f"Warning: could not list labeled Docker containers: {exc}")
        return []
    if completed.returncode != 0:
        err = (completed.stderr or "").strip()
        _print(f"Warning: docker ps failed: {err}")
        return []
    return [ln.strip() for ln in (completed.stdout or "").splitlines() if ln.strip()]


def _docker_rm_force_container_ids(ids: list[str]) -> None:
    """``docker rm -f`` in batches (argument length limits on Windows)."""
    if not ids:
        return
    chunk_size = 40
    for i in range(0, len(ids), chunk_size):
        batch = ids[i : i + chunk_size]
        try:
            completed = subprocess.run(
                ["docker", "rm", "-f", *batch],
                cwd=str(ROOT),
                check=False,
                text=True,
                capture_output=True,
                timeout=300,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            _print(f"Warning: docker rm -f batch failed: {exc}")
            continue
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            _print(f"Warning: docker rm -f exit {completed.returncode}: {detail[:1200]}")


def _chmod_writable_best_effort(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        os.chmod(path, mode | stat.S_IWRITE)
    except OSError:
        pass


def _remove_pid_lock_file(path: Path) -> bool:
    """``os.remove`` with one chmod retry (Windows/Linux permission quirks)."""
    try:
        os.remove(path)
        return True
    except PermissionError:
        _chmod_writable_best_effort(path)
        try:
            os.remove(path)
            return True
        except OSError as exc:
            _print(f"Warning: could not remove {path} after chmod: {exc}")
            return False
    except OSError as exc:
        _print(f"Warning: could not remove {path}: {exc}")
        return False


def _remove_dangling_pid_lock_files_under(local_root: Path) -> None:
    """Walk ``.local`` (recursive) and delete ``*.pid`` / ``*.lock`` files."""
    try:
        root = local_root.expanduser().resolve()
    except OSError as exc:
        _print(f"Warning: could not resolve local dir {local_root}: {exc}")
        return
    if not root.is_dir():
        _print(f"No directory at {root}; skipping pid/lock cleanup.")
        return
    removed = 0
    try:
        for pattern in ("*.pid", "*.lock"):
            for path in root.rglob(pattern):
                try:
                    if path.is_file() and _remove_pid_lock_file(path):
                        removed += 1
                except OSError as exc:
                    _print(f"Warning: skipped {path}: {exc}")
    except OSError as exc:
        _print(f"Warning: could not walk {root}: {exc}")
        return
    if removed:
        _print(f"Removed {removed} pid/lock file(s) under {root}.")
    else:
        _print(f"No .pid or .lock files found under {root}.")


def command_force_clean(env_file: Path) -> None:
    """Aggressive cleanup: kill labeled containers, remove compose networks, scrub ``.local`` locks."""
    _ = env_file  # CLI parity; Docker filters do not require parsing .env.local.
    _print(
        f"force-clean: targets label {OCTO_SPORK_PROJECT_LABEL_KEY}={COMPOSE_PROJECT_NAME!r}, "
        f"Docker networks for project {COMPOSE_PROJECT_NAME!r}, and *.pid / *.lock under .local/"
    )
    ids = _octospork_labeled_container_ids()
    if ids:
        _print(f"Removing {len(ids)} container(s) (docker rm -f) …")
        _docker_rm_force_container_ids(ids)
    else:
        _print(
            "No containers matched the octospork project label "
            "(nothing running with labels from docker-compose.project-labels.yml, or already removed)."
        )

    try:
        _docker_network_remove_labeled_project(COMPOSE_PROJECT_NAME)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: labeled network removal failed: {exc}")

    try:
        _docker_network_remove_strays_by_name_prefix(COMPOSE_PROJECT_NAME)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: stray network cleanup failed: {exc}")

    _remove_dangling_pid_lock_files_under(ROOT / ".local")
    _print("force-clean finished.")


def _env_file_from_arg(raw_path: str | None) -> Path:
    if not raw_path:
        return DEFAULT_ENV_FILE
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate


def _optional_example_file_from_arg(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve()


def _load_grounded_review():
    spec = importlib.util.spec_from_file_location("grounded_review", OVERLAY_SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load grounded_review module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_review_ticket_export():
    spec = importlib.util.spec_from_file_location("review_ticket_export", REVIEW_TICKET_EXPORT_SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load review_ticket_export module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command_export_tickets(review_file: str | None, snapshot_json: str | None, output: str, query: str) -> None:
    rte = _load_review_ticket_export()
    if review_file:
        md = Path(review_file).expanduser().resolve().read_text(encoding="utf-8")
    else:
        md = sys.stdin.read()
        if not md.strip():
            raise RuntimeError("Provide --review-file or pipe review markdown on stdin.")
    snap = None
    if snapshot_json:
        snap = rte.load_snapshot_from_json(Path(snapshot_json).expanduser().resolve())
    out = Path(output).expanduser().resolve()
    rte.export_review_tickets_json(md, snap, out, query=query or None)
    _print(f"Wrote ticket export: {out}")


def _repo_path_from_arg(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def command_diff_preview(repo: str, base: str, head: str, query: str) -> None:
    gr = _load_grounded_review()
    repo_path = _repo_path_from_arg(repo)
    snapshot = gr.fetch_local_diff_snapshot(repo_path, query, base, head)
    if snapshot is None:
        raise RuntimeError(
            "Could not build a diff snapshot. Use a git checkout where base and head resolve."
        )
    _print(gr.format_diff_preview_markdown(snapshot, base, head))


def command_review_diff(
    env_file: Path,
    repo: str,
    base: str,
    head: str,
    query: str,
    export_tickets: Path | None,
) -> None:
    _ensure_env_file(env_file)
    env_values = _parse_env_file(env_file)
    ollama_url = (env_values.get("OLLAMA_LOCAL_URL") or env_values.get("OLLAMA_BASE_URL") or "").strip()
    if not ollama_url:
        ollama_url = "http://127.0.0.1:11434"
    ollama_url = ollama_url.rstrip("/")
    model = env_values.get("OLLAMA_MODEL", "qwen2.5:14b").strip() or "qwen2.5:14b"

    gr = _load_grounded_review()
    repo_path = _repo_path_from_arg(repo)
    result = gr.grounded_local_diff_review(query, model, ollama_url, repo_path, base, head)
    if not result.get("success"):
        raise RuntimeError(str(result.get("answer", "Grounded diff review failed")))
    _print(str(result.get("answer", "")))
    if export_tickets is not None:
        rte = _load_review_ticket_export()
        out = export_tickets.expanduser().resolve()
        snap = result.get("snapshot")
        rte.export_review_tickets_json(
            str(result.get("answer", "")),
            snap if isinstance(snap, dict) else None,
            out,
            query=query,
        )
        _print(f"\nExported High-severity ticket stubs (JSON) to: {out}")


def command_resume(
    env_file: Path,
    workspace: Path,
    *,
    claude: bool,
    agent_cmd: list[str],
    print_json: bool,
) -> int:
    """Pull latest LangGraph snapshot from Redis (or delegate to Claude session resume)."""
    _ensure_env_file(env_file)
    env_values = _parse_env_file(env_file)
    merged = _merge_process_env(env_values)

    if claude:
        os.environ.clear()
        os.environ.update(merged)
        from claude_bridge.octo_cli import cmd_resume

        forward = ["--workspace", str(workspace.expanduser().resolve()), *agent_cmd]
        return cmd_resume(forward)

    src = ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from agent_guard.session_store import SessionStore

    redis_url = (env_values.get("REDIS_URL") or "").strip() or None
    store = SessionStore(redis_url=redis_url)
    snap = store.get_latest_snapshot()
    if not snap:
        _print(
            "No LangGraph session snapshot in Redis.\n"
            "Run your agent with SessionStore.start_periodic_save(300, get_state) "
            "(see agent_guard.session_store), ensure REDIS_URL matches the local Redis container, "
            "and that periodic saves have completed at least once."
        )
        return 1

    tid = str(snap.get("thread_id") or "")
    vals = snap.get("values")
    if print_json:
        _print(json.dumps(snap, indent=2, default=str))

    ws = workspace.expanduser().resolve()
    out_dir = ws / ".local" / "octo_session"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "langgraph_resume.json"
    out_path.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
    tid_disp = tid if len(tid) <= 48 else tid[:45] + "…"
    _print(f"Loaded snapshot thread_id={tid_disp}")
    _print(f"Wrote {out_path}")

    child_env = merged.copy()
    child_env["OCTO_LANGGRAPH_THREAD_ID"] = tid
    child_env["OCTO_LANGGRAPH_RESUME_JSON"] = str(out_path)
    child_env["OCTO_LANGGRAPH_VALUES_JSON"] = json.dumps(
        vals if isinstance(vals, dict) else {},
        default=str,
    )

    if agent_cmd:
        try:
            proc = subprocess.run(agent_cmd, env=child_env, cwd=str(ws))
            return int(proc.returncode or 0)
        except FileNotFoundError as exc:
            _print(f"Error: {exc}")
            return 127

    _print(
        "\nOptional env for a custom runner (large payloads — prefer reading the JSON file):\n"
        f"  export OCTO_LANGGRAPH_RESUME_JSON={shlex.quote(str(out_path))}\n"
        f"  export OCTO_LANGGRAPH_THREAD_ID={shlex.quote(tid)}"
    )
    return 0


def command_build_optimized(env_file: Path, *, no_cache: bool) -> None:
    """Build AgenticSeek backend/frontend and optional Claude agent using multi-stage Dockerfiles."""
    _ensure_env_file(env_file)
    env_values = _parse_env_file(env_file)
    agenticseek_path = _resolve_agenticseek_path(env_values)
    main_compose = agenticseek_path / "docker-compose.yml"
    if not main_compose.is_file():
        raise RuntimeError(
            f"AgenticSeek checkout missing docker-compose.yml at {agenticseek_path}. Run bootstrap first."
        )

    process_env = _merge_process_env(env_values)
    process_env["OCTO_SPORK_ROOT"] = str(ROOT)

    cmd: list[str] = [
        "docker",
        "compose",
        "--project-name",
        COMPOSE_PROJECT_NAME,
        "--env-file",
        str(env_file),
        "-f",
        str(main_compose),
        "-f",
        str(ROOT / "deploy" / "local-ai" / "docker-compose.addons.yml"),
    ]
    if _claude_agent_stack_available():
        cmd.extend(["-f", str(CLAUDE_AGENT_COMPOSE_FILE)])
    cmd.extend(
        [
            "-f",
            str(ROOT / "deploy" / "local-ai" / "docker-compose.build-optimized.agentic.yml"),
        ]
    )
    if _claude_agent_stack_available():
        cmd.extend(
            [
                "-f",
                str(ROOT / "deploy" / "local-ai" / "docker-compose.build-optimized.claude.yml"),
            ]
        )

    cmd.extend(["--profile", "full", "--profile", "addons", "build"])
    if no_cache:
        cmd.append("--no-cache")

    services = ["backend", "frontend"]
    if _claude_agent_stack_available():
        services.append("claude-agent")
    cmd.extend(services)

    _print(
        "Building optimized images (multi-stage Dockerfiles; runtime stages omit gcc/build-base/npm caches)."
    )
    _run(cmd, env=process_env)
    _print(
        "Optimized build complete. Final images exclude compiler toolchains from runtime layers "
        "(≥40% savings vs typical single-stage backend images). Compare: docker images"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cross-platform local AI stack runner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("bootstrap", "up", "verify", "down", "status", "force-clean"):
        fc_help = (
            "Kill com.octospork.project containers, remove project Docker networks, "
            "delete *.pid/*.lock under .local/"
            if name == "force-clean"
            else None
        )
        sub = subparsers.add_parser(name, **({"help": fc_help} if fc_help else {}))
        sub.add_argument("--env-file", dest="env_file", default=None, help="Path to .env.local")
        if name == "up":
            sub.add_argument(
                "--no-rewrite-conflicting-ports",
                dest="rewrite_conflicting_ports",
                action="store_false",
                default=True,
                help=(
                    "Do not edit the env file when host ports conflict; exit with an error "
                    "and suggestions instead."
                ),
            )

    diff_prev = subparsers.add_parser(
        "diff-preview",
        help="Print a markdown diff triage preview (no Ollama; safe for GitHub-hosted CI)",
    )
    diff_prev.add_argument("--repo", default=".", help="Path to git repository root")
    diff_prev.add_argument("--base", required=True, help="Git base ref (e.g. origin/main or SHA)")
    diff_prev.add_argument("--head", default="HEAD", help="Git head ref (default HEAD)")
    diff_prev.add_argument(
        "--query",
        default="Review the changed files for security, correctness, and regression risks.",
        help="Triage query passed to file selection",
    )

    rev_diff = subparsers.add_parser(
        "review-diff",
        help="Run grounded LLM review over files touched between base and head (requires Ollama)",
    )
    rev_diff.add_argument("--env-file", dest="env_file", default=None, help="Path to .env.local")
    rev_diff.add_argument("--repo", default=".", help="Path to git repository root")
    rev_diff.add_argument("--base", required=True, help="Git base ref")
    rev_diff.add_argument("--head", default="HEAD", help="Git head ref (default HEAD)")
    rev_diff.add_argument(
        "--query",
        default="Perform a grounded code review of the diff; focus on security and regressions.",
        help="Review prompt",
    )
    rev_diff.add_argument(
        "--export-tickets",
        dest="export_tickets",
        default=None,
        metavar="PATH",
        help="Write Jira/Linear-oriented JSON export for High findings to PATH",
    )

    chat_cmd = subparsers.add_parser(
        "chat",
        help=(
            "Interactive REPL: ask follow-ups about the last grounded review "
            "(loads .octo/review_session/last_review.json + Ollama)"
        ),
    )
    chat_cmd.add_argument("--env-file", dest="env_file", default=None, help="Path to .env.local for Ollama URL/model")
    chat_cmd.add_argument(
        "--repo",
        default=".",
        help="Git repository root that was reviewed (default: current directory)",
    )

    export_tickets_cmd = subparsers.add_parser(
        "export-tickets",
        help="Export High findings from review markdown to structured JSON (Jira / Linear oriented)",
    )
    export_tickets_cmd.add_argument(
        "--review-file",
        dest="review_file",
        default=None,
        help="Path to markdown review output (default: read stdin)",
    )
    export_tickets_cmd.add_argument(
        "--snapshot-json",
        dest="snapshot_json",
        default=None,
        help="Optional JSON snapshot with receipts (same shape as grounded_review snapshot)",
    )
    export_tickets_cmd.add_argument(
        "-o",
        "--output",
        dest="output",
        required=True,
        help="Output JSON path",
    )
    export_tickets_cmd.add_argument(
        "--query",
        default="",
        help="Original review query (metadata only)",
    )

    validate_cfg = subparsers.add_parser(
        "validate-config",
        help="Validate .env.local against .env.example; prompt or generate secrets when needed",
    )
    validate_cfg.add_argument("--env-file", dest="env_file", default=None, help="Path to .env.local")
    validate_cfg.add_argument(
        "--example-file",
        dest="example_file",
        default=None,
        help="Path to .env.example (default: deploy/local-ai/.env.example)",
    )
    validate_cfg.add_argument(
        "--no-interactive",
        dest="interactive",
        action="store_false",
        default=True,
        help="Do not prompt; fail if keys are missing or placeholders remain",
    )

    install_hook = subparsers.add_parser(
        "install-hook",
        help="Write a pre-push hook that runs the Octo-spork scan before git push",
    )
    install_hook.add_argument("--repo", default=".", help="Git repository root that receives the hook")
    install_hook.add_argument(
        "--env-file",
        dest="env_file",
        default=None,
        help="Path to .env.local used for infra health checks (default: deploy/local-ai/.env.local)",
    )
    install_hook.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .git/hooks/pre-push",
    )

    install_vlogic = subparsers.add_parser(
        "install-verify-logic-hook",
        help="Write a pre-commit hook that runs scripts/verify_logic.py (grounded golden-path test)",
    )
    install_vlogic.add_argument(
        "--repo", default=".", help="Git repository root that receives the hook"
    )
    install_vlogic.add_argument(
        "--env-file",
        dest="env_file",
        default=None,
        help="Path to .env.local (default: deploy/local-ai/.env.local)",
    )
    install_vlogic.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .git/hooks/pre-commit",
    )

    pre_push = subparsers.add_parser(
        "pre-push-scan",
        help="Lightweight verify-style infra probe + Trivy CRITICAL filesystem scan (used by pre-push hook)",
    )
    pre_push.add_argument("--env-file", dest="env_file", default=None, help="Path to .env.local")
    pre_push.add_argument(
        "--repo",
        default=".",
        help="Git repo root (Trivy scans this tree; must match the repository you push from)",
    )
    pre_push.add_argument(
        "--skip-health",
        action="store_true",
        help="Skip HTTP/docker probes that mirror `verify`",
    )
    pre_push.add_argument("--skip-trivy", action="store_true", help="Skip Trivy filesystem scan")
    pre_push.add_argument(
        "--require-trivy",
        action="store_true",
        help="Fail if the `trivy` CLI is missing (default: warn-only when Trivy is absent)",
    )

    privacy_mon = subparsers.add_parser(
        "privacy-monitor",
        help=(
            "Detached privacy guard loop (iptables DROP counter polling). "
            "Normally spawned by `up` when LOCAL_AI_PRIVACY_MODE=local-only."
        ),
    )
    privacy_mon.add_argument("--env-file", dest="env_file", default=None, help="Path to .env.local")

    data_wipe_cmd = subparsers.add_parser(
        "data-wipe",
        help="Securely overwrite and delete bind-mounted stack data under .local/data",
    )
    data_wipe_cmd.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Do not prompt for confirmation",
    )
    data_wipe_cmd.add_argument(
        "--passes",
        type=int,
        default=1,
        metavar="N",
        help="Random overwrite passes per file (default 1)",
    )

    build_opt = subparsers.add_parser(
        "build-optimized",
        help=(
            "Multi-stage Docker build for AgenticSeek backend/frontend and Claude agent "
            "(smaller runtime images without compiler toolchains)"
        ),
    )
    build_opt.add_argument("--env-file", dest="env_file", default=None, help="Path to .env.local")
    build_opt.add_argument(
        "--no-cache",
        action="store_true",
        help="Pass --no-cache to docker compose build",
    )

    swap_cmd = subparsers.add_parser(
        "swap-model",
        help="Pull an Ollama model via HTTP (/api/pull); optionally set OLLAMA_MODEL without restarting the stack",
    )
    swap_cmd.add_argument(
        "--model",
        required=True,
        metavar="NAME",
        help="Library tag to pull, e.g. qwen2.5:14b or llama3:8b",
    )
    swap_cmd.add_argument("--env-file", dest="env_file", default=None, help="Path to .env.local")
    swap_cmd.add_argument(
        "--update-env",
        action="store_true",
        help="After success, set OLLAMA_MODEL in the env file (containers keep running; backend reload may be needed)",
    )
    swap_cmd.add_argument(
        "--skip-registry-check",
        action="store_true",
        help="Skip https://ollama.com/library/… verification",
    )
    swap_cmd.add_argument(
        "--ignore-vram",
        action="store_true",
        help="Skip estimated footprint vs free VRAM/RAM check",
    )
    swap_cmd.add_argument(
        "--vram-headroom",
        type=float,
        default=1.15,
        metavar="MULT",
        help="Require free memory >= est_size * MULT (default 1.15)",
    )

    logs_cmd = subparsers.add_parser(
        "logs",
        help="Follow docker compose logs with color-coded services and red ERROR/CRITICAL/TIMEOUT highlights",
    )
    logs_cmd.add_argument("--env-file", dest="env_file", default=None, help="Path to .env.local")
    logs_cmd.add_argument(
        "--tail",
        type=int,
        default=200,
        metavar="N",
        help="Lines per service to show from the end (default 200; ignored with --all)",
    )
    logs_cmd.add_argument(
        "--all",
        dest="log_all",
        action="store_true",
        help="Show full log buffer per service (--tail all)",
    )
    logs_cmd.add_argument(
        "--no-follow",
        dest="no_follow",
        action="store_true",
        help="Print logs once and exit (no stream)",
    )
    logs_cmd.add_argument(
        "-t",
        "--timestamps",
        action="store_true",
        help="Prefix lines with timestamps (passed through to docker compose)",
    )
    logs_cmd.add_argument(
        "services",
        nargs="*",
        metavar="SERVICE",
        help="Optional compose service names (default: all services in full+addons profiles)",
    )

    doctor_cmd = subparsers.add_parser(
        "doctor",
        help="Developer environment check: core stack + Claude Code (Bun, image, Ollama from agent, workspace)",
    )
    doctor_cmd.add_argument(
        "--env-file",
        dest="env_file",
        default=None,
        help="Path to .env.local (default: deploy/local-ai/.env.local)",
    )
    doctor_cmd.add_argument(
        "--repo",
        default=".",
        help="Git repository root for disk / env checks (default: current directory)",
    )
    doctor_cmd.add_argument(
        "--strict",
        action="store_true",
        help="Exit with status 1 if any check is [FAIL]",
    )
    doctor_cmd.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Stability repair: detect Docker OOM signals, prune builder caches, "
            "set OLLAMA_NUM_GPU from hardware, restart stack"
        ),
    )
    doctor_cmd.add_argument(
        "--accept-prune",
        action="store_true",
        help="With --fix: run `docker builder prune -af` (destructive to unused build cache)",
    )

    remediation_ui = subparsers.add_parser(
        "remediation-ui",
        help=(
            "Interactive split-pane Doctor UI: security finding vs proposed fix; "
            "Apply performs a sandboxed file write (human-in-the-loop)."
        ),
    )
    remediation_ui.add_argument(
        "--repo",
        default=".",
        help="Repository root for SafePath edits (default: current directory)",
    )
    remediation_ui.add_argument("--finding-file", default=None, help="Markdown/text for left pane")
    remediation_ui.add_argument("--fix-file", default=None, help="Proposed file body for right pane")
    remediation_ui.add_argument(
        "--target",
        default=None,
        help="Repo-relative write path on Apply (overrides # OCTO_EDIT_TARGET in fix file)",
    )
    remediation_ui.add_argument(
        "--demo",
        action="store_true",
        help="Built-in sample panes (Apply writes .octo-remediation-demo.txt)",
    )

    bench_cmd = subparsers.add_parser(
        "benchmark",
        help=(
            "Standardized grounded diff review on octocat/Spoon-Knife (or --git-url); "
            "records clone, scan, LLM latency and tokens in performance.csv"
        ),
    )
    bench_cmd.add_argument(
        "--env-file",
        dest="env_file",
        default=None,
        help="Path to .env.local (default: deploy/local-ai/.env.local)",
    )
    bench_cmd.add_argument(
        "--git-url",
        default="https://github.com/octocat/Spoon-Knife.git",
        help="Remote repository to clone (default: octocat/Spoon-Knife)",
    )
    bench_cmd.add_argument(
        "--work-dir",
        dest="work_dir",
        default=None,
        help="Parent directory for the clone (default: .local/benchmarks under the repo root)",
    )
    bench_cmd.add_argument(
        "--depth",
        type=int,
        default=40,
        metavar="N",
        help="git clone --depth (default: 40)",
    )
    bench_cmd.add_argument(
        "-o",
        "--output",
        dest="output_csv",
        default=None,
        help="CSV path for appended metrics (default: performance.csv in the repo root)",
    )
    bench_cmd.add_argument("--model", default=None, help="Ollama model tag (overrides OLLAMA_MODEL in .env.local)")
    bench_cmd.add_argument(
        "--ollama-url",
        default=None,
        dest="ollama_url",
        help="Ollama base URL (overrides OLLAMA_LOCAL_URL / OLLAMA_BASE_URL in .env.local)",
    )
    bench_cmd.add_argument(
        "--base",
        default=None,
        help="Diff base ref (default: repository root commit)",
    )
    bench_cmd.add_argument(
        "--head",
        default=None,
        help="Diff head ref (default: HEAD)",
    )
    bench_cmd.add_argument(
        "--skip-clone",
        action="store_true",
        help="Use an existing clone from --repo-dir instead of cloning",
    )
    bench_cmd.add_argument(
        "--repo-dir",
        default=None,
        help="Path to an existing git clone (required with --skip-clone)",
    )
    bench_cmd.add_argument(
        "--show-review",
        action="store_true",
        help="Print the full markdown review to stdout after the run",
    )

    bench_models_cmd = subparsers.add_parser(
        "benchmark-models",
        help=(
            "Run a ~100-token probe through every locally pulled Ollama model; "
            "writes performance_profile.json (TTFT, tokens/sec, peak VRAM) for background PR review selection"
        ),
    )
    from local_ai_stack.performance_profile import configure_benchmark_models_args

    configure_benchmark_models_args(bench_models_cmd)

    resume_cmd = subparsers.add_parser(
        "resume",
        help=(
            "Restore from Redis: LangGraph thread_id + values snapshot (default), "
            "or --claude to resume the last Claude Code session id"
        ),
    )
    resume_cmd.add_argument(
        "--env-file",
        dest="env_file",
        default=None,
        help="Path to .env.local (default: deploy/local-ai/.env.local)",
    )
    resume_cmd.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace root for resume JSON (default: current directory)",
    )
    resume_cmd.add_argument(
        "--claude",
        action="store_true",
        help="Resume Claude Code using session id stored in Redis (claude_bridge)",
    )
    resume_cmd.add_argument(
        "--print-json",
        action="store_true",
        help="Print the LangGraph snapshot JSON to stdout",
    )
    resume_cmd.add_argument(
        "agent_cmd",
        nargs=argparse.REMAINDER,
        help="Optional command to run with OCTO_LANGGRAPH_* env set (use -- before argv)",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _ensure_scan_dev_dependencies(args)
    env_file = _env_file_from_arg(getattr(args, "env_file", None))
    example_override = _optional_example_file_from_arg(getattr(args, "example_file", None))

    try:
        if args.command == "bootstrap":
            bootstrap(env_file)
        elif args.command == "up":
            command_up(env_file, rewrite_conflicting_ports=args.rewrite_conflicting_ports)
        elif args.command == "verify":
            command_verify(env_file)
        elif args.command == "down":
            command_down(env_file)
        elif args.command == "force-clean":
            command_force_clean(env_file)
        elif args.command == "status":
            command_status(env_file)
        elif args.command == "diff-preview":
            command_diff_preview(args.repo, args.base, args.head, args.query)
        elif args.command == "review-diff":
            et = getattr(args, "export_tickets", None)
            export_path = Path(et).expanduser().resolve() if et else None
            command_review_diff(
                env_file,
                args.repo,
                args.base,
                args.head,
                args.query,
                export_path,
            )
        elif args.command == "chat":
            from local_ai_stack.octo_chat_repl import command_octo_chat

            return command_octo_chat(
                env_file,
                str(getattr(args, "repo", ".") or "."),
            )
        elif args.command == "export-tickets":
            command_export_tickets(
                getattr(args, "review_file", None),
                getattr(args, "snapshot_json", None),
                args.output,
                getattr(args, "query", "") or "",
            )
        elif args.command == "validate-config":
            validate_config(
                env_file,
                example_override,
                interactive=getattr(args, "interactive", True),
            )
        elif args.command == "install-hook":
            command_install_hook(
                getattr(args, "repo", "."),
                env_file,
                force=bool(getattr(args, "force", False)),
            )
        elif args.command == "install-verify-logic-hook":
            command_install_verify_logic_hook(
                getattr(args, "repo", "."),
                env_file,
                force=bool(getattr(args, "force", False)),
            )
        elif args.command == "pre-push-scan":
            command_pre_push_scan(
                env_file,
                getattr(args, "repo", "."),
                skip_health=bool(getattr(args, "skip_health", False)),
                skip_trivy=bool(getattr(args, "skip_trivy", False)),
                require_trivy=bool(getattr(args, "require_trivy", False)),
            )
        elif args.command == "privacy-monitor":
            from local_ai_stack.privacy_monitor import run_monitor_loop

            return run_monitor_loop(env_file)
        elif args.command == "data-wipe":
            command_data_wipe(
                assume_yes=bool(getattr(args, "yes", False)),
                passes=int(getattr(args, "passes", 1) or 1),
            )
        elif args.command == "build-optimized":
            command_build_optimized(env_file, no_cache=bool(getattr(args, "no_cache", False)))
        elif args.command == "swap-model":
            command_swap_model(
                env_file,
                str(getattr(args, "model", "") or ""),
                update_env=bool(getattr(args, "update_env", False)),
                skip_registry=bool(getattr(args, "skip_registry_check", False)),
                ignore_vram=bool(getattr(args, "ignore_vram", False)),
                vram_headroom=float(getattr(args, "vram_headroom", 1.15) or 1.15),
            )
        elif args.command == "logs":
            return command_logs(
                env_file,
                log_all=bool(getattr(args, "log_all", False)),
                tail=int(getattr(args, "tail", 200) or 200),
                follow=not bool(getattr(args, "no_follow", False)),
                timestamps=bool(getattr(args, "timestamps", False)),
                services=tuple(getattr(args, "services", None) or ()),
            )
        elif args.command == "doctor":
            return command_doctor(
                env_file,
                str(getattr(args, "repo", ".") or "."),
                strict=bool(getattr(args, "strict", False)),
                fix=bool(getattr(args, "fix", False)),
                accept_prune=bool(getattr(args, "accept_prune", False)),
            )
        elif args.command == "remediation-ui":
            src = ROOT / "src"
            if str(src) not in sys.path:
                sys.path.insert(0, str(src))
            from claude_bridge.remediation_tui import run_remediation_tui

            ff = getattr(args, "finding_file", None)
            xf = getattr(args, "fix_file", None)
            return run_remediation_tui(
                repo=Path(str(getattr(args, "repo", ".") or ".")).expanduser().resolve(),
                finding_file=Path(ff).expanduser().resolve() if ff else None,
                fix_file=Path(xf).expanduser().resolve() if xf else None,
                target=getattr(args, "target", None),
                demo=bool(getattr(args, "demo", False)),
            )
        elif args.command == "benchmark-models":
            src = ROOT / "src"
            if str(src) not in sys.path:
                sys.path.insert(0, str(src))
            from local_ai_stack.performance_profile import run_benchmark_models_with_namespace

            return run_benchmark_models_with_namespace(args)
        elif args.command == "benchmark":
            wd = getattr(args, "work_dir", None)
            work_parent = Path(wd).expanduser().resolve() if wd else (ROOT / ".local" / "benchmarks")
            oc = getattr(args, "output_csv", None)
            output_csv = Path(oc).expanduser().resolve() if oc else (ROOT / "performance.csv")
            rd = getattr(args, "repo_dir", None)
            repo_dir = Path(rd).expanduser().resolve() if rd else None
            return command_benchmark(
                env_file,
                git_url=str(getattr(args, "git_url", "") or ""),
                work_parent=work_parent,
                clone_depth=int(getattr(args, "depth", 40) or 40),
                output_csv=output_csv,
                model=getattr(args, "model", None),
                ollama_url=getattr(args, "ollama_url", None),
                base=getattr(args, "base", None),
                head=getattr(args, "head", None),
                skip_clone=bool(getattr(args, "skip_clone", False)),
                repo_dir=repo_dir,
                show_review=bool(getattr(args, "show_review", False)),
            )
        elif args.command == "resume":
            src = ROOT / "src"
            if str(src) not in sys.path:
                sys.path.insert(0, str(src))
            ac = list(getattr(args, "agent_cmd", None) or [])
            if ac and ac[0] == "--":
                ac = ac[1:]
            return command_resume(
                env_file,
                Path(getattr(args, "workspace", Path("."))),
                claude=bool(getattr(args, "claude", False)),
                agent_cmd=ac,
                print_json=bool(getattr(args, "print_json", False)),
            )
        else:
            parser.error(f"Unknown command: {args.command}")
    except subprocess.CalledProcessError as exc:
        _print(f"Command failed with exit code {exc.returncode}")
        return exc.returncode or 1
    except Exception as exc:  # noqa: BLE001
        _print(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

