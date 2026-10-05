"""Estate-embedding reuse tests (pure logic — no live Neo4j / fastembed).

Covers the load-bearing invariants for persisting :EstateColumn vectors in the
graph and reusing them at feasibility time:

1. Canonicalization — ``schema_dna.estate_column_embed_text`` produces EXACTLY the
   text ``ColumnFeature(...).semantic_text()`` embeds for the same column (the
   invariant that lets a stored vector hit the scorer's cache).
2. Content-hash keying — same text + model → cache hit; a changed description or a
   model swap → miss/recompute.
3. Seeded ``score_matrix`` embeds ONLY the missing (spec-side) texts — the estate
   side is served from the seed, never re-embedded.
4. ``EmbeddingCache`` round-trip — batch read/insert, order preserved, only misses
   re-embedded (the persistent content-addressed cache under ``embed_documents``).
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, select

from workbench.backend import embeddings as emb
from workbench.backend import schema_dna
from workbench.backend.database import engine
from workbench.backend.models import EmbeddingCache
from workbench.backend.schema_dna import ColumnFeature


# ── 1. canonicalization invariant ─────────────────────────────────────────────

def _feature_text(name, desc) -> str:
    """What the scorer actually embeds for an estate column (concept empty)."""
    return ColumnFeature(name=name or "", concept="", description=desc or "").semantic_text()


@pytest.mark.parametrize("name,desc", [
    ("customer_id", "The unique identifier for a customer."),
    ("customer_id", ""),
    ("amount", None),
    ("", "orphan description with no name"),
    ("order_date", "Date the order was placed."),
    ("status", "  "),  # whitespace-only description is treated identically by both
])
def test_estate_embed_text_matches_column_feature(name, desc):
    # The whole reuse scheme rests on this equality — if it ever drifts, every
    # stored estate vector misses the feasibility scorer's cache.
    assert schema_dna.estate_column_embed_text(name, desc or "") == _feature_text(name, desc)


def test_estate_embed_text_shape():
    assert schema_dna.estate_column_embed_text("customer_id", "A customer.") == "customer_id. A customer."
    assert schema_dna.estate_column_embed_text("customer_id", "") == "customer_id"
    assert schema_dna.estate_column_embed_text("customer_id", None) == "customer_id"


# ── 2. content-hash keying ─────────────────────────────────────────────────────

def test_cache_key_stable_and_content_addressed():
    k1 = emb._cache_key("some column text")
    k2 = emb._cache_key("some column text")
    k3 = emb._cache_key("a different text")
    assert k1 == k2          # same text + model → same key
    assert k1 != k3          # different text → different key


def test_cache_key_folds_in_model(monkeypatch):
    before = emb._cache_key("stable text")
    monkeypatch.setattr(emb, "MODEL_NAME", "some-other-model/v2")
    after = emb._cache_key("stable text")
    assert before != after   # a model swap re-keys → re-embeds rather than staling


def test_estate_embed_text_hash_content_addressed():
    from workbench.backend import estate as estate_mod
    t1 = schema_dna.estate_column_embed_text("customer_id", "A customer.")
    t2 = schema_dna.estate_column_embed_text("customer_id", "A different customer.")
    assert estate_mod.estate_embed_text_hash(t1) == estate_mod.estate_embed_text_hash(t1)
    assert estate_mod.estate_embed_text_hash(t1) != estate_mod.estate_embed_text_hash(t2)


# ── 3. seeded score_matrix embeds only the misses ─────────────────────────────

def test_score_matrix_embed_cache_seeds_estate_side(monkeypatch):
    src = [
        ColumnFeature(name="customer_id", concept="customer identifier"),
        ColumnFeature(name="amount", concept="amount"),
    ]
    tgt = [
        ColumnFeature(name="cust_id", concept="", description="customer key"),
        ColumnFeature(name="total", concept="", description="order total"),
    ]
    # Estate vectors already on hand (as read back from :EstateColumn), keyed by
    # exactly the text the scorer will look up.
    seed = {t.semantic_text(): [1.0, 0.0, 0.0] for t in tgt}

    embedded: list[str] = []

    def fake_embed(texts):
        texts = list(texts)
        embedded.extend(texts)
        return [[float(len(t)), 1.0, 0.0] for t in texts]

    monkeypatch.setattr(schema_dna.embeddings, "available", lambda: True)
    monkeypatch.setattr(schema_dna.embeddings, "embed_documents", fake_embed)

    schema_dna.score_matrix(src, tgt, embed_cache=seed)

    # The seeded estate texts are NEVER re-embedded; only the spec-side texts are.
    assert not (set(embedded) & set(seed.keys()))
    assert set(embedded) == {s.semantic_text() for s in src}


def test_build_embed_cache_returns_seed_when_model_unavailable(monkeypatch):
    # Even with no model, seeded vectors survive (a stored estate vector keeps
    # working when fastembed can't load at feasibility time).
    monkeypatch.setattr(schema_dna.embeddings, "available", lambda: False)
    tf = ColumnFeature(name="cust_id", concept="", description="customer key")
    seed = {tf.semantic_text(): [0.5, 0.5, 0.0]}
    cache = schema_dna._build_embed_cache([tf], seed=seed)
    assert cache == seed


# ── 4. EmbeddingCache round-trip (batch read/insert, order preserved) ──────────

def _clear_embedding_cache():
    with Session(engine) as s:
        for r in s.exec(select(EmbeddingCache)).all():
            s.delete(r)
        s.commit()


def test_embed_documents_cache_roundtrip(monkeypatch):
    _clear_embedding_cache()
    calls: list[list[str]] = []

    def fake_uncached(texts):
        texts = list(texts)
        calls.append(texts)
        return [[float(ord((t or "x")[0]))] for t in texts]

    monkeypatch.setattr(emb, "_embed_uncached", fake_uncached)

    out1 = emb.embed_documents(["alpha", "beta", "gamma"])
    assert out1 == [[97.0], [98.0], [103.0]]        # order preserved
    assert calls == [["alpha", "beta", "gamma"]]     # all three embedded once

    calls.clear()
    out2 = emb.embed_documents(["beta", "gamma", "delta"])
    assert out2 == [[98.0], [103.0], [100.0]]        # order preserved
    # beta + gamma served from cache; ONLY the miss (delta) reaches the model.
    assert calls == [["delta"]]


def test_embed_documents_fully_cached_needs_no_model(monkeypatch):
    _clear_embedding_cache()

    def fake_uncached(texts):
        return [[1.0] for _ in texts]

    monkeypatch.setattr(emb, "_embed_uncached", fake_uncached)
    emb.embed_documents(["only-one"])  # warm the cache

    # Now the model returns nothing (unavailable) — a fully-cached batch still works.
    monkeypatch.setattr(emb, "_embed_uncached", lambda texts: [])
    assert emb.embed_documents(["only-one"]) == [[1.0]]


def test_embed_documents_empty_and_failure(monkeypatch):
    assert emb.embed_documents([]) == []
    _clear_embedding_cache()
    monkeypatch.setattr(emb, "_embed_uncached", lambda texts: [])
    # A genuine miss with no model → all-or-nothing empty (unchanged contract).
    assert emb.embed_documents(["never-seen-before-xyz"]) == []
