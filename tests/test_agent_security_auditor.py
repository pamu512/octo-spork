"""Tests for ``claude_bridge.agent_security_auditor``."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claude_bridge import agent_security_auditor as aud  # noqa: E402


class ClassifyTests(unittest.TestCase):
    def test_rm_rf_compact(self) -> None:
        self.assertEqual(aud.classify_line("running: rm -rf /tmp/x"), "rm_rf")

    def test_rm_rf_split_flags(self) -> None:
        self.assertEqual(aud.classify_line('exec bash -c "rm -r -f ./dist"'), "rm_rf")

    def test_rm_only_r_no_match(self) -> None:
        self.assertIsNone(aud.classify_line("rm -r /tmp"))

    def test_curl_public_ip(self) -> None:
        self.assertEqual(
            aud.classify_line("curl -s http://8.8.8.8/path"),
            "curl_public_ip",
        )

    def test_curl_private_ip_ignored(self) -> None:
        self.assertIsNone(aud.classify_line("curl http://192.168.1.5/foo"))

    def test_curl_hostname_no_literal_ip(self) -> None:
        self.assertIsNone(aud.classify_line("curl https://example.com/api"))


class ViolationHandlingTests(unittest.TestCase):
    def test_logs_and_kills(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log_p = Path(td) / "agent_security.log"
            with mock.patch.object(aud, "kill_agent_container", return_value=(True, "ok")):
                aud.handle_violation(
                    kind="rm_rf",
                    line="rm -rf /\n",
                    container="test-c",
                    log_file=log_p,
                )
            text = log_p.read_text(encoding="utf-8")
            row = json.loads(text.strip().splitlines()[0])
            self.assertEqual(row["event"], "Security Violation")
            self.assertEqual(row["kind"], "rm_rf")
            self.assertEqual(row["container"], "test-c")


if __name__ == "__main__":
    unittest.main()
