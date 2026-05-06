"""Run CodeQL database create + security suite analysis on a clone; summarize for PR comments."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

_LOG = logging.getLogger(__name__)

_SKIP_ENV = "OCTO_SPORK_SKIP_CODEQL"
_CODEQL_PATH_ENV = "CODEQL_PATH"
_TIMEOUT_CREATE = int(os.environ.get("OCTO_SPORK_CODEQL_TIMEOUT_CREATE", "3600"))
_TIMEOUT_ANALYZE = int(os.environ.get("OCTO_SPORK_CODEQL_TIMEOUT_ANALYZE", "3600"))
_DEFAULT_LANG = os.environ.get("OCTO_SPORK_CODEQL_LANGUAGE", "").strip()

# Security-focused CodeQL suites (language packs must be resolved by local CodeQL distribution).
_DEFAULT_SUITES: dict[str, str] = {
    "python": "codeql/python-queries:codeql-suites/python-security-and-quality.qls",
    "javascript": "codeql/javascript-queries:codeql-suites/javascript-security-and-quality.qls",
    "typescript": "codeql/javascript-queries:codeql-suites/javascript-security-and-quality.qls",
    "go": "codeql/go-queries:codeql-suites/go-security-and-quality.qls",
    "java": "codeql/java-queries:codeql-suites/java-security-and-quality.qls",
    "csharp": "codeql/csharp-queries:codeql-suites/csharp-security-and-quality.qls",
    "cpp": "codeql/cpp-queries:codeql-suites/cpp-security-and-quality.qls",
    "ruby": "codeql/ruby-queries:codeql-suites/ruby-security-and-quality.qls",
}


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _uri_to_repo_relative(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path or "")
        return path.lstrip("/")
    return uri.replace("\\", "/")


@dataclass
class CriticalFinding:
    """One SARIF result treated as *Critical* tier for PR summary."""

    rule_id: str
    file_path: str
    line: int
    message: str
    sarif_level: str


@dataclass
class CodeQLPipelineResult:
    """Outcome of :meth:`CodeQLRunner.run_on_source_root`."""

    markdown: str
    build_failed: bool = False
    compiler_log: str | None = None


class CodeQLDatabaseCreateFailed(RuntimeError):
    """Raised when ``codeql database create`` exits non-zero (often build/compiler errors)."""

    def __init__(self, message: str, *, stderr: str, stdout: str) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.stdout = stdout


class CodeQLRunner:
    """CodeQL CLI wrapper: PATH probe, temp DB, security suite, Critical SARIF extraction."""

    def __init__(
        self,
        codeql_executable: str | None = None,
        *,
        timeout_create_sec: int | None = None,
        timeout_analyze_sec: int | None = None,
    ) -> None:
        resolved = codeql_executable or os.environ.get(_CODEQL_PATH_ENV) or shutil.which("codeql")
        self._codeql = resolved
        self._t_create = int(timeout_create_sec if timeout_create_sec is not None else _TIMEOUT_CREATE)
        self._t_analyze = int(timeout_analyze_sec if timeout_analyze_sec is not None else _TIMEOUT_ANALYZE)

    def available(self) -> bool:
        if not self._codeql:
            return False
        ep = Path(self._codeql)
        if ep.is_file():
            return True
        return shutil.which(self._codeql) is not None

    def infer_language(self, source_root: Path) -> str:
        """Pick a CodeQL language from repo layout (override with ``OCTO_SPORK_CODEQL_LANGUAGE``)."""
        if _DEFAULT_LANG:
            return _DEFAULT_LANG.lower()
        root = source_root.resolve()
        if (root / "go.mod").is_file():
            return "go"
        if (root / "pom.xml").is_file() or (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
            return "java"
        if (root / "Gemfile").is_file() or (root / ".ruby-version").is_file():
            return "ruby"
        tsconfig = root / "tsconfig.json"
        if tsconfig.is_file():
            return "typescript"
        if (root / "package.json").is_file():
            return "javascript"
        if (
            (root / "pyproject.toml").is_file()
            or (root / "setup.py").is_file()
            or (root / "requirements.txt").is_file()
            or (root / "setup.cfg").is_file()
        ):
            return "python"
        if any(root.glob("*.csproj")) or any(root.glob("*.sln")):
            return "csharp"
        ch = root / "CMakeLists.txt"
        cpp_hints = (root / "compile_commands.json").is_file() or ch.is_file()
        if cpp_hints:
            return "cpp"
        return "python"

    def resolve_security_suite(self, language: str) -> str:
        """Resolve security-focused query suite (env ``OCTO_SPORK_CODEQL_SUITE`` overrides)."""
        env_suite = os.environ.get("OCTO_SPORK_CODEQL_SUITE", "").strip()
        if env_suite:
            return env_suite
        lang = language.lower().strip()
        return _DEFAULT_SUITES.get(lang, _DEFAULT_SUITES["python"])

    def create_database(self, source_root: Path, database_path: Path, *, language: str) -> None:
        if not self._codeql:
            raise FileNotFoundError("codeql CLI not found on PATH (set CODEQL_PATH).")
        root = source_root.expanduser().resolve()
        db = database_path.expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"CodeQL source root is not a directory: {root}")
        db.parent.mkdir(parents=True, exist_ok=True)
        if db.exists():
            shutil.rmtree(db, ignore_errors=True)
        lang = str(language or "python").strip() or "python"
        cmd = [
            self._codeql,
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
                timeout=max(60, self._t_create),
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"codeql not runnable: {self._codeql}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"codeql database create timed out after {self._t_create}s") from exc
        if completed.returncode != 0:
            err = (completed.stderr or "").strip()
            out = (completed.stdout or "").strip()
            raise CodeQLDatabaseCreateFailed(
                f"codeql database create exited {completed.returncode}",
                stderr=err,
                stdout=out,
            )

    def analyze_to_sarif(
        self,
        database_path: Path,
        sarif_out: Path,
        *,
        query_suite: str,
    ) -> None:
        if not self._codeql:
            raise FileNotFoundError("codeql CLI not found on PATH.")
        db = database_path.expanduser().resolve()
        out = sarif_out.expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        suite = str(query_suite or "").strip()
        if not suite:
            raise ValueError("query_suite is empty")
        cmd = [
            self._codeql,
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
                timeout=max(60, self._t_analyze),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"codeql database analyze timed out after {self._t_analyze}s") from exc
        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"codeql database analyze exited {completed.returncode}: {err[:8000]}")
        if not out.is_file() or out.stat().st_size == 0:
            raise RuntimeError(f"SARIF missing or empty at {out}")

    @staticmethod
    def extract_critical_findings(sarif_payload: dict[str, Any]) -> list[CriticalFinding]:
        """Collect SARIF results classified as *Critical* for the PR table."""
        out: list[CriticalFinding] = []
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
                if not _sarif_result_is_critical(result, rules_map):
                    continue
                level = str(result.get("level") or "")
                rule_id = str(result.get("ruleId") or "")
                msg_obj = result.get("message")
                if isinstance(msg_obj, dict):
                    message = str(msg_obj.get("text") or rule_id or "")
                else:
                    message = str(msg_obj or "")
                message = message.strip().replace("\r\n", "\n")
                if len(message) > 500:
                    message = message[:497] + "..."

                loc_line = 0
                file_path = "(no location)"
                locations = result.get("locations") or []
                if isinstance(locations, list) and locations:
                    loc0 = locations[0] if isinstance(locations[0], dict) else {}
                    phys = loc0.get("physicalLocation") if isinstance(loc0.get("physicalLocation"), dict) else {}
                    region = phys.get("region") if isinstance(phys.get("region"), dict) else {}
                    al = phys.get("artifactLocation") if isinstance(phys.get("artifactLocation"), dict) else {}
                    uri = str(al.get("uri") or "")
                    file_path = _uri_to_repo_relative(uri) or uri or "(unknown file)"
                    try:
                        loc_line = int(region.get("startLine") or 0)
                    except (TypeError, ValueError):
                        loc_line = 0

                out.append(
                    CriticalFinding(
                        rule_id=rule_id or "(rule)",
                        file_path=file_path,
                        line=loc_line,
                        message=message or "(empty)",
                        sarif_level=level or "unknown",
                    )
                )
        return out

    def run_on_source_root(
        self,
        source_root: Path,
        *,
        work_dir: Path | None = None,
    ) -> CodeQLPipelineResult:
        """Create DB under ``work_dir``, run security suite, return markdown (findings or system warning)."""
        root = source_root.expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(source_root)

        base = work_dir if work_dir is not None else root / ".octo-spork-codeql-work"
        base.mkdir(parents=True, exist_ok=True)
        db_path = base / "db"
        sarif_path = base / "results.sarif"
        language = self.infer_language(root)
        suite = self.resolve_security_suite(language)

        try:
            self.create_database(root, db_path, language=language)
        except CodeQLDatabaseCreateFailed as exc:
            log_blob = _merge_build_logs(exc.stderr, exc.stdout)
            md = _format_system_warning_build_failed(log_blob, language=language)
            return CodeQLPipelineResult(markdown=md, build_failed=True, compiler_log=log_blob)

        try:
            self.analyze_to_sarif(db_path, sarif_path, query_suite=suite)
        except (RuntimeError, TimeoutError, OSError) as exc:
            _LOG.warning("CodeQL analyze failed: %s", exc)
            return CodeQLPipelineResult(
                markdown=_format_analyze_failed(str(exc)),
                build_failed=False,
                compiler_log=None,
            )

        try:
            payload = json.loads(sarif_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            return CodeQLPipelineResult(
                markdown=f"_CodeQL: could not read SARIF: `{exc}`_\n",
                build_failed=False,
                compiler_log=None,
            )

        findings = self.extract_critical_findings(payload)
        md = _format_critical_table(findings, language=language, suite=suite)
        return CodeQLPipelineResult(markdown=md, build_failed=False, compiler_log=None)


def _merge_build_logs(stderr: str, stdout: str) -> str:
    parts = []
    if stderr.strip():
        parts.append(stderr.strip())
    if stdout.strip():
        parts.append(stdout.strip())
    return "\n\n".join(parts) if parts else "(no compiler output captured)"


_COMPILER_HINTS = re.compile(
    r"(error:|fatal error:|FAILED|Traceback|Exception:|CMake Error|ninja: build stopped|"
    r"mvn:.+ERROR|gradle.+FAILED|could not compile|cannot find symbol|SyntaxError|IndentationError)",
    re.IGNORECASE,
)


def _sarif_result_is_critical(result: dict[str, Any], rules_map: dict[str, dict[str, Any]]) -> bool:
    """Treat GitHub/CodeQL *critical* / SARIF *error* results as Critical-tier findings."""
    props = result.get("properties")
    if isinstance(props, dict):
        for key in ("github/alertSeverity", "problem.severity", "severity"):
            val = props.get(key)
            if isinstance(val, str) and val.strip().lower() == "critical":
                return True
        gh = props.get("github/severity")
        if isinstance(gh, str) and "critical" in gh.lower():
            return True

    level = str(result.get("level") or "").lower()
    if level == "error":
        return True

    rule_id = str(result.get("ruleId") or "")
    rule = rules_map.get(rule_id) if rule_id else None
    if isinstance(rule, dict):
        props_r = rule.get("properties")
        if isinstance(props_r, dict):
            for key in ("problem.severity", "precision", "security-severity"):
                sev = props_r.get(key)
                if isinstance(sev, str) and "critical" in sev.lower():
                    return True
        dc = rule.get("defaultConfiguration")
        if isinstance(dc, dict):
            lev = str(dc.get("level") or "").lower()
            if lev == "error":
                return True

    return False


def _md_cell(text: str, *, max_len: int = 320) -> str:
    s = str(text).replace("\r\n", " ").replace("\n", " ").replace("|", "\\|").strip()
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s or " "


def _fence_compiler_log(text: str, *, max_len: int = 12_000) -> str:
    raw = text.strip()
    if len(raw) > max_len:
        raw = raw[: max_len - 80] + "\n\n… _(truncated)_"
    return raw.replace("```", "`\u200b``")


def _format_system_warning_build_failed(compiler_log: str, *, language: str) -> str:
    lines = [
        "### System Warning (CodeQL)",
        "",
        "CodeQL **database creation** failed while extracting or building the cloned PR tree.",
        f"_Inferred language:_ `{language}`. This usually indicates a **compiler / build / autoconfig** issue.",
        "",
        "<details>",
        "<summary>Compiler / extractor output (click to expand)</summary>",
        "",
        "```text",
        _fence_compiler_log(compiler_log),
        "```",
        "",
        "</details>",
        "",
    ]
    if _COMPILER_HINTS.search(compiler_log):
        lines.append("_Detected compiler/build error patterns in the log above._")
        lines.append("")
    return "\n".join(lines)


def _format_analyze_failed(msg: str) -> str:
    return (
        "### CodeQL (security suite)\n\n"
        f"_Analysis step failed (after database create): `{_md_cell(msg, max_len=400)}`_\n\n"
    )


def _format_critical_table(
    findings: list[CriticalFinding],
    *,
    language: str,
    suite: str,
) -> str:
    lines = [
        "### CodeQL — Critical findings",
        "",
        f"_Language:_ `{language}` · _Suite:_ `{suite}`",
        "",
    ]
    if not findings:
        lines.append("_No **Critical**-tier SARIF results (or none mapped to this severity)._")
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        [
            "| Rule | Location | Message |",
            "| --- | --- | --- |",
        ]
    )
    for f in findings[:80]:
        loc = f"`{f.file_path}`" + (f":{f.line}" if f.line else "")
        lines.append(
            f"| {_md_cell(f.rule_id, max_len=48)} | {_md_cell(loc, max_len=72)} | {_md_cell(f.message)} |"
        )
    if len(findings) > 80:
        lines.append("")
        lines.append(f"_Showing 80 of {len(findings)} Critical results._")
    lines.append("")
    return "\n".join(lines)


def critical_findings_markdown_from_sarif(
    sarif_payload: dict[str, Any],
    *,
    source_root: Path,
    runner: CodeQLRunner | None = None,
) -> str:
    """Rebuild the PR markdown table from cached CodeQL SARIF (same shape as a fresh pipeline run)."""
    r = runner or CodeQLRunner()
    findings = r.extract_critical_findings(sarif_payload)
    language = r.infer_language(source_root)
    suite = r.resolve_security_suite(language)
    return _format_critical_table(findings, language=language, suite=suite)


def scan_pr_branch_codeql_to_markdown(
    *,
    clone_url: str,
    branch: str,
    token: str,
    runner: CodeQLRunner | None = None,
    repo_full_name: str | None = None,
    pr_html_url: str | None = None,
) -> str:
    """Clone PR head to a temp dir, run CodeQL, return markdown for the PR body."""
    if _env_truthy(_SKIP_ENV):
        return "_CodeQL scan skipped (`OCTO_SPORK_SKIP_CODEQL`)._\n"

    r = runner or CodeQLRunner()
    if not r.available():
        return (
            "_CodeQL CLI not found on PATH; install the [CodeQL CLI](https://github.com/github/codeql-cli-binaries) "
            "or set `CODEQL_PATH` / `OCTO_SPORK_SKIP_CODEQL=1`._\n"
        )

    from github_bot.trivy_scanner import temporary_pr_clone

    try:
        with temporary_pr_clone(clone_url, branch, token) as repo_root:
            work = repo_root / ".octo-spork-codeql-work"
            res = r.run_on_source_root(repo_root, work_dir=work)
            md = res.markdown
            sarif_path = work / "results.sarif"
            if (
                repo_full_name
                and pr_html_url
                and sarif_path.is_file()
                and not res.build_failed
            ):
                try:
                    from github_bot.global_smell_index import ingest_sarif_findings

                    payload = json.loads(sarif_path.read_text(encoding="utf-8", errors="replace"))
                    smell_md, _rec = ingest_sarif_findings(
                        "codeql",
                        payload,
                        repo_root,
                        repo_full_name=repo_full_name,
                        pr_html_url=pr_html_url,
                    )
                    if smell_md.strip():
                        md = md.rstrip() + "\n\n" + smell_md.strip() + "\n"
                except Exception as exc:
                    _LOG.debug("global smell index (codeql): %s", exc)
            return md + "\n"
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _LOG.warning("CodeQL PR scan failed: %s", exc)
        return f"_CodeQL: unexpected failure `{exc}`_\n\n"
