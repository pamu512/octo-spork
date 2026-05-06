"""Turn a RepoGraph into a short markdown topology briefing for LLM prompts."""

from __future__ import annotations

from collections import Counter
from typing import Any

from repo_graph.constants import SUMMARY_MAX_CHARS
from repo_graph.graph import RepoGraph
from repo_graph.resolve import top_level_prefix


def flatten_repo_graph(graph: RepoGraph) -> str:
    """Produce a concise topology summary (dependencies bird's-eye view)."""
    if not graph.edges and not graph.nodes:
        return (
            "_No internal import edges were resolved (empty slice, externals only, or parse limits). "
            "This is not evidence of a flat codebase._"
        )

    out_deg: Counter[str] = Counter()
    in_deg: Counter[str] = Counter()
    cross_edges: Counter[tuple[str, str]] = Counter()

    for e in graph.edges:
        a = e.get("from") or ""
        b = e.get("to") or ""
        if not a or not b:
            continue
        out_deg[a] += 1
        in_deg[b] += 1
        pa = top_level_prefix(a)
        pb = top_level_prefix(b)
        if pa != pb:
            cross_edges[(pa, pb)] += 1

    top_importers = in_deg.most_common(12)
    top_exporters = out_deg.most_common(12)
    top_cross = cross_edges.most_common(14)

    lines: list[str] = [
        "### Repo topology (tree-sitter import graph)",
        "",
        f"- **Scan root:** `{graph.scan_root}`",
        f"- **Revision hint:** `{graph.revision_hint or 'none'}`",
        f"- **Files scanned:** {graph.source_files_scanned}",
        f"- **Internal edges:** {len(graph.edges)} (tree-sitter parse of import/export statements; omits most externals)",
        "",
        "#### Top dependency hubs (most imported internal targets)",
    ]
    if not top_importers:
        lines.append("- _(none)_")
    else:
        for node, deg in top_importers:
            lines.append(f"- `{node}` ← **{deg}** inbound edge(s)")

    lines.extend(["", "#### Primary exporters (most outbound internal imports)"])
    if not top_exporters:
        lines.append("- _(none)_")
    else:
        for node, deg in top_exporters:
            lines.append(f"- `{node}` → **{deg}** outbound edge(s)")

    lines.extend(["", "#### Cross-boundary flows (top-level path prefix)"])
    if not top_cross:
        lines.append("- _(no cross-prefix edges among resolved internals)_")
    else:
        for (pa, pb), n in top_cross:
            lines.append(f"- `{pa}/**` → `{pb}/**`: **{n}** edge(s)")

    lines.extend(
        [
            "",
            "_Use this graph as **macro-structure only**; dynamic imports and runtime wiring are not modeled._",
        ]
    )

    text = "\n".join(lines).strip()
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[: SUMMARY_MAX_CHARS - 40] + "\n\n… _(summary truncated; tune OCTO_REPO_GRAPH_SUMMARY_CHARS)_"
    return text


def flatten_json_stub(payload: dict[str, Any]) -> str:
    """Flatten from raw JSON dict (e.g. loaded file) without full validation."""
    g = RepoGraph(
        scan_root=str(payload.get("scan_root") or ""),
        revision_hint=str(payload.get("revision_hint") or ""),
        generated_at=str(payload.get("generated_at") or ""),
        nodes=list(payload.get("nodes") or []),
        edges=list(payload.get("edges") or []),
        source_files_scanned=int(payload.get("source_files_scanned") or 0),
        meta=dict(payload.get("meta") or {}),
    )
    return flatten_repo_graph(g)
