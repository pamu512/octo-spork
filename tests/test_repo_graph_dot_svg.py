"""Tests for repo_graph.dot_svg (Tarjan SCC + DOT emit)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class DotSvgTests(unittest.TestCase):
    def test_tarjan_cycle(self) -> None:
        from repo_graph.dot_svg import _cycle_edge_set, tarjan_sccs

        nodes = ["a", "b", "c"]
        pairs = [("a", "b"), ("b", "c"), ("c", "a")]
        sccs = tarjan_sccs(nodes, pairs)
        self.assertEqual(len(sccs), 1)
        self.assertEqual(set(sccs[0]), {"a", "b", "c"})
        ce = _cycle_edge_set(pairs, sccs)
        self.assertEqual(ce, set(pairs))

    def test_tarjan_dag(self) -> None:
        from repo_graph.dot_svg import _cycle_edge_set, tarjan_sccs

        nodes = ["a", "b", "c"]
        pairs = [("a", "b"), ("b", "c")]
        sccs = tarjan_sccs(nodes, pairs)
        self.assertEqual(len(sccs), 3)
        self.assertEqual(_cycle_edge_set(pairs, sccs), set())

    def test_repo_graph_to_dot_colors_cycle_edges(self) -> None:
        from repo_graph.graph import RepoGraph
        from repo_graph.dot_svg import repo_graph_to_dot

        g = RepoGraph(
            scan_root="/tmp",
            revision_hint="t",
            generated_at="",
            nodes=["x", "y"],
            edges=[{"from": "x", "to": "y", "kind": "imports"}, {"from": "y", "to": "x", "kind": "imports"}],
            source_files_scanned=2,
            meta={},
        )
        dot = repo_graph_to_dot(g, cycle_edges={("x", "y"), ("y", "x")})
        self.assertIn("crimson", dot)
        self.assertIn("x", dot)
        self.assertIn("y", dot)

    def test_write_skips_when_disabled(self) -> None:
        from repo_graph import dot_svg as mod

        os.environ["OCTO_DEPS_SVG"] = "0"
        try:
            p, md = mod.write_dependency_graph_svg(Path("/nope"))
            self.assertIsNone(p)
            self.assertEqual(md, "")
        finally:
            os.environ.pop("OCTO_DEPS_SVG", None)


if __name__ == "__main__":
    unittest.main()
