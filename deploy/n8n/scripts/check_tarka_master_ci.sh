#!/usr/bin/env bash
set -euo pipefail
# Latest completed-or-in-progress workflow run on master; alert only on hard failure.
JSON="$(gh run list -R pamu512/tarka --branch master --limit 1 --json conclusion,url,name,displayTitle)"
echo "$JSON"
printf '%s\n' "$JSON" | python3 -c '
import json, sys
runs = json.load(sys.stdin)
if not runs:
    raise SystemExit(0)
c = (runs[0].get("conclusion") or "").lower()
print(runs[0].get("url") or "")
raise SystemExit(0 if c not in {"failure", "timed_out", "cancelled"} else 1)
'
