#!/usr/bin/env bash
set -euo pipefail
ROOT="${OCTO_SPORK_ROOT:?}"
cd "$ROOT"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"
BASE="${OCTO_AUDIT_API_BASE:-http://127.0.0.1:8008}"
# Prefer API if up; else local script
if curl -sf --max-time 2 "${BASE}/health" >/dev/null 2>&1; then
  curl -sf -X POST -H "X-API-Key: ${OCTO_AUDIT_API_KEY:-}" \
    "${BASE}/octo/audit/run?repo=." >/dev/null
  sleep 120
  curl -sf -H "X-API-Key: ${OCTO_AUDIT_API_KEY:-}" \
    "${BASE}/octo/audit/status"
else
  ./scripts/nightly_audit.sh
fi
