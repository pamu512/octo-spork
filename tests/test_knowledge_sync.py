"""Tests for ``github_bot.knowledge_sync`` (CLAUDE.md parsing and prompts)."""

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


_SAMPLE = """---
title: proj
---
<!-- dropped -->

# Project

Intro paragraph without bullets.

## Coding rules

- Always validate inputs.
- Prefer pure functions.

```python
# This bullet-looking line must NOT become a rule:
# - fake bullet in code
```

- Rules resume after the fence.

## Random notes

Just prose, no lists here.

> Blockquote rule one
> still continues

## Other

1. First numbered item
2. Second
"""


class KnowledgeSyncTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_KNOWLEDGE_SYNC_ENABLED",
            "OCTO_SPORK_REPO_ROOT",
            "OCTO_KNOWLEDGE_SYNC_MAX_CHARS",
        ):
            os.environ.pop(k, None)

    def test_strip_front_matter_and_comments(self) -> None:
        from github_bot import knowledge_sync as ks

        raw = "---\nx: 1\n---\n\nHello <!-- x --> world\n"
        s = ks.strip_html_comments(ks.strip_yaml_front_matter(raw))
        self.assertNotIn("---", s.split("\n")[0])
        self.assertIn("Hello", s)
        self.assertNotIn("<!--", s)

    def test_parse_claude_md_extracts_rules(self) -> None:
        from github_bot import knowledge_sync as ks

        p = ks.parse_claude_md(_SAMPLE)
        self.assertIn("Always validate inputs.", p.rules_flat)
        self.assertIn("Rules resume after the fence.", p.rules_flat)
        self.assertNotIn("# - fake bullet in code", " ".join(p.rules_flat))
        bq = [r for r in p.rules_flat if "Blockquote" in r or "continues" in r]
        self.assertTrue(bq, "blockquotes should merge into a rule")
        self.assertIn("First numbered item", p.rules_flat)
        # "Random notes" has no list — not in rule_sections via _section_is_rule_like
        self.assertTrue(any("Random notes" in s.title for s in p.sections))

    def test_load_from_repo_path(self) -> None:
        from github_bot import knowledge_sync as ks

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text(
                "## Style\n\n- Use Black formatting.\n",
                encoding="utf-8",
            )
            md = ks.load_claude_md_rules_markdown(root)
            self.assertIn("Black formatting", md)
            sys_append = ks.knowledge_sync_system_append(root)
            self.assertIn("KnowledgeSync", sys_append)
            self.assertIn("Black formatting", sys_append)

    def test_disabled_returns_empty(self) -> None:
        from github_bot import knowledge_sync as ks

        os.environ["OCTO_KNOWLEDGE_SYNC_ENABLED"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text("- x", encoding="utf-8")
            self.assertEqual(ks.load_claude_md_rules_markdown(root), "")

    def test_recurring_hints_and_proposal(self) -> None:
        from github_bot import knowledge_sync as ks

        scanner = """### Recurring architectural debt (global smell index)

Some text.

- **Hash `abc`** (trivy · `rule`) at `f.py`:1 — previously seen.
"""
        hints = ks.extract_recurring_smell_hints(scanner)
        self.assertEqual(len(hints), 1)
        self.assertIn("Hash", hints[0])

        prop = ks.maybe_knowledge_sync_proposal_for_scanners(Path("/nonexistent"), scanner)
        self.assertIn("CLAUDE.md", prop)
        self.assertIn("[ ]", prop)

    @patch("github_bot.style_prefs.style_guide_system_prompt_suffix", return_value="")
    @patch("observability.knowledge_base.domain_constraints_system_append", return_value="")
    def test_prompt_generator_appends_knowledge_sync(self, _d: object, _s: object) -> None:
        from github_bot.prompt_generator import FileSnapshot, PromptGenerator

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CLAUDE.md").write_text("## Rules\n\n- Custom rule from file.\n", encoding="utf-8")
            gen = PromptGenerator(
                pr_title="t",
                pr_body="b",
                files=[FileSnapshot(path="p.py", status="added", before=None, after="x")],
                repo_root=root,
            )
            sys_p = gen.system_prompt()
            self.assertIn("Custom rule from file.", sys_p)
            self.assertIn("KnowledgeSync", sys_p)


if __name__ == "__main__":
    unittest.main()
