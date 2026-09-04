---
name: octo-nightly-audit
description: Schedule Octo-Spork audits without editing repos. Use when the user asks Hermes to run nightly security scans, doctor checks, or unattended octo audits.
---

# Octo-Spork Nightly Audit (scheduler only)

You are an **outer scheduler**, not the remediation engine.

## Hard rules

1. **Do not** edit application source trees while `/octo-spork fix` or a LangGraph remediation run may be active.
2. **Do not** bypass Trivy/RescanLoop gates. Never commit "fixes" from chat.
3. Prefer invoking the Octo-Spork CLI on the host that owns the checkout.

## Allowed commands

From the octo-spork repo root:

```bash
python3 -m local_ai_stack nightly-audit --repo <target-repo-or-.>
# equivalent:
./scripts/nightly_audit.sh
```

Optional checks:

```bash
python3 -m local_ai_stack doctor --strict --repo <path>
python3 -m local_ai_stack status
python3 -m local_ai_stack pre-push-scan --repo <path>
```

## Remediation (human / webhook only)

Remediation is triggered by authorized GitHub PR comments:

- `/octo-spork analyze`
- `/octo-spork fix`

Engine selection (host env, not Hermes):

- `OCTO_REMEDIATION_ENGINE=claude` (default)
- `OCTO_REMEDIATION_ENGINE=langgraph`

If asked to "fix a CVE", tell the user to comment `/octo-spork fix` on the PR (or enable the webhook bot). You may only **schedule audits** and **report log paths** under `logs/nightly_audit_*.log`.

## HTTP delivery (preferred from Hermes / n8n)

With the webhook bot running (`uvicorn github_bot.app:app`, default port from your deploy):

```bash
# Trigger (requires OCTO_AUDIT_API_KEY)
curl -X POST -H "X-API-Key: $OCTO_AUDIT_API_KEY" \
  "http://127.0.0.1:8008/octo/audit/run?repo=."

# Status JSON: latest log tail + metrics.db summary + trigger state
curl -H "X-API-Key: $OCTO_AUDIT_API_KEY" \
  "http://127.0.0.1:8008/octo/audit/status"
```

From inside the n8n container use `http://host.docker.internal:8008` (see `deploy/n8n/workflows/octo_nightly_audit.workflow.json`).

## Delivery

After a run, summarize:

- `trigger.last_exit_code` / `trigger.last_error` from `/octo/audit/status`
- path + tail of the latest `logs/nightly_audit_*.log`
- recent `metrics` success rate / avg TTR
- whether follow-up `/octo-spork fix` is recommended (High/Critical findings only)
