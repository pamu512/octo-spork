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

## Cross-platform runner (host OS support)

Primary command interface:

```bash
python -m local_ai_stack up --env-file deploy/local-ai/.env.local
python -m local_ai_stack verify --env-file deploy/local-ai/.env.local
python -m local_ai_stack down --env-file deploy/local-ai/.env.local
```

Diff-focused review (optional):

- `python -m local_ai_stack diff-preview --repo . --base <ref> --head <ref>` prints a markdown preview (no Ollama). GitHub Actions can run this on every PR; see `.github/workflows/pr-diff-preview.yml`.
- `python -m local_ai_stack review-diff --env-file deploy/local-ai/.env.local --repo . --base <ref> --head <ref>` runs the full grounded LLM review over files touched in that range (requires Ollama per your `.env.local`).

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

- **macOS Apple Silicon:** primary tested path. AgenticSeek backend images may use `linux/amd64` for browser automation reliability; Ollama stays on the host (Metal). Expect heavier CPU use from emulation.
- **Linux:** expected to work with standard Docker; confirm `host.docker.internal` (or an explicit extra host mapping) matches your Docker version. Mind case-sensitive filesystems vs bind mounts.
- **Windows:** best-effort — use **Docker Desktop + WSL2**, PowerShell wrappers, and a `WORK_DIR` on a drive Docker can mount; watch path separators and CRLF vs bind-mount quirks.
- **All OS:** GitHub API rate limits apply for remote repo snapshots; private repos need a local clone under `WORK_DIR` (mounted at `/opt/workspace` in the backend).

## Grounded review: what it is (and is not)

Grounded review is **priority-guided triage** over a **bounded sample** of files (heuristic scoring + caps), **not** a full-repo or formal security audit. It is **advisory** LLM output — useful for spotting likely hot spots, **not** a merge gate or proof that no critical bugs exist elsewhere.

**Default caps** (override via env, see `deploy/local-ai/.env.example`): up to **12** files, **~220k** bytes total evidence budget, **80k** bytes per file excerpt, README + excerpts fed to the model. Large repos will have many files **never examined**.

**Performance:** the **first** review of a repo/question is often **minutes** (LLM-bound). **Repeat identical** questions can be near-instant thanks to the answer cache until TTL expires. For quicker, shallower passes: set `GROUNDED_REVIEW_ENABLE_TWO_PASS=false` and lower `GROUNDED_REVIEW_MAX_FILES` (e.g. `6`).

**Caching:** snapshot cache is **time-TTL** only. **Answer** cache keys include the **revision SHA** when known so a new default-branch tip does not reuse stale narrative answers for the same prompt.

## Notes

- `scripts/local-ai/bootstrap-agenticseek.sh` clones upstream AgenticSeek into `.local/agenticseek` and updates `config.ini` to use Ollama.
- GitHub review requests fetch README + a **prioritized subset** of files (see scope note + coverage metadata in the response).
- Selection uses query tokens, path heuristics (CI/deploy/app/tests/docs), and optional “must-have” path patterns — easy to miss critical logic that does not match those hints.
- Two-pass mode runs a **best-effort** JSON “map” pass over up to **8** sampled files; malformed JSON falls back to single-pass with an explicit **map status** line in the scope note.
- Tunable: `GROUNDED_REVIEW_CACHE_TTL_SECONDS`, `GROUNDED_REVIEW_ANSWER_CACHE_TTL_SECONDS`, `GROUNDED_REVIEW_ENABLE_TWO_PASS`, `GROUNDED_REVIEW_STRICT_COVERAGE`, `GROUNDED_REVIEW_MAX_*`, `GROUNDED_REVIEW_NUM_CTX*`.
- PR **diff preview** (no Ollama) and optional **`review-diff`** (local Ollama) are documented above; they reinforce “changed files + triage” rather than full-repo guarantees.
- `.env.local` is git-ignored and auto-seeded for `SEARXNG_SECRET_KEY` / `N8N_ENCRYPTION_KEY` on first bootstrap.
- Default model is `qwen2.5:14b` in `deploy/local-ai/.env.local`.

## n8n → Ollama check

`scripts/local-ai/verify.sh` includes a runtime check that executes inside the n8n container:

- request: `GET $OLLAMA_BASE_URL/api/tags`
- expected: HTTP 200 with model list JSON
