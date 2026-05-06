"""VRAM-aware Ollama tag suggestions and optional HTTP proxy."""

from __future__ import annotations

from ollama_guard.policy import GuardDecision, analyze_model, resolve_model_for_run

__all__ = [
    "GuardDecision",
    "analyze_model",
    "resolve_model_for_run",
]
