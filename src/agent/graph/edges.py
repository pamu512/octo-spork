"""Conditional routing for LangGraph remediation graphs."""

from __future__ import annotations

import sys
import time

from agent.graph.state import AgentState


def should_continue(state: AgentState) -> str:
    """Route the graph after verification.

    Returns ``"end"`` when remediation is verified successfully or when the circuit breaker trips
    after ``state['test_failures'] >= 3``. Also returns ``"end"`` when wall-clock runtime since
    ``state['start_time']`` exceeds five minutes (overriding further retries). Otherwise returns
    ``"continue_to_agent"`` so the agent may iterate again.
    """
    if state["is_verified"]:
        return "end"
    current_time = time.time()
    if current_time - float(state["start_time"]) > 300.0:
        print(
            "Remediation aborted: Agent loop exceeded 5-minute limit.",
            file=sys.stderr,
        )
        return "end"
    if state["test_failures"] >= 3:
        return "end"
    return "continue_to_agent"
