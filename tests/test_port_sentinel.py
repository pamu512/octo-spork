"""Unit tests for ``local_ai_stack.port_sentinel``."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from local_ai_stack.port_sentinel import (
    PortConflict,
    _merge_override_services,
    is_stale_docker_listener,
    run_port_sentinel,
)


def test_is_stale_docker_listener_recognizes_proxy() -> None:
    assert is_stale_docker_listener(1, "docker-proxy") is True
    assert is_stale_docker_listener(1, "nginx") is False


def test_merge_override_redis_only() -> None:
    merged = _merge_override_services(None, 6379, 16379, ("REDIS_HOST_PORT",))
    assert merged["services"]["redis"]["ports"] == ["16379:6379"]
    assert merged["x-octo-port-sentinel"]["remapped_ports"]["6379"] == 16379


def test_run_port_sentinel_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OCTO_PORT_SENTINEL", "0")
    env_file = tmp_path / ".env.local"
    env_file.write_text("SEARXNG_PORT=8080\n", encoding="utf-8")
    log = logging.getLogger("test")
    out = run_port_sentinel(tmp_path, env_file, {"SEARXNG_PORT": "8080"}, logger=log)
    assert out["SEARXNG_PORT"] == "8080"


@patch("local_ai_stack.port_sentinel._gather_conflicts")
def test_run_port_sentinel_no_conflicts(mock_gather, tmp_path: Path) -> None:
    mock_gather.return_value = []
    env_file = tmp_path / ".env.local"
    env_file.write_text("x=1\n", encoding="utf-8")
    log = logging.getLogger("test")
    out = run_port_sentinel(tmp_path, env_file, {}, logger=log)
    assert out == {}


def test_non_interactive_remap_searxng(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OCTO_PORT_SENTINEL_ACTION", "remap")
    env_file = tmp_path / ".env.local"
    env_file.write_text("SEARXNG_PORT=8080\n", encoding="utf-8")
    log = logging.getLogger("test")

    def fake_pick(_m: object, host: str, start: int, reserved: set[int]) -> int:
        return 18080

    with patch(
        "local_ai_stack.port_sentinel._gather_conflicts",
        return_value=[
            PortConflict(port=8080, pids=(999,), process_names=("docker-proxy",)),
        ],
    ):
        with patch("local_ai_stack.port_sentinel._tcp_port_is_in_use", return_value=True):
            with patch("local_ai_stack.port_sentinel._should_skip_ollama_busy", return_value=False):
                with patch("local_ai_stack.port_sentinel.listening_process_ids", return_value=(999,)):
                    with patch("local_ai_stack.port_sentinel.process_command_name", return_value="docker-proxy"):
                        with patch("local_ai_stack.port_sentinel.sys.stdin.isatty", return_value=False):
                            with patch(
                                "local_ai_stack.port_sentinel._pick_free_host_port", side_effect=fake_pick
                            ):
                                with patch("local_ai_stack.__main__._rewrite_env_file_string_values") as mock_rw:
                                    out = run_port_sentinel(
                                        tmp_path,
                                        env_file,
                                        {"SEARXNG_PORT": "8080"},
                                        logger=log,
                                    )
    assert out["SEARXNG_PORT"] == "18080"
    mock_rw.assert_called_once()
