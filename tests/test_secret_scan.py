"""Tests for regex secret scan on PR diffs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class SecretScanTests(unittest.TestCase):
    def test_detects_aws_access_key_id(self) -> None:
        from github_bot.secret_scan import scan_diff_text

        diff = """
diff --git a/creds.env b/creds.env
+AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
"""
        hits = scan_diff_text(diff)
        self.assertTrue(any("AWS" in h.category for h in hits))

    def test_detects_github_pat_prefix(self) -> None:
        from github_bot.secret_scan import scan_diff_text

        long_pat = "ghp_" + ("x" * 36)
        diff = f"+token={long_pat}\n"
        hits = scan_diff_text(diff)
        self.assertTrue(any("GitHub" in h.category for h in hits))

    def test_detects_stripe_sk(self) -> None:
        from github_bot.secret_scan import scan_diff_text

        # Build without a contiguous sk_live_* literal (GitHub push protection).
        stripe_like = bytes([0x73, 0x6B, 0x5F, 0x6C, 0x69, 0x76, 0x65, 0x5F]).decode("ascii") + "0" * 24
        diff = f"+key = {stripe_like}\n"
        hits = scan_diff_text(diff)
        self.assertTrue(any("Stripe" in h.category for h in hits))

    def test_skip_env_disables(self) -> None:
        from github_bot.secret_scan import scan_diff_text

        diff = "+x=AKIAIOSFODNN7EXAMPLE\n"
        with patch.dict("os.environ", {"OCTO_SPORK_SKIP_SECRET_SCAN": "1"}):
            self.assertEqual(scan_diff_text(diff), [])

    def test_format_alert_never_shows_full_secret(self) -> None:
        from github_bot.secret_scan import SecretFinding, format_critical_alert_comment

        body = format_critical_alert_comment(
            html_url="https://github.com/o/r/pull/1",
            title="t",
            findings=[
                SecretFinding(
                    category="AWS (access key ID)",
                    redacted_preview="AKIA…MPLE _(id:abc)_",
                    pattern_name="x",
                )
            ],
        )
        self.assertIn("Critical Alert", body)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", body)


if __name__ == "__main__":
    unittest.main()
