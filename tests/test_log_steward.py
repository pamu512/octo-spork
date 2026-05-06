"""Tests for :mod:`local_ai_stack.log_steward`."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from local_ai_stack.log_steward import (
    ARCHIVE_MAX_AGE_SECONDS,
    SIZE_THRESHOLD_BYTES,
    run_log_steward,
)


def test_merge_two_logs_same_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCTO_LOG_STEWARD", raising=False)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "one.log").write_text("alpha\n", encoding="utf-8")
    (logs / "two.log").write_text("beta\n", encoding="utf-8")

    run_log_steward(tmp_path, announce=None)

    combined = logs / ".steward" / "daily"
    files = list(combined.glob("*-combined.log"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "alpha" in body and "beta" in body
    assert not (logs / "one.log").exists()
    assert not (logs / "two.log").exists()


def test_compress_over_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCTO_LOG_STEWARD", raising=False)
    logs = tmp_path / "logs"
    logs.mkdir()
    big = logs / "big.log"
    with big.open("wb") as f:
        f.seek(SIZE_THRESHOLD_BYTES + 1024)
        f.write(b"\n")

    run_log_steward(tmp_path, announce=None)

    gz = logs / "big.log.gz"
    assert gz.is_file()
    assert not big.exists()


def test_purge_old_gzip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCTO_LOG_STEWARD", raising=False)
    logs = tmp_path / "logs"
    logs.mkdir()
    stale = logs / "old.log.gz"
    stale.write_bytes(b"x")
    old_ts = time.time() - ARCHIVE_MAX_AGE_SECONDS - 3600
    os.utime(stale, (old_ts, old_ts))

    run_log_steward(tmp_path, announce=None)

    assert not stale.exists()


def test_disabled_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OCTO_LOG_STEWARD", "0")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.log").write_text("x", encoding="utf-8")
    (logs / "b.log").write_text("y", encoding="utf-8")
    run_log_steward(tmp_path, announce=None)
    assert (logs / "a.log").exists()
