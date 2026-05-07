"""Project Doctor: verify host binaries required for remediation and scans."""

from __future__ import annotations

import shutil
from typing import Final

# (executable name on PATH, recommended install line)
_DEPENDENCIES: Final[tuple[tuple[str, str], ...]] = (
    ("pytest", "python3 -m pip install pytest"),
    ("trivy", "brew install aquasecurity/tap/trivy"),
    ("bun", "curl -fsSL https://bun.sh/install | bash"),
)


def _print_missing_table(rows: list[tuple[str, str]]) -> None:
    col_bin = "Missing binary"
    col_cmd = "Recommended installation command"
    w_name = max(len(col_bin), max(len(r[0]) for r in rows))
    w_hint = max(len(col_cmd), max(len(r[1]) for r in rows))
    border = f"+-{'-' * w_name}-+-{'-' * w_hint}-+"
    print(border)
    print(f"| {col_bin:<{w_name}} | {col_cmd:<{w_hint}} |")
    print(border)
    for name, cmd in rows:
        print(f"| {name:<{w_name}} | {cmd:<{w_hint}} |")
    print(border)


def check_dependencies() -> bool:
    """Return ``True`` if ``pytest``, ``trivy``, and ``bun`` are on ``PATH``; otherwise print a table and return ``False``."""
    missing = [(name, hint) for name, hint in _DEPENDENCIES if shutil.which(name) is None]
    if missing:
        _print_missing_table(missing)
        return False
    return True
