# Mac n8n bus design

Date: 2026-08-23  
Status: draft for review  
Owner: Anoop Pamu / Github agent  

## Goal

Run **self-hosted n8n on the Mac** as a durable cron + webhook **bus**. Existing scripts and CLIs remain the source of truth. Agents keep judgment (research → discuss → implement, ship/defer/cut). n8n never auto-merges, never invents fixes, and never decides product honesty.

## Decisions locked

| Decision | Choice |
|---|---|
| Host | Mac (always-on relative to Hermes/Ollama/Mohawk) |
| Role | Approach B: bus only; scripts stay the truth |
| Delivery | WhatsApp |
| Chat layout | **Briefs** chat vs **Alerts** chat (split) |
| Agent loops | Unchanged (Tue–Fri Github routines + Tarka Monday) |

## Non-goals

- Replacing Hermes Agent cron internals with n8n node graphs that reimplement RSS/clustering.
- Replacing Cursor/Github agent ballots with n8n branches.
- Auto-remediation or auto-merge on CI failure.
- A second WhatsApp login owned by n8n (reuse the existing Hermes/WhatsApp Web delivery helper).
- Running a second n8n in Tarka compose for wave 1 (Tarka addons n8n stays optional later).

## Architecture

```text
GitHub webhooks ──┐
Schedule (HKT) ───┼──► Mac n8n ──► Execute / HTTP existing scripts ──► status
                  │         │
                  │         ├── success brief ──► WhatsApp Briefs
                  │         └── fail / red ─────► WhatsApp Alerts
                  │
Cursor agents ◄───┘ (judgment only; not in the hot path for wave 1)
```

### Components

1. **Mac n8n** — Docker Desktop or n8n desktop app. Timezone `Asia/Hong_Kong`. Data dir persisted on disk.
2. **Script surface** — shell/HTTP entrypoints that already exist (or thin wrappers):
   - `pamu512/octo-spork`: `python -m local_ai_stack nightly-audit` / `/octo/audit/run` + `/octo/audit/status` (existing workflow JSON).
   - `pamu512/hermes-fraud-intel`: `python3 scripts/dump_recent.py --write-brief` → `briefs/YYYY-MM-DD.md` or `.failed.md`.
   - `pamu512/hermes-finance-brief`: same pattern at 07:00.
   - `gh` / GitHub webhook for tarka `master` CI.
   - Optional: `curl localhost:11434/api/tags` for Ollama health.
3. **WhatsApp helper** — one callable path (CLI or local HTTP) used by Hermes today. n8n passes: chat id (Briefs vs Alerts), message body, optional file path. n8n does not scrape WhatsApp Web itself in wave 1 if a helper already exists.
4. **Secrets** — Mac keychain or n8n credentials store: `OCTO_AUDIT_API_KEY`, GitHub webhook secret, WhatsApp helper token if any. Never commit.

## Wave 1 workflows

Same shape as `deploy/n8n/octo_nightly_audit.workflow.json`: schedule → run → wait/status → route.

### 1. Octo-Spork Nightly Audit

- **Trigger:** cron (keep UTC 03:15 or map to HKT preference).
- **Action:** import existing workflow; POST audit run; wait; GET status.
- **Route:** green → silent; non-zero / unhealthy → Alerts with log path + one-line reason.
- **Out of scope:** starting full local-ai stack; `/octo-spork fix`.

### 2. Hermes fraud brief (07:30 HKT)

- **Trigger:** `30 7 * * *` Asia/Hong_Kong.
- **Action:** `cd` fraud-intel repo; ensure Ollama up (or fail fast); `python3 scripts/dump_recent.py --write-brief`.
- **Route:** `briefs/YYYY-MM-DD.md` exists → Briefs (short “fraud brief ready” + path or pasted TLDR); `.failed.md` or non-zero → Alerts.
- **Out of scope:** rewriting clustering in n8n nodes.

### 3. Hermes finance brief (07:00 HKT)

- Same as fraud, finance repo, Briefs chat (same Briefs destination unless later split by product).

### 4. Tarka CI red on `master`

- **Trigger:** GitHub webhook (`check_suite` / `workflow_run` failure) filtered to `pamu512/tarka` + `master`, or low-frequency `gh` poll if webhook setup lags.
- **Action:** parse failed run URL + name.
- **Route:** Alerts only. Message includes link. **No** auto branch, **no** cloud agent launch from n8n in wave 1.
- **Out of scope:** PR checks on feature branches (noise).

### 5. Ollama health (thin, optional)

- **Trigger:** once weekday morning before briefs (e.g. 06:50 HKT).
- **Action:** `curl -sf localhost:11434/api/tags`.
- **Route:** down → one Alerts message; up → silent. Dedupe so it does not spam all day.

## WhatsApp routing rules

| Event | Chat |
|---|---|
| Successful daily brief | Briefs |
| Brief failed / Ollama down before brief | Alerts |
| Octo audit unhealthy | Alerts |
| Tarka master CI red | Alerts |
| Green CI / green audit | (no message) |

User creates two WhatsApp chats (or groups) once and stores their ids in n8n env: `WA_BRIEFS_CHAT`, `WA_ALERTS_CHAT`.

## Failure and honesty

- Missing tool / missing binary → Alerts (fail closed), same spirit as octo-spork pre-push-scan.
- n8n must not treat “script skipped” as success.
- Alerts are actionable one-liners + link/path. No essay.
- Briefs chat stays low-noise: one message per successful brief.

## Wave 2 (explicitly later)

- Webhook from n8n → Cursor/Github agent “wake” for judgment items.
- Mohawk Threat Intel Desk FORCE SYNC when a stable CLI/IPC exists.
- Tarka compose health (decision-api / desk) if Mac can reach the compose host.
- GCP credit / Vertex probe for pack authoring.

## Success criteria

1. Mac n8n imports octo nightly workflow and can fire it against a running octo audit API or script.
2. Fraud brief lands in Briefs by ~07:35 HKT without Hermes cron dependency for that trigger (Hermes install cron may be disabled once n8n owns the schedule).
3. A forced Ollama-down test produces exactly one Alerts message, not a brief in Briefs.
4. A tarka master CI failure produces an Alerts link; agents still own the fix ballot.

## Open points (resolve at implement)

- Docker Desktop vs n8n desktop app on Mac.
- Exact WhatsApp helper invocation (reuse Hermes WhatsApp delivery vs `whatsapp-web-existing-tab` skill wrapper).
- Whether finance brief shares Briefs chat with fraud (default: yes for wave 1).

## References

- `pamu512/octo-spork` `deploy/n8n/octo_nightly_audit.workflow.json`
- `pamu512/octo-spork` `scripts/nightly_audit.sh`
- `pamu512/hermes-fraud-intel` README (07:30, `dump_recent.py --write-brief`)
- `pamu512/tarka` `infra/deploy/local-ai/docker-compose.addons.yml` (n8n service; not wave-1 host)
