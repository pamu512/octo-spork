#!/usr/bin/env python3
"""Record developer corrections (negative examples) into the Correction Ledger (ChromaDB)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    p = argparse.ArgumentParser(description="Append a negative example to the Correction Ledger.")
    sub = p.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="Store rejected assistant text vs developer-preferred text")
    rec.add_argument("--rejected", help="Previous AI-suggested text (or read stdin if omitted)")
    rec.add_argument("--corrected", required=True, help="Text after developer correction")
    rec.add_argument("--repo", default="manual/cli", help="owner/repo label for metadata")
    rec.add_argument("--editor", default=os.environ.get("USER", "cli"), help="Who recorded this")

    args = p.parse_args()
    if args.cmd != "record":
        return 2

    rejected = args.rejected
    if rejected is None:
        rejected = sys.stdin.read()
    if not (rejected or "").strip():
        sys.stderr.write("Provide --rejected or pipe rejected text on stdin.\n")
        return 2

    os.environ.setdefault("OCTO_CORRECTION_LEDGER", "1")

    from github_bot.correction_ledger import cli_record

    return cli_record(
        rejected=rejected,
        corrected=args.corrected,
        repo=args.repo,
        editor=args.editor,
        source="cli",
    )


if __name__ == "__main__":
    raise SystemExit(main())
