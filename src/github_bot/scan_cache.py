"""Persistent disk cache for security scanner SARIF keyed by repository + git commit."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_LOG = logging.getLogger(__name__)


def scan_cache_enabled() -> bool:
    return os.environ.get("OCTO_SPORK_SCAN_CACHE", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def scan_cache_root() -> Path:
    raw = (os.environ.get("OCTO_SPORK_SCAN_CACHE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    root = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if root:
        return Path(root).expanduser().resolve() / ".octo-spork" / "scan-cache"
    return Path.cwd() / ".octo-spork" / "scan-cache"


def _safe_repo_segment(repo_full_name: str) -> str:
    s = repo_full_name.strip().replace("/", "__")
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


def _sha_segment(commit_sha: str) -> str:
    return hashlib.sha256(commit_sha.strip().encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class ScanCacheKey:
    repo_full_name: str
    commit_sha: str
    scanner: str  # "trivy" | "codeql"


class ScanCache:
    """Filesystem-backed SARIF JSON cache keyed by (repo, commit, scanner)."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or scan_cache_root()).expanduser().resolve()

    def _path(self, key: ScanCacheKey) -> Path:
        repo = _safe_repo_segment(key.repo_full_name or "unknown")
        sub = f"{key.scanner}_{_sha_segment(key.commit_sha)}"
        return self._root / repo / sub / f"{key.commit_sha}.sarif.json"

    def get_sarif(self, key: ScanCacheKey) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            return json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("scan_cache: corrupt entry %s: %s", path, exc)
            return None

    def put_sarif(self, key: ScanCacheKey, sarif: dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".sarif.json.tmp")
        payload = json.dumps(sarif, ensure_ascii=False, separators=(",", ":"))
        tmp.write_text(payload + "\n", encoding="utf-8")
        tmp.replace(path)


def normalize_repo_relative_path(uri_or_path: str) -> str:
    """Normalize SARIF artifact uri / repo path for comparisons."""
    if not uri_or_path:
        return ""
    u = uri_or_path.strip().replace("\\", "/")
    if u.startswith("file://"):
        parsed = urlparse(u)
        path = unquote(parsed.path or "")
        u = path.lstrip("/")
    while u.startswith("./"):
        u = u[2:]
    return u.strip()


def sarif_primary_location_path(result: dict[str, Any]) -> str:
    """Best-effort primary artifact path for a SARIF result."""
    locs = result.get("locations") or []
    if isinstance(locs, list) and locs:
        loc0 = locs[0] if isinstance(locs[0], dict) else {}
        phys = loc0.get("physicalLocation") if isinstance(loc0.get("physicalLocation"), dict) else {}
        al = phys.get("artifactLocation") if isinstance(phys.get("artifactLocation"), dict) else {}
        uri = str(al.get("uri") or "")
        return normalize_repo_relative_path(uri)
    return ""


def iter_sarif_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for run in payload.get("runs") or []:
        if not isinstance(run, dict):
            continue
        for r in run.get("results") or []:
            if isinstance(r, dict):
                out.append(r)
    return out


def _result_dedupe_key(r: dict[str, Any]) -> tuple[str, str, int, str]:
    rid = str(r.get("ruleId") or "")
    path = sarif_primary_location_path(r)
    loc_line = 0
    locs = r.get("locations") or []
    if isinstance(locs, list) and locs:
        loc0 = locs[0] if isinstance(locs[0], dict) else {}
        phys = loc0.get("physicalLocation") if isinstance(loc0.get("physicalLocation"), dict) else {}
        region = phys.get("region") if isinstance(phys.get("region"), dict) else {}
        try:
            loc_line = int(region.get("startLine") or 0)
        except (TypeError, ValueError):
            loc_line = 0
    msg_obj = r.get("message")
    if isinstance(msg_obj, dict):
        msg = str(msg_obj.get("text") or "")
    else:
        msg = str(msg_obj or "")
    msg = msg[:240]
    return (rid, path, loc_line, msg)


def merge_sarif_base_and_delta(
    base_sarif: dict[str, Any],
    delta_sarif: dict[str, Any],
    *,
    changed_paths: set[str],
) -> dict[str, Any]:
    """Combine cached **base** scan with **delta** scan at PR head.

    Keeps base findings for paths **not** in ``changed_paths`` and adds all findings from
    ``delta_sarif`` (typically an incremental scan over changed paths at head).

    ``delta_sarif`` supplies the shell (``$schema``, ``runs[].tool``) when possible.
    """
    changed = {normalize_repo_relative_path(p) for p in changed_paths if p}

    base_results = iter_sarif_results(base_sarif)
    kept = [deepcopy(r) for r in base_results if sarif_primary_location_path(r) not in changed]

    delta_results = iter_sarif_results(delta_sarif)
    merged_list = kept + [deepcopy(r) for r in delta_results]

    seen: set[tuple[str, str, int, str]] = set()
    deduped: list[dict[str, Any]] = []
    for r in merged_list:
        key = _result_dedupe_key(r)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    shell = deepcopy(delta_sarif)
    if not isinstance(shell.get("runs"), list) or not shell["runs"]:
        shell = deepcopy(base_sarif)
    if not isinstance(shell.get("runs"), list) or not shell["runs"]:
        shell = {
            "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
            "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "octo-spork-merge"}}, "results": []}],
        }
    runs = shell["runs"]
    if not isinstance(runs, list) or not runs:
        shell["runs"] = [{"tool": {"driver": {"name": "octo-spork-merge"}}, "results": []}]
        runs = shell["runs"]
    if not isinstance(runs[0], dict):
        runs[0] = {}
    run0 = runs[0]
    run0["results"] = deduped
    return shell


def rebuild_single_run_sarif(template: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace ``runs[0].results`` while preserving tool metadata from ``template``."""
    shell = deepcopy(template)
    runs = shell.setdefault("runs", [])
    if not runs:
        runs.append({})
    if not isinstance(runs[0], dict):
        runs[0] = {}
    runs[0]["results"] = results
    return shell


def clear_repo_scanner(repo_full_name: str, scanner: str) -> None:
    """Delete all cached commits for a repo + scanner (admin)."""
    root = scan_cache_root() / _safe_repo_segment(repo_full_name)
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.is_dir() and child.name.startswith(f"{scanner}_"):
            shutil.rmtree(child, ignore_errors=True)
