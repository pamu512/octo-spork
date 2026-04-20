# Local AI Stack (XDA-style) on Docker

This repository replicates the XDA stack using:

- Ollama on host (native runtime on macOS, Linux, or Windows)
- AgenticSeek (frontend + backend + SearXNG + Redis) in Docker
- Open WebUI in Docker
- n8n in Docker

## Prerequisites

- Docker Engine/Desktop with `docker compose`
- Ollama installed on host
- Python 3.10+ on host

## Cross-platform runner

Primary command interface:

```bash
python -m local_ai_stack up --env-file deploy/local-ai/.env.local
python -m local_ai_stack verify --env-file deploy/local-ai/.env.local
python -m local_ai_stack down --env-file deploy/local-ai/.env.local
```

Wrappers:

- Linux/macOS: `scripts/local-ai/*.sh`
- Windows PowerShell: `scripts/local-ai/*.ps1`

Example on PowerShell:

```powershell
python -m local_ai_stack up --env-file deploy/local-ai/.env.local
python -m local_ai_stack verify --env-file deploy/local-ai/.env.local
```

## Quick start

```bash
cp deploy/local-ai/.env.example deploy/local-ai/.env.local
python -m local_ai_stack up --env-file deploy/local-ai/.env.local
python -m local_ai_stack verify --env-file deploy/local-ai/.env.local
```

Endpoints:

- AgenticSeek UI: `http://localhost:3010`
- AgenticSeek API health: `http://localhost:7777/health`
- Open WebUI: `http://localhost:3001`
- n8n: `http://localhost:5678`
- SearXNG: `http://localhost:8080`

Stop stack:

```bash
python -m local_ai_stack down --env-file deploy/local-ai/.env.local
```

## Model and hardware guidance

Model profiles:

- `qwen2.5:1.5b`
  - minimum: 8 GB RAM, 30 GB free disk
  - recommended: 16 GB RAM for smoother multitool use
  - use case: quick chat, low-cost iterations
- `qwen2.5:7b`
  - minimum: 16 GB RAM, 50 GB free disk
  - recommended: 32 GB RAM
  - use case: balanced quality/performance for code review
- `qwen2.5:14b`
  - minimum: 32 GB RAM, 70 GB free disk
  - recommended: 48-64 GB RAM (or strong GPU path)
  - use case: deeper analysis and hardening recommendations

Recommended balance for most teams:

- model: `qwen2.5:7b`
- host RAM: 32 GB
- free disk: 100 GB
- grounded review path enabled

## Platform limitations

- Windows:
  - current flow is best from PowerShell + Docker Desktop; WSL2 is recommended for best compatibility
  - ensure `WORK_DIR` points to an existing local path, and Docker Desktop has access to that drive
- Linux:
  - works natively; verify `host.docker.internal` support in your Docker version (or add explicit host gateway mapping if needed)
- macOS Apple Silicon:
  - backend defaults to `linux/amd64` compatibility mode for AgenticSeek browser stack reliability
  - Ollama still runs natively on host (Metal), but backend emulation reduces throughput
- All OS:
  - grounded review analyzes a prioritized subset of files, not every file in very large repositories
  - GitHub API rate limits apply for repeated external repo reviews
  - private repos require local clone fallback via `WORK_DIR` mount

## Notes

- `scripts/local-ai/bootstrap-agenticseek.sh` clones upstream AgenticSeek into `.local/agenticseek` and updates `config.ini` to use Ollama.
- GitHub repository review requests are handled by a grounded review path that fetches README + selected repo files via GitHub APIs and summarizes them using your local Ollama model.
- If a GitHub repo is private/unreachable, the grounded path falls back to a local clone under `WORK_DIR` (mounted to `/opt/workspace` in the backend container).
- `.env.local` is ignored by git and auto-seeded with random values for `SEARXNG_SECRET_KEY` and `N8N_ENCRYPTION_KEY` on first bootstrap.
- Default model is `qwen2.5:14b` in `deploy/local-ai/.env.local`.

## n8n → Ollama check

`scripts/local-ai/verify.sh` includes a runtime check that executes inside the n8n container:

- request: `GET $OLLAMA_BASE_URL/api/tags`
- expected: HTTP 200 with model list JSON
