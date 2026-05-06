#!/usr/bin/env python3
"""Pick the best locally available Ollama model from a preference list and sync ``CLAUDE_CODE_MODEL``.

Queries ``/api/tags``, chooses the first preference entry that matches an installed tag (ordered
from most capable / preferred first), writes ``CLAUDE_CODE_MODEL`` into the mounted Claude config
``.env`` on disk, then optionally restarts the agent container so :func:`dotenv` reload picks it up.

Environment (optional):

- ``OLLAMA_URL`` — base URL for Ollama (default ``http://127.0.0.1:11434`` for host runs; use
  ``http://ollama:11434`` inside Docker when ``extra_hosts`` maps ``ollama``).
- ``CLAUDE_CODE_PREFERRED_MODELS`` — comma-separated override for the preference order.
- ``CLAUDE_CONFIG_ENV_FILE`` — path to the ``.env`` file to update (default
  ``<repo>/.local/claude_config/.env``).
- ``CLAUDE_AGENT_CONTAINER`` — container name for ``docker restart`` (default
  ``local-ai-claude-agent``). Set empty to skip restart.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

REPO_ROOT = Path(__file__).resolve().parents[2]

# Highest priority first (most capable / preferred for coding workloads).
DEFAULT_PREFERRED_MODELS: tuple[str, ...] = (
    "qwen3-coder",
    "llama3.1:70b",
    "llama3.1",
    "qwen2.5-coder",
    "qwen2.5:14b",
    "qwen2.5",
    "llama3.2",
    "llama3",
)


def _default_config_env_path() -> Path:
    override = (os.environ.get("CLAUDE_CONFIG_ENV_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (REPO_ROOT / ".local" / "claude_config" / ".env").resolve()


def _default_ollama_base() -> str:
    return (
        (os.environ.get("OLLAMA_URL") or "").strip()
        or (os.environ.get("OLLAMA_LOCAL_URL") or "").strip()
        or (os.environ.get("OLLAMA_BASE_URL") or "").strip()
        or "http://127.0.0.1:11434"
    ).rstrip("/")


def _parse_preferred_list(raw: str | None) -> list[str]:
    if raw and str(raw).strip():
        return [p.strip() for p in str(raw).split(",") if p.strip()]
    env_raw = (os.environ.get("CLAUDE_CODE_PREFERRED_MODELS") or "").strip()
    if env_raw:
        return [p.strip() for p in env_raw.split(",") if p.strip()]
    return list(DEFAULT_PREFERRED_MODELS)


def fetch_ollama_model_tags(base_url: str, *, timeout_sec: float = 15.0) -> list[str]:
    """Return model ``name`` strings from Ollama ``GET /api/tags``."""
    base = base_url.rstrip("/")
    url = urljoin(base + "/", "api/tags")
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Ollama HTTP {exc.code} at {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama unreachable at {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"Ollama timed out at {url}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from Ollama at {url}") from exc

    models = data.get("models")
    if not isinstance(models, list):
        return []

    names: list[str] = []
    for item in models:
        if isinstance(item, dict):
            n = item.get("name")
            if isinstance(n, str) and n.strip():
                names.append(n.strip())
    return names


def _base_name(model_tag: str) -> str:
    return model_tag.split(":", 1)[0].strip().lower()


def model_matches_preference(installed_tag: str, preference: str) -> bool:
    """True if ``installed_tag`` satisfies ``preference`` (Ollama registry name)."""
    pref = preference.strip().lower()
    if not pref:
        return False
    tag = installed_tag.strip()
    tl = tag.lower()
    if tl == pref:
        return True
    if tl.startswith(pref + ":"):
        return True
    if _base_name(tag) == pref:
        return True
    # Allow preference without tag to match same base (e.g. llama3.1 vs llama3.1:70b-instruct-q4_0)
    if tl.startswith(pref) and (len(tl) == len(pref) or tl[len(pref)] in ":_-."):
        return True
    return False


def select_best_model(available_tags: list[str], preferred: list[str]) -> str | None:
    """First preference entry (in order) that matches any installed tag."""
    if not available_tags:
        return None
    for pref in preferred:
        for tag in available_tags:
            if model_matches_preference(tag, pref):
                return tag
    return None


def upsert_env_key(path: Path, key: str, value: str) -> None:
    """Create or update ``KEY=value`` in a line-oriented ``.env`` file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()

    out: list[str] = []
    found = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        k, _, _ = line.partition("=")
        if k.strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def maybe_restart_container(container_name: str | None) -> tuple[bool, str]:
    """Return (ok, message) from ``docker restart``."""
    if not container_name or not str(container_name).strip():
        return True, "restart skipped (no container name)"
    name = str(container_name).strip()
    try:
        proc = subprocess.run(
            ["docker", "restart", name],
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError:
        return False, "docker CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return False, f"docker restart {name!r} timed out"
    except OSError as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, f"restarted container {name!r}"
    err = (proc.stderr or proc.stdout or "").strip()
    return False, f"docker restart failed ({proc.returncode}): {err}"


def run_sync(
    *,
    ollama_base: str,
    preferred: list[str],
    env_file: Path,
    dry_run: bool,
    restart_container: bool,
    container_name: str | None,
) -> int:
    tags = fetch_ollama_model_tags(ollama_base)
    if not tags:
        print(f"No models reported by Ollama at {ollama_base}/api/tags.", file=sys.stderr)
        return 2

    chosen = select_best_model(tags, preferred)
    if not chosen:
        print(
            "None of the preferred models are installed. "
            f"Preferences: {', '.join(preferred)}.\n"
            f"Available: {', '.join(tags[:24])}{'…' if len(tags) > 24 else ''}.",
            file=sys.stderr,
        )
        return 3

    print(f"Selected model: {chosen}")
    print(f"Preference order used: {', '.join(preferred)}")

    if dry_run:
        print(f"DRY RUN: would set CLAUDE_CODE_MODEL={chosen} in {env_file}")
        return 0

    upsert_env_key(env_file, "CLAUDE_CODE_MODEL", chosen)
    print(f"Updated {env_file}: CLAUDE_CODE_MODEL={chosen}")

    if not restart_container:
        print("Restart skipped (--no-restart). Reload config by restarting the agent container.")
        return 0

    cname = (container_name or "").strip() or (
        (os.environ.get("CLAUDE_AGENT_CONTAINER") or "").strip() or "local-ai-claude-agent"
    )
    ok, msg = maybe_restart_container(cname)
    print(msg)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync CLAUDE_CODE_MODEL to the best locally available Ollama tag.",
    )
    parser.add_argument(
        "--ollama",
        default=None,
        help=f"Ollama base URL (default: env or {_default_ollama_base()!r})",
    )
    parser.add_argument(
        "--preferred",
        default=None,
        help="Comma-separated preference list (highest priority first). Overrides env.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to .env file to update (default: <repo>/.local/claude_config/.env)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print chosen model only; do not write files or restart Docker.",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not run docker restart after updating .env.",
    )
    parser.add_argument(
        "--container",
        default=None,
        help="Docker container name to restart (default: CLAUDE_AGENT_CONTAINER or local-ai-claude-agent).",
    )
    args = parser.parse_args(argv)

    ollama = (args.ollama or "").strip() or _default_ollama_base()
    preferred = _parse_preferred_list(args.preferred)
    env_path = args.env_file.expanduser().resolve() if args.env_file else _default_config_env_path()

    restart = not args.no_restart
    container = args.container or os.environ.get("CLAUDE_AGENT_CONTAINER")

    try:
        return run_sync(
            ollama_base=ollama,
            preferred=preferred,
            env_file=env_path,
            dry_run=args.dry_run,
            restart_container=restart,
            container_name=container,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
