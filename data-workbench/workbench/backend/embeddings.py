"""Local text-embedding layer for the business-concept semantic search.

Self-hosted, CPU-only, no external embedding API. Uses ``fastembed``
(ONNX runtime, no torch) with ``BAAI/bge-small-en-v1.5`` — 384-dim,
cosine, ~130 MB. The model is loaded once (lazy singleton) and kept
warm in the FastAPI process; embedding a handful of short concept
strings is sub-second on CPU, so this runs inline in the request path.

Everything degrades gracefully: if ``fastembed`` isn't installed or the
model fails to load, :func:`available` returns ``False`` and callers
fall back to the non-embedding (Full Context) path. The vectors are
stored on ``:BusinessConcept.embedding`` and queried via a Neo4j native
vector index (see ``business_concepts._CONSTRAINT_QUERIES``).

bge models are *asymmetric*: documents are embedded raw, queries get an
instruction prefix. ``fastembed.TextEmbedding`` exposes ``.embed()`` for
documents and ``.query_embed()`` for queries, which applies the prefix
for us — so use :func:`embed_documents` for concept text and
:func:`embed_query` for the user's question phrases.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Optional

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384

_lock = threading.Lock()
_model = None              # the TextEmbedding instance once loaded
_load_failed = False       # sticky flag so we don't retry a broken load every call


def _get_model():
    """Lazy-load the embedding model once. Returns None if unavailable."""
    global _model, _load_failed
    if _model is not None:
        return _model
    if _load_failed:
        return None
    with _lock:
        if _model is not None:
            return _model
        if _load_failed:
            return None
        try:
            from fastembed import TextEmbedding
            cache_dir = os.environ.get("FASTEMBED_CACHE_PATH") or None
            _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=cache_dir)
        except Exception:
            # fastembed not installed, model download failed, or ONNX
            # runtime issue — mark as unavailable and let callers fall back.
            _load_failed = True
            _model = None
    return _model


def available() -> bool:
    """True iff the embedding model is loadable. Cheap after first call."""
    return _get_model() is not None


def _embed_uncached(texts: list[str]) -> list[list[float]]:
    """Raw model encode (no cache) — document strings, no query prefix. Empty list
    on failure. This is the CPU-bound step; :func:`embed_documents` wraps it with a
    persistent content-addressed cache. Tests that want to count real encodes
    monkeypatch THIS function (the cache is transparent above it)."""
    model = _get_model()
    if model is None or not texts:
        return []
    try:
        return [vec.tolist() for vec in model.embed(list(texts))]
    except Exception:
        return []


def _cache_key(text: str) -> str:
    """Content hash keying the persistent cache — folds in ``MODEL_NAME`` so a
    model swap re-embeds rather than returning a stale vector of a different model."""
    return hashlib.sha256(f"{MODEL_NAME}\x00{text}".encode("utf-8")).hexdigest()


def _cache_read(keys: list[str]) -> dict[str, list[float]]:
    """Best-effort batch read from the ``EmbeddingCache`` table. {} on any error
    (a missing/locked cache never blocks embedding)."""
    if not keys:
        return {}
    try:
        from sqlmodel import Session, select

        from .database import engine
        from .models import EmbeddingCache
    except Exception:
        return {}
    out: dict[str, list[float]] = {}
    try:
        with Session(engine) as s:
            # Chunk the IN() list to stay under SQLite's bound-parameter limit.
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                rows = s.exec(
                    select(EmbeddingCache).where(EmbeddingCache.text_hash.in_(chunk))  # type: ignore[attr-defined]
                ).all()
                for r in rows:
                    try:
                        out[r.text_hash] = json.loads(r.vec_json)
                    except Exception:
                        pass
    except Exception:
        return {}
    return out


def _cache_write(items: list[tuple[str, list[float]]]) -> None:
    """Best-effort batch INSERT-OR-IGNORE of freshly-embedded vectors. Never raises."""
    if not items:
        return
    try:
        from sqlmodel import Session

        from .database import engine
        from .models import EmbeddingCache
    except Exception:
        return
    try:
        with Session(engine) as s:
            for key, vec in items:
                s.add(EmbeddingCache(text_hash=key, model=MODEL_NAME,
                                     dim=len(vec), vec_json=json.dumps(vec)))
            s.commit()
    except Exception:
        # A concurrent writer inserted the same key (PK clash) — retry per-row so
        # the surviving rows still land.
        for key, vec in items:
            try:
                with Session(engine) as s:  # type: ignore[misc]
                    s.add(EmbeddingCache(text_hash=key, model=MODEL_NAME,
                                         dim=len(vec), vec_json=json.dumps(vec)))
                    s.commit()
            except Exception:
                pass


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed concept/document strings (no query prefix), served from a persistent
    content-addressed SQLite cache. Misses are encoded once (in a single batch) and
    written back, so a text embedded on any prior run — or before a restart — is
    never re-embedded. Order is preserved and the contract is unchanged: a full
    list (``len == len(texts)``) on success, ``[]`` on failure. A fully-cached batch
    returns without touching the model at all.
    """
    texts = list(texts)
    if not texts:
        return []
    keys = [_cache_key(t) for t in texts]
    cached = _cache_read(keys)
    results: list[Optional[list[float]]] = [cached.get(k) for k in keys]
    missing_idx = [i for i, v in enumerate(results) if v is None]
    if missing_idx:
        vecs = _embed_uncached([texts[i] for i in missing_idx])
        if not vecs or len(vecs) != len(missing_idx):
            return []  # model unavailable / failed — all-or-nothing (as before)
        to_write: list[tuple[str, list[float]]] = []
        for pos, i in enumerate(missing_idx):
            results[i] = vecs[pos]
            to_write.append((keys[i], vecs[pos]))
        _cache_write(to_write)
    return [v for v in results if v is not None]


def embed_query(text: str) -> Optional[list[float]]:
    """Embed a query string (bge instruction prefix applied). None on failure."""
    model = _get_model()
    if model is None or not text:
        return None
    try:
        return next(iter(model.query_embed([text]))).tolist()
    except Exception:
        return None


def concept_embedding_text(name: str, definition: str, synonyms: Optional[list[str]] = None) -> str:
    """Compose the document string embedded for a :BusinessConcept.

    Name + definition carry the bulk of the signal; synonyms widen recall
    so a query phrased differently from the canonical name still matches.
    """
    parts = [name or "", definition or ""]
    syn = [s.strip() for s in (synonyms or []) if s and s.strip()]
    if syn:
        parts.append("synonyms: " + ", ".join(syn))
    return ". ".join(p for p in parts if p)
