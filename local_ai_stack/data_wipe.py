"""Secure deletion for Docker bind-mount data under ``.local/data``."""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

_CHUNK = 1024 * 1024


def wipe_file(path: Path, *, passes: int = 1) -> None:
    """Overwrite *path* with random bytes (same length), sync, then unlink."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if size == 0:
        path.unlink(missing_ok=True)
        return
    for _ in range(max(1, passes)):
        with open(path, "r+b") as handle:
            remaining = size
            while remaining > 0:
                block = min(remaining, _CHUNK)
                handle.write(secrets.token_bytes(block))
                remaining -= block
            handle.flush()
            os.fsync(handle.fileno())
    path.unlink(missing_ok=True)


def wipe_directory_tree(root: Path, *, passes: int = 1) -> None:
    """Best-effort secure wipe of all regular files under *root*, then remove directories."""
    if not root.exists():
        return
    root = root.resolve()
    if not root.is_dir():
        wipe_file(root, passes=passes)
        return

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        base = Path(dirpath)
        for name in filenames:
            candidate = base / name
            try:
                if candidate.is_symlink():
                    candidate.unlink(missing_ok=True)
                elif candidate.is_file():
                    wipe_file(candidate, passes=passes)
            except OSError:
                continue
        for name in dirnames:
            sub = base / name
            try:
                if sub.is_symlink():
                    sub.unlink(missing_ok=True)
                elif sub.is_dir():
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
            except OSError:
                continue

    try:
        root.rmdir()
    except OSError:
        shutil.rmtree(root, ignore_errors=True)
