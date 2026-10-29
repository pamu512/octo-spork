"""Stack teardown helpers for ``local_ai_stack down`` and ``force-clean``.

Pure move from ``__main__`` (after #12 StackOrchestrator): function bodies are
byte-identical to their previous definitions in ``__main__``, which re-exports
them for backward compatibility.

This module does not import ``local_ai_stack.__main__`` (even lazily). Helpers
that still live in the CLI module are bound onto this module by ``__main__``
after they are defined.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from local_ai_stack.env_validation import _parse_env_file

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PROJECT_NAME = "octo-spork-local-ai"
OCTO_SPORK_PROJECT_LABEL_KEY = "com.octospork.project"
GROUNDED_REVIEW_TEMP_CLONES_DIR = ROOT / ".local" / "temp_clones"


def _print(message: str) -> None:
    print(message, flush=True)


def _run_subprocess_allow_fail(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> int:
    try:
        _print(f"+ {' '.join(args)}")
        completed = subprocess.run(
            args,
            cwd=str(cwd or ROOT),
            env=env,
            check=False,
            text=True,
            capture_output=True,
            timeout=int(timeout),
        )
    except FileNotFoundError:
        _print("Warning: executable not found while running: " + " ".join(args))
        return 127
    except subprocess.TimeoutExpired:
        _print(f"Warning: command timed out after {timeout}s: {' '.join(args)}")
        return 124
    except OSError as exc:
        _print(f"Warning: OS error running {' '.join(args)}: {exc}")
        return 1
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        _print(f"Warning: exit {completed.returncode} from {' '.join(args)} — {err}")
    return completed.returncode


def _docker_volume_prune_project(project_name: str) -> None:
    label_filter = f"label=com.docker.compose.project={project_name}"
    args = ["docker", "volume", "prune", "-f", "--filter", label_filter]
    try:
        rc = _run_subprocess_allow_fail(args, timeout=600)
        if rc == 0:
            _print(f"Pruned unused Docker volumes labeled com.docker.compose.project={project_name!r}.")
    except (OSError, ValueError, TypeError) as exc:
        _print(f"Warning: volume prune failed unexpectedly: {exc}")


def _docker_network_ids_with_project_label(project_name: str) -> list[str]:
    args = [
        "docker",
        "network",
        "ls",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={project_name}",
    ]
    try:
        completed = subprocess.run(
            args,
            cwd=str(ROOT),
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _print(f"Warning: could not list labeled Docker networks: {exc}")
        return []
    if completed.returncode != 0:
        err = (completed.stderr or "").strip()
        _print(f"Warning: docker network ls failed: {err}")
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _docker_network_remove_by_id(network_id: str, human_name: str | None = None) -> None:
    if not network_id or not str(network_id).strip():
        raise ValueError("network_id is empty")
    nid = str(network_id).strip()
    label = human_name or nid
    try:
        rm = subprocess.run(
            ["docker", "network", "rm", nid],
            cwd=str(ROOT),
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _print(f"Warning: could not remove Docker network {label!r}: {exc}")
        return
    if rm.returncode == 0:
        _print(f"Removed Docker network {label!r} ({nid}).")
        return
    combined = ((rm.stderr or "") + (rm.stdout or "")).lower()
    if "no such network" in combined or "not found" in combined:
        return
    err = (rm.stderr or rm.stdout or "").strip()
    _print(f"Warning: docker network rm failed for {label!r}: {err}")


def _docker_network_remove_labeled_project(project_name: str) -> None:
    ids = _docker_network_ids_with_project_label(project_name)
    if not ids:
        _print("No Docker networks found with compose project label (already clean or none created).")
        return
    _print(f"Removing {len(ids)} Docker network(s) labeled for project {project_name!r}...")
    for nid in ids:
        try:
            _docker_network_remove_by_id(nid, human_name=f"<labeled:{nid[:12]}>")
        except ValueError as exc:
            _print(f"Warning: invalid network id skipped: {exc}")


def _docker_network_map_name_by_id() -> dict[str, str]:
    try:
        completed = subprocess.run(
            ["docker", "network", "ls", "--format", "{{.ID}}\t{{.Name}}"],
            cwd=str(ROOT),
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _print(f"Warning: could not list Docker networks for stray cleanup: {exc}")
        return {}
    if completed.returncode != 0:
        return {}
    result: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        nid, name = parts[0].strip(), parts[1].strip()
        if nid and name:
            result[nid] = name
    return result


def _docker_network_remove_strays_by_name_prefix(project_name: str) -> None:
    prefix = f"{project_name}_"
    network_map = _docker_network_map_name_by_id()
    if not network_map:
        return
    bridge_builtin = {"bridge", "host", "none"}
    _print(f"Checking for stray Docker networks named {prefix!r}* ...")
    for nid, net_name in network_map.items():
        try:
            if net_name in bridge_builtin:
                continue
            if not net_name.startswith(prefix):
                continue
            _docker_network_remove_by_id(nid, human_name=net_name)
        except (OSError, ValueError) as exc:
            _print(f"Warning: stray network cleanup step failed for {net_name!r}: {exc}")


def _cleanup_grounded_review_temp_clones(base_dir: Path | None = None) -> None:
    root_dir = Path(base_dir).resolve() if base_dir is not None else GROUNDED_REVIEW_TEMP_CLONES_DIR.resolve()
    try:
        if not root_dir.exists():
            _print(f"No grounded-review temp dir at {root_dir} (nothing to clean).")
            return
        if not root_dir.is_dir():
            _print(f"Warning: expected directory at {root_dir}; skipping temp clone cleanup.")
            return
    except OSError as exc:
        _print(f"Warning: could not stat temp clones dir {root_dir}: {exc}")
        return
    removed_items = 0
    try:
        for child in sorted(root_dir.iterdir(), key=lambda p: str(p)):
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed_items += 1
            except OSError as exc:
                _print(f"Warning: could not remove {child}: {exc}")
    except OSError as exc:
        _print(f"Warning: could not iterate temp clones dir {root_dir}: {exc}")
        return
    if removed_items:
        _print(f"Removed {removed_items} path(s) under {root_dir}.")
    else:
        _print(f"Grounded-review temp dir is empty: {root_dir}")


def _compose_down_stack(
    env_file: Path,
    agenticseek_path: Path,
    process_env: dict[str, str],
) -> None:
    compose_cmd = _compose_base_args(env_file, agenticseek_path) + [
        "--profile",
        "full",
        "--profile",
        "addons",
        "down",
        "--remove-orphans",
    ]
    try:
        _print(f"+ {' '.join(compose_cmd)}")
        completed = subprocess.run(
            compose_cmd,
            cwd=str(ROOT),
            env=process_env,
            check=False,
            text=True,
            capture_output=True,
            timeout=600,
        )
    except FileNotFoundError:
        _print("Warning: docker compose not found; skipping compose down.")
        return
    except subprocess.TimeoutExpired:
        _print("Warning: compose down timed out; continuing with volume/network cleanup.")
        return
    except OSError as exc:
        _print(f"Warning: compose down failed ({exc}); continuing with cleanup.")
        return
    if completed.returncode == 0:
        _print(f"Compose stack {COMPOSE_PROJECT_NAME!r} stopped (containers for declared compose files).")
        return
    err_out = (completed.stderr or completed.stdout or "").strip()
    _print(
        f"Warning: compose down exited with {completed.returncode}; continuing with volume/network cleanup. "
        f"{err_out}"
    )


def command_down(env_file: Path) -> None:
    _print(f"Shutting down stack project {COMPOSE_PROJECT_NAME!r} ...")
    try:
        from local_ai_stack import privacy_monitor as _privacy_monitor

        _privacy_monitor.teardown_from_down()
    except Exception as exc:
        _print(f"Warning: privacy monitor teardown: {exc}")
    env_values: dict[str, str] = {}
    try:
        env_values = _parse_env_file(env_file)
    except (OSError, UnicodeDecodeError) as exc:
        _print(f"Warning: could not read env file {env_file}: {exc}")

    process_env = _merge_process_env(env_values)
    agenticseek_path: Path | None = None
    try:
        agenticseek_path = _resolve_agenticseek_path(env_values)
    except (RuntimeError, OSError, ValueError) as exc:
        _print(
            f"Warning: could not resolve AGENTICSEEK_DIR ({exc}). "
            "Skipping compose down; still pruning Docker resources for this project name."
        )

    if agenticseek_path is not None:
        dc_yml = agenticseek_path / "docker-compose.yml"
        if not dc_yml.is_file():
            _print(
                f"Warning: {dc_yml} not found; skipping compose down. "
                "Run bootstrap or clone AgenticSeek so compose files exist."
            )
        else:
            try:
                ef = Path(env_file).resolve()
                _compose_down_stack(ef, agenticseek_path, process_env)
            except (RuntimeError, OSError, ValueError, TypeError) as exc:
                _print(f"Warning: unexpected error during compose down: {exc}")

    try:
        _docker_volume_prune_project(COMPOSE_PROJECT_NAME)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: volume prune failed unexpectedly: {exc}")

    try:
        _docker_network_remove_labeled_project(COMPOSE_PROJECT_NAME)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: labeled network removal failed unexpectedly: {exc}")

    try:
        _docker_network_remove_strays_by_name_prefix(COMPOSE_PROJECT_NAME)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: stray network cleanup failed unexpectedly: {exc}")

    try:
        _cleanup_grounded_review_temp_clones(GROUNDED_REVIEW_TEMP_CLONES_DIR)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: temp clone cleanup failed unexpectedly: {exc}")

    try:
        from local_ai_stack.log_steward import run_log_steward

        run_log_steward(ROOT, announce=_print)
    except Exception as exc:
        _print(f"Warning: LogSteward failed: {exc}")

    _print("Down cleanup finished.")


def _octospork_labeled_container_ids() -> list[str]:
    """Container IDs with ``com.octospork.project=<COMPOSE_PROJECT_NAME>`` (running or stopped)."""
    filt = f"label={OCTO_SPORK_PROJECT_LABEL_KEY}={COMPOSE_PROJECT_NAME}"
    try:
        completed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", filt],
            cwd=str(ROOT),
            check=False,
            text=True,
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _print(f"Warning: could not list labeled Docker containers: {exc}")
        return []
    if completed.returncode != 0:
        err = (completed.stderr or "").strip()
        _print(f"Warning: docker ps failed: {err}")
        return []
    return [ln.strip() for ln in (completed.stdout or "").splitlines() if ln.strip()]


def _docker_rm_force_container_ids(ids: list[str]) -> None:
    """``docker rm -f`` in batches (argument length limits on Windows)."""
    if not ids:
        return
    chunk_size = 40
    for i in range(0, len(ids), chunk_size):
        batch = ids[i : i + chunk_size]
        try:
            completed = subprocess.run(
                ["docker", "rm", "-f", *batch],
                cwd=str(ROOT),
                check=False,
                text=True,
                capture_output=True,
                timeout=300,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            _print(f"Warning: docker rm -f batch failed: {exc}")
            continue
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            _print(f"Warning: docker rm -f exit {completed.returncode}: {detail[:1200]}")


def _chmod_writable_best_effort(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        os.chmod(path, mode | stat.S_IWRITE)
    except OSError:
        pass


def _remove_pid_lock_file(path: Path) -> bool:
    """``os.remove`` with one chmod retry (Windows/Linux permission quirks)."""
    try:
        os.remove(path)
        return True
    except PermissionError:
        _chmod_writable_best_effort(path)
        try:
            os.remove(path)
            return True
        except OSError as exc:
            _print(f"Warning: could not remove {path} after chmod: {exc}")
            return False
    except OSError as exc:
        _print(f"Warning: could not remove {path}: {exc}")
        return False


def _remove_dangling_pid_lock_files_under(local_root: Path) -> None:
    """Walk ``.local`` (recursive) and delete ``*.pid`` / ``*.lock`` files."""
    try:
        root = local_root.expanduser().resolve()
    except OSError as exc:
        _print(f"Warning: could not resolve local dir {local_root}: {exc}")
        return
    if not root.is_dir():
        _print(f"No directory at {root}; skipping pid/lock cleanup.")
        return
    removed = 0
    try:
        for pattern in ("*.pid", "*.lock"):
            for path in root.rglob(pattern):
                try:
                    if path.is_file() and _remove_pid_lock_file(path):
                        removed += 1
                except OSError as exc:
                    _print(f"Warning: skipped {path}: {exc}")
    except OSError as exc:
        _print(f"Warning: could not walk {root}: {exc}")
        return
    if removed:
        _print(f"Removed {removed} pid/lock file(s) under {root}.")
    else:
        _print(f"No .pid or .lock files found under {root}.")


def command_force_clean(env_file: Path) -> None:
    """Aggressive cleanup: kill labeled containers, remove compose networks, scrub ``.local`` locks."""
    _ = env_file  # CLI parity; Docker filters do not require parsing .env.local.
    _print(
        f"force-clean: targets label {OCTO_SPORK_PROJECT_LABEL_KEY}={COMPOSE_PROJECT_NAME!r}, "
        f"Docker networks for project {COMPOSE_PROJECT_NAME!r}, and *.pid / *.lock under .local/"
    )
    ids = _octospork_labeled_container_ids()
    if ids:
        _print(f"Removing {len(ids)} container(s) (docker rm -f) …")
        _docker_rm_force_container_ids(ids)
    else:
        _print(
            "No containers matched the octospork project label "
            "(nothing running with labels from docker-compose.project-labels.yml, or already removed)."
        )

    try:
        _docker_network_remove_labeled_project(COMPOSE_PROJECT_NAME)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: labeled network removal failed: {exc}")

    try:
        _docker_network_remove_strays_by_name_prefix(COMPOSE_PROJECT_NAME)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        _print(f"Warning: stray network cleanup failed: {exc}")

    _remove_dangling_pid_lock_files_under(ROOT / ".local")
    _print("force-clean finished.")
