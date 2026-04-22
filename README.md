# Local AI Stack (XDA-style) on Docker

Docker-first stack: **Ollama on the host**, **AgenticSeek** (UI + API + SearXNG + Redis), **Open WebUI**, and **n8n**.

## Table of contents

- [What this is](#what-this-is)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [CLI commands](#cli-commands)
- [Service endpoints](#service-endpoints)
- [Grounded review](#grounded-review)
- [Tests and CI](#tests-and-ci)
- [Model and hardware](#model-and-hardware)
- [Platform limitations](#platform-limitations)
- [Runtime checks](#runtime-checks)
- [Contributing](#contributing)
- [License](#license)

## What this is

This repository is a **cross-platform runner** (Python + shell/PowerShell helpers) around `docker compose` profiles so you can bring up the same local AI lab on macOS, Linux, or Windows (with caveats below).

## Prerequisites

- Docker Engine or Docker Desktop with `docker compose`
- Ollama on the host
- Python 3.10+ on the host

## Quick start

```bash
cp deploy/local-ai/.env.example deploy/local-ai/.env.local
python3 -m local_ai_stack up --env-file deploy/local-ai/.env.local
python3 -m local_ai_stack verify --env-file deploy/local-ai/.env.local
```

Stop everything:

```bash
python3 -m local_ai_stack down --env-file deploy/local-ai/.env.local
```

On **Windows PowerShell**, use the same `python3 -m local_ai_stack …` commands from the repo root, or the wrappers under `scripts/local-ai/*.ps1`. On Linux/macOS, optional wrappers live in `scripts/local-ai/*.sh`.

## CLI commands

| Command | Purpose |
|--------|---------|
| `python3 -m local_ai_stack up` | Start stack (`--env-file` required) |
| `python3 -m local_ai_stack verify` | Health checks |
| `python3 -m local_ai_stack down` | Tear down |
| `python3 -m local_ai_stack diff-preview --repo . --base <ref> --head <ref>` | Markdown diff triage preview (**no Ollama**); used in [PR diff preview](.github/workflows/pr-diff-preview.yml) |
| `python3 -m local_ai_stack review-diff --env-file … --repo . --base <ref> --head <ref>` | Full grounded LLM review over the diff (**requires Ollama** per `.env.local`) |

Bootstrap AgenticSeek once: `scripts/local-ai/bootstrap-agenticseek.sh` clones upstream into `.local/agenticseek` and points `config.ini` at Ollama.

## Service endpoints

| Service | URL |
|--------|-----|
| AgenticSeek UI | http://localhost:3010 |
| AgenticSeek API | http://localhost:7777/health |
| Open WebUI | http://localhost:3001 |
| n8n | http://localhost:5678 |
| SearXNG | http://localhost:8080 |

## Grounded review

GitHub “review this repo” flows use a **grounded** path: README plus a **heuristic, capped sample** of files (not a full audit). Output is **advisory** LLM text, not a merge gate.

- **Defaults:** up to **12** files, **~220 KB** total evidence, **80 KB** per file excerpt (see `deploy/local-ai/.env.example`).
- **Performance:** first answer is often **minutes**; repeat **identical** questions can hit a short **answer cache** (TTL + revision when SHA is known).
- **Two-pass map:** best-effort JSON over up to **8** files; failures fall back to single-pass with a clear **map status** in the scope note.
- **Tuning:** `GROUNDED_REVIEW_*` env vars (cache TTL, `ENABLE_TWO_PASS`, `STRICT_COVERAGE`, limits, `NUM_CTX`).

Private or unreachable GitHub repos fall back to a clone under **`WORK_DIR`** (mounted at `/opt/workspace` in the backend).

## Tests and CI

**Local:**

```bash
python3 -m unittest tests.test_grounded_review -v
```

**GitHub Actions:**

| Workflow | When | What it does |
|----------|------|----------------|
| [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | Push / PR to `main` | Runs `tests.test_grounded_review` on Ubuntu |
| [`.github/workflows/pr-diff-preview.yml`](.github/workflows/pr-diff-preview.yml) | Pull requests | Posts / updates a **diff preview** comment (no Ollama) |

## Model and hardware

| Model | Rough minimum | Typical use |
|-------|----------------|-------------|
| `qwen2.5:1.5b` | 8 GB RAM | Fast iteration |
| `qwen2.5:7b` | 16 GB RAM | Balanced code review |
| `qwen2.5:14b` | 32 GB RAM | Deeper analysis |

Default in `.env.example` is `qwen2.5:14b`. A practical middle ground for many machines: **7b** and **~32 GB RAM**.

## Platform limitations

- **macOS Apple Silicon:** primary tested path; AgenticSeek backend may use `linux/amd64` for browser automation; Ollama stays on the host (Metal).
- **Linux:** expect standard Docker; confirm `host.docker.internal` or add an explicit host gateway.
- **Windows:** best-effort with **Docker Desktop + WSL2**; set `WORK_DIR` to a path Docker can mount.
- **All:** GitHub API rate limits for remote snapshots; large repos leave most files unexamined by design.

## Runtime checks

`scripts/local-ai/verify.sh` runs checks from the stack (including n8n calling `GET $OLLAMA_BASE_URL/api/tags` expecting HTTP 200 and a model list).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
