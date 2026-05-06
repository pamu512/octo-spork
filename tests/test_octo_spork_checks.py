"""Unit tests for GitHub Checks helpers (no PyGithub required)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_bot.octo_spork_checks import (  # noqa: E402
    scan_outputs_indicate_critical,
)


class ScanOutputsCriticalTests(unittest.TestCase):
    def test_trivy_critical_row(self) -> None:
        md = "| Severity | Rule |\n| CRITICAL | CVE-123 |"
        self.assertTrue(scan_outputs_indicate_critical(md, None))

    def test_trivy_clear(self) -> None:
        md = "| Severity | Rule |\n| HIGH | CVE-123 |"
        self.assertFalse(scan_outputs_indicate_critical(md, None))

    def test_codeql_critical_section_with_rows(self) -> None:
        md = """### CodeQL — Critical findings

| Rule | Location |
| --- | --- |
| rule-id | src/x.py:1 |
"""
        self.assertTrue(scan_outputs_indicate_critical(None, md))

    def test_codeql_empty_state_not_critical(self) -> None:
        md = """### CodeQL — Critical findings

_No **Critical** findings._
"""
        self.assertFalse(scan_outputs_indicate_critical(None, md))


if __name__ == "__main__":
    unittest.main()
