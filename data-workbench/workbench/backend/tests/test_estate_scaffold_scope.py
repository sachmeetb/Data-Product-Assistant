"""Connected-Estate → dpe-sa scaffold: carry the source connection + table scope.

When a dpe-sa project is scaffolded from a Product Assembly (Bridge B), the
assembly provenance carries a per-candidate ``source_scope`` (the estate
connection + the cluster's schema-qualified tables). The scaffold pre-binds the
source and pre-scopes Data Discovery — NO graph seed, NO enrichment carry-over
(live discovery stays authoritative). A submission WITHOUT ``source_scope`` (the
external-tool intake path) scaffolds exactly as before.
"""
from __future__ import annotations

import json
import shutil

from sqlmodel import Session, select

from workbench.backend import intake_scaffold
from workbench.backend.config import BASE_PROJECT_DIR
from workbench.backend.database import engine
from workbench.backend.estate import dataset_uri, parse_dataset_uri
from workbench.backend.models import (
    Estate, EstateSource, IntakeSubmission, PlatformConnection, Project,
    ProductRequest, SourceBinding, StageRun, StageStatus, Workflow,
)
from workbench.backend.routers import intake as intake_router
from workbench.backend.routers.stages import (
    _discovery_scope_defaults, _resolve_stage_id,
)


# ── parse_dataset_uri (inverse of the forward builder) ──────────────────────

def test_parse_dataset_uri_roundtrips():
    # 3-level (postgres/mysql — a real database segment)
    uri = dataset_uri(7, 3, "bank", "banking", "cards")
    assert uri == "estatedataset:7:3:bank.banking.cards"
    p = parse_dataset_uri(uri)
    assert p == {"estate_id": 7, "source_id": 3, "database": "bank",
                 "schema": "banking", "table": "cards"}
    # 2-level (empty database — dropped by the forward builder)
    p2 = parse_dataset_uri(dataset_uri(1, 2, "", "public", "users"))
    assert p2["database"] == "" and p2["schema"] == "public" and p2["table"] == "users"
    # 1-level (bare table)
    p1 = parse_dataset_uri(dataset_uri(1, 2, "", "", "orphan"))
    assert p1["schema"] == "" and p1["table"] == "orphan"


def test_parse_dataset_uri_rejects_malformed():
    assert parse_dataset_uri("") is None
    assert parse_dataset_uri("estatecolumn:1:2:a.b.c") is None  # wrong prefix
    assert parse_dataset_uri("estatedataset:x:2:a.b") is None   # non-int id
    assert parse_dataset_uri("estatedataset:1:2:") is None      # empty path


# ── _compute_source_scope (compose-time, over the cluster URIs) ─────────────

def _seed_estate_source(s: Session):
    conn = PlatformConnection(connection_name="estate-pg", platform_type="postgres",
                              host="h", port=5432, database="bank", username="u",
                              secret_ref="direct:p")
    s.add(conn); s.commit(); s.refresh(conn)
    est = Estate(name="E", domain="Cards", created_by="po@x.com")
    s.add(est); s.commit(); s.refresh(est)
    src = EstateSource(estate_id=est.id, connection_id=conn.id, platform="postgres")
    s.add(src); s.commit(); s.refresh(src)
    return conn, est, src


def _teardown_estate(s: Session, conn, est, src):
    for row in (src, est, conn):
        obj = s.get(type(row), row.id)
        if obj is not None:
            s.delete(obj)
    s.commit()


def test_compute_source_scope_from_cluster_uris():
    with Session(engine, expire_on_commit=False) as s:
        conn, est, src = _seed_estate_source(s)
        try:
            clusters = [{
                "cluster_id": "c0", "name": "Cards",
                "table_refs": ["cards", "card_accounts"],
                "uris": [dataset_uri(est.id, src.id, "bank", "banking", "cards"),
                         dataset_uri(est.id, src.id, "bank", "banking", "card_accounts")],
            }]
            # candidate_id encodes the cluster index (src-{i}-slug).
            source_aligned = [{"candidate_id": "src-0-cards", "name": {"value": "Cards"}}]
            scope = intake_router._compute_source_scope(s, clusters, source_aligned)
            assert set(scope) == {"src-0-cards"}
            row = scope["src-0-cards"]
            assert row["connection_id"] == conn.id
            assert row["default_schema"] == "banking"
            assert row["tables"] == ["banking.card_accounts", "banking.cards"]  # sorted
        finally:
            _teardown_estate(s, conn, est, src)


def test_compute_source_scope_name_fallback_and_missing_source():
    with Session(engine, expire_on_commit=False) as s:
        conn, est, src = _seed_estate_source(s)
        try:
            clusters = [{"cluster_id": "cX", "name": "Cards", "table_refs": ["cards"],
                         "uris": [dataset_uri(est.id, src.id, "bank", "banking", "cards")]}]
            # An unparseable candidate_id still resolves by name match.
            sa = [{"candidate_id": "weird-id", "name": {"value": "Cards"}}]
            scope = intake_router._compute_source_scope(s, clusters, sa)
            assert scope["weird-id"]["connection_id"] == conn.id
            # A cluster whose source_id doesn't resolve to a live EstateSource is dropped.
            gone = [{"cluster_id": "c0", "name": "Ghost", "table_refs": ["x"],
                     "uris": [dataset_uri(est.id, 999999, "bank", "banking", "x")]}]
            assert intake_router._compute_source_scope(
                s, gone, [{"candidate_id": "src-0-ghost", "name": {"value": "Ghost"}}]) == {}
        finally:
            _teardown_estate(s, conn, est, src)


# ── _discovery_scope_defaults (config-options intersection) ─────────────────

def test_discovery_scope_defaults_intersects_live_options():
    live = [{"value": "banking.cards", "label": "cards"},
            {"value": "banking.card_accounts", "label": "card_accounts"},
            {"value": "banking.customers", "label": "customers"}]
    # Stored scope includes a table that's since been dropped ("banking.legacy").
    scope = json.dumps(["banking.cards", "banking.card_accounts", "banking.legacy"])
    out = _discovery_scope_defaults(scope, live)
    # Multiselect defaults are `", "`-joined STRINGS (the frontend config contract);
    # returning bare lists crashed the config dialog (`.trim` on an array).
    assert out == {"discovery_tables": "banking.card_accounts, banking.cards",
                   "discovery_schemas": "banking"}
    # No scope → no defaults; no overlap → no defaults.
    assert _discovery_scope_defaults(None, live) is None
    assert _discovery_scope_defaults(json.dumps(["other.gone"]), live) is None


# ── the scaffold path (Bridge B) ────────────────────────────────────────────

def _modernization_submission(s: Session, source_scope: dict | None) -> IntakeSubmission:
    blueprint = {
        "scenario": "modernization", "overall_confidence": "high",
        "source_aligned": [{
            "candidate_id": "src-0-cards",
            "name": {"value": "Cards", "confidence": "high", "why": "cluster"},
            "domain": {"value": "cards", "confidence": "high", "why": "spec"},
            "product_idea": "source-align the cards tables",
        }],
        "consumer_aligned": [], "dependencies": [], "gaps": [], "rationale": "portfolio",
    }
    provenance: dict = {"assembly_id": 1, "estate_id": 1, "run_id": 1, "spec_id": "x"}
    if source_scope is not None:
        provenance["source_scope"] = source_scope
    sub = IntakeSubmission(
        source_system="connected-estate", external_ref="assembly:1",
        scenario="modernization", status="scaffolding", ingestion_mode="structured",
        blueprint_json=json.dumps(blueprint), blueprint_revision=1,
        raw_payload_json=json.dumps({"source": "assembly", "provenance": provenance}),
        reviewed_by="po@x.com",
    )
    s.add(sub); s.commit(); s.refresh(sub)
    return sub


def _rmtree_projects(codes):
    for code in codes:
        shutil.rmtree(BASE_PROJECT_DIR / code, ignore_errors=True)


def test_scaffold_source_aligned_carries_connection_and_scope():
    codes: list[str] = []
    with Session(engine, expire_on_commit=False) as s:
        conn, est, src = _seed_estate_source(s)
        try:
            tables = ["banking.cards", "banking.card_accounts"]
            sub = _modernization_submission(s, {
                "src-0-cards": {"connection_id": conn.id, "default_schema": "banking",
                                "tables": tables}})
            result = intake_scaffold.approve_and_scaffold(s, sub)
            assert result["scenario"] == "modernization"
            pid = result["source_projects"][0]
            proj = s.get(Project, pid)
            codes.append(proj.project_code)

            # (a) SourceBinding → the estate connection, with the cluster's schema.
            binding = s.exec(select(SourceBinding).where(
                SourceBinding.project_id == pid)).first()
            assert binding is not None
            assert binding.connection_id == conn.id
            assert binding.default_schema == "banking"

            # (b) select_data_source StageRun(s) flipped to complete.
            runs = s.exec(select(StageRun).where(StageRun.project_id == pid)).all()
            sds = [r for r in runs
                   if _resolve_stage_id(proj, r.stage_number, r.workflow_id, s) == "select_data_source"]
            assert sds and all(r.status == StageStatus.complete for r in sds)
            assert all(r.completed_at is not None for r in sds)

            # (c) discovery scope stashed verbatim (drives the pre-check).
            assert json.loads(proj.discovery_scope_json) == tables

            # NO operational graph seed happened (we never wrote :Dataset/:Column);
            # discovery stays as its own pending stage — assert it's NOT complete.
            disc = [r for r in runs
                    if _resolve_stage_id(proj, r.stage_number, r.workflow_id, s)
                    in ("data_discovery_composite", "data_discovery")]
            assert disc and all(r.status != StageStatus.complete for r in disc)
        finally:
            _cleanup(s, codes)
            _teardown_estate(s, conn, est, src)
    _rmtree_projects(codes)


def test_scaffold_without_source_scope_is_unchanged():
    codes: list[str] = []
    with Session(engine, expire_on_commit=False) as s:
        try:
            sub = _modernization_submission(s, source_scope=None)  # external-tool path
            result = intake_scaffold.approve_and_scaffold(s, sub)
            pid = result["source_projects"][0]
            proj = s.get(Project, pid)
            codes.append(proj.project_code)
            # No binding, no pre-scope — the engineer picks the source by hand.
            assert s.exec(select(SourceBinding).where(
                SourceBinding.project_id == pid)).first() is None
            assert proj.discovery_scope_json is None
            runs = s.exec(select(StageRun).where(StageRun.project_id == pid)).all()
            sds = [r for r in runs
                   if _resolve_stage_id(proj, r.stage_number, r.workflow_id, s) == "select_data_source"]
            assert sds and all(r.status != StageStatus.complete for r in sds)
        finally:
            _cleanup(s, codes)
    _rmtree_projects(codes)


def _cleanup(s: Session, codes: list[str]):
    """Remove scaffolded projects + child rows for the given project codes (the
    autouse fixture also sweeps these between tests; this keeps a single test tidy)."""
    for code in codes:
        proj = s.exec(select(Project).where(Project.project_code == code)).first()
        if proj is None:
            continue
        for model in (StageRun, Workflow, ProductRequest, SourceBinding):
            for row in s.exec(select(model).where(model.project_id == proj.id)).all():
                s.delete(row)
        s.delete(proj)
        s.commit()
