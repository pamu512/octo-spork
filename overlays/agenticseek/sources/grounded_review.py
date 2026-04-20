"""GitHub and local repository review helpers grounded on fetched file evidence.

Responses combine repository artifacts (README, sampled files) with optional LLM
synthesis; triage limits apply so outputs are not exhaustive audits.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests


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
GROUNDED_REVIEW_STRICT_COVERAGE = os.getenv("GROUNDED_REVIEW_STRICT_COVERAGE", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


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
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", ref],
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
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
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


def detect_recent_local_changes(repo_path: Path) -> set[str]:
    hints: set[str] = set()
    try:
        status = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
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
        diff = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--name-only", "HEAD~1..HEAD"],
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

    candidates.sort(key=lambda item: (-int(item["score"]), int(item["size"]), str(item["path"])))
    selected: list[str] = []
    total = 0
    selected_set: set[str] = set()

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
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--name-only", f"{base}...{head}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--name-only", base, head],
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
) -> dict[str, Any]:
    """Shared pipeline: map pass + synthesis. Optional answer cache for remote URL flow."""
    rev_raw = snapshot.get("revision_sha") or (snapshot.get("coverage") or {}).get("revision_sha")
    revision_sha = str(rev_raw).strip()[:40] if rev_raw else None

    if use_answer_cache and cache_owner and cache_repo:
        cached_answer = get_cached_answer(cache_owner, cache_repo, query, model, revision_sha)
        if cached_answer is not None:
            return {
                "success": bool(cached_answer.get("success", True)),
                "answer": str(cached_answer.get("answer", "")),
                "sources": list(cached_answer.get("sources", [])),
            }

    if not snapshot.get("readme") and not snapshot.get("files"):
        return {
            "success": False,
            "answer": "Repository snapshot is empty; unable to produce a grounded review.",
            "sources": [],
        }

    map_digest = ""
    map_status = "disabled"
    if GROUNDED_REVIEW_ENABLE_TWO_PASS:
        map_status = "not_needed"
        if should_use_two_pass_review(query, snapshot.get("files", [])):
            map_digest, map_status = run_map_review(query, snapshot, model, ollama_base_url)

    prompt = build_grounded_review_prompt(query, snapshot, map_digest=map_digest)
    try:
        answer = run_ollama_review(
            prompt,
            model,
            ollama_base_url,
            num_ctx=GROUNDED_REVIEW_NUM_CTX_TWO_PASS if map_digest else GROUNDED_REVIEW_NUM_CTX,
            timeout_seconds=210,
        )
    except Exception as exc:
        return {
            "success": False,
            "answer": f"Grounded review generation failed: {exc}",
            "sources": snapshot.get("sources", []),
        }
    answer_with_scope = f"{build_scope_note(snapshot, map_status)}\n\n{answer}"
    response_payload = {"success": True, "answer": answer_with_scope, "sources": snapshot["sources"]}
    if use_answer_cache and cache_owner and cache_repo:
        set_cached_answer(cache_owner, cache_repo, query, model, response_payload, revision_sha)
    return response_payload


def grounded_local_diff_review(
    query: str,
    model: str,
    ollama_base_url: str,
    repo_path: Path,
    base: str,
    head: str,
) -> dict[str, Any]:
    snapshot = fetch_local_diff_snapshot(repo_path, query, base, head)
    if snapshot is None:
        return {
            "success": False,
            "answer": "Not a git repository or diff could not be computed.",
            "sources": [],
        }
    cache_repo = f"{repo_path.resolve()}|{base}|{head}"
    return run_grounded_review_from_snapshot(
        query,
        model,
        ollama_base_url,
        snapshot,
        cache_owner="local-diff",
        cache_repo=cache_repo,
        use_answer_cache=True,
    )


def build_grounded_review_prompt(query: str, snapshot: dict[str, Any], map_digest: str = "") -> str:
    files_text = []
    for file_info in snapshot["files"]:
        files_text.append(f"\n### FILE: {file_info.path}\n{file_info.content}\n")
    files_joined = "\n".join(files_text)

    strict_block = ""
    if GROUNDED_REVIEW_STRICT_COVERAGE:
        strict_block = """
Severity discipline (strict coverage mode is ON via GROUNDED_REVIEW_STRICT_COVERAGE):
- Label a finding Critical only when the cited file excerpts clearly support that severity.
- If "selected_files_by_category" shows thin coverage in app/tests/deploy/ci, prefer Medium/Low and state the sampling gap explicitly.
"""

    return f"""
You are a senior software engineer and QA lead.
You must review the repository using only the supplied evidence.
If evidence is insufficient, explicitly say so and do not invent facts.

Important: This is LLM-assisted triage over a bounded, heuristic-selected subset of files (see coverage metadata).
It is not a deterministic exhaustive audit; identical runs may differ slightly even at low temperature.

User request:
{query}

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
README:
{snapshot["readme"]}

Repository files:
{files_joined}

Return markdown with these sections:
1) System summary (grounded)
2) Severity-ranked findings (Critical / High / Medium / Low)
3) Hardening plan (short-term, medium-term)
4) QA strategy (focus on regression prevention)
5) Top 5 concrete next actions
6) Confidence and evidence gaps
7) Coverage summary

Use explicit file citations like `path/to/file`.
For each Critical/High finding, include: risk, why it matters, and one concrete mitigation.
Be explicit that this review is based on a prioritized subset of repository files.
"""


def run_ollama_review(
    prompt: str,
    model: str,
    ollama_base_url: str,
    *,
    num_ctx: int = 16384,
    temperature: float = 0.1,
    timeout_seconds: int = 420,
) -> str:
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
    return payload.get("response", "").strip()


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
    file_chunks: list[str] = []
    for file_info in files:
        file_chunks.append(
            f"FILE: {file_info.path}\n"
            f"CONTENT_START\n{file_info.content[:4_000]}\nCONTENT_END\n"
        )
    return f"""
You are a senior software engineer and QA reviewer.
Analyze each file independently and produce compact JSON only.

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


def run_map_review(query: str, snapshot: dict[str, Any], model: str, ollama_base_url: str) -> tuple[str, str]:
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
