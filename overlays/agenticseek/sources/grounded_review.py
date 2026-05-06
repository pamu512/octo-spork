"""GitHub and local repository review helpers grounded on fetched file evidence.

Responses combine repository artifacts (README, sampled files) with optional LLM
synthesis; triage limits apply so outputs are not exhaustive audits.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlparse

import requests

_ATTACH_ARCHITECTURE_MAP = None


def _subprocess_run_git_traced(repo_path: Path, git_args: list[str], **kwargs: Any) -> Any:
    """Run ``git`` with optional OpenTelemetry span when octo-spork ``observability`` is on PYTHONPATH."""
    cmd = ["git", "-C", str(repo_path)] + git_args
    try:
        from observability.tracer import get_tracing_manager

        mgr = get_tracing_manager()
        sub = git_args[0] if git_args else ""
        with mgr.tool_span(
            "git",
            attributes={"git.subcommand": sub},
        ):
            return subprocess.run(cmd, **kwargs)
    except ImportError:
        return subprocess.run(cmd, **kwargs)


def _invoke_attach_architecture_map(snapshot: dict[str, Any]) -> None:
    """Load sibling ``architecture_map.py`` via importlib (works when only grounded_review is injected)."""
    global _ATTACH_ARCHITECTURE_MAP
    if _ATTACH_ARCHITECTURE_MAP is None:

        def _noop_arch(s: dict[str, Any]) -> None:
            s.setdefault("architecture_map_block", "")

        fn = None
        am_path = Path(__file__).resolve().parent / "architecture_map.py"
        if am_path.is_file():
            spec = importlib.util.spec_from_file_location("_octo_architecture_map", am_path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                fn = getattr(mod, "attach_architecture_map", None)
        _ATTACH_ARCHITECTURE_MAP = fn or _noop_arch
    _ATTACH_ARCHITECTURE_MAP(snapshot)


def _invoke_attach_repo_graph(snapshot: dict[str, Any]) -> None:
    """Tree-sitter RepoGraph topology (octo-spork ``repo_graph`` on PYTHONPATH)."""
    try:
        from repo_graph.snapshot_hook import attach_repo_graph_topology

        attach_repo_graph_topology(snapshot)
    except ImportError:
        snapshot.setdefault("repo_graph_topology_block", "")


def _invoke_attach_sovereign_intel(snapshot: dict[str, Any]) -> None:
    """Cross-repo credential fleet memory (``sovereign_intel`` on PYTHONPATH)."""
    try:
        from sovereign_intel.attach import attach_sovereign_intel

        attach_sovereign_intel(snapshot)
    except ImportError:
        snapshot.setdefault("sovereign_intel_block", "")


def _invoke_context_governor(snapshot: dict[str, Any], ollama_base_url: str) -> None:
    """VRAM-aware summarization of low-priority evidence (:mod:`observability.context_governor`)."""
    try:
        from observability.context_governor import ContextGovernor

        gov = ContextGovernor(ollama_base_url=ollama_base_url)
        gov.maybe_compress_snapshot(snapshot, estimate_tokens=estimate_token_units)
    except ImportError:
        pass


GITHUB_REPO_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:[/?#].*)?$"
)

REVIEW_KEYWORDS = (
    "review",
    "code review",
    "harden",
    "hardening",
    "security",
    "qa",
    "quality",
    "regression",
    "explain",
    "analyze",
    "analysis",
    "audit",
    "risks",
)

CODE_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".java",
    ".rb",
    ".rs",
    ".cs",
    ".kt",
    ".swift",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".json",
    ".md",
    ".sh",
    ".dockerfile",
}

PRIORITY_HINTS = (
    "security",
    "auth",
    "docker",
    "compose",
    "workflow",
    "ci",
    "api",
    "controller",
    "service",
    "model",
    "schema",
    "migration",
    "test",
    "policy",
    "infra",
    "deploy",
)

REVIEW_DIMENSION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "security": ("security", "harden", "hardening", "auth", "threat", "vulnerability"),
    "architecture": ("architecture", "design", "system", "service", "boundary"),
    "qa": ("qa", "test", "regression", "quality", "reliability"),
    "performance": ("performance", "latency", "speed", "optimize", "cost"),
}

MUST_HAVE_PATTERNS: tuple[str, ...] = (
    "readme.md",
    ".github/workflows/",
    "docker-compose",
    "dockerfile",
    "security.md",
    "deploy/",
    "services/",
    "src/",
    "tests/",
)

SENSITIVE_EXACT_FILENAMES: frozenset[str] = frozenset(
    {
        "settings.py",
        "local_settings.py",
        "production.py",
        "secrets.py",
        "auth.ts",
        "auth.tsx",
        "auth.js",
        "auth.jsx",
        "authorization.ts",
        "middleware.ts",
        "dockerfile",
    }
)

SENSITIVE_PATH_FRAGMENTS: tuple[tuple[str, int], ...] = (
    ("docker-compose", 55),
    (".github/workflows/", 70),
    ("/auth/", 65),
    ("authentication", 50),
    ("authorization", 50),
    ("middleware", 35),
    ("security", 45),
    ("secret", 55),
    ("credential", 55),
    ("password", 40),
    ("token", 35),
    ("crypto", 40),
    ("nginx.conf", 45),
    ("helm", 40),
    ("terraform", 40),
    (".tf", 25),
    ("kubernetes", 35),
    ("kube", 30),
    ("compose.yml", 50),
    ("compose.yaml", 50),
)

CACHE_FILE_PATH = Path(os.getenv("GROUNDED_REVIEW_CACHE_FILE", "/tmp/octo-spork-grounded-cache.json"))
CACHE_TTL_SECONDS = int(os.getenv("GROUNDED_REVIEW_CACHE_TTL_SECONDS", "900"))
ANSWER_CACHE_TTL_SECONDS = int(os.getenv("GROUNDED_REVIEW_ANSWER_CACHE_TTL_SECONDS", "600"))
GROUNDED_REVIEW_ENABLE_TWO_PASS = os.getenv("GROUNDED_REVIEW_ENABLE_TWO_PASS", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GROUNDED_REVIEW_MAX_FILES = int(os.getenv("GROUNDED_REVIEW_MAX_FILES", "12"))
GROUNDED_REVIEW_MAX_TOTAL_BYTES = int(os.getenv("GROUNDED_REVIEW_MAX_TOTAL_BYTES", "220000"))
GROUNDED_REVIEW_MAX_FILE_BYTES = int(os.getenv("GROUNDED_REVIEW_MAX_FILE_BYTES", "80000"))
GROUNDED_REVIEW_NUM_CTX = int(os.getenv("GROUNDED_REVIEW_NUM_CTX", "14336"))
GROUNDED_REVIEW_NUM_CTX_TWO_PASS = int(os.getenv("GROUNDED_REVIEW_NUM_CTX_TWO_PASS", "12288"))
GROUNDED_REVIEW_CHARS_PER_TOKEN = float(os.getenv("GROUNDED_REVIEW_CHARS_PER_TOKEN", "4"))
GROUNDED_REVIEW_EVIDENCE_TOKEN_BUDGET = int(os.getenv("GROUNDED_REVIEW_EVIDENCE_TOKEN_BUDGET", "0"))
GROUNDED_REVIEW_SENSITIVE_PRIORITY_THRESHOLD = int(os.getenv("GROUNDED_REVIEW_SENSITIVE_PRIORITY_THRESHOLD", "75"))
GROUNDED_REVIEW_STRICT_COVERAGE = os.getenv("GROUNDED_REVIEW_STRICT_COVERAGE", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GROUNDED_REVIEW_TRIVY_ENABLED = os.getenv("GROUNDED_REVIEW_TRIVY_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GROUNDED_REVIEW_TRIVY_TIMEOUT = int(os.getenv("GROUNDED_REVIEW_TRIVY_TIMEOUT", "600"))
GROUNDED_REVIEW_CODEQL_ENABLED = os.getenv("GROUNDED_REVIEW_CODEQL_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GROUNDED_REVIEW_CODEQL_TIMEOUT_CREATE = int(os.getenv("GROUNDED_REVIEW_CODEQL_TIMEOUT_CREATE", "3600"))
GROUNDED_REVIEW_CODEQL_TIMEOUT_ANALYZE = int(os.getenv("GROUNDED_REVIEW_CODEQL_TIMEOUT_ANALYZE", "3600"))
GROUNDED_REVIEW_CODEQL_LANGUAGE = os.getenv("GROUNDED_REVIEW_CODEQL_LANGUAGE", "python").strip() or "python"
GROUNDED_REVIEW_CODEQL_SUITE = os.getenv(
    "GROUNDED_REVIEW_CODEQL_SUITE",
    "codeql/python-queries:codeql-suites/python-security-and-quality.qls",
).strip() or "codeql/python-queries:codeql-suites/python-security-and-quality.qls"
GROUNDED_REVIEW_CODEQL_EVIDENCE_LIMIT = int(os.getenv("GROUNDED_REVIEW_CODEQL_EVIDENCE_LIMIT", "12"))
GROUNDED_REVIEW_CODEQL_WORKDIR = os.getenv("GROUNDED_REVIEW_CODEQL_WORKDIR", "").strip()
GROUNDED_REVIEW_CODEQL_KEEP_ARTIFACTS = os.getenv("GROUNDED_REVIEW_CODEQL_KEEP_ARTIFACTS", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GROUNDED_REVIEW_DEP_AUDIT_ENABLED = os.getenv("GROUNDED_REVIEW_DEP_AUDIT_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GROUNDED_REVIEW_DEP_AUDIT_TIMEOUT = int(os.getenv("GROUNDED_REVIEW_DEP_AUDIT_TIMEOUT", "300"))
GROUNDED_REVIEW_DEP_AUDIT_MAX_ROWS = int(os.getenv("GROUNDED_REVIEW_DEP_AUDIT_MAX_ROWS", "48"))

# Diff Manager: split oversized synthesis prompts into per-module chunks, then merge findings.
GROUNDED_DIFF_CHUNKING_ENABLED = os.getenv("GROUNDED_DIFF_CHUNKING_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GROUNDED_DIFF_CHUNK_PROMPT_TOKEN_THRESHOLD = int(os.getenv("GROUNDED_DIFF_CHUNK_PROMPT_TOKEN_THRESHOLD", "8000"))
GROUNDED_DIFF_MODULE_DEPTH = max(1, int(os.getenv("GROUNDED_DIFF_MODULE_DEPTH", "1")))
GROUNDED_DIFF_MERGE_SYNTHESIS = os.getenv("GROUNDED_DIFF_MERGE_SYNTHESIS", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

CVE_RE = re.compile(r"CVE-\d{4}-\d+")


def _normalize_pkg_name(name: str) -> str:
    return str(name or "").strip().lower().replace("_", "-")


def _escape_md_table_cell(value: str) -> str:
    s = str(value or "").replace("\r\n", " ").replace("\n", " ").replace("|", "\\|")
    return s[:2000] + ("…" if len(s) > 2000 else "")


def collect_python_direct_dependency_names(scan_root: Path) -> set[str]:
    """Best-effort direct dependency names from common Python manifests (declared deps only)."""
    names: set[str] = set()
    root = Path(scan_root).resolve()

    req_files = [root / "requirements.txt", root / "requirements-dev.txt"]
    try:
        for sub in (root / "requirements").glob("*.txt"):
            req_files.append(sub)
    except OSError:
        pass

    for rf in req_files:
        if not rf.is_file():
            continue
        try:
            text = rf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            m = re.match(r"([A-Za-z0-9][A-Za-z0-9_.\-]*(?:\[[^\]]+\])?)", line)
            if not m:
                continue
            raw = m.group(1)
            base = raw.split("[", 1)[0]
            names.add(_normalize_pkg_name(base))

    pipfile = root / "Pipfile"
    if pipfile.is_file():
        try:
            in_packages = False
            for line in pipfile.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s == "[packages]":
                    in_packages = True
                    continue
                if s.startswith("[") and s.endswith("]"):
                    in_packages = False
                    continue
                if not in_packages or not s or s.startswith("#"):
                    continue
                key = s.split("=", 1)[0].strip().strip('"').strip("'")
                if key and key.lower() != "python":
                    names.add(_normalize_pkg_name(key))
        except OSError:
            pass

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            try:
                import tomllib
            except ImportError:
                tomllib = None  # type: ignore[assignment]
            if tomllib is not None:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
                proj = data.get("project") or {}
                for dep in proj.get("dependencies") or []:
                    if isinstance(dep, str):
                        m = re.match(r"([A-Za-z0-9][A-Za-z0-9_.\-]*)", dep.strip())
                        if m:
                            names.add(_normalize_pkg_name(m.group(1)))
        except (OSError, UnicodeDecodeError, TypeError, ValueError):
            pass

    return names


def detect_dependency_audit_targets(scan_root: Path) -> tuple[bool, bool]:
    """Return (run_python, run_node) flags based on repository artifacts."""
    root = Path(scan_root).resolve()
    py_markers = (
        root / "requirements.txt",
        root / "pyproject.toml",
        root / "Pipfile",
        root / "setup.py",
        root / "setup.cfg",
    )
    has_req_dir = False
    try:
        has_req_dir = (root / "requirements").is_dir() and any((root / "requirements").glob("*.txt"))
    except OSError:
        has_req_dir = False
    wants_python = any(p.is_file() for p in py_markers) or has_req_dir
    wants_node = (root / "package.json").is_file()
    return wants_python, wants_node


@dataclass
class DepAuditRow:
    ecosystem: str
    package: str
    version_spec: str
    is_direct: bool
    severity: str
    ids: str
    fix_hint: str
    has_cve: bool
    highlight_direct_cve: bool


def _pip_audit_severity_label(_vuln: dict[str, Any]) -> str:
    return "—"


def parse_pip_audit_json(
    payload: dict[str, Any],
    *,
    direct_names: set[str],
) -> list[DepAuditRow]:
    rows: list[DepAuditRow] = []
    for dep in payload.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        if dep.get("skip_reason"):
            continue
        name_raw = str(dep.get("name") or "").strip()
        if not name_raw:
            continue
        ver = str(dep.get("version") or "").strip() or "—"
        vulns = dep.get("vulns") or []
        if not isinstance(vulns, list) or not vulns:
            continue
        id_parts: list[str] = []
        cves: list[str] = []
        for vn in vulns:
            if not isinstance(vn, dict):
                continue
            vid = str(vn.get("id") or "").strip()
            if vid:
                id_parts.append(vid)
            for al in vn.get("aliases") or []:
                if isinstance(al, str) and al:
                    id_parts.append(al)
                    if al.upper().startswith("CVE-"):
                        cves.append(al.upper())
            for m in CVE_RE.finditer(str(vn.get("description") or "")):
                cves.append(m.group(0))
        seen: set[str] = set()
        uniq_ids: list[str] = []
        for i in id_parts:
            if i not in seen:
                seen.add(i)
                uniq_ids.append(i)
        for m in CVE_RE.findall(" ".join(uniq_ids)):
            if m not in cves:
                cves.append(m)
        has_cve = bool(cves)
        is_direct = _normalize_pkg_name(name_raw) in direct_names
        fix_versions = []
        for vn in vulns:
            if isinstance(vn, dict):
                fix_versions.extend(str(x) for x in (vn.get("fix_versions") or []) if x)
        fix_hint = ", ".join(dict.fromkeys(fix_versions)) if fix_versions else "—"
        sev = _pip_audit_severity_label(vulns[0] if vulns else {})
        row = DepAuditRow(
            ecosystem="python",
            package=name_raw,
            version_spec=ver,
            is_direct=is_direct,
            severity=sev,
            ids=", ".join(uniq_ids[:12]) + (" …" if len(uniq_ids) > 12 else "") if uniq_ids else "—",
            fix_hint=fix_hint,
            has_cve=has_cve,
            highlight_direct_cve=bool(is_direct and has_cve),
        )
        rows.append(row)
    return rows


def parse_npm_audit_json(payload: dict[str, Any]) -> list[DepAuditRow]:
    rows: list[DepAuditRow] = []
    vulns = payload.get("vulnerabilities") or {}
    if not isinstance(vulns, dict):
        return rows
    for pkg_name, vul in vulns.items():
        if not isinstance(vul, dict):
            continue
        via = vul.get("via") or []
        id_parts: list[str] = []
        cves: list[str] = []
        if isinstance(via, list):
            for entry in via:
                if isinstance(entry, dict):
                    url = str(entry.get("url") or "").strip()
                    title = str(entry.get("title") or "")
                    if url:
                        id_parts.append(url)
                    for m in CVE_RE.finditer(title + " " + url):
                        cves.append(m.group(0))
                    sid = entry.get("source")
                    if sid is not None:
                        id_parts.append(f"npm-{sid}")
                elif isinstance(entry, str):
                    id_parts.append(entry)
        uniq_ids = list(dict.fromkeys(id_parts))
        has_cve = bool(cves)
        is_direct = bool(vul.get("isDirect"))
        severity = str(vul.get("severity") or "unknown")
        rng = str(vul.get("range") or "—")
        fix_hint = "—"
        fa = vul.get("fixAvailable")
        if isinstance(fa, dict):
            fv = fa.get("version")
            if fv:
                fix_hint = f"→ {fv}"
            elif fa.get("isSemVerMajor"):
                fix_hint = "semver-major bump"
        elif fa is True:
            fix_hint = "available"
        rows.append(
            DepAuditRow(
                ecosystem="npm",
                package=str(vul.get("name") or pkg_name),
                version_spec=rng,
                is_direct=is_direct,
                severity=severity,
                ids=", ".join(uniq_ids[:8]) + (" …" if len(uniq_ids) > 8 else "") if uniq_ids else "—",
                fix_hint=fix_hint,
                has_cve=has_cve,
                highlight_direct_cve=bool(is_direct and has_cve),
            )
        )
    return rows


def _dep_audit_severity_rank(severity: str) -> int:
    s = str(severity or "").lower()
    return {
        "critical": 5,
        "high": 4,
        "moderate": 3,
        "medium": 3,
        "low": 2,
        "info": 1,
        "unknown": 0,
        "—": 0,
    }.get(s, 0)


def sort_dep_audit_rows(rows: list[DepAuditRow]) -> list[DepAuditRow]:
    def key(r: DepAuditRow) -> tuple[int, int, int, str]:
        highlight = 2 if r.highlight_direct_cve else (1 if r.is_direct and r.has_cve else 0)
        rank = _dep_audit_severity_rank(r.severity)
        cve_boost = 1 if r.has_cve else 0
        return (-highlight, -rank, -cve_boost, r.package.lower())

    return sorted(rows, key=key)


def format_dep_audit_markdown_table(rows: list[DepAuditRow]) -> str:
    if not rows:
        return "_No vulnerable packages reported in this audit scope._\n"
    lines = [
        "| Risk | Ecosystem | Package | Version / constraint | Direct | Severity | IDs | Fix |",
        "|------|-----------|---------|----------------------|--------|----------|-----|-----|",
    ]
    for r in rows:
        risk = "**DIRECT + CVE**" if r.highlight_direct_cve else ("CVE" if r.has_cve else "")
        direct_cell = "Yes" if r.is_direct else "No"
        lines.append(
            "| "
            + _escape_md_table_cell(risk)
            + " | "
            + _escape_md_table_cell(r.ecosystem)
            + " | `"
            + _escape_md_table_cell(r.package)
            + "` | "
            + _escape_md_table_cell(r.version_spec)
            + " | "
            + _escape_md_table_cell(direct_cell)
            + " | "
            + _escape_md_table_cell(r.severity)
            + " | "
            + _escape_md_table_cell(r.ids)
            + " | "
            + _escape_md_table_cell(r.fix_hint)
            + " |"
        )
    lines.append("")
    lines.append(
        "Rows marked **DIRECT + CVE** are declared direct dependencies (manifest-grounded for Python; "
        "`npm audit` flag for Node) with at least one **CVE** identifier detected in advisory metadata."
    )
    lines.append("")
    return "\n".join(lines)


def _run_cmd_capture_json(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=max(30, int(timeout)),
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"executable not found for command {cmd[0]!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"command timed out after {timeout}s: {' '.join(cmd)}") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to run {' '.join(cmd)}: {exc}") from exc
    raw = (completed.stdout or "").strip()
    if not raw:
        err = (completed.stderr or "").strip()
        raise ValueError(f"empty stdout from {' '.join(cmd)} (exit {completed.returncode}); stderr={err[:800]}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON from {' '.join(cmd)}: {exc}; stdout[:500]={raw[:500]}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object from {' '.join(cmd)}, got {type(parsed).__name__}")
    return parsed


def run_pip_audit_json(scan_root: Path, *, timeout: int) -> dict[str, Any]:
    exe = shutil.which("pip-audit")
    if not exe:
        raise FileNotFoundError("pip-audit not found on PATH")
    cmd = [
        exe,
        str(scan_root.resolve()),
        "-f",
        "json",
        "--desc",
        "off",
        "--progress-spinner",
        "off",
    ]
    return _run_cmd_capture_json(cmd, cwd=scan_root, timeout=timeout)


def run_npm_audit_json(scan_root: Path, *, timeout: int) -> dict[str, Any]:
    exe = shutil.which("npm")
    if not exe:
        raise FileNotFoundError("npm not found on PATH")
    cmd = [exe, "audit", "--json"]
    return _run_cmd_capture_json(cmd, cwd=scan_root, timeout=timeout)


def format_dependency_audit_section(
    *,
    scan_root: Path,
    python_table: str | None,
    python_note: str | None,
    npm_table: str | None,
    npm_note: str | None,
) -> str:
    lines = [
        "### Dependency audit (pip-audit / npm audit)",
        "",
        f"- **Scan root:** `{scan_root.resolve()}`",
        "- **Purpose:** machine-grounded dependency vulnerability signals (not a substitute for full SBOM review).",
        "",
    ]
    lines.append("#### Python (`pip-audit`)")
    lines.append("")
    if python_note:
        lines.append(python_note.rstrip() + "\n\n")
    elif python_table:
        lines.append(python_table.rstrip() + "\n\n")
    else:
        lines.append("_Skipped (no Python manifest detected)._")
        lines.append("")

    lines.append("#### JavaScript (`npm audit`)")
    lines.append("")
    if npm_note:
        lines.append(npm_note.rstrip() + "\n\n")
    elif npm_table:
        lines.append(npm_table.rstrip() + "\n\n")
    else:
        lines.append("_Skipped (no `package.json`)._")
        lines.append("")

    return "\n".join(lines)


def attach_knowledge_sync_context(snapshot: dict[str, Any]) -> None:
    """Populate ``snapshot['knowledge_sync_block']`` when ``CLAUDE.md`` exists under ``scan_root``."""
    try:
        from github_bot.knowledge_sync import format_knowledge_sync_block_for_review

        root_raw = snapshot.get("scan_root")
        if not root_raw:
            return
        block = format_knowledge_sync_block_for_review(Path(str(root_raw)))
        if block:
            snapshot["knowledge_sync_block"] = "## Repository coding rules (KnowledgeSync)\n\n" + block + "\n"
    except ImportError:
        pass


def attach_dependency_audit_context(snapshot: dict[str, Any]) -> None:
    """Populate `snapshot['dependency_audit_block']` when `scan_root` exists and audit is enabled."""
    snapshot["dependency_audit_block"] = ""
    snapshot["dependency_audit_error"] = None
    snapshot["dependency_audit_rows"] = []

    root_raw = snapshot.get("scan_root")
    if not root_raw:
        return

    if not GROUNDED_REVIEW_DEP_AUDIT_ENABLED:
        snapshot["dependency_audit_block"] = (
            "### Dependency audit\n\nSkipped: `GROUNDED_REVIEW_DEP_AUDIT_ENABLED` is false.\n\n"
        )
        return

    scan_root = Path(str(root_raw)).expanduser().resolve()
    if not scan_root.is_dir():
        return

    wants_py, wants_node = detect_dependency_audit_targets(scan_root)
    direct_names = collect_python_direct_dependency_names(scan_root) if wants_py else set()

    py_table: str | None = None
    py_note: str | None = None
    npm_table: str | None = None
    npm_note: str | None = None
    combined_rows: list[DepAuditRow] = []

    if wants_py:
        try:
            payload = run_pip_audit_json(scan_root, timeout=GROUNDED_REVIEW_DEP_AUDIT_TIMEOUT)
            rows = parse_pip_audit_json(payload, direct_names=direct_names)
            combined_rows.extend(rows)
            trimmed = sort_dep_audit_rows(rows)[: max(1, GROUNDED_REVIEW_DEP_AUDIT_MAX_ROWS)]
            py_table = format_dep_audit_markdown_table(trimmed)
        except FileNotFoundError as exc:
            py_note = (
                f"`pip-audit` was not found on `PATH` ({exc}). "
                "Install [pip-audit](https://pypi.org/project/pip-audit/) or disable via "
                "`GROUNDED_REVIEW_DEP_AUDIT_ENABLED=false`."
            )
        except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError, TypeError) as exc:
            py_note = f"pip-audit **failed** for `{scan_root}`: `{exc}`"
    else:
        py_note = None

    if wants_node:
        try:
            payload = run_npm_audit_json(scan_root, timeout=GROUNDED_REVIEW_DEP_AUDIT_TIMEOUT)
            err_obj = payload.get("error")
            err_hint = ""
            if isinstance(err_obj, dict) and err_obj:
                err_hint = (str(err_obj.get("summary") or "") + " " + str(err_obj.get("detail") or "")).strip()
            rows = parse_npm_audit_json(payload)
            combined_rows.extend(rows)
            trimmed = sort_dep_audit_rows(rows)[: max(1, GROUNDED_REVIEW_DEP_AUDIT_MAX_ROWS)]
            table_body = format_dep_audit_markdown_table(trimmed)
            if err_hint:
                npm_table = f"> npm audit metadata reported: `{_escape_md_table_cell(err_hint)}`\n\n{table_body}"
            else:
                npm_table = table_body
        except FileNotFoundError as exc:
            npm_note = f"`npm` was not found on `PATH` ({exc})."
        except (OSError, RuntimeError, TimeoutError, ValueError, json.JSONDecodeError, TypeError) as exc:
            npm_note = f"npm audit **failed** for `{scan_root}`: `{exc}`"
    else:
        npm_note = None

    snapshot["dependency_audit_rows"] = [
        {
            "ecosystem": r.ecosystem,
            "package": r.package,
            "version_spec": r.version_spec,
            "is_direct": r.is_direct,
            "severity": r.severity,
            "ids": r.ids,
            "fix_hint": r.fix_hint,
            "has_cve": r.has_cve,
            "highlight_direct_cve": r.highlight_direct_cve,
        }
        for r in sort_dep_audit_rows(combined_rows)[:GROUNDED_REVIEW_DEP_AUDIT_MAX_ROWS]
    ]

    snapshot["dependency_audit_block"] = format_dependency_audit_section(
        scan_root=scan_root,
        python_table=py_table,
        python_note=py_note,
        npm_table=npm_table,
        npm_note=npm_note,
    )


@dataclass
class TrivyCriticalItem:
    """Single CRITICAL finding from a Trivy JSON report (vuln or misconfiguration)."""

    kind: str
    target: str
    score: float
    summary: str
    raw_id: str


class SecurityScanner:
    """Wraps the Trivy CLI for filesystem scans used in grounded security context."""

    def __init__(self, trivy_executable: str | None = None) -> None:
        resolved = trivy_executable or shutil.which("trivy")
        self._trivy_path = resolved

    def trivy_available(self) -> bool:
        return bool(self._trivy_path)

    def scan_filesystem(self, root: Path, *, timeout_seconds: int | None = None) -> dict[str, Any]:
        if self._trivy_path is None:
            raise FileNotFoundError(
                "Trivy CLI not found on PATH. Install Trivy or set GROUNDED_REVIEW_TRIVY_ENABLED=false."
            )
        scan_root = Path(root).expanduser().resolve()
        if not scan_root.is_dir():
            raise NotADirectoryError(f"Trivy scan root is not a directory: {scan_root}")
        timeout = int(timeout_seconds if timeout_seconds is not None else GROUNDED_REVIEW_TRIVY_TIMEOUT)
        cmd = [
            self._trivy_path,
            "fs",
            "--security-checks",
            "vuln,config",
            "--format",
            "json",
            "--quiet",
            str(scan_root),
        ]
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(scan_root),
                capture_output=True,
                text=True,
                timeout=max(30, timeout),
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Trivy executable missing or not runnable: {self._trivy_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Trivy timed out after {timeout}s") from exc
        except OSError as exc:
            raise RuntimeError(f"Failed to execute Trivy: {exc}") from exc
        out = (completed.stdout or "").strip()
        err = (completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = err or out or "(no output)"
            raise RuntimeError(f"Trivy exited with status {completed.returncode}: {detail[:4000]}")
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Trivy returned non-JSON output: {exc}") from exc


def _trivy_cvss_score(data: dict[str, Any]) -> float:
    """Best-effort CVSS score for sorting CRITICAL findings."""
    cvss = data.get("CVSS")
    best = 0.0
    if isinstance(cvss, dict):
        for _vendor, block in cvss.items():
            if not isinstance(block, dict):
                continue
            for key in ("V3Score", "v3_score", "Score", "score"):
                if key in block:
                    try:
                        best = max(best, float(block[key]))
                    except (TypeError, ValueError):
                        continue
    return best


def extract_top_critical_findings(report: dict[str, Any], *, limit: int = 5) -> list[TrivyCriticalItem]:
    """Collect CRITICAL vulns and misconfigs, sort by CVSS (desc), return top `limit`."""
    scored: list[tuple[float, TrivyCriticalItem]] = []
    for result in report.get("Results") or []:
        if not isinstance(result, dict):
            continue
        target = str(result.get("Target") or "")
        for vuln in result.get("Vulnerabilities") or []:
            if not isinstance(vuln, dict):
                continue
            if str(vuln.get("Severity") or "").upper() != "CRITICAL":
                continue
            score = _trivy_cvss_score(vuln)
            vid = str(vuln.get("VulnerabilityID") or "UNKNOWN")
            pkg = str(vuln.get("PkgName") or "")
            installed = str(vuln.get("InstalledVersion") or "")
            title = str(vuln.get("Title") or vid)
            summary = f"{title} — package `{pkg}` @ `{installed}`" if pkg else title
            scored.append(
                (
                    score,
                    TrivyCriticalItem(
                        kind="vulnerability",
                        target=target,
                        score=score,
                        summary=summary,
                        raw_id=vid,
                    ),
                )
            )
        for mis in result.get("Misconfigurations") or []:
            if not isinstance(mis, dict):
                continue
            if str(mis.get("Severity") or "").upper() != "CRITICAL":
                continue
            score = _trivy_cvss_score(mis)
            mid = str(mis.get("ID") or mis.get("AvdID") or mis.get("Title") or "MISCONFIG")
            title = str(mis.get("Title") or mid)
            summary = title
            scored.append(
                (
                    score,
                    TrivyCriticalItem(
                        kind="misconfiguration",
                        target=target,
                        score=score,
                        summary=summary,
                        raw_id=mid,
                    ),
                )
            )
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _s, item in scored[: max(0, int(limit))]]


def format_security_context_markdown(
    scan_root: Path,
    items: list[TrivyCriticalItem],
    *,
    command_note: str,
) -> str:
    lines = [
        "### Security Context (Trivy — grounded static analysis)",
        "",
        f"- **Scan root:** `{scan_root.resolve()}`",
        f"- **Command:** `{command_note}`",
        "",
    ]
    if not items:
        lines.extend(
            [
                "No **CRITICAL** vulnerability or misconfiguration findings reported by Trivy for this scan.",
                "(Lower severities may still exist; this block only surfaces CRITICAL per product requirement.)",
                "",
            ]
        )
    else:
        lines.append("Top **CRITICAL** findings (max 5), sorted by CVSS score when available:")
        lines.append("")
        for idx, it in enumerate(items, start=1):
            score_txt = f"{it.score:.1f}" if it.score > 0 else "n/a"
            lines.append(
                f"{idx}. **[{it.kind}]** `{it.raw_id}` (CVSS≈{score_txt}) — {it.summary}  ",
            )
            lines.append(f"   - _Target:_ `{it.target}`")
        lines.append("")
        lines.append(
            "Treat these as **machine-grounded evidence**. Cross-check against file excerpts below; "
            "cite CVE/AVD IDs when you align with them."
        )
        lines.append("")
    return "\n".join(lines)


def attach_trivy_security_context(snapshot: dict[str, Any]) -> None:
    """Populate `snapshot['security_context_block']` when a local `scan_root` exists."""
    snapshot["security_context_block"] = ""
    snapshot["trivy_scan_error"] = None
    snapshot["trivy_critical_items"] = []
    root_raw = snapshot.get("scan_root")
    if not root_raw:
        return
    if not GROUNDED_REVIEW_TRIVY_ENABLED:
        snapshot["security_context_block"] = (
            "### Security Context (Trivy)\n\nSkipped: `GROUNDED_REVIEW_TRIVY_ENABLED` is false.\n\n"
        )
        return
    scan_root = Path(str(root_raw)).expanduser().resolve()
    cmd_note = "trivy fs --security-checks vuln,config --format json --quiet <scan_root>"
    try:
        scanner = SecurityScanner()
        if not scanner.trivy_available():
            snapshot["security_context_block"] = (
                "### Security Context (Trivy)\n\n"
                "Trivy CLI was not found on `PATH`; install Trivy or disable via "
                "`GROUNDED_REVIEW_TRIVY_ENABLED=false`.\n\n"
            )
            snapshot["trivy_scan_error"] = "trivy_not_found"
            return
        report = scanner.scan_filesystem(scan_root, timeout_seconds=GROUNDED_REVIEW_TRIVY_TIMEOUT)
        items = extract_top_critical_findings(report, limit=5)
        snapshot["trivy_critical_items"] = [
            {
                "kind": it.kind,
                "target": it.target,
                "score": it.score,
                "id": it.raw_id,
                "summary": it.summary,
            }
            for it in items
        ]
        snapshot["security_context_block"] = format_security_context_markdown(
            scan_root,
            items,
            command_note=cmd_note,
        )
    except (OSError, RuntimeError, TimeoutError, FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        snapshot["trivy_scan_error"] = str(exc)
        snapshot["security_context_block"] = (
            "### Security Context (Trivy)\n\n"
            f"Trivy scan **failed** for `{scan_root}`: `{exc}`\n\n"
        )


@dataclass
class CodeQLEvidenceReceipt:
    """Single SARIF finding surfaced as prompt evidence (location + rule + message)."""

    file_path: str
    start_line: int
    end_line: int | None
    start_column: int | None
    level: str
    rule_id: str
    message: str


class CodeQLAnalyzer:
    """Wraps CodeQL CLI: database creation, suite analysis, SARIF export."""

    def __init__(self, codeql_executable: str | None = None) -> None:
        resolved = codeql_executable or shutil.which("codeql")
        self._codeql_path = resolved

    def codeql_available(self) -> bool:
        return bool(self._codeql_path)

    def create_database(
        self,
        source_root: Path,
        database_path: Path,
        *,
        language: str,
        timeout_seconds: int,
    ) -> None:
        if self._codeql_path is None:
            raise FileNotFoundError("codeql CLI not found on PATH")
        root = Path(source_root).expanduser().resolve()
        db = Path(database_path).expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"CodeQL source root is not a directory: {root}")
        db.parent.mkdir(parents=True, exist_ok=True)
        if db.exists():
            try:
                shutil.rmtree(db)
            except OSError as exc:
                raise RuntimeError(f"Could not remove stale CodeQL database directory {db}: {exc}") from exc
        lang = str(language or "python").strip() or "python"
        cmd = [
            self._codeql_path,
            "database",
            "create",
            str(db),
            f"--language={lang}",
            f"--source-root={root}",
            "--overwrite",
        ]
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=max(60, int(timeout_seconds)),
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"codeql executable not runnable: {self._codeql_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"codeql database create timed out after {timeout_seconds}s") from exc
        except OSError as exc:
            raise RuntimeError(f"codeql database create failed: {exc}") from exc
        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"codeql database create exited {completed.returncode}: {err[:6000]}")

    def analyze_to_sarif(
        self,
        database_path: Path,
        sarif_out: Path,
        *,
        query_suite: str,
        timeout_seconds: int,
    ) -> None:
        if self._codeql_path is None:
            raise FileNotFoundError("codeql CLI not found on PATH")
        db = Path(database_path).expanduser().resolve()
        out = Path(sarif_out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        suite = str(query_suite or "").strip()
        if not suite:
            raise ValueError("query_suite is empty")
        cmd = [
            self._codeql_path,
            "database",
            "analyze",
            str(db),
            "--format=sarifv2.1.0",
            f"--output={out}",
            suite,
        ]
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(db.parent),
                capture_output=True,
                text=True,
                timeout=max(60, int(timeout_seconds)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"codeql database analyze timed out after {timeout_seconds}s") from exc
        except OSError as exc:
            raise RuntimeError(f"codeql database analyze failed: {exc}") from exc
        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"codeql database analyze exited {completed.returncode}: {err[:6000]}")
        if not out.is_file() or out.stat().st_size == 0:
            raise RuntimeError(f"SARIF output missing or empty at {out}")


def _sarif_level_rank(level: str | None) -> int:
    normalized = str(level or "warning").lower()
    return {"error": 4, "warning": 3, "note": 2, "none": 1}.get(normalized, 2)


def _uri_to_repo_relative(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path or "")
        return path.lstrip("/")
    return uri.replace("\\", "/")


def summarize_sarif_evidence_receipts(
    sarif_payload: dict[str, Any],
    *,
    limit: int,
) -> list[CodeQLEvidenceReceipt]:
    """Extract highest-severity SARIF results with file + line locations for the prompt."""
    scored: list[tuple[int, int, CodeQLEvidenceReceipt]] = []
    idx = 0
    for run in sarif_payload.get("runs") or []:
        if not isinstance(run, dict):
            continue
        rules_map: dict[str, dict[str, Any]] = {}
        driver = ((run.get("tool") or {}).get("driver")) or {}
        for rule in driver.get("rules") or []:
            if isinstance(rule, dict) and rule.get("id"):
                rules_map[str(rule["id"])] = rule

        for result in run.get("results") or []:
            if not isinstance(result, dict):
                continue
            idx += 1
            level = str(result.get("level") or "warning")
            rank = _sarif_level_rank(level)
            rule_id = str(result.get("ruleId") or "")
            msg_obj = result.get("message")
            if isinstance(msg_obj, dict):
                message = str(msg_obj.get("text") or rule_id or "")
            else:
                message = str(msg_obj or "")
            message = message.strip().replace("\r\n", "\n")
            if len(message) > 400:
                message = message[:397] + "..."

            locations = result.get("locations") or []
            if not isinstance(locations, list) or not locations:
                receipt = CodeQLEvidenceReceipt(
                    file_path="(no physical location in SARIF)",
                    start_line=0,
                    end_line=None,
                    start_column=None,
                    level=level,
                    rule_id=rule_id or "(unknown rule)",
                    message=message or "(empty message)",
                )
                scored.append((rank, idx, receipt))
                continue

            phys = locations[0].get("physicalLocation") if isinstance(locations[0], dict) else {}
            if not isinstance(phys, dict):
                phys = {}
            region = phys.get("region") if isinstance(phys.get("region"), dict) else {}
            al = phys.get("artifactLocation") if isinstance(phys.get("artifactLocation"), dict) else {}
            uri = str(al.get("uri") or "")
            rel_path = _uri_to_repo_relative(uri) or uri or "(unknown file)"
            try:
                start_line = int(region.get("startLine") or 0)
            except (TypeError, ValueError):
                start_line = 0
            end_line_val = region.get("endLine")
            try:
                end_line = int(end_line_val) if end_line_val is not None else None
            except (TypeError, ValueError):
                end_line = None
            start_col_val = region.get("startColumn")
            try:
                start_column = int(start_col_val) if start_col_val is not None else None
            except (TypeError, ValueError):
                start_column = None

            receipt = CodeQLEvidenceReceipt(
                file_path=rel_path,
                start_line=start_line,
                end_line=end_line,
                start_column=start_column,
                level=level,
                rule_id=rule_id or "(unknown rule)",
                message=message or "(empty message)",
            )
            scored.append((rank, idx, receipt))

    scored.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in scored[: max(1, int(limit))]]


def format_codeql_evidence_markdown(
    receipts: list[CodeQLEvidenceReceipt],
    *,
    scan_root: Path,
    sarif_path: Path | None,
    suite_note: str,
) -> str:
    lines = [
        "### Evidence Receipts (CodeQL — SARIF)",
        "",
        f"- **Scan root:** `{scan_root.resolve()}`",
        f"- **Query suite:** `{suite_note}`",
    ]
    if sarif_path is not None:
        lines.append(f"- **SARIF output:** `{sarif_path.resolve()}`")
    lines.extend(["", "Machine-grounded locations for triage (sorted by SARIF severity level):", ""])
    if not receipts:
        lines.append("_No SARIF results with locations; repository may be clean or extraction skipped empty paths._")
        lines.append("")
        return "\n".join(lines)

    lines.append("| Severity | File | Line | Rule | Message |")
    lines.append("|----------|------|------|------|---------|")
    for r in receipts:
        loc = str(r.start_line) if r.start_line > 0 else "—"
        col = f":{r.start_column}" if r.start_column is not None else ""
        file_cell = r.file_path.replace("|", "\\|")
        msg_cell = r.message.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {r.level} | `{file_cell}` | {loc}{col} | `{r.rule_id}` | {msg_cell} |"
        )
    lines.append("")
    lines.append(
        "Cross-reference these paths when citing findings; prefer agreeing or disagreeing with CodeQL using file excerpts below."
    )
    lines.append("")
    return "\n".join(lines)


def attach_codeql_evidence(snapshot: dict[str, Any]) -> None:
    """Populate `snapshot['codeql_evidence_block']` when CodeQL CLI exists and `scan_root` is set."""
    snapshot["codeql_evidence_block"] = ""
    snapshot["codeql_evidence_items"] = []
    snapshot["codeql_error"] = None
    snapshot["codeql_sarif_path"] = None
    snapshot["codeql_database_path"] = None

    root_raw = snapshot.get("scan_root")
    if not root_raw:
        return

    if not GROUNDED_REVIEW_CODEQL_ENABLED:
        snapshot["codeql_evidence_block"] = (
            "### Evidence Receipts (CodeQL)\n\nSkipped: `GROUNDED_REVIEW_CODEQL_ENABLED` is false.\n\n"
        )
        return

    scan_root = Path(str(root_raw)).expanduser().resolve()
    analyzer = CodeQLAnalyzer()
    if not analyzer.codeql_available():
        snapshot["codeql_evidence_block"] = (
            "### Evidence Receipts (CodeQL)\n\n"
            "`codeql` was not found on `PATH`. Install the CodeQL CLI or disable this section via "
            "`GROUNDED_REVIEW_CODEQL_ENABLED=false`.\n\n"
        )
        snapshot["codeql_error"] = "codeql_not_found"
        return

    work_base = Path(GROUNDED_REVIEW_CODEQL_WORKDIR) if GROUNDED_REVIEW_CODEQL_WORKDIR else Path(tempfile.gettempdir()) / "octo-spork-codeql"
    rev = str((snapshot.get("coverage") or {}).get("revision_sha") or snapshot.get("revision_sha") or "")
    rev_short = hashlib.sha256(f"{scan_root}:{rev}".encode("utf-8")).hexdigest()[:12]
    session = uuid.uuid4().hex[:10]
    work_dir = work_base / f"gr-{rev_short}-{session}"
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / "db"
    sarif_path = work_dir / "results.sarif"
    suite = GROUNDED_REVIEW_CODEQL_SUITE
    language = GROUNDED_REVIEW_CODEQL_LANGUAGE

    try:
        analyzer.create_database(
            scan_root,
            db_path,
            language=language,
            timeout_seconds=GROUNDED_REVIEW_CODEQL_TIMEOUT_CREATE,
        )
        snapshot["codeql_database_path"] = str(db_path.resolve())
        analyzer.analyze_to_sarif(
            db_path,
            sarif_path,
            query_suite=suite,
            timeout_seconds=GROUNDED_REVIEW_CODEQL_TIMEOUT_ANALYZE,
        )
        snapshot["codeql_sarif_path"] = str(sarif_path.resolve())
        payload = json.loads(sarif_path.read_text(encoding="utf-8", errors="replace"))
        receipts = summarize_sarif_evidence_receipts(payload, limit=GROUNDED_REVIEW_CODEQL_EVIDENCE_LIMIT)
        snapshot["codeql_evidence_items"] = [
            {
                "file_path": r.file_path,
                "start_line": r.start_line,
                "end_line": r.end_line,
                "start_column": r.start_column,
                "level": r.level,
                "rule_id": r.rule_id,
                "message": r.message,
            }
            for r in receipts
        ]
        sarif_for_prompt = sarif_path if GROUNDED_REVIEW_CODEQL_KEEP_ARTIFACTS else None
        snapshot["codeql_evidence_block"] = format_codeql_evidence_markdown(
            receipts,
            scan_root=scan_root,
            sarif_path=sarif_for_prompt,
            suite_note=suite,
        )
        if GROUNDED_REVIEW_CODEQL_KEEP_ARTIFACTS:
            snapshot["codeql_sarif_path"] = str(sarif_path.resolve())
            snapshot["codeql_database_path"] = str(db_path.resolve())
    except (OSError, RuntimeError, TimeoutError, FileNotFoundError, ValueError, json.JSONDecodeError, TypeError) as exc:
        snapshot["codeql_error"] = str(exc)
        snapshot["codeql_evidence_block"] = (
            "### Evidence Receipts (CodeQL)\n\n"
            f"CodeQL pipeline **failed** for `{scan_root}`: `{exc}`\n\n"
        )
    finally:
        if not GROUNDED_REVIEW_CODEQL_KEEP_ARTIFACTS:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except (OSError, TypeError):
                pass


@dataclass
class RepoFile:
    path: str
    content: str
    size: int


def extract_github_repo(query: str) -> tuple[str, str] | None:
    for token in query.split():
        normalized = token.strip().rstrip(".,);:!?\"'")
        match = GITHUB_REPO_URL_RE.match(normalized)
        if not match:
            continue
        owner, repo = match.group(1), match.group(2).removesuffix(".git").rstrip(".")
        return owner, repo
    return None


def should_use_grounded_review(query: str) -> bool:
    if extract_github_repo(query) is None:
        return False
    lowered = query.lower()
    return any(keyword in lowered for keyword in REVIEW_KEYWORDS) or "github.com" in lowered


def build_review_profile(query: str) -> dict[str, int]:
    lowered = query.lower()
    profile: dict[str, int] = {}
    for dimension, keywords in REVIEW_DIMENSION_KEYWORDS.items():
        profile[dimension] = 3 if any(keyword in lowered for keyword in keywords) else 1
    return profile


def classify_candidate_path(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith("readme.md") or lowered.startswith("docs/"):
        return "docs"
    if lowered.startswith(".github/workflows/"):
        return "ci"
    if lowered.startswith("deploy/") or lowered.startswith("infra/") or "compose" in lowered:
        return "deploy"
    if lowered.startswith("tests/") or "/tests/" in lowered:
        return "tests"
    if lowered.startswith("services/") or lowered.startswith("src/"):
        return "app"
    if lowered.endswith("package.json") or lowered.endswith("pyproject.toml") or lowered.endswith("requirements.txt"):
        return "config"
    return "misc"


def score_candidate_path(path: str, query_tokens: set[str], review_profile: dict[str, int]) -> int:
    lowered = path.lower()
    score = 0
    if lowered.startswith(".github/workflows/"):
        score += 80
    if lowered.startswith("deploy/") or lowered.startswith("infra/"):
        score += 45
    if lowered.startswith("tests/") or "/tests/" in lowered:
        score += 40
    if lowered.startswith("services/") or lowered.startswith("src/"):
        score += 35
    if lowered.endswith("readme.md"):
        score += 100
    if lowered.endswith("security.md") or "threat" in lowered:
        score += 75
    if lowered.endswith("dockerfile") or "compose" in lowered:
        score += 65
    if lowered.endswith("pyproject.toml") or lowered.endswith("package.json"):
        score += 50
    if any(hint in lowered for hint in PRIORITY_HINTS):
        score += 20
    if query_tokens and any(token in lowered for token in query_tokens):
        score += 30
    if review_profile.get("security", 1) > 1 and any(k in lowered for k in ("security", "auth", "policy", "token", "secret")):
        score += 35
    if review_profile.get("architecture", 1) > 1 and any(k in lowered for k in ("service", "api", "controller", "model", "schema")):
        score += 30
    if review_profile.get("qa", 1) > 1 and any(k in lowered for k in ("test", "spec", "workflow", "ci")):
        score += 25
    if review_profile.get("performance", 1) > 1 and any(k in lowered for k in ("cache", "perf", "benchmark", "queue")):
        score += 20
    if "/dist/" in lowered or "/build/" in lowered or "node_modules/" in lowered:
        score -= 100
    return score


def _load_cache() -> dict[str, Any]:
    if not CACHE_FILE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_FILE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        CACHE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


def _snapshot_to_cacheable(snapshot: dict[str, Any]) -> dict[str, Any]:
    files = [
        {"path": f.path, "content": f.content, "size": f.size}
        for f in snapshot.get("files", [])
    ]
    data = dict(snapshot)
    data["files"] = files
    return data


def _snapshot_from_cacheable(snapshot: dict[str, Any]) -> dict[str, Any]:
    data = dict(snapshot)
    data["files"] = [
        RepoFile(path=str(f["path"]), content=str(f["content"]), size=int(f["size"]))
        for f in snapshot.get("files", [])
    ]
    return data


def _cache_key(owner: str, repo: str, query: str) -> str:
    q = " ".join(query.lower().split())
    return f"{owner}/{repo}::{q[:240]}"


def _answer_cache_key(owner: str, repo: str, query: str, model: str, revision_sha: str | None = None) -> str:
    base = f"answer::{model}::{_cache_key(owner, repo, query)}"
    if revision_sha:
        return f"{base}::rev:{revision_sha[:40]}"
    return base


def get_cached_snapshot(owner: str, repo: str, query: str) -> dict[str, Any] | None:
    cache = _load_cache()
    entry = cache.get(_cache_key(owner, repo, query))
    if not isinstance(entry, dict):
        return None
    ts = int(entry.get("ts", 0) or 0)
    if not ts or int(time.time()) - ts > CACHE_TTL_SECONDS:
        return None
    payload = entry.get("snapshot")
    if not isinstance(payload, dict):
        return None
    return _snapshot_from_cacheable(payload)


def set_cached_snapshot(owner: str, repo: str, query: str, snapshot: dict[str, Any]) -> None:
    cache = _load_cache()
    cache[_cache_key(owner, repo, query)] = {
        "ts": int(time.time()),
        "snapshot": _snapshot_to_cacheable(snapshot),
    }
    # Keep cache bounded.
    if len(cache) > 24:
        keys = sorted(
            cache.keys(),
            key=lambda k: int((cache.get(k) or {}).get("ts", 0) or 0),
        )
        for key in keys[:-24]:
            cache.pop(key, None)
    _save_cache(cache)


def get_cached_answer(
    owner: str, repo: str, query: str, model: str, revision_sha: str | None = None
) -> dict[str, Any] | None:
    cache = _load_cache()
    entry = cache.get(_answer_cache_key(owner, repo, query, model, revision_sha))
    if not isinstance(entry, dict):
        return None
    ts = int(entry.get("ts", 0) or 0)
    if not ts or int(time.time()) - ts > ANSWER_CACHE_TTL_SECONDS:
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("answer"), str):
        return None
    return payload


def set_cached_answer(
    owner: str, repo: str, query: str, model: str, payload: dict[str, Any], revision_sha: str | None = None
) -> None:
    cache = _load_cache()
    cache[_answer_cache_key(owner, repo, query, model, revision_sha)] = {
        "ts": int(time.time()),
        "payload": payload,
    }
    if len(cache) > 40:
        keys = sorted(
            cache.keys(),
            key=lambda k: int((cache.get(k) or {}).get("ts", 0) or 0),
        )
        for key in keys[:-40]:
            cache.pop(key, None)
    _save_cache(cache)


def git_resolve_ref(repo_path: Path, ref: str) -> str | None:
    try:
        proc = _subprocess_run_git_traced(
            repo_path,
            ["rev-parse", "--verify", ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        sha = proc.stdout.strip()
        return sha[:40] if sha else None
    except Exception:
        return None


def git_head_sha(repo_path: Path) -> str | None:
    if not (repo_path / ".git").exists():
        return None
    try:
        proc = _subprocess_run_git_traced(
            repo_path,
            ["rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        sha = proc.stdout.strip()
        return sha[:40] if sha else None
    except Exception:
        return None


def enrich_coverage_telemetry(
    snapshot: dict[str, Any],
    tree_entries: list[dict[str, Any]],
    selected_paths: list[str],
    *,
    revision_sha: str | None = None,
) -> dict[str, Any]:
    """Adds category histograms, rough prompt-scale hints, and optional revision id."""
    cov = dict(snapshot.get("coverage") or {})
    totals: dict[str, int] = {}
    for entry in tree_entries:
        if entry.get("type") != "blob":
            continue
        path = str(entry.get("path", ""))
        cat = classify_candidate_path(path)
        totals[cat] = totals.get(cat, 0) + 1
    selected_by_cat: dict[str, int] = {}
    for path in selected_paths:
        cat = classify_candidate_path(path)
        selected_by_cat[cat] = selected_by_cat.get(cat, 0) + 1
    cov["repo_files_by_category"] = totals
    cov["selected_files_by_category"] = selected_by_cat

    readme = str(snapshot.get("readme") or "")
    files = list(snapshot.get("files") or [])
    evidence_chars = len(readme) + sum(min(int(f.size), 8_000) for f in files)
    cov["approx_evidence_chars"] = evidence_chars
    cov["approx_input_tokens_hint"] = max(1, evidence_chars // 4)

    if revision_sha:
        cov["revision_sha"] = revision_sha[:40]
        snapshot["revision_sha"] = revision_sha[:40]
    return cov


def sensitive_priority_score(path: str) -> int:
    """Higher scores prioritize security- and ops-sensitive paths for Context Ranker."""
    if not path:
        return 0
    normalized = path.replace("\\", "/")
    lowered = normalized.lower()
    base = lowered.split("/")[-1]
    score = 0
    if base in SENSITIVE_EXACT_FILENAMES:
        score += 120
    elif base.endswith("dockerfile"):
        score += 110
    if base == "settings.py" or base.endswith("/settings.py"):
        score += 40
    for needle, weight in SENSITIVE_PATH_FRAGMENTS:
        if needle in lowered:
            score += weight
    return min(score, 500)


def estimate_token_units(text: str) -> int:
    """Rough token estimate for Ollama context budgeting (chars / configured ratio)."""
    if not text:
        return 0
    denom = float(GROUNDED_REVIEW_CHARS_PER_TOKEN) if GROUNDED_REVIEW_CHARS_PER_TOKEN > 0 else 4.0
    return max(1, int(len(text) / denom))


def compute_evidence_token_budget() -> int:
    """Budget reserved for ranked repository file excerpts in the synthesis prompt."""
    if GROUNDED_REVIEW_EVIDENCE_TOKEN_BUDGET > 0:
        return int(GROUNDED_REVIEW_EVIDENCE_TOKEN_BUDGET)
    return min(16000, max(4096, int(GROUNDED_REVIEW_NUM_CTX * 3 // 5)))


def _encode_path_segments_github(rel_path: str) -> str:
    segments = [s for s in rel_path.replace("\\", "/").split("/") if s != ""]
    return "/".join(quote(seg, safe="") for seg in segments)


def build_source_uri_markdown(snapshot: dict[str, Any], rel_path: str, *, line_start: int = 1) -> str:
    """Strict evidence locator: `source://[label](canonical-url)` for citations."""
    rel_norm = rel_path.replace("\\", "/").lstrip("/")
    owner = str(snapshot.get("owner") or "")
    repo = str(snapshot.get("repo") or "repo")
    branch = str(snapshot.get("default_branch") or "main")
    line_start = max(1, int(line_start))

    if owner == "local":
        scan_root_raw = snapshot.get("scan_root")
        if scan_root_raw:
            abs_file = Path(str(scan_root_raw)).expanduser().resolve() / rel_norm
            try:
                url = abs_file.as_uri()
            except (ValueError, OSError):
                url = "file://" + quote(str(abs_file))
            label = f"{repo}/{rel_norm}#L{line_start}"
        else:
            url = f"file:///{quote(repo + '/' + rel_norm, safe='/')}"
            label = f"local/{repo}/{rel_norm}#L{line_start}"
        return f"source://[{label}]({url})"

    path_enc = _encode_path_segments_github(rel_norm)
    branch_enc = quote(branch, safe="/@:+._-")
    url = f"https://github.com/{quote(owner, safe='')}/{quote(repo, safe='')}/blob/{branch_enc}/{path_enc}"
    label = f"github.com/{owner}/{repo}/blob/{branch}/{rel_norm}#L{line_start}"
    return f"source://[{label}]({url})"


def rank_evidence_files(files: list[RepoFile]) -> list[RepoFile]:
    """Context Ranker: sensitive paths first, then larger files, then stable path order."""
    return sorted(
        files,
        key=lambda rf: (-sensitive_priority_score(rf.path), -rf.size, rf.path),
    )


def render_context_ranked_evidence(snapshot: dict[str, Any]) -> str:
    """Token-budgeted, URI-prefixed code blocks for the synthesis prompt."""
    files = list(snapshot.get("files") or [])
    if not files:
        return "_No file excerpts retrieved._"
    ranked = rank_evidence_files(files)
    budget = compute_evidence_token_budget()
    used = 0
    blocks: list[str] = []
    per_file_cap = max(
        800,
        int(GROUNDED_REVIEW_MAX_FILE_BYTES / max(float(GROUNDED_REVIEW_CHARS_PER_TOKEN), 1.0)),
    )

    for rf in ranked:
        uri_line = build_source_uri_markdown(snapshot, rf.path, line_start=1)
        overhead = estimate_token_units(uri_line + "\n```text\n```\n")
        remaining = budget - used
        if remaining <= overhead + 8:
            break
        body = rf.content or ""
        allowance_tokens = min(per_file_cap, remaining - overhead - 4)
        if allowance_tokens <= 0:
            continue
        max_chars = max(256, int(allowance_tokens * float(GROUNDED_REVIEW_CHARS_PER_TOKEN)))
        if len(body) > max_chars:
            body = body[:max_chars] + "\n\n… [truncated to respect context token budget]"
        block = f"{uri_line}\n```text\n{body}\n```"
        used += estimate_token_units(block)
        blocks.append(block)
        if used >= budget:
            break

    if not blocks:
        return "_No file excerpts fit within the configured evidence token budget._"
    footer = (
        f"_Approximate evidence encoding: {GROUNDED_REVIEW_CHARS_PER_TOKEN} characters ≈ 1 token; "
        f"budget ≈ {budget} tokens; blocks included: {len(blocks)}._"
    )
    return "\n\n".join(blocks) + "\n\n" + footer


def _apply_snapshot_compression_if_needed(snapshot: dict[str, Any]) -> None:
    """Trim ranked evidence files after a VRAM spike (:mod:`observability.performance_tracker`)."""
    try:
        from observability.performance_tracker import compression_targets, should_compress_evidence
    except ImportError:
        return
    if not should_compress_evidence():
        return
    targets = compression_targets()
    if not targets:
        return
    files = list(snapshot.get("files") or [])
    if not files:
        return
    ranked = rank_evidence_files(files)
    max_keep = int(targets["max_files"])
    ratio = float(targets["content_ratio"])
    trimmed: list[RepoFile] = []
    for rf in ranked[:max_keep]:
        body = rf.content or ""
        if len(body) > 256 and ratio < 1.0:
            nlen = max(256, int(len(body) * ratio))
            body = body[:nlen] + "\n\n… [context compression: truncated for VRAM stability]\n"
        trimmed.append(
            RepoFile(path=rf.path, content=body, size=len(body.encode("utf-8", errors="replace")))
        )
    snapshot["files"] = trimmed
    snapshot["sources"] = list(dict.fromkeys([r.path for r in trimmed] + list(snapshot.get("sources") or [])))


EVIDENCE_CITATION_CONTRACT = """### Evidence citation contract (mandatory)
- Every factual claim about **specific repository files or code** MUST end with an inline markdown citation whose **link target** exactly matches the URL inside the parentheses of a `source://[label](URL)` line from the **Ranked repository evidence** section below (copy the `(...)` URL verbatim).
- If you state something supported only by Trivy, CodeQL, or **Dependency audit** tables, prefix with **(Scanner)** / **(Dependency audit)** and tie claims to the cited row (package + ID); do not use `source://` URLs for those unless you also link a matching file excerpt.
- The **Architecture map** Mermaid diagram is contextual coupling evidence — prefix structural claims with **(Architecture map)** when relying on it alone.
- When you mention a **DIRECT + CVE** row, restate the package name and CVE/advisory IDs exactly as shown in the dependency table.
- Mark pure inference as **(Inference)** and do not attach a `source://` URL.
- Never invent file paths, URLs, or citations that do not appear in this prompt.
"""


def detect_recent_local_changes(repo_path: Path) -> set[str]:
    hints: set[str] = set()
    try:
        status = _subprocess_run_git_traced(
            repo_path,
            ["status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in status.stdout.splitlines():
            candidate = line[3:].strip()
            if candidate:
                hints.add(candidate)
    except Exception:
        pass

    try:
        diff = _subprocess_run_git_traced(
            repo_path,
            ["diff", "--name-only", "HEAD~1..HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in diff.stdout.splitlines():
            candidate = line.strip()
            if candidate:
                hints.add(candidate)
    except Exception:
        pass
    return hints


def select_candidate_files(
    tree_entries: Iterable[dict[str, Any]],
    query: str = "",
    preferred_paths: set[str] | None = None,
    max_files: int = GROUNDED_REVIEW_MAX_FILES,
    max_total_bytes: int = GROUNDED_REVIEW_MAX_TOTAL_BYTES,
    max_file_bytes: int = GROUNDED_REVIEW_MAX_FILE_BYTES,
) -> list[str]:
    query_tokens = set(re.findall(r"[a-z0-9_]+", query.lower()))
    review_profile = build_review_profile(query)
    preferred_lower = {p.lower() for p in (preferred_paths or set())}
    candidates: list[dict[str, Any]] = []
    for entry in tree_entries:
        if entry.get("type") != "blob":
            continue
        path = str(entry.get("path", ""))
        size = int(entry.get("size", 0) or 0)
        if size <= 0 or size > max_file_bytes:
            continue
        lowered = path.lower()
        ext = os.path.splitext(lowered)[1]
        if lowered.endswith("dockerfile"):
            ext = ".dockerfile"
        if ext not in CODE_EXTENSIONS and "/" in lowered:
            continue
        category = classify_candidate_path(path)
        score = score_candidate_path(path, query_tokens, review_profile)
        if lowered in preferred_lower or any(lowered.endswith("/" + pref) for pref in preferred_lower):
            score += 120
        is_must_have = any(pattern in lowered for pattern in MUST_HAVE_PATTERNS)
        candidates.append(
            {
                "score": score,
                "size": size,
                "path": path,
                "category": category,
                "must_have": is_must_have,
            }
        )

    candidates.sort(
        key=lambda item: (
            -sensitive_priority_score(str(item["path"])),
            -int(item["score"]),
            int(item["size"]),
            str(item["path"]),
        )
    )
    selected: list[str] = []
    total = 0
    selected_set: set[str] = set()

    # Phase 0: high-sensitivity paths (auth, docker, CI, settings, …).
    for item in candidates:
        if len(selected) >= max_files:
            break
        path = str(item["path"])
        if sensitive_priority_score(path) < GROUNDED_REVIEW_SENSITIVE_PRIORITY_THRESHOLD:
            continue
        size = int(item["size"])
        if path in selected_set:
            continue
        if int(item["score"]) < 0 or total + size > max_total_bytes:
            continue
        selected.append(path)
        selected_set.add(path)
        total += size

    # Phase 1: force key coverage files when available.
    for item in candidates:
        if len(selected) >= max_files:
            break
        if not item["must_have"]:
            continue
        path = str(item["path"])
        size = int(item["size"])
        if path in selected_set:
            continue
        if total + size > max_total_bytes:
            continue
        if int(item["score"]) < 0:
            continue
        selected.append(path)
        selected_set.add(path)
        total += size

    # Phase 2: ensure breadth across major categories.
    major_categories = ("app", "tests", "ci", "deploy", "docs")
    for category in major_categories:
        if len(selected) >= max_files:
            break
        for item in candidates:
            path = str(item["path"])
            size = int(item["size"])
            if path in selected_set or item["category"] != category:
                continue
            if int(item["score"]) < 0 or total + size > max_total_bytes:
                continue
            selected.append(path)
            selected_set.add(path)
            total += size
            break

    # Phase 3: highest score fill to budget.
    for item in candidates:
        score = int(item["score"])
        size = int(item["size"])
        path = str(item["path"])
        if score < 0:
            continue
        if len(selected) >= max_files:
            break
        if path in selected_set:
            continue
        if total + size > max_total_bytes:
            continue
        selected.append(path)
        selected_set.add(path)
        total += size
    return selected


class GitHubSnapshotFetcher:
    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "octo-spork-grounded-review",
            }
        )

    def _get_json(self, url: str) -> dict[str, Any]:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def _get_text(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.text

    def fetch_snapshot(self, owner: str, repo: str, query: str = "") -> dict[str, Any]:
        meta = self._get_json(f"https://api.github.com/repos/{owner}/{repo}")
        default_branch = meta.get("default_branch", "main")

        readme_content = ""
        try:
            readme_json = self._get_json(f"https://api.github.com/repos/{owner}/{repo}/readme")
            encoded = readme_json.get("content", "")
            if encoded:
                readme_content = base64.b64decode(encoded).decode("utf-8", errors="ignore")
        except Exception:
            readme_content = ""

        tree_json = self._get_json(
            f"https://api.github.com/repos/{owner}/{repo}/git/trees/{default_branch}?recursive=1"
        )
        tree_entries = tree_json.get("tree", [])
        tip_json = self._get_json(f"https://api.github.com/repos/{owner}/{repo}/commits/{default_branch}")
        revision_sha = str(tip_json.get("sha", "") or "").strip()[:40] or None
        selected_paths = select_candidate_files(tree_entries, query=query or f"{owner}/{repo}")
        tree_sizes = {
            str(entry.get("path", "")): int(entry.get("size", 0) or 0)
            for entry in tree_entries
            if entry.get("type") == "blob"
        }

        files: list[RepoFile] = []
        for path in selected_paths:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/{path}"
            try:
                content = self._get_text(raw_url)
            except Exception:
                continue
            if "\x00" in content:
                continue
            files.append(RepoFile(path=path, content=content[:8_000], size=len(content)))

        total_files = len(tree_sizes)
        total_bytes = sum(tree_sizes.values())
        selected_bytes = sum(tree_sizes.get(path, 0) for path in selected_paths)
        analyzed_bytes = sum(file_info.size for file_info in files)
        snapshot: dict[str, Any] = {
            "owner": owner,
            "repo": repo,
            "default_branch": default_branch,
            "description": meta.get("description", ""),
            "stars": meta.get("stargazers_count", 0),
            "forks": meta.get("forks_count", 0),
            "open_issues": meta.get("open_issues_count", 0),
            "readme": readme_content[:30_000],
            "files": files,
            "sources": [f"README.md"] + [f.path for f in files],
            "coverage": {
                "total_files": total_files,
                "total_bytes": total_bytes,
                "selected_files": len(selected_paths),
                "selected_bytes": selected_bytes,
                "analyzed_files": len(files),
                "analyzed_bytes": analyzed_bytes,
            },
        }
        snapshot["coverage"] = enrich_coverage_telemetry(
            snapshot, tree_entries, selected_paths, revision_sha=revision_sha
        )
        return snapshot


def discover_local_repo(repo_name: str) -> Path | None:
    roots = [
        os.getenv("GROUNDED_REPO_ROOT"),
        "/opt/workspace",
    ]
    for root in roots:
        if not root:
            continue
        root_path = Path(root)
        if not root_path.exists() or not root_path.is_dir():
            continue

        direct = root_path / repo_name
        if direct.is_dir():
            return direct

        # One-level scan only to avoid expensive recursive traversal.
        for child in root_path.iterdir():
            if child.is_dir() and child.name == repo_name:
                return child
    return None


def build_local_tree_entries(repo_path: Path) -> list[dict[str, Any]]:
    ignored_parts = {".git", "node_modules", "dist", "build", ".venv", "__pycache__"}
    tree_entries: list[dict[str, Any]] = []
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(repo_path)
        if any(part in ignored_parts for part in relative.parts):
            continue
        size = path.stat().st_size
        tree_entries.append({"type": "blob", "path": str(relative), "size": size})
    return tree_entries


def git_diff_paths(repo_path: Path, base: str, head: str) -> set[str]:
    """Paths changed between base and head (files that exist on disk)."""
    proc = _subprocess_run_git_traced(
        repo_path,
        ["diff", "--name-only", f"{base}...{head}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        proc = _subprocess_run_git_traced(
            repo_path,
            ["diff", "--name-only", base, head],
            capture_output=True,
            text=True,
            check=False,
        )
    out: set[str] = set()
    for line in proc.stdout.splitlines():
        p = line.strip().replace("\\", "/")
        if not p:
            continue
        candidate = repo_path / p
        if candidate.is_file():
            out.add(p)
    return out


def fetch_local_snapshot(repo_name: str, query: str = "") -> dict[str, Any] | None:
    repo_path = discover_local_repo(repo_name)
    if repo_path is None:
        return None

    readme_text = ""
    for candidate in ("README.md", "readme.md", "README.MD"):
        readme_path = repo_path / candidate
        if readme_path.exists():
            readme_text = readme_path.read_text(encoding="utf-8", errors="ignore")
            break

    tree_entries = build_local_tree_entries(repo_path)

    changed_paths = detect_recent_local_changes(repo_path)
    selected_paths = select_candidate_files(
        tree_entries,
        query=query or repo_name,
        preferred_paths=changed_paths,
    )
    tree_sizes = {
        str(entry.get("path", "")): int(entry.get("size", 0) or 0)
        for entry in tree_entries
        if entry.get("type") == "blob"
    }
    files: list[RepoFile] = []
    for rel in selected_paths:
        file_path = repo_path / rel
        if not file_path.exists():
            continue
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        files.append(RepoFile(path=rel, content=content[:8_000], size=len(content)))

    total_files = len(tree_sizes)
    total_bytes = sum(tree_sizes.values())
    selected_bytes = sum(tree_sizes.get(path, 0) for path in selected_paths)
    analyzed_bytes = sum(file_info.size for file_info in files)
    revision_sha = git_head_sha(repo_path)
    snapshot: dict[str, Any] = {
        "owner": "local",
        "repo": repo_name,
        "default_branch": "local-worktree",
        "description": f"Local repository snapshot from {repo_path}",
        "stars": 0,
        "forks": 0,
        "open_issues": 0,
        "readme": readme_text[:30_000],
        "files": files,
        "sources": [f"README.md"] + [f.path for f in files],
        "coverage": {
            "total_files": total_files,
            "total_bytes": total_bytes,
            "selected_files": len(selected_paths),
            "selected_bytes": selected_bytes,
            "analyzed_files": len(files),
            "analyzed_bytes": analyzed_bytes,
            "recent_change_hints": len(changed_paths),
        },
    }
    snapshot["coverage"] = enrich_coverage_telemetry(
        snapshot, tree_entries, selected_paths, revision_sha=revision_sha
    )
    snapshot["scan_root"] = str(repo_path.resolve())
    return snapshot


def fetch_local_diff_snapshot(repo_path: Path, query: str, base: str, head: str) -> dict[str, Any] | None:
    """Prioritize files touched in git diff(base..head) plus normal triage guardrails."""
    if not (repo_path / ".git").is_dir():
        return None

    readme_text = ""
    for candidate in ("README.md", "readme.md", "README.MD"):
        readme_path = repo_path / candidate
        if readme_path.exists():
            readme_text = readme_path.read_text(encoding="utf-8", errors="ignore")
            break

    diff_paths = git_diff_paths(repo_path, base, head)
    tree_entries = build_local_tree_entries(repo_path)
    merged_preferred = diff_paths | detect_recent_local_changes(repo_path)

    selected_paths = select_candidate_files(
        tree_entries,
        query=query or f"diff {base}..{head}",
        preferred_paths=merged_preferred,
    )
    tree_sizes = {
        str(entry.get("path", "")): int(entry.get("size", 0) or 0)
        for entry in tree_entries
        if entry.get("type") == "blob"
    }
    files: list[RepoFile] = []
    for rel in selected_paths:
        file_path = repo_path / rel
        if not file_path.exists():
            continue
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        files.append(RepoFile(path=rel, content=content[:8_000], size=len(content)))

    total_files = len(tree_sizes)
    total_bytes = sum(tree_sizes.values())
    selected_bytes = sum(tree_sizes.get(path, 0) for path in selected_paths)
    analyzed_bytes = sum(file_info.size for file_info in files)
    repo_label = repo_path.name
    head_sha = git_resolve_ref(repo_path, head) or git_head_sha(repo_path)
    snapshot: dict[str, Any] = {
        "owner": "local",
        "repo": repo_label,
        "default_branch": f"diff {base}...{head}",
        "description": f"Diff-focused snapshot from {repo_path}",
        "stars": 0,
        "forks": 0,
        "open_issues": 0,
        "readme": readme_text[:30_000],
        "files": files,
        "sources": [f"README.md"] + [f.path for f in files],
        "coverage": {
            "total_files": total_files,
            "total_bytes": total_bytes,
            "selected_files": len(selected_paths),
            "selected_bytes": selected_bytes,
            "analyzed_files": len(files),
            "analyzed_bytes": analyzed_bytes,
            "diff_paths_count": len(diff_paths),
            "mode": "diff",
            "diff_base": base,
            "diff_head": head,
        },
    }
    snapshot["coverage"] = enrich_coverage_telemetry(
        snapshot, tree_entries, selected_paths, revision_sha=head_sha
    )
    snapshot["coverage"]["diff_head_resolved"] = head_sha
    snapshot["scan_root"] = str(repo_path.resolve())
    return snapshot


def format_diff_preview_markdown(snapshot: dict[str, Any], base: str, head: str) -> str:
    cov = snapshot.get("coverage", {})
    lines = [
        "### Octo-spork diff preview (no LLM)",
        "",
        f"- **Compare:** `{base}` … `{head}`",
        f"- **Diff paths (existing files):** {cov.get('diff_paths_count', 0)}",
        f"- **Selected for triage:** {cov.get('selected_files', 0)} files",
        "",
        "**Selected paths:**",
        "",
    ]
    for p in snapshot.get("sources", [])[1:16]:
        lines.append(f"- `{p}`")
    if len(snapshot.get("sources", [])) > 16:
        lines.append("- _(truncated in preview)_")
    lines.extend(
        [
            "",
            "Run a full local review with Ollama:",
            "",
            "```bash",
            f"python -m local_ai_stack review-diff --repo . --base {base} --head {head}",
            "```",
        ]
    )
    return "\n".join(lines)


def run_grounded_review_from_snapshot(
    query: str,
    model: str,
    ollama_base_url: str,
    snapshot: dict[str, Any],
    *,
    cache_owner: str | None = None,
    cache_repo: str | None = None,
    use_answer_cache: bool = True,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared pipeline: map pass + synthesis. Optional answer cache for remote URL flow."""
    from sources.tools.searx_query_policy import strict_repo_review_session

    owner_key = str(snapshot.get("owner") or "").strip() or None
    repo_key = str(snapshot.get("repo") or "").strip() or None
    with strict_repo_review_session(owner_key, repo_key):
        out = _run_grounded_review_from_snapshot_impl(
            query,
            model,
            ollama_base_url,
            snapshot,
            cache_owner=cache_owner,
            cache_repo=cache_repo,
            use_answer_cache=use_answer_cache,
            metrics=metrics,
        )
    try:
        if out.get("success"):
            from observability.memory_vector_store import index_successful_grounded_review

            index_successful_grounded_review(
                query=query,
                model=model,
                ollama_base_url=ollama_base_url,
                result=out,
            )
    except ImportError:
        pass
    except Exception:
        pass
    try:
        from observability.performance_tracker import clear_performance_session

        clear_performance_session()
    except ImportError:
        pass
    return out


def _module_path_prefix(path: str, depth: int) -> str:
    segments = [p for p in str(path).replace("\\", "/").split("/") if p]
    if not segments:
        return "(root)"
    take = min(max(1, depth), len(segments))
    return "/".join(segments[:take])


def _group_repo_files_by_module(
    files: list[RepoFile], depth: int
) -> list[tuple[str, list[RepoFile]]]:
    buckets: dict[str, list[RepoFile]] = {}
    for rf in files:
        key = _module_path_prefix(rf.path, depth)
        buckets.setdefault(key, []).append(rf)
    return sorted(buckets.items(), key=lambda kv: kv[0])


def _split_module_files_by_token_budget(
    label: str,
    files: list[RepoFile],
    max_evidence_tokens: int,
) -> list[tuple[str, list[RepoFile]]]:
    """Sub-split a module when raw file text still exceeds a token budget (rough chars÷ratio)."""
    if not files:
        return []
    batches: list[list[RepoFile]] = []
    cur: list[RepoFile] = []
    cur_tok = 0
    for rf in sorted(files, key=lambda x: x.path):
        t = estimate_token_units(rf.content or "")
        if cur and cur_tok + t > max_evidence_tokens:
            batches.append(cur)
            cur = []
            cur_tok = 0
        cur.append(rf)
        cur_tok += t
    if cur:
        batches.append(cur)
    if len(batches) == 1:
        return [(label, batches[0])]
    out: list[tuple[str, list[RepoFile]]] = []
    for i, batch in enumerate(batches):
        out.append((f"{label} (part {i + 1}/{len(batches)})", batch))
    return out


def _diff_manager_chunk_snapshot(
    parent: dict[str, Any], module_files: list[RepoFile], module_label: str
) -> dict[str, Any]:
    """Shallow+deep copy parent snapshot; replace file list for one module chunk (shared scanner blocks)."""
    snap: dict[str, Any] = copy.deepcopy(parent)
    snap["files"] = module_files
    snap["sources"] = ["README.md"] + [r.path for r in module_files]
    cov = snap.get("coverage")
    if isinstance(cov, dict):
        cov["diff_manager_module"] = module_label
        cov["analyzed_files"] = len(module_files)
        cov["analyzed_bytes"] = int(sum(r.size for r in module_files))
        evidence_chars = len(str(snap.get("readme") or "")) + sum(
            len(r.content or "") for r in module_files
        )
        cov["approx_input_tokens_hint"] = max(1, int(evidence_chars // 4))
    return snap


def _merge_diff_manager_chunk_bodies(pairs: list[tuple[str, str]]) -> str:
    lines = [
        "## Combined review (Diff Manager — per-module chunks)",
        "",
        "_Single-pass context would exceed the configured token budget; each section is one LLM pass on a path prefix, then merged._",
        "",
    ]
    for label, body in pairs:
        lines.append(f"### Module: `{label}`")
        lines.append("")
        lines.append(str(body).strip())
        lines.append("")
    return "\n".join(lines).strip()


def _run_diff_manager_consolidating_pass(
    *,
    original_query: str,
    merged_markdown: str,
    model: str,
    ollama_base_url: str,
    metrics: dict[str, Any] | None,
) -> str:
    """Optional second LLM pass: unify overlapping findings from chunked reviews."""
    merge_prompt = f"""You merge partial code review sections into one PR review (markdown).

Review request:
{original_query}

Below are per-module findings for the same change. Output ONE consolidated markdown document:
- Executive summary (3–6 bullets).
- Findings grouped by severity (Critical / High / Medium / Low / Informational).
- De-duplicate overlapping items; keep file paths and code references where present.
- If sections disagree, call that out briefly.

--- Partial findings ---
{merged_markdown}
"""
    try:
        return run_ollama_review(
            merge_prompt,
            model,
            ollama_base_url,
            num_ctx=min(8192, GROUNDED_REVIEW_NUM_CTX),
            timeout_seconds=180,
            metrics=metrics,
        )
    except Exception as exc:
        return (
            f"_Consolidating merge pass failed (`{exc}`). Showing raw combined sections._\n\n{merged_markdown}"
        )


def _run_chunked_grounded_synthesis(
    query: str,
    model: str,
    ollama_base_url: str,
    parent_snapshot: dict[str, Any],
    map_digest: str,
    map_status: str,
    *,
    cache_owner: str | None,
    cache_repo: str | None,
    use_answer_cache: bool,
    metrics: dict[str, Any] | None,
    revision_sha: str | None,
) -> dict[str, Any]:
    """Run one LLM pass per path-prefix module, then merge (optional consolidating pass)."""
    files = list(parent_snapshot.get("files") or [])
    if not files:
        return {
            "success": False,
            "answer": "Diff Manager: no files in snapshot to chunk.",
            "sources": list(parent_snapshot.get("sources") or []),
        }

    per_module_groups = _group_repo_files_by_module(files, GROUNDED_DIFF_MODULE_DEPTH)
    per_chunk_evidence_tok = max(2048, GROUNDED_DIFF_CHUNK_PROMPT_TOKEN_THRESHOLD // 2)
    expanded: list[tuple[str, list[RepoFile]]] = []
    for label, flist in per_module_groups:
        tok_est = sum(estimate_token_units(r.content or "") for r in flist)
        if tok_est > per_chunk_evidence_tok:
            expanded.extend(
                _split_module_files_by_token_budget(label, flist, per_chunk_evidence_tok)
            )
        else:
            expanded.append((label, flist))

    parent_scope = build_scope_note(parent_snapshot, map_status)
    chunk_rows: list[tuple[str, str]] = []
    for module_label, mod_files in expanded:
        chunk_snap = _diff_manager_chunk_snapshot(parent_snapshot, mod_files, module_label)
        chunk_query = (
            f"{query}\n\n"
            f"(**Chunk scope:** analyze only files under module `{module_label}` in this pass. "
            "Other paths are covered in separate chunks and should be ignored here.)"
        )
        try:
            _apply_snapshot_compression_if_needed(chunk_snap)
        except Exception:
            pass
        chunk_prompt = build_grounded_review_prompt(chunk_query, chunk_snap, map_digest=map_digest)
        try:
            raw_answer = run_ollama_review(
                chunk_prompt,
                model,
                ollama_base_url,
                num_ctx=GROUNDED_REVIEW_NUM_CTX_TWO_PASS if map_digest else GROUNDED_REVIEW_NUM_CTX,
                timeout_seconds=210,
                metrics=metrics,
            )
        except Exception as exc:
            raw_answer = f"_Synthesis failed for chunk `{module_label}`: {exc}_"
        chunk_rows.append((module_label, str(raw_answer).strip()))

    merged = _merge_diff_manager_chunk_bodies(chunk_rows)
    if GROUNDED_DIFF_MERGE_SYNTHESIS and len(chunk_rows) > 1:
        merged = _run_diff_manager_consolidating_pass(
            original_query=query,
            merged_markdown=merged,
            model=model,
            ollama_base_url=ollama_base_url,
            metrics=metrics,
        )
    final_answer = f"{parent_scope}\n\n{merged}"
    cache_payload = {
        "success": True,
        "answer": final_answer,
        "sources": list(parent_snapshot.get("sources") or []),
    }
    if use_answer_cache and cache_owner and cache_repo:
        set_cached_answer(
            cache_owner, cache_repo, query, model, cache_payload, revision_sha
        )
    out: dict[str, Any] = {
        "success": True,
        "answer": final_answer,
        "sources": list(parent_snapshot.get("sources") or []),
        "snapshot": parent_snapshot,
        "diff_manager": {
            "chunks": len(chunk_rows),
            "module_depth": GROUNDED_DIFF_MODULE_DEPTH,
            "merge_synthesis": bool(GROUNDED_DIFF_MERGE_SYNTHESIS and len(chunk_rows) > 1),
        },
    }
    if metrics is not None:
        out["benchmark_metrics"] = metrics
    return out


def _persist_review_followup_snapshot(
    snapshot: dict[str, Any],
    query: str,
    answer_markdown: str,
) -> None:
    """Save prompt + answer + scanner excerpts for ``python -m local_ai_stack chat`` follow-ups."""
    try:
        from observability.prompt_capture import get_last_prompt_snapshot
        from observability.review_session_store import persist_last_review_session

        snap_pc = get_last_prompt_snapshot() or {}
        cov = snapshot.get("coverage") if isinstance(snapshot.get("coverage"), dict) else {}
        rev = str(snapshot.get("revision_sha") or cov.get("revision_sha") or "")[:40]
        meta = {
            "owner": snapshot.get("owner"),
            "repo": snapshot.get("repo"),
            "revision_sha": rev,
            "default_branch": snapshot.get("default_branch"),
        }
        extras = {
            "security_context_block": str(snapshot.get("security_context_block") or "")[:200_000],
            "codeql_evidence_block": str(snapshot.get("codeql_evidence_block") or "")[:200_000],
            "dependency_audit_block": str(snapshot.get("dependency_audit_block") or "")[:150_000],
        }
        repo_root = None
        sr = snapshot.get("scan_root")
        if sr:
            try:
                repo_root = Path(str(sr)).expanduser().resolve()
            except Exception:
                repo_root = None
        persist_last_review_session(
            {
                "version": 1,
                "query": str(query),
                "answer": answer_markdown,
                "prompt": str(snap_pc.get("prompt") or ""),
                "model": str(snap_pc.get("model") or ""),
                "ollama_base_url": str(snap_pc.get("ollama_base_url") or ""),
                "meta": meta,
                "extras": extras,
            },
            repo_root=repo_root,
        )
    except ImportError:
        pass
    except Exception:
        pass


def _run_grounded_review_from_snapshot_impl(
    query: str,
    model: str,
    ollama_base_url: str,
    snapshot: dict[str, Any],
    *,
    cache_owner: str | None = None,
    cache_repo: str | None = None,
    use_answer_cache: bool = True,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Implementation for :func:`run_grounded_review_from_snapshot` (strict SearXNG scope applied by caller)."""
    rev_raw = snapshot.get("revision_sha") or (snapshot.get("coverage") or {}).get("revision_sha")
    revision_sha = str(rev_raw).strip()[:40] if rev_raw else None

    if metrics is not None:
        metrics.setdefault("llm_seconds", 0.0)
        metrics.setdefault("scan_seconds", 0.0)
        metrics.setdefault("prompt_tokens_total", 0)
        metrics.setdefault("completion_tokens_total", 0)

    if use_answer_cache and cache_owner and cache_repo:
        cached_answer = get_cached_answer(cache_owner, cache_repo, query, model, revision_sha)
        if cached_answer is not None:
            return {
                "success": bool(cached_answer.get("success", True)),
                "answer": str(cached_answer.get("answer", "")),
                "sources": list(cached_answer.get("sources", [])),
            }

    if not snapshot.get("readme") and not snapshot.get("files"):
        empty: dict[str, Any] = {
            "success": False,
            "answer": "Repository snapshot is empty; unable to produce a grounded review.",
            "sources": [],
        }
        if metrics is not None:
            empty["benchmark_metrics"] = metrics
        return empty

    try:
        from observability.performance_tracker import bind_evidence_manifest

        bind_evidence_manifest(snapshot)
    except ImportError:
        pass

    try:
        _invoke_attach_sovereign_intel(snapshot)
    except Exception:
        pass

    try:
        _invoke_context_governor(snapshot, ollama_base_url)
    except Exception:
        pass

    map_digest = ""
    map_status = "disabled"
    if GROUNDED_REVIEW_ENABLE_TWO_PASS:
        map_status = "not_needed"
        if should_use_two_pass_review(query, snapshot.get("files", [])):
            map_digest, map_status = run_map_review(
                query, snapshot, model, ollama_base_url, metrics=metrics
            )

    _scan_t0 = time.perf_counter() if metrics is not None else None
    try:
        attach_trivy_security_context(snapshot)
    except Exception as exc:
        snapshot["security_context_block"] = (
            "### Security Context (Trivy)\n\n"
            f"Unexpected error attaching Trivy context: `{exc}`\n\n"
        )
        snapshot["trivy_scan_error"] = str(exc)

    try:
        attach_codeql_evidence(snapshot)
    except Exception as exc:
        snapshot["codeql_evidence_block"] = (
            "### Evidence Receipts (CodeQL)\n\n"
            f"Unexpected error attaching CodeQL evidence: `{exc}`\n\n"
        )
        snapshot["codeql_error"] = str(exc)
    if _scan_t0 is not None and metrics is not None:
        metrics["scan_seconds"] = time.perf_counter() - _scan_t0

    try:
        attach_dependency_audit_context(snapshot)
    except Exception as exc:
        snapshot["dependency_audit_block"] = (
            "### Dependency audit\n\n"
            f"Unexpected error attaching dependency audit context: `{exc}`\n\n"
        )
        snapshot["dependency_audit_error"] = str(exc)

    try:
        attach_knowledge_sync_context(snapshot)
    except Exception:
        pass

    try:
        _invoke_attach_architecture_map(snapshot)
    except Exception as exc:
        snapshot["architecture_map_block"] = (
            "### Architecture map\n\n"
            f"Unexpected error attaching architecture map: `{exc}`\n\n"
        )

    try:
        _apply_snapshot_compression_if_needed(snapshot)
    except Exception as exc:
        snapshot["_compression_trim_error"] = str(exc)

    try:
        from observability.memory_vector_store import attach_similar_historical_findings

        attach_similar_historical_findings(query, snapshot, ollama_base_url)
    except ImportError:
        pass
    except Exception:
        pass

    try:
        from github_bot.correction_ledger import attach_lessons_learned_to_snapshot

        attach_lessons_learned_to_snapshot(query, snapshot, ollama_base_url)
    except ImportError:
        pass
    except Exception:
        pass

    try:
        _invoke_attach_repo_graph(snapshot)
    except Exception:
        pass

    prompt = build_grounded_review_prompt(query, snapshot, map_digest=map_digest)
    prompt_toks = estimate_token_units(prompt)
    files_list = list(snapshot.get("files") or [])
    if (
        GROUNDED_DIFF_CHUNKING_ENABLED
        and prompt_toks > GROUNDED_DIFF_CHUNK_PROMPT_TOKEN_THRESHOLD
        and len(files_list) > 1
    ):
        return _run_chunked_grounded_synthesis(
            query,
            model,
            ollama_base_url,
            snapshot,
            map_digest,
            map_status,
            cache_owner=cache_owner,
            cache_repo=cache_repo,
            use_answer_cache=use_answer_cache,
            metrics=metrics,
            revision_sha=revision_sha,
        )

    cache_model = model
    try:
        try:
            from observability.peer_review import (
                PEER_GATE_SUFFIX,
                build_audit_prompt,
                cache_model_label,
                parse_peer_gate,
                peer_review_enabled as _peer_review_enabled,
                resolve_fast_model,
            )
        except ImportError:

            def _peer_review_enabled() -> bool:
                return False

        if _peer_review_enabled():
            fast_m = resolve_fast_model()
            gate_prompt = prompt + PEER_GATE_SUFFIX
            fast_raw = run_ollama_review(
                gate_prompt,
                fast_m,
                ollama_base_url,
                num_ctx=GROUNDED_REVIEW_NUM_CTX_TWO_PASS if map_digest else GROUNDED_REVIEW_NUM_CTX,
                timeout_seconds=int(os.environ.get("OCTO_PEER_FAST_TIMEOUT_SEC", "210")),
                metrics=metrics,
            )
            flag, peer_body = parse_peer_gate(fast_raw)
            pr_meta: dict[str, Any] = {
                "enabled": True,
                "fast_model": fast_m,
                "audit_model": model,
                "gate_flag": flag,
            }
            if flag is False:
                answer = peer_body
                pr_meta["audit_invoked"] = False
            else:
                audit_prompt = build_audit_prompt(
                    query=query,
                    fast_review_body=peer_body,
                    snapshot=snapshot,
                    map_digest=map_digest,
                )
                answer = run_ollama_review(
                    audit_prompt,
                    model,
                    ollama_base_url,
                    num_ctx=GROUNDED_REVIEW_NUM_CTX_TWO_PASS if map_digest else GROUNDED_REVIEW_NUM_CTX,
                    timeout_seconds=int(os.environ.get("OCTO_PEER_AUDIT_TIMEOUT_SEC", "420")),
                    metrics=metrics,
                )
                pr_meta["audit_invoked"] = True
            snapshot["peer_review"] = pr_meta
            cache_model = cache_model_label(model, fast_m, True)
        else:
            answer = run_ollama_review(
                prompt,
                model,
                ollama_base_url,
                num_ctx=GROUNDED_REVIEW_NUM_CTX_TWO_PASS if map_digest else GROUNDED_REVIEW_NUM_CTX,
                timeout_seconds=210,
                metrics=metrics,
            )
    except Exception as exc:
        err: dict[str, Any] = {
            "success": False,
            "answer": f"Grounded review generation failed: {exc}",
            "sources": snapshot.get("sources", []),
        }
        if metrics is not None:
            err["benchmark_metrics"] = metrics
        return err
    answer_with_scope = f"{build_scope_note(snapshot, map_status)}\n\n{answer}"
    try:
        _persist_review_followup_snapshot(snapshot, query, answer_with_scope)
    except Exception:
        pass
    cache_payload = {"success": True, "answer": answer_with_scope, "sources": snapshot["sources"]}
    if use_answer_cache and cache_owner and cache_repo:
        set_cached_answer(cache_owner, cache_repo, query, cache_model, cache_payload, revision_sha)
    ok: dict[str, Any] = {
        "success": True,
        "answer": answer_with_scope,
        "sources": snapshot["sources"],
        "snapshot": snapshot,
    }
    if metrics is not None:
        ok["benchmark_metrics"] = metrics
    return ok


def grounded_local_diff_review(
    query: str,
    model: str,
    ollama_base_url: str,
    repo_path: Path,
    base: str,
    head: str,
    *,
    use_answer_cache: bool = True,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _snap_t0 = time.perf_counter() if metrics is not None else None
    snapshot = fetch_local_diff_snapshot(repo_path, query, base, head)
    if metrics is not None and _snap_t0 is not None:
        metrics["snapshot_seconds"] = time.perf_counter() - _snap_t0
    if snapshot is None:
        err: dict[str, Any] = {
            "success": False,
            "answer": "Not a git repository or diff could not be computed.",
            "sources": [],
        }
        if metrics is not None:
            err["benchmark_metrics"] = metrics
        return err
    cache_repo = f"{repo_path.resolve()}|{base}|{head}"
    return run_grounded_review_from_snapshot(
        query,
        model,
        ollama_base_url,
        snapshot,
        cache_owner="local-diff",
        cache_repo=cache_repo,
        use_answer_cache=use_answer_cache,
        metrics=metrics,
    )


def build_grounded_review_prompt(query: str, snapshot: dict[str, Any], map_digest: str = "") -> str:
    ranked_evidence = render_context_ranked_evidence(snapshot)

    strict_block = ""
    if GROUNDED_REVIEW_STRICT_COVERAGE:
        strict_block = """
Severity discipline (strict coverage mode is ON via GROUNDED_REVIEW_STRICT_COVERAGE):
- Label a finding Critical only when the cited file excerpts clearly support that severity.
- If "selected_files_by_category" shows thin coverage in app/tests/deploy/ci, prefer Medium/Low and state the sampling gap explicitly.
"""

    trivy_block = str(snapshot.get("security_context_block") or "").strip()
    codeql_block = str(snapshot.get("codeql_evidence_block") or "").strip()
    dep_audit_block = str(snapshot.get("dependency_audit_block") or "").strip()
    arch_block = str(snapshot.get("architecture_map_block") or "").strip()
    static_blocks: list[str] = [b for b in (trivy_block, codeql_block, dep_audit_block, arch_block) if b]
    static_insert = "\n\n".join(static_blocks) + "\n\n" if static_blocks else ""

    vec_mem = str(snapshot.get("vector_memory_similar_block") or "").strip()
    vec_part = ""
    if vec_mem:
        vec_part = (
            "\n\nSimilar historical findings from prior successful grounded reviews "
            "(local vector memory / ChromaDB; use only as triage hints — verify against current evidence):\n"
            f"{vec_mem}\n"
        )

    lessons_raw = str(snapshot.get("correction_ledger_lessons_block") or "").strip()
    lessons_part = ""
    if lessons_raw:
        lessons_part = (
            "\n\nDeveloper corrections from prior AI PR comments "
            "(Correction Ledger / negative examples — honor unless current evidence contradicts):\n"
            f"{lessons_raw}\n"
        )

    topo_raw = str(snapshot.get("repo_graph_topology_block") or "").strip()
    topo_insert = (topo_raw + "\n\n") if topo_raw else ""

    style_guide_insert = ""
    try:
        from github_bot.style_prefs import format_style_guide_block_for_review

        _sg = format_style_guide_block_for_review()
        if _sg:
            style_guide_insert = "\n\n" + _sg + "\n\n"
    except ImportError:
        pass

    domain_kb_insert = ""
    try:
        from observability.knowledge_base import format_domain_constraints_block_for_review

        _kb = format_domain_constraints_block_for_review()
        if _kb:
            domain_kb_insert = "\n\n" + _kb + "\n\n"
    except ImportError:
        pass

    ks_raw = str(snapshot.get("knowledge_sync_block") or "").strip()
    ks_insert = ""
    if ks_raw:
        ks_insert = (
            "\n\nProject-local coding conventions were extracted from `CLAUDE.md` when present "
            "(KnowledgeSync). Honor them unless contradicted by stronger evidence above:\n\n"
            f"{ks_raw}\n"
        )

    sov_raw = str(snapshot.get("sovereign_intel_block") or "").strip()
    sov_insert = (sov_raw + "\n\n") if sov_raw else ""

    _fallback_id = (
        "### Operator identity\n\n"
        "You are Octo-spork, assisting a solo developer who values local-first compute "
        "and fraud-infra security.\n\n"
    )
    identity_insert = _fallback_id
    try:
        from github_bot.user_summary_identity import (
            format_user_identity_block_for_review,
            user_summary_identity_enabled,
        )

        _sr = snapshot.get("scan_root")
        _repo_root = Path(str(_sr)) if _sr else None
        if user_summary_identity_enabled():
            _blk = format_user_identity_block_for_review(_repo_root).strip()
            if _blk:
                identity_insert = _blk + "\n\n"
        else:
            identity_insert = _fallback_id
    except ImportError:
        identity_insert = _fallback_id

    return f"""
{identity_insert}You are acting as **Octo-spork** — senior engineer and QA lead for this review.
You must review the repository using only the supplied evidence.
If evidence is insufficient, explicitly say so and do not invent facts.
{domain_kb_insert}{ks_insert}
Important: This is LLM-assisted triage over a bounded, heuristic-selected subset of files (see coverage metadata).
It is not a deterministic exhaustive audit; identical runs may differ slightly even at low temperature.
{static_insert}{style_guide_insert}{sov_insert}{EVIDENCE_CITATION_CONTRACT}
User request:
{query}{vec_part}{lessons_part}

If available, use this preliminary per-file analysis digest as additional evidence:
{map_digest or "(none)"}

Repository metadata:
- owner/repo: {snapshot["owner"]}/{snapshot["repo"]}
- default branch: {snapshot["default_branch"]}
- description: {snapshot["description"]}
- stars: {snapshot["stars"]}
- forks: {snapshot["forks"]}
- open issues: {snapshot["open_issues"]}

Coverage metadata:
{json.dumps(snapshot.get("coverage", {}), indent=2)}
{strict_block}
{topo_insert}README:
{snapshot["readme"]}

### Ranked repository evidence (Context Ranker)
{ranked_evidence}

Return markdown with these sections:
1) System summary (grounded)
2) Severity-ranked findings (Critical / High / Medium / Low) — honor **Sovereign Intelligence** High-Priority patterns when that section is present (cross-repo credential fleet context).
3) Architecture / coupling commentary — use the **Repo topology (tree-sitter)** import summary and the **Architecture map** Mermaid diagram when discussing tangled dependencies or layering violations (prefix **(Repo topology)** / **(Architecture map)** when appropriate).
4) Dependency risk summary — call out **DIRECT + CVE** rows from the Dependency audit table when present (prefix **(Dependency audit)**).
5) Hardening plan (short-term, medium-term)
6) QA strategy (focus on regression prevention)
7) Top 5 concrete next actions
8) Confidence and evidence gaps
9) Coverage summary
10) **Suggested `CLAUDE.md` updates (optional)** — if you notice a **new recurring cross-cutting pattern**
    (e.g. the same class of issue in multiple files) that is **not** already reflected in the KnowledgeSync
    / `CLAUDE.md` rules above, propose one or more concrete bullet lines the maintainer could add to
    `CLAUDE.md` under a *Coding rules* or *Conventions* heading. If nothing new, write
    "_No new CLAUDE.md updates suggested._"

When referencing repository files or code, cite using the exact markdown URL from the matching `source://[...](...)` line above.
For each Critical/High finding tied to code, include the mandatory `source://` citation URL from the evidence section.
Be explicit that this review is based on a prioritized, token-budgeted subset of repository files.
"""


def _ollama_generate_payload_impl(
    prompt: str,
    model: str,
    ollama_base_url: str,
    *,
    num_ctx: int = 16384,
    temperature: float = 0.1,
    timeout_seconds: int = 420,
) -> tuple[str, dict[str, Any]]:
    """Call Ollama ``/api/generate`` (non-streaming) and return response text plus usage metadata."""
    try:
        from observability.tui_bridge import check_agent_stop_or_raise

        check_agent_stop_or_raise()
    except ImportError:
        pass

    endpoint = ollama_base_url.rstrip("/") + "/api/generate"
    response = requests.post(
        endpoint,
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    text = str(payload.get("response", "")).strip()
    meta = {
        "prompt_eval_count": payload.get("prompt_eval_count"),
        "eval_count": payload.get("eval_count"),
        "total_duration": payload.get("total_duration"),
        "load_duration": payload.get("load_duration"),
        "prompt_eval_duration": payload.get("prompt_eval_duration"),
        "eval_duration": payload.get("eval_duration"),
    }
    return text, meta


def _ollama_generate_payload(
    prompt: str,
    model: str,
    ollama_base_url: str,
    *,
    num_ctx: int = 16384,
    temperature: float = 0.1,
    timeout_seconds: int = 420,
) -> tuple[str, dict[str, Any]]:
    """Call Ollama ``/api/generate`` with optional OpenTelemetry span (octo-spork ``observability``)."""
    try:
        from observability.tracer import trace_llm_call
    except ImportError:
        return _ollama_generate_payload_impl(
            prompt,
            model,
            ollama_base_url,
            num_ctx=num_ctx,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )

    return trace_llm_call(
        model=model,
        provider="ollama",
        ollama_base_url=ollama_base_url,
        prompt=prompt,
        call=lambda: _ollama_generate_payload_impl(
            prompt,
            model,
            ollama_base_url,
            num_ctx=num_ctx,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        ),
    )


def run_ollama_review(
    prompt: str,
    model: str,
    ollama_base_url: str,
    *,
    num_ctx: int = 16384,
    temperature: float = 0.1,
    timeout_seconds: int = 420,
    metrics: dict[str, Any] | None = None,
) -> str:
    perf_cm: Any = contextlib.nullcontext()
    try:
        from observability.performance_tracker import track_model_execution

        perf_cm = track_model_execution(model=model, phase="ollama.review")
    except ImportError:
        pass
    llm_prompt = prompt
    priv_map: dict[str, str] = {}
    try:
        from observability.privacy_filter import redact_for_llm

        llm_prompt, priv_map = redact_for_llm(prompt)
    except ImportError:
        pass
    degraded = (os.environ.get("OCTO_DEGRADED_TASK_INSTRUCTION") or "").strip()
    if degraded:
        llm_prompt = f"{degraded}\n\n{llm_prompt}"
    try:
        from observability.prompt_capture import record_ollama_review_prompt

        record_ollama_review_prompt(
            llm_prompt,
            model=model,
            ollama_base_url=ollama_base_url,
            num_ctx=num_ctx,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )
    except ImportError:
        pass

    t0 = time.perf_counter()
    swap_cm: Any = contextlib.nullcontext()
    try:
        from infra.smart_swapper import smart_swap_context

        swap_cm = smart_swap_context(model, ollama_base_url, llm_prompt)
    except ImportError:
        pass
    with swap_cm:
        with perf_cm:
            text, meta = _ollama_generate_payload(
                llm_prompt,
                model,
                ollama_base_url,
                num_ctx=num_ctx,
                temperature=temperature,
                timeout_seconds=timeout_seconds,
            )
    elapsed = time.perf_counter() - t0
    if metrics is not None:
        metrics["llm_seconds"] = float(metrics.get("llm_seconds", 0.0)) + elapsed
        pe = meta.get("prompt_eval_count")
        ev = meta.get("eval_count")
        if pe is not None:
            metrics["prompt_tokens_total"] = int(metrics.get("prompt_tokens_total", 0)) + int(pe)
        if ev is not None:
            metrics["completion_tokens_total"] = int(metrics.get("completion_tokens_total", 0)) + int(ev)
    if priv_map:
        try:
            from observability.privacy_filter import unredact_response

            text = unredact_response(text, priv_map)
        except ImportError:
            pass
    return text


def should_use_two_pass_review(query: str, files: Iterable[Any]) -> bool:
    file_count = len(list(files))
    lowered = query.lower()
    deep_request = any(
        token in lowered
        for token in (
            "critical",
            "architecture",
            "hardening",
            "security",
            "regression",
            "qa",
            "audit",
        )
    )
    if file_count >= 12:
        return True
    return deep_request and file_count >= 10


def build_map_review_prompt(query: str, snapshot: dict[str, Any], files: list[RepoFile]) -> str:
    ranked = rank_evidence_files(list(files))
    file_chunks: list[str] = []
    for file_info in ranked:
        uri = build_source_uri_markdown(snapshot, file_info.path, line_start=1)
        file_chunks.append(
            f"{uri}\n"
            f"CONTENT_START\n{file_info.content[:4_000]}\nCONTENT_END\n"
        )
    return f"""
You are a senior software engineer and QA reviewer.
Analyze each file independently and produce compact JSON only.
Each excerpt is prefixed with `source://[label](URL)` — preserve those URLs when you refer to a file in prose.

User request:
{query}

Repository:
- owner/repo: {snapshot["owner"]}/{snapshot["repo"]}
- branch: {snapshot["default_branch"]}

Files to analyze:
{chr(10).join(file_chunks)}

Return strict JSON object:
{{
  "file_findings": [
    {{
      "path": "string",
      "critical_risks": ["..."],
      "high_risks": ["..."],
      "regression_notes": ["..."],
      "hardening_actions": ["..."]
    }}
  ],
  "global_patterns": ["..."]
}}
"""


def run_map_review(
    query: str,
    snapshot: dict[str, Any],
    model: str,
    ollama_base_url: str,
    *,
    metrics: dict[str, Any] | None = None,
) -> tuple[str, str]:
    files = list(snapshot.get("files", []))
    if not files:
        return "", "skipped_no_files"
    map_files = files[:8]
    map_prompt = build_map_review_prompt(query, snapshot, map_files)
    try:
        response_text = run_ollama_review(
            map_prompt,
            model,
            ollama_base_url,
            num_ctx=8192,
            temperature=0.0,
            timeout_seconds=240,
            metrics=metrics,
        )
        parsed = json.loads(response_text)
        return json.dumps(parsed, indent=2), "used"
    except json.JSONDecodeError:
        return "", "fallback_map_json_parse_error"
    except Exception:
        return "", "fallback_map_runtime_error"


def build_scope_note(snapshot: dict[str, Any], map_status: str) -> str:
    coverage = snapshot.get("coverage", {})
    total_files = int(coverage.get("total_files", 0) or 0)
    analyzed_files = int(coverage.get("analyzed_files", 0) or 0)
    total_bytes = int(coverage.get("total_bytes", 0) or 0)
    analyzed_bytes = int(coverage.get("analyzed_bytes", 0) or 0)
    file_pct = (analyzed_files / total_files * 100.0) if total_files else 0.0
    byte_pct = (analyzed_bytes / total_bytes * 100.0) if total_bytes else 0.0
    approx_tok = int(coverage.get("approx_input_tokens_hint", 0) or 0)
    rev = str(coverage.get("revision_sha") or snapshot.get("revision_sha") or "").strip()
    map_line = f"Two-pass map status: {map_status}."
    if str(map_status).startswith("fallback"):
        map_line += " Per-file map digest was not applied; synthesis uses the same sampled files as single-pass."

    lines = [
        "Scope note: priority-guided triage over a sampled subset of repository files — not an exhaustive audit.",
        f"Coverage: analyzed {analyzed_files}/{total_files} files ({file_pct:.1f}%) and "
        f"{analyzed_bytes}/{total_bytes} bytes ({byte_pct:.1f}%).",
    ]
    if approx_tok:
        lines.append(
            f"Approx. evidence scale (rough): ~{approx_tok} token-equivalent (README + excerpts, chars÷4 heuristic; "
            "not a provider billing count).",
        )
    if coverage.get("repo_files_by_category") and coverage.get("selected_files_by_category"):
        lines.append(
            "Category snapshot — repo vs selected blob counts: "
            f"repo {coverage['repo_files_by_category']} | selected {coverage['selected_files_by_category']}.",
        )
    lines.append(map_line)
    if rev:
        lines.append(f"Revision keyed for answer cache: {rev[:12]}… (miss if the default branch tip changes).")
    lines.append(
        "Reliability: LLM + network + optional JSON map pass; identical prompts are not bit-for-bit deterministic.",
    )
    lines.append(
        "Repeated identical questions may hit a short TTL answer cache (see GROUNDED_REVIEW_ANSWER_CACHE_TTL_SECONDS).",
    )
    return "\n".join(lines)


def grounded_repo_review(query: str, model: str, ollama_base_url: str) -> dict[str, Any]:
    repo = extract_github_repo(query)
    if repo is None:
        return {
            "success": False,
            "answer": "No GitHub repository URL detected in the request.",
            "sources": [],
        }

    owner, name = repo

    fetcher = GitHubSnapshotFetcher()
    snapshot = get_cached_snapshot(owner, name, query)
    remote_error = ""
    if snapshot is None:
        try:
            snapshot = fetcher.fetch_snapshot(owner, name, query=query)
        except Exception as exc:
            remote_error = str(exc)
            snapshot = fetch_local_snapshot(name, query=query)
        if snapshot is not None:
            set_cached_snapshot(owner, name, query, snapshot)

    if snapshot is None:
        return {
            "success": False,
            "answer": (
                f"Could not fetch repository snapshot from GitHub ({remote_error}). "
                "No local clone was found in GROUNDED_REPO_ROOT or /opt/workspace."
            ),
            "sources": [],
        }

    return run_grounded_review_from_snapshot(
        query,
        model,
        ollama_base_url,
        snapshot,
        cache_owner=owner,
        cache_repo=name,
        use_answer_cache=True,
    )
