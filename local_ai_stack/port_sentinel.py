"""PortSentinel: probe canonical stack TCP ports before `up`, resolve conflicts interactively.

Checks 11434 (Ollama host), 5432 (Postgres), 6379 (Redis), 8080 (SearXNG). When a port is
busy, identifies listener PIDs and offers (1) terminating stale Docker ``docker-proxy``
listeners or (2) remapping via env updates plus an optional temporary compose override.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PORT_SENTINEL_OVERRIDE_REL = Path("deploy") / "local-ai" / "docker-compose.port-sentinel.override.yml"
REQUIRED_PORTS: tuple[int, ...] = (11434, 5432, 6379, 8080)

_DOCKER_PROXY_COMMS: frozenset[str] = frozenset(
    {
        "docker-proxy",
        "docker-proxy-current",
    }
)


def _tcp_port_is_in_use(host: str, port: int, connect_timeout: float = 0.4) -> bool:
    if port < 1 or port > 65535:
        raise ValueError(f"port out of range: {port}")
    if not host or not str(host).strip():
        raise ValueError("host is empty")
    host_s = str(host).strip()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(float(connect_timeout))
        except (TypeError, ValueError) as exc:
            raise ValueError("connect_timeout must be a finite number") from exc
        try:
            result = sock.connect_ex((host_s, int(port)))
        except OSError:
            return False
        return result == 0
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _lsof_listening_pids(port: int) -> list[int]:
    args = ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return []
    pids: list[int] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line, 10))
        except ValueError:
            continue
    out: list[int] = []
    seen: set[int] = set()
    for pid in pids:
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def _ss_listening_pids(port: int) -> list[int]:
    try:
        proc = subprocess.run(
            ["ss", "-Hlpnt", f"sport = :{int(port)}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return []
    pids: list[int] = []
    for line in (proc.stdout or "").splitlines():
        m = re.search(r"pid=(\d+)", line)
        if m:
            try:
                pids.append(int(m.group(1), 10))
            except ValueError:
                continue
    out: list[int] = []
    seen: set[int] = set()
    for pid in pids:
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    return out


def listening_process_ids(port: int) -> tuple[int, ...]:
    pids = _lsof_listening_pids(port)
    if not pids:
        pids = _ss_listening_pids(port)
    return tuple(pids)


def process_command_name(pid: int) -> str:
    """Best-effort short process name for *pid*."""
    plat = sys.platform.lower()
    try:
        if plat.startswith("linux"):
            comm_path = Path(f"/proc/{int(pid)}/comm")
            if comm_path.is_file():
                return comm_path.read_text(encoding="utf-8", errors="replace").strip()
        proc = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return (proc.stdout or "").strip().splitlines()[-1].strip()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def is_stale_docker_listener(pid: int, comm: str) -> bool:
    """True when *pid* looks like Docker's userspace TCP proxy (safe-ish to SIGTERM)."""
    if comm in _DOCKER_PROXY_COMMS:
        return True
    c = comm.lower()
    return "docker-proxy" in c


def kill_listener(pid: int, *, logger: logging.Logger) -> bool:
    try:
        os.kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        logger.info("PortSentinel: PID %s already exited", pid)
        return True
    except PermissionError as exc:
        logger.error("PortSentinel: permission denied sending SIGTERM to PID %s: %s", pid, exc)
        return False
    except OSError as exc:
        logger.error("PortSentinel: could not signal PID %s: %s", pid, exc)
        return False
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        time.sleep(0.15)
    try:
        os.kill(int(pid), signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError as exc:
        logger.error("PortSentinel: SIGKILL failed for PID %s: %s", pid, exc)
        return False
    return True


def _sentinel_disabled() -> bool:
    return (os.environ.get("OCTO_PORT_SENTINEL") or "").strip().lower() in {"0", "false", "no", "off"}


def _default_action_interactive() -> str:
    raw = (os.environ.get("OCTO_PORT_SENTINEL_ACTION") or "").strip().lower()
    if raw in {"prompt", "kill", "remap", "skip", "abort"}:
        return raw
    return "prompt"


def _default_action_non_interactive() -> str:
    raw = (os.environ.get("OCTO_PORT_SENTINEL_ACTION") or "").strip().lower()
    if raw in {"kill", "remap", "skip", "abort"}:
        return raw
    return "remap"


def _import_main_helpers() -> Any:
    from local_ai_stack import __main__ as main_mod

    return main_mod


def _stack_remappable_keys_for_port(port: int, env_values: dict[str, str]) -> tuple[str, ...] | None:
    m = _import_main_helpers()
    _, ollama_port = m._parse_ollama_local_url(env_values.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434"))
    if port == int(ollama_port):
        return ("OLLAMA_LOCAL_URL", "OLLAMA_HOST")
    sx = m._parse_positive_int_from_env(env_values.get("SEARXNG_PORT"), "SEARXNG_PORT", 8080)
    if port == int(sx):
        return ("SEARXNG_PORT",)
    pg = m._parse_positive_int_from_env(env_values.get("POSTGRES_PORT"), "POSTGRES_PORT", 5433)
    if port == int(pg):
        return ("POSTGRES_PORT",)
    if port == 6379:
        # Publish Valkey/Redis on a new host port via compose override (addons omit host ports by default).
        return ("REDIS_HOST_PORT",)
    return None


def _should_skip_ollama_busy(port: int, env_values: dict[str, str]) -> bool:
    if port != 11434:
        return False
    m = _import_main_helpers()
    probe = m._ollama_tags_probe_url_from_env(env_values)
    try:
        status = m._http_get_status(probe, timeout=5.0)
    except ValueError:
        return False
    return status == 200


@dataclass(frozen=True)
class PortConflict:
    port: int
    pids: tuple[int, ...]
    process_names: tuple[str, ...]


def _gather_conflicts(bind_host: str, env_values: dict[str, str]) -> list[PortConflict]:
    conflicts: list[PortConflict] = []
    for port in REQUIRED_PORTS:
        try:
            if not _tcp_port_is_in_use(bind_host, port):
                continue
        except (OSError, ValueError):
            continue
        if _should_skip_ollama_busy(port, env_values):
            continue
        pids = listening_process_ids(port)
        names = tuple(process_command_name(pid) for pid in pids) if pids else ("(could not resolve PID)",)
        conflicts.append(PortConflict(port=port, pids=pids, process_names=names))
    return conflicts


def _pick_free_host_port(m: Any, bind_host: str, start: int, reserved: set[int]) -> int:
    return int(m._pick_first_free_tcp_port(bind_host, int(start), set(reserved)))


def _apply_remap_keys(
    env_values: dict[str, str],
    keys: tuple[str, ...],
    new_host_port: int,
    m: Any,
) -> dict[str, str]:
    updates: dict[str, str] = {}
    if keys == ("OLLAMA_LOCAL_URL", "OLLAMA_HOST"):
        prev = env_values.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434")
        updates["OLLAMA_LOCAL_URL"] = m._format_ollama_local_url_with_port(prev, new_host_port)
        updates["OLLAMA_HOST"] = f"0.0.0.0:{new_host_port}"
    elif keys == ("SEARXNG_PORT",):
        updates["SEARXNG_PORT"] = str(new_host_port)
    elif keys == ("POSTGRES_PORT",):
        updates["POSTGRES_PORT"] = str(new_host_port)
    elif keys == ("REDIS_HOST_PORT",):
        updates["REDIS_HOST_PORT"] = str(new_host_port)
    else:
        raise ValueError(f"unsupported remap keys: {keys}")
    return updates


def _render_override_yaml(payload: dict[str, Any]) -> str:
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    except Exception:
        lines = [
            "# Generated by PortSentinel — safe to delete after `docker compose down`.",
            "x-octo-port-sentinel:",
            "  version: 1",
        ]
        meta = payload.get("x-octo-port-sentinel") if isinstance(payload, dict) else None
        if isinstance(meta, dict):
            for k, v in meta.items():
                lines.append(f"  {k}: {v!r}")
        lines.append("services: {}")
        return "\n".join(lines) + "\n"


def _merge_override_services(
    existing: dict[str, Any] | None,
    port: int,
    new_host_port: int,
    keys: tuple[str, ...],
) -> dict[str, Any]:
    """Merge compose override: Redis host binding only (other remaps use env interpolation)."""
    root: dict[str, Any] = dict(existing or {})
    root.setdefault("x-octo-port-sentinel", {})
    if not isinstance(root["x-octo-port-sentinel"], dict):
        root["x-octo-port-sentinel"] = {}
    remap_block = root["x-octo-port-sentinel"].setdefault("remapped_ports", {})
    if not isinstance(remap_block, dict):
        remap_block = {}
        root["x-octo-port-sentinel"]["remapped_ports"] = remap_block
    remap_block[str(port)] = int(new_host_port)

    if keys == ("REDIS_HOST_PORT",):
        svc = dict(root.get("services") or {})
        host_side = str(int(new_host_port))
        svc["redis"] = {"ports": [host_side + ":6379"]}
        root["services"] = svc
    return root


def sentinel_override_path(root: Path) -> Path:
    return (root / PORT_SENTINEL_OVERRIDE_REL).resolve()


def run_port_sentinel(
    root: Path,
    env_file: Path,
    env_values: dict[str, str],
    *,
    logger: logging.Logger,
    bind_host: str = "127.0.0.1",
) -> dict[str, str]:
    """Return possibly updated env values; may rewrite *env_file* and compose override yaml."""
    if _sentinel_disabled():
        logger.info("PortSentinel skipped (OCTO_PORT_SENTINEL disables).")
        return env_values

    conflicts = _gather_conflicts(bind_host, env_values)
    if not conflicts:
        return env_values

    m = _import_main_helpers()
    current = dict(env_values)
    interactive = sys.stdin.isatty()
    override_path = sentinel_override_path(root)
    yaml_root: dict[str, Any] | None = None
    if override_path.is_file():
        try:
            raw = override_path.read_text(encoding="utf-8")
            try:
                import yaml  # type: ignore[import-untyped]

                loaded = yaml.safe_load(raw)
                if isinstance(loaded, dict):
                    yaml_root = loaded
            except Exception:
                yaml_root = None
        except OSError:
            yaml_root = None

    yaml_root = dict(yaml_root or {})
    yaml_root.setdefault("x-octo-port-sentinel", {})
    assert isinstance(yaml_root["x-octo-port-sentinel"], dict)
    yaml_root["x-octo-port-sentinel"]["version"] = 1
    yaml_root["x-octo-port-sentinel"]["generated_at"] = datetime.now(timezone.utc).isoformat()

    for c in conflicts:
        port = int(c.port)
        try:
            if not _tcp_port_is_in_use(bind_host, port):
                continue
        except (OSError, ValueError):
            continue
        if _should_skip_ollama_busy(port, current):
            continue
        pids = listening_process_ids(port)
        names = tuple(process_command_name(pid) for pid in pids) if pids else ("(could not resolve PID)",)
        names_summary = ", ".join(f"{pid} ({name})" for pid, name in zip(pids, names)) if pids else ", ".join(names)
        keys_opt = _stack_remappable_keys_for_port(port, current)
        kill_ok = bool(pids) and all(is_stale_docker_listener(pid, n) for pid, n in zip(pids, names))
        msg = (
            f"PortSentinel: TCP {port} is in use on {bind_host}. "
            f"Listener(s): {names_summary}."
        )
        if keys_opt is None:
            msg += (
                " This port does not match the stack's current host bindings "
                "(see POSTGRES_PORT default 5433, SEARXNG_PORT, OLLAMA_LOCAL_URL)."
            )
        logger.warning(msg)
        _print_fn = m._print
        _rewrite = m._rewrite_env_file_string_values

        if interactive:
            action = _default_action_interactive()
        else:
            action = _default_action_non_interactive()

        choice: str | None = None
        if action == "prompt" and interactive:
            parts = ["How do you want to proceed?"]
            if kill_ok:
                parts.append("  [1] Kill stale Docker listener process(es) (docker-proxy)")
            else:
                parts.append("  [1] Kill Docker listener — unavailable (not docker-proxy)")
            if keys_opt is not None:
                parts.append("  [2] Remap stack port (updates env + temporary compose override metadata)")
            else:
                parts.append("  [2] Remap stack port — unavailable for this conflict")
            parts.append("  [3] Abort `up`")
            parts.append("  [4] Ignore for now (may fail later)")
            _print_fn("\n".join(parts))
            try:
                choice = input("Enter 1-4 [4]: ").strip() or "4"
            except EOFError:
                choice = "4"
        elif action == "kill":
            choice = "1"
        elif action == "remap":
            choice = "2"
        elif action == "abort":
            choice = "3"
        else:
            choice = "4"

        if choice == "3":
            raise RuntimeError(f"PortSentinel: aborted due to conflict on TCP port {port}.")

        if choice == "4":
            logger.warning("PortSentinel: ignoring conflict on port %s for now.", port)
            continue

        if choice == "1":
            if not kill_ok:
                if interactive:
                    raise RuntimeError(
                        f"PortSentinel: cannot kill non-docker-proxy listener(s) on port {port}. "
                        "Stop the process manually or choose remap when the stack owns this port."
                    )
                logger.warning(
                    "PortSentinel: kill requested but listener on port %s is not docker-proxy; skipping.",
                    port,
                )
                continue
            for pid in pids:
                if not kill_listener(pid, logger=logger):
                    raise RuntimeError(f"PortSentinel: failed to terminate PID {pid}.")
            _print_fn(f"PortSentinel: signaled Docker listener PID(s) on port {port}.")
            time.sleep(0.4)
            continue

        if choice == "2":
            if keys_opt is None:
                if interactive:
                    raise RuntimeError(
                        f"PortSentinel: cannot remap port {port} — stack is not configured to bind "
                        "this host port (see POSTGRES_PORT, SEARXNG_PORT, OLLAMA_LOCAL_URL, REDIS_HOST_PORT)."
                    )
                logger.warning(
                    "PortSentinel: remap unavailable for port %s (not a stack-bound host port); skipping.",
                    port,
                )
                continue
            reserved: set[int] = set()
            try:
                plan = m._stack_port_plan(current)
                reserved = {port for _l, port in plan}
            except (TypeError, ValueError):
                pass
            for p in REQUIRED_PORTS:
                reserved.add(int(p))
            try:
                new_port = _pick_free_host_port(m, bind_host, port + 1, reserved)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"PortSentinel: could not find a free TCP port to remap from {port}."
                ) from exc
            updates = _apply_remap_keys(current, keys_opt, new_port, m)
            current.update(updates)
            try:
                _rewrite(env_file, updates)
            except RuntimeError as exc:
                raise RuntimeError(f"PortSentinel: could not rewrite env file {env_file}") from exc
            logger.warning(
                "PortSentinel: remapped port %s -> %s via keys %s",
                port,
                new_port,
                ",".join(updates.keys()),
            )
            _print_fn(
                f"PortSentinel: remapped TCP {port} -> {new_port} ({', '.join(sorted(updates.keys()))})."
            )
            yaml_root = _merge_override_services(yaml_root, port, new_port, keys_opt)
            try:
                override_path.parent.mkdir(parents=True, exist_ok=True)
                override_path.write_text(_render_override_yaml(yaml_root), encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(f"PortSentinel: could not write {override_path}") from exc
            continue

        logger.warning("PortSentinel: unrecognized choice %r; ignoring port %s.", choice, port)

    return current
