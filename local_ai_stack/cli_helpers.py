"""Shared CLI print and repo-path helpers for the local AI stack.

Pure move from ``__main__``: function bodies are byte-identical to their
previous definitions in ``__main__``, which re-exports them for backward
compatibility.
"""
from __future__ import annotations

from pathlib import Path


def _print(message: str) -> None:
    print(message, flush=True)


def _repo_path_from_arg(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()
