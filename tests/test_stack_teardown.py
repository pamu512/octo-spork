"""Extract-contract tests for :mod:`local_ai_stack.stack_teardown`.

Pins the pure move of down / force-clean helpers out of ``__main__``:
re-export identity, no leftover function bodies, no back-reference into
``__main__``, and unchanged CLI help for ``down`` / ``force-clean``.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MAIN_PY = _REPO / "local_ai_stack" / "__main__.py"
_TEARDOWN_PY = _REPO / "local_ai_stack" / "stack_teardown.py"

_MOVED_DEFS = (
    "def _run_subprocess_allow_fail",
    "def _docker_volume_prune_project",
    "def _docker_network_ids_with_project_label",
    "def _docker_network_remove_by_id",
    "def _docker_network_remove_labeled_project",
    "def _docker_network_map_name_by_id",
    "def _docker_network_remove_strays_by_name_prefix",
    "def _cleanup_grounded_review_temp_clones",
    "def _compose_down_stack",
    "def command_down",
    "def _octospork_labeled_container_ids",
    "def _docker_rm_force_container_ids",
    "def _chmod_writable_best_effort",
    "def _remove_pid_lock_file",
    "def _remove_dangling_pid_lock_files_under",
    "def command_force_clean",
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
    src = _TEARDOWN_PY.read_text(encoding="utf-8")
    for name in _import_names(src):
        assert name != "__main__", f"stack_teardown imports {name!r}"
        assert name != "local_ai_stack.__main__", f"stack_teardown imports {name!r}"
        assert not name.startswith("local_ai_stack.__main__"), f"stack_teardown imports {name!r}"
    assert 'import_module("local_ai_stack.__main__")' not in src
    assert "import_module('local_ai_stack.__main__')" not in src


def test_main_reexports_same_objects_and_does_not_redefine() -> None:
    src = _MAIN_PY.read_text(encoding="utf-8")
    for name in _MOVED_DEFS:
        assert name not in src, f"duplicate teardown helper left in __main__.py: {name}"

    from local_ai_stack import stack_teardown
    from local_ai_stack import __main__ as stack_main

    assert stack_main._compose_down_stack is stack_teardown._compose_down_stack
    assert stack_main._docker_volume_prune_project is stack_teardown._docker_volume_prune_project
    assert stack_main._docker_network_remove_labeled_project is stack_teardown._docker_network_remove_labeled_project
    assert stack_main._docker_network_remove_strays_by_name_prefix is (
        stack_teardown._docker_network_remove_strays_by_name_prefix
    )
    assert stack_main._cleanup_grounded_review_temp_clones is stack_teardown._cleanup_grounded_review_temp_clones
    assert stack_main.command_down is stack_teardown.command_down
    assert stack_main._octospork_labeled_container_ids is stack_teardown._octospork_labeled_container_ids
    assert stack_main._docker_rm_force_container_ids is stack_teardown._docker_rm_force_container_ids
    assert stack_main._remove_pid_lock_file is stack_teardown._remove_pid_lock_file
    assert stack_main._remove_dangling_pid_lock_files_under is stack_teardown._remove_dangling_pid_lock_files_under
    assert stack_main.command_force_clean is stack_teardown.command_force_clean
    assert stack_main._run_subprocess_allow_fail is stack_teardown._run_subprocess_allow_fail


def test_cli_helpers_bound_from_main_are_same_objects() -> None:
    """Helpers that still live in the CLI module must be the same objects, not copies."""
    from local_ai_stack import stack_teardown as teardown
    from local_ai_stack import __main__ as stack_main

    assert teardown._compose_base_args is stack_main._compose_base_args
    assert teardown._merge_process_env is stack_main._merge_process_env
    assert teardown._resolve_agenticseek_path is stack_main._resolve_agenticseek_path


def test_cli_down_and_force_clean_help_unchanged() -> None:
    down = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "down", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert down.returncode == 0
    assert "--env-file" in down.stdout
    assert "Path to .env.local" in down.stdout

    force = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "force-clean", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert force.returncode == 0
    assert "--env-file" in force.stdout
    assert "Path to .env.local" in force.stdout

    parent = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert parent.returncode == 0
    assert "Cross-platform local AI stack runner" in parent.stdout
    assert "down" in parent.stdout
    assert "force-clean" in parent.stdout
    assert "com.octospork.project" in parent.stdout
    assert "*.pid/*.lock" in parent.stdout
