"""Tests for :mod:`infra.resource_reclaimer`."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class ResourceReclaimerTests(unittest.TestCase):
    def test_disabled_short_circuits(self) -> None:
        from infra.resource_reclaimer import ResourceReclaimer

        with patch.dict(os.environ, {"OCTO_RESOURCE_RECLAIMER_ENABLED": "0"}, clear=False):
            with ResourceReclaimer.pause_addon_services_for_inference():
                pass
        # no subprocess if disabled

    def test_stop_start_sequence_when_enabled(self) -> None:
        from infra import resource_reclaimer as rr

        calls: list[tuple[str, tuple[str, ...]]] = []

        def fake_action(root: Path, action: str, services: tuple[str, ...]) -> tuple[int, str]:
            calls.append((action, services))
            return 0, ""

        with patch.object(rr, "reclaim_enabled", return_value=True):
            with patch.object(rr, "_compose_service_action", side_effect=fake_action):
                with rr.ResourceReclaimer.pause_addon_services_for_inference():
                    pass
        self.assertEqual([x[0] for x in calls], ["stop", "start"])
        self.assertEqual(calls[0][1], rr.ResourceReclaimer.ADDON_SERVICES)


if __name__ == "__main__":
    unittest.main()
