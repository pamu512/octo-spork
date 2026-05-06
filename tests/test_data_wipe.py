"""Tests for secure ``data_wipe`` helpers."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from local_ai_stack.data_wipe import wipe_directory_tree, wipe_file

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DC = ROOT / "tests" / "fixtures" / "agenticseek" / "docker-compose.yml"
PATCH_REDIS = ROOT / "patches" / "agenticseek" / "patches" / "010-docker-compose-redis.patch"


class DataWipeTests(unittest.TestCase):
    def test_wipe_file_overwrites_then_removes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "secret.txt"
            p.write_text("hello-secret-world", encoding="utf-8")
            wipe_file(p, passes=1)
            self.assertFalse(p.exists())

    def test_wipe_directory_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "nest"
            (root / "a").mkdir(parents=True)
            (root / "a" / "f.txt").write_text("data", encoding="utf-8")
            (root / "b.txt").write_text("top", encoding="utf-8")
            wipe_directory_tree(root, passes=1)
            self.assertFalse(root.exists())


class ComposeRedisPatchTests(unittest.TestCase):
    def test_shipped_git_patch_applies_to_fixture_upstream_compose(self) -> None:
        self.assertTrue(FIXTURE_DC.is_file(), "frozen upstream fixture missing")
        self.assertTrue(PATCH_REDIS.is_file(), "redis bind-mount patch missing")
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            shutil.copy(FIXTURE_DC, td_path / "docker-compose.yml")
            r = subprocess.run(
                ["git", "apply", "-p1", str(PATCH_REDIS)],
                cwd=td_path,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(r.returncode, 0, msg=(r.stderr or "") + (r.stdout or ""))
            text = (td_path / "docker-compose.yml").read_text(encoding="utf-8")
            self.assertIn("../../.local/data/redis:/data", text)
            self.assertNotIn("redis-data:", text)
            self.assertIn("chrome_profiles:", text)


if __name__ == "__main__":
    unittest.main()
