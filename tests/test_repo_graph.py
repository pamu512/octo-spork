"""Tests for tree-sitter RepoGraph."""

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


class RepoGraphTests(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "OCTO_REPO_GRAPH_ENABLED",
            "OCTO_REPO_GRAPH_CACHE",
            "OCTO_REPO_GRAPH_REBUILD",
            "OCTO_REPO_GRAPH_MAX_FILES",
        ):
            os.environ.pop(k, None)

    def test_python_import_edge_tree_sitter(self) -> None:
        from repo_graph.graph import build_repo_graph

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app").mkdir()
            (root / "app" / "__init__.py").write_text("", encoding="utf-8")
            (root / "app" / "util.py").write_text("X = 1\n", encoding="utf-8")
            (root / "app" / "main.py").write_text("import app.util\n", encoding="utf-8")
            g = build_repo_graph(root, revision_hint="test")
            targets = {e["to"] for e in g.edges}
            self.assertTrue(any(t.endswith("app/util") or t == "app/util" for t in targets))

    def test_flatten_lists_hubs(self) -> None:
        from repo_graph.flatten import flatten_repo_graph
        from repo_graph.graph import RepoGraph

        g = RepoGraph(
            scan_root="/tmp/r",
            revision_hint="x",
            generated_at="t",
            nodes=["a", "b"],
            edges=[
                {"from": "pkg/a", "to": "pkg/b", "kind": "imports"},
                {"from": "pkg/c", "to": "pkg/b", "kind": "imports"},
            ],
            source_files_scanned=2,
        )
        text = flatten_repo_graph(g)
        self.assertIn("Repo topology", text)
        self.assertIn("pkg/b", text)

    def test_attach_disabled_skips(self) -> None:
        from repo_graph.snapshot_hook import attach_repo_graph_topology

        os.environ["OCTO_REPO_GRAPH_ENABLED"] = "false"
        snap: dict = {"scan_root": "/no/such/path"}
        attach_repo_graph_topology(snap)
        self.assertIn("Skipped", snap.get("repo_graph_topology_block", ""))


if __name__ == "__main__":
    unittest.main()
