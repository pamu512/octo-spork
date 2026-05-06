"""Run Trivy filesystem scans (SARIF) on a repository checkout and summarize for PR comments."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_LOG = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = int(os.environ.get("TRIVY_FS_TIMEOUT_SEC", "900"))
_SKIP_ENV = "OCTO_SPORK_SKIP_TRIVY"


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TrivyScanResult:
    """Outcome of ``trivy fs`` writing SARIF under the repository."""

    sarif_path: Path
    returncode: int
    stderr: str


class TrivyScanner:
    """Clone-assisted workflows use :func:`temporary_pr_clone`; scans use :meth:`run_fs_sarif`."""

    def __init__(
        self,
        trivy_executable: str | None = None,
        *,
        timeout_sec: int | None = None,
    ) -> None:
        import shutil as _shutil

        resolved = trivy_executable or os.environ.get("TRIVY_PATH") or _shutil.which("trivy")
        self._trivy = resolved
        self._timeout = int(timeout_sec if timeout_sec is not None else _DEFAULT_TIMEOUT)

    def available(self) -> bool:
        return bool(self._trivy)

    def run_fs_sarif(self, repository_root: Path) -> TrivyScanResult:
        """Run ``trivy fs --format sarif --output results.sarif ..`` from a child of ``repository_root``.

        The ``..`` target matches a scan of the repository root while keeping the SARIF file in a
        dedicated working subdirectory.
        """
        if not self._trivy:
            raise FileNotFoundError(
                "Trivy CLI not found on PATH. Install Trivy or set TRIVY_PATH / OCTO_SPORK_SKIP_TRIVY=1."
            )
        root = repository_root.expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"Repository root is not a directory: {root}")

        work = root / ".octo-spork-trivy-run"
        work.mkdir(parents=True, exist_ok=True)
        sarif_out = work / "results.sarif"

        cmd = [
            self._trivy,
            "fs",
            "--format",
            "sarif",
            "--output",
            "results.sarif",
            "..",
        ]
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(work),
                capture_output=True,
                text=True,
                timeout=max(60, self._timeout),
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Trivy executable not runnable: {self._trivy}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Trivy timed out after {self._timeout}s") from exc

        err = (completed.stderr or "").strip()
        if not sarif_out.is_file():
            detail = err or (completed.stdout or "").strip() or "(no stderr)"
            raise RuntimeError(
                f"Trivy did not write {sarif_out} (exit {completed.returncode}): {detail[:4000]}"
            )

        return TrivyScanResult(
            sarif_path=sarif_out,
            returncode=int(completed.returncode),
            stderr=err,
        )

    def run_fs_sarif_paths(self, repository_root: Path, relative_paths: list[str]) -> TrivyScanResult:
        """Run ``trivy fs`` scoped to specific repository-relative paths (PR delta scan).

        Paths are resolved under ``repository_root``; missing paths are skipped. When no paths
        remain, a minimal empty SARIF file is written without invoking Trivy.
        """
        if not self._trivy:
            raise FileNotFoundError(
                "Trivy CLI not found on PATH. Install Trivy or set TRIVY_PATH / OCTO_SPORK_SKIP_TRIVY=1."
            )
        root = repository_root.expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"Repository root is not a directory: {root}")

        work = root / ".octo-spork-trivy-run"
        work.mkdir(parents=True, exist_ok=True)
        sarif_out = work / "results.sarif"

        rel_args: list[str] = []
        for raw in relative_paths:
            p = (raw or "").strip().replace("\\", "/").lstrip("./")
            if not p:
                continue
            candidate = root / p
            try:
                if candidate.is_file() or candidate.is_dir():
                    rel_args.append(p)
            except OSError:
                continue

        if not rel_args:
            minimal = {
                "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
                "version": "2.1.0",
                "runs": [{"tool": {"driver": {"name": "trivy", "version": "skipped-empty-paths"}}, "results": []}],
            }
            sarif_out.write_text(json.dumps(minimal, indent=2), encoding="utf-8")
            return TrivyScanResult(sarif_path=sarif_out, returncode=0, stderr="")

        cmd: list[str] = [
            self._trivy,
            "fs",
            "--format",
            "sarif",
            "--output",
            "results.sarif",
            *[f"../{p}" for p in rel_args],
        ]
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(work),
                capture_output=True,
                text=True,
                timeout=max(60, self._timeout),
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Trivy executable not runnable: {self._trivy}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Trivy timed out after {self._timeout}s") from exc

        err = (completed.stderr or "").strip()
        if not sarif_out.is_file():
            detail = err or (completed.stdout or "").strip() or "(no stderr)"
            raise RuntimeError(
                f"Trivy did not write {sarif_out} (exit {completed.returncode}): {detail[:4000]}"
            )

        return TrivyScanResult(
            sarif_path=sarif_out,
            returncode=int(completed.returncode),
            stderr=err,
        )


def authenticated_clone_url(clone_url: str, token: str) -> str:
    """Return ``https://x-access-token:<token>@host/...`` for authenticated git clones."""
    raw = clone_url.strip()
    if not raw.startswith("https://"):
        raise ValueError(f"Expected https clone URL, got: {raw[:80]}")
    tail = raw[len("https://") :]
    safe_tok = urllib.parse.quote(token, safe="")
    return f"https://x-access-token:{safe_tok}@{tail}"


def git_clone_shallow_branch(clone_url: str, branch: str, dest: Path, *, timeout: int = 600) -> None:
    """``git clone --depth 1 --branch <branch> <url> <dest>``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        branch,
        clone_url,
        str(dest),
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"git clone timed out after {timeout}s") from exc
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"git clone failed (exit {completed.returncode}): {err[:4000]}")


@contextmanager
def temporary_pr_clone(clone_url: str, branch: str, token: str, *, clone_timeout: int = 600) -> Iterator[Path]:
    """Clone PR head into a temporary directory; yield the repository root; delete on exit."""
    tmp = Path(tempfile.mkdtemp(prefix="octo-spork-trivy-"))
    dest = tmp / "repo"
    try:
        auth_url = authenticated_clone_url(clone_url, token)
        git_clone_shallow_branch(auth_url, branch, dest, timeout=clone_timeout)
        yield dest.resolve()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _sarif_level_rank(level: str | None) -> int:
    normalized = str(level or "warning").lower()
    return {"error": 4, "warning": 3, "note": 2, "none": 1}.get(normalized, 2)


def _uri_to_repo_relative(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path or "")
        return path.lstrip("/")
    return uri.replace("\\", "/")


def _md_cell(text: str, *, max_len: int = 220) -> str:
    s = text.replace("\r\n", "\n").replace("\n", " ").strip()
    s = s.replace("|", "\\|")
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s or " "


def parse_sarif_to_markdown_table(
    sarif: dict[str, Any] | Path | str,
    *,
    limit: int = 40,
    heading: str = "### Trivy filesystem scan (SARIF)",
) -> str:
    """Turn Trivy SARIF JSON into a compact GitHub-flavored markdown table."""
    if isinstance(sarif, (str, Path)):
        path = Path(sarif)
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    else:
        payload = sarif

    rows: list[tuple[int, int, str, str, str, str]] = []
    seq = 0
    for run in payload.get("runs") or []:
        if not isinstance(run, dict):
            continue
        rules_map: dict[str, dict[str, Any]] = {}
        driver = ((run.get("tool") or {}).get("driver")) or {}
        for rule in driver.get("rules") or []:
            if isinstance(rule, dict) and rule.get("id"):
                rules_map[str(rule["id"])] = rule

        for result in run.get("results") or []:
            if not isinstance(result, dict):
                continue
            seq += 1
            level = str(result.get("level") or "warning")
            rank = _sarif_level_rank(level)
            rule_id = str(result.get("ruleId") or "")
            rule_meta = rules_map.get(rule_id) or {}
            short = rule_meta.get("shortDescription")
            short_txt = str(short.get("text") or "") if isinstance(short, dict) else ""
            rule_name = str(rule_meta.get("name") or short_txt or "")
            msg_obj = result.get("message")
            if isinstance(msg_obj, dict):
                message = str(msg_obj.get("text") or rule_id or "")
            else:
                message = str(msg_obj or "")
            message = message.strip()

            loc_label = "(no location)"
            locations = result.get("locations") or []
            if isinstance(locations, list) and locations:
                loc0 = locations[0] if isinstance(locations[0], dict) else {}
                phys = loc0.get("physicalLocation") if isinstance(loc0.get("physicalLocation"), dict) else {}
                region = phys.get("region") if isinstance(phys.get("region"), dict) else {}
                al = phys.get("artifactLocation") if isinstance(phys.get("artifactLocation"), dict) else {}
                uri = str(al.get("uri") or "")
                rel_path = _uri_to_repo_relative(uri) or uri or "(unknown file)"
                try:
                    line = int(region.get("startLine") or 0)
                except (TypeError, ValueError):
                    line = 0
                loc_label = f"`{rel_path}`:{line}" if line else str(rel_path)

            rid_disp = rule_id or "(rule)"
            display_rule = rid_disp + (
                f" — {rule_name}" if rule_name and rule_name not in rid_disp else ""
            )

            rows.append(
                (
                    -rank,
                    seq,
                    level.upper(),
                    display_rule,
                    loc_label,
                    message or "(no message)",
                )
            )

    rows.sort(key=lambda t: (t[0], t[1]))
    chosen = rows[: max(1, min(limit, 500))]

    lines = [
        heading,
        "",
        "_Command: `trivy fs --format sarif --output results.sarif ..` (run from a subdirectory of the clone)._",
        "",
    ]
    if not chosen:
        lines.append("_No SARIF results (clean scan or no findings reported)._")
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        [
            "| Severity | Rule | Location | Message |",
            "| --- | --- | --- | --- |",
        ]
    )
    for _, _, sev, display_rule, loc, msg in chosen:
        lines.append(
            f"| {_md_cell(sev, max_len=16)} | {_md_cell(display_rule, max_len=64)} | "
            f"{_md_cell(loc, max_len=120)} | {_md_cell(msg)} |"
        )
    if len(rows) > len(chosen):
        lines.append("")
        lines.append(f"_Showing {len(chosen)} of {len(rows)} SARIF results._")
    lines.append("")
    return "\n".join(lines)


def scan_pr_branch_to_markdown(
    *,
    clone_url: str,
    branch: str,
    token: str,
    scanner: TrivyScanner | None = None,
    repo_full_name: str | None = None,
    pr_html_url: str | None = None,
) -> str:
    """Clone ``branch`` from ``clone_url`` into a temp dir, run Trivy SARIF, return markdown summary."""
    if _env_truthy(_SKIP_ENV):
        return "_Trivy scan skipped (`OCTO_SPORK_SKIP_TRIVY`)._\n"

    sc = scanner or TrivyScanner()
    if not sc.available():
        return "_Trivy CLI not found on PATH; install [Trivy](https://aquasecurity.github.io/trivy/) or set `OCTO_SPORK_SKIP_TRIVY=1`._\n"

    try:
        with temporary_pr_clone(clone_url, branch, token) as repo_root:
            result = sc.run_fs_sarif(repo_root)
            raw = json.loads(result.sarif_path.read_text(encoding="utf-8", errors="replace"))
            table = parse_sarif_to_markdown_table(raw)
            if result.returncode != 0 and result.stderr:
                table += f"\n_trivy exited {result.returncode}; stderr (truncated): `{_md_cell(result.stderr, max_len=400)}`_\n"
            if repo_full_name and pr_html_url:
                try:
                    from github_bot.global_smell_index import ingest_sarif_findings

                    smell_md, _rec = ingest_sarif_findings(
                        "trivy",
                        raw,
                        repo_root,
                        repo_full_name=repo_full_name,
                        pr_html_url=pr_html_url,
                    )
                    if smell_md.strip():
                        table = table.rstrip() + "\n\n" + smell_md.strip() + "\n"
                except Exception as exc:
                    _LOG.debug("global smell index (trivy): %s", exc)
            return table + "\n"
    except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        _LOG.warning("Trivy PR scan failed: %s", exc)
        return f"_Trivy scan failed: `{exc}`_\n\n"
