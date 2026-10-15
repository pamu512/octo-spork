"""Port planning helpers for the local AI stack: URL/port parsing, free-port
selection, ``.env`` rewrite, and port-conflict fix computation.

Pure move from ``__main__`` (work w-1792025745-29378): function bodies are
byte-identical to their previous definitions in ``__main__``, which re-exports
them for backward compatibility.
"""
from __future__ import annotations

import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, urlunparse


def _parse_ollama_local_url(url: str) -> tuple[str, int]:
    if not url or not str(url).strip():
        raise ValueError("OLLAMA_LOCAL_URL is empty")
    raw = str(url).strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid Ollama URL: {url!r}") from exc
    if not parsed.hostname:
        raise ValueError(f"OLLAMA_LOCAL_URL has no host: {url!r}")
    if parsed.port is not None:
        return parsed.hostname, int(parsed.port)
    try:
        return parsed.hostname, 11434
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Could not determine Ollama port from: {url!r}") from exc


def _parse_positive_int_from_env(value: str | None, key_name: str, default: int) -> int:
    if value is None or not str(value).strip():
        return int(default)
    try:
        n = int(str(value).strip(), 10)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key_name} must be a positive integer, got {value!r}") from exc
    if n < 1 or n > 65535:
        raise ValueError(f"{key_name} must be between 1 and 65535, got {n}")
    return n


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
        except OSError as exc:
            return False
        return result == 0
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _http_get_status(url: str, timeout: float = 5.0) -> int | None:
    if not url or not str(url).strip():
        raise ValueError("url is empty")
    try:
        request = urllib.request.Request(
            str(url).strip(),
            method="GET",
            headers={"User-Agent": "local-ai-stack-orchestrator/1.0"},
        )
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:  # noqa: S310
            code = getattr(response, "status", None)
            if code is None:
                code = response.getcode()
            return int(code)
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code)
        except (TypeError, ValueError):
            return None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, TypeError):
        return None


def _find_alternative_host_port(host: str, start_candidate: int, max_attempts: int = 96) -> int:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    end = min(65535, int(start_candidate) + int(max_attempts))
    for candidate in range(int(start_candidate), end + 1):
        if candidate < 1 or candidate > 65535:
            continue
        if not _tcp_port_is_in_use(host, candidate):
            return candidate
    raise RuntimeError(f"No free TCP port found on {host!r} starting near {start_candidate}")


def _stack_port_plan(env_values: dict[str, str]) -> list[tuple[str, int]]:
    fe = _parse_positive_int_from_env(
        env_values.get("AGENTIC_FRONTEND_PORT"),
        "AGENTIC_FRONTEND_PORT",
        3010,
    )
    be = _parse_positive_int_from_env(
        env_values.get("AGENTIC_BACKEND_PORT"),
        "AGENTIC_BACKEND_PORT",
        7777,
    )
    sx = _parse_positive_int_from_env(env_values.get("SEARXNG_PORT"), "SEARXNG_PORT", 8080)
    ow = _parse_positive_int_from_env(
        env_values.get("OPEN_WEBUI_PORT"),
        "OPEN_WEBUI_PORT",
        3001,
    )
    n8 = _parse_positive_int_from_env(env_values.get("N8N_PORT"), "N8N_PORT", 5678)
    _, ollama_port = _parse_ollama_local_url(
        env_values.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434"),
    )
    return [
        ("AGENTIC_FRONTEND_PORT", fe),
        ("AGENTIC_BACKEND_PORT", be),
        ("SEARXNG_PORT", sx),
        ("OPEN_WEBUI_PORT", ow),
        ("N8N_PORT", n8),
        ("OLLAMA_LOCAL_URL port", ollama_port),
    ]


def _ollama_tags_probe_url_from_env(env_values: dict[str, str]) -> str:
    base = env_values.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434").strip()
    if not base:
        base = "http://127.0.0.1:11434"
    return base.rstrip("/") + "/api/tags"


def _format_ollama_local_url_with_port(previous_raw: str | None, new_port: int) -> str:
    if new_port < 1 or new_port > 65535:
        raise ValueError(f"new_port out of range: {new_port}")
    raw = (previous_raw or "").strip() or "http://127.0.0.1:11434"
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid OLLAMA_LOCAL_URL: {previous_raw!r}") from exc
    scheme = parsed.scheme or "http"
    host = parsed.hostname
    if host is None:
        host = "127.0.0.1"
    host_norm = host.strip("[]")
    if ":" in host_norm:
        netloc = f"[{host_norm}]:{int(new_port)}"
    else:
        netloc = f"{host_norm}:{int(new_port)}"
    return urlunparse((scheme, netloc, parsed.path or "", parsed.params, parsed.query, parsed.fragment))


def _pick_first_free_tcp_port(host: str, start_candidate: int, reserved_tcp_ports: set[int]) -> int:
    if not host or not str(host).strip():
        raise ValueError("host is empty")
    begin = max(1, int(start_candidate))
    if begin > 65535:
        raise RuntimeError(f"No TCP ports left to try starting from {start_candidate}")
    for candidate in range(begin, 65536):
        if candidate in reserved_tcp_ports:
            continue
        try:
            in_use = _tcp_port_is_in_use(str(host).strip(), int(candidate))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"TCP probe failed for port {candidate} on {host!r}") from exc
        if not in_use:
            return int(candidate)
    raise RuntimeError(f"No free TCP port found on {host!r} starting from {start_candidate}")


def _rewrite_env_file_string_values(env_file: Path, updates: dict[str, str]) -> None:
    if env_file is None:
        raise ValueError("env_file is required")
    if not updates:
        return
    for key in updates:
        if not key or not str(key).strip():
            raise ValueError("update keys must be non-empty")
    path = Path(env_file).resolve()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not read env file for rewrite: {path}") from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Env file is not valid UTF-8: {path}") from exc
    lines = raw.splitlines()
    keys_remaining = dict(updates)
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key_part, sep, _value_part = line.partition("=")
        key_name = key_part.strip()
        if key_name in keys_remaining:
            replacement = keys_remaining[key_name]
            new_lines.append(f"{key_name}={replacement}")
            try:
                del keys_remaining[key_name]
            except KeyError:
                pass
        else:
            new_lines.append(line)
    for key_name in sorted(keys_remaining.keys()):
        new_lines.append(f"{key_name}={keys_remaining[key_name]}")
    out = "\n".join(new_lines) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(out, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Could not write env file: {path}") from exc


def _compute_port_fix_updates(env_values: dict[str, str]) -> dict[str, str]:
    if env_values is None:
        raise ValueError("env_values is required")
    work: dict[str, str] = dict(env_values)
    accumulated: dict[str, str] = {}
    bind_host = "127.0.0.1"
    max_inner_passes = 96
    for _ in range(max_inner_passes):
        try:
            plan = _stack_port_plan(work)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid port configuration while computing fixes: {exc}") from exc
        reserved_tcp_ports = {port for _label, port in plan}
        fixed_one = False
        for label, port in plan:
            try:
                in_use = _tcp_port_is_in_use(bind_host, port)
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"Could not check TCP port {port} ({label}) while computing fixes."
                ) from exc
            if not in_use:
                continue
            if label == "OLLAMA_LOCAL_URL port":
                probe_url = _ollama_tags_probe_url_from_env(work)
                try:
                    status = _http_get_status(probe_url, timeout=5.0)
                except ValueError as exc:
                    raise RuntimeError(f"Invalid Ollama probe URL {probe_url!r}") from exc
                if status == 200:
                    continue
                try:
                    new_port = _pick_first_free_tcp_port(bind_host, port + 1, reserved_tcp_ports)
                except RuntimeError as exc:
                    raise RuntimeError(
                        "Could not allocate a free port for Ollama; free a port manually or "
                        "edit OLLAMA_LOCAL_URL / OLLAMA_HOST."
                    ) from exc
                reserved_tcp_ports.discard(port)
                reserved_tcp_ports.add(new_port)
                previous_ollama_url = work.get("OLLAMA_LOCAL_URL", "http://127.0.0.1:11434")
                try:
                    new_url = _format_ollama_local_url_with_port(previous_ollama_url, new_port)
                except ValueError as exc:
                    raise RuntimeError("Could not rebuild OLLAMA_LOCAL_URL with new port") from exc
                new_host_binding = f"0.0.0.0:{new_port}"
                work["OLLAMA_LOCAL_URL"] = new_url
                work["OLLAMA_HOST"] = new_host_binding
                accumulated["OLLAMA_LOCAL_URL"] = new_url
                accumulated["OLLAMA_HOST"] = new_host_binding
                fixed_one = True
                break
            env_key = label
            try:
                new_port = _pick_first_free_tcp_port(bind_host, port + 1, reserved_tcp_ports)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"Could not allocate a free port for {env_key}; free a port or edit {env_key} manually."
                ) from exc
            reserved_tcp_ports.discard(port)
            reserved_tcp_ports.add(new_port)
            new_val = str(new_port)
            work[env_key] = new_val
            accumulated[env_key] = new_val
            if env_key == "AGENTIC_BACKEND_PORT":
                backend_url = f"http://localhost:{new_val}"
                work["BACKEND_PORT"] = new_val
                accumulated["BACKEND_PORT"] = new_val
                work["REACT_APP_BACKEND_URL"] = backend_url
                accumulated["REACT_APP_BACKEND_URL"] = backend_url
            fixed_one = True
            break
        if not fixed_one:
            break
    return accumulated
