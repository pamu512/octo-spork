#!/usr/bin/env bash
# Route a short message to WhatsApp Briefs or Alerts via Hermes deliver target.
set -euo pipefail

CHANNEL=""
TEXT=""
FILE=""

usage() {
  echo "Usage: wa_send.sh --channel briefs|alerts (--text MSG | --file PATH)" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel) CHANNEL="${2:-}"; shift 2 ;;
    --text) TEXT="${2:-}"; shift 2 ;;
    --file) FILE="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done

[[ -n "$CHANNEL" ]] || usage
[[ -n "$TEXT" || -n "$FILE" ]] || usage

case "$CHANNEL" in
  briefs) TO="${WA_BRIEFS_TO:?WA_BRIEFS_TO unset}" ;;
  alerts) TO="${WA_ALERTS_TO:?WA_ALERTS_TO unset}" ;;
  *) echo "unknown channel: $CHANNEL" >&2; exit 2 ;;
esac

if [[ -n "$FILE" ]]; then
  [[ -f "$FILE" ]] || { echo "missing file: $FILE" >&2; exit 1; }
  BODY="$(cat "$FILE")"
else
  BODY="$TEXT"
fi

# Prefer Hermes CLI send if present; fail closed otherwise.
if command -v hermes >/dev/null 2>&1; then
  # Exact subcommand may be `hermes send` / `hermes message` depending on Hermes version —
  # probe with `hermes --help` on the Mac and adjust one line here only.
  printf '%s\n' "$BODY" | hermes send --to "$TO" --stdin
  exit $?
fi

echo "hermes not on PATH; cannot deliver to $TO" >&2
exit 1
