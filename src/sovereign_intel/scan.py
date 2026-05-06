"""Scan local worktrees for a subset of credential patterns (fleet priority)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from github_bot.secret_scan import SecretFinding, scan_diff_text, scan_text_for_pattern_names

_TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".json",
        ".yml",
        ".yaml",
        ".toml",
        ".md",
        ".sh",
        ".env",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".cs",
        ".php",
    }
)

_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "venv",
        ".venv",
        "dist",
        "build",
        "__pycache__",
        ".tox",
    }
)


def _git_ls_files(repo: Path) -> list[Path] | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    raw = completed.stdout.split(b"\0")
    out: list[Path] = []
    for chunk in raw:
        if not chunk.strip():
            continue
        try:
            rel = chunk.decode("utf-8", errors="replace")
        except Exception:
            continue
        p = (repo / rel).resolve()
        try:
            p.relative_to(repo.resolve())
        except ValueError:
            continue
        if p.is_file():
            out.append(p)
    return out


def _walk_files(repo: Path, *, max_files: int) -> list[Path]:
    collected: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        for fn in filenames:
            if len(collected) >= max_files:
                return collected
            p = Path(dirpath) / fn
            if p.suffix.lower() in _TEXT_EXTENSIONS or p.suffix == "":
                if p.is_file():
                    collected.append(p)
    return collected


def scan_worktree_for_patterns(
    repo_root: Path,
    pattern_names: set[str],
    *,
    max_total_bytes: int = 2_000_000,
    max_files: int = 800,
) -> list[SecretFinding]:
    """Read text files under ``repo_root`` and run fleet pattern subset scan."""
    root = repo_root.expanduser().resolve()
    if not root.is_dir():
        return []

    paths = _git_ls_files(root)
    if paths is None:
        paths = _walk_files(root, max_files=max_files)

    paths.sort(key=lambda x: str(x))
    blob_parts: list[str] = []
    total = 0
    for p in paths[:max_files]:
        suf = p.suffix.lower()
        if suf and suf not in _TEXT_EXTENSIONS:
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        rel = ""
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            rel = str(p)
        blob_parts.append(f"\n\n===== FILE {rel} =====\n\n{text}")
        total += len(blob_parts[-1])
        if total >= max_total_bytes:
            break

    combined = "".join(blob_parts)
    return scan_text_for_pattern_names(combined, pattern_names, max_findings=64)


def ingest_repo_scan(repo_root: Path, *, max_total_bytes: int = 4_000_000) -> list[SecretFinding]:
    """Full secret regex scan over worktree text (used by CLI ``ingest``)."""
    root = repo_root.expanduser().resolve()
    if not root.is_dir():
        return []

    paths = _git_ls_files(root) or _walk_files(root, max_files=1200)
    paths.sort(key=lambda x: str(x))
    parts: list[str] = []
    total = 0
    for p in paths:
        suf = p.suffix.lower()
        if suf and suf not in _TEXT_EXTENSIONS and suf not in {".svg", ".html", ".css"}:
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            continue
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            rel = str(p)
        parts.append(f"\n\n===== {rel} =====\n\n{text}")
        total += len(parts[-1])
        if total >= max_total_bytes:
            break
    return scan_diff_text("".join(parts))
