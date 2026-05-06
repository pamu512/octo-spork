"""Validate agent-produced unified diffs by applying them to a shallow clone."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger(__name__)

_DEFAULT_VERIFY_ROOT = Path("/tmp/octo_verify")


@dataclass(frozen=True)
class PatchValidationResult:
    """Outcome of :meth:`PatchValidator.validate`.

    On failure, :attr:`stderr` holds ``git clone`` or ``git apply`` diagnostics for LLM feedback.
    """

    success: bool
    stderr: str = ""
    """Captured stderr from the failing ``git`` invocation (empty when ``success`` is True)."""

    @property
    def clean(self) -> bool:
        """True when apply succeeded (and any ``on_apply_success`` hook completed without error)."""
        return self.success


class PatchValidator:
    """Apply a Claude/agent unified diff to a disposable shallow clone and report success.

    Workflow:

    1. **Shallow clone** the source repository under ``/tmp/octo_verify`` (configurable).
    2. Run **git apply** for the supplied diff text inside the clone.
    3. Return :class:`PatchValidationResult` with a boolean and captured stderr on failure.

    Environment:

    - ``OCTO_PATCH_VERIFY_ROOT`` — override directory for verification workspaces (default
      ``/tmp/octo_verify``).
    """

    def __init__(
        self,
        repo_path: Path | str,
        *,
        verify_root: Path | str | None = None,
    ) -> None:
        self._repo_path = Path(repo_path).expanduser().resolve()
        raw = verify_root if verify_root is not None else os.environ.get("OCTO_PATCH_VERIFY_ROOT", "")
        self._verify_root = (
            Path(raw).expanduser().resolve()
            if str(raw).strip()
            else Path(_DEFAULT_VERIFY_ROOT)
        )

    def validate(
        self,
        diff_text: str,
        *,
        on_apply_success: Callable[[Path], None] | None = None,
    ) -> PatchValidationResult:
        """Clone shallowly, apply *diff_text*, return success and stderr if ``git apply`` fails.

        If apply succeeds and *on_apply_success* is set, it is called with the clone directory
        before the temporary workspace is deleted.
        """
        if not self._repo_path.exists():
            return PatchValidationResult(False, stderr=f"repository path does not exist: {self._repo_path}")
        git_meta = self._repo_path / ".git"
        if not git_meta.exists():
            return PatchValidationResult(
                False,
                stderr=f"not a git repository (missing .git): {self._repo_path}",
            )

        self._verify_root.mkdir(parents=True, exist_ok=True)
        work = Path(
            tempfile.mkdtemp(prefix="octo-patch-", dir=str(self._verify_root)),
        )
        clone_dest = work / "repo"
        patch_path = work / "agent.patch"

        try:
            clone_err = self._git_clone_shallow(clone_dest)
            if clone_err is not None:
                return PatchValidationResult(False, stderr=clone_err)

            patch_path.write_text(diff_text, encoding="utf-8")

            proc = subprocess.run(
                ["git", "-C", str(clone_dest), "apply", "--verbose", str(patch_path)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            err_out = (proc.stderr or "").strip()
            std_out = (proc.stdout or "").strip()
            if proc.returncode == 0:
                if on_apply_success is not None:
                    on_apply_success(clone_dest)
                return PatchValidationResult(True)

            feedback = err_out or std_out or f"git apply exited with code {proc.returncode}"
            _LOG.debug("git apply failed: %s", feedback[:2000])
            return PatchValidationResult(False, stderr=feedback)
        finally:
            try:
                shutil.rmtree(work, ignore_errors=True)
            except OSError as exc:
                _LOG.warning("could not remove patch verify workspace %s: %s", work, exc)

    def _git_clone_shallow(self, dest: Path) -> str | None:
        """Return stderr message on failure, or ``None`` on success."""
        src = str(self._repo_path)
        proc = subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--single-branch",
                src,
                str(dest),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if proc.returncode == 0:
            return None
        err = (proc.stderr or "").strip()
        out = (proc.stdout or "").strip()
        combined = "\n".join(x for x in (err, out) if x)
        return combined or f"git clone failed with exit code {proc.returncode}"
