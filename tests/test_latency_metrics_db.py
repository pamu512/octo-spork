"""SQLite metrics.db helpers used by the fix-it latency path."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TestLatencyMetricsDb(unittest.TestCase):
    def test_record_metric_roundtrip(self) -> None:
        from observability import latency as lat

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(lat, "_repo_root", return_value=root):
                lat.record_metric(
                    pr_name="https://example.com/pr/1",
                    start_time=100.0,
                    end_time=130.5,
                    success=True,
                )
                db = root / "logs" / "metrics.db"
                self.assertTrue(db.is_file())
                conn = sqlite3.connect(str(db))
                try:
                    row = conn.execute(
                        "SELECT pr_name, ttr_seconds, success FROM metrics"
                    ).fetchone()
                finally:
                    conn.close()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "https://example.com/pr/1")
                self.assertAlmostEqual(float(row[1]), 30.5, places=3)
                self.assertEqual(int(row[2]), 1)


if __name__ == "__main__":
    unittest.main()
