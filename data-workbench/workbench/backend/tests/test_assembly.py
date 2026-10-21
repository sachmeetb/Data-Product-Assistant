"""Product Assembly workspace tests (Phase 0 — persistence shell + entry).

The assembly is a thin overlay on a FeasibilityScore: create-from-score is
idempotent per (score, owner), patch-plan uses optimistic concurrency, and get
returns the score evidence the overlay sits on. Graph reads aren't needed.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from workbench.backend.auth import AuthUser
from workbench.backend.database import engine
from workbench.backend.models import (
    Estate, EstateScan, FeasibilityRun, FeasibilityScore, ProductAssembly,
)
from workbench.backend.routers import assembly as asm


def _seed(s: Session):
    est = Estate(name="E", domain="Cards", created_by="po@x.com")
    s.add(est); s.commit(); s.refresh(est)
    scan = EstateScan(estate_id=est.id, source_id=1, scan_version=1, state="completed")
    s.add(scan); s.commit(); s.refresh(scan)
    run = FeasibilityRun(estate_id=est.id, scan_id=scan.id, domain="Cards", state="completed")
    s.add(run); s.commit(); s.refresh(run)
    score = FeasibilityScore(
        run_id=run.id, estate_id=est.id, spec_id="src_credit_card", spec_name="Credit Card",
        domain="Cards", tier="absent", required_coverage=0.2,
        matched_json=json.dumps([{"attr_id": "#0:card_id", "spec_attr": "card_id"}]),
        gaps_json="[]", evidence_json=json.dumps({"raw_candidates": {"alternatives": {}}}),
    )
    s.add(score); s.commit(); s.refresh(score)
    return est, scan, run, score


def _cleanup(s: Session, est, scan, run, score):
    for a in s.exec(select(ProductAssembly).where(ProductAssembly.score_id == score.id)).all():
        s.delete(a)
    s.delete(s.get(FeasibilityScore, score.id))
    s.delete(s.get(FeasibilityRun, run.id))
    s.delete(s.get(EstateScan, scan.id))
    s.delete(s.get(Estate, est.id))
    s.commit()


def test_create_is_idempotent_per_score_owner():
    owner = AuthUser(email="po@x.com", name="PO", role="owner")
    with Session(engine, expire_on_commit=False) as s:
        est, scan, run, score = _seed(s)
        body = asm.CreateAssemblyRequest(run_id=run.id, spec_id=score.spec_id)
        r1 = asm.create_assembly(body, session=s, user=owner, _role=None)
        r2 = asm.create_assembly(body, session=s, user=owner, _role=None)
        assert r1["id"] == r2["id"]  # same workspace, not a duplicate
        assert r1["status"] == "draft" and r1["plan"] == {"attributes": {}, "clusters": [], "shortlist_override": {}}
        rows = s.exec(select(ProductAssembly).where(ProductAssembly.score_id == score.id)).all()
        assert len(rows) == 1
        # A different owner gets their own workspace over the same score.
        other = AuthUser(email="po2@x.com", name="PO2", role="owner")
        r3 = asm.create_assembly(body, session=s, user=other, _role=None)
        assert r3["id"] != r1["id"]
        _cleanup(s, est, scan, run, score)


def test_create_404_on_missing_score():
    owner = AuthUser(email="po@x.com", name="PO", role="owner")
    with Session(engine, expire_on_commit=False) as s:
        with pytest.raises(HTTPException) as exc:
            asm.create_assembly(asm.CreateAssemblyRequest(run_id=999999, spec_id="nope"),
                                session=s, user=owner, _role=None)
        assert exc.value.status_code == 404


def test_get_returns_evidence():
    owner = AuthUser(email="po@x.com", name="PO", role="owner")
    with Session(engine, expire_on_commit=False) as s:
        est, scan, run, score = _seed(s)
        created = asm.create_assembly(asm.CreateAssemblyRequest(run_id=run.id, spec_id=score.spec_id),
                                      session=s, user=owner, _role=None)
        got = asm.get_assembly(created["id"], session=s, user=owner)
        assert got["tier"] == "absent"
        assert got["required_coverage"] == 0.2
        assert "raw_candidates" in got["evidence"]
        assert got["matched"][0]["spec_attr"] == "card_id"
        _cleanup(s, est, scan, run, score)


def test_patch_plan_optimistic_concurrency():
    owner = AuthUser(email="po@x.com", name="PO", role="owner")
    with Session(engine, expire_on_commit=False) as s:
        est, scan, run, score = _seed(s)
        created = asm.create_assembly(asm.CreateAssemblyRequest(run_id=run.id, spec_id=score.spec_id),
                                      session=s, user=owner, _role=None)
        aid, rev = created["id"], created["plan_revision"]
        plan = {"attributes": {"#1:card_type": {"decision": "excluded"}},
                "clusters": [], "shortlist_override": {}}
        patched = asm.patch_plan(aid, asm.PatchPlanRequest(expected_revision=rev, plan=plan),
                                 session=s, user=owner, _role=None)
        assert patched["plan_revision"] == rev + 1
        assert patched["plan"]["attributes"]["#1:card_type"]["decision"] == "excluded"
        # A stale revision is rejected 409 (concurrent write / re-eval landed).
        with pytest.raises(HTTPException) as exc:
            asm.patch_plan(aid, asm.PatchPlanRequest(expected_revision=rev, plan=plan),
                           session=s, user=owner, _role=None)
        assert exc.value.status_code == 409
        _cleanup(s, est, scan, run, score)


def test_archive_then_reopen_reactivates():
    owner = AuthUser(email="po@x.com", name="PO", role="owner")
    with Session(engine, expire_on_commit=False) as s:
        est, scan, run, score = _seed(s)
        body = asm.CreateAssemblyRequest(run_id=run.id, spec_id=score.spec_id)
        created = asm.create_assembly(body, session=s, user=owner, _role=None)
        asm.archive_assembly(created["id"], session=s, user=owner, _role=None)
        assert s.get(ProductAssembly, created["id"]).status == "archived"
        # Archived is filtered from the list…
        assert all(r["id"] != created["id"]
                   for r in asm.list_assemblies(session=s, user=owner)["assemblies"])
        # …and re-opening reactivates the SAME row (not a duplicate).
        reopened = asm.create_assembly(body, session=s, user=owner, _role=None)
        assert reopened["id"] == created["id"] and reopened["status"] == "draft"
        _cleanup(s, est, scan, run, score)


# ── assembly → modernization blueprint compiler ────────────────────────────────

def test_build_modernization_blueprint():
    from workbench.backend import assembly_blueprint as ab
    from workbench.backend import feasibility_spec as fspec
    from workbench.backend import intake_blueprint as ibp

    spec = fspec.FeasibilitySpec(
        spec_id="cust360", name="Customer 360", domain="Customers",
        grain=fspec.GrainSpec(keys=["customer_id"]),
        attributes=[
            fspec.SpecAttribute(name="customer_id", type="bigint", concept="id", is_key=True),
            fspec.SpecAttribute(name="name", type="varchar", concept="name", description="full name"),
            fspec.SpecAttribute(name="order_date", type="date", concept="order date"),
            fspec.SpecAttribute(name="ssn", type="varchar", concept="ssn"),
        ])
    assignment = [
        {"attr_id": "#0:customer_id", "dataset_table": "customer", "column": "c_customer_sk"},
        {"attr_id": "#1:name", "dataset_table": "customer", "column": "c_name"},
        {"attr_id": "#2:order_date", "dataset_table": "store_sales", "column": "ss_sold_date",
         "cast_hint": {"needed": True, "from": "int", "to": "date", "cost": "cross_family_castable"}},
    ]
    decisions = {"#3:ssn": {"decision": "exclude"}}
    clusters = [{"name": "Customers", "table_refs": ["customer", "customer_address"]},
                {"name": "Store Sales", "table_refs": ["store_sales", "store_returns"]}]

    bp = ab.build_modernization_blueprint(spec, assignment, decisions, clusters, owner_email="po@x.com")
    # Re-validates as a real modernization blueprint (fail-closed already ran inside).
    model = ibp.parse_blueprint(bp)
    assert isinstance(model, ibp.ModernizationBlueprint)
    # A source-aligned product IS the WHOLE consumed cluster — every table in table_refs,
    # not just the tables a column is mapped from (a 2-table cluster is a 2-table product).
    srcs = {c["name"]["value"]: [d["name"]["value"] for d in c["datasets"]] for c in bp["source_aligned"]}
    assert srcs == {"Customers": ["customer", "customer_address"],
                    "Store Sales": ["store_returns", "store_sales"]}
    # Consumed tables carry the consumed columns; the rest of the cluster is columns:[]
    # (the engineer discovers the full source at build time).
    cust = next(c for c in bp["source_aligned"] if c["name"]["value"] == "Customers")
    cols = {d["name"]["value"]: [col["name"]["value"] for col in d["columns"]] for d in cust["datasets"]}
    assert cols == {"customer": ["c_customer_sk", "c_name"], "customer_address": []}
    # The aggregate carries a native ODCS with productKind=aggregate; ssn was excluded.
    odcs = bp["consumer_aligned"][0]["odcs"]
    assert odcs["productKind"] == "aggregate"
    names = [p["name"] for p in odcs["schema"][0]["properties"]]
    assert names == ["customer_id", "name", "order_date"] and "ssn" not in names
    # Source-intent hints + the cast are carried in the property descriptions.
    order = next(p for p in odcs["schema"][0]["properties"] if p["name"] == "order_date")
    assert "source: store_sales.ss_sold_date" in order["description"] and "cast int→date" in order["description"]
    # Dependencies wire the aggregate to both source clusters.
    assert {d["to_candidate_id"] for d in bp["dependencies"]} == {"src-0-customers", "src-1-store-sales"}


def _demo_blueprint():
    """A small blueprint + its inputs, shared by the rationale + report tests."""
    from workbench.backend import assembly_blueprint as ab
    from workbench.backend import feasibility_spec as fspec

    spec = fspec.FeasibilitySpec(
        spec_id="cust360", name="Customer 360", domain="Customers",
        description="A unified customer view.",
        grain=fspec.GrainSpec(keys=["customer_id"]),
        attributes=[
            fspec.SpecAttribute(name="customer_id", type="bigint", concept="id", is_key=True),
            fspec.SpecAttribute(name="name", type="varchar", concept="name", description="full name"),
            fspec.SpecAttribute(name="order_date", type="date", concept="order date"),
            fspec.SpecAttribute(name="ssn", type="varchar", concept="ssn"),
        ])
    assignment = [
        {"attr_id": "#0:customer_id", "spec_attr": "customer_id", "required": True, "is_key": True,
         "dataset_table": "customer", "column": "c_customer_sk"},
        {"attr_id": "#1:name", "spec_attr": "name", "required": True,
         "dataset_table": "customer", "column": "c_name"},
    ]
    decisions = {"#2:order_date": {"decision": "defer"}, "#3:ssn": {"decision": "exclude"}}
    clusters = [{"name": "Customers", "table_refs": ["customer", "customer_address"]},
                {"name": "Store Sales", "table_refs": ["store_sales"]}]
    bp = ab.build_modernization_blueprint(
        spec, assignment, decisions, clusters, spec_name="Customer 360",
        domain="Customers", owner_email="po@x.com")
    return spec, assignment, decisions, bp


def test_blueprint_rationale_is_forward_looking():
    """The intake SUMMARY (rationale) describes the portfolio + next steps — NOT the
    backward-looking feasibility-run provenance (which rides raw_payload_json)."""
    _spec, _assign, _dec, bp = _demo_blueprint()
    rat = bp["rationale"]
    # Forward-looking: the named source product → the aggregate + coverage + next steps.
    assert "Product Assembly portfolio" in rat
    assert "'Customers'" in rat and "'Customer 360' aggregate" in rat
    assert "Next:" in rat
    # NO feasibility-run/spec provenance leak.
    assert "run #" not in rat and "From Connected-Estate" not in rat
    assert "cust360" not in rat and "feasibility run" not in rat


def test_build_report_markdown():
    """The deterministic report renders valid markdown with the expected section
    headers + a mermaid block, and its counts match the compiled blueprint."""
    from workbench.backend import assembly_report as arep

    spec, assignment, decisions, bp = _demo_blueprint()
    evidence = {"required_coverage": 1.0, "total_coverage": 0.5,
                "schema_shortlist": [{"database": "db", "schema": "sales",
                                      "included": True, "matched_tables": ["customer"]}]}
    md = arep.build_report_markdown(
        spec=spec, spec_name="Customer 360", domain="Customers", tier="assemblable",
        evidence=evidence, assignment=assignment, decisions=decisions, blueprint=bp,
        attribute_groups=[{"name": "Identity", "attribute_names": ["customer_id", "name"], "rationale": "who"}],
        estate_name="Prod Estate", scan_finished_at="2026-08-25 10:00 UTC",
        generated_at="2026-08-25 12:00 UTC")
    for header in ["# Functional report — Customer 360", "## Overview",
                   "## The estate & scan", "## The target", "## Curated attributes by theme",
                   "## Source-aligned products", "## The aggregate", "## Portfolio flow",
                   "## Next steps"]:
        assert header in md, f"missing section: {header}"
    assert "```mermaid" in md and "flowchart LR" in md
    # Multi-table source product is described (item 1 flows into the report).
    assert "customer_address" in md
    # Counts match the blueprint: one source product per consumed cluster.
    assert md.count("### ") >= len(bp["source_aligned"])  # each source product is an h3
    # The aggregate's output-column count matches the ODCS the blueprint carries.
    n_out = len(bp["consumer_aligned"][0]["odcs"]["schema"][0]["properties"])
    assert f"**Output columns:** {n_out}" in md
    # Estate + scan metadata surfaced.
    assert "Prod Estate" in md and "2026-08-25 10:00 UTC" in md


def test_report_endpoint_persists_and_caches(monkeypatch):
    """The report endpoint builds + persists on first ask (surfacing
    report_generated_at), then returns the cached copy until ?regenerate=true."""
    from datetime import datetime
    from workbench.backend import feasibility_spec as fspec
    from workbench.backend import template_corpus

    # The report resolves the reference spec via the runtime graph loader; route it
    # to the retained file corpus (the test Neo4j has no published templates).
    monkeypatch.setattr(template_corpus, "load_specs_from_graph",
                        lambda session, domain=None: fspec.load_corpus(domain))

    owner = AuthUser(email="po@x.com", name="PO", role="owner")
    with Session(engine, expire_on_commit=False) as s:
        est = Estate(name="Prod Estate", domain="Aggregated", created_by="po@x.com")
        s.add(est); s.commit(); s.refresh(est)
        scan = EstateScan(estate_id=est.id, source_id=1, scan_version=1, state="completed",
                          finished_at=datetime.utcnow())
        s.add(scan); s.commit(); s.refresh(scan)
        run = FeasibilityRun(estate_id=est.id, scan_id=scan.id, domain="Aggregated", state="completed")
        s.add(run); s.commit(); s.refresh(run)
        score = FeasibilityScore(
            run_id=run.id, estate_id=est.id, spec_id="agg_customer_360", spec_name="Customer 360",
            domain="Aggregated", tier="assemblable", required_coverage=0.4,
            matched_json="[]", gaps_json="[]",
            evidence_json=json.dumps({"required_coverage": 0.4, "total_coverage": 0.3,
                                      "raw_candidates": {"assignment": [], "gaps": [], "alternatives": {}},
                                      "schema_shortlist": []}))
        s.add(score); s.commit(); s.refresh(score)
        created = asm.create_assembly(asm.CreateAssemblyRequest(run_id=run.id, spec_id=score.spec_id),
                                      session=s, user=owner, _role=None)
        r1 = asm.get_report(created["id"], regenerate=False, session=s, user=owner)
        assert r1["cached"] is False and "# Functional report" in r1["markdown"]
        assert "Prod Estate" in r1["markdown"]
        # Persisted + surfaced on the assembly row.
        row = asm.get_assembly(created["id"], session=s, user=owner)
        assert row["report_generated_at"] is not None
        # A second read (no regenerate) returns the cached copy verbatim.
        r2 = asm.get_report(created["id"], regenerate=False, session=s, user=owner)
        assert r2["cached"] is True and r2["markdown"] == r1["markdown"]
        for a in s.exec(select(ProductAssembly).where(ProductAssembly.score_id == score.id)).all():
            s.delete(a)
        s.delete(s.get(FeasibilityScore, score.id)); s.delete(s.get(FeasibilityRun, run.id))
        s.delete(s.get(EstateScan, scan.id)); s.delete(s.get(Estate, est.id)); s.commit()
