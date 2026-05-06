"""Tests for :mod:`remediation.rescan_loop`."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class RescanLoopTests(unittest.TestCase):
    def test_raises_when_cve_still_present(self) -> None:
        from remediation.exceptions import VerificationFailedError
        from remediation.rescan_loop import RescanLoop
        from remediation.validator import PatchValidationResult, PatchValidator

        vuln_json = {
            "SchemaVersion": 2,
            "Results": [
                {
                    "Target": "package.json",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2024-99999",
                            "PkgName": "lodash",
                            "InstalledVersion": "4.17.20",
                        }
                    ],
                }
            ],
        }

        def fake_validate(
            self: PatchValidator,
            diff_text: str,
            *,
            on_apply_success=None,
        ) -> PatchValidationResult:
            if on_apply_success:
                on_apply_success(Path("/tmp/fake-clone"))
            return PatchValidationResult(True)

        with tempfile.TemporaryDirectory() as tmp:
            v = PatchValidator(Path(tmp))
            loop = RescanLoop(v, "CVE-2024-99999", trivy_executable="/usr/bin/true")
            with patch.object(PatchValidator, "validate", fake_validate):
                with patch("remediation.rescan_loop._run_trivy_fs_json", return_value=vuln_json):
                    with self.assertRaises(VerificationFailedError) as ctx:
                        loop.run("diff")
            self.assertIn("CVE-2024-99999", str(ctx.exception))
            self.assertIn("lodash", ctx.exception.snippet)

    def test_passes_when_cve_cleared(self) -> None:
        from remediation.rescan_loop import RescanLoop
        from remediation.validator import PatchValidationResult, PatchValidator

        clean_json = {"Results": [{"Target": "x", "Vulnerabilities": []}]}

        def fake_validate(
            self: PatchValidator,
            diff_text: str,
            *,
            on_apply_success=None,
        ) -> PatchValidationResult:
            if on_apply_success:
                on_apply_success(Path("/tmp/fake-clone"))
            return PatchValidationResult(True)

        with tempfile.TemporaryDirectory() as tmp:
            v = PatchValidator(Path(tmp))
            loop = RescanLoop(v, "CVE-2024-99999", trivy_executable="/ignored")
            with patch.object(PatchValidator, "validate", fake_validate):
                with patch("remediation.rescan_loop._run_trivy_fs_json", return_value=clean_json):
                    result = loop.run("diff")
            self.assertTrue(result.success)


class CollectVulnTests(unittest.TestCase):
    def test_collect_nested(self) -> None:
        from remediation.rescan_loop import _collect_vuln_records_with_id

        data = {"Results": [{"Vulnerabilities": [{"VulnerabilityID": "CVE-2023-1"}]}]}
        got = _collect_vuln_records_with_id(data, "cve-2023-1")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["VulnerabilityID"], "CVE-2023-1")


if __name__ == "__main__":
    unittest.main()
