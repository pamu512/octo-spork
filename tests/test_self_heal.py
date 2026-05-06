"""Tests for ``claude_bridge.self_heal``."""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class SelfHealUnitTests(unittest.TestCase):
    def test_build_fix_prompt_contains_heading(self) -> None:
        from claude_bridge.self_heal import build_fix_prompt

        p = build_fix_prompt("FAILED test_foo.py::test_x")
        self.assertIn("Fix these test failures", p)
        self.assertIn("FAILED test_foo.py::test_x", p)

    def test_render_report_sections(self) -> None:
        from claude_bridge.self_heal import (
            ClaudeAttempt,
            PytestAttempt,
            SelfHealOutcome,
            render_grounded_failure_report,
        )

        ws = Path("/tmp/work")
        outcome = SelfHealOutcome(workspace=ws, max_fix_attempts=3, success=False)
        outcome.pytest_attempts.append(
            PytestAttempt(round_index=1, exit_code=1, combined_output="E assertions")
        )
        outcome.claude_attempts.append(
            ClaudeAttempt(
                round_index=1,
                exit_code=0,
                stdout="done",
                stderr="",
                prompt_excerpt="Fix these",
            )
        )
        md = render_grounded_failure_report(outcome)
        self.assertIn("Grounded Failure Report", md)
        self.assertIn("E assertions", md)
        self.assertIn("What the agent tried", md)
        self.assertIn("Where this got stuck", md)

    def test_run_self_heal_exits_on_pass(self) -> None:
        from claude_bridge import self_heal as sh

        ws = Path.cwd()

        with mock.patch.object(sh, "run_pytest", return_value=(0, "ok")):
            with mock.patch.object(sh, "run_claude_fix") as cf:
                out = sh.run_self_heal(ws, [], max_fix_attempts=3)
        self.assertTrue(out.success)
        self.assertEqual(len(out.pytest_attempts), 1)
        self.assertEqual(len(out.claude_attempts), 0)
        self.assertFalse(cf.called)

    def test_run_self_heal_three_fix_rounds_then_fail(self) -> None:
        from claude_bridge import self_heal as sh

        ws = Path.cwd()
        seq = [(1, "fail")] * 4

        def pytest_side_effect(*_a, **_k):
            return seq.pop(0) if seq else (1, "fail")

        with mock.patch.object(sh, "run_pytest", side_effect=pytest_side_effect):
            with mock.patch.object(sh, "run_claude_fix", return_value=(0, "", "")):
                out = sh.run_self_heal(ws, [], max_fix_attempts=3)

        self.assertFalse(out.success)
        self.assertEqual(len(out.pytest_attempts), 4)
        self.assertEqual(len(out.claude_attempts), 3)

    def test_skip_claude_stops_after_first_failure(self) -> None:
        from claude_bridge import self_heal as sh

        ws = Path.cwd()
        with mock.patch.object(sh, "run_pytest", return_value=(1, "boom")):
            with mock.patch.object(sh, "run_claude_fix") as cf:
                out = sh.run_self_heal(ws, [], max_fix_attempts=3, skip_claude=True)
        self.assertFalse(out.success)
        self.assertEqual(len(out.pytest_attempts), 1)
        self.assertEqual(len(out.claude_attempts), 0)
        self.assertFalse(cf.called)

    def test_main_success_rc0(self) -> None:
        from claude_bridge import self_heal as sh

        err = io.StringIO()
        with mock.patch.object(sh, "run_self_heal") as rs:
            rs.return_value = mock.Mock(success=True)
            with mock.patch.object(sh.sys, "stderr", err):
                rc = sh.main(["--workspace", str(Path.cwd())])
        self.assertEqual(rc, 0)
        self.assertIn("All tests passed", err.getvalue())

    def test_main_failure_prints_report(self) -> None:
        from claude_bridge import self_heal as sh

        err = io.StringIO()
        out_buf = io.StringIO()

        from claude_bridge.self_heal import PytestAttempt, SelfHealOutcome

        fake = SelfHealOutcome(workspace=Path("/w"), max_fix_attempts=3, success=False)
        fake.pytest_attempts.append(
            PytestAttempt(round_index=1, exit_code=1, combined_output="ERR")
        )

        with mock.patch.object(sh, "run_self_heal", return_value=fake):
            with mock.patch.object(sh.sys, "stderr", err):
                with mock.patch.object(sh.sys, "stdout", out_buf):
                    rc = sh.main(["--workspace", str(Path.cwd())])
        self.assertEqual(rc, 1)
        self.assertIn("Grounded Failure Report", out_buf.getvalue())
        self.assertIn("still failing", err.getvalue())


if __name__ == "__main__":
    unittest.main()
