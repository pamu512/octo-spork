"""Learned developer style preferences: YAML persistence + Ollama merge + prompt injection."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

_DEFAULT_MARKERS = (
    "Posted by octo-spork webhook",
    "octo-spork",
    "Review Refiner",
)


def style_learn_enabled() -> bool:
    return os.environ.get("OCTO_STYLE_LEARN_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def _correction_ledger_env_enabled() -> bool:
    return os.environ.get("OCTO_CORRECTION_LEDGER", "").strip().lower() in {"1", "true", "yes", "on"}


def style_guide_injection_enabled() -> bool:
    return os.environ.get("OCTO_STYLE_GUIDE_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def workspace_root() -> Path:
    raw = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def style_prefs_path() -> Path:
    """``.local/style_prefs.yaml`` under the workspace root."""
    return workspace_root() / ".local" / "style_prefs.yaml"


def _markers() -> tuple[str, ...]:
    raw = (os.environ.get("OCTO_STYLE_COMMENT_MARKERS") or "").strip()
    if not raw:
        return _DEFAULT_MARKERS
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts if parts else _DEFAULT_MARKERS


def looks_like_ai_generated_comment(text: str) -> bool:
    """True when text appears to be an octo-spork / automated review comment."""
    t = text or ""
    return any(m in t for m in _markers())


def sender_login(payload: dict[str, Any]) -> str | None:
    s = payload.get("sender")
    if isinstance(s, dict):
        login = s.get("login")
        if isinstance(login, str) and login.strip():
            return login.strip()
    return None


def _repository_full_name(payload: dict[str, Any]) -> str | None:
    repo = payload.get("repository")
    if isinstance(repo, dict):
        fn = repo.get("full_name")
        if isinstance(fn, str) and fn.strip():
            return fn.strip()
    return None


def load_prefs_file() -> dict[str, Any]:
    path = style_prefs_path()
    if not path.is_file():
        return {"version": 1, "developer_style_guide": "", "recent_corrections": []}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _LOG.warning("style_prefs: could not load %s: %s", path, exc)
        return {"version": 1, "developer_style_guide": "", "recent_corrections": []}
    if not isinstance(data, dict):
        return {"version": 1, "developer_style_guide": "", "recent_corrections": []}
    data.setdefault("version", 1)
    data.setdefault("developer_style_guide", "")
    data.setdefault("recent_corrections", [])
    return data


def _save_prefs(data: dict[str, Any]) -> None:
    path = style_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import yaml

    text = yaml.safe_dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _ollama_merge_guide(
    *,
    existing_guide: str,
    before: str,
    after: str,
    repo: str,
    editor: str,
) -> str:
    base = (
        os.environ.get("OLLAMA_BASE_URL")
        or os.environ.get("OCTO_OLLAMA_URL")
        or "http://127.0.0.1:11434"
    ).rstrip("/")
    model = (os.environ.get("OCTO_STYLE_GUIDE_MODEL") or os.environ.get("OCTO_REVIEW_MODEL") or "llama3.2").strip()

    sys_prompt = (
        "You maintain a concise Developer Style Guide as markdown bullet lists for code reviewers. "
        "Merge new feedback into the guide; remove duplicates; keep 4–12 bullets unless more are essential. "
        "Output ONLY the guide markdown (bullets, short sentences). No preamble or fences."
    )
    user_prompt = f"""Repository: {repo}
Editor (human): {editor}

Existing guide (may be empty):
---
{existing_guide.strip() or "(none yet)"}
---

Before (previous comment text):
---
{before.strip()[:12000]}
---

After (corrected comment text):
---
{after.strip()[:12000]}
---

Produce the updated **Developer Style Guide** as markdown bullets reflecting team preferences implied by the correction.
"""

    from observability.privacy_filter import redact_for_llm, unredact_response

    user_prompt, priv_map = redact_for_llm(user_prompt)

    import httpx

    chat_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.2},
    }
    url = f"{base}/api/chat"
    with httpx.Client(timeout=120.0) as client:
        r = client.post(url, json=chat_payload)
        r.raise_for_status()
        body = r.json()
    msg = body.get("message")
    text = ""
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        text = str(msg["content"]).strip()
    if not text:
        text = str(body.get("response") or "").strip()
    if not text:
        raise RuntimeError("empty Ollama response for style guide merge")
    if priv_map:
        text = unredact_response(text, priv_map)
    max_chars = int(os.environ.get("OCTO_STYLE_GUIDE_MAX_CHARS", "12000"))
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n…"
    return text


def _correction_fingerprint(before: str, after: str, comment_id: int | None) -> str:
    h = hashlib.sha256(f"{comment_id}:{before}:{after}".encode("utf-8")).hexdigest()[:16]
    return h


def apply_learned_correction(
    *,
    before: str,
    after: str,
    repo_full: str,
    editor_login: str,
    comment_id: int | None = None,
) -> bool:
    """Merge Before/After into ``.local/style_prefs.yaml`` via Ollama. Returns True if saved."""
    if not before.strip() or not after.strip():
        return False
    if before.strip() == after.strip():
        return False

    data = load_prefs_file()
    recent = data.get("recent_corrections")
    if not isinstance(recent, list):
        recent = []
    fp = _correction_fingerprint(before, after, comment_id)
    for item in recent[-50:]:
        if isinstance(item, dict) and item.get("fingerprint") == fp:
            _LOG.info("style_prefs: duplicate correction fingerprint=%s; skip", fp)
            return False

    existing = str(data.get("developer_style_guide") or "")
    try:
        merged = _ollama_merge_guide(
            existing_guide=existing,
            before=before,
            after=after,
            repo=repo_full,
            editor=editor_login,
        )
    except Exception as exc:
        _LOG.exception("style_prefs: Ollama merge failed: %s", exc)
        return False

    entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "repo": repo_full,
        "editor": editor_login,
        "fingerprint": fp,
        "before_excerpt": before[:2000],
        "after_excerpt": after[:2000],
    }
    recent.append(entry)
    data["developer_style_guide"] = merged
    data["recent_corrections"] = recent[-100:]
    data["updated_at"] = entry["at"]

    try:
        _save_prefs(data)
    except Exception as exc:
        _LOG.exception("style_prefs: save failed: %s", exc)
        return False

    _LOG.info(
        "style_prefs: updated developer_style_guide from correction by %s on %s",
        editor_login,
        repo_full,
    )
    return True


def style_guide_system_prompt_suffix() -> str:
    """Appendix for strict JSON / system prompts (token-efficient)."""
    if not style_guide_injection_enabled():
        return ""
    data = load_prefs_file()
    guide = str(data.get("developer_style_guide") or "").strip()
    if not guide:
        return ""
    return (
        "\n\n## Developer style preferences (learned from human corrections)\n"
        "Apply these team preferences when wording findings and severity labels:\n\n"
        f"{guide}\n"
    )


def format_style_guide_block_for_review() -> str:
    """Markdown block for grounded / narrative reviews (full paragraph)."""
    if not style_guide_injection_enabled():
        return ""
    data = load_prefs_file()
    guide = str(data.get("developer_style_guide") or "").strip()
    if not guide:
        return ""
    return (
        "### Developer style preferences (learned)\n\n"
        "_These bullets were distilled from human corrections to prior automated reviews._\n\n"
        f"{guide}\n"
    )


def process_issue_comment_edited_payload(payload: dict[str, Any]) -> None:
    """Handle GitHub ``issue_comment`` ``edited`` delivery (sync)."""
    if not style_learn_enabled() and not _correction_ledger_env_enabled():
        return

    allowed_raw = (os.environ.get("ALLOWED_USERS") or "").strip()
    if not allowed_raw:
        _LOG.warning("style_prefs: ALLOWED_USERS empty; refusing style learn (configure allowed editors).")
        return

    allowed = frozenset(p.strip().lower() for p in allowed_raw.split(",") if p.strip())
    sender = (sender_login(payload) or "").lower()
    if sender not in allowed:
        _LOG.info("style_prefs: sender %s not in ALLOWED_USERS; skip", sender or "?")
        return

    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return
    after = comment.get("body")
    if not isinstance(after, str):
        return

    changes = payload.get("changes")
    if not isinstance(changes, dict):
        return
    body_change = changes.get("body")
    if not isinstance(body_change, dict):
        return
    before = body_change.get("from")
    if not isinstance(before, str):
        return

    if not looks_like_ai_generated_comment(before) and not looks_like_ai_generated_comment(after):
        _LOG.info("style_prefs: comment does not match AI/bot markers; skip")
        return

    repo = _repository_full_name(payload) or "unknown/unknown"
    cid = comment.get("id")
    comment_id = int(cid) if isinstance(cid, int) else None

    try:
        from github_bot.correction_ledger import record_negative_example_from_comment_edit

        record_negative_example_from_comment_edit(
            before=before,
            after=after,
            repo_full=repo,
            editor_login=sender or "unknown",
            comment_id=comment_id,
        )
    except ImportError:
        pass
    except Exception:
        _LOG.debug("correction_ledger: record skipped", exc_info=True)

    if not style_learn_enabled():
        return

    apply_learned_correction(
        before=before,
        after=after,
        repo_full=repo,
        editor_login=sender or "unknown",
        comment_id=comment_id,
    )


def should_learn_style_from_issue_comment(headers: dict[str, str], payload: dict[str, Any]) -> bool:
    if not style_learn_enabled() and not _correction_ledger_env_enabled():
        return False
    event = (headers.get("X-GitHub-Event") or headers.get("x-github-event") or "").strip()
    if event != "issue_comment":
        return False
    if str(payload.get("action") or "").strip().lower() != "edited":
        return False
    issue = payload.get("issue")
    if not isinstance(issue, dict) or not issue.get("pull_request"):
        return False
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        return False
    body_change = changes.get("body")
    if not isinstance(body_change, dict):
        return False
    if "from" not in body_change:
        return False
    return True
