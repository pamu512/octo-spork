import unittest
from pathlib import Path
import importlib.util
import sys
import types
import tempfile
import os


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
            {"type": "blob", "path": "node_modules/leftpad/index.js", "size": 600},
        ]
        selected = grounded_review.select_candidate_files(tree, max_files=3, max_total_bytes=20_000)
        self.assertIn("README.md", selected)
        self.assertIn(".github/workflows/ci.yml", selected)
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


if __name__ == "__main__":
    unittest.main()
