#!/usr/bin/env python3
"""GitHub PR commenter: fetch PR diff via API, run local grounded review, post GFM comment.

Authentication: set ``GITHUB_TOKEN`` or ``GH_TOKEN`` in the environment or in a ``.env`` file
(loaded automatically from the current directory and optionally ``--env-file``).

Ollama settings follow ``local_ai_stack`` conventions: ``OLLAMA_LOCAL_URL`` / ``OLLAMA_BASE_URL``,
``OLLAMA_MODEL``. For unattended PR reviews, ``performance_profile.json`` (from
``python -m local_ai_stack benchmark-models``) selects ``most_stable_model`` unless
``OCTO_BACKGROUND_REVIEW_MODEL`` is set or ``OCTO_PERF_PROFILE_DISABLE=1``.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]

try:
    from github import Auth, Github, GithubException
except ImportError as exc:
    raise SystemExit(
        "PyGithub is required. Install with: pip install PyGithub\n"
        f"Import error: {exc}"
    ) from exc

ROOT = Path(__file__).resolve().parent
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OVERLAY_SOURCE = ROOT / "overlays" / "agenticseek" / "sources" / "grounded_review.py"

PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<num>\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)

GITHUB_COMMENT_MAX_CHARS = 62_000
DIFF_EXCERPT_MAX = 12_000
RATE_LIMIT_MIN_REMAINING = 40


def _parse_env_simple(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        env[key] = value
    return env


def load_environment(*, env_file: Path | None) -> None:
    if load_dotenv is not None:
        load_dotenv(Path.cwd() / ".env", override=False)
        if env_file is not None:
            load_dotenv(env_file, override=True)
    else:
        for key, val in _parse_env_simple(Path.cwd() / ".env").items():
            os.environ.setdefault(key, val)
        if env_file is not None:
            for key, val in _parse_env_simple(env_file).items():
                os.environ[key] = val


def parse_pr_url(url: str) -> tuple[str, str, int]:
    m = PR_URL_RE.match(url.strip())
    if not m:
        raise ValueError(
            f"Not a GitHub PR URL: {url!r} "
            "(expected https://github.com/owner/repo/pull/123)"
        )
    return m.group("owner"), m.group("repo").removesuffix(".git"), int(m.group("num"))


def _load_grounded_review():
    spec = importlib.util.spec_from_file_location("grounded_review", OVERLAY_SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load grounded_review from {OVERLAY_SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_for_rate_limit(github: Github, *, min_remaining: int = RATE_LIMIT_MIN_REMAINING) -> None:
    """Sleep until core REST quota is above ``min_remaining`` if needed."""
    try:
        rl = github.get_rate_limit()
        core = rl.core
        if core.remaining > min_remaining:
            return
        reset_at = core.reset
        if isinstance(reset_at, datetime):
            if reset_at.tzinfo is None:
                reset_utc = reset_at.replace(tzinfo=timezone.utc)
            else:
                reset_utc = reset_at.astimezone(timezone.utc)
            wait_s = max(0.0, (reset_utc - datetime.now(timezone.utc)).total_seconds()) + 2.0
        else:
            wait_s = 60.0
        wait_s = min(wait_s, 3600.0)
        sys.stderr.write(
            f"[github_integrator] Rate limit low ({core.remaining} remaining); "
            f"sleeping ~{wait_s:.0f}s until reset…\n"
        )
        time.sleep(wait_s)
    except (GithubException, AttributeError, OSError, TypeError, ValueError) as exc:
        sys.stderr.write(f"[github_integrator] Rate limit check skipped: {exc}\n")


def fetch_pr_diff_http(api_pull_url: str, token: str, *, timeout: int = 120) -> str:
    """Return unified diff text for the PR (GitHub ``Accept: application/vnd.github.diff``)."""
    req = urllib.request.Request(
        api_pull_url,
        headers={
            "Accept": "application/vnd.github.diff",
            "Authorization": f"Bearer {token}",
            "User-Agent": "octo-spork-github-integrator",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000] if exc.fp else ""
        raise RuntimeError(f"GitHub diff HTTP {exc.code}: {body}") from exc


def _git(args: list[str], *, cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def sync_pull_refs(repo_path: Path, pull_number: int, base_sha: str, head_sha: str) -> None:
    """Fetch PR head and ensure base/head SHAs exist for ``git diff`` / grounded review."""
    if not (repo_path / ".git").is_dir():
        raise NotADirectoryError(f"Not a git repository: {repo_path}")

    fetch_origin = _git(["fetch", "origin"], cwd=repo_path)
    if fetch_origin.returncode != 0:
        sys.stderr.write(
            f"[github_integrator] warning: git fetch origin failed: {fetch_origin.stderr.strip()}\n"
        )

    pr_ref = f"refs/pull/{pull_number}/head"
    local_branch = f"octo-spork-pr-{pull_number}"
    pr_fetch = _git(
        ["fetch", "origin", f"+{pr_ref}:refs/heads/{local_branch}"],
        cwd=repo_path,
    )
    if pr_fetch.returncode != 0:
        raise RuntimeError(
            f"Could not fetch PR head ({pr_ref}). Ensure this is the GitHub repository "
            f"that hosts the PR and that you have access.\n{pr_fetch.stderr.strip()}"
        )

    for sha, label in ((base_sha, "base"), (head_sha, "head")):
        verify = _git(["cat-file", "-t", sha], cwd=repo_path)
        if verify.returncode != 0:
            shallow = _git(["fetch", "--depth", "500", "origin", sha], cwd=repo_path)
            if shallow.returncode != 0:
                full = _git(["fetch", "origin", sha], cwd=repo_path)
                if full.returncode != 0:
                    sys.stderr.write(
                        f"[github_integrator] warning: could not fetch {label} sha {sha}: "
                        f"{full.stderr.strip()}\n"
                    )


def _grounded_receipts_markdown(paths: list[str] | None) -> str:
    """Immutable receipt list for integrator prose reviews (API file paths)."""
    lines = ["## Grounded Receipts", ""]
    if paths:
        uniq = sorted({p.strip() for p in paths if isinstance(p, str) and p.strip()})
        lines.append("_Evidence for this review was scoped to these PR paths (GitHub API snapshot):_")
        lines.append("")
        for p in uniq:
            lines.append(f"- **Analyzed from:** `{p}`")
        lines.append("")
        return "\n".join(lines)
    lines.append(
        "_This review used the unified PR diff and repository snapshots passed to the grounded "
        "review pipeline. For per-suggestion receipts with strict JSON validation, use "
        "`ReviewFormatter` output._"
    )
    lines.append("")
    return "\n".join(lines)


def format_evidence_comment(
    *,
    html_url: str,
    title: str,
    base_label: str,
    head_label: str,
    additions: int,
    deletions: int,
    changed_files: int,
    review_markdown: str,
    diff_excerpt: str,
    model: str,
    rate_remaining: int | None,
    trivy_markdown: str | None = None,
    codeql_markdown: str | None = None,
    grounded_receipt_paths: list[str] | None = None,
    dependency_graph_markdown: str | None = None,
    risk_analysis_markdown: str | None = None,
    knowledge_sync_markdown: str | None = None,
) -> str:
    """GitHub-flavored markdown for an issue/PR comment."""
    safe_review = review_markdown.strip()
    if len(safe_review) > 50_000:
        safe_review = safe_review[:49_500] + "\n\n_(Review body truncated for GitHub comment size.)_"

    diff_part = diff_excerpt.strip()
    if len(diff_part) > DIFF_EXCERPT_MAX:
        diff_part = diff_part[:DIFF_EXCERPT_MAX] + "\n\n_(Diff excerpt truncated.)_"

    rl = f"{rate_remaining}" if rate_remaining is not None else "n/a"
    diff_html = html.escape(diff_part, quote=False)
    trivy_tail = ""
    if trivy_markdown and str(trivy_markdown).strip():
        trivy_tail = "\n\n" + str(trivy_markdown).strip() + "\n"

    codeql_tail = ""
    if codeql_markdown and str(codeql_markdown).strip():
        codeql_tail = "\n\n" + str(codeql_markdown).strip() + "\n"

    ks_tail = ""
    if knowledge_sync_markdown and str(knowledge_sync_markdown).strip():
        ks_tail = "\n\n" + str(knowledge_sync_markdown).strip() + "\n"

    receipts_md = _grounded_receipts_markdown(grounded_receipt_paths)

    dep_insert = ""
    if dependency_graph_markdown and str(dependency_graph_markdown).strip():
        dep_insert = "\n" + str(dependency_graph_markdown).strip() + "\n"

    risk_insert = ""
    if risk_analysis_markdown and str(risk_analysis_markdown).strip():
        risk_insert = "\n" + str(risk_analysis_markdown).strip() + "\n"

    return f"""## Evidence-first grounded review (automated)

**PR:** [{title}]({html_url})

**Compare:** `{base_label}` … `{head_label}` · **Files:** {changed_files} · **+{additions} / -{deletions}**

_Model:_ `{model}` · _GitHub REST core quota (snapshot before posting comment):_ **{rl}**
{dep_insert}{risk_insert}
---

{safe_review}

{receipts_md}{trivy_tail}{codeql_tail}{ks_tail}
---

<details>
<summary>📎 Unified diff excerpt (API; truncated)</summary>

<pre>{diff_html}</pre>

</details>

<sub>Generated by `github_integrator.py` — scanner blocks (Trivy/CodeQL/deps) reflect the local clone at review time.</sub>
"""


@dataclass
class IntegratorConfig:
    token: str
    ollama_url: str
    model: str
    query: str
    repo_path: Path
    dry_run: bool


class GitHubIntegrator:
    """Coordinates PyGithub, local git, grounded review, and PR comments."""

    def __init__(self, token: str) -> None:
        if not token or not token.strip():
            raise ValueError(
                "GitHub token missing. Set GITHUB_TOKEN or GH_TOKEN in the environment or .env file."
            )
        auth = Auth.Token(token.strip())
        self._github = Github(auth=auth, timeout=60)
        self._token = token.strip()

    def get_pull(self, owner: str, repo: str, number: int):
        wait_for_rate_limit(self._github)
        repository = self._github.get_repo(f"{owner}/{repo}", lazy=False)
        return repository.get_pull(number)

    def run(
        self,
        pr_url: str,
        cfg: IntegratorConfig,
    ) -> int:
        from github_bot.octo_spork_checks import OctoSporkAnalysisSession, scan_outputs_indicate_critical
        from github_bot.secret_scan import format_critical_alert_comment, scan_diff_text

        owner, repo_name, number = parse_pr_url(pr_url)
        wait_for_rate_limit(self._github)

        pull = self.get_pull(owner, repo_name, number)
        api_url = pull.url
        html_url = pull.html_url
        title = pull.title or f"PR #{number}"

        sys.stderr.write(f"[github_integrator] Fetching diff via API…\n")
        diff_text = fetch_pr_diff_http(api_url, self._token)

        base_sha = pull.base.sha
        head_sha = pull.head.sha

        repo_api = self._github.get_repo(f"{owner}/{repo_name}", lazy=False)
        check_session = OctoSporkAnalysisSession(
            repo_api,
            head_sha,
            dry_run=cfg.dry_run,
            before_api_call=lambda: wait_for_rate_limit(self._github),
        )
        check_session.start()

        components: dict[str, str] = {}
        check_exc: BaseException | None = None

        try:
            secret_findings = scan_diff_text(diff_text)
            components["Secret scan"] = "no credential patterns matched"
            if secret_findings:
                check_session.mark_critical()
                try:
                    from sovereign_intel.store import record_critical_pattern_hits

                    record_critical_pattern_hits(
                        cfg.repo_path, [f.pattern_name for f in secret_findings]
                    )
                except Exception:
                    pass
                components["Secret scan"] = (
                    f"{len(secret_findings)} credential pattern(s) matched (**Critical**)"
                )
                components["Grounded AI review"] = "skipped (secrets)"
                components["Trivy SARIF"] = "skipped"
                components["CodeQL"] = "skipped"
                alert_body = format_critical_alert_comment(
                    html_url=html_url,
                    title=title,
                    findings=secret_findings,
                    skipped_ai=True,
                )
                sys.stderr.write(
                    f"[github_integrator] Secret scan: {len(secret_findings)} pattern hit(s); "
                    "posting Critical Alert + REQUEST_CHANGES (skipping AI).\n"
                )
                if cfg.dry_run:
                    print(alert_body)
                    return 0

                wait_for_rate_limit(self._github)
                repo = pull.base.repo
                head_commit = repo.get_commit(head_sha)
                review_posted = False
                for attempt in range(3):
                    try:
                        wait_for_rate_limit(self._github)
                        pull.create_review(
                            commit=head_commit,
                            body=alert_body,
                            event="REQUEST_CHANGES",
                        )
                        review_posted = True
                        break
                    except GithubException as exc:
                        status = getattr(exc, "status", None)
                        if status == 429 and attempt < 2:
                            wait_for_rate_limit(self._github, min_remaining=80)
                            continue
                        sys.stderr.write(
                            f"[github_integrator] create_review REQUEST_CHANGES failed ({exc}); "
                            "falling back to issue comment.\n"
                        )
                        break

                if not review_posted:
                    comment_posted = False
                    for attempt in range(3):
                        try:
                            wait_for_rate_limit(self._github)
                            pull.create_issue_comment(alert_body)
                            comment_posted = True
                            break
                        except GithubException as exc:
                            if getattr(exc, "status", None) == 429 and attempt < 2:
                                wait_for_rate_limit(self._github, min_remaining=80)
                                continue
                            raise
                    if not comment_posted:
                        raise RuntimeError(
                            "Could not post secret alert (review and issue comment failed)."
                        )

                sys.stderr.write(
                    "[github_integrator] Critical alert posted; skipping grounded review and scanners.\n"
                )
                return 0

            sys.stderr.write(
                f"[github_integrator] Syncing git refs ({base_sha[:7]}… {head_sha[:7]}…)…\n"
            )
            sync_pull_refs(cfg.repo_path, number, base_sha, head_sha)

            gr = _load_grounded_review()
            from github_bot.ollama_preflight import verify_ollama_preflight

            sys.stderr.write("[github_integrator] Ollama pre-flight check…\n")
            ok_pf, pf_msg = verify_ollama_preflight(cfg.ollama_url, cfg.model)
            if ok_pf:
                components["Ollama pre-flight"] = "ok"
                sys.stderr.write("[github_integrator] Running grounded diff review (local)…\n")
                result = gr.grounded_local_diff_review(
                    cfg.query,
                    cfg.model,
                    cfg.ollama_url,
                    cfg.repo_path,
                    base_sha,
                    head_sha,
                )
                if not result.get("success"):
                    raise RuntimeError(result.get("answer", "Grounded review failed"))

                review_md = str(result.get("answer", ""))
                components["Grounded AI review"] = "completed"
            else:
                check_session.mark_system_offline(pf_msg)
                components["Ollama pre-flight"] = pf_msg
                components["Grounded AI review"] = "skipped (System Offline)"
                sys.stderr.write(
                    f"[github_integrator] Pre-flight failed; skipping LLM review: {pf_msg}\n"
                )
                review_md = (
                    "## Review delayed — **System Offline**\n\n"
                    f"{pf_msg}\n\n"
                    "_Trivy and CodeQL sections below still run when possible._"
                )

            from github_bot.review_refiner import (
                maybe_refine_ai_section_for_integrator,
                refinement_enabled,
            )

            if refinement_enabled() and "Review delayed — **System Offline**" not in review_md:
                diff_excerpt = diff_text[:20000]
                if len(diff_text) > 20000:
                    diff_excerpt += "\n\n_(diff truncated for refiner context)_"
                pr_ctx_for_refine = (
                    f"Title: {title}\nURL: {html_url}\n\n"
                    f"### Unified diff (truncated)\n\n```diff\n{diff_excerpt}\n```\n"
                )
                review_md = maybe_refine_ai_section_for_integrator(
                    review_md,
                    pr_context=pr_ctx_for_refine,
                    repo_path=cfg.repo_path,
                )

            changed_paths: list[str] = []
            try:
                from github_bot.pr_security_scans import iter_pull_changed_filenames

                changed_paths = iter_pull_changed_filenames(pull)
            except Exception as exc:
                sys.stderr.write(f"[github_integrator] PR changed-files list skipped: {exc}\n")

            trivy_md: str | None = None
            try:
                from github_bot.pr_security_scans import run_integrator_trivy_scan

                sys.stderr.write(
                    "[github_integrator] Trivy SARIF (ScanCache + incremental paths when enabled)…\n"
                )
                trivy_md = run_integrator_trivy_scan(
                    local_repo=cfg.repo_path,
                    clone_url=pull.head.repo.clone_url,
                    branch=pull.head.ref,
                    token=self._token,
                    base_sha=base_sha,
                    head_sha=head_sha,
                    changed_paths=changed_paths,
                    repo_full_name=f"{owner}/{repo_name}",
                    pr_html_url=str(html_url),
                )
                components["Trivy SARIF"] = (
                    "completed" if not (trivy_md or "").startswith("_Trivy") else "warn/skip"
                )
            except Exception as exc:
                sys.stderr.write(f"[github_integrator] Trivy SARIF section skipped: {exc}\n")
                trivy_md = f"_Trivy SARIF section unavailable: `{exc}`_\n"
                components["Trivy SARIF"] = f"error: {exc}"

            codeql_md: str | None = None
            try:
                from github_bot.pr_security_scans import run_integrator_codeql_scan

                sys.stderr.write(
                    "[github_integrator] CodeQL (ScanCache by head SHA when enabled)…\n"
                )
                codeql_md = run_integrator_codeql_scan(
                    local_repo=cfg.repo_path,
                    clone_url=pull.head.repo.clone_url,
                    branch=pull.head.ref,
                    token=self._token,
                    head_sha=head_sha,
                    repo_full_name=f"{owner}/{repo_name}",
                    pr_html_url=str(html_url),
                )
                components["CodeQL"] = (
                    "completed" if not (codeql_md or "").startswith("_CodeQL") else "warn/skip"
                )
            except Exception as exc:
                sys.stderr.write(f"[github_integrator] CodeQL section skipped: {exc}\n")
                codeql_md = f"_CodeQL section unavailable: `{exc}`_\n"
                components["CodeQL"] = f"error: {exc}"

            if scan_outputs_indicate_critical(trivy_md, codeql_md):
                check_session.mark_critical()
                components["Critical-tier (scanners)"] = (
                    "Trivy and/or CodeQL reported Critical-tier rows"
                )
            else:
                components["Critical-tier (scanners)"] = "none detected in summaries"

            wait_for_rate_limit(self._github)
            rate_remaining: int | None = None
            try:
                rate_remaining = self._github.get_rate_limit().core.remaining
            except (GithubException, AttributeError, TypeError):
                pass

            receipt_paths: list[str] = []
            try:
                for f in pull.get_files():
                    fn = getattr(f, "filename", None)
                    if isinstance(fn, str) and fn.strip():
                        receipt_paths.append(fn.strip())
            except (GithubException, AttributeError, TypeError, OSError) as exc:
                sys.stderr.write(
                    f"[github_integrator] Could not list PR files for grounded receipts: {exc}\n"
                )

            dep_graph_md: str | None = None
            try:
                from repo_graph.dot_svg import write_dependency_graph_svg

                sys.stderr.write("[github_integrator] Import dependency graph (Graphviz SVG)…\n")
                _, dep_graph_md = write_dependency_graph_svg(cfg.repo_path, head_sha[:40])
            except Exception as exc:
                sys.stderr.write(f"[github_integrator] Dependency graph section skipped: {exc}\n")
                dep_graph_md = (
                    f"\n\n### Import dependency graph\n\n"
                    f"_Section skipped: `{exc}`_\n"
                )

            risk_md: str | None = None
            if "Review delayed — **System Offline**" not in review_md:
                try:
                    from github_bot.negative_constraint import build_negative_constraint_section

                    sys.stderr.write("[github_integrator] Negative constraint risk analysis…\n")
                    nc_ctx = f"Title: {title}\nURL: {html_url}\nCompare: {base_sha[:7]}…{head_sha[:7]}\n"
                    risk_md = build_negative_constraint_section(
                        review_md,
                        pr_context=nc_ctx,
                        ollama_base_url=cfg.ollama_url,
                        model=os.environ.get("OCTO_NEGATIVE_CONSTRAINT_MODEL") or cfg.model,
                    )
                    if risk_md and risk_md.strip():
                        components["Negative constraint risk"] = "completed"
                    else:
                        components["Negative constraint risk"] = "skipped"
                except Exception as exc:
                    sys.stderr.write(f"[github_integrator] Negative constraint skipped: {exc}\n")
                    components["Negative constraint risk"] = f"error: {exc}"
                    risk_md = (
                        f"\n\n### Negative constraint — risk analysis\n\n"
                        f"_Section skipped: `{exc}`_\n"
                    )
            else:
                components["Negative constraint risk"] = "skipped (system offline)"

            ks_proposal = ""
            try:
                from github_bot.knowledge_sync import maybe_knowledge_sync_proposal_for_scanners

                ks_proposal = maybe_knowledge_sync_proposal_for_scanners(
                    cfg.repo_path, trivy_md, codeql_md
                )
            except Exception as exc:
                sys.stderr.write(f"[github_integrator] KnowledgeSync proposal skipped: {exc}\n")

            body = format_evidence_comment(
                html_url=html_url,
                title=title,
                base_label=pull.base.ref or base_sha[:7],
                head_label=pull.head.ref or head_sha[:7],
                additions=int(pull.additions or 0),
                deletions=int(pull.deletions or 0),
                changed_files=int(pull.changed_files or 0),
                review_markdown=review_md,
                diff_excerpt=diff_text,
                model=cfg.model,
                rate_remaining=rate_remaining,
                trivy_markdown=trivy_md,
                codeql_markdown=codeql_md,
                grounded_receipt_paths=receipt_paths or None,
                dependency_graph_markdown=dep_graph_md,
                risk_analysis_markdown=risk_md,
                knowledge_sync_markdown=ks_proposal or None,
            )

            if len(body) > GITHUB_COMMENT_MAX_CHARS:
                body = (
                    body[: GITHUB_COMMENT_MAX_CHARS - 200]
                    + "\n\n_(Comment truncated to GitHub size limit.)_"
                )

            if cfg.dry_run:
                print(body)
                return 0

            posted = False
            for attempt in range(3):
                try:
                    wait_for_rate_limit(self._github)
                    pull.create_issue_comment(body)
                    posted = True
                    break
                except GithubException as exc:
                    status = getattr(exc, "status", None)
                    if status in (403, 429) and attempt < 2:
                        wait_for_rate_limit(self._github, min_remaining=80)
                        continue
                    raise
            if not posted:
                raise RuntimeError("Could not post PR comment after retries (rate limit or permissions).")

            try:
                rate_remaining = self._github.get_rate_limit().core.remaining
            except (GithubException, AttributeError, TypeError):
                rate_remaining = None
            sys.stderr.write(
                f"[github_integrator] Comment posted. API core rate-limit remaining ≈ {rate_remaining}\n"
            )
            return 0

        except BaseException as exc:
            check_exc = exc
            raise
        finally:
            check_session.finish(exc=check_exc, components=components)


def _resolve_token(explicit: str | None) -> str:
    return (
        (explicit or "").strip()
        or os.environ.get("GITHUB_TOKEN", "").strip()
        or os.environ.get("GH_TOKEN", "").strip()
    )


def _resolve_ollama() -> tuple[str, str]:
    url = (
        os.environ.get("OLLAMA_LOCAL_URL")
        or os.environ.get("OLLAMA_BASE_URL")
        or "http://127.0.0.1:11434"
    )
    url = str(url).strip().rstrip("/")
    try:
        from local_ai_stack.performance_profile import resolve_background_review_model

        model = resolve_background_review_model(tooling_root=ROOT, ollama_base_url=url)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(
            f"[github_integrator] could not resolve model from performance profile ({exc}); "
            "using OLLAMA_MODEL.\n"
        )
        model = (os.environ.get("OLLAMA_MODEL") or "qwen2.5:14b").strip() or "qwen2.5:14b"
    return url, model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post an evidence-first grounded review comment to a GitHub PR (PyGithub + local git + Ollama)."
    )
    parser.add_argument(
        "--pr-url",
        required=True,
        help="Full URL to the pull request, e.g. https://github.com/org/repo/pull/42",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to local clone of the repository (default: current directory)",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional .env file (e.g. deploy/local-ai/.env.local) for token and Ollama settings",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub token (otherwise GITHUB_TOKEN / GH_TOKEN from environment)",
    )
    parser.add_argument(
        "--query",
        default="Perform an evidence-first grounded code review of this PR diff; prioritize security, correctness, and regression risks.",
        help="Review prompt passed to grounded review",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the comment body to stdout instead of posting",
    )
    args = parser.parse_args(argv)

    env_path = Path(args.env_file).expanduser().resolve() if args.env_file else None
    load_environment(env_file=env_path)

    token = _resolve_token(args.token)
    ollama_url, model = _resolve_ollama()

    repo_path = Path(args.repo).expanduser()
    if not repo_path.is_absolute():
        repo_path = Path.cwd() / repo_path
    repo_path = repo_path.resolve()

    cfg = IntegratorConfig(
        token=token,
        ollama_url=ollama_url,
        model=model,
        query=args.query,
        repo_path=repo_path,
        dry_run=bool(args.dry_run),
    )

    try:
        bot = GitHubIntegrator(cfg.token)
        return bot.run(args.pr_url, cfg)
    except (GithubException, ValueError, RuntimeError, OSError) as exc:
        sys.stderr.write(f"[github_integrator] Error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
