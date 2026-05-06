"""Tests for octo_spork_mcp Turntable + CTI helpers (no stdio MCP session)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class OctoMcpLogicTests(unittest.TestCase):
    def test_normalize_sha256(self) -> None:
        from octo_spork_mcp.octo_tools import normalize_sha256

        self.assertIsNone(normalize_sha256("not-a-hash"))
        self.assertEqual(
            normalize_sha256("SHA256:" + "a" * 64),
            "a" * 64,
        )

    def test_cti_lookup_miss(self) -> None:
        from octo_spork_mcp.octo_tools import cti_lookup_markdown

        out = cti_lookup_markdown("b" * 64)
        self.assertIn("no entry", out.lower())

    def test_cti_lookup_hit_sample(self) -> None:
        from octo_spork_mcp.octo_tools import cti_lookup_markdown

        demo = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        out = cti_lookup_markdown(demo)
        self.assertIn("suspicious_demo", out)

    def test_overlay_merge(self) -> None:
        from octo_spork_mcp import octo_tools as ot

        with tempfile.TemporaryDirectory() as td:
            overlay = Path(td) / "db.json"
            overlay.write_text(
                json.dumps(
                    {
                        "entries": {
                            "f" * 64: {
                                "verdict": "custom",
                                "family": "overlay-test",
                                "confidence": 1.0,
                                "refs": [],
                                "notes": "from temp file",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            old = os.environ.pop("OCTO_CTI_PILOT_DB", None)
            try:
                os.environ["OCTO_CTI_PILOT_DB"] = str(overlay)
                out = ot.cti_lookup_markdown("f" * 64)
                self.assertIn("custom", out)
            finally:
                if old is None:
                    os.environ.pop("OCTO_CTI_PILOT_DB", None)
                else:
                    os.environ["OCTO_CTI_PILOT_DB"] = old


if __name__ == "__main__":
    unittest.main()
