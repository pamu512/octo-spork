"""Tests for :mod:`local_ai_stack.pre_push_scan` fail-closed behaviour."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local_ai_stack.pre_push_scan import (
    collect_trivy_critical_evidence,
    command_install_hook,
    run_pre_push_scan,
)


class TestCollectTrivyCriticalEvidence(unittest.TestCase):
    def test_missing_trivy_returns_skipped(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            _critical, _msgs, skipped = collect_trivy_critical_evidence(Path("."))
        self.assertTrue(skipped)

    def test_nonzero_returncode_empty_results_is_critical(self) -> None:
        completed = mock.Mock(returncode=1, stdout='{"Results":[]}', stderr="")
        with mock.patch("shutil.which", return_value="/usr/bin/trivy"):
            with mock.patch("subprocess.run", return_value=completed):
                critical, _msgs, skipped = collect_trivy_critical_evidence(Path("."))
        self.assertTrue(critical)
        self.assertFalse(skipped)


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

    def test_trivy_nonzero_empty_results_fails_scan(self) -> None:
        completed = mock.Mock(returncode=1, stdout='{"Results":[]}', stderr="")
        with mock.patch("shutil.which", return_value="/usr/bin/trivy"):
            with mock.patch("subprocess.run", return_value=completed):
                code = run_pre_push_scan(
                    Path("/dev/null"),
                    Path("."),
                    skip_health=True,
                    skip_trivy=False,
                )
        self.assertEqual(code, 1)


class TestCommandInstallHookFailClosed(unittest.TestCase):
    def test_chmod_oserror_does_not_claim_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git" / "hooks").mkdir(parents=True)
            env_file = repo / ".env"
            env_file.write_text("", encoding="utf-8")
            printed: list[str] = []
            with mock.patch("local_ai_stack.__main__._print", side_effect=printed.append):
                with mock.patch.object(Path, "chmod", side_effect=OSError("EPERM")):
                    with self.assertRaises(RuntimeError):
                        command_install_hook(str(repo), env_file, force=False)
            self.assertFalse(any("Wrote" in msg for msg in printed))


if __name__ == "__main__":
    unittest.main()
