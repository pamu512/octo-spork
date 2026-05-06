"""Background **Bash / network** usage auditor for the Claude Code agent container.

Best-effort: streams ``docker logs -f`` for the agent service and applies heuristics to each line.
Claude Code may or may not echo raw shell commands in container logs; when it does, this process
can react. For higher assurance, combine with read-only rootfs, seccomp, or a custom agent
runtime that wraps BashTool.

Heuristics are shared with :mod:`claude_bridge.safety_regex_middleware` (``classify_bash_line``).

On violation:

- ``docker kill -s KILL <container>``
- Append one line to ``<repo>/logs/agent_security.log``

Violations (see middleware for the full set):

- **rm_rf** — ``rm -rf`` / ``rm -r -f`` style deletes
- **curl_public_ip** — ``curl`` toward a **global (public) IPv4** literal
- **curl_pipe_shell** — ``curl``/``wget`` piped to ``sh``/``bash``
- **protected_config_write** — shell writes to ``.env`` or compose YAML files
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_bridge.safety_regex_middleware import (
    classify_bash_line,
    kill_agent_container,
    violation_detail,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = REPO_ROOT / "logs"
DEFAULT_LOG_FILE = LOG_DIR / "agent_security.log"
DEFAULT_CONTAINER = "local-ai-claude-agent"

_LOG = logging.getLogger("agent_security_auditor")


def classify_line(line: str) -> str | None:
    """Return violation kind, or ``None`` if the line is acceptable."""
    return classify_bash_line(line)


def append_security_log(
    path: Path,
    *,
    kind: str,
    detail: str,
    container: str,
    raw_line: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "event": "Security Violation",
        "kind": kind,
        "detail": detail,
        "container": container,
        "matched_line": raw_line[:2000],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def handle_violation(
    *,
    kind: str,
    line: str,
    container: str,
    log_file: Path,
) -> None:
    detail = violation_detail(kind, line)
    append_security_log(
        log_file,
        kind=kind,
        detail=detail,
        container=container,
        raw_line=line.rstrip("\n"),
    )
    ok, msg = kill_agent_container(container)
    _LOG.warning("Security Violation %s — docker kill: %s (%s)", kind, ok, msg)


def stream_docker_logs_forever(
    container: str,
    *,
    log_file: Path,
    poll_tail: str = "200",
) -> None:
    """Block forever on ``docker logs -f`` and scan each line."""
    cmd = ["docker", "logs", "-f", "--tail", poll_tail, container]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        _LOG.error("docker not found; cannot stream logs")
        sys.exit(127)
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            kind = classify_line(line)
            if kind:
                handle_violation(kind=kind, line=line, container=container, log_file=log_file)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


_stop = False


def _on_signal(_sig: int, _frame: Any) -> None:
    global _stop
    _stop = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor agent docker logs for bash security violations.")
    parser.add_argument("--container", default=None, help=f"Container name (default env or {DEFAULT_CONTAINER!r})")
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help=f"Log path (default: {DEFAULT_LOG_FILE})",
    )
    parser.add_argument("--tail", default="200", help="docker logs --tail initial buffer")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging to stderr",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    container = (args.container or "").strip() or (
        __import__("os").environ.get("CLAUDE_AGENT_CONTAINER", "").strip() or DEFAULT_CONTAINER
    )
    log_file = (args.log_file or DEFAULT_LOG_FILE).expanduser().resolve()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    _LOG.info("Agent security auditor watching container=%s log_file=%s", container, log_file)

    while not _stop:
        try:
            stream_docker_logs_forever(container, log_file=log_file, poll_tail=args.tail)
        except KeyboardInterrupt:
            break
        except OSError as exc:
            _LOG.error("Stream failed: %s — retry in 5s", exc)
            time.sleep(5.0)
        if _stop:
            break
        _LOG.warning("Log stream ended; restarting tail in 2s")
        time.sleep(2.0)

    _LOG.info("Auditor stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
