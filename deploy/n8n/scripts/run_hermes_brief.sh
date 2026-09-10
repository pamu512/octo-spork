#!/usr/bin/env bash
set -euo pipefail
KIND="${1:?usage: run_hermes_brief.sh fraud|finance}"
case "$KIND" in
  fraud) ROOT="${HERMES_FRAUD_ROOT:?}" ;;
  finance) ROOT="${HERMES_FINANCE_ROOT:?}" ;;
  *) echo "kind must be fraud|finance" >&2; exit 2 ;;
esac
cd "$ROOT"
python3 scripts/dump_recent.py --write-brief
DAY="$(date +%F)"
if [[ -f "briefs/${DAY}.md" ]]; then
  echo "OK briefs/${DAY}.md"
  exit 0
fi
if [[ -f "briefs/${DAY}.failed.md" ]]; then
  echo "FAIL briefs/${DAY}.failed.md"
  exit 1
fi
echo "FAIL no brief for ${DAY}" >&2
exit 1
