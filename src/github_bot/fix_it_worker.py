"""Remediation PR workflow for ``/octo-spork fix`` PR comments.

Clones the repository, checks out a branch from the PR head, runs the local Claude Code agent
via Docker Compose (same stack as ``local_ai_stack``), then — when verification is enabled —
runs :class:`remediation.rescan_loop.RescanLoop` until **Clean** before committing. After three
failed attempts, posts a **System Warning** comment instead of opening a remediation PR.

Environment:

- ``OCTO_FIX_VERIFY_ENABLED`` — ``0`` / ``false`` to skip RescanLoop (default: on).
- ``OCTO_FIX_VERIFY_CVE`` — CVE id to clear (e.g. ``CVE-2024-12345``); otherwise first CVE in the brief.
- ``OCTO_FIX_VERIFY_MAX_ATTEMPTS`` — default ``3``.
- **Latency logger** — :mod:`observability.latency_success_logger` records **Scan Start** (run begin)
  through **Verified Patch** (RescanLoop clean) in ``.local/latency_success.db``. Slow verified TTR
  triggers **context reset** (see ``OCTO_REMEDIATION_SLOW_TTR_SEC``, ``OCTO_CONTEXT_RESET_DISABLE``).
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
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from github import Auth, Github

from github_bot.git_manager import build_grounded_pull_request

_LOG = logging.getLogger(__name__)

CVE_INLINE_RE = re.compile(r"\b(CVE-\d{4}-\d+)\b", re.IGNORECASE)


def _record_remediation_latency(
    *,
    scan_start: float,
    pr_html_url: str,
    cve_id: str,
    outcome: str,
    success_verified_patch: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    """Persist Scan Start → event latency; may trigger context reset if verified TTR is slow."""
    try:
        from observability.latency_success_logger import (
            RemediationLatencyRow,
            log_remediation_latency,
            maybe_trigger_context_reset_for_ttr,
        )
    except ImportError:
        return
    end = time.time()
    ttr = max(0.0, end - scan_start)
    row = RemediationLatencyRow(
        created_at_utc=datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        scan_start_unix=scan_start,
        event_end_unix=end,
        ttr_seconds=ttr,
        success_verified_patch=success_verified_patch,
        outcome=outcome,
        pr_html_url=pr_html_url[:2048],
        cve_id=cve_id[:128],
        extra=dict(extra or {}),
    )
    try:
        log_remediation_latency(row)
    except OSError as exc:
        _LOG.debug("remediation latency log skipped: %s", exc)
    maybe_trigger_context_reset_for_ttr(
        ttr_seconds=ttr,
        success_verified_patch=success_verified_patch,
        repo_root=octo_spork_repo_root(),
    )


def extract_cve_for_fix_verification(text: str) -> str:
    """Return the first CVE-id mentioned in *text*, uppercased, or empty string if none."""
    m = CVE_INLINE_RE.search(text or "")
    return m.group(1).upper() if m else ""


def format_system_warning_verification_failed(
    *,
    original_html: str,
    pr_number: int,
    max_attempts: int,
    last_detail: str,
) -> str:
    """Markdown body for the PR comment when RescanLoop never reaches Clean."""
    detail = (last_detail or "").strip()
    if len(detail) > 6000:
        detail = detail[:5900] + "\n\n… _(truncated)_\n"
    detail_display = detail if detail else "(no detail captured)"
    try:
        from github_bot.negative_reinforcement import negative_reinforcement_markdown_section

        nr_block = negative_reinforcement_markdown_section(validation_error=detail_display)
    except ImportError:
        nr_block = ""
    return (
        "## System Warning — remediation verification failed\n\n"
        "The automated fix pipeline **did not reach a Clean status** from `RescanLoop` "
        f"after **{max_attempts}** attempt(s): patch validation and/or Trivy CVE rescan did not pass.\n\n"
        "**The agent was unable to produce a verified solution.** "
        f"No remediation pull request was opened for [{original_html}]({original_html}).\n\n"
        "### Last verification detail\n\n"
        "```text\n"
        f"{detail_display}\n"
        "```\n"
        + (f"\n{nr_block}" if nr_block else "")
    )


def _run_rescan_verification(
    clone_dir: Path,
    *,
    agent_diff: str,
    cve_id: str,
    max_attempts: int,
) -> tuple[bool, str]:
    """Run :class:`remediation.rescan_loop.RescanLoop` up to *max_attempts* times.

    Returns ``(True, \"\")`` when a run reaches **Clean**
    (:attr:`~remediation.validator.PatchValidationResult.clean`).
    Otherwise ``(False, last_error_detail)``.
    """
    from remediation.exceptions import VerificationFailedError
    from remediation.rescan_loop import RescanLoop
    from remediation.validator import PatchValidator

    rv_root = (os.environ.get("OCTO_PATCH_VERIFY_ROOT") or "").strip()
    validator = (
        PatchValidator(clone_dir, verify_root=Path(rv_root).expanduser().resolve())
        if rv_root
        else PatchValidator(clone_dir)
    )
    loop = RescanLoop(validator, cve_id)
    last_note = ""
    for attempt in range(1, max_attempts + 1):
        try:
            result = loop.run(agent_diff)
            if result.clean:
                return True, ""
            last_note = result.stderr or "git apply did not succeed (non-clean RescanLoop result)"
        except VerificationFailedError as exc:
            last_note = ((str(exc) + "\n" + (exc.snippet or "")).strip())[:8000]
        except Exception as exc:  # noqa: BLE001 -- aggregate failures for PR comment
            last_note = f"{type(exc).__name__}: {exc}"[:8000]
        _LOG.warning(
            "fix-it RescanLoop attempt %s/%s did not reach Clean: %s",
            attempt,
            max_attempts,
            last_note[:500],
        )
    return False, last_note


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

    scan_start = time.time()
    cve_id_for_log = ""
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
    default_max_ctx = int(os.environ.get("OCTO_FIX_CONTEXT_MAX_CHARS", "120000"))
    try:
        from observability.latency_success_logger import aggressive_prune_effective_max_chars

        max_ctx = aggressive_prune_effective_max_chars(default_max_ctx, repo_root=repo_root)
    except ImportError:
        max_ctx = default_max_ctx
    if max_ctx < default_max_ctx:
        _LOG.info(
            "Remediation brief capped by aggressive prune: %s chars (default_max=%s)",
            max_ctx,
            default_max_ctx,
        )

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

    cve_id_for_log = (os.environ.get("OCTO_FIX_VERIFY_CVE") or "").strip() or extract_cve_for_fix_verification(
        brief_doc,
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

        head_proc = _git(["rev-parse", "HEAD"], cwd=clone_dir, timeout=30)
        if head_proc.returncode != 0:
            raise RuntimeError(
                f"git rev-parse HEAD failed: {(head_proc.stderr or head_proc.stdout or '').strip()[:2000]}"
            )
        pr_head_sha = (head_proc.stdout or "").strip()

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

        diff_proc = _git(["diff", pr_head_sha], cwd=clone_dir, timeout=120)
        agent_diff = diff_proc.stdout or ""
        if diff_proc.returncode != 0:
            raise RuntimeError(
                f"git diff failed: {(diff_proc.stderr or diff_proc.stdout or '').strip()[:2000]}"
            )

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
            _record_remediation_latency(
                scan_start=scan_start,
                pr_html_url=original_html,
                cve_id=cve_id_for_log,
                outcome="no_agent_diff",
                success_verified_patch=False,
                extra={"agent_exit_code": code},
            )
            return

        verify_on = os.environ.get("OCTO_FIX_VERIFY_ENABLED", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        cve_for_verify = cve_id_for_log
        max_verify_attempts = max(1, int(os.environ.get("OCTO_FIX_VERIFY_MAX_ATTEMPTS", "3")))
        verified_patch_reached = False
        if verify_on and cve_for_verify and agent_diff.strip():
            clean, last_note = _run_rescan_verification(
                clone_dir,
                agent_diff=agent_diff,
                cve_id=cve_for_verify,
                max_attempts=max_verify_attempts,
            )
            if not clean:
                warn_body = format_system_warning_verification_failed(
                    original_html=original_html,
                    pr_number=pr_number,
                    max_attempts=max_verify_attempts,
                    last_detail=last_note,
                )
                _post_pr_comment(owner, repo_name, pr_number, warn_body, token)
                _record_remediation_latency(
                    scan_start=scan_start,
                    pr_html_url=original_html,
                    cve_id=cve_id_for_log,
                    outcome="verification_failed",
                    success_verified_patch=False,
                    extra={"detail_head": (last_note or "")[:500]},
                )
                return
            verified_patch_reached = True
        elif verify_on and not cve_for_verify:
            _LOG.warning(
                "OCTO_FIX_VERIFY_ENABLED but no CVE id (set OCTO_FIX_VERIFY_CVE or mention CVE in brief); "
                "skipping RescanLoop — posting remediation without CVE verification.",
            )

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
            _record_remediation_latency(
                scan_start=scan_start,
                pr_html_url=original_html,
                cve_id=cve_id_for_log,
                outcome="empty_index_after_stage",
                success_verified_patch=False,
                extra={"agent_exit_code": code},
            )
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
        _record_remediation_latency(
            scan_start=scan_start,
            pr_html_url=original_html,
            cve_id=cve_id_for_log,
            outcome="verified_patch" if verified_patch_reached else "remediation_pr_without_verified_scan",
            success_verified_patch=verified_patch_reached,
            extra={
                "remediation_pr_url": remed_html,
                "fix_branch": fix_branch,
                "agent_exit_code": code,
            },
        )
    except Exception as exc:
        _record_remediation_latency(
            scan_start=scan_start,
            pr_html_url=original_html,
            cve_id=cve_id_for_log,
            outcome=f"error:{type(exc).__name__}",
            success_verified_patch=False,
            extra={"message": str(exc)[:1200]},
        )
        raise
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


async def run_fix_remediation_pr(envelope: dict[str, Any]) -> None:
    """Entry point from the webhook worker (async wrapper)."""
    await asyncio.to_thread(run_fix_remediation_pr_sync, envelope)
