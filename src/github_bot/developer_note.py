"""Developer-visible failure reporting for PR review workers (no silent failures)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

_ISSUE_SPEC_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<num>\d+)$"
)


def parse_developer_note_issue(spec: str) -> tuple[str, str, int] | None:
    """Parse ``owner/repo#issue_number`` (e.g. ``acme/spork#42``)."""
    m = _ISSUE_SPEC_RE.match(str(spec).strip())
    if not m:
        return None
    return m.group("owner"), m.group("repo"), int(m.group("num"))


def _resolve_github_token() -> str:
    return (
        (os.environ.get("GITHUB_TOKEN") or "").strip()
        or (os.environ.get("GH_TOKEN") or "").strip()
    )


def _append_local_failure_log(log_path: Path, body: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(body)
        if not body.endswith("\n"):
            fh.write("\n")


def _post_issue_comment(owner: str, repo: str, issue_number: int, body: str, token: str) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
    payload = json.dumps({"body": body}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "octo-spork-github-bot",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if getattr(resp, "status", 200) not in (200, 201):
                raw = resp.read()[:2000].decode("utf-8", errors="replace")
                _LOG.error("Developer-note GitHub comment unexpected status: %s %s", resp.status, raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        _LOG.error(
            "Developer-note GitHub comment failed (%s): %s",
            exc.code,
            detail,
        )
    except urllib.error.URLError as exc:
        _LOG.error("Developer-note GitHub comment network error: %s", exc)


def report_pr_processing_failure(
    envelope: dict[str, Any],
    exc: BaseException,
    formatted_traceback: str,
) -> None:
    """Append traceback to an optional log file and/or a configured GitHub Issue (team-visible).

    Use a dedicated, non-public repo issue or org-internal repo for the Issue URL — GitHub does not
    support truly private issue comments; restrict repo access as ``private``.
    """
    delivery = envelope.get("delivery", "?")
    trigger = envelope.get("trigger", "?")
    event = envelope.get("event", "?")
    lines = [
        "### Developer Note (automated)",
        "",
        f"- **Delivery:** `{delivery}`",
        f"- **Event:** `{event}` · **trigger:** `{trigger}`",
        f"- **Exception:** `{type(exc).__name__}: {exc}`",
        "",
        "<details><summary>Full traceback</summary>",
        "",
        "```",
    ]
    tb = formatted_traceback.strip()
    max_tb = 58_000
    if len(tb) > max_tb:
        tb = tb[:max_tb] + "\n… [traceback truncated]"
    lines.append(tb)
    lines.extend(["```", "</details>", ""])
    note = "\n".join(lines)

    log_raw = (os.environ.get("OCTO_SPORK_FAILURE_LOG") or "").strip()
    if log_raw:
        log_path = Path(log_raw).expanduser()
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            _append_local_failure_log(
                log_path,
                f"\n{'=' * 72}\n{stamp} UTC\n{note}\n{'=' * 72}\n",
            )
        except OSError as err:
            _LOG.error("Could not append OCTO_SPORK_FAILURE_LOG %s: %s", log_path, err)

    issue_spec = (os.environ.get("OCTO_SPORK_DEVELOPER_NOTE_ISSUE") or "").strip()
    if issue_spec:
        parsed = parse_developer_note_issue(issue_spec)
        if not parsed:
            _LOG.error(
                "Invalid OCTO_SPORK_DEVELOPER_NOTE_ISSUE=%r (expected owner/repo#123)",
                issue_spec,
            )
        else:
            owner, repo, num = parsed
            token = _resolve_github_token()
            if not token:
                _LOG.error(
                    "OCTO_SPORK_DEVELOPER_NOTE_ISSUE is set but GITHUB_TOKEN/GH_TOKEN is missing"
                )
            else:
                body = note
                if len(body) > 65_000:
                    body = (
                        note[:62_000]
                        + "\n\n_… [comment truncated for GitHub size limit]_\n"
                    )
                _post_issue_comment(owner, repo, num, body, token)
