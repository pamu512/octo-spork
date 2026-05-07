"""Ephemeral git clones for isolated remediation verification."""

from __future__ import annotations

import errno
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

_LOG = logging.getLogger(__name__)

SANDBOX_PATH = Path("/tmp/octo_verify")


def initialize_sandbox(repo_path: str) -> str:
    """Clone ``repo_path`` into ``/tmp/octo_verify`` and return that absolute path as a string.

    If ``/tmp/octo_verify`` already exists, it is removed first with :func:`shutil.rmtree`. The clone
    uses ``git clone --depth 1`` with a ``file://`` URL built from the resolved, absolute
    ``repo_path``.

    Raises
    ------
    FileNotFoundError
        If ``repo_path`` does not exist on disk before cloning.
    NotADirectoryError
        If ``repo_path`` is not a directory.
    PermissionError
        If the process cannot remove the previous sandbox, open the source tree, or write the
        destination because of access rights. This includes :exc:`OSError` values whose
        ``errno`` is :data:`errno.EACCES` or :data:`errno.EPERM` when those are the appropriate
        representation on the current platform, all re-wrapped with context.
    OSError
        For other platform errors while removing the old tree or launching ``git`` (for example
        ``EIO``, ``ENOSPC`` on the target filesystem, or :data:`errno.ENOTEMPTY` if removal races).
    subprocess.CalledProcessError
        If ``git clone`` exits with a non-zero status; ``stderr`` and ``stdout`` are available on
        the exception when ``capture_output=True``.
    """
    source = Path(repo_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"repository path does not exist: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"repository path is not a directory: {source}")

    sandbox = SANDBOX_PATH
    if sandbox.exists():
        try:
            shutil.rmtree(sandbox)
        except PermissionError as exc:
            raise PermissionError(
                f"cannot remove existing sandbox at {sandbox!s} (insufficient permissions): {exc}"
            ) from exc
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise PermissionError(
                    f"permission denied while removing existing sandbox {sandbox!s}: {exc}"
                ) from exc
            raise

    file_url = f"file://{source.as_posix()}"
    cmd = ["git", "clone", "--depth", "1", file_url, str(sandbox)]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except PermissionError as exc:
        raise PermissionError(
            f"permission denied while running git clone into {sandbox!s}: {exc}"
        ) from exc
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise PermissionError(
                f"permission denied starting git for clone into {sandbox!s}: {exc}"
            ) from exc
        _cleanup_partial_clone(sandbox)
        raise
    except subprocess.CalledProcessError:
        _cleanup_partial_clone(sandbox)
        raise
    except subprocess.SubprocessError:
        _cleanup_partial_clone(sandbox)
        raise

    return str(sandbox.resolve())


def apply_patch(sandbox_path: str, diff_content: str) -> bool:
    """Apply a unified diff inside ``sandbox_path`` using ``git apply``.

    Writes ``diff_content`` to a temporary path ending in ``.patch``, runs
    ``git apply <patch_file>`` with *cwd* set to the sandbox directory, deletes the patch file,
    and returns ``True`` only when ``git`` exits with status ``0``. On failure, captures stderr from
    ``git apply`` and emits it at ERROR level on this module's logger.
    """
    root = Path(sandbox_path).expanduser().resolve()
    if not root.is_dir():
        _LOG.error("apply_patch: sandbox path is not a directory: %s", root)
        return False

    patch_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".patch",
            delete=False,
        ) as tmp:
            tmp.write(diff_content)
            patch_file = Path(tmp.name)

        completed = subprocess.run(
            ["git", "apply", str(patch_file)],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        _LOG.error("apply_patch: failed to write patch or run git apply: %s", exc)
        return False
    finally:
        if patch_file is not None:
            patch_file.unlink(missing_ok=True)

    if completed.returncode == 0:
        return True

    err = (completed.stderr or "").strip()
    if err:
        _LOG.error(
            "git apply failed in %s (exit %s): %s",
            root,
            completed.returncode,
            err,
        )
    else:
        _LOG.error(
            "git apply failed in %s (exit %s) with no stderr output",
            root,
            completed.returncode,
        )
    return False


def _cleanup_partial_clone(sandbox: Path) -> None:
    if not sandbox.exists():
        return
    try:
        shutil.rmtree(sandbox)
    except (PermissionError, OSError):
        # Best effort: downstream callers already receive the primary failure from clone or spawn.
        pass
