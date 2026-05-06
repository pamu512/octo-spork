"""Enumerate Python / TS / JS source files under a scan root."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from repo_graph.constants import MAX_FILES, SKIP_DIR_NAMES, TS_JS_EXTENSIONS


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.startswith(".")


def discover_source_files(scan_root: Path, *, max_files: int = MAX_FILES) -> list[Path]:
    """Return sorted source paths (``.py`` + TS/JS), capped by ``max_files``."""
    scan_root = scan_root.expanduser().resolve()
    collected_rg: list[Path] = []
    rg = shutil.which("rg")
    if rg:
        try:
            completed = subprocess.run(
                [
                    rg,
                    "--files",
                    "-g",
                    "*.py",
                    "-g",
                    "*.ts",
                    "-g",
                    "*.tsx",
                    "-g",
                    "*.js",
                    "-g",
                    "*.jsx",
                    "-g",
                    "*.mjs",
                    str(scan_root),
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                paths: list[Path] = []
                for line in completed.stdout.splitlines():
                    p = Path(line.strip())
                    if p.is_file():
                        paths.append(p)
                paths.sort(key=lambda x: str(x))
                for p in paths:
                    if any(part in SKIP_DIR_NAMES for part in p.parts):
                        continue
                    suf = p.suffix.lower()
                    if suf == ".py" or suf in TS_JS_EXTENSIONS:
                        collected_rg.append(p)
                    if len(collected_rg) >= max_files:
                        break
                collected_rg.sort(key=lambda x: str(x))
                return collected_rg[:max_files]
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass

    collected: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(scan_root, followlinks=False):
        dirnames[:] = [d for d in sorted(dirnames) if not _should_skip_dir(d)]
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            suf = p.suffix.lower()
            if suf == ".py" or suf in TS_JS_EXTENSIONS:
                collected.append(p)
            if len(collected) >= max_files:
                break
        if len(collected) >= max_files:
            break
    collected.sort(key=lambda x: str(x))
    return collected[:max_files]
