"""Unit tests for remediation PR worker."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class FixItWorkerTests(unittest.TestCase):
    def test_extract_cve_for_fix_verification(self) -> None:
        from github_bot.fix_it_worker import extract_cve_for_fix_verification

        self.assertEqual(
            extract_cve_for_fix_verification("Fix CVE-2024-12345 in lodash"),
            "CVE-2024-12345",
        )
        self.assertEqual(extract_cve_for_fix_verification("no id here"), "")

    def test_format_system_warning_contains_expected_sections(self) -> None:
        from github_bot.fix_it_worker import format_system_warning_verification_failed

        body = format_system_warning_verification_failed(
            original_html="https://github.com/o/r/pull/1",
            pr_number=1,
            max_attempts=3,
            last_detail="trivy failed",
        )
        self.assertIn("System Warning", body)
        self.assertIn("unable to produce a verified solution", body)
        self.assertIn("trivy failed", body)
        self.assertIn("Negative reinforcement", body)
        self.assertIn("Your previous attempt failed validation with error:", body)
        self.assertIn("Do not repeat the previous logic.", body)

    def test_parse_pr_html_url(self) -> None:
        from github_bot.fix_it_worker import parse_pr_html_url

        self.assertEqual(
            parse_pr_html_url("https://github.com/acme/widget/pull/42"),
            ("acme", "widget", 42),
        )

    def test_resolve_compose_paths_requires_agenticseek(self) -> None:
        from github_bot.fix_it_worker import resolve_compose_paths

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "deploy" / "local-ai").mkdir(parents=True)
            env_local = root / "deploy" / "local-ai" / ".env.local"
            env_local.write_text("# no AGENTICSEEK_DIR\n", encoding="utf-8")
            with patch.dict(os.environ, {"OCTO_LOCAL_AI_ENV_FILE": str(env_local)}, clear=False):
                with self.assertRaises(RuntimeError) as ctx:
                    resolve_compose_paths(root)
                self.assertIn("AGENTICSEEK_DIR", str(ctx.exception))

    def test_resolve_compose_paths_ok(self) -> None:
        from github_bot.fix_it_worker import resolve_compose_paths

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agentic = Path(td) / "agenticseek"
            agentic.mkdir(parents=True)
            (agentic / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (root / "deploy" / "local-ai").mkdir(parents=True)
            env_local = root / "deploy" / "local-ai" / ".env.local"
            env_local.write_text(f"AGENTICSEEK_DIR={agentic}\n", encoding="utf-8")
            with patch.dict(os.environ, {"OCTO_LOCAL_AI_ENV_FILE": str(env_local)}, clear=False):
                env_file, asp = resolve_compose_paths(root)
            self.assertEqual(env_file, env_local.resolve())
            self.assertEqual(asp, agentic.resolve())


if __name__ == "__main__":
    unittest.main()
