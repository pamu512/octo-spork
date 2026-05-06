import unittest

from local_ai_stack.__main__ import (
    _claude_agent_stack_available,
    _lines_from_trivy_critical_report,
)


class LocalAiStackHookTests(unittest.TestCase):
    def test_lines_from_trivy_critical_report_vuln(self):
        report = {
            "Results": [
                {
                    "Target": "requirements.txt",
                    "Vulnerabilities": [
                        {
                            "Severity": "CRITICAL",
                            "VulnerabilityID": "CVE-2099-1",
                            "PkgName": "badpkg",
                            "Title": "oh no",
                        },
                        {"Severity": "HIGH", "VulnerabilityID": "CVE-high", "PkgName": "x"},
                    ],
                }
            ]
        }
        lines = _lines_from_trivy_critical_report(report)
        self.assertEqual(len(lines), 1)
        self.assertIn("CVE-2099-1", lines[0])
        self.assertIn("badpkg", lines[0])

    def test_lines_from_trivy_critical_report_misconfig(self):
        report = {
            "Results": [
                {
                    "Target": "Dockerfile",
                    "Misconfigurations": [
                        {"Severity": "CRITICAL", "ID": "DS001", "Title": "root user"},
                    ],
                }
            ]
        }
        lines = _lines_from_trivy_critical_report(report)
        self.assertTrue(any("DS001" in ln for ln in lines))

    def test_claude_agent_stack_available_when_bundled(self) -> None:
        """Repo ships ``deploy/claude-code`` + compose fragment for optional Claude Agent."""
        self.assertTrue(_claude_agent_stack_available())


if __name__ == "__main__":
    unittest.main()
