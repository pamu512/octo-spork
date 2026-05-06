"""Tests for scan/remediation dev-dependency preflight in ``local_ai_stack.__main__``."""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class ScanDevDepsTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("OCTO_SKIP_DEV_DEP_CHECK", None)

    def test_all_present_no_exit(self) -> None:
        import local_ai_stack.__main__ as las

        ns = argparse.Namespace(command="review-diff")
        with mock.patch.object(las, "_missing_scan_dev_dependencies", return_value=[]):
            las._ensure_scan_dev_dependencies(ns)

    def test_missing_triggers_exit(self) -> None:
        import local_ai_stack.__main__ as las

        ns = argparse.Namespace(command="review-diff")
        with mock.patch.object(las, "_missing_scan_dev_dependencies", return_value=["trivy CLI"]):
            with mock.patch.object(las.sys, "exit", side_effect=RuntimeError("exit 2")) as ex:
                with self.assertRaises(RuntimeError):
                    las._ensure_scan_dev_dependencies(ns)
            ex.assert_called_once_with(2)

    def test_skip_env_bypasses(self) -> None:
        import local_ai_stack.__main__ as las

        os.environ["OCTO_SKIP_DEV_DEP_CHECK"] = "1"
        ns = argparse.Namespace(command="review-diff")
        with mock.patch.object(las, "_missing_scan_dev_dependencies") as m:
            las._ensure_scan_dev_dependencies(ns)
        m.assert_not_called()

    def test_pre_push_skip_trivy_does_not_require_trivy(self) -> None:
        import local_ai_stack.__main__ as las

        calls: list[bool] = []

        def _capture(**kw: bool) -> list[str]:
            calls.append(kw.get("require_trivy", True))
            return []

        ns = argparse.Namespace(command="pre-push-scan", skip_trivy=True)
        with mock.patch.object(las, "_missing_scan_dev_dependencies", side_effect=_capture):
            las._ensure_scan_dev_dependencies(ns)
        self.assertEqual(calls, [False])


if __name__ == "__main__":
    unittest.main()
