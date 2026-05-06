#!/usr/bin/env python3
"""Wrapper so you can run ``scripts/ollama_vram_guard.py`` without installing the package."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from ollama_guard.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
