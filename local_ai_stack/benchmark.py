"""Spoon-Knife (or custom URL) grounded-review benchmark with CSV metrics."""

from __future__ import annotations

import csv
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

SPOON_KNIFE_URL_DEFAULT = "https://github.com/octocat/Spoon-Knife.git"

STANDARD_BENCHMARK_QUERY = """Perform a standardized security, QA, and regression review of this repository diff.
Use only the supplied evidence. Produce markdown with severity-ranked findings and QA notes suitable for comparing local LLMs."""

CSV_COLUMNS = (
    "timestamp_utc",
    "git_url",
    "base_ref",
    "head_ref",
    "model",
    "clone_seconds",
    "snapshot_seconds",
    "scan_seconds",
    "llm_seconds",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "success",
    "notes",
)


def ensure_agenticseek_import_path() -> None:
    agentic = ROOT / "overlays" / "agenticseek"
    s = str(agentic.resolve())
    if s not in sys.path:
        sys.path.insert(0, s)


def append_performance_csv(output_path: Path, row: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not output_path.exists()
    with output_path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
        if write_header:
            w.writeheader()
        safe = {k: row.get(k, "") for k in CSV_COLUMNS}
        w.writerow(safe)


def git_clone_timed(url: str, dest: Path, depth: int) -> float:
    """Clone via :class:`utils.git_utils.IOThrottle` (shallow, disk check, selective fetch if huge)."""
    if dest.exists():
        raise FileExistsError(str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    from utils.git_utils import IOThrottle

    t0 = time.perf_counter()
    IOThrottle.clone_repository(url, dest, depth=depth)
    return time.perf_counter() - t0


def resolve_default_refs(repo: Path) -> tuple[str, str]:
    """Oldest commit vs ``HEAD`` so the diff spans repository history."""
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError(f"Could not resolve root commit under {repo}")
    root = lines[0]
    rh = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    head = (rh.stdout or "").strip()
    if not head:
        raise RuntimeError(f"Could not resolve HEAD under {repo}")
    return root, head


def run_benchmark(
    *,
    env_file: Path,
    git_url: str,
    work_parent: Path,
    clone_depth: int,
    output_csv: Path,
    model_override: str | None,
    ollama_url_override: str | None,
    base_ref: str | None,
    head_ref: str | None,
    skip_clone: bool,
    repo_dir: Path | None,
    show_review: bool,
) -> int:
    """Clone Spoon-Knife (unless skipped), run grounded diff review with timing; append ``performance.csv`` row.

    Returns shell exit code (0 when review reports success).
    """
    ensure_agenticseek_import_path()

    os.environ.setdefault("GROUNDED_REVIEW_CODEQL_LANGUAGE", "javascript")
    os.environ.setdefault(
        "GROUNDED_REVIEW_CODEQL_SUITE",
        "codeql/javascript-queries:codeql-suites/javascript-security-and-quality.qls",
    )

    from local_ai_stack.__main__ import _load_grounded_review, _parse_env_file

    env_values = _parse_env_file(env_file)
    ollama_url = (
        (ollama_url_override or env_values.get("OLLAMA_LOCAL_URL") or env_values.get("OLLAMA_BASE_URL") or "").strip()
    )
    if not ollama_url:
        ollama_url = "http://127.0.0.1:11434"
    ollama_url = ollama_url.rstrip("/")
    model = (model_override or env_values.get("OLLAMA_MODEL") or "qwen2.5:14b").strip() or "qwen2.5:14b"

    clone_sec = 0.0
    if skip_clone:
        if repo_dir is None:
            raise RuntimeError("--skip-clone requires --repo-dir pointing at an existing git clone")
        target = repo_dir.resolve()
        if not (target / ".git").is_dir():
            raise RuntimeError(f"Not a git repository: {target}")
    else:
        work_parent.mkdir(parents=True, exist_ok=True)
        name = git_url.rstrip("/").split("/")[-1].removesuffix(".git") or "benchmark-repo"
        target = work_parent / name
        if target.exists():
            raise RuntimeError(
                f"Clone destination already exists: {target}\n"
                "Remove it, choose another --work-dir, or use --skip-clone --repo-dir …"
            )
        clone_sec = git_clone_timed(git_url, target, clone_depth)

    if base_ref and head_ref:
        base, head = base_ref, head_ref
    elif base_ref or head_ref:
        raise RuntimeError("Provide both --base and --head, or neither (defaults to root commit … HEAD)")
    else:
        base, head = resolve_default_refs(target)

    metrics: dict[str, Any] = {}
    gr = _load_grounded_review()
    result = gr.grounded_local_diff_review(
        STANDARD_BENCHMARK_QUERY,
        model,
        ollama_url,
        target,
        base,
        head,
        use_answer_cache=False,
        metrics=metrics,
    )

    bm = result.get("benchmark_metrics") if isinstance(result.get("benchmark_metrics"), dict) else metrics
    prompt_t = int(bm.get("prompt_tokens_total", 0) or 0)
    comp_t = int(bm.get("completion_tokens_total", 0) or 0)
    total_tok = prompt_t + comp_t
    snap_s = float(bm.get("snapshot_seconds", 0) or 0)
    scan_s = float(bm.get("scan_seconds", 0) or 0)
    llm_s = float(bm.get("llm_seconds", 0) or 0)

    notes = ""
    if not result.get("success"):
        notes = str(result.get("answer", ""))[:1200].replace("\n", " ").strip()

    row = {
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_url": git_url,
        "base_ref": base,
        "head_ref": head,
        "model": model,
        "clone_seconds": f"{clone_sec:.6f}",
        "snapshot_seconds": f"{snap_s:.6f}",
        "scan_seconds": f"{scan_s:.6f}",
        "llm_seconds": f"{llm_s:.6f}",
        "prompt_tokens": prompt_t,
        "completion_tokens": comp_t,
        "total_tokens": total_tok,
        "success": "1" if result.get("success") else "0",
        "notes": notes,
    }

    append_performance_csv(output_csv, row)

    if show_review and result.get("success"):
        print(result.get("answer", ""), flush=True)

    print(
        f"[benchmark] clone={clone_sec:.2f}s snapshot={snap_s:.2f}s scan={scan_s:.2f}s "
        f"llm={llm_s:.2f}s tokens_total={total_tok} (prompt={prompt_t} completion={comp_t}) "
        f"csv={output_csv}",
        flush=True,
    )

    return 0 if result.get("success") else 1
