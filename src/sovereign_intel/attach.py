"""Inject Sovereign Intelligence block into grounded-review snapshots."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sovereign_intel.scan import scan_worktree_for_patterns
from sovereign_intel.store import pattern_names_from_other_repos


def sovereign_intel_enabled() -> bool:
    return os.environ.get("OCTO_SOVEREIGN_INTEL_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def attach_sovereign_intel(snapshot: dict[str, Any]) -> None:
    """Set ``sovereign_intel_block`` when cross-repo Critical patterns apply to this workspace."""
    snapshot.setdefault("sovereign_intel_block", "")
    if not sovereign_intel_enabled():
        snapshot["sovereign_intel_block"] = (
            "### Sovereign Intelligence\n\n"
            "_Skipped (`OCTO_SOVEREIGN_INTEL_ENABLED=false`)._\n\n"
        )
        return

    raw = snapshot.get("scan_root")
    if not raw:
        return

    root = Path(str(raw)).expanduser().resolve()
    if not root.is_dir():
        return

    fleet_names = pattern_names_from_other_repos(root)
    if not fleet_names:
        snapshot["sovereign_intel_block"] = (
            "### Sovereign Intelligence (local fleet)\n\n"
            "_No **Critical** credential patterns have been recorded from other repositories yet._ "
            "Run `python -m sovereign_intel ingest <path>` after hits in another clone, or trigger a PR "
            "review that surfaces secrets.\n\n"
        )
        return

    names_set = set(fleet_names)
    hits = scan_worktree_for_patterns(root, names_set)

    lines = [
        "### Sovereign Intelligence (local fleet)",
        "",
        "These **credential pattern types** were previously flagged **Critical** in **another** local "
        "repository on this machine. Treat any match here as **High Priority** — verify and rotate "
        "material immediately.",
        "",
        "**Fleet-tracked patterns (from peer repos):**",
    ]
    for n in fleet_names:
        lines.append(f"- `{n}`")

    if hits:
        lines.extend(
            [
                "",
                "**Possible matches in this workspace** (heuristic scan of tracked/text files; confirm in context):",
                "",
                "| Pattern | Category | Redacted preview |",
                "| --- | --- | --- |",
            ]
        )
        seen: set[str] = set()
        for h in hits[:24]:
            key = f"{h.pattern_name}|{h.category}"
            if key in seen:
                continue
            seen.add(key)
            cat = h.category.replace("|", "\\|")
            prev = h.redacted_preview.replace("|", "\\|")
            pn = h.pattern_name.replace("|", "\\|")
            lines.append(f"| `{pn}` | {cat} | {prev} |")
        lines.append("")
        lines.append("_This scan is bounded by size limits; absence of rows does not prove safety._")
    else:
        lines.extend(
            [
                "",
                "_No immediate subset-pattern hits in the sampled worktree text — still apply **High Priority** "
                "review discipline for the patterns listed above._",
            ]
        )

    lines.append("")
    snapshot["sovereign_intel_block"] = "\n".join(lines).strip() + "\n\n"
