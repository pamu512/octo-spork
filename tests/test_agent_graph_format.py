"""Unit tests for remediation graph edges and format enforcer (no live Ollama)."""

from __future__ import annotations

import time
import unittest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


class TestShouldContinue(unittest.TestCase):
    def _base(self, **overrides):
        state = {
            "messages": [],
            "current_file": ".",
            "target_cve": "CVE-2024-0001",
            "vulnerability_context": {"runs": []},
            "test_failures": 0,
            "is_verified": False,
            "start_time": time.time(),
        }
        state.update(overrides)
        return state

    def test_verified_ends(self) -> None:
        from agent.graph.edges import should_continue

        self.assertEqual(should_continue(self._base(is_verified=True)), "end")

    def test_timeout_overrides_retries(self) -> None:
        from agent.graph.edges import should_continue

        st = self._base(start_time=time.time() - 400, test_failures=0)
        self.assertEqual(should_continue(st), "end")

    def test_failures_trip(self) -> None:
        from agent.graph.edges import should_continue

        self.assertEqual(should_continue(self._base(test_failures=3)), "end")

    def test_continue(self) -> None:
        from agent.graph.edges import should_continue

        self.assertEqual(should_continue(self._base(test_failures=1)), "continue_to_agent")


class TestEnforceToolFormat(unittest.TestCase):
    def test_bash_fence_slapped(self) -> None:
        from agent.graph.nodes import enforce_tool_format

        state = {
            "messages": [AIMessage(content="Try this:\n```bash\nls\n```")],
            "current_file": ".",
            "target_cve": "",
            "vulnerability_context": {"runs": []},
            "test_failures": 0,
            "is_verified": False,
            "start_time": time.time(),
        }
        out = enforce_tool_format(state)  # type: ignore[arg-type]
        self.assertIn("messages", out)
        last = out["messages"][-1]
        self.assertIsInstance(last, SystemMessage)
        self.assertIn("FORMAT ERROR", last.content)

    def test_tool_calls_passthrough(self) -> None:
        from agent.graph.nodes import enforce_tool_format

        ai = AIMessage(
            content="",
            tool_calls=[{"id": "1", "name": "terminal", "args": {"command": "ls"}}],
        )
        state = {
            "messages": [HumanMessage(content="fix"), ai],
            "current_file": ".",
            "target_cve": "",
            "vulnerability_context": {"runs": []},
            "test_failures": 0,
            "is_verified": False,
            "start_time": time.time(),
        }
        self.assertEqual(enforce_tool_format(state), {})  # type: ignore[arg-type]


class TestRemediationEngine(unittest.TestCase):
    def test_default_claude(self) -> None:
        import os

        from agent.remediation_runner import remediation_engine

        os.environ.pop("OCTO_REMEDIATION_ENGINE", None)
        self.assertEqual(remediation_engine(), "claude")

    def test_langgraph_flag(self) -> None:
        import os

        from agent.remediation_runner import remediation_engine

        os.environ["OCTO_REMEDIATION_ENGINE"] = "langgraph"
        try:
            self.assertEqual(remediation_engine(), "langgraph")
        finally:
            os.environ.pop("OCTO_REMEDIATION_ENGINE", None)


if __name__ == "__main__":
    unittest.main()
