"""Tests for ``infra.smart_swapper``."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class SmartSwapperTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_SMART_SWAPPER",
            "OCTO_SMART_SWAP_MIN_PARAMS_B",
            "OCTO_SMART_SWAP_COMPLEX_REGEX",
        ):
            os.environ.pop(k, None)

    def test_disabled_by_default(self) -> None:
        from infra.smart_swapper import smart_swapper_enabled

        os.environ.pop("OCTO_SMART_SWAPPER", None)
        self.assertFalse(smart_swapper_enabled())
        os.environ["OCTO_SMART_SWAPPER"] = "1"
        self.assertTrue(smart_swapper_enabled())

    def test_complex_reasoning_keyword(self) -> None:
        from infra.smart_swapper import task_requires_complex_reasoning

        self.assertTrue(task_requires_complex_reasoning("Please do an architectural refactoring plan"))
        self.assertFalse(task_requires_complex_reasoning("fix typo"))

    @patch("infra.smart_swapper.estimate_parameter_count_billions", return_value=32.0)
    def test_should_swap_large_model(self, _est: MagicMock) -> None:
        from infra.smart_swapper import should_consider_smart_swap

        os.environ["OCTO_SMART_SWAPPER"] = "1"
        self.assertTrue(
            should_consider_smart_swap("any", "http://127.0.0.1:11434", query=None, task_kind=None)
        )


if __name__ == "__main__":
    unittest.main()
