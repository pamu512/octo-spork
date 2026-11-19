"""Extract-contract tests for :mod:`local_ai_stack.cli_helpers`.

Pins the pure move of ``_print`` and ``_repo_path_from_arg`` out of
``__main__``: re-export identity, no leftover function bodies, no back-reference
into ``__main__``, and listed helpers no longer pulling those symbols from
``__main__``.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MAIN_PY = _REPO / "local_ai_stack" / "__main__.py"
_HELPERS_PY = _REPO / "local_ai_stack" / "cli_helpers.py"
_SCAN_PY = _REPO / "local_ai_stack" / "pre_push_scan.py"
_SENTINEL_PY = _REPO / "local_ai_stack" / "port_sentinel.py"
_STATUS_PY = _REPO / "local_ai_stack" / "status.py"
_FALLBACK_PY = _REPO / "local_ai_stack" / "model_fallback.py"
_BENCH_PY = _REPO / "local_ai_stack" / "benchmark.py"
_BOOT_PY = _REPO / "local_ai_stack" / "stack_bootstrap.py"
_VERIFY_PY = _REPO / "local_ai_stack" / "stack_verify.py"
_DOCTOR_FIX_PY = _REPO / "local_ai_stack" / "doctor_fix.py"

_MOVED_DEFS = (
    "def _print(message: str) -> None:",
    "def _repo_path_from_arg(raw: str) -> Path:",
)

_FORBIDDEN_FROM_MAIN = {
    "_print",
    "_repo_path_from_arg",
    "_http_get_status",
    "_parse_env_file",
    "_import_main_helpers",
}


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


def _imported_from(src: str, module: str) -> set[str]:
    tree = ast.parse(src)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "") == module:
            found.update(alias.name for alias in node.names)
    return found


def _module_level_func_names(src: str) -> set[str]:
    tree = ast.parse(src)
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def test_helpers_module_does_not_import_main() -> None:
    src = _HELPERS_PY.read_text(encoding="utf-8")
    for name in _import_names(src):
        assert name != "__main__", f"cli_helpers imports {name!r}"
        assert name != "local_ai_stack.__main__", f"cli_helpers imports {name!r}"
        assert not name.startswith("local_ai_stack.__main__"), f"cli_helpers imports {name!r}"
    assert 'import_module("local_ai_stack.__main__")' not in src
    assert "import_module('local_ai_stack.__main__')" not in src
    from_main = _imported_from(src, "local_ai_stack.__main__")
    assert not from_main


def test_main_reexports_same_objects_and_does_not_redefine() -> None:
    src = _MAIN_PY.read_text(encoding="utf-8")
    for name in _MOVED_DEFS:
        assert name not in src, f"duplicate helper left in __main__.py: {name}"
    defined = _module_level_func_names(src)
    assert "_print" not in defined
    assert "_repo_path_from_arg" not in defined

    from local_ai_stack import cli_helpers
    from local_ai_stack import __main__ as stack_main

    assert stack_main._print is cli_helpers._print
    assert stack_main._repo_path_from_arg is cli_helpers._repo_path_from_arg


def test_moved_bodies_match_prior_definitions() -> None:
    src = _HELPERS_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    bodies: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {"_print", "_repo_path_from_arg"}:
            bodies[node.name] = node
    assert set(bodies) == {"_print", "_repo_path_from_arg"}

    print_src = ast.get_source_segment(src, bodies["_print"])
    assert print_src is not None
    assert print_src.splitlines() == [
        "def _print(message: str) -> None:",
        "    print(message, flush=True)",
    ]

    repo_src = ast.get_source_segment(src, bodies["_repo_path_from_arg"])
    assert repo_src is not None
    assert repo_src.splitlines() == [
        "def _repo_path_from_arg(raw: str) -> Path:",
        "    path = Path(raw).expanduser()",
        "    if not path.is_absolute():",
        "        path = Path.cwd() / path",
        "    return path.resolve()",
    ]


def test_repo_path_from_arg_resolves_relative_against_cwd(
    tmp_path: Path, monkeypatch: object
) -> None:
    from local_ai_stack.cli_helpers import _repo_path_from_arg

    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    rel = tmp_path / "work"
    rel.mkdir()
    assert _repo_path_from_arg("work") == rel.resolve()
    assert _repo_path_from_arg(str(rel)) == rel.resolve()
    assert _repo_path_from_arg("~/no-such-octo-rel-check").is_absolute()


def test_listed_helpers_do_not_import_moved_symbols_from_main() -> None:
    for path in (_SCAN_PY, _SENTINEL_PY, _STATUS_PY, _FALLBACK_PY, _BENCH_PY):
        src = path.read_text(encoding="utf-8")
        from_main = _imported_from(src, "local_ai_stack.__main__")
        leaked = from_main & _FORBIDDEN_FROM_MAIN
        assert not leaked, f"{path.name} still imports {sorted(leaked)} from __main__"
        names = _import_names(src)
        assert "__main__" not in names or path == _BENCH_PY
        if path != _BENCH_PY:
            assert "local_ai_stack.__main__" not in names
            assert "def _import_main_helpers" not in src


def test_listed_helpers_bind_from_extracted_modules() -> None:
    scan_src = _SCAN_PY.read_text(encoding="utf-8")
    sentinel_src = _SENTINEL_PY.read_text(encoding="utf-8")
    status_src = _STATUS_PY.read_text(encoding="utf-8")
    fallback_src = _FALLBACK_PY.read_text(encoding="utf-8")
    bench_src = _BENCH_PY.read_text(encoding="utf-8")

    scan_helpers = _imported_from(scan_src, "local_ai_stack.cli_helpers")
    assert "_print" in scan_helpers
    assert "_repo_path_from_arg" in scan_helpers
    assert "ROOT" not in _imported_from(scan_src, "local_ai_stack.__main__")
    assert "ROOT = Path(__file__).resolve().parents[1]" in scan_src

    assert "_print" in _imported_from(sentinel_src, "local_ai_stack.cli_helpers")
    assert "def _import_main_helpers" not in sentinel_src
    assert "_rewrite = m._rewrite_env_file_string_values" not in sentinel_src
    assert "m._print" not in sentinel_src

    assert "_print" in _imported_from(status_src, "local_ai_stack.cli_helpers")
    assert "_http_get_status" in _imported_from(status_src, "local_ai_stack.port_plan")
    assert "_parse_env_file" in _imported_from(status_src, "local_ai_stack.env_validation")

    assert "_print" in _imported_from(fallback_src, "local_ai_stack.cli_helpers")
    assert "_rewrite_env_file_string_values" in _imported_from(
        fallback_src, "local_ai_stack.port_plan"
    )

    assert "_parse_env_file" in _imported_from(bench_src, "local_ai_stack.env_validation")
    assert "_load_grounded_review" in _imported_from(bench_src, "local_ai_stack.__main__")
    assert "_parse_env_file" not in _imported_from(bench_src, "local_ai_stack.__main__")


def test_bootstrap_and_verify_keep_local_print() -> None:
    boot_src = _BOOT_PY.read_text(encoding="utf-8")
    verify_src = _VERIFY_PY.read_text(encoding="utf-8")
    assert "def _print(message: str) -> None:" in boot_src
    assert "def _print(message: str) -> None:" in verify_src
    assert "_print" in _module_level_func_names(boot_src)
    assert "_print" in _module_level_func_names(verify_src)


def test_install_verify_logic_hook_stays_in_main_with_silent_chmod() -> None:
    main_src = _MAIN_PY.read_text(encoding="utf-8")
    helpers_src = _HELPERS_PY.read_text(encoding="utf-8")
    assert "def command_install_verify_logic_hook" in main_src
    assert "def command_install_verify_logic_hook" not in helpers_src

    tree = ast.parse(main_src)
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "command_install_verify_logic_hook"
    )
    silent_chmod = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.name is not None:
            continue
        if not (isinstance(node.type, ast.Name) and node.type.id == "OSError"):
            continue
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            silent_chmod = True
    assert silent_chmod, "command_install_verify_logic_hook chmod must still silently no-op"


def test_doctor_fix_still_calls_command_up_on_main() -> None:
    src = _DOCTOR_FIX_PY.read_text(encoding="utf-8")
    assert "import local_ai_stack.__main__ as stack_main" in src
    assert "stack_main.command_up(" in src


def test_cli_help_unchanged() -> None:
    parent = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert parent.returncode == 0
    assert "Cross-platform local AI stack runner" in parent.stdout
    assert "pre-push-scan" in parent.stdout
    assert "install-hook" in parent.stdout
    assert "status" in parent.stdout
