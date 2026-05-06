"""Tests for ``github_bot.user_summary_identity``."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class UserSummaryIdentityTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in ("OCTO_USER_SUMMARY_ENABLED", "OCTO_USER_SUMMARY_JSON", "OCTO_SPORK_REPO_ROOT"):
            os.environ.pop(k, None)

    def test_defaults_match_octospork_baseline(self) -> None:
        from github_bot.user_summary_identity import UserSummaryIdentity

        ident = UserSummaryIdentity()
        line = ident.baseline_one_liner()
        self.assertIn("Octo-spork", line)
        self.assertIn("solo developer", line)
        self.assertIn("local-first", line)
        self.assertIn("fraud-infra", line)

    def test_json_overrides_tone_and_stack(self) -> None:
        from github_bot import user_summary_identity as mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {
                "identity": {
                    "tone": "friendly",
                    "technical_depth": "medium",
                    "stack_assumptions": ["Rust", "Wasm"],
                    "values": ["privacy", "supply-chain security"],
                }
            }
            path = root / ".octo" / "user_summary.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(payload), encoding="utf-8")

            os.environ["OCTO_SPORK_REPO_ROOT"] = str(root)
            ident = mod.load_user_summary_identity(root)
            self.assertEqual(ident.tone, "friendly")
            self.assertEqual(ident.technical_depth, "medium")
            self.assertIn("Rust", ident.stack_assumptions)
            sys_txt = ident.stack_instruction()
            self.assertIn("Rust", sys_txt)

    @patch("github_bot.style_prefs.style_guide_system_prompt_suffix", return_value="")
    @patch("observability.knowledge_base.domain_constraints_system_append", return_value="")
    @patch("github_bot.knowledge_sync.knowledge_sync_system_append", return_value="")
    def test_prompt_generator_prepends_identity(self, _ks: object, _kb: object, _sg: object) -> None:
        from github_bot.prompt_generator import FileSnapshot, PromptGenerator

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "USER_SUMMARY.json"
            path.write_text(
                json.dumps({"tone": "concise", "technical_depth": "high"}),
                encoding="utf-8",
            )
            os.environ["OCTO_SPORK_REPO_ROOT"] = str(root)

            gen = PromptGenerator(
                pr_title="t",
                pr_body="b",
                files=[FileSnapshot(path="p.py", status="added", before=None, after="x")],
                repo_root=root,
            )
            sys_p = gen.system_prompt()
            self.assertIn("Operator context", sys_p)
            self.assertIn("Octo-spork", sys_p)
            self.assertIn("findings", sys_p)


if __name__ == "__main__":
    unittest.main()
