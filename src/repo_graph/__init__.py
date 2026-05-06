"""Tree-sitter–based repository import graph, JSON persistence, and LLM topology summaries."""

from repo_graph.flatten import flatten_repo_graph
from repo_graph.graph import RepoGraph, build_repo_graph, graph_to_jsonable
from repo_graph.snapshot_hook import attach_repo_graph_topology

__all__ = [
    "RepoGraph",
    "attach_repo_graph_topology",
    "build_repo_graph",
    "flatten_repo_graph",
    "graph_to_jsonable",
]
