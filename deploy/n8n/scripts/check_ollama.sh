#!/usr/bin/env bash
set -euo pipefail
curl -sf --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null
echo "OK ollama"
