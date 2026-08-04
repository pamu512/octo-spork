"""Tests for audit status payload + API key gate on trigger route."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TestAuditStatusPayload(unittest.TestCase):
    def test_payload_with_log_and_metrics(self) -> None:
        from observability import audit_status as mod
        from observability.latency import record_metric

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            (logs / "nightly_audit_20260101T000000Z.log").write_text("doctor ok\n", encoding="utf-8")
            with mock.patch.object(mod, "_repo_root", return_value=root):
                with mock.patch("observability.latency._repo_root", return_value=root):
                    record_metric(pr_name="pr", start_time=1.0, end_time=2.0, success=True)
                payload = mod.audit_status_payload()
            self.assertTrue(payload["nightly_audit"]["path"])
            self.assertIn("doctor ok", payload["nightly_audit"]["tail"])
            self.assertGreaterEqual(payload["metrics"]["count"], 1)


class TestAuditRoutesAuth(unittest.TestCase):
    def test_run_requires_key(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from github_bot.audit_routes import router

        app = FastAPI()
        app.include_router(router)

        os.environ.pop("OCTO_AUDIT_API_KEY", None)
        client = TestClient(app)
        r = client.post("/octo/audit/run")
        self.assertEqual(r.status_code, 503)

        os.environ["OCTO_AUDIT_API_KEY"] = "secret-test-key"
        try:
            r = client.post("/octo/audit/run")
            self.assertEqual(r.status_code, 401)
            with mock.patch("github_bot.audit_routes.threading.Thread") as thr:
                thr.return_value.start = mock.Mock()
                r = client.post("/octo/audit/run", headers={"X-API-Key": "secret-test-key"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json().get("accepted"))
        finally:
            os.environ.pop("OCTO_AUDIT_API_KEY", None)
            # Reset in-process run flag left by a successful accept.
            from github_bot import audit_routes as ar

            with ar._run_lock:
                ar._run_state["running"] = False


if __name__ == "__main__":
    unittest.main()
