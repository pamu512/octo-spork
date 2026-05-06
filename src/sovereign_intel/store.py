"""JSON persistence for cross-repository Critical credential-pattern intelligence."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_LOCK = threading.Lock()


def workspace_data_dir() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    base = Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()
    return base / ".local" / "sovereign_intel"


def network_store_path() -> Path:
    return workspace_data_dir() / "network.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_network() -> dict[str, Any]:
    path = network_store_path()
    if not path.is_file():
        return {"version": 1, "patterns": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"version": 1, "patterns": {}}
    if not isinstance(data, dict):
        return {"version": 1, "patterns": {}}
    data.setdefault("version", 1)
    data.setdefault("patterns", {})
    if not isinstance(data["patterns"], dict):
        data["patterns"] = {}
    return data


def _save_network(data: dict[str, Any]) -> None:
    path = network_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def record_critical_pattern_hits(repo_root: Path, pattern_names: Iterable[str]) -> None:
    """Record pattern names that matched at Critical severity in ``repo_root``."""
    root_s = str(repo_root.expanduser().resolve())
    names = [str(n).strip() for n in pattern_names if str(n).strip()]
    if not names:
        return

    with _LOCK:
        data = load_network()
        patterns: dict[str, Any] = data["patterns"]
        now = _utc_now()
        for pname in names:
            entry = patterns.get(pname)
            if not isinstance(entry, dict):
                entry = {"severity": "critical", "sources": []}
            sources = entry.setdefault("sources", [])
            if not isinstance(sources, list):
                sources = []
                entry["sources"] = sources
            found = False
            for s in sources:
                if isinstance(s, dict) and s.get("repo") == root_s:
                    s["last_seen"] = now
                    found = True
                    break
            if not found:
                sources.append({"repo": root_s, "first_seen": now, "last_seen": now})
            entry["severity"] = "critical"
            patterns[pname] = entry
        data["updated_at"] = now
        _save_network(data)


def pattern_names_from_other_repos(repo_root: Path) -> list[str]:
    """Pattern names with at least one Critical hit recorded from a **different** local clone path."""
    mine = repo_root.expanduser().resolve()
    data = load_network()
    patterns = data.get("patterns") or {}
    if not isinstance(patterns, dict):
        return []
    out: list[str] = []
    for pname, meta in patterns.items():
        if not isinstance(meta, dict):
            continue
        sources = meta.get("sources")
        if not isinstance(sources, list):
            continue
        for s in sources:
            if not isinstance(s, dict):
                continue
            r = s.get("repo")
            if not isinstance(r, str) or not r.strip():
                continue
            try:
                other = Path(r).expanduser().resolve()
            except OSError:
                continue
            if other != mine:
                out.append(str(pname))
                break
    return sorted(set(out))


def all_pattern_repo_matrix() -> dict[str, list[str]]:
    """``pattern_name -> list of repo paths`` for CLI comparison."""
    data = load_network()
    patterns = data.get("patterns") or {}
    matrix: dict[str, list[str]] = {}
    if not isinstance(patterns, dict):
        return matrix
    for pname, meta in patterns.items():
        if not isinstance(meta, dict):
            continue
        repos: list[str] = []
        for s in meta.get("sources") or []:
            if isinstance(s, dict) and isinstance(s.get("repo"), str):
                repos.append(s["repo"])
        if repos:
            matrix[str(pname)] = sorted(set(repos))
    return matrix
