"""Git clone helpers with disk-space checks and selective sparse checkout for large repositories.

Used for grounded-review flows that clone upstream repos (e.g. benchmarks). Shallow clones
use ``--depth 1`` and ``--filter=blob:none``. When the repository tree at ``HEAD`` is larger
than :attr:`IOThrottle.LARGE_REPO_THRESHOLD_BYTES`, only ``src/`` and ``config/`` are checked
out (\"Selective Fetch\") so local SSD is not filled by huge trees.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final, Iterable

_LOG = logging.getLogger(__name__)

_GIB: Final[float] = 1024.0**3


class IOThrottle:
    """Clone policy: shallow clone, free-space gate, optional sparse checkout for giant repos."""

    LARGE_REPO_THRESHOLD_BYTES: Final[int] = 500 * 1024 * 1024
    """Switch to Selective Fetch when the tree at ``HEAD`` is larger than this (bytes)."""

    SELECTIVE_FETCH_DIRS: Final[tuple[str, ...]] = ("src", "config")
    """Top-level paths to retain when Selective Fetch activates."""

    DEFAULT_MIN_FREE_BYTES: Final[int] = 2 * LARGE_REPO_THRESHOLD_BYTES
    """Require at least this much free space on the destination filesystem before cloning."""

    @classmethod
    def ensure_disk_space(
        cls,
        destination: Path,
        *,
        min_free_bytes: int | None = None,
    ) -> None:
        """Raise ``OSError`` if the filesystem hosting *destination* lacks free space."""
        parent = destination.expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        need = int(min_free_bytes if min_free_bytes is not None else cls.DEFAULT_MIN_FREE_BYTES)
        usage = shutil.disk_usage(parent)
        if usage.free < need:
            raise OSError(
                f"IOThrottle: insufficient disk space on {parent}: "
                f"{usage.free / _GIB:.2f} GiB free; need at least {need / _GIB:.2f} GiB "
                f"before cloning into {destination.name!r}."
            )

    @staticmethod
    def _run_git(
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = 600,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    @classmethod
    def _approx_tree_bytes_at_head(cls, repo: Path) -> int:
        """Sum blob sizes recorded in ``git ls-tree`` at ``HEAD`` (no blob fetch required)."""
        proc = cls._run_git(["git", "-C", str(repo), "ls-tree", "-r", "-l", "HEAD"], timeout=300)
        if proc.returncode != 0:
            _LOG.warning(
                "IOThrottle: ls-tree failed (%s); treating tree size as unknown",
                (proc.stderr or proc.stdout or "").strip()[:400],
            )
            return 0
        total = 0
        for line in (proc.stdout or "").splitlines():
            if "\t" not in line:
                continue
            meta, _path = line.split("\t", 1)
            toks = meta.split()
            # ``git ls-tree -l``: mode type object … size (last token before tab)
            if len(toks) >= 4:
                try:
                    total += int(toks[-1])
                except ValueError:
                    continue
        return total

    @classmethod
    def _root_entry_names(cls, repo: Path) -> set[str]:
        proc = cls._run_git(["git", "-C", str(repo), "ls-tree", "--name-only", "HEAD"], timeout=120)
        if proc.returncode != 0:
            return set()
        return {ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()}

    @classmethod
    def _existing_selective_paths(cls, repo: Path, wanted: Iterable[str]) -> list[str]:
        root = cls._root_entry_names(repo)
        return [w for w in wanted if w in root]

    @classmethod
    def _apply_selective_fetch(cls, repo: Path, paths: list[str]) -> None:
        if not paths:
            raise ValueError("Selective Fetch requires at least one path")
        init = cls._run_git(["git", "-C", str(repo), "sparse-checkout", "init", "--cone"], timeout=120)
        if init.returncode != 0:
            raise RuntimeError(
                f"git sparse-checkout init failed: {(init.stderr or init.stdout or '').strip()[:2000]}"
            )
        st = cls._run_git(["git", "-C", str(repo), "sparse-checkout", "set", *paths], timeout=300)
        if st.returncode != 0:
            raise RuntimeError(
                f"git sparse-checkout set failed: {(st.stderr or st.stdout or '').strip()[:2000]}"
            )

    @classmethod
    def clone_repository(
        cls,
        url: str,
        dest: Path,
        *,
        depth: int = 1,
        branch: str | None = None,
        min_free_bytes: int | None = None,
        large_threshold_bytes: int | None = None,
        timeout_sec: int = 600,
    ) -> dict[str, Any]:
        """
        Clone *url* into *dest* using a shallow, blob-filtered clone without checkout, measure the
        tree at ``HEAD``, optionally restrict checkout to :attr:`SELECTIVE_FETCH_DIRS`, then checkout.

        Returns metadata including approximate pre-checkout tree bytes and whether selective fetch ran.
        """
        dest = dest.expanduser().resolve()
        if dest.exists():
            raise FileExistsError(str(dest))

        cls.ensure_disk_space(dest, min_free_bytes=min_free_bytes)
        threshold = int(large_threshold_bytes if large_threshold_bytes is not None else cls.LARGE_REPO_THRESHOLD_BYTES)

        cmd: list[str] = [
            "git",
            "clone",
            "--depth",
            str(int(depth)),
            "--filter=blob:none",
            "--no-checkout",
        ]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([url, str(dest)])

        proc = cls._run_git(cmd, timeout=timeout_sec)
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {(proc.stderr or proc.stdout or '').strip()[:4000]}")

        approx = cls._approx_tree_bytes_at_head(dest)
        selective_paths = cls._existing_selective_paths(dest, cls.SELECTIVE_FETCH_DIRS)
        meta: dict[str, Any] = {
            "approx_tree_bytes_at_head": approx,
            "selective_fetch": False,
            "selective_paths": [],
        }

        use_selective = approx > threshold and bool(selective_paths)
        if approx > threshold and not selective_paths:
            _LOG.warning(
                "IOThrottle: tree ~%.1f MiB exceeds %d MiB but repo has no top-level %s — "
                "checking out full tree (higher disk use).",
                approx / (1024 * 1024),
                threshold // (1024 * 1024),
                list(cls.SELECTIVE_FETCH_DIRS),
            )

        if use_selective:
            _LOG.warning(
                "IOThrottle: Selective Fetch — tree ~%.1f MiB at HEAD exceeds %d MiB; "
                "checking out only %s",
                approx / (1024 * 1024),
                threshold // (1024 * 1024),
                selective_paths,
            )
            cls._apply_selective_fetch(dest, selective_paths)
            meta["selective_fetch"] = True
            meta["selective_paths"] = selective_paths

        checkout = cls._run_git(["git", "-C", str(dest), "checkout"], timeout=timeout_sec)
        if checkout.returncode != 0:
            raise RuntimeError(
                f"git checkout failed after clone: {(checkout.stderr or checkout.stdout or '').strip()[:4000]}"
            )

        meta["working_tree_bytes"] = cls.directory_tree_bytes(dest)
        return meta

    @staticmethod
    def directory_tree_bytes(root: Path) -> int:
        """Total byte size of regular files under *root* (best-effort; skips unreadable files)."""
        total = 0
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    st = fp.stat()
                    if st.is_file():
                        total += st.st_size
                except OSError:
                    continue
        return total
