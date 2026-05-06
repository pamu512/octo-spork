"""Unit tests for outbound privacy monitor helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from local_ai_stack import privacy_monitor as pm


class PrivacyMonitorTests(unittest.TestCase):
    def test_is_local_only_enabled_from_dict(self) -> None:
        self.assertTrue(pm.is_local_only_enabled({"LOCAL_AI_PRIVACY_MODE": "local-only"}))
        self.assertTrue(pm.is_local_only_enabled({"LOCAL_AI_PRIVACY_MODE": "strict"}))
        self.assertFalse(pm.is_local_only_enabled({"LOCAL_AI_PRIVACY_MODE": ""}))
        self.assertFalse(pm.is_local_only_enabled({}))

    def test_read_violation_drop_packets_parses_drop_line(self) -> None:
        stdout = """Chain OCTO_SPORK_PRIV_VIOL (2 references)
 pkts      bytes target     prot opt in     out     source               destination
    0        0 LOG        all  --  *      *       0.0.0.0/0            0.0.0.0/0
   42   123456 DROP       all  --  *      *       0.0.0.0/0            0.0.0.0/0
"""
        with patch.object(pm, "_iptables", return_value=MagicMock(returncode=0, stdout=stdout)):
            self.assertEqual(pm.read_violation_drop_packets(), 42)

    def test_read_violation_drop_packets_chain_missing(self) -> None:
        with patch.object(
            pm, "_iptables", return_value=MagicMock(returncode=1, stdout="", stderr="nope")
        ):
            self.assertIsNone(pm.read_violation_drop_packets())

    def test_parse_env_simple(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.env"
            p.write_text('LOCAL_AI_PRIVACY_MODE=local-only\n# c\nFOO="bar"\n', encoding="utf-8")
            env = pm._parse_env_simple(p)
            self.assertEqual(env.get("LOCAL_AI_PRIVACY_MODE"), "local-only")
            self.assertEqual(env.get("FOO"), "bar")


if __name__ == "__main__":
    unittest.main()
