"""Extended developer environment check for the Octo-spork stack (core checklist + Claude Code)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Status = Literal["green", "yellow", "red"]

# Matches ``COMPOSE_PROJECT_NAME`` in ``local_ai_stack.__main__`` (``docker compose`` image prefix).
CLAUDE_COMPOSE_PROJECT_NAME = "octo-spork-local-ai"
CLAUDE_AGENT_CONTAINER = "local-ai-claude-agent"
CLAUDE_WORKSPACE_ENV = "OCTO_CLAUDE_WORKSPACE"
_DEFAULT_CLAUDE_WORKSPACE_IN_CONTAINER = "/workspace"


@dataclass(frozen=True)
class CheckItem:
    number: int
    title: str
    status: Status
    detail: str
    fix_commands: tuple[str, ...] = ()


def _run(cmd: list[str], *, timeout: float = 25.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
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


def _bytes_human(n: float) -> str:
    for unit, thresh in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if n >= thresh:
            return f"{n / thresh:.1f} {unit}"
    return f"{int(n)} B"


def _check_python() -> CheckItem:
    ver = sys.version_info
    ok = ver.major >= 3 and ver.minor >= 10
    detail = f"{sys.executable} — Python {ver.major}.{ver.minor}.{ver.micro}"
    if ok:
        return CheckItem(0, "Python ≥ 3.10", "green", detail)
    return CheckItem(
        0,
        "Python ≥ 3.10",
        "red",
        detail,
        (
            "Install Python 3.10+ from https://www.python.org/downloads/",
            "macOS: brew install python@3.12",
            "Ubuntu: sudo apt install python3.12",
        ),
    )


def _check_cpu() -> CheckItem:
    machine = platform.machine().lower()
    detail = f"{platform.system()} / {machine} ({platform.processor() or 'CPU'})"
    if machine in {"arm64", "aarch64"}:
        return CheckItem(
            0,
            "CPU / architecture compatibility",
            "yellow",
            detail
            + " — compose images often target amd64; Docker emulates unless you enable native arm64 (AGENTICSEEK_NATIVE_ARM64).",
            (
                "See deploy/local-ai/.env.example → AGENTICSEEK_NATIVE_ARM64",
                "Docker Desktop: enable Rosetta / QEMU for amd64 images on Apple Silicon.",
            ),
        )
    if machine in {"x86_64", "amd64"}:
        return CheckItem(0, "CPU / architecture compatibility", "green", detail + " — matches common linux/amd64 images.")
    return CheckItem(
        0,
        "CPU / architecture compatibility",
        "yellow",
        detail + " — confirm Docker can run the stack’s platform expectations.",
        (),
    )


def _check_gpu() -> CheckItem:
    rc, out, _ = _run(["nvidia-smi", "-L"], timeout=10)
    if rc == 0 and out.strip():
        return CheckItem(0, "GPU / CUDA availability", "green", out.strip().splitlines()[0][:160])

    if sys.platform == "darwin":
        rc2, brand, _ = _run(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=5)
        b = brand.strip() if rc2 == 0 else "CPU"
        return CheckItem(
            0,
            "GPU / acceleration",
            "yellow",
            f"{b} — host Ollama uses Metal (Apple Silicon) or CPU; no NVIDIA in this OS path.",
            ("Install Ollama from https://ollama.com/download",),
        )

    rc3, _, _ = _run(["rocm-smi"], timeout=8)
    if rc3 == 0:
        return CheckItem(0, "GPU (ROCm)", "green", "rocm-smi OK — AMD GPU stack detected.")

    return CheckItem(
        0,
        "GPU / acceleration",
        "yellow",
        "No NVIDIA (`nvidia-smi`) or ROCm detected — expect CPU inference unless you use a remote provider.",
        (
            "NVIDIA Linux: install proprietary drivers; verify `nvidia-smi`.",
            "Or configure cloud keys in deploy/local-ai/.env.local.",
        ),
    )


def _check_docker_daemon() -> CheckItem:
    rc, out, err = _run(["docker", "info"], timeout=30)
    if rc == 0:
        snippet = [ln.strip() for ln in out.splitlines() if "Server Version" in ln or "Operating System" in ln][:2]
        return CheckItem(0, "Docker daemon", "green", " ".join(snippet) or "Docker daemon OK.")
    msg = (err or out or "").strip()[:400]
    return CheckItem(
        0,
        "Docker daemon",
        "red",
        f"docker info failed ({rc}): {msg}",
        (
            "macOS/Windows: start Docker Desktop.",
            "Linux: sudo systemctl start docker && sudo usermod -aG docker $USER",
            "Verify: docker run --rm hello-world",
        ),
    )


def _check_docker_memory() -> CheckItem:
    rc, out, _ = _run(["docker", "info", "-f", "{{.MemTotal}}"], timeout=20)
    raw = (out or "").strip()
    if rc == 0 and raw.isdigit():
        total = int(raw)
        hb = _bytes_human(total)
        if total >= 8 * (1024**3):
            return CheckItem(0, "Docker memory limit", "green", f"{hb} visible to Docker.")
        if total >= 4 * (1024**3):
            return CheckItem(
                0,
                "Docker memory limit",
                "yellow",
                f"{hb} — increase toward ≥ 8 GiB for full stack comfort.",
                ("Docker Desktop → Settings → Resources → Memory",),
            )
        return CheckItem(
            0,
            "Docker memory limit",
            "red",
            f"{hb} — too low for comfortable multi-service compose.",
            (
                "Docker Desktop → Settings → Resources → set Memory to at least 8 GiB (16 GiB recommended).",
                "Linux: raise cgroup/memory limits per distro docs.",
            ),
        )

    return CheckItem(
        0,
        "Docker memory limit",
        "yellow",
        "Could not parse `docker info -f '{{.MemTotal}}'`. Open Docker Desktop → Resources and ensure ≥ 8 GiB RAM.",
        ("Docker Desktop → Settings → Resources → Memory / Swap",),
    )


def _check_disk(repo_root: Path) -> CheckItem:
    candidates = [repo_root, repo_root / ".local", Path.home() / ".ollama"]
    best_free_gib = float("inf")
    best_path = repo_root
    for p in candidates:
        try:
            base = p if p.exists() else p.parent
            if not base.exists():
                continue
            u = shutil.disk_usage(base.resolve())
            gib = u.free / (1024**3)
            if gib < best_free_gib:
                best_free_gib = gib
                best_path = base
        except OSError:
            continue
    if best_free_gib is float("inf"):
        best_free_gib = 0.0
    detail = f"Lowest free space near `{best_path}`: {best_free_gib:.1f} GiB"

    if best_free_gib >= 25:
        return CheckItem(0, "Disk space for models & data", "green", detail)
    if best_free_gib >= 12:
        return CheckItem(
            0,
            "Disk space for models & data",
            "yellow",
            detail + " — may be tight for multiple large models.",
            ("docker system prune -a", "ollama list && ollama rm <unused>"),
        )
    return CheckItem(
        0,
        "Disk space for models & data",
        "red",
        detail + " — pull/extract may fail.",
        (
            "Free space on the volume containing ~/.ollama and the repo .local directory.",
            "docker system prune -a",
            "rm -rf ~/.ollama/models/blobs/*   # only if you accept deleting cached blobs",
        ),
    )


def _check_trivy() -> CheckItem:
    if shutil.which("trivy"):
        rc, out, _ = _run(["trivy", "--version"], timeout=10)
        line = out.strip().splitlines()[0] if rc == 0 else "trivy on PATH"
        return CheckItem(0, "Trivy on PATH", "green", line)
    return CheckItem(
        0,
        "Trivy on PATH",
        "red",
        "`trivy` not found.",
        (
            "macOS: brew install trivy",
            "https://aquasecurity.github.io/trivy/latest/getting-started/installation/",
        ),
    )


def _check_codeql() -> CheckItem:
    if shutil.which("codeql"):
        rc, out, _ = _run(["codeql", "version"], timeout=10)
        line = out.strip().splitlines()[0] if rc == 0 else "codeql on PATH"
        return CheckItem(0, "CodeQL on PATH", "green", line)

    gh = shutil.which("gh")
    if gh:
        rc, out, _ = _run(["gh", "extension", "list"], timeout=15)
        if rc == 0 and "codeql" in out.lower():
            return CheckItem(
                0,
                "CodeQL on PATH",
                "yellow",
                "`gh` present with a CodeQL-related extension, but `codeql` binary not on PATH.",
                (
                    "Add CodeQL CLI to PATH: https://github.com/github/codeql-cli-binaries/releases",
                    "Or: gh extension install github/gh-codeql  (then follow gh instructions)",
                ),
            )

    return CheckItem(
        0,
        "CodeQL on PATH",
        "red",
        "`codeql` not found — CodeQL evidence blocks in grounded review may be unavailable.",
        (
            "https://docs.github.com/en/code-security/codeql-cli",
            "curl -fsSL https://github.com/github/codeql-cli-binaries/releases/latest/download/codeql-osx64.zip -o /tmp/cq.zip && unzip /tmp/cq.zip -d \"$HOME/opt\" && export PATH=\"$HOME/opt/codeql:$PATH\"",
        ),
    )


def _check_network() -> CheckItem:
    """HTTPS reachability + DNS for core hosts."""
    https_fail: list[str] = []

    def _probe_registry(url: str) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "octo-spork-doctor/1"})
        try:
            with urllib.request.urlopen(req, timeout=15) as _:
                return
        except urllib.error.HTTPError as exc:
            if getattr(exc, "code", None) in {401, 403}:
                return
            raise

    try:
        gh = urllib.request.Request("https://github.com", headers={"User-Agent": "octo-spork-doctor/1"})
        with urllib.request.urlopen(gh, timeout=15) as _:
            pass
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        https_fail.append(f"https://github.com: {exc}")

    try:
        _probe_registry("https://registry-1.docker.io/v2/")
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        https_fail.append(f"Docker Hub registry: {exc}")

    dns_hosts = ("github.com", "registry-1.docker.io", "ollama.com")
    dns_fail: list[str] = []
    for h in dns_hosts:
        try:
            socket.getaddrinfo(h, 443)
        except OSError as exc:
            dns_fail.append(f"{h}: {exc}")

    if not https_fail and not dns_fail:
        return CheckItem(
            0,
            "Network connectivity (HTTPS + DNS)",
            "green",
            "HTTPS + DNS OK for github.com, Docker Hub registry, ollama.com.",
        )

    parts = https_fail + dns_fail
    return CheckItem(
        0,
        "Network connectivity (HTTPS + DNS)",
        "red",
        "; ".join(parts)[:600],
        (
            "curl -I https://github.com",
            "Check VPN/firewall; export HTTPS_PROXY / NO_PROXY if needed.",
            "Try DNS 8.8.8.8 in system network settings.",
        ),
    )


def _check_stack_readiness(repo_root: Path, env_file: Path | None) -> CheckItem:
    default_env = repo_root / "deploy" / "local-ai" / ".env.local"
    example_env = repo_root / "deploy" / "local-ai" / ".env.example"
    target = env_file if env_file else default_env

    ollama_url = "http://127.0.0.1:11434"
    if target.is_file():
        try:
            for line in target.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("OLLAMA_LOCAL_URL="):
                    ollama_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            pass

    env_ok = target.is_file()
    ollama_ok = False
    ollama_detail = ""
    try:
        req = urllib.request.Request(
            ollama_url.rstrip("/") + "/api/tags",
            headers={"User-Agent": "octo-spork-doctor/1"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                n = len((data or {}).get("models") or []) if isinstance(data, dict) else 0
                ollama_ok = True
                ollama_detail = f"Ollama `{ollama_url}` lists {n} model(s)."
    except (urllib.error.URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        ollama_detail = f"Ollama probe failed ({ollama_url}): {exc}"

    if env_ok and ollama_ok:
        return CheckItem(
            0,
            "Stack env + Ollama",
            "green",
            f"`{target}` present. {ollama_detail}",
        )
    if not env_ok:
        return CheckItem(
            0,
            "Stack env + Ollama",
            "yellow",
            f"Missing `{target}` — create from example before `up`.",
            (
                f"cp {example_env} {default_env}",
                "python3 -m local_ai_stack bootstrap --env-file deploy/local-ai/.env.local",
            ),
        )
    return CheckItem(
        0,
        "Stack env + Ollama",
        "red",
        f"`{target}` exists but {ollama_detail}",
        (
            "Start Ollama: ollama serve   # or open the Ollama app (macOS)",
            f"Align OLLAMA_LOCAL_URL in `{target}` with the listening address.",
        ),
    )


def _claude_stack_enabled(repo_root: Path) -> bool:
    return (
        (repo_root / "deploy" / "claude-code" / "Dockerfile").is_file()
        and (repo_root / "deploy" / "local-ai" / "docker-compose.claude-agent.yml").is_file()
    )


def _docker_inspect_running(container: str) -> bool:
    rc, out, _ = _run(["docker", "inspect", "-f", "{{.State.Running}}", container], timeout=12)
    return rc == 0 and (out or "").strip().lower() == "true"


def _compose_claude_image_labels() -> list[str]:
    """Return matching ``docker images`` lines for the bundled Claude Agent service image."""
    prefix = f"{CLAUDE_COMPOSE_PROJECT_NAME}-claude-agent"
    rc, out, _ = _run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=30)
    if rc != 0:
        return []
    found: list[str] = []
    for ln in out.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith(prefix) or (CLAUDE_COMPOSE_PROJECT_NAME in s and "claude-agent" in s):
            found.append(s)
    return found


def _check_bun_on_path() -> CheckItem:
    bun = shutil.which("bun")
    if bun:
        rc, out, _ = _run(["bun", "--version"], timeout=8)
        ver = out.strip().splitlines()[0] if rc == 0 else bun
        return CheckItem(0, "Claude Code: Bun on PATH", "green", ver)
    return CheckItem(
        0,
        "Claude Code: Bun on PATH",
        "red",
        "`bun` not found — required to develop/run the Bun-based Claude Code image locally.",
        ("curl -fsSL https://bun.sh/install | bash", "https://bun.sh/docs/installation"),
    )


def _check_claude_agent_image(repo_root: Path) -> CheckItem:
    if not _claude_stack_enabled(repo_root):
        return CheckItem(
            0,
            "Claude Code: Agent Docker image built",
            "yellow",
            "Claude Agent compose fragment or Dockerfile missing — image check skipped.",
            (),
        )
    labels = _compose_claude_image_labels()
    if labels:
        return CheckItem(
            0,
            "Claude Code: Agent Docker image built",
            "green",
            f"Found agent image: {labels[0]}",
        )
    expect = f"{CLAUDE_COMPOSE_PROJECT_NAME}-claude-agent"
    return CheckItem(
        0,
        "Claude Code: Agent Docker image built",
        "red",
        f"No Docker image matching `{expect}` — build the claude-agent service before running the stack.",
        (
            "python3 -m local_ai_stack up --env-file deploy/local-ai/.env.local",
            f"docker compose --project-name {CLAUDE_COMPOSE_PROJECT_NAME} "
            "-f <agenticseek>/docker-compose.yml -f deploy/local-ai/docker-compose.addons.yml "
            f"-f deploy/local-ai/docker-compose.claude-agent.yml build claude-agent",
        ),
    )


def _check_ollama_from_claude_agent(repo_root: Path) -> CheckItem:
    """Agent reaches host Ollama via ``ollama:11434`` (``extra_hosts`` → host-gateway); host listens per ``OLLAMA_HOST``."""
    if not _claude_stack_enabled(repo_root):
        return CheckItem(
            0,
            "Claude Code: Ollama reachable from agent container",
            "yellow",
            "Claude Agent stack not present — reachability check skipped.",
            (),
        )
    if not _docker_inspect_running(CLAUDE_AGENT_CONTAINER):
        return CheckItem(
            0,
            "Claude Code: Ollama reachable from agent container",
            "yellow",
            f"`{CLAUDE_AGENT_CONTAINER}` is not running — start the stack to verify http://ollama:11434 from inside the agent.",
            ("python3 -m local_ai_stack up --env-file deploy/local-ai/.env.local",),
        )
    probe = (
        'fetch("http://ollama:11434/api/tags").then(r => { if (!r.ok) process.exit(1); })'
        ".catch(() => process.exit(1));"
    )
    rc, _, err = _run(
        ["docker", "exec", "-T", CLAUDE_AGENT_CONTAINER, "bun", "-e", probe],
        timeout=25,
    )
    if rc == 0:
        return CheckItem(
            0,
            "Claude Code: Ollama reachable from agent container",
            "green",
            "http://ollama:11434/api/tags OK from inside the agent (host Ollama via extra_hosts / OLLAMA_HOST listener).",
        )
    msg = (err or "").strip()[:240]
    return CheckItem(
        0,
        "Claude Code: Ollama reachable from agent container",
        "red",
        f"Probe from agent container failed (exit {rc}): {msg or 'bun fetch failed'} — "
        "ensure host Ollama is listening on 11434 and ``extra_hosts: ollama:host-gateway`` is applied.",
        (
            "On the host: `ollama serve` or start the Ollama app; match deploy/local-ai `.env.local` OLLAMA_LOCAL_URL.",
            "Confirm `docker compose` for claude-agent includes extra_hosts mapping `ollama` to host-gateway.",
        ),
    )


def _check_claude_repo_workspace(repo_root: Path) -> CheckItem:
    """Host repo must be writable for bind mounts; agent container should expose a writable workspace path."""
    ws = (os.environ.get(CLAUDE_WORKSPACE_ENV) or "").strip() or _DEFAULT_CLAUDE_WORKSPACE_IN_CONTAINER

    try:
        host_ok = os.access(repo_root, os.W_OK)
    except OSError:
        host_ok = False

    if not host_ok:
        return CheckItem(
            0,
            "Claude Code: Repository workspace writable",
            "red",
            f"Host path `{repo_root}` is not writable — bind-mounted repo volumes need write access.",
            ("chmod -R u+w <repo> or fix filesystem permissions.",),
        )

    if not _claude_stack_enabled(repo_root):
        return CheckItem(
            0,
            "Claude Code: Repository workspace writable",
            "green",
            f"Host repo `{repo_root}` is writable. (Claude Agent compose not present — in-container mount not checked.)",
        )

    if not _docker_inspect_running(CLAUDE_AGENT_CONTAINER):
        return CheckItem(
            0,
            "Claude Code: Repository workspace writable",
            "yellow",
            f"Host repo `{repo_root}` is writable. Agent container not running — could not verify `{ws}` inside the agent.",
            ("python3 -m local_ai_stack up --env-file deploy/local-ai/.env.local",),
        )

    rc_dir, _, _ = _run(
        ["docker", "exec", "-T", CLAUDE_AGENT_CONTAINER, "sh", "-c", f'test -d "{ws}"'],
        timeout=15,
    )
    if rc_dir != 0:
        return CheckItem(
            0,
            "Claude Code: Repository workspace writable",
            "yellow",
            f"Host repo writable; `{ws}` is not mounted in `{CLAUDE_AGENT_CONTAINER}` — "
            "add a read-write bind mount for the target repository.",
            (
                f"Example: `-v {repo_root}:{ws}` on the claude-agent service (see {CLAUDE_WORKSPACE_ENV}).",
            ),
        )

    rc_w, _, _ = _run(
        ["docker", "exec", "-T", CLAUDE_AGENT_CONTAINER, "sh", "-c", f'test -w "{ws}"'],
        timeout=15,
    )
    if rc_w == 0:
        return CheckItem(
            0,
            "Claude Code: Repository workspace writable",
            "green",
            f"Host repo writable; `{ws}` exists and is writable in `{CLAUDE_AGENT_CONTAINER}`.",
        )

    return CheckItem(
        0,
        "Claude Code: Repository workspace writable",
        "red",
        f"Host repo writable; `{ws}` exists in `{CLAUDE_AGENT_CONTAINER}` but is not writable.",
        (f"Fix container volume mode (use `:rw`) or permissions on `{ws}`.",),
    )


def run_doctor(*, env_file: Path | None, repo_root: Path | None = None) -> list[CheckItem]:
    """Run the core checklist plus Claude Code environment checks (numbered 1–14)."""
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    raw = [
        _check_python(),
        _check_cpu(),
        _check_gpu(),
        _check_docker_daemon(),
        _check_docker_memory(),
        _check_disk(root),
        _check_trivy(),
        _check_codeql(),
        _check_network(),
        _check_stack_readiness(root, env_file),
        _check_bun_on_path(),
        _check_claude_agent_image(root),
        _check_ollama_from_claude_agent(root),
        _check_claude_repo_workspace(root),
    ]
    out: list[CheckItem] = []
    for i, c in enumerate(raw, start=1):
        out.append(CheckItem(i, c.title, c.status, c.detail, c.fix_commands))
    return out


def _claude_code_items(items: list[CheckItem]) -> list[CheckItem]:
    return [it for it in items if it.title.startswith("Claude Code:")]


def claude_code_environment_ready(items: list[CheckItem]) -> bool:
    """True only when every Claude Code line item is green (for remediation engines)."""
    cl = _claude_code_items(items)
    return bool(cl) and all(it.status == "green" for it in cl)


def format_doctor_report(items: list[CheckItem]) -> str:
    lines: list[str] = [
        "",
        "Octo-spork doctor — environment checklist (core + Claude Code)",
        "=" * 44,
        "",
    ]
    worst: Status = "green"
    for it in items:
        if it.status == "red":
            worst = "red"
        elif it.status == "yellow" and worst == "green":
            worst = "yellow"

        sym = {"green": "[OK]  ", "yellow": "[WARN] ", "red": "[FAIL] "}[it.status]
        lines.append(f"{sym}{it.number}. {it.title}")
        lines.append(f"     {it.detail}")
        if it.status == "red" and it.fix_commands:
            lines.append("     Fix:")
            for cmd in it.fix_commands:
                lines.append(f"       $ {cmd}")
        elif it.status == "yellow" and it.fix_commands:
            lines.append("     Suggested:")
            for cmd in it.fix_commands:
                lines.append(f"       $ {cmd}")
        lines.append("")

    lines.append(f"Overall: {worst.upper()} (address any [FAIL], review [WARN])")
    lines.append("")
    cl_items = _claude_code_items(items)
    if cl_items:
        ready = claude_code_environment_ready(items)
        lines.append(f"Claude Code environment: {'READY' if ready else 'NOT READY'} (remediation engine)")
        lines.append("")
    return "\n".join(lines)
