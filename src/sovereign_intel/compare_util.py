"""Compare findings across repositories for CLI output."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from sovereign_intel.scan import ingest_repo_scan
from sovereign_intel.store import all_pattern_repo_matrix, load_network


def format_network_markdown() -> str:
    """Human-readable view of persisted fleet store."""
    matrix = all_pattern_repo_matrix()
    if not matrix:
        return "_Empty fleet store — no patterns recorded._\n"

    lines = [
        "## Sovereign Intelligence — persisted patterns",
        "",
        "| Pattern | Seen in repos |",
        "| --- | --- |",
    ]
    for pname in sorted(matrix.keys()):
        repos = matrix[pname]
        cell = "; ".join(f"`{r}`" for r in repos[:6])
        if len(repos) > 6:
            cell += f" _(+{len(repos) - 6} more)_"
        pn = pname.replace("|", "\\|")
        lines.append(f"| `{pn}` | {cell} |")
    lines.append("")
    data = load_network()
    if isinstance(data.get("updated_at"), str):
        lines.append(f"_Last updated: {data['updated_at']}_\n")
    return "\n".join(lines)


def compare_live_scan_markdown(repo_paths: list[Path]) -> str:
    """Run ingest scan on each repo and render a comparison matrix."""
    rows: dict[str, dict[str, int]] = defaultdict(dict)
    cols: list[str] = []
    for p in repo_paths:
        root = p.expanduser().resolve()
        label = str(root)
        cols.append(label)
        findings = ingest_repo_scan(root)
        by_name: dict[str, int] = defaultdict(int)
        for f in findings:
            by_name[f.pattern_name] += 1
        for pname, cnt in by_name.items():
            rows[pname][label] = cnt

    if not rows:
        return "_No secret-pattern hits in scanned repositories (within byte/file limits)._\n"

    lines = [
        "## Live comparison — pattern hits per repository",
        "",
        "| Pattern | " + " | ".join(f"`{c[:48]}…`" if len(c) > 48 else f"`{c}`" for c in cols) + " |",
        "| --- | " + " | ".join("---" for _ in cols) + " |",
    ]
    for pname in sorted(rows.keys()):
        cells = []
        for c in cols:
            v = rows[pname].get(c, 0)
            cells.append(str(v) if v else "—")
        pn = pname.replace("|", "\\|")
        lines.append(f"| `{pn}` | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("_Counts are heuristic regex hits (same rules as PR secret scan)._")
    return "\n".join(lines)
