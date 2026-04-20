"""Deterministic GitHub repository review utilities.

This module is designed to reduce hallucinations for "review/explain this repo"
requests by grounding the response on fetched repository artifacts.
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


def _answer_cache_key(owner: str, repo: str, query: str, model: str) -> str:
    return f"answer::{model}::{_cache_key(owner, repo, query)}"


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


def get_cached_answer(owner: str, repo: str, query: str, model: str) -> dict[str, Any] | None:
    cache = _load_cache()
    entry = cache.get(_answer_cache_key(owner, repo, query, model))
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


def set_cached_answer(owner: str, repo: str, query: str, model: str, payload: dict[str, Any]) -> None:
    cache = _load_cache()
    cache[_answer_cache_key(owner, repo, query, model)] = {
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
    max_files: int = 12,
    max_total_bytes: int = 220_000,
    max_file_bytes: int = 80_000,
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
        selected_paths = select_candidate_files(tree_json.get("tree", []), query=query or f"{owner}/{repo}")

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

        return {
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
        }


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

    changed_paths = detect_recent_local_changes(repo_path)
    selected_paths = select_candidate_files(
        tree_entries,
        query=query or repo_name,
        preferred_paths=changed_paths,
    )
    files: list[RepoFile] = []
    for rel in selected_paths:
        file_path = repo_path / rel
        if not file_path.exists():
            continue
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        files.append(RepoFile(path=rel, content=content[:8_000], size=len(content)))

    return {
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
    }


def build_grounded_review_prompt(query: str, snapshot: dict[str, Any], map_digest: str = "") -> str:
    files_text = []
    for file_info in snapshot["files"]:
        files_text.append(f"\n### FILE: {file_info.path}\n{file_info.content}\n")
    files_joined = "\n".join(files_text)

    return f"""
You are a senior software engineer and QA lead.
You must review the repository using only the supplied evidence.
If evidence is insufficient, explicitly say so and do not invent facts.

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

Use explicit file citations like `path/to/file`.
For each Critical/High finding, include: risk, why it matters, and one concrete mitigation.
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


def run_map_review(query: str, snapshot: dict[str, Any], model: str, ollama_base_url: str) -> str:
    files = list(snapshot.get("files", []))
    if not files:
        return ""
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
        return json.dumps(parsed, indent=2)
    except Exception:
        return ""


def grounded_repo_review(query: str, model: str, ollama_base_url: str) -> dict[str, Any]:
    repo = extract_github_repo(query)
    if repo is None:
        return {
            "success": False,
            "answer": "No GitHub repository URL detected in the request.",
            "sources": [],
        }

    owner, name = repo
    cached_answer = get_cached_answer(owner, name, query, model)
    if cached_answer is not None:
        return {
            "success": bool(cached_answer.get("success", True)),
            "answer": str(cached_answer.get("answer", "")),
            "sources": list(cached_answer.get("sources", [])),
        }

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

    if not snapshot["readme"] and not snapshot["files"]:
        return {
            "success": False,
            "answer": "Repository snapshot is empty; unable to produce a grounded review.",
            "sources": [],
        }

    map_digest = ""
    if should_use_two_pass_review(query, snapshot.get("files", [])):
        map_digest = run_map_review(query, snapshot, model, ollama_base_url)

    prompt = build_grounded_review_prompt(query, snapshot, map_digest=map_digest)
    try:
        answer = run_ollama_review(
            prompt,
            model,
            ollama_base_url,
            num_ctx=12288 if map_digest else 14336,
            timeout_seconds=210,
        )
    except Exception as exc:
        return {
            "success": False,
            "answer": f"Grounded review generation failed: {exc}",
            "sources": snapshot["sources"],
        }
    response_payload = {"success": True, "answer": answer, "sources": snapshot["sources"]}
    set_cached_answer(owner, name, query, model, response_payload)
    return response_payload
