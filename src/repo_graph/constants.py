"""Shared limits and ignore lists for RepoGraph scans."""

from __future__ import annotations

import os

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "dist",
        "build",
        ".next",
        ".tox",
        "coverage",
        "htmlcov",
        ".mypy_cache",
        ".pytest_cache",
    }
)

TS_JS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

MAX_FILES = max(50, int(os.environ.get("OCTO_REPO_GRAPH_MAX_FILES", "500")))
MAX_EDGES_STORE = max(100, int(os.environ.get("OCTO_REPO_GRAPH_MAX_EDGES", "8000")))
SUMMARY_MAX_CHARS = min(12000, int(os.environ.get("OCTO_REPO_GRAPH_SUMMARY_CHARS", "6500")))

def graph_enabled() -> bool:
    return os.environ.get("OCTO_REPO_GRAPH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def use_graph_cache() -> bool:
    return os.environ.get("OCTO_REPO_GRAPH_CACHE", "true").lower() in {"1", "true", "yes", "on"}
