import unittest
from pathlib import Path
import importlib.util
import subprocess
import sys
import types
import tempfile
import os
import time
from unittest.mock import patch


if "requests" not in sys.modules:
    fake_requests = types.SimpleNamespace(Session=lambda: None, post=lambda *args, **kwargs: None)
    sys.modules["requests"] = fake_requests


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "overlays"
    / "agenticseek"
    / "sources"
    / "grounded_review.py"
)

SPEC = importlib.util.spec_from_file_location("grounded_review", MODULE_PATH)
grounded_review = importlib.util.module_from_spec(SPEC)
sys.modules["grounded_review"] = grounded_review
SPEC.loader.exec_module(grounded_review)


class GroundedReviewTests(unittest.TestCase):
    def test_extract_github_repo(self):
        repo = grounded_review.extract_github_repo("Explain https://github.com/pamu512/tarka")
        self.assertEqual(repo, ("pamu512", "tarka"))
        repo_with_punctuation = grounded_review.extract_github_repo(
            "Review https://github.com/pamu512/tarka."
        )
        self.assertEqual(repo_with_punctuation, ("pamu512", "tarka"))

    def test_should_use_grounded_review(self):
        self.assertTrue(
            grounded_review.should_use_grounded_review(
                "Run a security review for https://github.com/pamu512/tarka"
            )
        )
        self.assertFalse(grounded_review.should_use_grounded_review("hello there"))

    def test_select_candidate_files_prefers_important_paths(self):
        tree = [
            {"type": "blob", "path": "README.md", "size": 2000},
            {"type": "blob", "path": "services/api/main.py", "size": 5000},
            {"type": "blob", "path": ".github/workflows/ci.yml", "size": 1200},
            {"type": "blob", "path": "deploy/docker-compose.yml", "size": 1800},
            {"type": "blob", "path": "node_modules/leftpad/index.js", "size": 600},
        ]
        selected = grounded_review.select_candidate_files(
            tree,
            query="Perform a security hardening review",
            max_files=4,
            max_total_bytes=20_000,
        )
        self.assertIn("README.md", selected)
        self.assertIn(".github/workflows/ci.yml", selected)
        self.assertIn("deploy/docker-compose.yml", selected)
        self.assertNotIn("node_modules/leftpad/index.js", selected)

    def test_discover_local_repo_from_env_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "demo-repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            old_root = os.environ.get("GROUNDED_REPO_ROOT")
            os.environ["GROUNDED_REPO_ROOT"] = tmp
            try:
                found = grounded_review.discover_local_repo("demo-repo")
                self.assertEqual(found, repo_path)
            finally:
                if old_root is None:
                    os.environ.pop("GROUNDED_REPO_ROOT", None)
                else:
                    os.environ["GROUNDED_REPO_ROOT"] = old_root

    def test_select_candidate_files_balances_coverage_across_areas(self):
        tree = [
            {"type": "blob", "path": "README.md", "size": 1500},
            {"type": "blob", "path": ".github/workflows/ci.yml", "size": 1000},
            {"type": "blob", "path": "deploy/docker-compose.production-hardening.yml", "size": 2500},
            {"type": "blob", "path": "services/api/main.py", "size": 9000},
            {"type": "blob", "path": "services/api/auth.py", "size": 7000},
            {"type": "blob", "path": "tests/test_auth.py", "size": 4500},
            {"type": "blob", "path": "docs/security.md", "size": 3200},
            {"type": "blob", "path": "src/lib/utils.ts", "size": 3500},
        ]
        selected = grounded_review.select_candidate_files(
            tree,
            query="Critical architecture and security review with QA regression analysis",
            max_files=8,
            max_total_bytes=80_000,
        )
        self.assertIn("README.md", selected)
        self.assertIn(".github/workflows/ci.yml", selected)
        self.assertIn("deploy/docker-compose.production-hardening.yml", selected)
        self.assertIn("tests/test_auth.py", selected)
        self.assertIn("services/api/main.py", selected)

    def test_should_use_two_pass_review_prefers_large_or_deep_requests(self):
        small = [{"path": "README.md", "content": "hello", "size": 5}]
        self.assertFalse(grounded_review.should_use_two_pass_review("Explain repo quickly", small))

        larger = [{"path": f"services/s{i}.py", "content": "x" * 20, "size": 20} for i in range(12)]
        self.assertTrue(
            grounded_review.should_use_two_pass_review(
                "Run a critical security and architecture hardening review",
                larger,
            )
        )

    def test_select_candidate_files_prefers_recent_change_hints(self):
        tree = [
            {"type": "blob", "path": "README.md", "size": 1500},
            {"type": "blob", "path": "services/api/main.py", "size": 6500},
            {"type": "blob", "path": "docs/overview.md", "size": 1200},
        ]
        selected = grounded_review.select_candidate_files(
            tree,
            query="Review architecture",
            preferred_paths={"services/api/main.py"},
            max_files=2,
            max_total_bytes=20_000,
        )
        self.assertIn("services/api/main.py", selected)

    def test_snapshot_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cache = grounded_review.CACHE_FILE_PATH
            old_ttl = grounded_review.CACHE_TTL_SECONDS
            old_answer_ttl = grounded_review.ANSWER_CACHE_TTL_SECONDS
            grounded_review.CACHE_FILE_PATH = Path(tmp) / "cache.json"
            grounded_review.CACHE_TTL_SECONDS = 60
            grounded_review.ANSWER_CACHE_TTL_SECONDS = 60
            try:
                snapshot = {
                    "owner": "o",
                    "repo": "r",
                    "default_branch": "main",
                    "description": "",
                    "stars": 0,
                    "forks": 0,
                    "open_issues": 0,
                    "readme": "x",
                    "files": [grounded_review.RepoFile(path="README.md", content="hello", size=5)],
                    "sources": ["README.md"],
                }
                grounded_review.set_cached_snapshot("o", "r", "review this", snapshot)
                cached = grounded_review.get_cached_snapshot("o", "r", "review this")
                self.assertIsNotNone(cached)
                self.assertEqual(cached["files"][0].path, "README.md")

                # Force expiration.
                data = grounded_review._load_cache()
                for key in list(data.keys()):
                    data[key]["ts"] = int(time.time()) - 120
                grounded_review._save_cache(data)
                expired = grounded_review.get_cached_snapshot("o", "r", "review this")
                self.assertIsNone(expired)

                answer_payload = {
                    "success": True,
                    "answer": "cached text",
                    "sources": ["README.md"],
                }
                grounded_review.set_cached_answer("o", "r", "review this", "m1", answer_payload)
                cached_answer = grounded_review.get_cached_answer("o", "r", "review this", "m1")
                self.assertEqual(cached_answer["answer"], "cached text")
            finally:
                grounded_review.CACHE_FILE_PATH = old_cache
                grounded_review.CACHE_TTL_SECONDS = old_ttl
                grounded_review.ANSWER_CACHE_TTL_SECONDS = old_answer_ttl

    def test_scope_note_includes_coverage_and_map_status(self):
        snapshot = {
            "coverage": {
                "total_files": 100,
                "total_bytes": 10000,
                "analyzed_files": 10,
                "analyzed_bytes": 1200,
                "approx_input_tokens_hint": 400,
                "repo_files_by_category": {"app": 40, "tests": 20},
                "selected_files_by_category": {"app": 3, "tests": 2},
                "revision_sha": "abcdef1234567890",
            }
        }
        note = grounded_review.build_scope_note(snapshot, "used")
        self.assertIn("priority-guided triage", note)
        self.assertIn("10/100 files", note)
        self.assertIn("Two-pass map status: used", note)
        self.assertIn("Approx. evidence scale", note)
        self.assertIn("Category snapshot", note)
        self.assertIn("answer cache", note.lower())

    def test_scope_note_explains_map_fallback(self):
        snapshot = {"coverage": {"total_files": 10, "total_bytes": 1000, "analyzed_files": 2, "analyzed_bytes": 200}}
        note = grounded_review.build_scope_note(snapshot, "fallback_map_json_parse_error")
        self.assertIn("not applied", note)

    def test_run_map_review_skips_when_no_files(self):
        digest, status = grounded_review.run_map_review(
            "review",
            {"files": [], "owner": "o", "repo": "r", "default_branch": "main"},
            model="m",
            ollama_base_url="http://localhost:11434",
        )
        self.assertEqual(digest, "")
        self.assertEqual(status, "skipped_no_files")

    def test_run_map_review_json_fallback_status(self):
        snapshot = {
            "files": [grounded_review.RepoFile(path="a.py", content="x", size=1)],
            "owner": "o",
            "repo": "r",
            "default_branch": "main",
        }
        with patch.object(grounded_review, "run_ollama_review", return_value="not-json"):
            digest, status = grounded_review.run_map_review(
                "review",
                snapshot,
                model="m",
                ollama_base_url="http://localhost:11434",
            )
        self.assertEqual(digest, "")
        self.assertEqual(status, "fallback_map_json_parse_error")

    def test_answer_cache_key_includes_revision_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = grounded_review.CACHE_FILE_PATH
            grounded_review.CACHE_FILE_PATH = Path(tmp) / "cache.json"
            grounded_review.ANSWER_CACHE_TTL_SECONDS = 3600
            try:
                p1 = {"success": True, "answer": "one", "sources": []}
                p2 = {"success": True, "answer": "two", "sources": []}
                grounded_review.set_cached_answer(
                    "o", "r", "q", "m", p1, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                )
                grounded_review.set_cached_answer(
                    "o", "r", "q", "m", p2, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                )
                self.assertEqual(
                    grounded_review.get_cached_answer(
                        "o", "r", "q", "m", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    )["answer"],
                    "one",
                )
                self.assertEqual(
                    grounded_review.get_cached_answer(
                        "o", "r", "q", "m", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                    )["answer"],
                    "two",
                )
                self.assertIsNone(
                    grounded_review.get_cached_answer(
                        "o", "r", "q", "m", "cccccccccccccccccccccccccccccccccccccccc"
                    )
                )
            finally:
                grounded_review.CACHE_FILE_PATH = old_path

    def test_git_diff_paths_lists_changed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "t@example.com"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "test"],
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True, capture_output=True)
            (repo / "a.txt").write_text("a", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "init"],
                check=True,
                capture_output=True,
            )
            (repo / "b.txt").write_text("b", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "add b"],
                check=True,
                capture_output=True,
            )
            base = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
                text=True,
            ).strip()
            head = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            paths = grounded_review.git_diff_paths(repo, base, head)
            self.assertIn("b.txt", paths)
            snap = grounded_review.fetch_local_diff_snapshot(repo, "review diff", base, head)
            self.assertIsNotNone(snap)
            assert snap is not None
            self.assertEqual(snap.get("scan_root"), str(repo.resolve()))
            self.assertEqual(snap["coverage"].get("diff_paths_count"), 1)
            md = grounded_review.format_diff_preview_markdown(snap, base, head)
            self.assertIn("b.txt", md)
            self.assertIn("no llm", md.lower())

    def test_sensitive_priority_score_prefers_auth_and_docker(self):
        self.assertGreater(
            grounded_review.sensitive_priority_score("services/auth/login.ts"),
            grounded_review.sensitive_priority_score("misc/foo.txt"),
        )
        self.assertGreaterEqual(grounded_review.sensitive_priority_score("Dockerfile"), 100)

    def test_build_source_uri_markdown_github_and_local(self):
        gh = {
            "owner": "acme",
            "repo": "demo",
            "default_branch": "main",
        }
        line = grounded_review.build_source_uri_markdown(gh, "src/auth.ts", line_start=12)
        self.assertIn("source://[", line)
        self.assertIn("github.com/acme/demo/blob/main/src/auth.ts#L12", line)
        self.assertIn("](https://github.com/acme/demo/blob/main/", line)
        local_snap = {
            "owner": "local",
            "repo": "myrepo",
            "default_branch": "local-worktree",
            "scan_root": "/tmp/gr-repo",
        }
        loc = grounded_review.build_source_uri_markdown(local_snap, "app/settings.py", line_start=1)
        self.assertTrue(loc.startswith("source://["))
        self.assertIn("myrepo/app/settings.py#L1", loc)
        self.assertIn("](file://", loc)

    def test_summarize_sarif_evidence_receipts_orders_by_severity(self):
        sarif = {
            "runs": [
                {
                    "tool": {"driver": {"rules": [{"id": "py/warn"}]}},
                    "results": [
                        {
                            "ruleId": "py/warn",
                            "level": "warning",
                            "message": {"text": "warn"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "b.py"},
                                        "region": {"startLine": 2},
                                    }
                                }
                            ],
                        },
                        {
                            "ruleId": "py/err",
                            "level": "error",
                            "message": {"text": "bad"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "a.py"},
                                        "region": {"startLine": 10},
                                    }
                                }
                            ],
                        },
                    ],
                }
            ]
        }
        items = grounded_review.summarize_sarif_evidence_receipts(sarif, limit=10)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].rule_id, "py/err")
        self.assertEqual(items[0].start_line, 10)
        self.assertEqual(items[1].rule_id, "py/warn")

    def test_extract_top_critical_findings_sorts_by_cvss(self):
        report = {
            "Results": [
                {
                    "Target": "package-lock.json",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-LOW",
                            "Severity": "CRITICAL",
                            "PkgName": "a",
                            "InstalledVersion": "1",
                            "Title": "low first",
                            "CVSS": {"nvd": {"V3Score": 7.0}},
                        },
                        {
                            "VulnerabilityID": "CVE-HIGH",
                            "Severity": "CRITICAL",
                            "PkgName": "b",
                            "InstalledVersion": "2",
                            "Title": "high second",
                            "CVSS": {"nvd": {"V3Score": 9.8}},
                        },
                    ],
                }
            ]
        }
        items = grounded_review.extract_top_critical_findings(report, limit=5)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].raw_id, "CVE-HIGH")
        self.assertEqual(items[1].raw_id, "CVE-LOW")

    def test_collect_python_direct_dependency_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text(
                "requests>=2.31\n# comment\npip_tools\n", encoding="utf-8"
            )
            names = grounded_review.collect_python_direct_dependency_names(root)
            self.assertIn("requests", names)
            self.assertIn("pip-tools", names)

    def test_parse_pip_audit_json_marks_direct_cve(self):
        payload = {
            "dependencies": [
                {
                    "name": "urllib3",
                    "version": "1.26.0",
                    "vulns": [
                        {
                            "id": "GHSA-xyz",
                            "fix_versions": ["2.0.0"],
                            "aliases": ["CVE-2023-45803"],
                        }
                    ],
                },
                {
                    "name": "transitive-pkg",
                    "version": "0.1",
                    "vulns": [{"id": "PYSEC-1", "fix_versions": [], "aliases": ["CVE-2020-9999"]}],
                },
            ],
            "fixes": [],
        }
        direct = {"urllib3"}
        rows = grounded_review.parse_pip_audit_json(payload, direct_names=direct)
        by_pkg = {r.package: r for r in rows}
        self.assertTrue(by_pkg["urllib3"].highlight_direct_cve)
        self.assertTrue(by_pkg["urllib3"].has_cve)
        self.assertFalse(by_pkg["transitive-pkg"].is_direct)

    def test_parse_npm_audit_json_detects_cve_in_title(self):
        payload = {
            "vulnerabilities": {
                "axios": {
                    "name": "axios",
                    "severity": "high",
                    "isDirect": True,
                    "range": "<= 0.21.0",
                    "via": [
                        {
                            "title": "axios SSRF (CVE-2021-3749)",
                            "url": "https://github.com/advisories/GHSA-test",
                            "severity": "high",
                        }
                    ],
                    "effects": [],
                    "fixAvailable": {"version": "1.6.0", "isSemVerMajor": False},
                },
                "nested": {
                    "name": "nested",
                    "severity": "low",
                    "isDirect": False,
                    "range": "*",
                    "via": [{"title": "Indirect advisory only GHSA-aaa", "url": "https://x"}],
                    "effects": [],
                    "fixAvailable": False,
                },
            },
            "metadata": {},
        }
        rows = grounded_review.parse_npm_audit_json(payload)
        ax = next(r for r in rows if r.package == "axios")
        self.assertTrue(ax.highlight_direct_cve)
        self.assertTrue(ax.has_cve)

    def test_sort_dep_audit_rows_prioritizes_direct_cve(self):
        rows = [
            grounded_review.DepAuditRow(
                ecosystem="npm",
                package="z",
                version_spec="*",
                is_direct=False,
                severity="critical",
                ids="x",
                fix_hint="—",
                has_cve=True,
                highlight_direct_cve=False,
            ),
            grounded_review.DepAuditRow(
                ecosystem="npm",
                package="a",
                version_spec="*",
                is_direct=True,
                severity="low",
                ids="CVE-2021-1",
                fix_hint="—",
                has_cve=True,
                highlight_direct_cve=True,
            ),
        ]
        ordered = grounded_review.sort_dep_audit_rows(rows)
        self.assertEqual(ordered[0].package, "a")

    def test_attach_dependency_audit_context_with_mocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("requests\n", encoding="utf-8")
            (root / "package.json").write_text('{"name":"x"}', encoding="utf-8")
            snapshot: dict = {"scan_root": str(root)}
            pip_payload = {"dependencies": [{"name": "requests", "version": "2.0", "vulns": []}], "fixes": []}
            npm_payload = {"vulnerabilities": {}, "metadata": {}}
            with (
                patch.object(grounded_review, "run_pip_audit_json", return_value=pip_payload),
                patch.object(grounded_review, "run_npm_audit_json", return_value=npm_payload),
            ):
                grounded_review.attach_dependency_audit_context(snapshot)
            block = snapshot.get("dependency_audit_block") or ""
            self.assertIn("pip-audit", block.lower())
            self.assertIn("npm audit", block.lower())
            self.assertIn(str(root.resolve()), block)


class DiffManagerTests(unittest.TestCase):
    def test_module_path_prefix(self) -> None:
        self.assertEqual(grounded_review._module_path_prefix("src/a/b.py", 1), "src")
        self.assertEqual(grounded_review._module_path_prefix("src/a/b.py", 2), "src/a")

    def test_group_repo_files_by_module(self) -> None:
        rf = grounded_review.RepoFile
        files = [
            rf("src/x.py", "a", 1),
            rf("src/y.py", "b", 1),
            rf("tests/t.py", "c", 1),
        ]
        groups = grounded_review._group_repo_files_by_module(files, 1)
        keys = [g[0] for g in groups]
        self.assertEqual(sorted(keys), ["src", "tests"])

    def test_split_module_files_by_token_budget(self) -> None:
        rf = grounded_review.RepoFile
        body = "x" * (4000 * 4)
        files = [rf("a.py", body, len(body)), rf("b.py", body, len(body))]
        parts = grounded_review._split_module_files_by_token_budget("mod", files, max_evidence_tokens=4000)
        self.assertGreaterEqual(len(parts), 2)


if __name__ == "__main__":
    unittest.main()
