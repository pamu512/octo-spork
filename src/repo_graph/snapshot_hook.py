"""Attach RepoGraph topology briefing to a grounded-review snapshot."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from repo_graph.constants import graph_enabled
from repo_graph.flatten import flatten_repo_graph
from repo_graph.graph import build_repo_graph
from repo_graph.persist import cache_path_for, save_graph, try_load_cached


def attach_repo_graph_topology(snapshot: dict[str, Any]) -> None:
    """Set ``repo_graph_topology_block`` — concise topology summary for the LLM (before file bodies)."""
    snapshot.setdefault("repo_graph_topology_block", "")
    if not graph_enabled():
        snapshot["repo_graph_topology_block"] = (
            "### Repo topology (tree-sitter)\n\n"
            "Skipped: `OCTO_REPO_GRAPH_ENABLED` is false.\n\n"
        )
        return

    raw = snapshot.get("scan_root")
    if not raw:
        return

    scan_root = Path(str(raw)).expanduser().resolve()
    if not scan_root.is_dir():
        return

    rev_raw = snapshot.get("revision_sha") or (snapshot.get("coverage") or {}).get("revision_sha")
    revision_hint = str(rev_raw).strip()[:40] if rev_raw else ""

    force = os.environ.get("OCTO_REPO_GRAPH_REBUILD", "").lower() in ("1", "true", "yes", "on")
    graph = None if force else try_load_cached(scan_root, revision_hint)
    if graph is None:
        graph = build_repo_graph(scan_root, revision_hint=revision_hint or "norev")
        try:
            save_graph(cache_path_for(scan_root, revision_hint), graph)
        except OSError:
            pass

    summary = flatten_repo_graph(graph)
    snapshot["repo_graph_topology_block"] = summary
    snapshot["repo_graph_edges_json"] = graph.edges[:400]
