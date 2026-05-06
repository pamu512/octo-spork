"""Orchestrate Trivy/CodeQL PR scans with :class:`scan_cache.ScanCache` and git-aware incremental work."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

_TRIVY_CHUNK = max(5, int(os.environ.get("OCTO_SPORK_TRIVY_PATH_CHUNK", "40")))
_SKIP_TRIVY = "OCTO_SPORK_SKIP_TRIVY"
_SKIP_CODEQL = "OCTO_SPORK_SKIP_CODEQL"


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@contextlib.contextmanager
def git_detached(repo: Path, sha: str):
    """Temporarily ``git checkout --detach <sha>``, restoring the previous HEAD afterward."""
    root = repo.expanduser().resolve()
    cur = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    before = (cur.stdout or "").strip()
    if cur.returncode != 0 or not before:
        raise RuntimeError(f"git rev-parse failed in {root}: {cur.stderr}")
    checkout = subprocess.run(
        ["git", "-C", str(root), "checkout", "--detach", sha],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if checkout.returncode != 0:
        raise RuntimeError(f"git checkout --detach {sha} failed: {checkout.stderr}")
    try:
        yield root
    finally:
        subprocess.run(
            ["git", "-C", str(root), "checkout", "--detach", before],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )


def iter_pull_changed_filenames(pull: Any) -> list[str]:
    """Collect unique repository-relative paths from a PyGithub ``PullRequest``."""
    paths: list[str] = []
    try:
        for f in pull.get_files():
            fn = getattr(f, "filename", None)
            if isinstance(fn, str) and fn.strip():
                paths.append(fn.strip())
    except Exception as exc:
        _LOG.warning("iter_pull_changed_filenames: %s", exc)
    return sorted(set(paths))


def _run_trivy_batched_paths(scanner: Any, repo_root: Path, paths: list[str]) -> dict[str, Any]:
    from github_bot.scan_cache import iter_sarif_results

    if not paths:
        res = scanner.run_fs_sarif_paths(repo_root, [])
        return json.loads(res.sarif_path.read_text(encoding="utf-8", errors="replace"))

    chunks: list[dict[str, Any]] = []
    for i in range(0, len(paths), _TRIVY_CHUNK):
        batch = paths[i : i + _TRIVY_CHUNK]
        result = scanner.run_fs_sarif_paths(repo_root, batch)
        chunks.append(json.loads(result.sarif_path.read_text(encoding="utf-8", errors="replace")))
    if len(chunks) == 1:
        return chunks[0]
    shell = json.loads(json.dumps(chunks[0]))
    merged_results: list[dict[str, Any]] = []
    for ch in chunks:
        merged_results.extend(iter_sarif_results(ch))
    runs = shell.setdefault("runs", [])
    if not runs:
        runs.append({})
    if not isinstance(runs[0], dict):
        runs[0] = {}
    runs[0]["results"] = merged_results
    return shell


def _ingest_trivy_smells(
    merged_sarif: dict[str, Any],
    repo_root: Path,
    *,
    repo_full_name: str | None,
    pr_html_url: str | None,
) -> str:
    if not (repo_full_name and pr_html_url):
        return ""
    try:
        from github_bot.global_smell_index import ingest_sarif_findings

        smell_md, _rec = ingest_sarif_findings(
            "trivy",
            merged_sarif,
            repo_root,
            repo_full_name=repo_full_name,
            pr_html_url=pr_html_url,
        )
        return smell_md.strip()
    except Exception as exc:
        _LOG.debug("global smell index (trivy merged): %s", exc)
        return ""


def run_integrator_trivy_scan(
    *,
    local_repo: Path | None,
    clone_url: str,
    branch: str,
    token: str,
    base_sha: str,
    head_sha: str,
    changed_paths: list[str],
    repo_full_name: str,
    pr_html_url: str,
) -> str:
    """Trivy SARIF for PR comments: cache at base + incremental paths at head (when enabled)."""
    from github_bot.scan_cache import (
        ScanCache,
        ScanCacheKey,
        merge_sarif_base_and_delta,
        scan_cache_enabled,
    )
    from github_bot.trivy_scanner import (
        TrivyScanner,
        parse_sarif_to_markdown_table,
        scan_pr_branch_to_markdown,
    )

    if _env_truthy(_SKIP_TRIVY):
        return scan_pr_branch_to_markdown(
            clone_url=clone_url,
            branch=branch,
            token=token,
            repo_full_name=repo_full_name,
            pr_html_url=pr_html_url,
        )

    scanner = TrivyScanner()
    if not scanner.available():
        return scan_pr_branch_to_markdown(
            clone_url=clone_url,
            branch=branch,
            token=token,
            repo_full_name=repo_full_name,
            pr_html_url=pr_html_url,
        )

    use_cache = (
        scan_cache_enabled()
        and local_repo is not None
        and (local_repo / ".git").is_dir()
        and base_sha.strip()
        and head_sha.strip()
    )
    if not use_cache:
        return scan_pr_branch_to_markdown(
            clone_url=clone_url,
            branch=branch,
            token=token,
            repo_full_name=repo_full_name,
            pr_html_url=pr_html_url,
        )

    assert local_repo is not None
    cache = ScanCache()
    base_key = ScanCacheKey(repo_full_name=repo_full_name, commit_sha=base_sha, scanner="trivy")

    try:
        base_sarif = cache.get_sarif(base_key)
        if base_sarif is None:
            with git_detached(local_repo, base_sha):
                res = scanner.run_fs_sarif(local_repo)
                base_sarif = json.loads(res.sarif_path.read_text(encoding="utf-8", errors="replace"))
                cache.put_sarif(base_key, base_sarif)

        with git_detached(local_repo, head_sha):
            if not changed_paths:
                res = scanner.run_fs_sarif(local_repo)
                merged = json.loads(res.sarif_path.read_text(encoding="utf-8", errors="replace"))
                note = (
                    f"\n\n<sub>ScanCache: full Trivy scan at head `{head_sha[:7]}` "
                    f"(no changed-file list from the API).</sub>\n"
                )
            else:
                existing = [p for p in changed_paths if (local_repo / p).is_file() or (local_repo / p).is_dir()]
                delta_sarif = _run_trivy_batched_paths(scanner, local_repo, existing)
                merged = merge_sarif_base_and_delta(
                    base_sarif,
                    delta_sarif,
                    changed_paths=set(changed_paths),
                )
                note = (
                    f"\n\n<sub>ScanCache: merged cached base `{base_sha[:7]}` + incremental Trivy on "
                    f"{len(existing)} path(s) at head `{head_sha[:7]}`.</sub>\n"
                )

            table = parse_sarif_to_markdown_table(merged)
            smell = _ingest_trivy_smells(
                merged,
                local_repo,
                repo_full_name=repo_full_name,
                pr_html_url=pr_html_url,
            )
            if smell:
                table = table.rstrip() + "\n\n" + smell + "\n"
            return table + note
    except Exception as exc:
        _LOG.warning("ScanCache Trivy path failed; falling back to ephemeral clone: %s", exc)
        return scan_pr_branch_to_markdown(
            clone_url=clone_url,
            branch=branch,
            token=token,
            repo_full_name=repo_full_name,
            pr_html_url=pr_html_url,
        )


def _ingest_codeql_smells(
    sarif_payload: dict[str, Any],
    repo_root: Path,
    *,
    repo_full_name: str | None,
    pr_html_url: str | None,
) -> str:
    if not (repo_full_name and pr_html_url):
        return ""
    try:
        from github_bot.global_smell_index import ingest_sarif_findings

        smell_md, _rec = ingest_sarif_findings(
            "codeql",
            sarif_payload,
            repo_root,
            repo_full_name=repo_full_name,
            pr_html_url=pr_html_url,
        )
        return smell_md.strip()
    except Exception as exc:
        _LOG.debug("global smell index (codeql): %s", exc)
        return ""


def run_integrator_codeql_scan(
    *,
    local_repo: Path | None,
    clone_url: str,
    branch: str,
    token: str,
    head_sha: str,
    repo_full_name: str,
    pr_html_url: str,
) -> str:
    """CodeQL markdown for PR comments; cache hit on ``head_sha`` skips database rebuild."""
    from github_bot.codeql_runner import (
        CodeQLRunner,
        critical_findings_markdown_from_sarif,
        scan_pr_branch_codeql_to_markdown,
    )
    from github_bot.scan_cache import ScanCache, ScanCacheKey, scan_cache_enabled

    if _env_truthy(_SKIP_CODEQL):
        return scan_pr_branch_codeql_to_markdown(
            clone_url=clone_url,
            branch=branch,
            token=token,
            repo_full_name=repo_full_name,
            pr_html_url=pr_html_url,
        )

    runner = CodeQLRunner()
    if not runner.available():
        return scan_pr_branch_codeql_to_markdown(
            clone_url=clone_url,
            branch=branch,
            token=token,
            repo_full_name=repo_full_name,
            pr_html_url=pr_html_url,
        )

    use_cache = (
        scan_cache_enabled()
        and local_repo is not None
        and (local_repo / ".git").is_dir()
        and head_sha.strip()
    )
    if not use_cache:
        return scan_pr_branch_codeql_to_markdown(
            clone_url=clone_url,
            branch=branch,
            token=token,
            repo_full_name=repo_full_name,
            pr_html_url=pr_html_url,
        )

    assert local_repo is not None
    cache = ScanCache()
    head_key = ScanCacheKey(repo_full_name=repo_full_name, commit_sha=head_sha, scanner="codeql")

    try:
        hit = cache.get_sarif(head_key)
        if hit is not None:
            with git_detached(local_repo, head_sha):
                md = critical_findings_markdown_from_sarif(hit, source_root=local_repo, runner=runner)
                smell = _ingest_codeql_smells(
                    hit,
                    local_repo,
                    repo_full_name=repo_full_name,
                    pr_html_url=pr_html_url,
                )
                tail = f"\n\n<sub>ScanCache: CodeQL SARIF served from cache for `{head_sha[:7]}`.</sub>\n"
                if smell:
                    md = md.rstrip() + "\n\n" + smell + "\n"
                return md + tail

        with git_detached(local_repo, head_sha):
            work = local_repo / ".octo-spork-codeql-work"
            res = runner.run_on_source_root(local_repo, work_dir=work)
            md = res.markdown
            sarif_path = work / "results.sarif"
            payload: dict[str, Any] | None = None
            if sarif_path.is_file() and not res.build_failed:
                try:
                    payload = json.loads(sarif_path.read_text(encoding="utf-8", errors="replace"))
                    cache.put_sarif(head_key, payload)
                except (OSError, json.JSONDecodeError) as exc:
                    _LOG.warning("scan_cache codeql put failed: %s", exc)
                    payload = None
            smell = (
                _ingest_codeql_smells(
                    payload,
                    local_repo,
                    repo_full_name=repo_full_name,
                    pr_html_url=pr_html_url,
                )
                if payload
                else ""
            )
            if smell:
                md = md.rstrip() + "\n\n" + smell + "\n"
            tail = f"\n\n<sub>ScanCache: CodeQL stored SARIF for `{head_sha[:7]}`.</sub>\n"
            return md + tail
    except Exception as exc:
        _LOG.warning("ScanCache CodeQL path failed; falling back to ephemeral clone: %s", exc)
        return scan_pr_branch_codeql_to_markdown(
            clone_url=clone_url,
            branch=branch,
            token=token,
            repo_full_name=repo_full_name,
            pr_html_url=pr_html_url,
        )
