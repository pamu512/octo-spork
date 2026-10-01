"""Thin tests for :mod:`local_ai_stack.env_validation` (pure validators + fail-closed validate).

Does not cover interactive prompt UIs. CLI ``validate-config --no-interactive``
pins exit code 1 and operator-facing error text.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from local_ai_stack.env_validation import (
    _collect_env_validation_errors,
    _env_port_suffix_key,
    _parse_env_example_keys_ordered,
    _parse_env_file,
    _seed_env_secrets,
    _validate_boolean_field,
    _validate_http_url_field,
    _validate_ollama_host_binding,
    _validate_positive_int_field,
    _value_is_placeholder_secret,
    validate_config,
)

_MAIN_PY = Path(__file__).resolve().parents[1] / "local_ai_stack" / "__main__.py"


def _mini_pair(tmp_path: Path) -> tuple[Path, Path]:
    example = tmp_path / ".env.example"
    example.write_text(
        "\n".join(
            [
                "OLLAMA_BASE_URL=http://127.0.0.1:11434",
                "OLLAMA_HOST=0.0.0.0:11434",
                "SEARXNG_PORT=8080",
                "AGENTICSEEK_NATIVE_ARM64=false",
                "GROUNDED_REVIEW_CACHE_TTL_SECONDS=900",
                "SEARXNG_SECRET_KEY=ok-not-placeholder",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env = tmp_path / ".env.local"
    env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return env, example


def test_validate_http_url_field() -> None:
    assert _validate_http_url_field("OLLAMA_BASE_URL", "http://127.0.0.1:11434") is None
    assert _validate_http_url_field("OLLAMA_BASE_URL", "https://host.example/v1") is None
    assert "empty" in (_validate_http_url_field("OLLAMA_BASE_URL", "  ") or "")
    assert "missing" in (_validate_http_url_field("OLLAMA_BASE_URL", None) or "")  # type: ignore[arg-type]
    assert "http or https" in (_validate_http_url_field("OLLAMA_BASE_URL", "ftp://x") or "")
    assert "no host" in (_validate_http_url_field("OLLAMA_BASE_URL", "http://") or "")


def test_validate_ollama_host_binding() -> None:
    assert _validate_ollama_host_binding("OLLAMA_HOST", "0.0.0.0:11434") is None
    assert _validate_ollama_host_binding("OLLAMA_HOST", "localhost:11434") is None
    assert _validate_ollama_host_binding("OLLAMA_HOST", "[::1]:11434") is None
    assert "host:port" in (_validate_ollama_host_binding("OLLAMA_HOST", "127.0.0.1") or "")
    assert "between 1 and 65535" in (_validate_ollama_host_binding("OLLAMA_HOST", "127.0.0.1:0") or "")
    assert "not an integer" in (_validate_ollama_host_binding("OLLAMA_HOST", "127.0.0.1:abc") or "")
    assert "empty" in (_validate_ollama_host_binding("OLLAMA_HOST", "") or "")
    assert "IPv6 bracket" in (_validate_ollama_host_binding("OLLAMA_HOST", "[::1") or "")


def test_validate_positive_int_and_boolean() -> None:
    assert _validate_positive_int_field("SEARXNG_PORT", "8080") is None
    assert "positive" in (_validate_positive_int_field("SEARXNG_PORT", "0") or "")
    assert _validate_positive_int_field("TTL", "0", allow_zero=True) is None
    assert "integer" in (_validate_positive_int_field("SEARXNG_PORT", "8.5") or "")
    assert _validate_boolean_field("FLAG", "true") is None
    assert _validate_boolean_field("FLAG", "0") is None
    assert "true/false" in (_validate_boolean_field("FLAG", "maybe") or "")


def test_port_suffix_and_placeholder_secret() -> None:
    assert _env_port_suffix_key("SEARXNG_PORT") is True
    assert _env_port_suffix_key("REDIS_PORT") is True
    assert _env_port_suffix_key("REPORT") is False
    assert _value_is_placeholder_secret("SEARXNG_SECRET_KEY", "replace-me-with-a-random-secret")
    assert _value_is_placeholder_secret("N8N_ENCRYPTION_KEY", "changeme")
    assert _value_is_placeholder_secret("SEARXNG_SECRET_KEY", "")
    assert not _value_is_placeholder_secret("SEARXNG_SECRET_KEY", "a" * 48)
    assert not _value_is_placeholder_secret("OPENAI_API_KEY", "")


def test_collect_env_validation_errors_routes_keys() -> None:
    with pytest.raises(ValueError, match="env_map is required"):
        _collect_env_validation_errors(None)  # type: ignore[arg-type]
    good = {
        "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
        "OLLAMA_HOST": "0.0.0.0:11434",
        "AGENTICSEEK_NATIVE_ARM64": "false",
        "GROUNDED_REVIEW_CACHE_TTL_SECONDS": "0",
        "SEARXNG_PORT": "8080",
        "OTHER": "ignored",
    }
    assert _collect_env_validation_errors(good) == []
    bad = {
        "OLLAMA_BASE_URL": "not-a-url",
        "OLLAMA_HOST": "nope",
        "AGENTICSEEK_NATIVE_ARM64": "maybe",
        "GROUNDED_REVIEW_CACHE_TTL_SECONDS": "-1",
        "SEARXNG_PORT": "0",
    }
    errs = _collect_env_validation_errors(bad)
    assert len(errs) == 5
    joined = "\n".join(errs)
    assert "OLLAMA_BASE_URL" in joined
    assert "OLLAMA_HOST" in joined
    assert "AGENTICSEEK_NATIVE_ARM64" in joined
    assert "GROUNDED_REVIEW_CACHE_TTL_SECONDS" in joined
    assert "SEARXNG_PORT" in joined


def test_parse_env_file_and_example_keys(tmp_path: Path) -> None:
    missing = tmp_path / "absent.env"
    assert _parse_env_file(missing) == {}
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "\n"
        "FOO=bar\n"
        'QUOTED="x=y"\n'
        "DUP=first\n"
        "DUP=second\n"
        "NOEQUALS\n"
        "  SPACED = 1  \n",
        encoding="utf-8",
    )
    parsed = _parse_env_file(env)
    assert parsed["FOO"] == "bar"
    assert parsed["QUOTED"] == "x=y"
    assert parsed["DUP"] == "second"
    assert parsed["SPACED"] == "1"
    assert "NOEQUALS" not in parsed

    example = tmp_path / ".env.example"
    example.write_text("# c\nA=1\nB=2\nA=ignored\n", encoding="utf-8")
    assert _parse_env_example_keys_ordered(example) == ["A", "B"]
    with pytest.raises(ValueError, match="example_path is required"):
        _parse_env_example_keys_ordered(None)  # type: ignore[arg-type]


def test_validate_config_fail_closed_and_ok(tmp_path: Path) -> None:
    env, example = _mini_pair(tmp_path)
    merged = validate_config(env, example, interactive=False)
    assert merged["SEARXNG_PORT"] == "8080"

    env.write_text("OLLAMA_BASE_URL=http://127.0.0.1:11434\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"Configuration validation failed:[\s\S]*Missing keys") as missing:
        validate_config(env, example, interactive=False)
    assert "SEARXNG_SECRET_KEY" in str(missing.value)

    env, example = _mini_pair(tmp_path)
    env.write_text(
        env.read_text(encoding="utf-8").replace("ok-not-placeholder", "replace-me-with-a-random-secret"),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="Unresolved placeholder or empty stack secrets"):
        validate_config(env, example, interactive=False)

    env, example = _mini_pair(tmp_path)
    env.write_text(
        env.read_text(encoding="utf-8").replace("http://127.0.0.1:11434", "ftp://nope"),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match=r"Configuration validation failed:[\s\S]*http or https"):
        validate_config(env, example, interactive=False)


def test_seed_env_secrets_replaces_placeholder_suffix(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "SEARXNG_SECRET_KEY=replace-me-with-a-random-secret\n"
        "N8N_ENCRYPTION_KEY=replace-me-with-a-random-secret\n"
        "OTHER=keep\n",
        encoding="utf-8",
    )
    _seed_env_secrets(env)
    parsed = _parse_env_file(env)
    assert parsed["OTHER"] == "keep"
    assert parsed["SEARXNG_SECRET_KEY"] != "replace-me-with-a-random-secret"
    assert parsed["N8N_ENCRYPTION_KEY"] != "replace-me-with-a-random-secret"
    assert len(parsed["SEARXNG_SECRET_KEY"]) == 48


def test_cli_validate_config_no_interactive_returns_1(tmp_path: Path) -> None:
    env, example = _mini_pair(tmp_path)
    env.write_text("OLLAMA_BASE_URL=http://127.0.0.1:11434\n", encoding="utf-8")
    sub = subprocess.run(
        [
            sys.executable,
            "-m",
            "local_ai_stack",
            "validate-config",
            "--no-interactive",
            "--env-file",
            str(env),
            "--example-file",
            str(example),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sub.returncode == 1
    combined = f"{sub.stdout}{sub.stderr}"
    assert "Error:" in combined
    assert "Configuration validation failed" in combined
    assert "Missing keys" in combined


def test_main_reexports_same_objects_and_does_not_redefine() -> None:
    src = _MAIN_PY.read_text(encoding="utf-8")
    for name in (
        "def _parse_env_file",
        "def _ensure_env_file",
        "def _collect_env_validation_errors",
        "def validate_config",
        "def _seed_env_secrets",
        "def _validate_http_url_field",
        "def _validate_ollama_host_binding",
        "def _validate_boolean_field",
        "def _validate_positive_int_field",
    ):
        assert name not in src, f"duplicate validator left in __main__.py: {name}"

    from local_ai_stack import env_validation
    from local_ai_stack import __main__ as stack_main

    assert stack_main._parse_env_file is env_validation._parse_env_file
    assert stack_main.validate_config is env_validation.validate_config
    assert stack_main._seed_env_secrets is env_validation._seed_env_secrets
    assert stack_main._ensure_env_file is env_validation._ensure_env_file
