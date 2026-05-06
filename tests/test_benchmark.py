"""Tests for benchmark CSV helpers."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from local_ai_stack.benchmark import CSV_COLUMNS, append_performance_csv


class BenchmarkCsvTests(unittest.TestCase):
    def test_append_creates_header_and_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "performance.csv"
            append_performance_csv(
                p,
                {
                    "timestamp_utc": "2026-01-01T00:00:00Z",
                    "git_url": "https://github.com/octocat/Spoon-Knife.git",
                    "base_ref": "abc",
                    "head_ref": "def",
                    "model": "m",
                    "clone_seconds": "1.0",
                    "snapshot_seconds": "0.5",
                    "scan_seconds": "2.0",
                    "llm_seconds": "3.0",
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                    "success": "1",
                    "notes": "",
                },
            )
            self.assertTrue(p.is_file())
            lines = p.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreaterEqual(len(lines), 2)
            header = lines[0].split(",")
            self.assertEqual(header[0], CSV_COLUMNS[0])

            with p.open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["model"], "m")
            self.assertEqual(rows[0]["total_tokens"], "30")


if __name__ == "__main__":
    unittest.main()
