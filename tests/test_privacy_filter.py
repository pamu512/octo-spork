"""Tests for observability privacy_filter (regex redaction + symmetric un-redaction)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class PrivacyFilterTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("OCTO_PRIVACY_FILTER", None)

    def test_redacts_email_ip_and_round_trips(self) -> None:
        from observability.privacy_filter import PrivacyFilter, redact_for_llm, unredact_response

        raw = "Contact admin@example.com from 192.168.1.10."
        red, m = redact_for_llm(raw)
        self.assertIn("<REDACTED_SECRET_", red)
        self.assertNotIn("example.com", red)
        self.assertNotIn("192.168.1.10", red)
        self.assertEqual(len(m), 2)
        restored = unredact_response(red, m)
        self.assertEqual(restored, raw)

        pf = PrivacyFilter(enabled=True)
        r2, m2 = pf.filter_request(raw)
        self.assertEqual(r2, red)
        self.assertEqual(pf.filter_response(r2, m2), raw)

    def test_model_echo_unredacts(self) -> None:
        from observability.privacy_filter import redact_for_llm, unredact_response

        raw = "token sk-abcdefghijklmnopqrstuvwxyz0123456789abcd here"
        red, m = redact_for_llm(raw)
        self.assertNotIn("sk-", red)
        fake_reply = f"The key is {list(m.keys())[0]} per policy."
        self.assertEqual(unredact_response(fake_reply, m).count("sk-"), 1)

    def test_disabled_env_leaves_text(self) -> None:
        from observability import privacy_filter as pf

        os.environ["OCTO_PRIVACY_FILTER"] = "0"
        self.assertFalse(pf.is_enabled())
        raw = "mail user@corp.internal use 10.0.0.1"
        red, m = pf.redact_for_llm(raw)
        self.assertEqual(red, raw)
        self.assertEqual(m, {})

    def test_overlapping_spans_merge(self) -> None:
        from observability.privacy_filter import redact_for_llm

        # Nested/overlapping hits should become one redacted span.
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\nMII\n-----END RSA PRIVATE KEY-----"
        )
        red, m = redact_for_llm(pem)
        self.assertEqual(red.count("<REDACTED_SECRET_"), 1)
        self.assertIn(pem, m.values())


if __name__ == "__main__":
    unittest.main()
