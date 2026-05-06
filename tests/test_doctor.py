"""Tests for ``doctor`` checklist formatting."""

from __future__ import annotations

import unittest

from local_ai_stack.doctor import CheckItem, claude_code_environment_ready, format_doctor_report


class DoctorReportTests(unittest.TestCase):
    def test_overall_red_if_any_fail(self) -> None:
        items = [
            CheckItem(1, "Python ≥ 3.10", "green", "/usr/bin/python3 — Python 3.12.1"),
            CheckItem(2, "Something", "red", "broken", ("brew install foo",)),
        ]
        report = format_doctor_report(items)
        self.assertIn("Overall: RED", report)
        self.assertIn("$ brew install foo", report)

    def test_overall_yellow_if_warn_only(self) -> None:
        items = [
            CheckItem(1, "A", "green", "ok"),
            CheckItem(2, "B", "yellow", "maybe", ("hint",)),
        ]
        report = format_doctor_report(items)
        self.assertIn("Overall: YELLOW", report)
        self.assertIn("$ hint", report)
        self.assertNotIn("Claude Code environment:", report)

    def test_claude_ready_when_all_green(self) -> None:
        items = [
            CheckItem(10, "Stack env + Ollama", "green", "ok"),
            CheckItem(11, "Claude Code: Bun on PATH", "green", "bun 1.0"),
            CheckItem(12, "Claude Code: Agent Docker image built", "green", "image ok"),
            CheckItem(13, "Claude Code: Ollama reachable from agent container", "green", "ok"),
            CheckItem(14, "Claude Code: Repository workspace writable", "green", "ok"),
        ]
        self.assertTrue(claude_code_environment_ready(items))
        report = format_doctor_report(items)
        self.assertIn("Claude Code environment: READY", report)

    def test_claude_not_ready_on_warn_or_fail(self) -> None:
        items = [
            CheckItem(11, "Claude Code: Bun on PATH", "green", "bun"),
            CheckItem(12, "Claude Code: Agent Docker image built", "yellow", "skipped"),
            CheckItem(13, "Claude Code: Ollama reachable from agent container", "green", "ok"),
            CheckItem(14, "Claude Code: Repository workspace writable", "green", "ok"),
        ]
        self.assertFalse(claude_code_environment_ready(items))
        report = format_doctor_report(items)
        self.assertIn("Claude Code environment: NOT READY", report)


if __name__ == "__main__":
    unittest.main()
