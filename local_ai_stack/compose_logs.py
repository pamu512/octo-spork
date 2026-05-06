"""Colored ``docker compose logs`` stream (service hues + red severity keywords)."""

from __future__ import annotations

import os
import re
import subprocess
import sys

_RESET = "\033[0m"
_RED_BRIGHT = "\033[91m"
_DIM = "\033[2m"

# Longest prefix wins (container_name-style prefixes).
_SERVICE_PREFIX_COLORS: tuple[tuple[str, str], ...] = (
    ("local-ai-agentic-api", "\033[94m"),  # bright blue — backend / Ollama integration
    ("local-ai-agentic-ui", "\033[96m"),  # cyan — frontend
    ("local-ai-searxng", "\033[35m"),  # magenta
    ("local-ai-redis", "\033[33m"),  # yellow
    ("local-ai-open-webui", "\033[36m"),  # cyan-ish
    ("local-ai-n8n", "\033[92m"),  # green
    ("local-ai-postgres", "\033[95m"),  # bright magenta
    ("redis", "\033[33m"),
    ("searxng", "\033[35m"),
    ("backend", "\033[94m"),
    ("frontend", "\033[96m"),
    ("open-webui", "\033[36m"),
    ("n8n", "\033[92m"),
    ("postgres", "\033[95m"),
)

_KEYWORD_RE = re.compile(r"\b(ERROR|CRITICAL|TIMEOUT)\b", re.IGNORECASE)


def color_for_log_prefix(prefix: str) -> str:
    """Pick ANSI foreground for the compose log prefix (before `` | ``)."""
    pl = prefix.lower()
    for key, color in sorted(_SERVICE_PREFIX_COLORS, key=lambda kv: len(kv[0]), reverse=True):
        if key in pl:
            return color
    return _DIM


def highlight_severity_keywords(fragment: str) -> str:
    """Wrap ERROR / CRITICAL / TIMEOUT tokens in bright red."""

    def _sub(m: re.Match[str]) -> str:
        return f"{_RED_BRIGHT}{m.group(1)}{_RESET}"

    return _KEYWORD_RE.sub(_sub, fragment)


def format_compose_log_line(line: str) -> str:
    """Apply service color to prefix and red highlights in the message body."""
    core = line.rstrip("\r\n")
    sep = " | "
    if sep not in core:
        return highlight_severity_keywords(core) + "\n"
    left, right = core.split(sep, 1)
    svc_color = color_for_log_prefix(left)
    body = highlight_severity_keywords(right)
    return f"{svc_color}{left}{_RESET}{sep}{body}\n"


def run_follow_logs(
    compose_cmd: list[str],
    *,
    tail: str | None = "200",
    follow: bool = True,
    timestamps: bool = False,
    services: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> int:
    """Run ``docker compose … logs`` and stream through :func:`format_compose_log_line`."""
    cmd = list(compose_cmd)
    cmd.append("logs")
    cmd.append("--no-color")
    if timestamps:
        cmd.append("--timestamps")
    if tail is not None:
        cmd.extend(["--tail", str(tail)])
    if follow:
        cmd.append("-f")
    cmd.extend(services)

    popen_env = {**os.environ, **env} if env else None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=popen_env,
        )
    except FileNotFoundError:
        print("docker CLI not found on PATH.", file=sys.stderr)
        return 127

    assert proc.stdout is not None

    try:
        for raw in proc.stdout:
            sys.stdout.write(format_compose_log_line(raw))
            sys.stdout.flush()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 130
    except BrokenPipeError:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        else:
            proc.wait()
    return int(proc.returncode or 0)
