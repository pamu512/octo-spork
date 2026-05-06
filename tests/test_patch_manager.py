"""Tests for :mod:`local_ai_stack.patch_manager`."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_ai_stack.patch_manager import PatchConflictError, PatchManager

ROOT = Path(__file__).resolve().parents[1]
PATCH_REDIS = ROOT / "patches" / "agenticseek" / "patches" / "010-docker-compose-redis.patch"


class PatchManagerTests(unittest.TestCase):
    def test_git_apply_raises_on_conflict(self) -> None:
        pm = PatchManager(ROOT / "patches" / "agenticseek", ROOT)
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "docker-compose.yml").write_text("services: x\n", encoding="utf-8")
            with self.assertRaises(PatchConflictError):
                pm._git_apply(td_path, PATCH_REDIS)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
