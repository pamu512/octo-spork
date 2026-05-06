"""Unit tests for scripts/verify_logic.py helpers (no Ollama required)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
# Import script as module (filename has underscore)
import importlib.util

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "verify_logic",
    _REPO_ROOT / "scripts" / "verify_logic.py",
)
assert _SPEC and _SPEC.loader
_vl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_vl)


def _git_available() -> bool:
    return shutil.which("git") is not None


@unittest.skipUnless(_git_available(), "git not on PATH")
class VerifyLogicDummyRepoTests(unittest.TestCase):
    def test_materialize_commits_and_vuln_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "d"
            base, head = _vl.materialize_dummy_repo(root)
            self.assertEqual(base, "HEAD~1")
            self.assertEqual(head, "HEAD")
            sec = (root / "app" / "secrets.py").read_text(encoding="utf-8")
            self.assertIn("API_KEY", sec)
            self.assertIn("sk-integration-hardcoded-key", sec)
            proc = subprocess.run(
                ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.stdout.strip(), "2")


class VerifyLogicHeuristicTests(unittest.TestCase):
    def test_count_identified_themes(self) -> None:
        text = (
            "Hardcoded api_key in secrets.py and SQL concatenation in sql.py; "
            "avoid shell=True."
        )
        n = _vl.count_identified_themes(text.lower())
        self.assertGreaterEqual(n, 2)

    def test_citation_covers_vuln(self) -> None:
        from claude_bridge.receipt_auditor import Citation

        cites = [
            Citation(raw="app/secrets.py", line=None, span_start=0),
            Citation(raw="app/secrets.py", line=2, span_start=10),
        ]
        self.assertTrue(_vl.citation_covers_vuln(cites, "app/secrets.py", 2))

    def test_assert_receipts_dummy_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dummy = Path(td)
            (dummy / "app").mkdir(parents=True)
            _vl._write_dummy_sources(dummy / "app")
            answer = (
                "Issue in app/secrets.py:2 — hardcoded credential.\n"
                "Also app/sql.py:2 uses concatenated SQL.\n"
                "Finally app/shell.py:5 sets shell=True.\n"
            )
            errs = _vl.assert_receipts_for_dummy(answer, dummy)
            self.assertEqual(errs, [], errs)


if __name__ == "__main__":
    unittest.main()
