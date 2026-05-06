import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "overlays"
    / "agenticseek"
    / "sources"
    / "review_ticket_export.py"
)
SPEC = importlib.util.spec_from_file_location("review_ticket_export", MODULE_PATH)
rte = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(rte)


class ReviewTicketExportTests(unittest.TestCase):
    def test_extract_high_section_and_export(self):
        md = """## Report

### High
- **Weak session handling** — cookies lack HttpOnly in `auth/session.py`.
- Plaintext logging of tokens in `services/log.py`.

### Medium
- Minor nit.

### Hardening plan
- Add integration tests for auth flows.
"""
        doc = rte.build_export_document(md, None, query="review please")
        self.assertGreaterEqual(doc["count"], 1)
        self.assertEqual(doc["tickets"][0]["severity"], "high")
        self.assertIn("session", doc["tickets"][0]["title"].lower())

    def test_roundtrip_json_file(self):
        md = "### High\n\n- **Bug** in `app/x.py`.\n"
        snap = {"owner": "o", "repo": "r", "scan_root": "/tmp/z", "sources": ["app/x.py"]}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            rte.export_review_tickets_json(md, snap, out)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["repository"]["owner"], "o")
            self.assertTrue(any("jira_cloud" in t.get("integrations", {}) for t in data["tickets"]))


if __name__ == "__main__":
    unittest.main()
