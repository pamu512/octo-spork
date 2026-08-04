# 🐙🍴 Octo-Spork

**The Sovereign, Local-First AI Stack for Repo Hardening & Agentic Remediation.**

Octo-Spork orchestrates Ollama, AgenticSeek, SearXNG, and Claude Code on local hardware for private, evidence-grounded reviews and remediation.

---

## Quick Start

### Prerequisites

* Docker & Docker Compose v2
* Ollama on port **11434**
* **pytest**, **trivy**, and **bun** on `PATH` (for scans / Claude remediation)
* 16GB+ VRAM recommended for large models

### Install

```bash
git clone https://github.com/pamu512/octo-spork.git
cd octo-spork
cp deploy/local-ai/.env.example deploy/local-ai/.env.local
pip install -r requirements.txt
```

### Dependency check (PATH)

```bash
python src/runner/local_ai_stack.py doctor   # pytest / trivy / bun only
python -m local_ai_stack doctor             # full stack doctor
```

### Launch

```bash
python -m local_ai_stack bootstrap   # first time
python -m local_ai_stack up
```

Common: `status`, `verify`, `down`, `doctor --fix`.

---

## Remediation engines

`/octo-spork fix` clones the PR head and runs a remediation agent, then RescanLoop (Trivy) when enabled.

| `OCTO_REMEDIATION_ENGINE` | Behavior |
| ------------------------- | -------- |
| `claude` (default) | Docker Claude Code agent (`local-ai-claude-agent`) |
| `langgraph` | In-process LangGraph + Ollama (`src/agent/graph/graph.py`) with terminal / file_write tools, format enforcer, 5-minute / 3-failure circuit |

Verified Chroma patterns are injected into `OCTO_REMEDIATION_BRIEF.md` when available (`OCTO_FIX_MEMORY_BRIEF=0` to disable).

---

## Unattended audit

For cron / n8n / Hermes-as-scheduler (do **not** let Hermes edit while `/octo-spork fix` runs):

```bash
python -m local_ai_stack nightly-audit --repo .
# or: ./scripts/nightly_audit.sh
```

Runs doctor (strict), status, and pre-push-scan; logs under `logs/nightly_audit_*.log`.

Verified remediations are upserted into the Chroma ledger (`upsert_verified_pattern`) when RescanLoop passes; later fixes pull them via `query_verified_patterns` (with optional fallback to review memory).

### Schedulers

| Tool | Asset |
| ---- | ----- |
| Host cron | `./scripts/nightly_audit.sh` or `python -m local_ai_stack nightly-audit` |
| n8n | Import `deploy/n8n/octo_nightly_audit.workflow.json` — HTTP trigger/status against the webhook bot (`OCTO_AUDIT_API_KEY`, `OCTO_AUDIT_API_BASE`) |
| Hermes | Skill at `deploy/hermes/octo-nightly-audit/SKILL.md` — **schedule only**, never edit during `/octo-spork fix` |

Audit HTTP (bot must be running):

- `POST /octo/audit/run` — start nightly-audit (API key required)
- `GET /octo/audit/status` — latest log tail + `metrics.db` summary

Latency rows also land in `logs/metrics.db` via `observability.latency.record_metric`. n8n mounts the repo read-only at `/opt/octo-spork`.

---

## Project structure

```text
octo-spork/
├── deploy/local-ai/         # Compose + env
├── local_ai_stack/          # python -m local_ai_stack
├── scripts/nightly_audit.sh # Scheduled audit hook
├── src/
│   ├── agent/               # LangGraph remediation graph + tools
│   ├── github_bot/          # Webhooks, /octo-spork fix|analyze
│   ├── remediation/         # RescanLoop, sandbox, verifier
│   ├── memory/              # Verified Chroma ledger queries
│   ├── runner/              # ChatOllama bind + thin PATH doctor
│   └── observability/       # Tracing, vector memory, latency
└── logs/
```

---

## Contributing

Forks and local modifications are encouraged. Open a PR for MCP integrations or VRAM scheduling improvements.
