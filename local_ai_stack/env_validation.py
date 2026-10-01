"""Env-file parse and validation for the local AI stack.

CLI path stays ``python -m local_ai_stack validate-config``. Shared parse
(``_parse_env_file``) is re-exported from ``__main__`` for status/bootstrap.
"""
from __future__ import annotations

import ipaddress
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / "deploy" / "local-ai" / ".env.local"
EXAMPLE_ENV_FILE = ROOT / "deploy" / "local-ai" / ".env.example"


def _print(message: str) -> None:
    print(message, flush=True)


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
