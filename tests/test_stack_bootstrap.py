"""Extract-contract tests for :mod:`local_ai_stack.stack_bootstrap`.

Pins the pure move of bootstrap / up helpers out of ``__main__``:
re-export identity, no leftover function bodies, no back-reference into
``__main__``, bound helper object identity, and unchanged ``up`` / ``bootstrap`` CLI help.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MAIN_PY = _REPO / "local_ai_stack" / "__main__.py"
_BOOT_PY = _REPO / "local_ai_stack" / "stack_bootstrap.py"
_ORCH_PY = _REPO / "local_ai_stack" / "stack_orchestrator.py"
_TEARDOWN_PY = _REPO / "local_ai_stack" / "stack_teardown.py"

_MOVED_DEFS = (
    "def _run(",
    "def _resolve_agenticseek_path",
    "def _normalize_dockerfile_backend",
    "def ensure_repo_local_data_dirs",
    "def _claude_agent_stack_available",
    "def _ensure_claude_config_dir",
    "def _configure_agenticseek_ini",
    "def _compose_base_args",
    "def _merge_process_env",
    "def _orchestrator_log_path",
    "def _configure_orchestrator_file_logger",
    "def _http_ok",
    "def _start_ollama_if_needed",
    "def _wait_for_http",
    "def bootstrap",
    "def command_up",
)


def _import_names(src: str) -> list[str]:
    tree = ast.parse(src)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names.append(mod)
            if mod == "local_ai_stack":
                names.extend(alias.name for alias in node.names)
    return names


def test_module_does_not_import_main() -> None:
    src = _BOOT_PY.read_text(encoding="utf-8")
    for name in _import_names(src):
        assert name != "__main__", f"stack_bootstrap imports {name!r}"
        assert name != "local_ai_stack.__main__", f"stack_bootstrap imports {name!r}"
        assert not name.startswith("local_ai_stack.__main__"), f"stack_bootstrap imports {name!r}"
    assert 'import_module("local_ai_stack.__main__")' not in src
    assert "import_module('local_ai_stack.__main__')" not in src


def test_orchestrator_and_teardown_do_not_import_bootstrap() -> None:
    for path, label in ((_ORCH_PY, "stack_orchestrator"), (_TEARDOWN_PY, "stack_teardown")):
        src = path.read_text(encoding="utf-8")
        for name in _import_names(src):
            assert name != "local_ai_stack.stack_bootstrap", f"{label} imports {name!r}"
            assert name != "stack_bootstrap", f"{label} imports {name!r}"


def test_main_reexports_same_objects_and_does_not_redefine() -> None:
    src = _MAIN_PY.read_text(encoding="utf-8")
    for name in _MOVED_DEFS:
        assert name not in src, f"duplicate bootstrap helper left in __main__.py: {name}"

    from local_ai_stack import stack_bootstrap
    from local_ai_stack import __main__ as stack_main

    assert stack_main._run is stack_bootstrap._run
    assert stack_main._resolve_agenticseek_path is stack_bootstrap._resolve_agenticseek_path
    assert stack_main._normalize_dockerfile_backend is stack_bootstrap._normalize_dockerfile_backend
    assert stack_main.ensure_repo_local_data_dirs is stack_bootstrap.ensure_repo_local_data_dirs
    assert stack_main._claude_agent_stack_available is stack_bootstrap._claude_agent_stack_available
    assert stack_main._ensure_claude_config_dir is stack_bootstrap._ensure_claude_config_dir
    assert stack_main._configure_agenticseek_ini is stack_bootstrap._configure_agenticseek_ini
    assert stack_main._compose_base_args is stack_bootstrap._compose_base_args
    assert stack_main._merge_process_env is stack_bootstrap._merge_process_env
    assert stack_main._orchestrator_log_path is stack_bootstrap._orchestrator_log_path
    assert stack_main._configure_orchestrator_file_logger is (
        stack_bootstrap._configure_orchestrator_file_logger
    )
    assert stack_main._http_ok is stack_bootstrap._http_ok
    assert stack_main._start_ollama_if_needed is stack_bootstrap._start_ollama_if_needed
    assert stack_main._wait_for_http is stack_bootstrap._wait_for_http
    assert stack_main.bootstrap is stack_bootstrap.bootstrap
    assert stack_main.command_up is stack_bootstrap.command_up


def test_importing_bootstrap_binds_orchestrator_and_teardown_helpers() -> None:
    from local_ai_stack import stack_bootstrap as boot
    from local_ai_stack import stack_orchestrator as orch
    from local_ai_stack import stack_teardown as teardown

    assert orch.bootstrap is boot.bootstrap
    assert orch._run is boot._run
    assert orch._compose_base_args is boot._compose_base_args
    assert orch._merge_process_env is boot._merge_process_env
    assert orch._start_ollama_if_needed is boot._start_ollama_if_needed
    assert orch._claude_agent_stack_available is boot._claude_agent_stack_available
    assert teardown._compose_base_args is boot._compose_base_args
    assert teardown._merge_process_env is boot._merge_process_env
    assert teardown._resolve_agenticseek_path is boot._resolve_agenticseek_path


def test_cli_up_and_bootstrap_help_unchanged() -> None:
    up = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "up", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert up.returncode == 0
    assert "--env-file" in up.stdout
    assert "--no-rewrite-conflicting-ports" in up.stdout
    assert "Path to .env.local" in up.stdout

    boot = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "bootstrap", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert boot.returncode == 0
    assert "--env-file" in boot.stdout
    assert "Path to .env.local" in boot.stdout

    parent = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert parent.returncode == 0
    assert "Cross-platform local AI stack runner" in parent.stdout
    assert "up" in parent.stdout
    assert "bootstrap" in parent.stdout
