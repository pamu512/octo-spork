"""Command validation middleware: block obviously destructive shell patterns."""

from __future__ import annotations

import re
from typing import Final

# Destructive shell idioms (compiled once at import). Patterns are conservative heuristics;
# false positives are preferable to allowing filesystem or disk-wiping commands.
_BLOCKLIST_PATTERN_SOURCES: Final[tuple[str, ...]] = (
    # rm with recursive+force targeting absolute paths (e.g. rm -rf /, sudo rm -rf /boot)
    r"\b(?:sudo\s+)?rm\s+(?:-[^\s\n]*[rR][^\s\n]*[fF][^\s\n]*|-[^\s\n]*rf[^\s\n]*|-[^\s\n]*fr[^\s\n]*)\s+/",
    r"\b(?:sudo\s+)?rm\s+(?:-[a-zA-Z]+\s+)+/\s",
    # mkfs / filesystem formatters
    r"\bmkfs(?:\.\w+)?\b",
    r"\bwipefs\b",
    # Redirect or stream into block devices
    r"[><]\s*/dev/(?:sd[a-z]*|hd[a-z]|nvme\d+n\d+|mmcblk\d+|vd[a-z]|fd\d+|loop\d+)",
    # Remote code execution via curl/wget piped to a shell
    r"\bcurl\b[^\n]*\|\s*(?:bash|sh)\b",
    r"\bwget\b[^\n]*\|\s*(?:bash|sh)\b",
    # dd directly onto raw devices
    r"\bdd\b[^\n]*\bof=/dev/(?:sd|hd|nvme|mmcblk|vd|loop)[^\s]*",
    # Classic fork bomb
    r":\(\)\{\s*:\|:&\s*\};:",
    # Recursive permission blast on filesystem root
    r"\bchmod\s+-R\s+\d{3,4}\s+/",
)

_COMPILED_BLOCKLIST: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE) for pattern in _BLOCKLIST_PATTERN_SOURCES
)


class SecurityViolationError(Exception):
    """Raised when :meth:`SecurityValidator.validate_command` matches a blocked pattern."""

    def __init__(self, matched_pattern: str, *, command: str | None = None) -> None:
        self.matched_pattern = matched_pattern
        self.command = command
        detail = f"blocked by pattern {matched_pattern!r}"
        if command is not None:
            detail = f"{detail}; command={command!r}"
        super().__init__(detail)


class SecurityValidator:
    """Reject commands that match known destructive shell signatures."""

    __slots__ = ("_patterns",)

    def __init__(self) -> None:
        self._patterns: tuple[re.Pattern[str], ...] = _COMPILED_BLOCKLIST

    def validate_command(self, command: str) -> bool:
        """Return ``True`` if ``command`` is allowed.

        If any hardcoded blocklist regular expression matches ``command``,
        raises :exc:`SecurityViolationError` with :attr:`SecurityViolationError.matched_pattern`
        set to the **regex source string** of the rule that matched (not the substring match).

        Parameters
        ----------
        command
            Full command line string as issued to the terminal tool.

        Returns
        -------
        bool
            ``True`` when no blocklist pattern matches.

        Raises
        ------
        SecurityViolationError
            When a destructive pattern matches.
        """
        for compiled in self._patterns:
            if compiled.search(command):
                raise SecurityViolationError(compiled.pattern, command=command)
        return True
