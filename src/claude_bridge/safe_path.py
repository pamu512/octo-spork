"""Sandbox **SafePath** guard for file-edit operations in the Octo-spork bridge.

Use :class:`SafePathMiddleware` before forwarding paths to Claude Code’s edit surface (e.g. FileEdit)
or any host-side tool that mutates files. The guard:

- Resolves the path and requires it to stay **inside** the configured repository root (no ``..`` /
  symlink escape from the repo).
- **Denies** edits to:

  - any file named ``.env``;
  - common Docker Compose filenames (``docker-compose.yml``, ``docker-compose.yaml``,
    ``compose.yml``, ``compose.yaml``);
  - the entire tree ``src/github_bot/`` (orchestrator / GitHub bot — must not be modified by a
    sandboxed agent).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "COMPOSE_FILE_NAMES",
    "SafePathMiddleware",
    "SafePathViolation",
    "is_edit_path_allowed",
    "resolve_under_repo",
]


class SafePathViolation(PermissionError):
    """Raised when a path is outside the repo or matches a blocked pattern."""


# Filenames (case-insensitive match on basename) for compose roots we never sandbox-edit.
COMPOSE_FILE_NAMES: frozenset[str] = frozenset(
    {
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
    }
)


def _blocked_github_bot_rel(rel: Path) -> bool:
    parts = rel.parts
    return len(parts) >= 2 and parts[0] == "src" and parts[1] == "github_bot"


def resolve_under_repo(repo_root: Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` to an absolute path; relative paths are taken relative to ``repo_root``."""
    repo = repo_root.expanduser().resolve()
    raw = Path(candidate)
    return (raw if raw.is_absolute() else (repo / raw)).resolve()


def is_edit_path_allowed(repo_root: Path, candidate: str | Path, *, strict: bool = True) -> tuple[bool, str]:
    """Return ``(True, "")`` if edits are allowed, else ``(False, reason)``.

    When ``strict`` is False, only log-friendly checks are skipped (same logic).
    """
    del strict  # reserved for future relaxed modes
    repo = repo_root.expanduser().resolve()
    if not repo.is_dir():
        return False, f"repository root is not a directory: {repo}"

    try:
        path = resolve_under_repo(repo, candidate)
    except OSError as exc:
        return False, f"invalid path: {exc}"

    try:
        rel = path.relative_to(repo)
    except ValueError:
        return False, "path escapes repository sandbox (outside target repo)"

    name_lower = path.name.lower()
    if path.name == ".env" or name_lower == ".env":
        return False, "editing `.env` is blocked (secrets / environment orchestration)"

    if name_lower in {n.lower() for n in COMPOSE_FILE_NAMES}:
        return False, f"editing `{path.name}` is blocked (stack orchestration)"

    if _blocked_github_bot_rel(rel):
        return False, "editing under `src/github_bot/` is blocked (orchestrator code)"

    return True, ""


@dataclass(frozen=True)
class SafePathMiddleware:
    """Callable middleware: ``middleware(path)`` returns the resolved path or raises."""

    repo_root: Path

    def __call__(self, candidate: str | Path) -> Path:
        return self.assert_allowed(candidate)

    def assert_allowed(self, candidate: str | Path) -> Path:
        """Resolve ``candidate`` under :attr:`repo_root` or raise :exc:`SafePathViolation`."""
        ok, reason = is_edit_path_allowed(self.repo_root, candidate)
        if not ok:
            raise SafePathViolation(reason)
        return resolve_under_repo(self.repo_root, candidate)

    def check(self, candidate: str | Path) -> bool:
        ok, _ = is_edit_path_allowed(self.repo_root, candidate)
        return ok

    def filter_paths(self, paths: list[str | Path]) -> list[Path]:
        """Return only paths that pass the guard; drops blocked paths (no exception)."""
        out: list[Path] = []
        for p in paths:
            ok, _ = is_edit_path_allowed(self.repo_root, p)
            if ok:
                out.append(self.assert_allowed(p))
        return out
