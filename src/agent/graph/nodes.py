"""LangGraph node helpers (system prompts, etc.)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from agent.graph.state import AgentState
from remediation.rescan import run_trivy_scan, verify_cve_resolved
from remediation.verifier import run_test_suite
from agent.tools.file_write import AtomicWriteFailed, FileWriteTool
from agent.tools.terminal import TerminalTool

_ERR_NOT_FOUND = "ERROR: Tool not found."

_terminal_tool = TerminalTool()
_file_write_tool = FileWriteTool()


def _extract_tool_calls(message: BaseMessage) -> list[Any]:
    calls = getattr(message, "tool_calls", None)
    if not calls:
        return []
    return list(calls)


def _tool_call_id(call: object) -> str:
    if isinstance(call, dict):
        raw = call.get("id")
        return str(raw) if raw is not None else ""
    return str(getattr(call, "id", "") or "")


def _tool_name(call: object) -> str:
    if isinstance(call, dict):
        if "name" in call and call["name"] is not None:
            return str(call["name"])
        fn = call.get("function")
        if isinstance(fn, dict) and fn.get("name") is not None:
            return str(fn["name"])
        return ""
    return str(getattr(call, "name", "") or "")


def _tool_args(call: object) -> dict[str, Any]:
    if isinstance(call, dict):
        args = call.get("args")
        if args is None:
            fn = call.get("function")
            if isinstance(fn, dict) and "arguments" in fn:
                args = fn.get("arguments")
        if isinstance(args, str):
            return json.loads(args) if args else {}
        if isinstance(args, Mapping):
            return dict(args)
        return {}
    raw = getattr(call, "args", None)
    if isinstance(raw, str):
        return json.loads(raw) if raw else {}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _normalize_tool(name: str) -> str | None:
    key = re.sub(r"[-\s]+", "_", name.strip()).lower()
    if key in ("terminal", "terminaltool"):
        return "terminal"
    if key in ("file_write", "filewritetool"):
        return "file_write"
    return None


def generate_system_prompt(state: AgentState) -> str:
    """Return the remediation compiler system prompt.

    Parameters
    ----------
    state
        Graph checkpoint; reserved so callers can pass :class:`~agent.graph.state.AgentState`
        without altering the static prompt text.
    """
    return (
        "You are an autonomous remediation compiler. Do not explain your steps. "
        "If you need to verify code, output a tool call for pytest immediately. "
        "You are forbidden from outputting markdown code blocks containing fixes without using the FileWrite tool."
    )


def execute_tools(state: AgentState) -> AgentState:
    """Execute tool calls from the last AI message; append :class:`ToolMessage` results.

    Routes ``terminal`` / ``TerminalTool`` to :class:`~agent.tools.terminal.TerminalTool` and
    ``file_write`` / ``FileWriteTool`` to :class:`~agent.tools.file_write.FileWriteTool`.
    """
    try:
        messages: list[BaseMessage] = list(state["messages"])
    except KeyError:
        return {**state, "messages": [ToolMessage(content=_ERR_NOT_FOUND, tool_call_id="")]}

    if not messages:
        return {**state, "messages": messages}

    last = messages[-1]
    tool_calls = _extract_tool_calls(last)
    if not tool_calls:
        return {**state, "messages": messages}

    out: list[ToolMessage] = []
    for call in tool_calls:
        call_id = _tool_call_id(call)
        name = _tool_name(call)
        target = _normalize_tool(name)
        if target is None:
            out.append(ToolMessage(content=_ERR_NOT_FOUND, tool_call_id=call_id))
            continue
        try:
            args = _tool_args(call)
        except (json.JSONDecodeError, TypeError, ValueError):
            out.append(ToolMessage(content=_ERR_NOT_FOUND, tool_call_id=call_id))
            continue
        try:
            if target == "terminal":
                command = str(args["command"])
                payload = _terminal_tool.execute(command)
                out.append(
                    ToolMessage(
                        content=json.dumps(payload, sort_keys=True),
                        tool_call_id=call_id,
                    )
                )
            else:
                filepath = str(args["filepath"])
                content = str(args["content"])
                try:
                    _file_write_tool.write_content(filepath, content)
                except AtomicWriteFailed as exc:
                    out.append(ToolMessage(content=str(exc), tool_call_id=call_id))
                else:
                    out.append(ToolMessage(content="OK", tool_call_id=call_id))
        except KeyError:
            out.append(ToolMessage(content=_ERR_NOT_FOUND, tool_call_id=call_id))

    return {**state, "messages": messages + out}


_VERIFY_RETRY_INSTRUCTION = (
    "You must read the complete pytest output above in full, diagnose the failure from those logs, "
    "and try again with a corrected remediation. Do not proceed without addressing what pytest reported."
)

_CVE_STILL_PRESENT_MESSAGE = (
    "Your patch applied successfully, but the vulnerability [target_cve] remains active in the latest scan. "
    "Analyze your previous logic. You must use a fundamentally different approach. Do not repeat the same code modification."
)


def _trivy_scan_root(current_file: str) -> str:
    """Directory passed to :func:`remediation.rescan.run_trivy_scan` (repository-ish root when possible)."""

    path = Path(current_file).expanduser().resolve()
    base = path.parent if path.is_file() else path
    for ancestor in [base, *base.parents]:
        if (ancestor / ".git").is_dir() or (ancestor / "pyproject.toml").is_file():
            return str(ancestor)
    return str(base)


def verify_fix(state: AgentState) -> AgentState:
    """Run pytest on ``state['current_file']``, then a Trivy JSON scan for ``state['target_cve']``.

    If pytest fails, increments ``test_failures``, appends pytest logs with retry instructions, and sets
    ``is_verified`` to ``False``. If pytest passes but :func:`remediation.rescan.verify_cve_resolved`
    reports the CVE is still present, appends a fixed :class:`~langchain_core.messages.SystemMessage`
    instructing a different remediation approach, increments ``test_failures``, and sets
    ``is_verified`` to ``False``. When tests pass and the CVE no longer appears in the Trivy JSON,
    sets ``is_verified`` to ``True``.
    """
    target = state["current_file"]
    suite = run_test_suite(target)
    if not suite["passed"]:
        logs = suite["logs"]
        failures = int(state["test_failures"]) + 1
        follow_up = SystemMessage(content=f"{logs}\n\n{_VERIFY_RETRY_INSTRUCTION}")
        return {
            **state,
            "test_failures": failures,
            "messages": list(state["messages"]) + [follow_up],
            "is_verified": False,
        }

    scan_results = run_trivy_scan(_trivy_scan_root(target))
    if not verify_cve_resolved(scan_results, state["target_cve"]):
        cve = state["target_cve"]
        body = _CVE_STILL_PRESENT_MESSAGE.replace("[target_cve]", cve, 1)
        failures = int(state["test_failures"]) + 1
        return {
            **state,
            "test_failures": failures,
            "messages": list(state["messages"]) + [SystemMessage(content=body)],
            "is_verified": False,
        }

    return {**state, "is_verified": True}


def _protected_history_indices(messages: list[BaseMessage]) -> set[int]:
    """Indices of the original system prompt and the first human turn — never trimmed."""

    prot: set[int] = set()
    if not messages:
        return prot
    if isinstance(messages[0], SystemMessage):
        prot.add(0)
    for i, msg in enumerate(messages):
        if isinstance(msg, HumanMessage):
            prot.add(i)
            break
    return prot


def _tool_response_verified_success(content: str) -> bool:
    """Heuristic: terminal pytest/json exit_code 0, FileWrite ``OK``, else failures."""

    text = (content or "").strip()
    if not text:
        return True
    if text.startswith("ERROR:"):
        return False
    low = text.lower()
    if "atomicwritefailed" in low or "atomic write failed" in low:
        return False
    if text == "OK":
        return True
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and "exit_code" in payload:
            return int(payload["exit_code"]) == 0
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return True


def _tool_round_verification_succeeded(segment: list[BaseMessage]) -> bool:
    """True when every ``ToolMessage`` in this AIMessage-led segment indicates success."""

    if not segment or not isinstance(segment[0], AIMessage):
        return True
    ai = segment[0]
    calls = getattr(ai, "tool_calls", None) or []
    tools = [m for m in segment[1:] if isinstance(m, ToolMessage)]
    if calls and len(tools) < len(calls):
        return False
    for tm in tools:
        if not _tool_response_verified_success(tm.content):
            return False
    return True


def _find_oldest_failed_verification_round(
    messages: list[BaseMessage],
    protected: set[int],
) -> tuple[int, int] | None:
    """Return ``(start, end)`` slice indices for the oldest failing tool round, or ``None``."""

    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            start = i
            j = i + 1
            while j < n and isinstance(messages[j], ToolMessage):
                j += 1
            segment = messages[start:j]
            if not _tool_round_verification_succeeded(segment):
                span_ids = set(range(start, j))
                if not (span_ids & protected):
                    return start, j
            i = j
        else:
            i += 1
    return None


def trim_history(state: AgentState) -> AgentState:
    """Drop one old unsuccessful tool round when the transcript grows beyond fifteen messages.

    When ``len(state['messages']) > 15``, finds the **earliest** block consisting of an
    :class:`~langchain_core.messages.AIMessage` with ``tool_calls`` plus its consecutive
    :class:`~langchain_core.messages.ToolMessage` responses where at least one tool outcome does
    not look successful (non-zero ``exit_code`` from :class:`~agent.tools.terminal.TerminalTool`,
    FileWrite failure text, or ``ERROR:`` tool routing). That entire block is removed.

    The first :class:`~langchain_core.messages.SystemMessage` (if present at index ``0``) and the
    first :class:`~langchain_core.messages.HumanMessage` are never deleted. If no qualifying block
    exists, or the history is already short, returns the state unchanged.
    """
    messages = list(state["messages"])
    if len(messages) <= 15:
        return {**state, "messages": messages}

    protected = _protected_history_indices(messages)
    span = _find_oldest_failed_verification_round(messages, protected)
    if span is None:
        return {**state, "messages": messages}

    start, end = span
    trimmed = messages[:start] + messages[end:]
    return {**state, "messages": trimmed}
