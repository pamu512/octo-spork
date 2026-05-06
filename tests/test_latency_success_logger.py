"""Tests for remediation latency SQLite logging and context reset."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class LatencySuccessLoggerTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in ("OCTO_LATENCY_SUCCESS_DB", "OCTO_REMEDIATION_SLOW_TTR_SEC", "OCTO_CONTEXT_RESET_DISABLE"):
            os.environ.pop(k, None)

    def test_sqlite_roundtrip(self) -> None:
        from observability.latency_success_logger import (
            RemediationLatencyRow,
            latency_log_db_path,
            log_remediation_latency,
        )

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "lat.db"
            os.environ["OCTO_LATENCY_SUCCESS_DB"] = str(db)
            rid = log_remediation_latency(
                RemediationLatencyRow(
                    created_at_utc="2026-01-01T00:00:00Z",
                    scan_start_unix=100.0,
                    event_end_unix=400.0,
                    ttr_seconds=300.0,
                    success_verified_patch=True,
                    outcome="verified_patch",
                    pr_html_url="https://github.com/o/r/pull/1",
                    cve_id="CVE-2024-1",
                    extra={"k": "v"},
                )
            )
            self.assertGreater(rid, 0)
            conn = sqlite3.connect(str(latency_log_db_path()))
            row = conn.execute("SELECT outcome, ttr_seconds FROM remediation_latency WHERE id=?", (rid,)).fetchone()
            conn.close()
            self.assertEqual(row[0], "verified_patch")
            self.assertEqual(row[1], 300.0)

    def test_aggressive_prune_cap(self) -> None:
        from observability.latency_success_logger import (
            aggressive_prune_effective_max_chars,
            set_aggressive_pruning_active,
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            set_aggressive_pruning_active(repo_root=root, reason="test", ttl_sec=3600.0)
            cap = aggressive_prune_effective_max_chars(100_000, repo_root=root)
            self.assertLessEqual(cap, 100_000)
            self.assertGreater(cap, 1000)

    def test_slow_ttr_triggers_reset_mocked(self) -> None:
        from observability import latency_success_logger as ll

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(ll, "clear_ollama_runtime_cache", return_value=["m"]) as cc:
                with mock.patch.object(ll, "set_aggressive_pruning_active") as sa:
                    ll.maybe_trigger_context_reset_for_ttr(
                        ttr_seconds=400.0,
                        success_verified_patch=True,
                        repo_root=root,
                    )
            cc.assert_called_once()
            sa.assert_called_once()


if __name__ == "__main__":
    unittest.main()
