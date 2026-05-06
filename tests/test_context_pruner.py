"""Tests for AST / tree-sitter context pruning."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class ContextPrunerTests(unittest.TestCase):
    def test_python_keeps_imports_and_helper_and_target(self) -> None:
        from context_pruner import prune_file_for_llm

        src = '''\
import json
import os
from pathlib import Path

SECRET = "x"

def helper(x):
    return x + 1

def other():
    return 3

def vulnerable(line):
    data = json.loads(line)
    return helper(Path(".").name)

class Unused:
    pass
'''
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "mod.py"
            p.write_text(src, encoding="utf-8")
            # Line inside ``vulnerable``
            r = prune_file_for_llm(p, 14)
        self.assertEqual(r.engine, "python-ast")
        self.assertIn("def vulnerable", r.text)
        self.assertIn("def helper", r.text)
        self.assertIn("import json", r.text)
        self.assertIn("from pathlib import Path", r.text)
        self.assertNotIn("class Unused", r.text)
        self.assertNotIn("def other", r.text)
        self.assertIn("# ... code omitted for context", r.text)

    def test_javascript_import_and_dep(self) -> None:
        from context_pruner import prune_file_for_llm

        src = """\
import { parse } from \"./parse.js\";

function util(n) { return n * 2; }

function noise() { return 1; }

export function handle(req) {
  return util(parse(req.body));
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "api.js"
            p.write_text(src, encoding="utf-8")
            r = prune_file_for_llm(p, 8)
        self.assertEqual(r.engine, "tree-sitter")
        self.assertIn("export function handle", r.text)
        self.assertIn("function util", r.text)
        self.assertIn('from "./parse.js"', r.text)
        self.assertNotIn("function noise", r.text)
        self.assertIn("// ... code omitted for context", r.text)

    def test_fallback_window_unknown_extension(self) -> None:
        from context_pruner import prune_file_for_llm

        lines = [f"LINE_{i}" for i in range(100)]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "docker-compose.yml"
            p.write_text("\n".join(lines), encoding="utf-8")
            r = prune_file_for_llm(p, 50)
        self.assertEqual(r.engine, "fallback")
        self.assertIn("LINE_50", r.text)
        self.assertIn("// ... code omitted for context", r.text)


if __name__ == "__main__":
    unittest.main()
