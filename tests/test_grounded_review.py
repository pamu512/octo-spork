import unittest
from pathlib import Path
import importlib.util
import sys
import types
import tempfile
import os
import time


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


if __name__ == "__main__":
    unittest.main()
