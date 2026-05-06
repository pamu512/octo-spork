"""Correction ledger: store developer corrections to AI output as negative examples (Chroma + Ollama)."""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

_COLLECTION_NAME = "octo_corrections"
_PREVIEW_LEN = 400
_DOC_MAX = 12_000

# Default top-k for prompt injection
_DEFAULT_LESSONS_K = 5


def correction_ledger_enabled() -> bool:
    v = (os.environ.get("OCTO_CORRECTION_LEDGER") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _ollama_base_url(explicit: str | None = None) -> str | None:
    if explicit and str(explicit).strip():
        return str(explicit).strip().rstrip("/")
    base = (
        (os.environ.get("OLLAMA_LOCAL_URL") or os.environ.get("OLLAMA_BASE_URL") or "")
        .strip()
        .rstrip("/")
    )
    return base or None


def _default_embedding_model() -> str:
    return (os.environ.get("OCTO_EMBEDDING_MODEL") or "nomic-embed-text").strip()


def _fingerprint(rejected: str, corrected: str, repo_full: str, comment_id: int | None) -> str:
    return hashlib.sha256(
        f"{comment_id}|{repo_full}|{rejected}|{corrected}".encode("utf-8")
    ).hexdigest()


def _short(s: str, max_chars: int = 160) -> str:
    t = " ".join((s or "").strip().split())
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1] + "…"


class CorrectionLedger:
    """Persistent negative examples: rejected assistant text vs developer-preferred text."""

    def __init__(
        self,
        *,
        ollama_base_url: str,
        embed_model: str | None = None,
        persist_directory: Path | None = None,
    ) -> None:
        from observability.memory_vector_store import _ollama_embed, _persistent_chroma_client, chroma_persist_path

        self._ollama_base = ollama_base_url.rstrip("/")
        self._embed_model = (embed_model or _default_embedding_model()).strip()
        persist = persist_directory or chroma_persist_path()
        self._client = _persistent_chroma_client(persist)
        self._collection = self._client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"description": "Developer corrections to AI PR comments (negative examples)"},
        )
        self._embed = lambda text: _ollama_embed(text, self._ollama_base, self._embed_model)

    def record_negative_example(
        self,
        *,
        rejected_text: str,
        corrected_text: str,
        repo_full: str,
        editor: str,
        source: str,
        comment_id: int | None = None,
    ) -> str | None:
        """Embed and upsert one correction. Returns id or None if skipped."""
        rej = (rejected_text or "").strip()
        cor = (corrected_text or "").strip()
        if not rej:
            return None
        if not cor:
            cor = "(developer removed or rejected the suggestion without substituting new text in this capture)"

        fp = _fingerprint(rej, cor, repo_full, comment_id)
        rid = f"neg_{fp[:24]}"

        doc = (
            "Negative example (assistant output later corrected by a developer).\n\n"
            "Previously suggested (patterns to avoid repeating):\n"
            f"{rej[:_DOC_MAX]}\n\n"
            "Developer-preferred outcome:\n"
            f"{cor[:_DOC_MAX]}\n"
        )
        try:
            emb = self._embed(doc)
        except Exception as exc:
            _LOG.warning("correction_ledger: embed failed: %s", exc)
            return None

        meta: dict[str, Any] = {
            "record_type": "negative_example",
            "repo_full": repo_full[:512],
            "editor": editor[:256],
            "source": source[:64],
            "rejected_preview": rej[:_PREVIEW_LEN],
            "corrected_preview": cor[:_PREVIEW_LEN],
            "fingerprint": fp[:64],
        }
        if comment_id is not None:
            meta["comment_id"] = str(int(comment_id))

        try:
            self._collection.upsert(ids=[rid], embeddings=[emb], documents=[doc], metadatas=[meta])
        except Exception as exc:
            _LOG.warning("correction_ledger: chroma upsert failed: %s", exc)
            return None

        _LOG.info("correction_ledger: recorded negative example id=%s repo=%s", rid, repo_full)
        return rid

    def similar_lessons(self, query_text: str, k: int | None = None) -> list[dict[str, Any]]:
        """Return nearest negative examples for prompt injection."""
        k = k if k is not None else int(os.environ.get("OCTO_CORRECTION_LEDGER_TOP_K") or _DEFAULT_LESSONS_K)
        k = max(1, min(k, 25))
        emb = self._embed(query_text.strip() or " ")
        res = self._collection.query(
            query_embeddings=[emb],
            n_results=k,
            include=["documents", "metadatas", "distances"],
        )
        ids = res.get("ids") or []
        metas = res.get("metadatas") or []
        dists = res.get("distances") or []
        if not ids or not ids[0]:
            return []
        out: list[dict[str, Any]] = []
        for i in range(len(ids[0])):
            m = metas[0][i] if metas and metas[0] else {}
            dist = None
            if dists and dists[0] and i < len(dists[0]):
                dist = float(dists[0][i])
            out.append(
                {
                    "id": ids[0][i],
                    "repo_full": str(m.get("repo_full") or ""),
                    "rejected_preview": str(m.get("rejected_preview") or ""),
                    "corrected_preview": str(m.get("corrected_preview") or ""),
                    "distance": dist,
                }
            )
        return out


def format_lessons_learned_section(hits: list[dict[str, Any]]) -> str:
    """Markdown block: explicit Avoid X / corrected to Y lines."""
    if not hits:
        return ""
    lines: list[str] = [
        "## Lessons Learned",
        "",
        "_The developer previously adjusted AI-generated review text. Prefer the corrected patterns below._",
        "",
    ]
    for h in hits:
        px = _short(str(h.get("rejected_preview") or ""), 200)
        py = _short(str(h.get("corrected_preview") or ""), 200)
        if not px and not py:
            continue
        repo = str(h.get("repo_full") or "").strip()
        repo_note = f" _(repository `{repo}`)_" if repo else ""
        lines.append(
            f"- Avoid **{px}**, as the developer previously corrected it to **{py}**{repo_note}."
        )
    lines.append("")
    return "\n".join(lines).strip() + "\n\n"


def lessons_learned_markdown(query_text: str, ollama_base_url: str | None = None, *, k: int | None = None) -> str:
    """Full markdown section for injection, or empty string."""
    if not correction_ledger_enabled():
        return ""
    base = _ollama_base_url(ollama_base_url)
    if not base:
        return ""
    try:
        ledger = CorrectionLedger(ollama_base_url=base)
        hits = ledger.similar_lessons(query_text, k=k)
    except Exception as exc:
        _LOG.debug("correction_ledger: similar_lessons failed: %s", exc)
        return ""
    return format_lessons_learned_section(hits)


def attach_lessons_learned_to_snapshot(
    query: str,
    snapshot: dict[str, Any],
    ollama_base_url: str,
) -> None:
    """Set ``snapshot['correction_ledger_lessons_block']`` when enabled."""
    if not correction_ledger_enabled():
        return
    owner = str(snapshot.get("owner") or "unknown")
    repo = str(snapshot.get("repo") or "unknown")
    qtext = f"{query}\n\nRepository context: {owner}/{repo}"
    block = lessons_learned_markdown(qtext, ollama_base_url)
    if block.strip():
        snapshot["correction_ledger_lessons_block"] = block.strip()


def record_negative_example_from_comment_edit(
    *,
    before: str,
    after: str,
    repo_full: str,
    editor_login: str,
    comment_id: int | None = None,
) -> bool:
    """Store an edited AI PR comment as a negative example (webhook path)."""
    if not correction_ledger_enabled():
        return False
    if not before.strip() or not after.strip():
        return False
    if before.strip() == after.strip():
        return False
    base = _ollama_base_url(None)
    if not base:
        _LOG.info("correction_ledger: no Ollama base URL; skip record")
        return False
    try:
        ledger = CorrectionLedger(ollama_base_url=base)
        rid = ledger.record_negative_example(
            rejected_text=before,
            corrected_text=after,
            repo_full=repo_full,
            editor=editor_login,
            source="issue_comment_edited",
            comment_id=comment_id,
        )
    except Exception as exc:
        _LOG.debug("correction_ledger: record failed: %s", exc)
        return False
    return rid is not None


def cli_record(
    *,
    rejected: str,
    corrected: str,
    repo: str,
    editor: str,
    source: str,
) -> int:
    """CLI entry: record one correction. Returns process exit code."""
    if not correction_ledger_enabled():
        sys.stderr.write("Set OCTO_CORRECTION_LEDGER=1 to enable recording.\n")
        return 1
    base = _ollama_base_url(None)
    if not base:
        sys.stderr.write("Set OLLAMA_BASE_URL or OLLAMA_LOCAL_URL for embeddings.\n")
        return 1
    ledger = CorrectionLedger(ollama_base_url=base)
    rid = ledger.record_negative_example(
        rejected_text=rejected,
        corrected_text=corrected,
        repo_full=repo,
        editor=editor,
        source=source,
        comment_id=None,
    )
    if not rid:
        sys.stderr.write("Nothing recorded (empty rejected text or embed/upser failure).\n")
        return 2
    print(rid)
    return 0
