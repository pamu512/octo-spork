"""Run the compiled LangGraph remediation agent against a workspace clone."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)


def run_langgraph_remediation_agent(
    workspace: Path,
    *,
    brief: str,
    target_cve: str = "",
    current_file: str | None = None,
) -> tuple[int, str]:
    """Execute the LangGraph remediation loop in ``workspace``.

    Returns ``(exit_code, transcript)`` where ``0`` means ``is_verified`` ended true.
    Changes cwd to ``workspace`` for the duration so terminal/file tools resolve relative paths.
    """
    from agent.graph.graph import build_remediation_graph, initial_agent_state

    ws = Path(workspace).expanduser().resolve()
    if not ws.is_dir():
        raise RuntimeError(f"LangGraph remediation workspace missing: {ws}")

    prev = Path.cwd()
    os.chdir(ws)
    try:
        graph = build_remediation_graph()
        state = initial_agent_state(
            brief=brief,
            workspace=str(ws),
            target_cve=(target_cve or "").strip(),
            current_file=current_file,
        )
        recursion_limit = max(8, int(os.environ.get("OCTO_LANGGRAPH_RECURSION_LIMIT", "40")))
        final = graph.invoke(state, config={"recursion_limit": recursion_limit})
    except Exception as exc:
        _LOG.exception("LangGraph remediation failed")
        return 1, f"LangGraph remediation failed: {exc}"
    finally:
        try:
            os.chdir(prev)
        except OSError:
            pass

    verified = bool(final.get("is_verified")) if isinstance(final, dict) else False
    messages = final.get("messages") if isinstance(final, dict) else []
    lines: list[str] = ["# LangGraph remediation transcript", ""]
    for msg in messages or []:
        role = type(msg).__name__
        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            content = str(content)
        lines.append(f"## {role}")
        lines.append("")
        lines.append(content[:8000])
        lines.append("")
        calls = getattr(msg, "tool_calls", None) or []
        if calls:
            lines.append(f"_tool_calls_: {calls!r}"[:4000])
            lines.append("")
    transcript = "\n".join(lines)
    return (0 if verified else 1), transcript


def remediation_engine() -> str:
    """Return ``claude`` (default) or ``langgraph`` from ``OCTO_REMEDIATION_ENGINE``."""

    raw = (os.environ.get("OCTO_REMEDIATION_ENGINE") or "claude").strip().lower()
    if raw in {"langgraph", "graph", "lg"}:
        return "langgraph"
    return "claude"
