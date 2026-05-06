"""Remediation PR workflow for ``/octo-spork fix`` PR comments.

Clones the repository, checks out a branch from the PR head, runs the local Claude Code agent
via Docker Compose (same stack as ``local_ai_stack``), pushes ``octo-fix/pr-<n>``, and opens a
linked remediation pull request with an agent transcript.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from github import Auth, Github

from github_bot.git_manager import build_grounded_pull_request

_LOG = logging.getLogger(__name__)

PR_HTML_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<num>\d+)",
    re.IGNORECASE,
)

COMPOSE_PROJECT_NAME = "octo-spork-local-ai"
CLAUDE_AGENT_SERVICE = "claude-agent"
CONTAINER_WORKSPACE = "/workspace"

DEFAULT_BRANCH_PREFIX = "octo-fix/pr-"


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


def octo_spork_repo_root() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _github_token() -> str:
    tok = (
        (os.environ.get("GITHUB_TOKEN") or "").strip()
        or (os.environ.get("GH_TOKEN") or "").strip()
    )
    if not tok:
        raise RuntimeError(
            "GITHUB_TOKEN or GH_TOKEN must be set for remediation PRs (repo scope: contents, pull requests)."
        )
    return tok


def parse_pr_html_url(pr_html_url: str) -> tuple[str, str, int]:
    m = PR_HTML_URL_RE.search(pr_html_url.strip())
    if not m:
        raise ValueError(f"Could not parse GitHub PR URL: {pr_html_url!r}")
    return m.group("owner"), m.group("repo").removesuffix(".git"), int(m.group("num"))


def _compose_base_cmd(repo_root: Path, env_file: Path, agenticseek_path: Path) -> list[str]:
    root = repo_root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from local_ai_stack.resource_hardener import ensure_compose_resource_override

        ensure_compose_resource_override(root, logger=_LOG)
    except Exception as exc:
        _LOG.warning("ResourceHardener could not refresh compose override: %s", exc)

    override = root / "deploy" / "local-ai" / "docker-compose.override.yaml"
    cmd: list[str] = [
        "docker",
        "compose",
        "--project-name",
        os.environ.get("COMPOSE_PROJECT_NAME", COMPOSE_PROJECT_NAME),
        "--env-file",
        str(env_file),
        "-f",
        str(agenticseek_path / "docker-compose.yml"),
        "-f",
        str(repo_root / "deploy" / "local-ai" / "docker-compose.addons.yml"),
        "-f",
        str(repo_root / "deploy" / "local-ai" / "docker-compose.claude-agent.yml"),
    ]
    if override.is_file():
        cmd.extend(["-f", str(override)])
    cmd.extend(
        [
            "--profile",
            "full",
            "--profile",
            "addons",
        ]
    )
    return cmd


def resolve_compose_paths(repo_root: Path) -> tuple[Path, Path]:
    """Return ``(env_file, agenticseek_path)`` for Docker Compose."""
    env_path = (
        os.environ.get("OCTO_LOCAL_AI_ENV_FILE") or str(repo_root / "deploy" / "local-ai" / ".env.local")
    ).strip()
    env_file = Path(env_path).expanduser().resolve()
    env_values = _parse_env_simple(env_file)
    agenticseek_raw = (
        (os.environ.get("AGENTICSEEK_DIR") or "").strip()
        or env_values.get("AGENTICSEEK_DIR", "").strip()
    )
    if not agenticseek_raw:
        raise RuntimeError(
            "AGENTICSEEK_DIR is not set. Add it to deploy/local-ai/.env.local (see .env.example) "
            "or export AGENTICSEEK_DIR so the Claude agent compose files can be resolved."
        )
    agenticseek_path = Path(agenticseek_raw).expanduser().resolve()
    if not (agenticseek_path / "docker-compose.yml").is_file():
        raise RuntimeError(f"AGENTICSEEK_DIR does not contain docker-compose.yml: {agenticseek_path}")
    return env_file, agenticseek_path


def ensure_claude_agent_container(repo_root: Path, env_file: Path, agenticseek_path: Path) -> None:
    """Start ``claude-agent`` via Compose so the image exists and the stack network is available."""
    if os.environ.get("OCTO_FIX_SKIP_DOCKER", "").strip() == "1":
        _LOG.info("OCTO_FIX_SKIP_DOCKER=1 — skipping docker compose up for claude-agent.")
        return
    cmd = _compose_base_cmd(repo_root, env_file, agenticseek_path) + [
        "up",
        "-d",
        CLAUDE_AGENT_SERVICE,
    ]
    _LOG.info("Ensuring Claude Code agent container: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("OCTO_FIX_DOCKER_UP_TIMEOUT_SEC", "600")),
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"docker compose up claude-agent failed ({proc.returncode}): {err[:4000]}")


def run_claude_remediation_agent(
    repo_root: Path,
    env_file: Path,
    agenticseek_path: Path,
    workspace: Path,
    *,
    timeout_sec: int,
) -> tuple[int, str]:
    """Run ``bun run src/main.tsx`` in a one-off claude-agent container with ``workspace`` mounted."""
    brief_path = workspace / "OCTO_REMEDIATION_BRIEF.md"
    if not brief_path.is_file():
        raise RuntimeError("Internal error: OCTO_REMEDIATION_BRIEF.md missing before agent run.")

    inner_prompt = (
        "Read the appended system context (remediation brief). Implement minimal, correct fixes "
        "in this repository for the issues described. Use Read, Grep, Glob, Edit, and Bash as needed. "
        "When finished, write a detailed trace of your reasoning to "
        f"`{CONTAINER_WORKSPACE}/OCTO_AGENT_TRACE.md` at the repo root "
        "(steps taken, files changed, rationale, and any remaining risks)."
    )

    try:
        from infra.resource_manager import enforce_before_agent_task

        enforce_before_agent_task()
    except ResourceWarning as exc:
        return (
            1,
            "### Predictive VRAM governor\n\n"
            f"{exc}\n\n"
            "_Free GPU memory ratio is below `OCTO_VRAM_MIN_FREE_RATIO` or unload models via "
            "`VRAMManager.clear_cache()` / `ollama` stop._\n",
        )

    from claude_bridge.resource_monitor import vram_guard_allows_claude_launch

    ok_guard, guard_msg = vram_guard_allows_claude_launch()
    if not ok_guard:
        if guard_msg:
            _LOG.warning("VRAM guard blocked remediation agent: %s", guard_msg[:500])
        return (
            1,
            "### VRAM guard\n\n"
            "Host GPU memory is above the configured threshold; the remediation agent was not started. "
            "Use a smaller Ollama model or stop heavy Docker services (e.g. `docker stop local-ai-n8n`), "
            "or set `OCTO_SKIP_VRAM_GUARD=1` if appropriate.\n",
        )

    cmd = _compose_base_cmd(repo_root, env_file, agenticseek_path) + [
        "run",
        "--rm",
        "--no-deps",
        "-v",
        f"{workspace}:{CONTAINER_WORKSPACE}",
        "-e",
        f"OCTO_WORKSPACE={CONTAINER_WORKSPACE}",
        "-e",
        "OCTO_SKIP_GROUNDED_EVIDENCE=1",
        "-e",
        "OCTO_SKIP_SIDECAR=1",
        "-e",
        "OCTO_CLAUDE_ALLOWED_TOOLS=Read,Grep,Glob,Edit,Bash",
        CLAUDE_AGENT_SERVICE,
        "bun",
        "run",
        "src/main.tsx",
        "--",
        "--append-system-prompt-file",
        f"{CONTAINER_WORKSPACE}/OCTO_REMEDIATION_BRIEF.md",
        "-p",
        inner_prompt,
        "--allowedTools",
        "Read,Grep,Glob,Edit,Bash",
    ]

    _LOG.info("Running Claude remediation agent (docker compose run) timeout=%ss", timeout_sec)
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    raw_out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if raw_out.strip():
        from claude_bridge.session_store import (
            extract_session_id,
            record_session_enabled,
            save_last_session_id,
        )

        if record_session_enabled():
            sid = extract_session_id(raw_out)
            if sid:
                save_last_session_id(repo_root, sid)

    combined = ""
    if proc.stdout:
        combined += f"### stdout\n\n```text\n{proc.stdout.strip()}\n```\n\n"
    if proc.stderr:
        combined += f"### stderr\n\n```text\n{proc.stderr.strip()}\n```\n\n"
    return proc.returncode, combined


def _git(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _post_pr_comment(owner: str, repo_n: str, pr_number: int, body: str, token: str) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo_n}/issues/{pr_number}/comments"
    payload = json.dumps({"body": body}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "octo-spork-fix-it-worker",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            if getattr(resp, "status", 200) not in (200, 201):
                raw = resp.read()[:2000].decode("utf-8", errors="replace")
                _LOG.error("Unexpected status posting PR comment: %s %s", resp.status, raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        _LOG.error("GitHub HTTP error posting PR comment: %s %s", exc.code, detail)
    except urllib.error.URLError as exc:
        _LOG.error("Failed to post PR comment: %s", exc)


def run_fix_remediation_pr_sync(envelope: dict[str, Any]) -> None:
    """Synchronous remediation pipeline (invoked via :func:`asyncio.to_thread`)."""
    pr_html_url = envelope.get("pr_html_url")
    if not isinstance(pr_html_url, str) or not pr_html_url.strip():
        raise ValueError("envelope missing pr_html_url")

    owner, repo_name, pr_number = parse_pr_html_url(pr_html_url)
    token = _github_token()
    repo_root = octo_spork_repo_root()

    gh = Github(auth=Auth.Token(token), timeout=120)
    gh_repo = gh.get_repo(f"{owner}/{repo_name}")
    pull = gh_repo.get_pull(pr_number)

    base_ref = pull.base.ref
    head_sha = pull.head.sha
    original_title = pull.title or f"PR #{pr_number}"
    original_html = pull.html_url

    fix_branch = f"{DEFAULT_BRANCH_PREFIX}{pr_number}"
    max_ctx = int(os.environ.get("OCTO_FIX_CONTEXT_MAX_CHARS", "120000"))

    grounded = build_grounded_pull_request(
        owner,
        repo_name,
        pr_number,
        head_sha,
        pull.base.sha,
        token,
    )
    llm_blob = grounded.format_for_llm()
    if len(llm_blob) > max_ctx:
        llm_blob = llm_blob[: max_ctx - 200] + "\n\n… [context truncated for OCTO_FIX_CONTEXT_MAX_CHARS]\n"

    brief_doc = "\n".join(
        [
            "# Remediation brief",
            "",
            f"- Original pull request: {original_html}",
            f"- Title: {original_title}",
            f"- Base branch: `{base_ref}`",
            f"- PR head SHA: `{head_sha}`",
            "",
            "## Repository context for the agent",
            "",
            llm_blob,
        ]
    )

    env_file, agenticseek_path = resolve_compose_paths(repo_root)
    ensure_claude_agent_container(repo_root, env_file, agenticseek_path)

    clones_root = Path(
        os.environ.get("OCTO_SPORK_TEMP_CLONES_DIR") or (repo_root / ".temp_clones")
    ).expanduser().resolve()
    clones_root.mkdir(parents=True, exist_ok=True)

    clone_dir = Path(tempfile.mkdtemp(prefix=f"fix-pr-{pr_number}-", dir=str(clones_root)))
    auth_clone_url = (
        f"https://x-access-token:{token}@github.com/{owner}/{repo_name}.git"
    )

    try:
        clone = _git(
            ["clone", "--depth", "100", auth_clone_url, str(clone_dir)],
            cwd=clones_root,
            timeout=900,
        )
        if clone.returncode != 0:
            raise RuntimeError(
                f"git clone failed: {(clone.stderr or clone.stdout or '').strip()[:4000]}"
            )

        fetch = _git(
            ["fetch", "origin", f"pull/{pr_number}/head"],
            cwd=clone_dir,
            timeout=600,
        )
        if fetch.returncode != 0:
            raise RuntimeError(
                f"git fetch pull/{pr_number}/head failed: {(fetch.stderr or fetch.stdout or '').strip()[:4000]}"
            )

        checkout = _git(["checkout", "-B", fix_branch, "FETCH_HEAD"], cwd=clone_dir, timeout=120)
        if checkout.returncode != 0:
            raise RuntimeError(
                f"git checkout failed: {(checkout.stderr or checkout.stdout or '').strip()[:4000]}"
            )

        (clone_dir / "OCTO_REMEDIATION_BRIEF.md").write_text(brief_doc, encoding="utf-8")

        timeout_sec = int(os.environ.get("OCTO_FIX_CLAUDE_TIMEOUT_SEC", "3600"))
        code, transcript = run_claude_remediation_agent(
            repo_root,
            env_file,
            agenticseek_path,
            clone_dir,
            timeout_sec=timeout_sec,
        )

        trace_path = clone_dir / "OCTO_AGENT_TRACE.md"
        trace_body = ""
        if trace_path.is_file():
            trace_body = trace_path.read_text(encoding="utf-8", errors="replace").strip()
        if not trace_body:
            trace_body = transcript.strip() or "_No agent trace file or stdout captured._"

        for p in (clone_dir / "OCTO_REMEDIATION_BRIEF.md", trace_path):
            try:
                if p.is_file():
                    p.unlink()
            except OSError:
                pass

        st = _git(["status", "--porcelain"], cwd=clone_dir, timeout=60)
        if st.returncode != 0:
            raise RuntimeError("git status failed in clone.")

        if not (st.stdout or "").strip():
            body = (
                f"**Octo-spork fix** — no changes were produced for [{original_html}]({original_html}).\n\n"
                f"Agent exit code: `{code}`.\n\n"
                "## Agent reasoning trace\n\n"
                + trace_body[:60000]
            )
            _post_pr_comment(owner, repo_name, pr_number, body, token)
            return

        ident_name = (os.environ.get("OCTO_FIX_GIT_USER_NAME") or "octo-spork bot").strip()
        ident_email = (
            os.environ.get("OCTO_FIX_GIT_USER_EMAIL") or "octo-spork-bot@users.noreply.github.com"
        ).strip()
        _git(["config", "user.name", ident_name], cwd=clone_dir, timeout=30)
        _git(["config", "user.email", ident_email], cwd=clone_dir, timeout=30)

        stage = _git(["add", "-A"], cwd=clone_dir, timeout=120)
        if stage.returncode != 0:
            raise RuntimeError(
                f"git add failed: {(stage.stderr or stage.stdout or '').strip()[:4000]}"
            )

        empty_idx = _git(["diff", "--cached", "--quiet"], cwd=clone_dir, timeout=30)
        if empty_idx.returncode == 0:
            body = (
                f"**Octo-spork fix** — nothing to commit after staging for [{original_html}]({original_html}).\n\n"
                f"Agent exit code: `{code}`.\n\n"
                "## Agent reasoning trace\n\n"
                + trace_body[:60000]
            )
            _post_pr_comment(owner, repo_name, pr_number, body, token)
            return

        commit = _git(
            ["commit", "-m", f"fix: remediation for PR #{pr_number} ({original_title})"],
            cwd=clone_dir,
            timeout=120,
        )
        if commit.returncode != 0:
            raise RuntimeError(
                f"git commit failed: {(commit.stderr or commit.stdout or '').strip()[:4000]}"
            )

        push = _git(["push", "-u", "origin", fix_branch], cwd=clone_dir, timeout=600)
        if push.returncode != 0:
            raise RuntimeError(
                f"git push failed: {(push.stderr or push.stdout or '').strip()[:4000]}"
            )

        remediation_title = f"Remediation for #{pr_number}: {original_title}"
        pr_body_max = 62000
        description = (
            f"This **remediation PR** was generated by `/octo-spork fix` for "
            f"[#{pr_number}]({original_html}).\n\n"
            f"- Base branch: `{base_ref}`\n"
            f"- Fix branch: `{fix_branch}`\n"
            f"- Claude agent exit code: `{code}`\n\n"
            "## Agent reasoning trace\n\n"
            + trace_body[: pr_body_max - 500]
        )
        if len(trace_body) > pr_body_max - 500:
            description += "\n\n_(Trace truncated for GitHub PR body size.)_\n"

        existing = list(
            gh_repo.get_pulls(state="open", head=f"{owner}:{fix_branch}")
        )
        if existing:
            remed_pr = existing[0]
            remed_pr.edit(body=description[:65500])
            remed_html = remed_pr.html_url
        else:
            remed_pr = gh_repo.create_pull(
                title=remediation_title[:240],
                body=description[:65500],
                base=base_ref,
                head=fix_branch,
            )
            remed_html = remed_pr.html_url

        _post_pr_comment(
            owner,
            repo_name,
            pr_number,
            f"**Octo-spork remediation PR:** [{remediation_title}]({remed_html})\n\n"
            f"_Branch `{fix_branch}` pushed; agent exit code `{code}`._",
            token,
        )
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


async def run_fix_remediation_pr(envelope: dict[str, Any]) -> None:
    """Entry point from the webhook worker (async wrapper)."""
    await asyncio.to_thread(run_fix_remediation_pr_sync, envelope)
