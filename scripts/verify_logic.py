#!/usr/bin/env python3
"""Golden-path integration check: optional stack bring-up, grounded diff review on a dummy vuln repo.

Steps:
  1. Optionally ensure ``local_ai_stack verify`` passes, then ``local_ai_stack up`` if requested.
  2. Materialize a tiny git repo with three deliberate weak patterns (secrets / SQL concat / shell=True).
  3. Run ``grounded_local_diff_review`` (Ollama) and require at least two vulnerability themes recalled.
  4. Audit citations with :mod:`claude_bridge.receipt_auditor` — paths must exist and line numbers in-range.

Bypass hook / emergency commit::

    OCTO_SKIP_VERIFY_LOGIC=1 git commit ...

Full stack spin-up (slow; CI / manual validation)::

    python3 scripts/verify_logic.py --repo-root . --bring-up

Hook-friendly (no Docker compose up; fails fast if Ollama/stack unreachable)::

    python3 scripts/verify_logic.py --repo-root . --no-bring-up
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def _repo_root_from_env_or_arg(raw: str | None) -> Path:
    if raw:
        p = Path(raw).expanduser().resolve()
        return p
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if out.returncode == 0 and (out.stdout or "").strip():
            return Path(out.stdout.strip()).resolve()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return Path.cwd().resolve()


def _parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        env[key] = value
    return env


def ensure_sys_path(repo_root: Path, env: dict[str, str]) -> Path:
    """Expose AgenticSeek ``sources`` package (overlay clone or AGENTICSEEK_DIR)."""
    overlay = repo_root / "overlays" / "agenticseek"
    if (overlay / "sources").is_dir():
        root = overlay.resolve()
    else:
        raw = (env.get("AGENTICSEEK_DIR") or "").strip()
        if not raw:
            raise RuntimeError(
                "No overlays/agenticseek/sources tree and AGENTICSEEK_DIR unset. "
                "Run bootstrap or set AGENTICSEEK_DIR in deploy/local-ai/.env.local."
            )
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (repo_root / p).resolve()
        if not (p / "sources").is_dir():
            raise RuntimeError(f"AGENTICSEEK_DIR does not contain sources/: {p}")
        root = p
    rs = str(root)
    if rs not in sys.path:
        sys.path.insert(0, rs)
    src = str(repo_root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    rr = str(repo_root)
    if rr not in sys.path:
        sys.path.insert(0, rr)
    return root


def load_grounded_review(repo_root: Path, agentic_root: Path):
    """Load overlay grounded_review (same file ``local_ai_stack`` uses)."""
    candidates = [
        repo_root / "overlays" / "agenticseek" / "sources" / "grounded_review.py",
        agentic_root / "sources" / "grounded_review.py",
    ]
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("grounded_review_verify", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("grounded_review.py not found under overlay or AGENTICSEEK_DIR")


# (relative_path, expected_line, keyword_groups for “did the model mention this theme?”)
_KNOWN_VULNS: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("app/secrets.py", 2, ("secret", "hardcod", "api_key", "credential", "password")),
    ("app/sql.py", 2, ("sql", "injection", "concat", "select")),
    ("app/shell.py", 5, ("shell", "subprocess")),
)


def _write_dummy_sources(app: Path) -> None:
    (app / "secrets.py").write_text(
        "# Integration-test dummy only — not a real credential.\n"
        'API_KEY = "sk-integration-hardcoded-key"\n',
        encoding="utf-8",
    )
    (app / "sql.py").write_text(
        'def user_rows(uid: str) -> str:\n'
        '    return "SELECT * FROM users WHERE id = " + uid\n',
        encoding="utf-8",
    )
    (app / "shell.py").write_text(
        "import subprocess\n\n\n"
        "def run(cmd: str) -> None:\n"
        "    subprocess.call(cmd, shell=True)\n",
        encoding="utf-8",
    )


def materialize_dummy_repo(path: Path) -> tuple[str, str]:
    """Two commits: base README, then three vulnerable modules. Returns (base_ref, head_ref)."""
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "verify-logic@local.test"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "verify-logic"], cwd=path, check=True)
    (path / "README.md").write_text("# dummy golden-path fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=path, check=True)

    app = path / "app"
    app.mkdir()
    _write_dummy_sources(app)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "add intentional test-only weak patterns"], cwd=path, check=True)
    return "HEAD~1", "HEAD"


def count_identified_themes(answer_lower: str) -> int:
    hit = 0
    for _path, _line, keywords in _KNOWN_VULNS:
        if any(k in answer_lower for k in keywords):
            hit += 1
    return hit


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def citation_covers_vuln(
    citations: Sequence[object],
    rel_path: str,
    expected_line: int,
    *,
    tol: int = 2,
) -> bool:
    rel = _norm(rel_path)
    for c in citations:
        raw = _norm(str(getattr(c, "raw", "")).strip())
        if not raw.endswith(rel) and not raw.endswith("/" + rel):
            continue
        line = getattr(c, "line", None)
        if line is None:
            continue
        if abs(int(line) - int(expected_line)) <= tol:
            return True
    return False


def assert_receipts_for_dummy(answer: str, dummy_root: Path) -> list[str]:
    """Return list of error strings; empty means receipt checks passed."""
    from claude_bridge.receipt_auditor import audit_transcript, extract_citations

    cites = extract_citations(answer)
    errors: list[str] = []
    warn = audit_transcript(answer, dummy_root)
    if warn:
        errors.extend(warn)

    for rel, line, _keys in _KNOWN_VULNS:
        if not citation_covers_vuln(cites, rel, line):
            errors.append(
                f"Missing grounded citation near {rel}:{line} (expected path:line within ±2 lines "
                f"in model answer; extracted {len(cites)} citation(s))."
            )
    return errors


def ensure_stack(repo_root: Path, env_file: Path, *, bring_up: bool) -> None:
    py = sys.executable
    env_fp = env_file.resolve()
    verify_cmd = [py, "-m", "local_ai_stack", "verify", "--env-file", str(env_fp)]
    r = subprocess.run(verify_cmd, cwd=str(repo_root), check=False)
    if r.returncode == 0:
        return
    sys.stderr.write(
        "[verify_logic] `local_ai_stack verify` failed — stack may be down.\n",
    )
    if not bring_up:
        sys.stderr.write(
            "[verify_logic] Re-run with --bring-up to start Docker compose, "
            "or start the stack manually.\n",
        )
        raise SystemExit(1)
    up_cmd = [py, "-m", "local_ai_stack", "up", "--env-file", str(env_fp)]
    subprocess.run(up_cmd, cwd=str(repo_root), check=True)
    r2 = subprocess.run(verify_cmd, cwd=str(repo_root), check=False)
    if r2.returncode != 0:
        sys.stderr.write("[verify_logic] verify still failing after `up`.\n")
        raise SystemExit(1)


def run_review(
    repo_root: Path,
    env: dict[str, str],
    dummy_repo: Path,
    base: str,
    head: str,
) -> dict:
    agentic_root = ensure_sys_path(repo_root, env)
    gr = load_grounded_review(repo_root, agentic_root)
    ollama_url = (
        env.get("OLLAMA_LOCAL_URL") or env.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434"
    ).rstrip("/")
    model = (env.get("OLLAMA_MODEL") or "qwen2.5:14b").strip() or "qwen2.5:14b"
    query = (
        "Security review of this diff only. Identify hardcoded secrets, SQL injection risks "
        "(unsafe string concatenation building SQL), and unsafe subprocess/exec/shell usage. "
        "For each finding cite the evidence as relative/path.py:LINE using repository-relative paths."
    )
    return gr.grounded_local_diff_review(
        query,
        model,
        ollama_url,
        dummy_repo,
        base,
        head,
        use_answer_cache=False,
        metrics=None,
    )


def main(argv: list[str] | None = None) -> int:
    if (os.environ.get("OCTO_SKIP_VERIFY_LOGIC") or "").strip():
        print("[verify_logic] OCTO_SKIP_VERIFY_LOGIC set — skipping golden-path check.", file=sys.stderr)
        return 0

    parser = argparse.ArgumentParser(description="Octo-spork golden-path integration (stack + grounded review).")
    parser.add_argument("--repo-root", default=None, help="Octo-spork repository root (default: git toplevel)")
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to deploy/local-ai/.env.local (default: <repo>/deploy/local-ai/.env.local)",
    )
    parser.add_argument(
        "--dummy-repo",
        default=None,
        help="Where to create the dummy git fixture (default: <repo>/.local/verify_logic/dummy_repo)",
    )
    parser.add_argument(
        "--bring-up",
        action="store_true",
        help="If verify fails, run `local_ai_stack up` then verify again (slow).",
    )
    parser.add_argument(
        "--no-bring-up",
        action="store_true",
        help="Never run docker compose up (default unless --bring-up).",
    )
    args = parser.parse_args(argv)

    bring_up = bool(args.bring_up) and not bool(args.no_bring_up)

    repo_root = _repo_root_from_env_or_arg(args.repo_root)
    env_path = Path(args.env_file or (repo_root / "deploy" / "local-ai" / ".env.local")).resolve()
    if not env_path.is_file():
        sys.stderr.write(f"[verify_logic] Missing env file: {env_path}\n")
        return 1

    env = _parse_env_file(env_path)

    dummy = Path(args.dummy_repo or (repo_root / ".local" / "verify_logic" / "dummy_repo")).resolve()

    ensure_stack(repo_root, env_path, bring_up=bring_up)

    base, head = materialize_dummy_repo(dummy)
    try:
        result = run_review(repo_root, env, dummy, base, head)
    except Exception as exc:
        sys.stderr.write(f"[verify_logic] grounded review failed: {exc}\n")
        return 1

    if not result.get("success"):
        sys.stderr.write(f"[verify_logic] Review reported failure: {result.get('answer', '')[:2000]}\n")
        return 1

    answer = str(result.get("answer") or "")
    lower = answer.lower()
    themes = count_identified_themes(lower)
    if themes < 2:
        sys.stderr.write(
            f"[verify_logic] Expected at least 2 of 3 vulnerability themes in the answer; saw {themes}.\n"
            f"Answer excerpt:\n{answer[:4000]}\n",
        )
        return 1

    receipt_errs = assert_receipts_for_dummy(answer, dummy)
    if receipt_errs:
        for e in receipt_errs:
            sys.stderr.write(f"[verify_logic] Receipt check: {e}\n")
        return 1

    print(
        f"[verify_logic] OK — themes={themes}/3, receipts validated for {len(_KNOWN_VULNS)} files "
        f"(dummy repo: {dummy}).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
