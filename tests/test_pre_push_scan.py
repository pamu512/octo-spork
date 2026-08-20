"""Tests for :mod:`local_ai_stack.pre_push_scan` fail-closed behaviour."""
from __future__ import annotations

import textwrap
import unittest
from pathlib import Path
from unittest import mock

from local_ai_stack.pre_push_scan import (
    collect_trivy_critical_evidence,
    run_pre_push_scan,
)


class TestCollectTrivyCriticalEvidence(unittest.TestCase):
    def test_missing_trivy_returns_skipped(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            _critical, _msgs, skipped = collect_trivy_critical_evidence(Path("."))
        self.assertTrue(skipped)


class TestRunPrePushScanFailClosed(unittest.TestCase):
    """Missing trivy binary must cause a non-zero exit unless --skip-trivy."""

    def test_missing_trivy_fails(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            code = run_pre_push_scan(
                Path("/dev/null"),
                Path("."),
                skip_health=True,
                skip_trivy=False,
            )
        self.assertEqual(code, 1)

    def test_skip_trivy_allows_missing(self) -> None:
        code = run_pre_push_scan(
            Path("/dev/null"),
            Path("."),
            skip_health=True,
            skip_trivy=True,
        )
        self.assertEqual(code, 0)

    def test_clean_scan_returns_zero(self) -> None:
        with mock.patch(
            "local_ai_stack.pre_push_scan.collect_trivy_critical_evidence",
            return_value=(False, [], False),
        ):
            code = run_pre_push_scan(
                Path("/dev/null"),
                Path("."),
                skip_health=True,
                skip_trivy=False,
            )
        self.assertEqual(code, 0)

    def test_critical_finding_fails(self) -> None:
        with mock.patch(
            "local_ai_stack.pre_push_scan.collect_trivy_critical_evidence",
            return_value=(True, ["[security] CRITICAL vuln CVE-2099-1"], False),
        ):
            code = run_pre_push_scan(
                Path("/dev/null"),
                Path("."),
                skip_health=True,
                skip_trivy=False,
            )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
