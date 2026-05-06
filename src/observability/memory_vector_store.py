"""ChromaDB-backed memory for successful grounded reviews (Ollama local embeddings)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

_LOG = logging.getLogger(__name__)

_COLLECTION_NAME = "octo_memory"
_DEFAULT_EMBED_MODEL = "nomic-embed-text"
_MAX_DOC_CHARS = 14_000
# Chunking: keep segments embedder-friendly; overlap preserves cross-boundary context.
_DEFAULT_CHUNK_CHARS = 1600
_DEFAULT_CHUNK_OVERLAP = 120

# First markdown heading that starts a "fixes" section (case-insensitive title match).
_FIXES_SECTION = re.compile(
    r"(?mi)^#{1,3}\s*("
    r"fixes|recommended fixes|remediation|suggested fixes"
    r")(?:\b.*)?\s*$"
)


def vector_memory_enabled() -> bool:
    v = (os.environ.get("OCTO_VECTOR_MEMORY") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _repo_root() -> Path:
    env = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _default_persist_dir() -> Path:
    override = (os.environ.get("OCTO_CHROMA_DATA_DIR") or os.environ.get("OCTO_CHROMA_PERSIST_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _repo_root() / ".local" / "chroma_data"


def chroma_persist_path() -> Path:
    """Directory used for persistent ChromaDB data (same as VectorMemory)."""
    return _default_persist_dir()


def _persistent_chroma_client(persist_directory: Path):
    import chromadb

    persist_directory.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(persist_directory))


def split_findings_and_fixes(markdown: str) -> tuple[str, str]:
    """Split review markdown into (findings_block, fixes_block).

    A ``## Fixes``-style heading starts the fixes block; content before that is treated as
    findings (summary, evidence, issues, etc.). If no fixes heading exists, the full text
    is returned as findings only.
    """
    text = markdown.strip()
    if not text:
        return "", ""
    m = _FIXES_SECTION.search(text)
    if not m:
        return text, ""
    findings = text[: m.start()].strip()
    fixes_body = text[m.end() :].strip()
    return findings, fixes_body


def chunk_text(
    text: str,
    *,
    max_chars: int = _DEFAULT_CHUNK_CHARS,
    overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Greedy paragraph packing with hard-split fallback for very long paragraphs."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        p = para.strip()
        if not p:
            continue
        candidate = f"{buf}\n\n{p}" if buf else p
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            parts.append(buf)
        if len(p) <= max_chars:
            buf = p
        else:
            step = max(1, max_chars - overlap)
            i = 0
            while i < len(p):
                parts.append(p[i : i + max_chars])
                i += step
            buf = ""
    if buf:
        parts.append(buf)
    return parts if parts else [text[:max_chars]]


def _ollama_embed(text: str, ollama_base_url: str, model: str) -> list[float]:
    base = ollama_base_url.rstrip("/")
    url = f"{base}/api/embeddings"
    payload: dict[str, Any] = {"model": model, "prompt": text}
    with httpx.Client(timeout=120.0) as client:
        r = client.post(url, json=payload)
        if r.status_code >= 400:
            payload_alt = {"model": model, "input": text}
            r = client.post(url, json=payload_alt)
        r.raise_for_status()
        data = r.json()
    emb = data.get("embedding")
    if emb is None and isinstance(data.get("embeddings"), list) and data["embeddings"]:
        emb = data["embeddings"][0]
    if not isinstance(emb, list):
        raise RuntimeError("Ollama embeddings response missing embedding vector")
    return [float(x) for x in emb]


class VectorMemory:
    """Chunked embeddings via local Ollama; persistent Chroma collection ``octo_memory``.

    Each chunk stores ``is_verified`` metadata: ``True`` only when the remediation
    :class:`remediation.rescan_loop.RescanLoop` passed for that ingestion (see ``rescan_loop_passed``).
    Queries return **verified** chunks only (``where is_verified == True``).
    """

    def __init__(
        self,
        *,
        ollama_base_url: str,
        embed_model: str | None = None,
        collection_name: str = _COLLECTION_NAME,
        persist_directory: Path | None = None,
    ) -> None:
        self._ollama_base = ollama_base_url.rstrip("/")
        self._embed_model = (embed_model or os.environ.get("OCTO_EMBEDDING_MODEL") or _DEFAULT_EMBED_MODEL).strip()
        self._persist_dir = persist_directory or _default_persist_dir()
        self._client = _persistent_chroma_client(self._persist_dir)
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"description": "Octo-spork review chunks (findings + fixes), local-first"},
        )

    def embed(self, text: str) -> list[float]:
        return _ollama_embed(text, self._ollama_base, self._embed_model)

    def index_review(
        self,
        *,
        owner: str,
        repo: str,
        revision_sha: str,
        query: str,
        answer_markdown: str,
        review_model: str,
        rescan_loop_passed: bool = False,
    ) -> list[str]:
        """Chunk findings and fixes, embed with Ollama, upsert into Chroma. Returns inserted ids."""
        findings_text, fixes_text = split_findings_and_fixes(answer_markdown)
        ids: list[str] = []
        embeddings: list[list[float]] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []
        verified = bool(rescan_loop_passed)

        for kind, body in (("findings", findings_text), ("fixes", fixes_text)):
            if not body.strip():
                continue
            for idx, chunk in enumerate(
                chunk_text(
                    body,
                    max_chars=int(os.environ.get("OCTO_VECTOR_CHUNK_CHARS") or _DEFAULT_CHUNK_CHARS),
                    overlap=int(os.environ.get("OCTO_VECTOR_CHUNK_OVERLAP") or _DEFAULT_CHUNK_OVERLAP),
                )
            ):
                rid = self._chunk_id(owner, repo, revision_sha, query, kind, idx, chunk)
                ids.append(rid)
                embeddings.append(self.embed(chunk))
                documents.append(chunk[:_MAX_DOC_CHARS])
                metadatas.append(
                    {
                        "owner": owner[:256],
                        "repo": repo[:256],
                        "revision_sha": revision_sha[:40],
                        "kind": kind,
                        "chunk_index": idx,
                        "query_head": query[:512],
                        "review_model": review_model[:128],
                        "is_verified": verified,
                    }
                )

        if ids:
            self._collection.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
        return ids

    def add_memory(
        self,
        *,
        owner: str,
        repo: str,
        revision_sha: str,
        query: str,
        answer_markdown: str,
        review_model: str,
        rescan_loop_passed: bool = False,
    ) -> list[str]:
        """Persist review chunks; sets ``is_verified`` only when *rescan_loop_passed* is ``True``."""
        return self.index_review(
            owner=owner,
            repo=repo,
            revision_sha=revision_sha,
            query=query,
            answer_markdown=answer_markdown,
            review_model=review_model,
            rescan_loop_passed=rescan_loop_passed,
        )

    def upsert_review(
        self,
        *,
        owner: str,
        repo: str,
        revision_sha: str,
        query: str,
        answer_markdown: str,
        review_model: str,
        rescan_loop_passed: bool = False,
    ) -> str:
        """Backward-compatible alias: returns first chunk id (or empty string)."""
        out = self.index_review(
            owner=owner,
            repo=repo,
            revision_sha=revision_sha,
            query=query,
            answer_markdown=answer_markdown,
            review_model=review_model,
            rescan_loop_passed=rescan_loop_passed,
        )
        return out[0] if out else ""

    def query_memory(self, query_text: str, k: int = 5) -> list[dict[str, Any]]:
        """Retrieve similar chunks that were verified by a passing RescanLoop (``is_verified=True`` only)."""
        return self.similar_findings(query_text, k=k)

    def similar_findings(self, query_text: str, k: int = 5) -> list[dict[str, Any]]:
        emb = self.embed(query_text.strip() or " ")
        res = self._collection.query(
            query_embeddings=[emb],
            n_results=max(1, min(k, 50)),
            where={"is_verified": True},
            include=["documents", "metadatas", "distances"],
        )
        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        dists = res.get("distances") or []
        if not ids or not ids[0]:
            return []
        out: list[dict[str, Any]] = []
        for i in range(len(ids[0])):
            m = metas[0][i] if metas and metas[0] else {}
            drow = docs[0][i] if docs and docs[0] else ""
            dist = None
            if dists and dists[0] and i < len(dists[0]):
                dist = float(dists[0][i])
            owner = str(m.get("owner") or "")
            repo = str(m.get("repo") or "")
            kind = str(m.get("kind") or "")
            out.append(
                {
                    "id": ids[0][i],
                    "owner": owner,
                    "repo": repo,
                    "repo_full": f"{owner}/{repo}".strip("/"),
                    "revision_sha": str(m.get("revision_sha") or ""),
                    "query_head": str(m.get("query_head") or ""),
                    "kind": kind,
                    "is_verified": bool(m.get("is_verified")),
                    "excerpt": str(drow or "")[:1500],
                    "distance": dist,
                }
            )
        return out

    @staticmethod
    def _chunk_id(
        owner: str,
        repo: str,
        revision_sha: str,
        query: str,
        kind: str,
        chunk_index: int,
        chunk_text_content: str,
    ) -> str:
        h = hashlib.sha256(
            f"{owner}|{repo}|{revision_sha}|{query}|{kind}|{chunk_index}|{chunk_text_content}".encode("utf-8")
        ).hexdigest()
        return f"mem_{h[:24]}"


MemoryVectorStore = VectorMemory
MemoryManager = VectorMemory


def attach_similar_historical_findings(
    query: str,
    snapshot: dict[str, Any],
    ollama_base_url: str,
) -> None:
    """Populate ``snapshot['vector_memory_similar_block']`` for the review prompt."""
    if not vector_memory_enabled():
        return
    try:
        store = VectorMemory(ollama_base_url=ollama_base_url)
    except Exception as exc:
        _LOG.debug("vector memory: Chroma init failed: %s", exc)
        return
    owner = str(snapshot.get("owner") or "unknown")
    repo = str(snapshot.get("repo") or "unknown")
    qtext = f"{query}\n\nRepository context: {owner}/{repo}"
    try:
        hits = store.query_memory(qtext, k=int(os.environ.get("OCTO_VECTOR_MEMORY_TOP_K") or "5"))
    except Exception as exc:
        _LOG.debug("vector memory: similarity query failed: %s", exc)
        return
    if not hits:
        return
    lines: list[str] = []
    for i, h in enumerate(hits, 1):
        dist = h.get("distance")
        dist_s = f"{dist:.4f}" if isinstance(dist, (int, float)) else "n/a"
        rev = str(h.get("revision_sha") or "")
        rev_s = (rev[:10] + "…") if len(rev) > 10 else rev
        excerpt = str(h.get("excerpt") or "").strip().replace("\r\n", "\n")
        if len(excerpt) > 900:
            excerpt = excerpt[:900] + "…"
        qh = str(h.get("query_head") or "").strip()
        qh_line = f"\n   _Prior request:_ {qh}" if qh else ""
        kind = str(h.get("kind") or "").strip()
        kind_tag = f" _[{kind}]_" if kind else ""
        lines.append(
            f"{i}. **{h.get('repo_full') or 'unknown'}**{kind_tag} (revision `{rev_s}`, distance {dist_s}){qh_line}\n   {excerpt}"
        )
    snapshot["vector_memory_similar_block"] = "\n\n".join(lines)


def index_successful_grounded_review(
    *,
    query: str,
    model: str,
    ollama_base_url: str,
    result: dict[str, Any],
) -> None:
    """Persist a successful review outcome for future similarity search."""
    if not vector_memory_enabled():
        return
    if not result.get("success"):
        return
    snap = result.get("snapshot")
    if not isinstance(snap, dict):
        return
    answer = str(result.get("answer") or "").strip()
    if not answer:
        return
    owner = str(snap.get("owner") or "unknown")
    repo = str(snap.get("repo") or "unknown")
    rev_raw = snap.get("revision_sha") or (snap.get("coverage") or {}).get("revision_sha")
    revision_sha = str(rev_raw).strip()[:40] if rev_raw else ""
    rescan_passed = bool(result.get("rescan_loop_passed")) or bool(snap.get("rescan_loop_passed"))
    try:
        store = VectorMemory(ollama_base_url=ollama_base_url)
        store.index_review(
            owner=owner,
            repo=repo,
            revision_sha=revision_sha,
            query=query,
            answer_markdown=answer,
            review_model=model,
            rescan_loop_passed=rescan_passed,
        )
    except Exception as exc:
        _LOG.debug("vector memory: index failed: %s", exc)
