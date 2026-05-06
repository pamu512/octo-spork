"""Build a directed dependency graph from tree-sitter import edges."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repo_graph.constants import MAX_EDGES_STORE
from repo_graph.discover import discover_source_files
from repo_graph.extract import build_edges_for_file
from repo_graph.resolve import is_internal_target


@dataclass
class RepoGraph:
    """Serializable directed graph (nodes are repo-relative ids; edges are imports)."""

    scan_root: str
    revision_hint: str
    generated_at: str
    nodes: list[str]
    edges: list[dict[str, str]]
    source_files_scanned: int
    meta: dict[str, Any] = field(default_factory=dict)


def _digest_scan_root(scan_root: Path) -> str:
    return hashlib.sha256(str(scan_root.resolve()).encode("utf-8")).hexdigest()[:16]


def build_repo_graph(scan_root: Path, *, revision_hint: str = "") -> RepoGraph:
    """Parse sources under ``scan_root`` and collect internal dependency edges."""
    root = scan_root.expanduser().resolve()
    if not root.is_dir():
        return RepoGraph(
            scan_root=str(root),
            revision_hint=revision_hint,
            generated_at=_utc_now(),
            nodes=[],
            edges=[],
            source_files_scanned=0,
            meta={"error": "not_a_directory"},
        )

    paths = discover_source_files(root)
    raw_edges: set[tuple[str, str]] = set()
    for p in paths:
        raw_edges |= build_edges_for_file(p, root)

    internal: list[tuple[str, str]] = []
    for a, b in sorted(raw_edges):
        if a == b:
            continue
        if not is_internal_target(b, root):
            continue
        internal.append((a, b))

    if len(internal) > MAX_EDGES_STORE:
        internal = internal[:MAX_EDGES_STORE]

    nodes_set: set[str] = set()
    edge_dicts: list[dict[str, str]] = []
    for a, b in internal:
        nodes_set.add(a)
        nodes_set.add(b)
        edge_dicts.append({"from": a, "to": b, "kind": "imports"})

    return RepoGraph(
        scan_root=str(root),
        revision_hint=revision_hint,
        generated_at=_utc_now(),
        nodes=sorted(nodes_set),
        edges=edge_dicts,
        source_files_scanned=len(paths),
        meta={
            "scan_root_digest": _digest_scan_root(root),
            "edge_count_raw": len(raw_edges),
            "edge_count_internal": len(edge_dicts),
        },
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def graph_to_jsonable(g: RepoGraph) -> dict[str, Any]:
    return {
        "version": 1,
        "scan_root": g.scan_root,
        "revision_hint": g.revision_hint,
        "generated_at": g.generated_at,
        "nodes": g.nodes,
        "edges": g.edges,
        "source_files_scanned": g.source_files_scanned,
        "meta": g.meta,
    }


def graph_from_jsonable(data: dict[str, Any]) -> RepoGraph:
    return RepoGraph(
        scan_root=str(data.get("scan_root") or ""),
        revision_hint=str(data.get("revision_hint") or ""),
        generated_at=str(data.get("generated_at") or ""),
        nodes=list(data.get("nodes") or []),
        edges=list(data.get("edges") or []),
        source_files_scanned=int(data.get("source_files_scanned") or 0),
        meta=dict(data.get("meta") or {}),
    )
