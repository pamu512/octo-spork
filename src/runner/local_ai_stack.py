"""Bind the remediation LangGraph LLM to Ollama with unified-memory-aware model selection."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# Support `python …/src/runner/local_ai_stack.py …` without requiring PYTHONPATH=src.
_SRC_ROOT = Path(__file__).resolve().parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from infra.vram_monitor import check_memory_pressure

_FALLBACK_SMALL_MODEL = "qwen2.5-coder:7b"
_DEFAULT_32B_MODEL = (
    os.environ.get("OCTO_DEFAULT_32B_MODEL", "qwen2.5-coder:32b").strip() or "qwen2.5-coder:32b"
)


def _memory_pressure_fallback_banner(selected_model: str, default_model: str) -> None:
    """Emit a highly visible stderr warning when the small model is selected."""

    border = "=" * 76
    lines = (
        border,
        "  WARNING: UNIFIED MEMORY PRESSURE — FALLBACK MODEL ACTIVE",
        "",
        f"  Host memory pressure is HIGH (swap-backed pressure detected). The stack is using",
        f"  '{selected_model}' instead of the default large model '{default_model}'.",
        "",
        "  Expect faster, lower-footprint runs with reduced reasoning depth. Free RAM or swap,",
        "  then restart this process to return to the 32B profile.",
        border,
    )
    for ln in lines:
        print(ln, file=sys.stderr)


def build_langgraph_chat_model(
    *,
    ollama_base_url: str | None = None,
    temperature: float = 0.2,
) -> Any:
    """Construct the chat model used by the remediation LangGraph **before** the graph is compiled.

    Calls :func:`infra.vram_monitor.check_memory_pressure`. Returns a LangChain **ChatOllama**
    instance targeting ``qwen2.5-coder:7b`` when pressure is ``\"HIGH\"``, otherwise
    ``qwen2.5-coder:32b`` (override via ``OCTO_DEFAULT_32B_MODEL``).

    Raises
    ------
    ImportError
        If neither ``langchain_ollama`` nor ``langchain_community`` provides ``ChatOllama``.
    """
    pressure = check_memory_pressure()
    base = (
        (ollama_base_url or os.environ.get("OLLAMA_LOCAL_URL") or os.environ.get("OLLAMA_BASE_URL") or "")
        .strip()
        or "http://127.0.0.1:11434"
    ).rstrip("/")

    if pressure == "HIGH":
        model_name = _FALLBACK_SMALL_MODEL
        _memory_pressure_fallback_banner(model_name, _DEFAULT_32B_MODEL)
    else:
        model_name = _DEFAULT_32B_MODEL

    try:
        from langchain_ollama import ChatOllama
    except ImportError:  # pragma: no cover - optional dependency path
        try:
            from langchain_community.chat_models import ChatOllama
        except ImportError as exc:
            raise ImportError(
                "Install langchain-ollama (recommended) or langchain-community to use ChatOllama."
            ) from exc

    return ChatOllama(
        model=model_name,
        base_url=base,
        temperature=temperature,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="local_ai_stack",
        description="Local AI stack runner entrypoints.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="Verify pytest, trivy, and bun are on PATH.")

    args = parser.parse_args()
    if args.command == "doctor":
        from runner.doctor import check_dependencies

        if not check_dependencies():
            sys.exit(1)
        sys.exit(0)
