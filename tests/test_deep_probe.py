"""Tests for :mod:`local_ai_stack.deep_probe`."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from local_ai_stack.deep_probe import (
    deep_probe_once,
    probe_ollama_api_tags,
    run_deep_probe_until_ready,
)


def test_probe_ollama_api_tags_accepts_empty_models() -> None:
    payload = '{"models": []}'
    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = mock_open.return_value.__enter__.return_value
        mock_resp.read.return_value = payload.encode()
        mock_resp.status = 200
        mock_resp.getcode = lambda: 200
        ok, detail = probe_ollama_api_tags("http://127.0.0.1:11434")
    assert ok is True
    assert "model tag" in detail.lower()


def test_deep_probe_once_structure() -> None:
    with patch("local_ai_stack.deep_probe.probe_ollama_api_tags", return_value=(True, "ok")):
        with patch(
            "local_ai_stack.deep_probe.probe_postgres_select1",
            return_value=(True, "SELECT 1 ok"),
        ):
            with patch("local_ai_stack.deep_probe.probe_redis_ping", return_value=(True, "PONG")):
                ok, results = deep_probe_once({})
    assert ok is True
    assert set(results.keys()) == {"ollama", "postgres", "redis"}


def test_run_deep_probe_until_ready_times_out() -> None:
    with patch("local_ai_stack.deep_probe.deep_probe_once") as mock_once:
        mock_once.return_value = (
            False,
            {"ollama": (False, "down"), "postgres": (False, "down"), "redis": (False, "down")},
        )
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="DeepProbe timed out"):
            run_deep_probe_until_ready({}, timeout_sec=0.15)
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0


def test_run_deep_probe_until_ready_success_first_round() -> None:
    called: list[str] = []

    def announce(msg: str) -> None:
        called.append(msg)

    with patch("local_ai_stack.deep_probe.deep_probe_once") as mock_once:
        mock_once.return_value = (
            True,
            {"ollama": (True, "ok"), "postgres": (True, "ok"), "redis": (True, "ok")},
        )
        run_deep_probe_until_ready({}, announce=announce, timeout_sec=60.0)
    mock_once.assert_called_once()
    assert any("Stack Ready" in c for c in called)
