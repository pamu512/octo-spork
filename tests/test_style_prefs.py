"""Tests for learned style preferences (YAML + webhook gating)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class StylePrefsTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "ALLOWED_USERS",
            "OCTO_STYLE_LEARN_ENABLED",
            "OCTO_STYLE_GUIDE_ENABLED",
            "OCTO_SPORK_REPO_ROOT",
        ):
            os.environ.pop(k, None)

    def test_looks_like_ai_comment(self) -> None:
        from github_bot import style_prefs as sp

        self.assertTrue(
            sp.looks_like_ai_generated_comment("foo\n\nPosted by octo-spork webhook\n")
        )
        self.assertFalse(sp.looks_like_ai_generated_comment("lgtm thanks"))

    def test_should_learn_from_edited_pr_comment(self) -> None:
        from github_bot.style_prefs import should_learn_style_from_issue_comment

        os.environ["OCTO_STYLE_LEARN_ENABLED"] = "true"
        headers = {"X-GitHub-Event": "issue_comment"}
        payload = {
            "action": "edited",
            "issue": {"number": 1, "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/1"}},
            "changes": {"body": {"from": "old"}},
            "comment": {"body": "new"},
        }
        self.assertTrue(should_learn_style_from_issue_comment(headers, payload))

    def test_apply_learned_correction_mocked_ollama(self) -> None:
        from github_bot import style_prefs as sp

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["ALLOWED_USERS"] = "alice"
            os.environ["OCTO_SPORK_REPO_ROOT"] = tmp
            with patch.object(sp, "_ollama_merge_guide", return_value="- prefer X over Y\n"):
                ok = sp.apply_learned_correction(
                    before="Posted by octo-spork webhook\n\nold text",
                    after="Posted by octo-spork webhook\n\nnew text",
                    repo_full="o/r",
                    editor_login="alice",
                    comment_id=99,
                )
            self.assertTrue(ok)
            path = Path(tmp) / ".local" / "style_prefs.yaml"
            self.assertTrue(path.is_file())
            raw = path.read_text(encoding="utf-8")
            self.assertIn("prefer X", raw)

    def test_process_payload_requires_allowed_sender(self) -> None:
        from github_bot import style_prefs as sp

        os.environ["ALLOWED_USERS"] = "alice"
        payload = {
            "sender": {"login": "bob"},
            "changes": {"body": {"from": "Posted by octo-spork webhook\na"}},
            "comment": {"body": "b"},
            "repository": {"full_name": "o/r"},
        }
        with patch.object(sp, "apply_learned_correction") as m:
            sp.process_issue_comment_edited_payload(payload)
            m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
