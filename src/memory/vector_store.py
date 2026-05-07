"""ChromaDB-backed ledger vectors with a strict remediation metadata schema."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, TypedDict

_LOG = logging.getLogger(__name__)

_DEFAULT_COLLECTION_NAME = "octo_ledger"


class LedgerMetadata(TypedDict):
    """Required metadata on every document stored in the ledger collection."""

    cve_id: str
    file_path: str
    is_verified: bool


if TYPE_CHECKING:
    import chromadb


def coerce_ledger_metadata(raw: Mapping[str, Any]) -> LedgerMetadata:
    """Normalise arbitrary mapping into :class:`LedgerMetadata`; raises :exc:`KeyError` if missing keys."""

    return LedgerMetadata(
        cve_id=str(raw["cve_id"]),
        file_path=str(raw["file_path"]),
        is_verified=bool(raw["is_verified"]),
    )


def migrate_reset_ledger_collection(
    client: "chromadb.PersistentClient",
    collection_name: str,
    *,
    log_wipe: bool = True,
) -> None:
    """Delete ``collection_name`` if it exists so legacy rows cannot violate :class:`LedgerMetadata`.

    Call this **before** :func:`init_collection` (which invokes it by default) or from maintenance
    scripts when enforcing schema migrations that cannot be done in place.
    """
    names = {c.name for c in client.list_collections()}
    if collection_name not in names:
        if log_wipe:
            _LOG.debug(
                "Chroma ledger migration: collection %r absent; nothing to drop",
                collection_name,
            )
        return
    client.delete_collection(name=collection_name)
    if log_wipe:
        _LOG.warning(
            "Chroma ledger migration: wiped collection %r to enforce metadata schema "
            "{cve_id: str, file_path: str, is_verified: bool}",
            collection_name,
        )


def init_collection(
    persist_directory: str | Path,
    *,
    collection_name: str = _DEFAULT_COLLECTION_NAME,
    skip_migration_wipe: bool = False,
) -> Any:
    """Return a fresh Chroma collection pinned to the ledger metadata schema.

    Creates ``persist_directory``, connects with ``chromadb.PersistentClient``, optionally runs
    :func:`migrate_reset_ledger_collection`, then ``create_collection`` with collection-level
    metadata documenting the schema contract.

    Parameters
    ----------
    persist_directory
        On-disk path for Chroma persistence.
    collection_name
        Logical collection name (default ``octo_ledger``).
    skip_migration_wipe
        When ``True``, skip the destructive migration step (tests only).
    """
    import chromadb

    root = Path(persist_directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(root))

    if not skip_migration_wipe:
        migrate_reset_ledger_collection(client, collection_name)

    collection = client.create_collection(
        name=collection_name,
        metadata={
            "description": "Octo-spork remediation ledger (CVE-scoped chunks)",
            "metadata_schema": "cve_id:str,file_path:str,is_verified:bool",
        },
    )
    return collection


def _repo_root() -> Path:
    env = (os.environ.get("OCTO_SPORK_REPO_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _ledger_persist_path() -> Path:
    override = (
        os.environ.get("OCTO_LEDGER_CHROMA_DIR")
        or os.environ.get("OCTO_CHROMA_DATA_DIR")
        or os.environ.get("OCTO_CHROMA_PERSIST_DIR")
        or ""
    ).strip()
    if override:
        return Path(override).expanduser().resolve()
    return _repo_root() / ".local" / "chroma_ledger"


def _get_ledger_collection() -> Any | None:
    """Open the existing ledger collection, or ``None`` if the DB or collection is absent."""
    import chromadb

    persist = _ledger_persist_path()
    if not persist.exists():
        return None
    name = (os.environ.get("OCTO_LEDGER_COLLECTION") or _DEFAULT_COLLECTION_NAME).strip()
    client = chromadb.PersistentClient(path=str(persist))
    try:
        return client.get_collection(name=name)
    except Exception:
        return None


def _rows_from_ledger_query(res: Mapping[str, Any]) -> list[dict[str, Any]]:
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
        dist: float | None = None
        if dists and dists[0] and i < len(dists[0]):
            dist = float(dists[0][i])
        out.append(
            {
                "id": ids[0][i],
                "document": drow,
                "cve_id": str(m.get("cve_id") or ""),
                "file_path": str(m.get("file_path") or ""),
                "is_verified": bool(m.get("is_verified")),
                "distance": dist,
            }
        )
    return out


def query_verified_patterns(query_text: str) -> list[dict[str, Any]]:
    """Similarity search over the ledger; returns **only** verified rows (never unverified fallbacks).

    Uses Ollama embeddings (``OLLAMA_LOCAL_URL``, ``OCTO_EMBEDDING_MODEL``) and the ledger Chroma
    path (``OCTO_LEDGER_CHROMA_DIR`` or ``OCTO_CHROMA_DATA_DIR`` / default ``.local/chroma_ledger``).
    """
    from observability.memory_vector_store import _ollama_embed

    coll = _get_ledger_collection()
    if coll is None:
        return []

    k = int(os.environ.get("OCTO_LEDGER_QUERY_K", "5"))
    k = max(1, min(k, 50))

    text = query_text.strip() or " "
    url = (os.environ.get("OLLAMA_LOCAL_URL") or "http://127.0.0.1:11434").strip()
    model = (os.environ.get("OCTO_EMBEDDING_MODEL") or "nomic-embed-text").strip()
    emb = _ollama_embed(text, url, model)

    res = coll.query(
        query_embeddings=[emb],
        n_results=k,
        where={"is_verified": True},
        include=["documents", "metadatas", "distances", "ids"],
    )
    return _rows_from_ledger_query(res)
