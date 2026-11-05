"""Extract-contract tests for :mod:`local_ai_stack.stack_orchestrator`.

Pins the pure move of ``StackOrchestrator`` out of ``__main__``: re-export
identity, no leftover class body, no back-reference into ``__main__``, and
one fail-closed method on the extracted class.
"""
from __future__ import annotations

import ast
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO = Path(__file__).resolve().parents[1]
_MAIN_PY = _REPO / "local_ai_stack" / "__main__.py"
_ORCH_PY = _REPO / "local_ai_stack" / "stack_orchestrator.py"


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
    src = _ORCH_PY.read_text(encoding="utf-8")
    for name in _import_names(src):
        assert name != "__main__", f"stack_orchestrator imports {name!r}"
        assert name != "local_ai_stack.__main__", f"stack_orchestrator imports {name!r}"
        assert not name.startswith("local_ai_stack.__main__"), f"stack_orchestrator imports {name!r}"
        assert name != "local_ai_stack.stack_bootstrap", f"stack_orchestrator imports {name!r}"
        assert name != "stack_bootstrap", f"stack_orchestrator imports {name!r}"
    assert 'import_module("local_ai_stack.__main__")' not in src
    assert "import_module('local_ai_stack.__main__')" not in src


def test_main_reexports_same_class_and_does_not_redefine() -> None:
    src = _MAIN_PY.read_text(encoding="utf-8")
    assert "class StackOrchestrator" not in src

    from local_ai_stack import stack_orchestrator
    from local_ai_stack import __main__ as stack_main

    assert stack_main.StackOrchestrator is stack_orchestrator.StackOrchestrator


def test_cli_helpers_bound_from_bootstrap_are_same_objects() -> None:
    """Helpers live in stack_bootstrap; bindings and __main__ re-exports are the same objects."""
    from local_ai_stack import stack_bootstrap as boot
    from local_ai_stack import stack_orchestrator as orch
    from local_ai_stack import __main__ as stack_main

    assert orch.bootstrap is boot.bootstrap
    assert orch._run is boot._run
    assert orch._compose_base_args is boot._compose_base_args
    assert orch._merge_process_env is boot._merge_process_env
    assert orch._start_ollama_if_needed is boot._start_ollama_if_needed
    assert orch._claude_agent_stack_available is boot._claude_agent_stack_available
    assert stack_main.bootstrap is boot.bootstrap
    assert stack_main._run is boot._run
    assert stack_main._compose_base_args is boot._compose_base_args
    assert stack_main.command_up is boot.command_up


def test_verify_docker_daemon_missing_cli(tmp_path: Path) -> None:
    from local_ai_stack.stack_orchestrator import StackOrchestrator

    env_file = tmp_path / ".env.local"
    env_file.write_text("X=1\n", encoding="utf-8")
    log = logging.getLogger("test.stack_orchestrator")
    orch = StackOrchestrator(tmp_path, env_file, log)
    with patch(
        "local_ai_stack.stack_orchestrator.subprocess.run",
        side_effect=FileNotFoundError("docker"),
    ):
        with pytest.raises(RuntimeError, match="Docker CLI was not found"):
            orch._verify_docker_daemon()


def test_cli_up_help_unchanged() -> None:
    sub = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "up", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sub.returncode == 0
    assert "--env-file" in sub.stdout
    assert "--no-rewrite-conflicting-ports" in sub.stdout
