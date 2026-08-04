"""Compiled LangGraph remediation loop (Ollama + local tools)."""

from __future__ import annotations

import os
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, StateGraph

from agent.graph.edges import should_continue
from agent.graph.nodes import (
    enforce_tool_format,
    execute_tools,
    generate_system_prompt,
    path_sentinel,
    verify_fix,
)
from agent.graph.state import AgentState
from agent.tools.file_write import AtomicWriteFailed, FileWriteTool
from agent.tools.terminal import TerminalTool
from runner.local_ai_stack import build_langgraph_chat_model

_terminal = TerminalTool()
_file_write = FileWriteTool()


def _terminal_tool_fn(command: str) -> str:
    import json

    return json.dumps(_terminal.execute(command), sort_keys=True)


def _file_write_tool_fn(filepath: str, content: str) -> str:
    try:
        _file_write.write_content(filepath, content)
    except AtomicWriteFailed as exc:
        return str(exc)
    return "OK"


def _bound_tools() -> list[Any]:
    return [
        StructuredTool.from_function(
            func=_terminal_tool_fn,
            name="terminal",
            description="Run a shell command in the workspace. Prefer this over printing bash markdown.",
        ),
        StructuredTool.from_function(
            func=_file_write_tool_fn,
            name="file_write",
            description="Atomically write file contents to filepath under the workspace.",
        ),
    ]


def call_model(state: AgentState) -> AgentState:
    """Invoke the remediation chat model (tool-bound) and append the AI message."""

    model = build_langgraph_chat_model().bind_tools(_bound_tools())
    response = model.invoke(list(state["messages"]))
    return {**state, "messages": list(state["messages"]) + [response]}


def route_after_enforce(state: AgentState) -> Literal["agent", "tools", "abort"]:
    """After format enforcement: retry the model on FORMAT ERROR, else run tools when present."""

    messages = list(state["messages"])
    if not messages:
        return "agent"
    format_errors = sum(
        1
        for m in messages
        if isinstance(m, SystemMessage) and "FORMAT ERROR" in str(getattr(m, "content", ""))
    )
    if format_errors >= 5:
        return "abort"
    last = messages[-1]
    if isinstance(last, SystemMessage) and "FORMAT ERROR" in str(getattr(last, "content", "")):
        return "agent"
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            calls = getattr(msg, "tool_calls", None) or []
            if calls:
                return "tools"
            break
    return "agent"


def route_after_verify(state: AgentState) -> Literal["end", "continue_to_agent"]:
    decision = should_continue(state)
    return "end" if decision == "end" else "continue_to_agent"


def build_remediation_graph() -> Any:
    """Compile agent → enforce → tools → path_sentinel → verify → (end|agent)."""

    g: StateGraph = StateGraph(AgentState)
    g.add_node("agent", call_model)
    g.add_node("enforce", enforce_tool_format)
    g.add_node("tools", execute_tools)
    g.add_node("path_sentinel", path_sentinel)
    g.add_node("verify", verify_fix)

    g.set_entry_point("agent")
    g.add_edge("agent", "enforce")
    g.add_conditional_edges(
        "enforce",
        route_after_enforce,
        {"agent": "agent", "tools": "tools", "abort": END},
    )
    g.add_edge("tools", "path_sentinel")
    g.add_edge("path_sentinel", "verify")
    g.add_conditional_edges(
        "verify",
        route_after_verify,
        {"end": END, "continue_to_agent": "agent"},
    )
    return g.compile()


def initial_agent_state(
    *,
    brief: str,
    workspace: str,
    target_cve: str,
    current_file: str | None = None,
) -> AgentState:
    """Build the checkpoint dict for a remediation run."""

    import time

    cwd = current_file or (os.environ.get("OCTO_FIX_CURRENT_FILE") or "").strip() or workspace
    system = generate_system_prompt(  # type: ignore[arg-type]
        {
            "messages": [],
            "current_file": cwd,
            "target_cve": target_cve,
            "vulnerability_context": {"runs": []},
            "test_failures": 0,
            "is_verified": False,
            "start_time": time.time(),
        }
    )
    human = (
        f"Workspace: {workspace}\n"
        f"Target CVE: {target_cve or '(none specified)'}\n"
        f"Primary path hint: {cwd}\n\n"
        f"{brief.strip()}"
    )
    return {
        "messages": [SystemMessage(content=system), HumanMessage(content=human)],
        "current_file": cwd,
        "target_cve": target_cve,
        "vulnerability_context": {"runs": []},
        "test_failures": 0,
        "is_verified": False,
        "start_time": time.time(),
    }
