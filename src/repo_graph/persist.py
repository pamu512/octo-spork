"""JSON persistence for :class:`RepoGraph`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from repo_graph.graph import RepoGraph, graph_from_jsonable, graph_to_jsonable


def default_data_dir() -> Path:
    env = os.environ.get("OCTO_REPO_GRAPH_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        root = Path(base) / "octo-spork" / "repo_graph"
    else:
        root = Path.home() / ".local" / "share" / "octo-spork" / "repo_graph"
    return root


def cache_path_for(scan_root: Path, revision_hint: str) -> Path:
    import hashlib

    key = f"{scan_root.resolve()}::{revision_hint}".encode("utf-8")
    short = hashlib.sha256(key).hexdigest()[:20]
    safe_rev = "".join(c if c.isalnum() or c in "-._" else "_" for c in revision_hint)[:48]
    name = f"{short}_{safe_rev or 'norev'}.json"
    return default_data_dir() / name


def save_graph(path: Path, graph: RepoGraph) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = graph_to_jsonable(graph)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_graph(path: Path) -> RepoGraph | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return graph_from_jsonable(data)


def try_load_cached(scan_root: Path, revision_hint: str) -> RepoGraph | None:
    from repo_graph.constants import use_graph_cache

    if not use_graph_cache():
        return None
    p = cache_path_for(scan_root, revision_hint)
    g = load_graph(p)
    if g is None:
        return None
    if Path(g.scan_root).resolve() != scan_root.resolve():
        return None
    return g
