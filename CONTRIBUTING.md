# Contributing to Octo-Spork

Thanks for helping improve this stack. Small, focused changes are easier to review than large refactors.

## Before you open a PR

1. **Run the test suite** (see [README — Tests and CI](README.md#tests-and-ci)).
2. **Do not commit secrets** — keep `deploy/local-ai/.env.local` out of git (it is ignored). Use `.env.example` as the template.
3. **Match existing style** in Python and shell scripts (formatting, naming, minimal comments).

## Grounded review overlay

Changes to `overlays/agenticseek/sources/grounded_review.py` affect AgenticSeek behavior when that overlay is applied. Extend `tests/test_grounded_review.py` when you change selection, caching, or API-facing behavior.

## CI and workflows

- **`.github/workflows/ci.yml`** — Python unit tests on push and pull requests.
- **`.github/workflows/pr-diff-preview.yml`** — posts a no-Ollama diff triage comment on PRs; requires `pull-requests: write` for the `GITHUB_TOKEN`.

If you add a new workflow, document it in the README [Tests and CI](README.md#tests-and-ci) section.

## Questions

Open a [GitHub issue](https://github.com/pamu512/octo-spork/issues) for design questions or bugs.
