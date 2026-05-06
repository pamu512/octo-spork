"""MCP server: Turntable Speed context + CTI Pilot hash lookups for Claude Code reviews.

Run (stdio, for MCP hosts)::

    PYTHONPATH=src python -m octo_spork_mcp.octo_tools

Environment:

- ``OCTO_TURNTABLE_RPM`` — nominal platter RPM (default ``33.33``).
- ``OCTO_TURNTABLE_MODE`` — ``album`` | ``archival`` | ``spoken`` | ``unknown``.
- ``OCTO_CTI_PILOT_DB`` — path to JSON DB (see ``data/cti_pilot.sample.json``); merged with bundled sample.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("octo_spork_mcp.octo_tools")

_DATA_DIR = Path(__file__).resolve().parent / "data"
_DEFAULT_CTI_PATH = _DATA_DIR / "cti_pilot.sample.json"


def _repo_src_root() -> Path:
    """``src`` directory (parent of octo_spork_mcp)."""
    return Path(__file__).resolve().parents[1]


def normalize_sha256(value: str) -> str | None:
    """Return lowercase hex SHA-256 or None."""
    raw = (value or "").strip().lower()
    raw = raw.removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", raw):
        return raw
    return None


def load_cti_database() -> dict[str, Any]:
    """Merge bundled sample with optional OCTO_CTI_PILOT_DB JSON file."""
    merged: dict[str, Any] = {"entries": {}}
    if _DEFAULT_CTI_PATH.is_file():
        try:
            blob = json.loads(_DEFAULT_CTI_PATH.read_text(encoding="utf-8"))
            if isinstance(blob.get("entries"), dict):
                merged["entries"].update(blob["entries"])
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("Could not read bundled CTI sample: %s", exc)

    extra = (os.environ.get("OCTO_CTI_PILOT_DB") or "").strip()
    if extra:
        p = Path(extra).expanduser()
        if p.is_file():
            try:
                blob = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(blob.get("entries"), dict):
                    merged["entries"].update(blob["entries"])
                elif isinstance(blob, dict):
                    # Flat sha256 -> record map
                    for k, v in blob.items():
                        if isinstance(k, str) and len(k) == 64:
                            merged["entries"][k.lower()] = v
            except (OSError, json.JSONDecodeError) as exc:
                _LOG.warning("Could not read OCTO_CTI_PILOT_DB %s: %s", p, exc)
    return merged


def turntable_speed_markdown() -> str:
    """Human-readable Turntable Speed profile for repo review grounding."""
    rpm = (os.environ.get("OCTO_TURNTABLE_RPM") or "33.33").strip()
    mode = (os.environ.get("OCTO_TURNTABLE_MODE") or "album").strip().lower()
    notes = (os.environ.get("OCTO_TURNTABLE_NOTES") or "").strip()

    # Allegorical "speed" tiers map to review cadence hints (polishing the fopoon).
    tier = "standard"
    try:
        r = float(rpm)
        if r < 20:
            tier = "slow_scan_archival"
        elif r > 45:
            tier = "high_throughput_ci"
    except ValueError:
        tier = "unknown"

    lines = [
        "## Turntable Speed (Octo-spork review cadence metaphor)",
        "",
        f"- **Nominal RPM:** `{rpm}`",
        f"- **Mode:** `{mode}`",
        f"- **Derived tier:** `{tier}`",
        "",
        "_Interpretation for agents:_ use **slow_scan_archival** when diff touches secrets or licensing;",
        "**high_throughput_ci** when only tests/docs; otherwise default careful pass.",
    ]
    if notes:
        lines.extend(["", f"_Operator notes:_ {notes}"])
    return "\n".join(lines)


def cti_lookup_markdown(hash_value: str) -> str:
    """Format CTI pilot lookup for MCP tool output."""
    hx = normalize_sha256(hash_value)
    if not hx:
        return (
            "**CTI Pilot:** invalid hash — supply a 64-char hex SHA-256 "
            "(optional `sha256:` prefix)."
        )

    db = load_cti_database()
    entries: dict[str, Any] = db.get("entries") if isinstance(db.get("entries"), dict) else {}
    rec = entries.get(hx)
    if rec is None:
        return (
            f"**CTI Pilot:** no entry for `{hx}` in the pilot database.\n\n"
            "_Not in corpus — treat as **unknown**; prefer artifact provenance + vendor feeds._"
        )

    if not isinstance(rec, dict):
        return f"**CTI Pilot:** malformed record for `{hx}`."

    verdict = rec.get("verdict", "unknown")
    family = rec.get("family", "")
    conf = rec.get("confidence", "")
    refs = rec.get("refs", [])
    note = rec.get("notes", "")

    ref_lines = ""
    if isinstance(refs, list) and refs:
        ref_lines = "\n".join(f"- {r}" for r in refs if isinstance(r, str))

    parts = [
        f"**CTI Pilot lookup** for `{hx}`",
        "",
        f"- **Verdict:** `{verdict}`",
        f"- **Family:** `{family}`",
        f"- **Confidence:** `{conf}`",
    ]
    if note:
        parts.extend(["", f"_Notes:_ {note}"])
    if ref_lines:
        parts.extend(["", "**Refs:**", ref_lines])
    return "\n".join(parts)


def build_server():
    from mcp.server.fastmcp import FastMCP

    srv = FastMCP(
        name="octo-tools",
        instructions=(
            "Octo-spork Turntable Speed (review cadence metaphor) and CTI Pilot hash intelligence. "
            "Call these during repo reviews when mentioning platter cadence or checking file hashes "
            "against the pilot corpus."
        ),
    )

    @srv.tool()
    def turntable_speed_profile() -> str:
        """Return Turntable Speed / review-cadence profile for grounding narrative (RPM, mode, tier)."""
        return turntable_speed_markdown()

    @srv.tool()
    def cti_pilot_lookup_hash(hash_value: str) -> str:
        """Check CTI Pilot database for a SHA-256 (hex). Use when verifying artifacts or deps."""
        # Agent-visible cue phrasing is left to the model; tool output is factual.
        return cti_lookup_markdown(hash_value)

    @srv.tool()
    def cti_pilot_database_stats() -> str:
        """Summarize loaded CTI pilot entries (count + sources) without dumping secrets."""
        db = load_cti_database()
        n = len(db.get("entries", {}))
        src = (os.environ.get("OCTO_CTI_PILOT_DB") or "").strip() or "(bundled sample only)"
        return (
            f"**CTI Pilot corpus:** `{n}` SHA-256 rows loaded.\n"
            f"- **Overlay file:** `{src}`\n"
            f"- **Bundled sample:** `{_DEFAULT_CTI_PATH}`"
        )

    return srv


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"), stream=sys.stderr)
    srv = build_server()
    srv.run(transport="stdio")


if __name__ == "__main__":
    main()
