"""Outbound privacy guard for local-only Docker stacks (Linux iptables).

When ``LOCAL_AI_PRIVACY_MODE=local-only``, installs ``iptables`` rules so containers may reach
private/link-local ranges and the host, while **SearXNG** keeps general internet egress for search.
All other listed containers get **DROP** + rate-limited **LOG** for packets destined to the public
internet — typical cloud LLM API calls use HTTPS to public IPs and are blocked here.

A detached ``python -m local_ai_stack privacy-monitor`` process polls DROP counters and triggers
``down`` if violations occur.

Requires Linux, CAP_NET_ADMIN / root for iptables. Non-Linux hosts log a warning and skip install.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

LOCAL_AI_PRIVACY_MODE = os.getenv("LOCAL_AI_PRIVACY_MODE", "").strip().lower()
LOCAL_AI_PRIVACY_POLL_SECONDS = float(os.getenv("LOCAL_AI_PRIVACY_POLL_SECONDS", "3"))
LOCAL_AI_PRIVACY_VIOLATION_PKTS = int(os.getenv("LOCAL_AI_PRIVACY_VIOLATION_PKTS", "1"))

CHAIN_GUARD = "OCTO_SPORK_PRIVACY"
CHAIN_VIOL = "OCTO_SPORK_PRIV_VIOL"

# RFC1918 + loopback + link-local (Docker DNS sometimes uses 127.x from container POV via proxy — kept broad)
PRIVATE_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
)

MONITORED_CONTAINERS: tuple[str, ...] = (
    "local-ai-redis",
    "local-ai-agentic-api",
    "local-ai-agentic-ui",
    "local-ai-open-webui",
    "local-ai-n8n",
)

EXEMPT_CONTAINERS: tuple[str, ...] = ("local-ai-searxng",)

PID_PATH = ROOT / ".local" / "privacy_monitor.pid"
LOG_PATH = ROOT / "logs" / "privacy_monitor.log"


def is_local_only_enabled(env_values: dict[str, str] | None = None) -> bool:
    """True when privacy firewall should arm."""
    if env_values:
        v = str(env_values.get("LOCAL_AI_PRIVACY_MODE", "") or "").strip().lower()
        if v in {"local-only", "local_only", "localonly", "strict"}:
            return True
    v = LOCAL_AI_PRIVACY_MODE or os.getenv("LOCAL_AI_PRIVACY_MODE", "")
    return str(v).strip().lower() in {"local-only", "local_only", "localonly", "strict"}


def _logger_for_file() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger("octo_privacy_monitor")
    lg.setLevel(logging.INFO)
    if not lg.handlers:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(fh)
    return lg


def _iptables(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["iptables", "-t", "filter", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _iptables_available() -> bool:
    try:
        r = subprocess.run(["iptables", "-V"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _linux_host() -> bool:
    return sys.platform.startswith("linux")


def _can_modify_iptables() -> bool:
    try:
        r = _iptables(["-S", "DOCKER-USER"], timeout=10)
        return r.returncode == 0
    except OSError:
        return False


def container_ipv4_primary(container_name: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "-f", "{{range.NetworkSettings.Networks}}{{.IPAddress}} {{end}}", container_name],
            text=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    ips = [x.strip() for x in out.split() if x.strip()]
    for ip in ips:
        try:
            ipaddress.IPv4Address(ip)
            return ip
        except ValueError:
            continue
    return None


def discover_roles() -> tuple[str | None, dict[str, str]]:
    """Return (searxng_ip, monitored_name -> ip)."""
    searx = None
    sx = container_ipv4_primary(EXEMPT_CONTAINERS[0])
    if sx:
        searx = sx
    monitored: dict[str, str] = {}
    for name in MONITORED_CONTAINERS:
        ip = container_ipv4_primary(name)
        if ip:
            monitored[name] = ip
    return searx, monitored


def _chain_exists(name: str) -> bool:
    r = _iptables(["-L", name, "-n"])
    return r.returncode == 0


def teardown_privacy_iptables(logger: logging.Logger | None = None) -> None:
    """Remove OCTO_SPORK chains and jump from DOCKER-USER."""
    log = logger or logging.getLogger(__name__)

    # Remove jump rule(s) from DOCKER-USER pointing to our guard chain
    for _ in range(40):
        chk = _iptables(["-C", "DOCKER-USER", "-j", CHAIN_GUARD])
        if chk.returncode != 0:
            break
        _iptables(["-D", "DOCKER-USER", "-j", CHAIN_GUARD])

    if _chain_exists(CHAIN_GUARD):
        _iptables(["-F", CHAIN_GUARD])
        _iptables(["-X", CHAIN_GUARD])
    if _chain_exists(CHAIN_VIOL):
        _iptables(["-F", CHAIN_VIOL])
        _iptables(["-X", CHAIN_VIOL])

    log.info("Privacy iptables chains removed (best-effort).")


def install_privacy_iptables(searx_ip: str | None, monitored: dict[str, str], logger: logging.Logger) -> bool:
    """Insert egress guard rules. Returns False if skipped/failed."""
    if not _linux_host():
        logger.warning("Privacy firewall skipped: not a Linux host.")
        return False
    if not _iptables_available():
        logger.warning("Privacy firewall skipped: iptables not found.")
        return False
    if os.geteuid() != 0:
        logger.warning(
            "Privacy firewall skipped: need root/CAP_NET_ADMIN (euid=%s). "
            "Run stack under sudo or grant iptables capability.",
            os.geteuid(),
        )
        return False
    if not _can_modify_iptables():
        logger.warning("Privacy firewall skipped: cannot read iptables (permission denied).")
        return False

    teardown_privacy_iptables(logger)

    # Violation chain: LOG + DROP (counter on DROP line for polling)
    _iptables(["-N", CHAIN_VIOL])
    _iptables(
        [
            "-A",
            CHAIN_VIOL,
            "-m",
            "limit",
            "--limit",
            "20/min",
            "--limit-burst",
            "5",
            "-j",
            "LOG",
            "--log-prefix",
            "octo-privacy-violation ",
            "--log-level",
            "4",
        ]
    )
    _iptables(["-A", CHAIN_VIOL, "-j", "DROP"])

    _iptables(["-N", CHAIN_GUARD])

    # SearXNG: allow full egress (search engines need internet)
    if searx_ip:
        _iptables(["-A", CHAIN_GUARD, "-s", searx_ip, "-j", "RETURN"])
        logger.info("Exempt SearXNG egress for %s", searx_ip)

    for _name, ip in sorted(monitored.items()):
        for cidr in PRIVATE_CIDRS:
            _iptables(["-A", CHAIN_GUARD, "-s", ip, "-d", cidr, "-j", "RETURN"])
        _iptables(["-A", CHAIN_GUARD, "-s", ip, "-j", CHAIN_VIOL])
        logger.info("Restricted egress for %s (%s) — public destinations dropped/logged.", _name, ip)

    ins = _iptables(["-I", "DOCKER-USER", "1", "-j", CHAIN_GUARD])
    if ins.returncode != 0:
        logger.error("Could not insert DOCKER-USER jump: %s", ins.stderr.strip())
        teardown_privacy_iptables(logger)
        return False

    logger.info("Privacy firewall installed on chain %s -> %s.", CHAIN_GUARD, CHAIN_VIOL)
    return True


def read_violation_drop_packets() -> int | None:
    """Return packet count on DROP rule in CHAIN_VIOL, or None if unavailable."""
    r = _iptables(["-L", CHAIN_VIOL, "-v", "-x", "-n"])
    if r.returncode != 0:
        return None
    # Lines like: pkts bytes target ...
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "DROP":
            try:
                return int(parts[0])
            except ValueError:
                continue
    return None


def write_pid_file() -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def clear_pid_file() -> None:
    try:
        PID_PATH.unlink(missing_ok=True)
    except TypeError:
        if PID_PATH.exists():
            PID_PATH.unlink()


def read_pid_file() -> int | None:
    try:
        raw = PID_PATH.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError):
        return None


def terminate_existing_monitor(logger: logging.Logger) -> None:
    pid = read_pid_file()
    if pid is None or pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
    except ProcessLookupError:
        pass
    except PermissionError:
        logger.warning("Could not SIGTERM old privacy monitor pid=%s", pid)


def spawn_detached_monitor(env_file: Path, logger: logging.Logger) -> None:
    """Start background monitor process (survives after `up` exits)."""
    terminate_existing_monitor(logger)
    log_fd = os.open(str(LOG_PATH), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    cmd = [
        sys.executable,
        "-m",
        "local_ai_stack",
        "privacy-monitor",
        "--env-file",
        str(env_file.resolve()),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        logger.info("Spawned privacy-monitor subprocess pid=%s", proc.pid)
    except OSError as exc:
        logger.error("Could not spawn privacy-monitor: %s", exc)
    finally:
        try:
            os.close(log_fd)
        except OSError:
            pass


def run_monitor_loop(env_file: Path) -> int:
    """Poll iptables DROP counters; on violation compose down."""
    lg = _logger_for_file()
    baseline: int | None = None
    lg.info("privacy-monitor started pid=%s env_file=%s", os.getpid(), env_file)
    write_pid_file()

    def shutdown(signum: int, _frame: Any) -> None:
        lg.info("privacy-monitor stopping on signal %s", signum)
        clear_pid_file()
        teardown_privacy_iptables(lg)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Warm-up: wait for iptables counters to exist
    time.sleep(2)
    baseline = read_violation_drop_packets()
    if baseline is None:
        lg.warning("Cannot read %s counters; monitor inactive.", CHAIN_VIOL)
        while True:
            time.sleep(3600)

    lg.info("Violation DROP packet baseline=%s", baseline)

    while True:
        time.sleep(max(1.0, LOCAL_AI_PRIVACY_POLL_SECONDS))
        cur = read_violation_drop_packets()
        if cur is None:
            continue
        if cur >= baseline + LOCAL_AI_PRIVACY_VIOLATION_PKTS:
            lg.error(
                "Privacy violation: non-exempt container attempted public internet egress "
                "(DROP pkts %s -> %s). Shutting stack down.",
                baseline,
                cur,
            )
            try:
                subprocess.run(
                    [sys.executable, "-m", "local_ai_stack", "down", "--env-file", str(env_file)],
                    cwd=str(ROOT),
                    timeout=900,
                    check=False,
                )
            except OSError as exc:
                lg.exception("down subprocess failed: %s", exc)
            teardown_privacy_iptables(lg)
            clear_pid_file()
            return 1

    return 0


def maybe_arm_after_up(env_file: Path, orchestrator_logger: logging.Logger) -> None:
    """Called at end of StackOrchestrator.execute when stack is up."""
    try:
        env_values = _parse_env_simple(env_file)
    except OSError:
        env_values = {}
    if not is_local_only_enabled(env_values):
        orchestrator_logger.info("LOCAL_AI_PRIVACY_MODE not local-only; privacy firewall not armed.")
        return

    searx, monitored = discover_roles()
    if not monitored and not searx:
        orchestrator_logger.warning(
            "Privacy mode: no container IPs discovered yet; skipping iptables (retry after containers settle)."
        )
        return

    lg = _logger_for_file()
    ok = install_privacy_iptables(searx, monitored, lg)
    if ok:
        spawn_detached_monitor(env_file, orchestrator_logger)


def _parse_env_simple(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def teardown_from_down(logger: logging.Logger | None = None) -> None:
    lg = logger or _logger_for_file()
    terminate_existing_monitor(lg)
    teardown_privacy_iptables(lg)
    clear_pid_file()
