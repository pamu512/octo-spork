"""Render repository import graphs as Graphviz SVG (cycle-highlighted).

Requires the ``dot`` executable (Graphviz). Disable SVG generation with
``OCTO_DEPS_SVG=0``. Output path (under the scanned repo): ``.octo/artifacts/dependency_graph.svg``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from repo_graph.constants import graph_enabled
from repo_graph.graph import RepoGraph, build_repo_graph
from repo_graph.persist import try_load_cached

_ARTIFACT_REL = Path(".octo") / "artifacts" / "dependency_graph.svg"


def deps_svg_enabled() -> bool:
    return os.environ.get("OCTO_DEPS_SVG", "true").lower() in {"1", "true", "yes", "on"}


def _adjacency(nodes: list[str], pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in pairs:
        if a in adj and b in adj:
            adj[a].append(b)
    return adj


def tarjan_sccs(nodes: list[str], pairs: list[tuple[str, str]]) -> list[list[str]]:
    """Return strongly connected components (each non-empty list is one SCC)."""
    adj = _adjacency(nodes, pairs)
    index = 0
    stack: list[str] = []
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = index
        lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack[v] = True

        for w in adj.get(v, []):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack.get(w):
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            comp: list[str] = []
            while stack:
                w = stack.pop()
                on_stack[w] = False
                comp.append(w)
                if w == v:
                    break
            if comp:
                sccs.append(comp)

    for v in nodes:
        if v not in indices:
            strongconnect(v)

    return sccs


def _cycle_edge_set(pairs: list[tuple[str, str]], sccs: list[list[str]]) -> set[tuple[str, str]]:
    """Edges whose endpoints lie in the same SCC with size ≥ 2 (mutual / circular import risk)."""
    rep: dict[str, int] = {}
    for i, comp in enumerate(sccs):
        for n in comp:
            rep[n] = i
    cycles: set[tuple[str, str]] = set()
    for a, b in pairs:
        ia, ib = rep.get(a), rep.get(b)
        if ia is None or ib is None or ia != ib:
            continue
        if len(sccs[ia]) >= 2:
            cycles.add((a, b))
    return cycles


def _dot_escape_label(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def repo_graph_to_dot(graph: RepoGraph, *, cycle_edges: set[tuple[str, str]]) -> str:
    """Emit directed graph DOT; cycle edges styled red."""
    lines = [
        "digraph G {",
        '  graph [rankdir=LR; fontsize=10; label="Import dependency graph (internal modules); '
        'red edges participate in a strongly connected group (circular dependency risk)."; labelloc=t];',
        "  node [shape=box; fontname=Helvetica; fontsize=9];",
        "  edge [color=gray40];",
    ]
    for n in graph.nodes:
        lbl = _dot_escape_label(n)
        lines.append(f'  "{lbl}" [label="{lbl}"];')
    for e in graph.edges:
        a = str(e.get("from") or "")
        b = str(e.get("to") or "")
        if not a or not b:
            continue
        ae = _dot_escape_label(a)
        be = _dot_escape_label(b)
        if (a, b) in cycle_edges:
            lines.append(f'  "{ae}" -> "{be}" [color=crimson; penwidth=2.0];')
        else:
            lines.append(f'  "{ae}" -> "{be}";')
    lines.append("}")
    return "\n".join(lines) + "\n"


def run_dot_to_svg(dot_source: str, out_svg: Path) -> None:
    dot_exe = shutil.which("dot")
    if not dot_exe:
        raise FileNotFoundError("Graphviz `dot` executable not found on PATH")
    out_svg.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".dot", delete=False, encoding="utf-8") as tf:
        tf.write(dot_source)
        dot_path = tf.name
    try:
        subprocess.run(
            [dot_exe, "-Tsvg", "-o", str(out_svg), dot_path],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        try:
            os.unlink(dot_path)
        except OSError:
            pass


def write_dependency_graph_svg(
    scan_root: Path,
    revision_hint: str = "",
    *,
    graph: RepoGraph | None = None,
) -> tuple[Path | None, str]:
    """Build or reuse a repo graph, write ``.octo/artifacts/dependency_graph.svg``, return markdown section.

    Returns ``(svg_path_or_none, markdown_block)``. On failure, markdown explains the skip.
    """
    if not deps_svg_enabled():
        return None, ""

    root = scan_root.expanduser().resolve()
    rel_display = str(_ARTIFACT_REL).replace("\\", "/")
    out_path = root / _ARTIFACT_REL

    if not graph_enabled():
        return None, (
            f"\n\n### Import dependency graph\n\n"
            f"_Skipped:_ repo graph disabled (`OCTO_REPO_GRAPH_ENABLED`). "
            f"Enable it to generate `{rel_display}`.\n"
        )

    try:
        g = graph
        if g is None:
            force = os.environ.get("OCTO_REPO_GRAPH_REBUILD", "").lower() in ("1", "true", "yes", "on")
            loaded = None if force else try_load_cached(root, revision_hint)
            g = loaded if loaded is not None else build_repo_graph(root, revision_hint=revision_hint or "norev")
        pairs = [(str(e.get("from") or ""), str(e.get("to") or "")) for e in g.edges]
        pairs = [(a, b) for a, b in pairs if a and b]
        sccs = tarjan_sccs(list(g.nodes), pairs)
        cycle_e = _cycle_edge_set(pairs, sccs)
        dot = repo_graph_to_dot(g, cycle_edges=cycle_e)
        run_dot_to_svg(dot, out_path)
    except FileNotFoundError as exc:
        return None, (
            f"\n\n### Import dependency graph\n\n"
            f"_Could not render SVG:_ {exc}. "
            f"Install [Graphviz](https://graphviz.org/download/) so `dot` is on `PATH`, "
            f"or set `OCTO_DEPS_SVG=0` to hide this notice.\n"
        )
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc))[:400]
        return None, (
            f"\n\n### Import dependency graph\n\n"
            f"_Graphviz failed:_ `{err}`\n"
        )
    except OSError as exc:
        return None, f"\n\n### Import dependency graph\n\n_Skipped:_ `{exc}`\n"

    n_cycle_edges = len(cycle_e)
    n_scc_bad = sum(1 for c in sccs if len(c) >= 2)
    uri = out_path.as_uri()
    return out_path, (
        "\n\n### Import dependency graph (local SVG)\n\n"
        f"- **Open (file URL):** [dependency_graph.svg]({uri})\n"
        f"- **Relative path:** `{rel_display}` (under scanned repository root)\n"
        f"- **Stats:** {len(g.nodes)} modules, {len(g.edges)} import edges, "
        f"{n_scc_bad} strongly connected group(s) with ≥2 modules, "
        f"{n_cycle_edges} edge(s) highlighted in **crimson** (within-SCC imports).\n"
        "_Use this diagram before merging agent-proposed refactors that might introduce new cross-module imports "
        "(crimson chains indicate existing circular dependency risk)._\n"
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Render repo import graph as SVG via Graphviz.")
    p.add_argument(
        "--scan-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root to scan (default: cwd)",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=f"Copy SVG to this path after render (default: write under scan-root/{_ARTIFACT_REL})",
    )
    p.add_argument("--revision-hint", default="", help="Optional revision label for cache lookup")
    args = p.parse_args(argv)

    root = args.scan_root.expanduser().resolve()
    svg_path, md = write_dependency_graph_svg(root, args.revision_hint)
    if svg_path is None:
        print(md.strip(), file=sys.stderr)
        return 1
    if args.output is not None:
        import shutil

        dest = args.output.expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(svg_path, dest)
        print(dest)
        return 0
    print(svg_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
