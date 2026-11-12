"""Extract-contract tests for :mod:`local_ai_stack.stack_verify`.

Pins the pure move of verify / hook-infra probes and log-tail helpers out of
``__main__``: re-export identity, no leftover function bodies, no back-reference
into ``__main__``, helper object identity, and unchanged ``verify`` CLI help.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[1]
_MAIN_PY = _REPO / "local_ai_stack" / "__main__.py"
_VERIFY_PY = _REPO / "local_ai_stack" / "stack_verify.py"
_STATUS_PY = _REPO / "local_ai_stack" / "status.py"
_SCAN_PY = _REPO / "local_ai_stack" / "pre_push_scan.py"

_MOVED_DEFS = (
    "def _docker_logs_tail",
    "def _ollama_host_log_snippet",
    "def _hook_probe_n8n_ollama_reachability",
    "def run_hook_infra_health_probe",
    "def command_verify",
)

_MOVED_NAMES = {
    "STATUS_CONTAINER_N8N",
    "STATUS_CONTAINER_REDIS",
    "STATUS_CONTAINER_SEARXNG",
    "HOOK_LABEL_TO_CONTAINER",
    "_docker_logs_tail",
    "_ollama_host_log_snippet",
    "_hook_probe_n8n_ollama_reachability",
    "run_hook_infra_health_probe",
    "command_verify",
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


def test_module_does_not_import_main() -> None:
    src = _VERIFY_PY.read_text(encoding="utf-8")
    for name in _import_names(src):
        assert name != "__main__", f"stack_verify imports {name!r}"
        assert name != "local_ai_stack.__main__", f"stack_verify imports {name!r}"
        assert not name.startswith("local_ai_stack.__main__"), f"stack_verify imports {name!r}"
    assert 'import_module("local_ai_stack.__main__")' not in src
    assert "import_module('local_ai_stack.__main__')" not in src
    from_main = _imported_from(src, "local_ai_stack.__main__")
    assert "ROOT" not in from_main
    assert "_print" not in from_main
    assert "_repo_path_from_arg" not in from_main


def test_main_reexports_same_objects_and_does_not_redefine() -> None:
    src = _MAIN_PY.read_text(encoding="utf-8")
    for name in _MOVED_DEFS:
        assert name not in src, f"duplicate verify helper left in __main__.py: {name}"
    assert 'STATUS_CONTAINER_SEARXNG = "local-ai-searxng"' not in src
    assert 'STATUS_CONTAINER_REDIS = "local-ai-redis"' not in src
    assert 'STATUS_CONTAINER_N8N = "local-ai-n8n"' not in src
    assert "HOOK_LABEL_TO_CONTAINER: dict[str, str]" not in src

    from local_ai_stack import stack_verify
    from local_ai_stack import __main__ as stack_main

    assert stack_main.STATUS_CONTAINER_SEARXNG is stack_verify.STATUS_CONTAINER_SEARXNG
    assert stack_main.STATUS_CONTAINER_REDIS is stack_verify.STATUS_CONTAINER_REDIS
    assert stack_main.STATUS_CONTAINER_N8N is stack_verify.STATUS_CONTAINER_N8N
    assert stack_main.HOOK_LABEL_TO_CONTAINER is stack_verify.HOOK_LABEL_TO_CONTAINER
    assert stack_main._docker_logs_tail is stack_verify._docker_logs_tail
    assert stack_main._ollama_host_log_snippet is stack_verify._ollama_host_log_snippet
    assert stack_main._hook_probe_n8n_ollama_reachability is (
        stack_verify._hook_probe_n8n_ollama_reachability
    )
    assert stack_main.run_hook_infra_health_probe is stack_verify.run_hook_infra_health_probe
    assert stack_main.command_verify is stack_verify.command_verify


def test_helpers_imported_from_extracted_modules_are_same_objects() -> None:
    from local_ai_stack import env_validation
    from local_ai_stack import stack_bootstrap as boot
    from local_ai_stack import stack_verify as verify

    assert verify._parse_env_file is env_validation._parse_env_file
    assert verify._http_ok is boot._http_ok
    assert verify._wait_for_http is boot._wait_for_http
    assert verify._compose_base_args is boot._compose_base_args
    assert verify._merge_process_env is boot._merge_process_env
    assert verify._resolve_agenticseek_path is boot._resolve_agenticseek_path
    assert verify._run is boot._run


def test_status_and_pre_push_scan_do_not_import_moved_symbols_from_main() -> None:
    status_from_main = _imported_from(_STATUS_PY.read_text(encoding="utf-8"), "local_ai_stack.__main__")
    scan_from_main = _imported_from(_SCAN_PY.read_text(encoding="utf-8"), "local_ai_stack.__main__")
    assert not (status_from_main & _MOVED_NAMES), status_from_main & _MOVED_NAMES
    assert not (scan_from_main & _MOVED_NAMES), scan_from_main & _MOVED_NAMES

    status_from_verify = _imported_from(_STATUS_PY.read_text(encoding="utf-8"), "local_ai_stack.stack_verify")
    scan_from_verify = _imported_from(_SCAN_PY.read_text(encoding="utf-8"), "local_ai_stack.stack_verify")
    assert {
        "STATUS_CONTAINER_N8N",
        "STATUS_CONTAINER_REDIS",
        "STATUS_CONTAINER_SEARXNG",
        "_docker_logs_tail",
        "_ollama_host_log_snippet",
    } <= status_from_verify
    assert "run_hook_infra_health_probe" in scan_from_verify


def test_install_verify_logic_hook_stays_in_main_with_silent_chmod() -> None:
    main_src = _MAIN_PY.read_text(encoding="utf-8")
    verify_src = _VERIFY_PY.read_text(encoding="utf-8")
    assert "def command_install_verify_logic_hook" in main_src
    assert "def command_install_verify_logic_hook" not in verify_src

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


def test_pre_push_scan_health_patches_stack_verify_probe(tmp_path: Path) -> None:
    """Call site moved: patch the new module, not ``__main__``."""
    from local_ai_stack.pre_push_scan import run_pre_push_scan

    env_file = tmp_path / ".env.local"
    env_file.write_text("X=1\n", encoding="utf-8")
    with mock.patch(
        "local_ai_stack.stack_verify.run_hook_infra_health_probe",
        return_value=(True, ["[infra] FAILED n8n"]),
    ) as probe:
        with mock.patch(
            "local_ai_stack.pre_push_scan.collect_trivy_critical_evidence",
            return_value=(False, [], False),
        ):
            code = run_pre_push_scan(
                env_file,
                tmp_path,
                skip_health=False,
                skip_trivy=False,
            )
    assert code == 1
    probe.assert_called_once_with(env_file)


def test_cli_verify_help_unchanged() -> None:
    verify = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "verify", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 0
    assert "--env-file" in verify.stdout
    assert "Path to .env.local" in verify.stdout

    hook = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "install-verify-logic-hook", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert hook.returncode == 0
    assert "--force" in hook.stdout
    assert "pre-commit" in hook.stdout

    parent = subprocess.run(
        [sys.executable, "-m", "local_ai_stack", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert parent.returncode == 0
    assert "Cross-platform local AI stack runner" in parent.stdout
    assert "verify" in parent.stdout
    assert "install-verify-logic-hook" in parent.stdout
