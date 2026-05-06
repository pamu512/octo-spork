"""Tests for ``claude_bridge.safe_path``."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claude_bridge.safe_path import (  # noqa: E402
    SafePathMiddleware,
    SafePathViolation,
    is_edit_path_allowed,
    resolve_under_repo,
)


class SafePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.repo = Path(self._td.name)
        (self.repo / "src" / "app").mkdir(parents=True)
        (self.repo / "src" / "github_bot" / "x.py").parent.mkdir(parents=True)
        (self.repo / "src" / "github_bot" / "x.py").write_text("# bot\n", encoding="utf-8")
        (self.repo / "README.md").write_text("hi", encoding="utf-8")
        (self.repo / ".env").write_text("SECRET=x\n", encoding="utf-8")
        (self.repo / "docker-compose.yml").write_text("services:\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_allows_readme(self) -> None:
        ok, msg = is_edit_path_allowed(self.repo, "README.md")
        self.assertTrue(ok, msg)

    def test_blocks_dot_env(self) -> None:
        ok, msg = is_edit_path_allowed(self.repo, ".env")
        self.assertFalse(ok)
        self.assertIn(".env", msg)

    def test_blocks_nested_dot_env(self) -> None:
        d = self.repo / "pkg"
        d.mkdir()
        (d / ".env").write_text("x\n", encoding="utf-8")
        ok, _ = is_edit_path_allowed(self.repo, "pkg/.env")
        self.assertFalse(ok)

    def test_blocks_compose_file(self) -> None:
        ok, _ = is_edit_path_allowed(self.repo, "docker-compose.yml")
        self.assertFalse(ok)

    def test_blocks_github_bot_tree(self) -> None:
        ok, _ = is_edit_path_allowed(self.repo, "src/github_bot/x.py")
        self.assertFalse(ok)

    def test_blocks_escape_parent(self) -> None:
        ok, _ = is_edit_path_allowed(self.repo, "../outside.txt")
        self.assertFalse(ok)

    def test_middleware_raises(self) -> None:
        mw = SafePathMiddleware(self.repo)
        mw.assert_allowed("README.md")
        with self.assertRaises(SafePathViolation):
            mw.assert_allowed(".env")

    def test_resolve_under_repo(self) -> None:
        p = resolve_under_repo(self.repo, "README.md")
        self.assertTrue(p.is_absolute())
        self.assertTrue(str(p).endswith("README.md"))


if __name__ == "__main__":
    unittest.main()
