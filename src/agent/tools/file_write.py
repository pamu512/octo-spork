"""Write text files with backups of previous on-disk contents."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_BACKUP_ROOT = Path("/tmp/octo_backups")


class AtomicWriteFailed(OSError):
    """Raised when :meth:`FileWriteTool.write_content` cannot finish or cannot restore a backup."""

    def __init__(self, message: str, *, filepath: str | None = None) -> None:
        self.filepath = filepath
        super().__init__(message)


class FileWriteTool:
    """Persist content to a path with atomic replace and backup rollback."""

    __slots__ = ("_pending_restore_backup",)

    def __init__(self) -> None:
        self._pending_restore_backup: Path | None = None

    def write_content(self, filepath: str, content: str) -> None:
        """Write ``content`` to ``filepath`` atomically using a temporary file.

        A backup of any existing regular file is taken before writing (see :meth:`_create_backup`).
        Content is written as UTF-8 to a temporary file in the target directory, then moved into
        place with :func:`os.replace` so readers observe either the previous file or the full new
        contents.

        Raises
        ------
        AtomicWriteFailed
            On any :exc:`OSError` or :exc:`IOError` during the write or replace steps. After such a
            failure, :meth:`_restore_backup` runs when a backup exists; the raised exception chains
            the original error unless restore also fails (then both are described in the message).
        """
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        backup_path = self._create_backup(filepath)
        self._pending_restore_backup = backup_path

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tf:
                tmp_path = Path(tf.name)
                tf.write(content)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
        except OSError as exc:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            try:
                self._restore_backup(filepath)
            except OSError as restore_exc:
                raise AtomicWriteFailed(
                    f"atomic write failed for {filepath!r}; backup restore failed: {restore_exc}",
                    filepath=filepath,
                ) from restore_exc
            raise AtomicWriteFailed(
                f"atomic write failed for {filepath!r}: {exc}",
                filepath=filepath,
            ) from exc
        finally:
            self._pending_restore_backup = None

    def _restore_backup(self, filepath: str) -> None:
        """Restore ``filepath`` from the backup created at the start of the current write attempt.

        Uses :attr:`_pending_restore_backup`, set by :meth:`write_content` before any temporary
        write. If no backup path is recorded or the backup file is missing, there is nothing to
        restore (for example the target did not exist prior to the failed write). Otherwise copies
        the backup onto ``filepath`` with :func:`shutil.copy2`. Propagates :exc:`OSError` /
        :exc:`IOError` from :func:`shutil.copy2` so callers do not treat restore as silent success.
        """
        bp = self._pending_restore_backup
        if bp is None or not bp.is_file():
            return
        shutil.copy2(bp, filepath)

    def _create_backup(self, filepath: str) -> Path | None:
        """Copy ``filepath`` into ``/tmp/octo_backups`` before it is overwritten.

        The destination is ``/tmp/octo_backups/<timestamp>_<basename>.bak`` where ``timestamp`` is
        UTC wall time formatted as ``YYYYMMDD_HHMMSS_microseconds``. The backup directory hierarchy
        is created with :func:`pathlib.Path.mkdir` using ``parents=True`` and ``exist_ok=True``.

        If ``filepath`` does not exist or cannot be read as the source for :func:`shutil.copy2`
        because it was removed concurrently, :exc:`FileNotFoundError` is handled and ``None`` is
        returned. Only existing regular files are copied; missing paths are skipped so new files do
        not require a prior artifact on disk.

        Returns
        -------
        pathlib.Path | None
            Path to the backup file when one was written; ``None`` when no backup was created.
        """
        src = Path(filepath)
        _BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        dest = _BACKUP_ROOT / f"{stamp}_{src.name}.bak"
        try:
            if not src.is_file():
                return None
            shutil.copy2(src, dest)
        except FileNotFoundError:
            return None
        return dest
