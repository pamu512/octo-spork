"""Comment placeholders for pruned regions."""

from __future__ import annotations

from pathlib import Path


def omitted_comment(*, path: Path | None = None, language: str | None = None) -> str:
    """Line comment used where code was removed (language-aware)."""
    suf = (path.suffix.lower() if path else "") or ""
    lang = (language or "").lower()
    if suf == ".py" or lang == "python":
        return "# ... code omitted for context"
    return "// ... code omitted for context"
