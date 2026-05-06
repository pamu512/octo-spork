"""Background **Bash / network** usage auditor for the Claude Code agent container.

Best-effort: streams ``docker logs -f`` for the agent service and applies heuristics to each line.
Claude Code may or may not echo raw shell commands in container logs; when it does, this process
can react. For higher assurance, combine with read-only rootfs, seccomp, or a custom agent
runtime that wraps BashTool.

On violation:

- ``docker kill -s KILL <container>``
- Append one line to ``<repo>/logs/agent_security.log``

Violations:

- **rm_rf** — line matches ``rm`` usage with ``-rf`` (destructive recursive delete).
- **curl_public_ip** — ``curl`` toward a **global (public) IPv4** literal in the line (private /
  loopback / link-local addresses are ignored).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = REPO_ROOT / "logs"
DEFAULT_LOG_FILE = LOG_DIR / "agent_security.log"
DEFAULT_CONTAINER = "local-ai-claude-agent"

_CURL_RE = re.compile(
    r"\bcurl\b[^\n;|&`]*?(?P<ip>\d{1,3}(?:\.\d{1,3}){3})",
    re.IGNORECASE,
)

_LOG = logging.getLogger("agent_security_auditor")


def _looks_like_rm_rf(line: str) -> bool:
    """Detect destructive ``rm`` invocations such as ``rm -rf`` / ``rm -r -f``."""
    if not re.search(r"\brm\b", line):
        return False
    nospace = re.sub(r"\s+", "", line)
    if "-rf" in nospace or "-fr" in nospace:
        return True
    m = re.search(r"\brm\b", line)
    if not m:
        return False
    tail = line[m.end() :]
    # ``rm -r -f path`` style (two short flags)
    return bool(
        re.search(r"(?:^|\s)-r(?:\s|$)", tail)
        and re.search(r"(?:^|\s)-f(?:\s|$)", tail)
    )


def _public_ipv4_in_curl(line: str) -> str | None:
    m = _CURL_RE.search(line)
    if not m:
        return None
    raw = m.group("ip")
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if ip.version != 4:
        return None
    if ip.is_global:
        return str(ip)
    return None


def classify_line(line: str) -> str | None:
    """Return violation kind, or ``None`` if the line is acceptable."""
    if _looks_like_rm_rf(line):
        return "rm_rf"
    if _public_ipv4_in_curl(line) is not None:
        return "curl_public_ip"
    return None


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


def kill_agent_container(container: str) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["docker", "kill", "-s", "KILL", container],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return False, "docker CLI not found"
    except subprocess.TimeoutExpired:
        return False, "docker kill timed out"
    if proc.returncode == 0:
        return True, "SIGKILL sent"
    err = (proc.stderr or proc.stdout or "").strip()
    return False, err or f"exit {proc.returncode}"


def handle_violation(
    *,
    kind: str,
    line: str,
    container: str,
    log_file: Path,
) -> None:
    detail = kind
    if kind == "curl_public_ip":
        ip = _public_ipv4_in_curl(line)
        detail = f"curl_public_ip:{ip}" if ip else kind
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
