#!/usr/bin/env bash
# Unattended audit hook for cron / n8n / Hermes-style schedulers.
# Run from repo root (or set OCTO_SPORK_REPO_ROOT). Does not start the full stack.
#
# Examples:
#   python -m local_ai_stack nightly-audit --repo .
#   ./scripts/nightly_audit.sh
#
# cron (host):
#   15 3 * * * cd /path/to/octo-spork && ./scripts/nightly_audit.sh
#
# Hermes Agent (outer scheduler only — do not let Hermes edit the repo while
# /octo-spork fix is running): schedule a daily job that shells:
#   python -m local_ai_stack nightly-audit --repo /path/to/target-repo
#
set -euo pipefail

ROOT="${OCTO_SPORK_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"

ENV_FILE="${OCTO_ENV_FILE:-${ROOT}/deploy/local-ai/.env.local}"
REPO="${OCTO_AUDIT_REPO:-.}"
LOG_DIR="${ROOT}/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="${LOG_DIR}/nightly_audit_${STAMP}.log"

{
  echo "== nightly_audit ${STAMP} =="
  echo "root=${ROOT}"
  python3 -m local_ai_stack doctor --env-file "$ENV_FILE" --repo "$REPO" --strict || true
  python3 -m local_ai_stack status --env-file "$ENV_FILE" || true
  python3 -m local_ai_stack pre-push-scan --env-file "$ENV_FILE" --repo "$REPO" || true
} 2>&1 | tee "$LOG"

echo "Wrote ${LOG}"
