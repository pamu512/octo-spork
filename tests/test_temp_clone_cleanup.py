"""Tests for ``.temp_clones`` hourly pruning."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from github_bot.temp_clone_cleanup import cleanup_stale_temp_clones  # noqa: E402


class TempCloneCleanupTests(unittest.TestCase):
    def test_removes_only_directories_older_than_max_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old = base / "clone-old"
            old.mkdir()
            ancient = time.time() - 4 * 3600
            os.utime(old, (ancient, ancient))

            young = base / "clone-young"
            young.mkdir()
            fresh = time.time() - 300
            os.utime(young, (fresh, fresh))

            file_only = base / "not-a-dir.txt"
            file_only.write_text("x", encoding="utf-8")

            summary = cleanup_stale_temp_clones(base, max_age_seconds=2 * 3600)
            self.assertEqual(summary["removed"], 1)
            self.assertEqual(summary["skipped_young"], 1)
            self.assertFalse(old.exists())
            self.assertTrue(young.exists())
            self.assertTrue(file_only.is_file())

    def test_missing_base_returns_note(self) -> None:
        summary = cleanup_stale_temp_clones(Path("/nonexistent/octo-spork-temp-clones-xyz"))
        self.assertEqual(summary.get("removed"), 0)
        self.assertEqual(summary.get("note"), "base_missing_or_not_dir")

    def test_permission_denied_counts_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stale = base / "locked"
            stale.mkdir()
            ancient = time.time() - 4 * 3600
            os.utime(stale, (ancient, ancient))
            with patch("github_bot.temp_clone_cleanup.shutil.rmtree", side_effect=PermissionError("denied")):
                summary = cleanup_stale_temp_clones(base, max_age_seconds=2 * 3600)
            self.assertEqual(summary["errors"], 1)
            self.assertEqual(summary["removed"], 0)


if __name__ == "__main__":
    unittest.main()
