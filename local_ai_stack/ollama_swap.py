"""Swap the active Ollama model via HTTP (`/api/pull`) without restarting Docker."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

LIBRARY_BASE = "https://ollama.com/library/"
DEFAULT_VRAM_HEADROOM = 1.15


def registry_library_url(model: str) -> str:
    """HTTPS URL for the model's library page (tag may contain ``:``)."""
    from urllib.parse import quote

    return LIBRARY_BASE + quote(model, safe="")


def verify_model_on_registry(model: str, *, timeout: float = 30.0) -> tuple[bool, str]:
    """Return (ok, detail). Uses public library HTTP status (404 => unknown model)."""
    url = registry_library_url(model.strip())
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "octo-spork-local-ai-stack/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            if code == 200:
                return True, url
            return False, f"unexpected HTTP {code} for {url}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, f"model not found in Ollama library (HTTP 404): {url}"
        return False, f"library HTTP error {exc.code}: {url}"
    except urllib.error.URLError as exc:
        return False, f"could not reach library: {exc.reason}"
    except OSError as exc:
        return False, f"library request failed: {exc}"


def estimate_model_size_gb_from_library(model: str, *, timeout: float = 30.0) -> tuple[float | None, str]:
    """Best-effort size (GB) from library HTML; tag pages usually list one dominant size."""
    url = registry_library_url(model.strip())
    req = urllib.request.Request(url, headers={"User-Agent": "octo-spork-local-ai-stack/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as exc:
        return None, str(exc)

    sizes = [float(m.group(1)) for m in re.finditer(r"(\d+(?:\.\d+)?)\s*GB", html, flags=re.IGNORECASE)]
    if not sizes:
        return None, "no GB sizes found on library page"
    est = max(sizes)
    return est, f"parsed from library page (max {est} GB among {len(sizes)} matches)"


def _nvidia_free_vram_gb() -> float | None:
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=15,
        )
        mibs = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                mibs.append(float(line.replace(" MiB", "").strip()))
            except ValueError:
                continue
        if not mibs:
            return None
        return max(mibs) / 1024.0
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, OSError):
        return None


def _darwin_free_memory_gb() -> float | None:
    """Approximate free RAM on macOS (unified memory machines — heuristic for Metal)."""
    try:
        page_size = 4096
        free_pages = None
        out = subprocess.check_output(["vm_stat"], text=True, timeout=10)
        for line in out.splitlines():
            line_l = line.lower()
            if "page size of" in line_l and "bytes" in line_l:
                parts = line.split()
                for i, p in enumerate(parts):
                    if p.isdigit() and i + 1 < len(parts) and parts[i + 1].lower() == "bytes":
                        page_size = int(p)
                        break
            if line.startswith("Pages free:"):
                tok = line.split()[2].rstrip(".")
                free_pages = int(tok)
        if free_pages is None:
            return None
        return free_pages * page_size / (1024.0**3)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, OSError):
        return None


def _linux_memavailable_gb() -> float | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        kb = int(parts[1])
                        return kb / (1024.0**2)
        return None
    except OSError:
        return None


def available_memory_for_models_gb() -> tuple[float | None, str]:
    """Return (gigabytes, source label) for the best local probe."""
    v = _nvidia_free_vram_gb()
    if v is not None:
        return v, "nvidia-smi GPU memory.free (GiB→GB)"
    if sys.platform == "darwin":
        v = _darwin_free_memory_gb()
        if v is not None:
            return v, "macOS vm_stat Pages free (heuristic; unified memory)"
    if sys.platform.startswith("linux"):
        v = _linux_memavailable_gb()
        if v is not None:
            return v, "/proc/meminfo MemAvailable"
    return None, "no VRAM/RAM probe available (install drivers or use --ignore-vram)"


def pull_model_http(
    base_url: str,
    model: str,
    *,
    stream_progress: bool = True,
    timeout_s: float | None = 3600.0,
) -> None:
    """``POST {base}/api/pull`` with streaming NDJSON; raises on failure."""
    url = base_url.rstrip("/") + "/api/pull"
    payload = json.dumps({"model": model, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "octo-spork-local-ai-stack/1"},
    )

    def _read_stream(resp: Any) -> None:
        buf = b""
        saw_success = False
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    err = obj.get("error")
                    if err:
                        raise RuntimeError(str(err))
                    if stream_progress:
                        st = obj.get("status")
                        if st:
                            print(f"  pull: {st}", file=sys.stderr, flush=True)
                    if obj.get("status") == "success":
                        saw_success = True
        if buf.strip():
            try:
                obj = json.loads(buf.decode("utf-8").strip())
                if isinstance(obj, dict):
                    if obj.get("error"):
                        raise RuntimeError(str(obj["error"]))
                    if obj.get("status") == "success":
                        saw_success = True
            except json.JSONDecodeError:
                pass
        if not saw_success:
            raise RuntimeError("pull stream ended without success status")

    opener = urllib.request.build_opener()
    handle = None
    try:
        handle = opener.open(req, timeout=timeout_s)
        _read_stream(handle)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"pull HTTP {exc.code}: {body[:500]}") from exc
    finally:
        if handle is not None:
            handle.close()


def maybe_rewrite_env_model(env_file: Path, model: str) -> None:
    """Set ``OLLAMA_MODEL=`` line in *env_file* (same-line KEY=value format)."""
    if not env_file.is_file():
        raise OSError(f"env file not found: {env_file}")
    lines = env_file.read_text(encoding="utf-8").splitlines(keepends=True)
    key = "OLLAMA_MODEL"
    new_lines: list[str] = []
    found = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(f"{key}="):
            new_lines.append(f'{key}={model}\n')
            found = True
        else:
            new_lines.append(line)
    if not found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(f"{key}={model}\n")
    env_file.write_text("".join(new_lines), encoding="utf-8")


def run_swap(
    model: str,
    *,
    ollama_base_url: str,
    env_file: Path | None,
    update_env: bool,
    skip_registry: bool,
    ignore_vram: bool,
    vram_headroom: float,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Verify registry + memory, pull via HTTP, optionally update ``OLLAMA_MODEL`` in env."""
    log = progress or (lambda m: print(m, flush=True))

    m = model.strip()
    if not m:
        raise ValueError("model name is empty")
    if vram_headroom <= 0:
        raise ValueError("vram_headroom must be positive")

    if not skip_registry:
        ok, detail = verify_model_on_registry(m)
        if not ok:
            raise RuntimeError(detail)
        log(f"Registry: OK ({detail})")
    else:
        log("Registry check skipped (--skip-registry-check).")

    est_gb: float | None = None
    est_note = ""
    if not ignore_vram:
        est_gb, est_note = estimate_model_size_gb_from_library(m)
        if est_gb is not None:
            log(f"Estimated model footprint (library page): ~{est_gb:.2f} GB ({est_note})")
        else:
            log(f"Warning: could not estimate model size ({est_note}); VRAM check skipped.")
        avail, src = available_memory_for_models_gb()
        if est_gb is not None and avail is not None:
            need = est_gb * max(1.0, vram_headroom)
            log(f"Available: ~{avail:.2f} GB ({src})")
            if avail < need:
                raise RuntimeError(
                    f"Insufficient memory for pull: need ~{need:.2f} GB (model ~{est_gb:.2f} GB × "
                    f"{vram_headroom} headroom), have ~{avail:.2f} GB. "
                    "Free resources or use --ignore-vram."
                )
        elif avail is None:
            log(f"Warning: {src}")
        else:
            log(f"Available: ~{avail:.2f} GB ({src})")
    else:
        log("VRAM/RAM check skipped (--ignore-vram).")

    base = ollama_base_url.strip().rstrip("/")
    probe = base + "/api/tags"
    try:
        urllib.request.urlopen(probe, timeout=15)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama does not appear reachable at {base} ({exc.reason}).") from exc
    except OSError as exc:
        raise RuntimeError(f"Ollama probe failed at {base}: {exc}") from exc

    log(f"Pulling {m!r} via POST {base}/api/pull …")
    pull_model_http(base, m, stream_progress=True)
    log(f"Pulled {m!r} successfully.")

    if update_env:
        if env_file is None:
            raise ValueError("update_env requires env_file")
        maybe_rewrite_env_model(env_file, m)
        log(f"Updated {env_file} OLLAMA_MODEL={m}")
        log(
            "Note: running containers still use their previous env until restarted or "
            "they read config from disk; AgenticSeek backend picks up OLLAMA_MODEL on next process start."
        )
