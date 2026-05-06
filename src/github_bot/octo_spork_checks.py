"""GitHub Checks API — ``Octo-spork Analysis`` check run with duration and Success/Failure conclusion."""

from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

CHECK_RUN_NAME = "Octo-spork Analysis"
_SKIP_ENV = "OCTO_SPORK_SKIP_CHECKS"


def checks_api_enabled(*, dry_run: bool) -> bool:
    if dry_run:
        return False
    return os.environ.get(_SKIP_ENV, "").strip().lower() not in {"1", "true", "yes", "on"}


def scan_outputs_indicate_critical(trivy_md: str | None, codeql_md: str | None) -> bool:
    """True if scanner markdown reports Critical-tier rows (Trivy SARIF table or CodeQL critical section)."""
    if _trivy_markdown_has_critical(trivy_md):
        return True
    if _codeql_markdown_has_critical(codeql_md):
        return True
    return False


def _trivy_markdown_has_critical(md: str | None) -> bool:
    if not md:
        return False
    return bool(re.search(r"\|\s*CRITICAL\s*\|", md, re.IGNORECASE))


def _codeql_markdown_has_critical(md: str | None) -> bool:
    if not md or "### CodeQL — Critical findings" not in md:
        return False
    start = md.index("### CodeQL — Critical findings")
    section = md[start : start + 8000]
    if "_No **Critical**" in section or "_No SARIF results" in section:
        return False
    lines = [ln for ln in section.splitlines() if ln.strip().startswith("|")]
    data_rows = [
        ln
        for ln in lines
        if "---" not in ln and "Severity |" not in ln and "| Rule |" not in ln and "| --- |" not in ln
    ]
    return len(data_rows) >= 1


class OctoSporkAnalysisSession:
    """Create an ``in_progress`` check run at start; call :meth:`finish` when the pipeline ends."""

    def __init__(
        self,
        repository: Any,
        head_sha: str,
        *,
        dry_run: bool,
        before_api_call: Callable[[], None] | None = None,
    ) -> None:
        self._repo = repository
        self._head_sha = head_sha
        self._dry_run = dry_run
        self._before_api = before_api_call
        self._run: Any | None = None
        self._t0 = time.monotonic()
        self._started_wall = datetime.now(timezone.utc)
        self._critical = False
        self._system_offline_reason: str | None = None
        self._finished = False

    def mark_critical(self) -> None:
        self._critical = True

    def mark_system_offline(self, reason: str) -> None:
        """Mark run as blocked by local AI unavailable — check conclusion should reflect **System Offline**."""
        self._system_offline_reason = str(reason).strip() or "Local Ollama or model unavailable."

    def start(self) -> None:
        """POST ``in_progress`` check run (requires ``checks:write`` on the token)."""
        if not checks_api_enabled(dry_run=self._dry_run):
            return
        if self._before_api:
            self._before_api()
        try:
            self._run = self._repo.create_check_run(
                name=CHECK_RUN_NAME,
                head_sha=self._head_sha,
                status="in_progress",
                started_at=self._started_wall,
                output={
                    "title": CHECK_RUN_NAME,
                    "summary": "In progress — running AI and security scans.",
                },
            )
        except Exception as exc:
            sys.stderr.write(f"[octo-spork] Could not create GitHub check run: {exc}\n")
            self._run = None

    def finish(
        self,
        *,
        exc: BaseException | None = None,
        components: dict[str, str] | None = None,
    ) -> None:
        """PATCH check run to ``completed`` with ``success`` or ``failure`` and scan duration in output."""
        if self._finished:
            return
        self._finished = True
        if not checks_api_enabled(dry_run=self._dry_run) or not self._run:
            return
        if self._before_api:
            self._before_api()

        duration_sec = time.monotonic() - self._t0
        components = components or {}

        if exc is not None:
            conclusion = "failure"
            summary = (
                f"**Duration:** {duration_sec:.1f}s\n\n"
                f"Run failed with an error before scans completed.\n\n"
                f"```\n{exc}\n```"
            )
            text = _format_check_text(
                duration_sec=duration_sec,
                critical=False,
                conclusion_failed=True,
                error=str(exc),
                components=components,
            )
        elif self._system_offline_reason is not None:
            conclusion = "failure"
            summary = (
                f"## System Offline\n\n"
                f"**Duration:** {duration_sec:.1f}s\n\n"
                f"The grounded LLM review was **not** run because local inference is unavailable.\n\n"
                f"{self._system_offline_reason}\n\n"
                f"_Security scanners may still have run; see check details._"
            )
            text = _format_check_text(
                duration_sec=duration_sec,
                critical=False,
                conclusion_failed=True,
                error=self._system_offline_reason,
                components=components,
            )
        else:
            conclusion = "failure" if self._critical else "success"
            summary = (
                f"**Duration:** {duration_sec:.1f}s · "
                f"**Critical-tier issues:** {'yes' if self._critical else 'no'}\n\n"
                + (
                    "Octo-spork detected Critical-tier security findings (secrets and/or scanners)."
                    if self._critical
                    else "No Critical-tier issues reported by secret scan or Trivy/CodeQL summaries."
                )
            )
            text = _format_check_text(
                duration_sec=duration_sec,
                critical=self._critical,
                conclusion_failed=False,
                error=None,
                components=components,
            )

        try:
            title = (
                f"{CHECK_RUN_NAME} — System Offline"
                if self._system_offline_reason is not None
                else CHECK_RUN_NAME
            )
            self._run.edit(
                status="completed",
                conclusion=conclusion,
                completed_at=datetime.now(timezone.utc),
                output={
                    "title": title[:255],
                    "summary": summary[:65535],
                    "text": text[:65535],
                },
            )
        except Exception as exc:
            sys.stderr.write(f"[octo-spork] Could not update GitHub check run: {exc}\n")


def _format_check_text(
    *,
    duration_sec: float,
    critical: bool,
    conclusion_failed: bool,
    error: str | None,
    components: dict[str, str],
) -> str:
    lines = [
        "## Octo-spork Analysis",
        "",
        f"- **Scan duration:** {duration_sec:.2f}s",
        f"- **Check conclusion:** {'failure' if (critical or conclusion_failed) else 'success'}",
        "",
    ]
    if error:
        lines.extend(["### Error", "", error, ""])
    lines.append("### Components")
    lines.append("")
    for name, detail in sorted(components.items()):
        lines.append(f"- **{name}:** {detail}")
    lines.append("")
    return "\n".join(lines)
