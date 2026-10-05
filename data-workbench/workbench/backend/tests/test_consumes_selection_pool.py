"""Phase 1 — the match_inputs candidate pool admits source / aggregate /
consumer, excludes drafts + empty-kind rows, excludes the caller's own contract,
and threads product_kind through to each candidate.

SQLite-safe: `list_published` (Neo4j) is monkeypatched to a canned mixed pool,
and the classifier skill is stubbed so the semantic tier never spawns a
subprocess.
"""

import pytest
from sqlmodel import Session

from workbench.backend.database import engine
from workbench.backend.routers import ingest_products as ip
from workbench.backend.routers import marketplace as mp


# ── Regression: list_published must be safe to call IN-PROCESS with no kwargs ──
# match_inputs calls list_published() in-process; FastAPI's `product_kind` default
# is a Query object (not None), so a bare `(product_kind or "").strip()` raised
# AttributeError BEFORE any graph access — swallowed into an empty candidate pool,
# making the wizard recommend CREATING products that already exist. This locks the
# fix (fires without Neo4j — the crash was before the graph block).

def test_list_published_in_process_without_product_kind_does_not_raise():
    with Session(engine) as s:
        resp = mp.list_published(session=s, owned_by=None)  # no product_kind
    assert isinstance(resp, dict)
    assert "products" in resp


def test_list_published_in_process_with_explicit_none():
    with Session(engine) as s:
        resp = mp.list_published(session=s, owned_by=None, product_kind=None)
    assert isinstance(resp, dict)
    assert "products" in resp


def _row(cid, kind, lifecycle, domain="sales"):
    return {
        "uri": f"dprod:{cid}",
        "contract_id": cid,
        "name": cid.replace("-contract", "").title(),
        "domain": domain,
        "description": "",
        "purpose": "",
        "product_kind": kind,
        "lifecycle_state": lifecycle,
        "column_count": 3,
    }


MIXED_POOL = [
    _row("src-contract", "source", "published"),
    _row("agg-contract", "aggregate", "published"),
    _row("leaf-contract", "consumer", "published"),
    _row("draft-contract", "source", "draft"),        # unpublished — excluded
    _row("blank-contract", "", "published"),           # empty kind — excluded
    _row("self-contract", "consumer", "published"),    # caller's own — excluded
]


@pytest.fixture
def _patched_pool(monkeypatch):
    monkeypatch.setattr(
        ip._marketplace, "list_published",
        lambda **kw: {"products": list(MIXED_POOL)},
    )

    async def _fake_skill(mode, payload):
        return ({}, None)

    monkeypatch.setattr(ip, "_run_classifier_skill", _fake_skill)
    # Neutralise the heuristic fallback so unmatched slots stay empty and the
    # test asserts only on the deterministic exact-URI tier.
    monkeypatch.setattr(ip, "_heuristic_rank_marketplace", lambda hint, pool: [])


def _call(inputs):
    spec = {
        "id": "self-contract",
        "domain": "sales",
        "name": "My Consumer",
        "inputs": inputs,
    }
    with Session(engine) as session:
        return ip.match_inputs(ip.MatchInputsBody(spec=spec), session)


def _slot_by_declared_uri(resp, uri):
    for s in resp["slots"]:
        if (s.get("declared") or {}).get("dprod_uri") == uri:
            return s
    return None


def test_pool_admits_source_aggregate_consumer(_patched_pool):
    resp = _call([
        {"dprod_uri": "dprod:src-contract", "name": "Src"},
        {"dprod_uri": "dprod:agg-contract", "name": "Agg"},
        {"dprod_uri": "dprod:leaf-contract", "name": "Leaf"},
    ])
    kinds = {}
    for uri, expect in [
        ("dprod:src-contract", "source"),
        ("dprod:agg-contract", "aggregate"),
        ("dprod:leaf-contract", "consumer"),
    ]:
        slot = _slot_by_declared_uri(resp, uri)
        assert slot is not None, f"no slot for {uri}"
        assert slot["preselected_candidate_uri"] == uri
        top = slot["candidates"][0]
        assert top["uri"] == uri
        # product_kind is threaded onto each candidate.
        assert top["product_kind"] == expect
        kinds[uri] = top["product_kind"]
    assert set(kinds.values()) == {"source", "aggregate", "consumer"}


def test_pool_excludes_draft_blank_and_self(_patched_pool):
    resp = _call([
        {"dprod_uri": "dprod:draft-contract", "name": "Draft"},
        {"dprod_uri": "dprod:blank-contract", "name": "Blank"},
        {"dprod_uri": "dprod:self-contract", "name": "Self"},
    ])
    # None of the excluded products may preselect (they're not in the pool)...
    for uri in ("dprod:draft-contract", "dprod:blank-contract", "dprod:self-contract"):
        slot = _slot_by_declared_uri(resp, uri)
        assert slot is not None
        assert slot["preselected_candidate_uri"] is None, f"{uri} should not be bindable"
    # ...and must never appear as a candidate on ANY slot.
    all_candidate_uris = {
        c["uri"] for s in resp["slots"] for c in s.get("candidates", [])
    }
    assert "dprod:draft-contract" not in all_candidate_uris
    assert "dprod:blank-contract" not in all_candidate_uris
    assert "dprod:self-contract" not in all_candidate_uris
