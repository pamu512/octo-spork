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
4. Leave every workflow **inactive** until the Verification steps below pass on the Mac.
5. n8n must see the same env names as `.env.example` (`OCTO_SPORK_ROOT`, `HERMES_*`, `WA_BRIEFS_TO`, `WA_ALERTS_TO`, …).

Execute Command nodes call `bash "$OCTO_SPORK_ROOT/deploy/n8n/scripts/<script>"` so host n8n works. Docker users: set `OCTO_SPORK_ROOT` to the checkout under the `/host/repos` mount (e.g. `/host/repos/Downloads/octo-spork` when `N8N_HOST_REPOS_ROOT=/Users/pamu`). n8n 2.x hides Execute Command unless `NODES_EXCLUDE=[]`.

## Verification

Run these on the Mac only. This repo does not claim Mac E2E from a cloud agent. Keep all imported workflows **inactive** until each check below passes; execute a workflow once from the n8n UI (do not activate the cron).

Load env first:

```bash
set -a && source deploy/n8n/.env.local && set +a
```

### 1. Ollama health → Alerts only

```bash
# Ollama stopped
bash deploy/n8n/scripts/check_ollama.sh; echo exit:$?
# expect exit 1
```

Execute **Ollama Health** once in the n8n UI. Expect one message in WhatsApp Alerts (`WA_ALERTS_TO`), nothing in Briefs.

### 2. Fraud brief happy path → Briefs

```bash
bash deploy/n8n/scripts/run_hermes_brief.sh fraud
# expect: OK briefs/$(date +%F).md
```

Execute **Hermes Fraud Brief** once. Expect WhatsApp Briefs (`WA_BRIEFS_TO`) to get the brief (or path); Alerts stay quiet.

### 3. Fraud brief fail path → Alerts only

Force a fail (stop Ollama, or temporarily move `briefs/$(date +%F).md` aside so the script cannot treat the day as success). Re-run `run_hermes_brief.sh fraud` (expect non-zero / `briefs/DATE.failed.md`). Execute the fraud workflow once. Expect Alerts (`fraud brief failed`), **nothing new** in Briefs.

### 4. Tarka CI dry-run (green = silence)

```bash
bash deploy/n8n/scripts/check_tarka_master_ci.sh; echo exit:$?
```

When `pamu512/tarka` `master` is green (or in-progress / empty conclusion), expect exit 0 and **no WhatsApp**. Do not activate the CI workflow for this check.

### 5. Confirm `hermes send` on the Mac

```bash
hermes --help
# one probe (Alerts):
./deploy/n8n/scripts/wa_send.sh --channel alerts --text "n8n bus probe $(date -Iseconds)"
```

Confirmed Mac CLI: `hermes send --to …` with a stdin pipe (no `--stdin` flag). `--file PATH` is used when `wa_send.sh --file` is set. `--stdin` is unrecognized on this Hermes.

### 6. Activate and retire the duplicate Hermes cron

Leave workflows inactive until 1–5 pass. After two successful n8n fraud mornings, disable the Hermes `fraud-daily-brief` cron so briefs are not double-sent (Task 6 — Mac ops, not this checkout):

```bash
hermes cron list
# disable fraud-daily-brief only after two good n8n mornings
```
