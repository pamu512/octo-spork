# Mac n8n bus

## Install n8n

```bash
cp deploy/n8n/.env.example deploy/n8n/.env.local
# set N8N_ENCRYPTION_KEY (e.g. openssl rand -hex 32) and Mac paths
docker compose -f deploy/n8n/docker-compose.yml --env-file deploy/n8n/.env.local up -d
```

Open http://localhost:5678

## Workflows

All exports stay **inactive** (`active: false`) so import does not auto-fire.

| File | Schedule |
| ---- | -------- |
| `workflows/hermes_finance_brief.workflow.json` | `0 7 * * *` Asia/Hong_Kong |
| `workflows/hermes_fraud_brief.workflow.json` | `30 7 * * *` Asia/Hong_Kong |
| `workflows/ollama_health.workflow.json` | `50 6 * * 1-5` Asia/Hong_Kong |
| `workflows/tarka_ci_red.workflow.json` | `*/30 9-19 * * 1-5` Asia/Hong_Kong |
| `workflows/octo_nightly_audit.workflow.json` | `15 3 * * *` UTC (HTTP trigger/status; unchanged after move) |

### Import

1. Open http://localhost:5678 and create the owner account if needed.
2. Menu → **Import from File**.
3. Import each JSON under `deploy/n8n/workflows/`.
4. Leave every workflow **inactive** until Mac verify (Task 5).
5. n8n must see the same env names as `.env.example` (`OCTO_SPORK_ROOT`, `HERMES_*`, `WA_BRIEFS_TO`, `WA_ALERTS_TO`, …).

Execute Command nodes call `bash "$OCTO_SPORK_ROOT/deploy/n8n/scripts/<script>"` so host n8n works. Docker users: set `OCTO_SPORK_ROOT` to the checkout under the `/host/repos` mount (e.g. `/host/repos/Downloads/octo-spork` when `N8N_HOST_REPOS_ROOT=/Users/pamu`). n8n 2.x hides Execute Command unless `NODES_EXCLUDE=[]`.

Step 6 (disable the Hermes fraud-daily-brief cron so briefs are not double-sent) is **Mac ops after verify** — do not do it from this checkout.
