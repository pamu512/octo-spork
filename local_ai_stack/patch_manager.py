"""Declarative AgenticSeek layering: ``git apply`` patches plus overlay copies / optional JSON Patch."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PatchConflictError(RuntimeError):
    """Raised when a unified diff cannot be applied cleanly (upstream drift)."""

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


class PatchManager:
    """Apply ``manifest.json`` from ``patches/agenticseek`` after cloning upstream."""

    def __init__(self, bundle_dir: Path, repo_root: Path | None = None) -> None:
        self.bundle_dir = Path(bundle_dir).resolve()
        self.repo_root = Path(repo_root).resolve() if repo_root else ROOT

    def apply(
        self,
        agenticseek_path: Path,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        """Run manifest steps in order; raises :class:`PatchConflictError` on failed ``git apply``."""
        manifest_path = self.bundle_dir / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"patch manifest missing: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        steps = manifest.get("steps")
        if not isinstance(steps, list):
            raise ValueError("manifest.steps must be a list")

        emit = progress or (lambda _s: None)

        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"manifest.steps[{idx}] must be an object")
            kind = step.get("type")
            note = step.get("note", "")
            label = f"[{idx + 1}/{len(steps)}] {kind}"
            emit(f"{label}" + (f" — {note}" if note else ""))

            if kind == "git_apply":
                rel = step.get("patch")
                if not rel or not isinstance(rel, str):
                    raise ValueError(f"git_apply step missing patch path at index {idx}")
                patch_file = self.bundle_dir / rel
                if not patch_file.is_file():
                    raise FileNotFoundError(f"patch file not found: {patch_file}")
                self._git_apply(agenticseek_path, patch_file)
            elif kind == "overlay_copy":
                src_rel = step.get("src")
                dst_rel = step.get("dst")
                if not src_rel or not dst_rel:
                    raise ValueError(f"overlay_copy missing src/dst at index {idx}")
                src = self.repo_root / src_rel
                dst = Path(agenticseek_path) / dst_rel
                if not src.is_file():
                    raise FileNotFoundError(f"overlay source missing: {src}")
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            elif kind == "jsonpatch":
                self._apply_jsonpatch_step(agenticseek_path, step, idx)
            else:
                raise ValueError(f"unknown manifest step type {kind!r} at index {idx}")

    def _git_apply(self, agenticseek_path: Path, patch_file: Path) -> None:
        """Verify then apply a unified diff with ``-p1`` path stripping."""
        chk = subprocess.run(
            ["git", "apply", "--check", "-p1", str(patch_file)],
            cwd=str(agenticseek_path),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if chk.returncode != 0:
            detail = (chk.stderr or "") + (chk.stdout or "")
            raise PatchConflictError(
                "git apply --check failed (upstream AgenticSeek likely changed — refresh patches "
                f"under {self.bundle_dir} or pin AGENTICSEEK_REF).\n{detail.strip()}",
                stderr=detail,
            )
        apl = subprocess.run(
            ["git", "apply", "-p1", str(patch_file)],
            cwd=str(agenticseek_path),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if apl.returncode != 0:
            detail = (apl.stderr or "") + (apl.stdout or "")
            raise PatchConflictError(
                f"git apply failed after successful check:\n{detail.strip()}",
                stderr=detail,
            )

    def _apply_jsonpatch_step(self, agenticseek_path: Path, step: dict[str, object], idx: int) -> None:
        try:
            import jsonpatch  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "jsonpatch package required for jsonpatch manifest steps. "
                "Install with: python3 -m pip install jsonpatch"
            ) from exc

        target_rel = step.get("target")
        ops_rel = step.get("ops_file")
        if not target_rel or not ops_rel:
            raise ValueError(f"jsonpatch step missing target or ops_file at index {idx}")
        target = Path(agenticseek_path) / target_rel
        ops_path = self.bundle_dir / ops_rel
        if not target.is_file():
            raise FileNotFoundError(f"jsonpatch target missing: {target}")
        if not ops_path.is_file():
            raise FileNotFoundError(f"jsonpatch ops file missing: {ops_path}")
        doc = json.loads(target.read_text(encoding="utf-8"))
        ops = json.loads(ops_path.read_text(encoding="utf-8"))
        patched = jsonpatch.apply_patch(doc, ops)
        target.write_text(json.dumps(patched, indent=2, sort_keys=False) + "\n", encoding="utf-8")
