"""Deterministic GitHub repository review utilities.

This module is designed to reduce hallucinations for "review/explain this repo"
requests by grounding the response on fetched repository artifacts.
"""

from __future__ import annotations

import base64
import os
import re
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


def score_candidate_path(path: str) -> int:
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
    if "/dist/" in lowered or "/build/" in lowered or "node_modules/" in lowered:
        score -= 100
    return score


def select_candidate_files(
    tree_entries: Iterable[dict[str, Any]],
    max_files: int = 16,
    max_total_bytes: int = 350_000,
    max_file_bytes: int = 120_000,
) -> list[str]:
    candidates: list[tuple[int, int, str]] = []
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
        candidates.append((score_candidate_path(path), size, path))

    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    selected: list[str] = []
    total = 0
    for score, size, path in candidates:
        if score < 0:
            continue
        if len(selected) >= max_files:
            break
        if total + size > max_total_bytes:
            continue
        selected.append(path)
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

    def fetch_snapshot(self, owner: str, repo: str) -> dict[str, Any]:
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
        selected_paths = select_candidate_files(tree_json.get("tree", []))

        files: list[RepoFile] = []
        for path in selected_paths:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/{path}"
            try:
                content = self._get_text(raw_url)
            except Exception:
                continue
            if "\x00" in content:
                continue
            files.append(RepoFile(path=path, content=content[:20_000], size=len(content)))

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


def fetch_local_snapshot(repo_name: str) -> dict[str, Any] | None:
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

    selected_paths = select_candidate_files(tree_entries)
    files: list[RepoFile] = []
    for rel in selected_paths:
        file_path = repo_path / rel
        if not file_path.exists():
            continue
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        files.append(RepoFile(path=rel, content=content[:20_000], size=len(content)))

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


def build_grounded_review_prompt(query: str, snapshot: dict[str, Any]) -> str:
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
1) What this system does (grounded)
2) Strengths
3) Critical risks and potential regressions (highest severity first)
4) Hardening recommendations (short-term, medium-term)
5) QA test strategy for existing features
6) Top 5 concrete next actions

Use explicit file citations like `path/to/file`.
"""


def run_ollama_review(prompt: str, model: str, ollama_base_url: str) -> str:
    endpoint = ollama_base_url.rstrip("/") + "/api/generate"
    response = requests.post(
        endpoint,
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_ctx": 16384},
        },
        timeout=420,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("response", "").strip()


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
    snapshot = None
    remote_error = ""
    try:
        snapshot = fetcher.fetch_snapshot(owner, name)
    except Exception as exc:
        remote_error = str(exc)
        snapshot = fetch_local_snapshot(name)

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

    prompt = build_grounded_review_prompt(query, snapshot)
    try:
        answer = run_ollama_review(prompt, model, ollama_base_url)
    except Exception as exc:
        return {
            "success": False,
            "answer": f"Grounded review generation failed: {exc}",
            "sources": snapshot["sources"],
        }
    return {"success": True, "answer": answer, "sources": snapshot["sources"]}
