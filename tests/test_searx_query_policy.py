"""Tests for overlay `searx_query_policy` (loaded from repo path)."""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_POLICY = ROOT / "overlays" / "agenticseek" / "sources" / "tools" / "searx_query_policy.py"


def _load_policy():
    spec = importlib.util.spec_from_file_location("_octo_searx_policy", _POLICY)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load searx_query_policy")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SearxQueryPolicyTests(unittest.TestCase):
    def test_sanitize_strips_email_github_sha(self) -> None:
        pol = _load_policy()
        raw = (
            "Contact admin@company.test about https://github.com/acme/secret-service "
            "commit deadbeefdeadbeefdeadbeefdeadbeefbad"
        )
        out = pol.sanitize_searx_query(raw)
        self.assertNotIn("admin@", out)
        self.assertNotIn("github.com", out)
        self.assertNotIn("deadbeef", out)
        self.assertIn("git repository", out)

    def test_active_repo_env_removes_slugs(self) -> None:
        pol = _load_policy()
        prev = os.environ.get("OCTO_SPORK_ACTIVE_REPO")
        try:
            os.environ["OCTO_SPORK_ACTIVE_REPO"] = "acme-corp/payroll-processor"
            q = "Does acme-corp payroll-processor use JWT?"
            out = pol.sanitize_searx_query(q)
            self.assertNotIn("acme-corp", out.lower())
            self.assertNotIn("payroll-processor", out.lower())
        finally:
            if prev is None:
                os.environ.pop("OCTO_SPORK_ACTIVE_REPO", None)
            else:
                os.environ["OCTO_SPORK_ACTIVE_REPO"] = prev

    def test_strict_session_sets_context(self) -> None:
        pol = _load_policy()
        self.assertFalse(pol.strict_review_search.get())
        with pol.strict_repo_review_session("org", "repo"):
            self.assertTrue(pol.strict_review_search.get())
        self.assertFalse(pol.strict_review_search.get())


if __name__ == "__main__":
    unittest.main()
