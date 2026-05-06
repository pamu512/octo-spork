"""Fetch PR diff plus full file contents for grounded LLM context (GitHub REST API)."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

_GITHUB_API_VERSION = os.getenv("GITHUB_API_VERSION", "2022-11-28")
_DEFAULT_API_BASE = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
_USER_AGENT = "octo-spork-github-bot-git-manager"


@dataclass(frozen=True)
class GroundedFile:
    """One file touched in the PR, with optional patch hunk and full-file snapshot at ``head_sha``."""

    path: str
    status: str
    patch_hunk: str | None
    full_text: str | None
    is_binary: bool = False
    fetch_error: str | None = None
    previous_path: str | None = None


@dataclass
class GroundedPullRequest:
    """Unified PR diff plus per-file full content at the PR head for grounded review."""

    owner: str
    repo: str
    number: int
    head_sha: str
    base_sha: str
    unified_diff: str
    files: list[GroundedFile] = field(default_factory=list)

    def format_for_llm(self, *, max_chars_per_file: int | None = 500_000) -> str:
        """Render diff + full files into one document suitable for model context."""
        lines: list[str] = [
            f"# Pull request {self.owner}/{self.repo}#{self.number}",
            f"- head: `{self.head_sha}`",
            f"- base: `{self.base_sha}`",
            "",
            "## Unified diff (PR)",
            "```diff",
            self.unified_diff.strip(),
            "```",
            "",
            "## Full files at PR head (`head_sha`)",
            "",
        ]
        for gf in self.files:
            lines.append(f"### `{gf.path}` ({gf.status})")
            if gf.previous_path:
                lines.append(f"(renamed from `{gf.previous_path}`)")
            if gf.fetch_error:
                lines.append(f"_Could not load full file: {gf.fetch_error}_")
                if gf.patch_hunk:
                    lines.append("#### Per-file patch (API)")
                    lines.append("```diff")
                    lines.append(gf.patch_hunk.strip())
                    lines.append("```")
                lines.append("")
                continue
            if gf.is_binary:
                lines.append("_Binary file; full text omitted._")
                if gf.patch_hunk:
                    lines.append("#### Per-file patch (API)")
                    lines.append("```diff")
                    lines.append(gf.patch_hunk.strip())
                    lines.append("```")
                lines.append("")
                continue
            if gf.status == "removed" and not (gf.full_text or "").strip():
                if gf.patch_hunk:
                    lines.append("_File deleted at head; per-file patch from API:_")
                    lines.append("```diff")
                    lines.append(gf.patch_hunk.strip())
                    lines.append("```")
                else:
                    lines.append("_File deleted; no per-file patch in API response._")
                lines.append("")
                continue
            body = gf.full_text or ""
            if max_chars_per_file is not None and len(body) > max_chars_per_file:
                body = body[:max_chars_per_file] + "\n\n… [truncated]"
            lines.append("```text")
            lines.append(body.rstrip())
            lines.append("```")
            if gf.patch_hunk and gf.status not in {"removed"}:
                lines.append("")
                lines.append("#### Per-file patch (API)")
                lines.append("```diff")
                lines.append(gf.patch_hunk.strip())
                lines.append("```")
            lines.append("")
        return "\n".join(lines)


def _request(
    method: str,
    url: str,
    token: str,
    *,
    accept: str = "application/vnd.github+json",
    json_body: dict[str, Any] | None = None,
    timeout: int = 120,
) -> tuple[int, bytes]:
    payload = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": _USER_AGENT,
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()
        raise RuntimeError(
            f"GitHub API HTTP {exc.code} for {method} {url}: {detail.decode('utf-8', errors='replace')[:4000]}"
        ) from exc


def _get_json(url: str, token: str, *, timeout: int = 120) -> Any:
    code, raw = _request("GET", url, token, timeout=timeout)
    if code != 200:
        raise RuntimeError(f"Unexpected HTTP {code} for GET {url}")
    return json.loads(raw.decode("utf-8"))


def fetch_pr_unified_diff(
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
    *,
    api_base: str = _DEFAULT_API_BASE,
) -> str:
    """Return the full PR unified diff (``Accept: application/vnd.github.diff``)."""
    url = f"{api_base}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}/pulls/{pr_number}"
    _, raw = _request(
        "GET",
        url,
        token,
        accept="application/vnd.github.diff",
        timeout=180,
    )
    return raw.decode("utf-8", errors="replace")


def iter_pull_request_files(
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
    *,
    api_base: str = _DEFAULT_API_BASE,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    """Return all file objects from ``GET /repos/.../pulls/{n}/files`` (paginated)."""
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        q = urllib.parse.urlencode({"per_page": str(per_page), "page": str(page)})
        url = (
            f"{api_base}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"
            f"/pulls/{pr_number}/files?{q}"
        )
        batch = _get_json(url, token)
        if not isinstance(batch, list):
            raise RuntimeError("Unexpected /pulls/.../files response shape")
        out.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return out


def _decode_content_item(item: dict[str, Any]) -> tuple[str | None, bool, str | None]:
    """Decode ``contents`` API JSON into text or mark binary / error."""
    typ = item.get("type")
    if typ == "file":
        enc = item.get("encoding")
        raw_b64 = item.get("content")
        if enc == "base64" and isinstance(raw_b64, str):
            raw_b64 = raw_b64.replace("\n", "")
            try:
                data = base64.b64decode(raw_b64)
            except (ValueError, TypeError) as exc:
                return None, False, f"base64 decode failed: {exc}"
            try:
                return data.decode("utf-8"), False, None
            except UnicodeDecodeError:
                return None, True, None
        return None, False, "missing base64 content"
    if typ == "symlink":
        return None, False, "symlink target not resolved for LLM text"
    if typ == "submodule":
        return None, False, "git submodule (full tree not expanded here)"
    return None, False, f"unsupported content type: {typ}"


def fetch_file_at_ref(
    owner: str,
    repo: str,
    path: str,
    ref: str,
    token: str,
    *,
    api_base: str = _DEFAULT_API_BASE,
) -> tuple[str | None, bool, str | None]:
    """Return ``(text, is_binary, error)`` for file at ref using Contents API."""
    enc_path = urllib.parse.quote(path, safe="")
    q = urllib.parse.urlencode({"ref": ref})
    url = f"{api_base}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}/contents/{enc_path}?{q}"
    try:
        item = _get_json(url, token)
    except RuntimeError as exc:
        return None, False, str(exc)
    if not isinstance(item, dict):
        return None, False, "unexpected contents response"
    return _decode_content_item(item)


def build_grounded_pull_request(
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    token: str,
    *,
    api_base: str = _DEFAULT_API_BASE,
    include_removed_base_snapshot: bool = False,
) -> GroundedPullRequest:
    """Fetch PR diff and full text for each changed file at ``head_sha``.

    ``removed`` files have no snapshot at head; by default we only attach the per-file patch
    from the files API. Set ``include_removed_base_snapshot=True`` to load the last version at
    ``base_sha`` for deleted paths (extra API call per removal).
    """
    unified = fetch_pr_unified_diff(owner, repo, pr_number, token, api_base=api_base)
    raw_files = iter_pull_request_files(owner, repo, pr_number, token, api_base=api_base)

    grounded: list[GroundedFile] = []
    for entry in raw_files:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("filename") or "")
        status = str(entry.get("status") or "modified")
        patch_hunk = entry.get("patch")
        patch_str = str(patch_hunk) if isinstance(patch_hunk, str) else None
        prev = entry.get("previous_filename")
        prev_str = str(prev) if isinstance(prev, str) else None

        full_text: str | None = None
        is_binary = False
        fetch_error: str | None = None

        if status == "removed":
            if include_removed_base_snapshot and path:
                full_text, is_binary, fetch_error = fetch_file_at_ref(
                    owner, repo, path, base_sha, token, api_base=api_base
                )
            else:
                fetch_error = None
                full_text = None
        elif path:
            full_text, is_binary, fetch_error = fetch_file_at_ref(
                owner, repo, path, head_sha, token, api_base=api_base
            )

        grounded.append(
            GroundedFile(
                path=path,
                status=status,
                patch_hunk=patch_str,
                full_text=full_text,
                is_binary=is_binary,
                fetch_error=fetch_error,
                previous_path=prev_str,
            )
        )

    return GroundedPullRequest(
        owner=owner,
        repo=repo,
        number=pr_number,
        head_sha=head_sha,
        base_sha=base_sha,
        unified_diff=unified,
        files=grounded,
    )


def parse_repository_owner_repo(payload: dict[str, Any]) -> tuple[str, str]:
    """Resolve ``owner``, ``repo`` from a webhook ``repository`` object."""
    repo_obj = payload.get("repository")
    if not isinstance(repo_obj, dict):
        raise ValueError("payload.repository is missing or not an object")
    full = repo_obj.get("full_name")
    if isinstance(full, str) and "/" in full:
        owner, _, name = full.partition("/")
        if owner and name:
            return owner, name
    owner_obj = repo_obj.get("owner")
    name = repo_obj.get("name")
    if isinstance(owner_obj, dict) and isinstance(name, str):
        login = owner_obj.get("login")
        if isinstance(login, str) and login:
            return login, name
    raise ValueError("Could not resolve owner/repo from payload.repository")


def parse_pull_request_refs(payload: dict[str, Any]) -> tuple[int, str, str]:
    """Return PR number, head SHA, base SHA from ``payload['pull_request']``."""
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        raise ValueError("payload.pull_request is missing or not an object")
    num = pr.get("number")
    head = pr.get("head")
    base = pr.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise ValueError("pull_request.head / pull_request.base missing")
    hs = head.get("sha")
    bs = base.get("sha")
    if not isinstance(num, int) or not isinstance(hs, str) or not isinstance(bs, str):
        raise ValueError("pull_request number or SHAs invalid")
    return num, hs, bs


def grounded_context_from_pull_request_event(
    payload: dict[str, Any],
    access_token: str,
    *,
    api_base: str = _DEFAULT_API_BASE,
    action_filter: frozenset[str] | None = frozenset({"opened"}),
    include_removed_base_snapshot: bool = False,
) -> GroundedPullRequest:
    """Build :class:`GroundedPullRequest` from a ``pull_request`` webhook JSON body.

    If ``action_filter`` is set (default: ``opened`` only), raises :class:`ValueError` when
    ``payload['action']`` is not allowed — skip early for irrelevant hooks.
    Pass ``action_filter=None`` to accept any action.
    """
    action = payload.get("action")
    if action_filter is not None:
        if action not in action_filter:
            raise ValueError(f"pull_request action {action!r} not in allowed set {sorted(action_filter)}")
    owner, repo = parse_repository_owner_repo(payload)
    number, head_sha, base_sha = parse_pull_request_refs(payload)
    return build_grounded_pull_request(
        owner,
        repo,
        number,
        head_sha,
        base_sha,
        access_token,
        api_base=api_base,
        include_removed_base_snapshot=include_removed_base_snapshot,
    )
