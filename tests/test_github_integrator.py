import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "github_integrator.py"
SPEC = importlib.util.spec_from_file_location("github_integrator", MODULE_PATH)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["github_integrator"] = mod
SPEC.loader.exec_module(mod)


class GithubIntegratorParseTests(unittest.TestCase):
    def test_parse_pr_url(self):
        self.assertEqual(
            mod.parse_pr_url("https://github.com/acme/widget/pull/42"),
            ("acme", "widget", 42),
        )
        self.assertEqual(
            mod.parse_pr_url("https://github.com/acme/widget/pull/42/files"),
            ("acme", "widget", 42),
        )

    def test_parse_pr_url_invalid(self):
        with self.assertRaises(ValueError):
            mod.parse_pr_url("https://gitlab.com/x/y/-/merge_requests/1")

    def test_format_evidence_comment_escapes_diff(self):
        body = mod.format_evidence_comment(
            html_url="https://github.com/o/r/pull/1",
            title="t",
            base_label="main",
            head_label="feat",
            additions=1,
            deletions=2,
            changed_files=3,
            review_markdown="Hello",
            diff_excerpt='diff --git a/x\n+<evil>&</evil>',
            model="m",
            rate_remaining=4999,
        )
        self.assertIn("&lt;evil&gt;", body)
        self.assertNotIn("<evil>", body)
        self.assertIn("Evidence-first grounded review", body)

    def test_format_evidence_comment_appends_trivy(self):
        body = mod.format_evidence_comment(
            html_url="https://github.com/o/r/pull/1",
            title="t",
            base_label="main",
            head_label="feat",
            additions=1,
            deletions=2,
            changed_files=3,
            review_markdown="AI body",
            diff_excerpt="diff",
            model="m",
            rate_remaining=10,
            trivy_markdown="### Trivy\n\n| a | b |\n| - | - |",
        )
        self.assertIn("AI body", body)
        self.assertIn("### Trivy", body)
        self.assertTrue(body.index("### Trivy") > body.index("AI body"))

    def test_format_evidence_comment_appends_codeql_after_trivy(self):
        body = mod.format_evidence_comment(
            html_url="https://github.com/o/r/pull/1",
            title="t",
            base_label="main",
            head_label="feat",
            additions=1,
            deletions=2,
            changed_files=3,
            review_markdown="AI body",
            diff_excerpt="diff",
            model="m",
            rate_remaining=10,
            trivy_markdown="### Trivy\n\nx",
            codeql_markdown="### CodeQL — Critical findings\n\ny",
        )
        self.assertLess(body.index("### Trivy"), body.index("### CodeQL"))

    def test_format_evidence_inserts_risk_analysis(self):
        body = mod.format_evidence_comment(
            html_url="https://github.com/o/r/pull/1",
            title="t",
            base_label="main",
            head_label="feat",
            additions=1,
            deletions=2,
            changed_files=3,
            review_markdown="Hello",
            diff_excerpt="d",
            model="m",
            rate_remaining=10,
            risk_analysis_markdown="### NC\n\n| # | x |\n|---|",
        )
        self.assertIn("### NC", body)
        self.assertLess(body.index("### NC"), body.index("Hello"))

    def test_format_evidence_inserts_dependency_graph(self):
        body = mod.format_evidence_comment(
            html_url="https://github.com/o/r/pull/1",
            title="t",
            base_label="main",
            head_label="feat",
            additions=1,
            deletions=2,
            changed_files=3,
            review_markdown="Hello",
            diff_excerpt="d",
            model="m",
            rate_remaining=10,
            dependency_graph_markdown="\n\n### Import dependency graph\n\n[link](file:///tmp/x.svg)\n",
        )
        self.assertIn("### Import dependency graph", body)
        self.assertLess(body.index("### Import dependency graph"), body.index("Hello"))

    def test_grounded_receipts_lists_paths(self):
        body = mod.format_evidence_comment(
            html_url="https://github.com/o/r/pull/1",
            title="t",
            base_label="main",
            head_label="feat",
            additions=1,
            deletions=2,
            changed_files=3,
            review_markdown="Hello",
            diff_excerpt="d",
            model="m",
            rate_remaining=10,
            grounded_receipt_paths=["src/a.py", "README.md", "src/a.py"],
        )
        self.assertIn("## Grounded Receipts", body)
        self.assertIn("**Analyzed from:** `README.md`", body)
        self.assertLess(body.index("Hello"), body.index("## Grounded Receipts"))


if __name__ == "__main__":
    unittest.main()
