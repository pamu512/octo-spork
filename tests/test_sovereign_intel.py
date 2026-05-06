"""Tests for Sovereign Intelligence cross-repo pattern store."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class SovereignIntelTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("OCTO_SPORK_REPO_ROOT", None)

    def test_pattern_names_from_other_repos(self) -> None:
        from sovereign_intel.store import pattern_names_from_other_repos, record_critical_pattern_hits

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo_a = base / "a"
            repo_b = base / "b"
            repo_a.mkdir()
            repo_b.mkdir()
            os.environ["OCTO_SPORK_REPO_ROOT"] = str(base)

            record_critical_pattern_hits(repo_a, ["access key ID"])
            names = pattern_names_from_other_repos(repo_b)
            self.assertIn("access key ID", names)
            self.assertEqual(pattern_names_from_other_repos(repo_a), [])

    def test_scan_subset_matches_only_named_patterns(self) -> None:
        from github_bot.secret_scan import scan_text_for_pattern_names

        text = "AKIA0123456789ABCDEF"
        hits = scan_text_for_pattern_names(text, {"access key ID"})
        self.assertTrue(any(h.pattern_name == "access key ID" for h in hits))
        hits2 = scan_text_for_pattern_names(text, {"secret access key (assignment)"})
        self.assertEqual(hits2, [])

    def test_attach_populates_block_when_fleet_data(self) -> None:
        from sovereign_intel.attach import attach_sovereign_intel
        from sovereign_intel.store import record_critical_pattern_hits

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            ra = base / "ra"
            rb = base / "rb"
            ra.mkdir()
            rb.mkdir()
            os.environ["OCTO_SPORK_REPO_ROOT"] = str(base)
            record_critical_pattern_hits(ra, ["personal access token"])

            snap: dict = {"scan_root": str(rb)}
            attach_sovereign_intel(snap)
            body = snap.get("sovereign_intel_block", "")
            self.assertIn("Sovereign Intelligence", body)
            self.assertIn("personal access token", body)


if __name__ == "__main__":
    unittest.main()
