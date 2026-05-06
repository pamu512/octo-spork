"""Tests for :mod:`remediation.validator`."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "patch-validator@test.local"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Patch Validator Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


class PatchValidatorTests(unittest.TestCase):
    def test_validate_success_on_good_diff(self) -> None:
        from remediation.validator import PatchValidator

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            _init_repo(repo)
            (repo / "foo.txt").write_text("line-a\n", encoding="utf-8")
            subprocess.run(["git", "add", "foo.txt"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

            (repo / "foo.txt").write_text("line-b\n", encoding="utf-8")
            diff_text = subprocess.check_output(["git", "diff"], cwd=str(repo), text=True)
            (repo / "foo.txt").write_text("line-a\n", encoding="utf-8")

            verify_root = base / "octo_verify"
            verify_root.mkdir(parents=True, exist_ok=True)
            validator = PatchValidator(repo, verify_root=verify_root)
            result = validator.validate(diff_text)

            self.assertTrue(result.success)
            self.assertEqual(result.stderr, "")

    def test_validate_failure_returns_stderr(self) -> None:
        from remediation.validator import PatchValidator

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            _init_repo(repo)
            (repo / "foo.txt").write_text("content\n", encoding="utf-8")
            subprocess.run(["git", "add", "foo.txt"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

            bogus_diff = """diff --git a/missing.txt b/missing.txt
--- a/missing.txt
+++ b/missing.txt
@@ -1 +1 @@
-old
+new
"""

            verify_root = base / "octo_verify"
            verify_root.mkdir(parents=True, exist_ok=True)
            validator = PatchValidator(repo, verify_root=verify_root)
            result = validator.validate(bogus_diff)

            self.assertFalse(result.success)
            self.assertTrue(len(result.stderr) > 0)

    def test_nonexistent_repo(self) -> None:
        from remediation.validator import PatchValidator

        validator = PatchValidator("/nonexistent/repo/path/xyz")
        result = validator.validate("")
        self.assertFalse(result.success)
        self.assertIn("does not exist", result.stderr)


if __name__ == "__main__":
    unittest.main()
