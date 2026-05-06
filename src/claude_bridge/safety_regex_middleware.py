"""Safety-regex middleware for Claude agent **Bash** commands.

Scan shell text **before** execution (or in log auditors). Forbidden patterns include destructive
``rm -rf`` (especially filesystem root), ``curl``/``wget`` piped into a shell, writes targeting
``.env`` or compose files, and ``curl`` toward public IPv4 literals.

On violation: optionally ``docker kill -s KILL`` the agent container and raise :exc:`SecurityViolation`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import subprocess
_LOG = logging.getLogger(__name__)

DEFAULT_AGENT_CONTAINER = "local-ai-claude-agent"

_CURL_RE = re.compile(
    r"\bcurl\b[^\n;|&`]*?(?P<ip>\d{1,3}(?:\.\d{1,3}){3})",
    re.IGNORECASE,
)

# curl/wget | sh/bash — remote code execution pattern
_CURL_PIPE_SHELL = re.compile(
    r"\b(?:curl|wget)\b[^\n]{0,800}\|\s*(?:ba)?sh\b",
    re.IGNORECASE,
)

# Shell redirection / append into sensitive repo files
_REDIRECT_PROTECTED = re.compile(
    r"(?:>>|>)\s*[^\n]*?(?:\.env\b|docker-compose\.ya?ml\b|compose\.ya?ml\b)",
    re.IGNORECASE,
)

_SED_INPLACE_PROTECTED = re.compile(
    r"\bsed\b[^\n]*?\s-i(?:\s+[^\n]*)?[^\n]*?(?:\.env\b|docker-compose\.ya?ml\b|compose\.ya?ml\b)",
    re.IGNORECASE,
)

_TEE_PROTECTED = re.compile(
    r"\btee\b[^\n]*?(?:\.env\b|docker-compose\.ya?ml\b|compose\.ya?ml\b)",
    re.IGNORECASE,
)


class SecurityViolation(Exception):
    """Raised when a Bash command matches a forbidden safety pattern."""

    def __init__(self, message: str, *, kind: str, command_excerpt: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.command_excerpt = command_excerpt


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


def _protected_config_write(line: str) -> bool:
    if _REDIRECT_PROTECTED.search(line):
        return True
    if _SED_INPLACE_PROTECTED.search(line):
        return True
    if _TEE_PROTECTED.search(line):
        return True
    return False


def classify_bash_line(line: str) -> str | None:
    """
    Return a violation kind string if *line* matches a forbidden Bash pattern, else ``None``.

    Kinds:

    - ``curl_pipe_shell`` — pipe curl/wget into sh/bash
    - ``protected_config_write`` — redirect/sed/tee touching ``.env`` or compose YAML
    - ``rm_rf`` — ``rm -rf`` / ``rm -r -f`` style deletes
    - ``curl_public_ip`` — curl to a literal **global** IPv4 address
    """
    s = line or ""
    if _CURL_PIPE_SHELL.search(s):
        return "curl_pipe_shell"
    if _protected_config_write(s):
        return "protected_config_write"
    if _looks_like_rm_rf(s):
        return "rm_rf"
    if _public_ipv4_in_curl(s) is not None:
        return "curl_public_ip"
    return None


def violation_detail(kind: str, line: str) -> str:
    if kind == "curl_public_ip":
        ip = _public_ipv4_in_curl(line)
        return f"curl_public_ip:{ip}" if ip else kind
    return kind


def kill_agent_container(container: str) -> tuple[bool, str]:
    """Send ``SIGKILL`` to the running Claude agent container (best-effort)."""
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


def enforce_safe_bash_command(
    command: str,
    *,
    container: str | None = None,
    kill_session: bool = True,
) -> None:
    """
    If *command* matches a forbidden pattern, optionally kill the agent container and raise
    :exc:`SecurityViolation`.

    Use from any middleware that can inspect Bash before it runs (host wrappers, future hooks).
    """
    kind = classify_bash_line(command)
    if kind is None:
        return
    name = (container or "").strip() or (
        os.environ.get("CLAUDE_AGENT_CONTAINER", "").strip()
        or os.environ.get("OCTO_CLAUDE_AGENT_CONTAINER", "").strip()
        or DEFAULT_AGENT_CONTAINER
    )
    excerpt = (command or "").strip().replace("\n", "\\n")[:2000]
    if kill_session:
        ok, msg = kill_agent_container(name)
        _LOG.warning(
            "Safety regex: blocked kind=%s docker_kill_ok=%s (%s) excerpt=%r",
            kind,
            ok,
            msg,
            excerpt[:240],
        )
    raise SecurityViolation(
        f"Forbidden Bash pattern ({kind}); agent session terminated.",
        kind=kind,
        command_excerpt=excerpt,
    )


def scan_bash_command(command: str) -> None:
    """Raise :exc:`SecurityViolation` if *command* is unsafe (alias for :func:`enforce_safe_bash_command`)."""
    enforce_safe_bash_command(command, kill_session=True)
