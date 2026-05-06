"""Tests for ``observability.knowledge_base`` (grounding/rules Markdown injection)."""

from __future__ import annotations

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


class KnowledgeBaseTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_DOMAIN_CONSTRAINTS_ENABLED",
            "OCTO_SPORK_REPO_ROOT",
            "OCTO_GROUNDING_RULES_DIR",
            "OCTO_GROUNDING_RULES_MAX_CHARS",
        ):
            os.environ.pop(k, None)

    def test_loads_sorted_markdown_files(self) -> None:
        from observability import knowledge_base as kb

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = root / "grounding" / "rules"
            rules.mkdir(parents=True)
            (rules / "zebra.md").write_text("# Z\nlast", encoding="utf-8")
            (rules / "alpha.md").write_text("# A\nfirst", encoding="utf-8")

            text = kb.load_domain_constraints_markdown(repo_root=root)
            self.assertIn("### Rules file: `alpha.md`", text)
            self.assertIn("### Rules file: `zebra.md`", text)
            pos_a = text.index("alpha.md")
            pos_z = text.index("zebra.md")
            self.assertLess(pos_a, pos_z)

    def test_disabled_returns_empty(self) -> None:
        from observability import knowledge_base as kb

        os.environ["OCTO_DOMAIN_CONSTRAINTS_ENABLED"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "grounding" / "rules").mkdir(parents=True)
            (root / "grounding" / "rules" / "x.md").write_text("secret", encoding="utf-8")
            self.assertEqual(kb.load_domain_constraints_markdown(repo_root=root), "")

    def test_prompt_generator_gets_appendix(self) -> None:
        from github_bot.prompt_generator import FileSnapshot, PromptGenerator

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules = root / "grounding" / "rules"
            rules.mkdir(parents=True)
            (rules / "cti_indicators.md").write_text("- indicator foo", encoding="utf-8")

            os.environ["OCTO_SPORK_REPO_ROOT"] = str(root)
            with patch("github_bot.style_prefs.style_guide_system_prompt_suffix", return_value=""):
                gen = PromptGenerator(
                    pr_title="t",
                    pr_body="b",
                    files=[FileSnapshot(path="p.py", status="added", before=None, after="x")],
                )
                sys_p = gen.system_prompt()
            self.assertIn("Domain Constraints", sys_p)
            self.assertIn("indicator foo", sys_p)
