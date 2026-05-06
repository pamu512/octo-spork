"""Stability remediation for local Docker + Ollama (``octo-spork doctor --fix``)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 120.0,
) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def detect_docker_oom_signals() -> dict[str, Any]:
    """Best-effort detection of Docker / container OOM conditions.

    Checks:
    - Containers whose inspect reports ``State.OOMKilled`` (includes exited instances).
    - Optional Docker daemon warning lines from ``docker info`` (best-effort).
    """
    result: dict[str, Any] = {
        "oom_killed_containers": [],
        "daemon_hints": [],
        "docker_ok": False,
    }
    rc, out, err = _run(["docker", "info"], timeout=45)
    result["docker_ok"] = rc == 0
    blob = ((out or "") + "\n" + (err or "")).lower()
    if not result["docker_ok"]:
        result["daemon_hints"].append(f"docker info failed (rc={rc}); daemon may be unhealthy or OOM-hostile.")
        return result

    if "out of memory" in blob or "oom" in blob or "cannot allocate memory" in blob:
        result["daemon_hints"].append("docker info output mentions memory pressure / OOM-related wording.")

    rc2, ids_out, _ = _run(["docker", "ps", "-aq"], timeout=60)
    if rc2 != 0:
        return result

    for cid in ids_out.splitlines():
        cid = cid.strip()
        if not cid:
            continue
        rc3, insp, _ = _run(
            ["docker", "inspect", "-f", "{{.State.OOMKilled}} {{.State.Status}} {{.Name}}", cid],
            timeout=30,
        )
        if rc3 != 0:
            continue
        parts = insp.strip().split(None, 2)
        if len(parts) >= 3 and parts[0].lower() == "true":
            result["oom_killed_containers"].append(parts[2].strip())

    return result


def prune_builder_caches(*, accept: bool) -> tuple[bool, str]:
    """Prune BuildKit / legacy builder caches."""
    if not accept:
        return False, "skipped (pass --accept-prune to run `docker builder prune -af`)"
    rc, out, err = _run(["docker", "builder", "prune", "-af"], timeout=600)
    tail = ((out or "") + (err or "")).strip()[-800:]
    if rc != 0:
        return False, f"docker builder prune failed rc={rc}: {tail}"
    return True, tail or "docker builder prune completed"


def detect_ollama_num_gpu() -> int:
    """Return a sane ``OLLAMA_NUM_GPU`` for this host (CUDA GPUs only)."""
    rc, out, _ = _run(["nvidia-smi", "-L"], timeout=15)
    if rc == 0 and out.strip():
        lines = [ln for ln in out.splitlines() if ln.strip()]
        gpu_lines = [ln for ln in lines if re.match(r"^\s*GPU\s+", ln)]
        if gpu_lines:
            return len(gpu_lines)
        return len(lines) if lines else 1

    if sys.platform == "darwin":
        return 0

    rc2, out2, _ = _run(["rocm-smi", "-i"], timeout=10)
    if rc2 == 0 and out2.strip():
        return 1

    return 0


def upsert_env_key(env_path: Path, key: str, value: str) -> None:
    """Insert or replace ``KEY=value`` while preserving unrelated lines."""
    if not env_path.parent.is_dir():
        env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    prefix = f"{key}="
    out_lines: list[str] = []
    found = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out_lines.append(line)
            continue
        k = line.split("=", 1)[0].strip()
        if k == key:
            out_lines.append(f"{key}={value}")
            found = True
        else:
            out_lines.append(line)
    if not found:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append(f"# Set automatically by `octo-spork doctor --fix` ({Path(__file__).name})")
        out_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def run_stability_fix(repo_root: Path, env_file: Path, *, accept_prune: bool) -> int:
    """Run stability remediation and bring the stack back up."""
    repo_root = repo_root.expanduser().resolve()
    env_file = env_file.expanduser().resolve()

    print("=== octo-spork doctor --fix (stability) ===", flush=True)

    oom = detect_docker_oom_signals()
    print("\n[1] Docker / OOM signals", flush=True)
    if oom["oom_killed_containers"]:
        print(
            "  Containers previously OOM-killed (inspect): "
            + ", ".join(oom["oom_killed_containers"]),
            flush=True,
        )
    else:
        print("  No OOMKilled containers reported by docker inspect.", flush=True)
    for hint in oom["daemon_hints"]:
        print(f"  Note: {hint}", flush=True)
    if not oom["docker_ok"]:
        print("  Docker daemon does not look healthy — fix Docker before retrying.", flush=True)
        return 1

    print("\n[2] Prune unused builder caches", flush=True)
    ok_prune, prune_msg = prune_builder_caches(accept=accept_prune)
    print(f"  {prune_msg}", flush=True)

    print("\n[3] OLLAMA_NUM_GPU from hardware", flush=True)
    n_gpu = detect_ollama_num_gpu()
    upsert_env_key(env_file, "OLLAMA_NUM_GPU", str(n_gpu))
    print(f"  Set OLLAMA_NUM_GPU={n_gpu} in {env_file}", flush=True)

    print("\n[4] Restart stack (docker compose up -d)", flush=True)
    try:
        import local_ai_stack.__main__ as stack_main

        stack_main._ensure_env_file(env_file)
        stack_main.command_up(env_file, rewrite_conflicting_ports=True)
    except Exception as exc:
        print(f"  Stack restart failed: {exc}", flush=True)
        return 1

    print("\nDone. If issues persist: increase Docker Desktop memory, reduce concurrent models, then re-run.", flush=True)
    return 0
