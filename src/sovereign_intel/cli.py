"""CLI: ``python -m sovereign_intel`` — ingest, compare, and fleet status."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sovereign_intel.compare_util import compare_live_scan_markdown, format_network_markdown
from sovereign_intel.scan import ingest_repo_scan
from sovereign_intel.store import record_critical_pattern_hits


def _cmd_status(_args: argparse.Namespace) -> int:
    print(format_network_markdown())
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    root = Path(args.repo).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2
    findings = ingest_repo_scan(root)
    names = sorted({f.pattern_name for f in findings})
    if names:
        record_critical_pattern_hits(root, names)
        print(f"Recorded {len(names)} pattern type(s) from {root}:")
        for n in names:
            print(f"  - {n}")
    else:
        print(f"No credential patterns matched in sampled files under {root}.")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    paths = [Path(p).expanduser().resolve() for p in args.repos]
    for p in paths:
        if not p.is_dir():
            print(f"Not a directory: {p}", file=sys.stderr)
            return 2
    print(compare_live_scan_markdown(paths))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sovereign Intelligence — cross-repo credential-pattern fleet memory.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Show persisted fleet patterns (JSON store).")
    p_status.set_defaults(func=_cmd_status)

    p_ingest = sub.add_parser(
        "ingest",
        help="Scan a local repo and record Critical-tier pattern names into the fleet store.",
    )
    p_ingest.add_argument("repo", type=str, help="Path to git repository root")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_cmp = sub.add_parser(
        "compare",
        help="Live-scan multiple repos and print a hit matrix (no persistence).",
    )
    p_cmp.add_argument(
        "repos",
        nargs="+",
        type=str,
        help="Paths to repository roots",
    )
    p_cmp.set_defaults(func=_cmd_compare)

    ns = parser.parse_args(argv)
    return int(ns.func(ns))


if __name__ == "__main__":
    raise SystemExit(main())
