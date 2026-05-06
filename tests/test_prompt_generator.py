"""Tests for ``github_bot.prompt_generator``."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class ValidateFindingsJsonTests(unittest.TestCase):
    def test_accepts_valid_payload(self) -> None:
        from github_bot.prompt_generator import validate_findings_json

        payload = {
            "findings": [
                {
                    "file": "a.py",
                    "line_start": 1,
                    "line_end": 3,
                    "issue_type": "security",
                    "severity": "high",
                    "evidence_quote": "eval(user_input)",
                }
            ]
        }
        ok, err = validate_findings_json(json.dumps(payload))
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_rejects_missing_key(self) -> None:
        from github_bot.prompt_generator import validate_findings_json

        bad = {"findings": [{"file": "x", "line_start": 1, "line_end": 1}]}
        ok, err = validate_findings_json(json.dumps(bad))
        self.assertFalse(ok)
        self.assertIn("missing keys", err)


class PromptGeneratorTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_VECTOR_MEMORY",
            "OCTO_CORRECTION_LEDGER",
            "OLLAMA_BASE_URL",
            "OLLAMA_LOCAL_URL",
        ):
            os.environ.pop(k, None)

    def test_user_includes_description_and_before_after(self) -> None:
        from github_bot.prompt_generator import FileSnapshot, PromptGenerator

        gen = PromptGenerator(
            pr_title="Fix bug",
            pr_body="## Summary\n\nImportant.",
            files=[
                FileSnapshot(
                    path="foo.py",
                    status="modified",
                    before="old",
                    after="new",
                )
            ],
            unified_diff="diff --git",
            owner="o",
            repo="r",
            number=9,
        )
        user = gen.user_prompt()
        self.assertIn("Fix bug", user)
        self.assertIn("## Summary", user)
        self.assertIn("**Before (base)**", user)
        self.assertIn("old", user)
        self.assertIn("**After (head)**", user)
        self.assertIn("new", user)
        self.assertIn("```diff", user)

    @patch("github_bot.style_prefs.style_guide_system_prompt_suffix", return_value="")
    @patch("github_bot.prompt_generator.vector_memory_enabled", return_value=True)
    @patch("github_bot.prompt_generator.VectorMemory")
    def test_historical_context_queries_vector_memory_top_three(
        self,
        mock_vm_cls: MagicMock,
        _mem_on: MagicMock,
        _style: MagicMock,
    ) -> None:
        os.environ["OCTO_VECTOR_MEMORY"] = "1"
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        fake_store = MagicMock()
        fake_store.similar_findings.return_value = [
            {
                "repo_full": "acme/api",
                "excerpt": "SQL injection risk in auth handler.",
                "revision_sha": "abc",
                "owner": "acme",
                "repo": "api",
                "id": "a",
                "kind": "findings",
            },
            {
                "repo_full": "other/lib",
                "excerpt": "Missing CSRF token.",
                "revision_sha": "def",
                "owner": "other",
                "repo": "lib",
                "id": "b",
                "kind": "findings",
            },
            {
                "repo_full": "acme/api",
                "excerpt": "Weak crypto defaults.",
                "revision_sha": "ghi",
                "owner": "acme",
                "repo": "api",
                "id": "c",
                "kind": "fixes",
            },
        ]
        mock_vm_cls.return_value = fake_store

        from github_bot.prompt_generator import FileSnapshot, PromptGenerator

        gen = PromptGenerator(
            pr_title="Security hardening",
            pr_body="Audit dependencies.",
            files=[FileSnapshot(path="p.py", status="added", before=None, after="x")],
            owner="myorg",
            repo="svc",
            number=42,
        )
        user = gen.user_prompt()
        self.assertIn("## Historical Context", user)
        self.assertIn(
            "In the past, you found similar issues in **acme/api**. "
            "Ensure this review checks for those patterns again.",
            user,
        )
        self.assertIn("SQL injection risk", user)
        fake_store.similar_findings.assert_called_once()
        _args, kwargs = fake_store.similar_findings.call_args
        self.assertEqual(kwargs.get("k"), 3)
        self.assertIn("myorg/svc", _args[0])

    def test_no_historical_context_when_vector_memory_off(self) -> None:
        """No block when OCTO_VECTOR_MEMORY is unset (default off)."""
        from github_bot.prompt_generator import FileSnapshot, PromptGenerator

        gen = PromptGenerator(
            pr_title="t",
            pr_body="b",
            files=[FileSnapshot(path="p.py", status="added", before=None, after="x")],
            owner="o",
            repo="r",
        )
        user = gen.user_prompt()
        self.assertNotIn("## Historical Context", user)

    @patch("github_bot.style_prefs.style_guide_system_prompt_suffix", return_value="")
    @patch("github_bot.prompt_generator.lessons_learned_markdown")
    @patch("github_bot.prompt_generator.correction_ledger_enabled", return_value=True)
    def test_lessons_learned_injected_when_enabled(
        self,
        _cle: MagicMock,
        mock_lessons: MagicMock,
        _style: MagicMock,
    ) -> None:
        os.environ["OCTO_CORRECTION_LEDGER"] = "1"
        os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
        mock_lessons.return_value = "## Lessons Learned\n\n- Avoid **x**, as the developer previously corrected it to **y**.\n"

        from github_bot.prompt_generator import FileSnapshot, PromptGenerator

        gen = PromptGenerator(
            pr_title="t",
            pr_body="b",
            files=[FileSnapshot(path="p.py", status="added", before=None, after="x")],
        )
        user = gen.user_prompt()
        self.assertIn("## Lessons Learned", user)
        self.assertIn("Avoid **x**", user)
        mock_lessons.assert_called_once()

    @patch("github_bot.style_prefs.style_guide_system_prompt_suffix", return_value="")
    def test_system_prompt_lists_schema_fields(self, _m: MagicMock) -> None:
        from github_bot.prompt_generator import FileSnapshot, PromptGenerator

        gen = PromptGenerator(
            pr_title="t",
            pr_body="b",
            files=[FileSnapshot(path="p.py", status="added", before=None, after="x")],
        )
        sys_p = gen.system_prompt()
        for key in ("file", "line_start", "line_end", "issue_type", "severity", "evidence_quote"):
            self.assertIn(key, sys_p)
        self.assertIn("JSON", sys_p)

    @patch("github_bot.style_prefs.style_guide_system_prompt_suffix", return_value="")
    def test_messages_structure(self, _m: MagicMock) -> None:
        from github_bot.prompt_generator import FileSnapshot, PromptGenerator

        gen = PromptGenerator(
            pr_title="t",
            pr_body="b",
            files=[FileSnapshot(path="p.py", status="added", before=None, after="x")],
        )
        msgs = gen.messages()
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")

    @patch("github_bot.prompt_generator.fetch_file_at_ref", return_value=("BASECONTENT", False, None))
    def test_from_grounded_fetches_before(self, _mock: MagicMock) -> None:
        from github_bot.git_manager import GroundedFile, GroundedPullRequest
        from github_bot.prompt_generator import PromptGenerator

        ctx = GroundedPullRequest(
            owner="o",
            repo="r",
            number=1,
            head_sha="H",
            base_sha="B",
            unified_diff="",
            files=[
                GroundedFile(
                    path="f.py",
                    status="modified",
                    patch_hunk="@@",
                    full_text="HEADCONTENT",
                )
            ],
        )
        gen = PromptGenerator.from_grounded_pull_request(
            ctx,
            pr_title="t",
            pr_body="body",
            token="tok",
            include_unified_diff=False,
        )
        self.assertEqual(gen.files[0].before, "BASECONTENT")
        self.assertEqual(gen.files[0].after, "HEADCONTENT")


class ParseDescriptionTests(unittest.TestCase):
    def test_parse(self) -> None:
        from github_bot.prompt_generator import parse_pull_request_description

        title, body = parse_pull_request_description(
            {"pull_request": {"title": "Hello", "body": "World"}},
        )
        self.assertEqual(title, "Hello")
        self.assertEqual(body, "World")


if __name__ == "__main__":
    unittest.main()
