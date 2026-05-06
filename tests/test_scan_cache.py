"""Tests for ``ScanCache`` and SARIF merge helpers."""

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


class ScanCacheTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in ("OCTO_SPORK_SCAN_CACHE", "OCTO_SPORK_SCAN_CACHE_DIR"):
            os.environ.pop(k, None)

    def test_put_get_roundtrip(self) -> None:
        from github_bot.scan_cache import ScanCache, ScanCacheKey

        with tempfile.TemporaryDirectory() as tmp:
            cache = ScanCache(root=Path(tmp))
            key = ScanCacheKey(repo_full_name="o/r", commit_sha="abc123", scanner="trivy")
            payload = {"version": "2.1.0", "runs": [{"results": [{"ruleId": "x"}]}]}
            cache.put_sarif(key, payload)
            got = cache.get_sarif(key)
            self.assertEqual(got["runs"][0]["results"][0]["ruleId"], "x")

    def test_merge_prefers_delta_for_changed_paths(self) -> None:
        from github_bot.scan_cache import merge_sarif_base_and_delta

        base = {
            "runs": [
                {
                    "tool": {"driver": {"name": "trivy"}},
                    "results": [
                        {
                            "ruleId": "old",
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "src/a.py"},
                                        "region": {"startLine": 1},
                                    }
                                }
                            ],
                        },
                        {
                            "ruleId": "keep",
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "src/b.py"},
                                        "region": {"startLine": 2},
                                    }
                                }
                            ],
                        },
                    ],
                }
            ]
        }
        delta = {
            "runs": [
                {
                    "tool": {"driver": {"name": "trivy"}},
                    "results": [
                        {
                            "ruleId": "new",
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "src/a.py"},
                                        "region": {"startLine": 10},
                                    }
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        merged = merge_sarif_base_and_delta(base, delta, changed_paths={"src/a.py"})
        rules = [r["ruleId"] for r in merged["runs"][0]["results"]]
        self.assertIn("keep", rules)
        self.assertIn("new", rules)
        self.assertNotIn("old", rules)


if __name__ == "__main__":
    unittest.main()
