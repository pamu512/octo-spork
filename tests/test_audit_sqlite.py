"""Tests for SQLite agent audit log."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class AuditSqliteTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in ("OCTO_AUDIT_DB", "OCTO_AUDIT_SESSION_ID", "OCTO_AUDIT_SQLITE"):
            os.environ.pop(k, None)
        try:
            from observability.audit_sqlite import set_audit_session

            set_audit_session(None)
        except ImportError:
            pass

    def test_session_tree_and_export(self) -> None:
        from observability import audit_sqlite as a

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "a.db"
            os.environ["OCTO_AUDIT_DB"] = str(db)
            sid = a.start_session(meta={"run": "test"})
            a.record_thought(sid, {"thought": "Plan A: use foo"})
            a.record_thought(sid, {"thought": "Actually, plan B is safer"})
            a.record_action(sid, {"tool": "git", "git.subcommand": "status"})
            a.update_decision_tree(
                sid,
                {"root": {"plan": "B", "steps": [{"id": 1, "name": "verify"}]}},
            )
            md = a.export_summary(sid)
            self.assertIn(sid, md)
            self.assertIn("Decision tree", md)
            self.assertIn("possible change of mind", md.lower())
            self.assertIn("Actually", md)

    def test_mirror_trace_requires_flag(self) -> None:
        from observability import audit_sqlite as a

        os.environ.pop("OCTO_AUDIT_SQLITE", None)
        with tempfile.TemporaryDirectory() as td:
            os.environ["OCTO_AUDIT_DB"] = str(Path(td) / "x.db")
            sid = a.start_session()
            a.mirror_from_trace_record({"kind": "llm", "thought": "x"})
            # mirror skipped without OCTO_AUDIT_SQLITE
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".db") as fh:
                path = fh.name
            try:
                os.environ["OCTO_AUDIT_DB"] = path
                os.environ["OCTO_AUDIT_SQLITE"] = "1"
                a.set_audit_session(sid)
                a.mirror_from_trace_record({"kind": "llm", "thought": "mirrored"})
                md = a.export_summary(sid)
                self.assertIn("mirrored", md)
            finally:
                Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
