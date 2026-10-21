"""Feasibility evidence + verdict tests (pure logic — no Neo4j / live DB).

Covers the load-bearing invariants: bipartite 1:1 assignment (a column can't cover
two attributes), product-vs-raw separation, tier invariants (no green without a
product candidate; ``absent`` only on complete-empty evidence), evaluation-state
(a partial/failed scan never yields ``absent``), joinability, the corpus validator,
and the deterministic heuristic fallback.
"""
from __future__ import annotations

import pytest

from workbench.backend import feasibility as feas
from workbench.backend import feasibility_spec as fspec


# ── corpus + validator ────────────────────────────────────────────────────────

def test_corpus_loads_and_validates():
    specs = fspec.load_corpus()
    assert len(specs) >= 30
    assert fspec.corpus_version() != "0"
    cc = next(s for s in specs if s.spec_id == "src_credit_card")
    assert cc.product_kind == fspec.ProductKind.source
    assert cc.grain.keys == ["card_id"]
    assert len(cc.required_attributes) >= 1


def test_corpus_has_aggregate_with_composition():
    specs = fspec.load_corpus()
    aggs = [s for s in specs if s.product_kind == fspec.ProductKind.aggregate]
    assert aggs, "expected at least one aggregate spec"
    assert aggs[0].composition.composed_of  # resolved to composing spec ids


def test_validator_fail_closed_on_bad_spec():
    with pytest.raises(fspec.FeasibilitySpecValidationError):
        fspec.parse_spec({"spec_id": "x", "name": "X"})  # no domain, no attributes
    with pytest.raises(fspec.FeasibilitySpecValidationError):
        fspec.parse_spec({"spec_id": "x", "name": "X", "domain": "D", "attributes": []})


def test_load_corpus_by_domain():
    cards = fspec.load_corpus(domain="Cards")
    assert cards and all(s.domain == "Cards" for s in cards)


# ── bipartite 1:1 assignment ──────────────────────────────────────────────────

def _attr(name, required=True, type="bigint", concept="", derivations=None):
    return fspec.SpecAttribute(name=name, type=type, concept=concept or name,
                               required=required, derivations=derivations or [])


def test_bipartite_one_to_one_no_double_count():
    # Two spec attributes both resemble the ONE candidate column — only one may
    # claim it; the other becomes a gap (a column can't cover two requirements).
    spec_attrs = [_attr("customer_id"), _attr("cust_id")]
    cand = [{"name": "customer_id", "type": "bigint", "concept": "Customer ID"}]
    res = feas._bipartite_assign(spec_attrs, cand)
    assert len(res["assignment"]) == 1
    used_cols = {r["column"] for r in res["assignment"]}
    assert used_cols == {"customer_id"}
    assert len(res["gaps"]) == 1


def test_bipartite_full_coverage():
    spec_attrs = [_attr("card_id"), _attr("card_type", type="varchar")]
    cand = [{"name": "card_id", "type": "bigint", "concept": "Card ID"},
            {"name": "card_type", "type": "varchar", "concept": "Card Type"}]
    res = feas._bipartite_assign(spec_attrs, cand)
    assert res["required_coverage"] == 1.0
    assert len(res["assignment"]) == 2


def test_score_matrix_shape():
    src = [feas.schema_dna.ColumnFeature(name="amount", type="numeric")]
    tgt = [feas.schema_dna.ColumnFeature(name="amount", type="numeric"),
           feas.schema_dna.ColumnFeature(name="zzz_widget", type="text")]
    m = feas.schema_dna.score_matrix(src, tgt)
    assert len(m["cells"]) == 1 and len(m["cells"][0]) == 2
    # The identical-name pair scores higher than the unrelated one.
    assert m["cells"][0][0]["overall"] > m["cells"][0][1]["overall"]


# ── joinability ────────────────────────────────────────────────────────────────

def test_joinability_single_dataset():
    res = feas.joinability([{"table": "t", "columns": [{"name": "id"}]}])
    assert res["joinable"] and res["single_dataset"]


def test_joinability_connected_on_shared_key():
    datasets = [
        {"table": "accounts", "columns": [{"name": "account_id"}, {"name": "customer_id"}]},
        {"table": "customers", "columns": [{"name": "customer_id"}, {"name": "name"}]},
    ]
    res = feas.joinability(datasets)
    assert res["joinable"] is True
    assert any("customer_id" in p["on"] for p in res["paths"])


def test_joinability_disconnected_islands():
    datasets = [
        {"table": "a", "columns": [{"name": "a_id"}]},
        {"table": "b", "columns": [{"name": "b_id"}]},
    ]
    res = feas.joinability(datasets)
    assert res["joinable"] is False
    assert res["missing"]  # two islands reported


# ── product-vs-raw separation in the evidence bundle ──────────────────────────

def _spec(spec_id="s1", domain="Cards", kind="source"):
    return fspec.FeasibilitySpec(
        spec_id=spec_id, name="Cards Product", domain=domain, product_kind=kind,
        attributes=[_attr("card_id"), _attr("card_type", type="varchar"),
                    _attr("amount", required=False, type="numeric")],
    )


def test_build_spec_evidence_separates_product_and_raw():
    spec = _spec()
    products = [{
        "uri": "cards-contract:v1", "version": "1.0", "contract_id": "cards-contract",
        "product_kind": "source", "domain": "Cards", "name": "Cards",
        "columns": [{"name": "card_id", "type": "bigint", "concept": "Card ID"},
                    {"name": "card_type", "type": "varchar", "concept": "Card Type"}],
    }]
    datasets = [{
        "uri": "estatedataset:1:1:db.public.cards", "table": "cards", "schema": "public",
        "columns": [{"name": "card_id", "data_type": "bigint"},
                    {"name": "amount", "data_type": "numeric"}],
    }]
    ev = feas.build_spec_evidence(spec, products, datasets)
    assert "product_candidates" in ev and "raw_candidates" in ev
    assert ev["product_candidates"] and ev["product_candidates"][0]["uri"] == "cards-contract:v1"
    # Raw candidates are estate datasets, never conflated with the product pool.
    assert ev["raw_candidates"]["datasets"]
    assert all("estatedataset" in d["uri"] for d in ev["raw_candidates"]["datasets"])


# ── tier invariants + heuristic ceiling ───────────────────────────────────────

def _product_cand(req_cov, total_cov=None, deriv=None):
    total_cov = req_cov if total_cov is None else total_cov
    assignment = [{"spec_attr": "card_id", "required": True, "column": "card_id",
                   "score": 95, "axes": {}, "derivation": deriv}]
    return {"uri": "p:v1", "version": "1.0", "name": "P", "product_kind": "source",
            "required_coverage": req_cov, "total_coverage": total_cov,
            "assignment": assignment, "gaps": []}


def _ev(products=None, raw_req=0.0, raw_join=True, raw_assignment=None):
    return {
        "product_candidates": products or [],
        "raw_candidates": {
            "required_coverage": raw_req, "total_coverage": raw_req,
            "assignment": raw_assignment if raw_assignment is not None else ([{"x": 1}] if raw_req else []),
            "gaps": [], "datasets": [{"uri": "d1", "table": "t"}],
            "join_plan": {"joinable": raw_join, "single_dataset": False, "paths": [], "missing": []},
        },
    }


def test_heuristic_ready_needs_high_product_coverage():
    v = feas.heuristic_verdict(_ev(products=[_product_cand(0.95)]), "completed")
    assert v["tier"] == "ready"
    assert v["best_product"]["uri"] == "p:v1"


def test_heuristic_adaptable_mid_product_coverage():
    v = feas.heuristic_verdict(_ev(products=[_product_cand(0.7, deriv="currency_normalize")]), "completed")
    assert v["tier"] == "adaptable"


def test_heuristic_assemblable_needs_raw_only():
    # No product candidate at all → can only be assemblable (from raw), never green.
    v = feas.heuristic_verdict(_ev(products=[], raw_req=0.8, raw_join=True), "completed")
    assert v["tier"] == "assemblable"
    assert v["best_product"] is None


def test_heuristic_assemblable_blocked_when_not_joinable():
    v = feas.heuristic_verdict(_ev(products=[], raw_req=0.8, raw_join=False), "completed")
    assert v["tier"] == "absent"


def test_absent_only_on_complete_empty():
    v = feas.heuristic_verdict(_ev(products=[], raw_req=0.0), "completed")
    assert v["tier"] == "absent"
    assert v["evaluation_state"] == "completed"


def test_partial_scan_never_yields_bare_absent():
    v = feas.heuristic_verdict(_ev(products=[], raw_req=0.0), "partial")
    assert v["evaluation_state"] == "insufficient_evidence"
    # tier may be 'absent' but the state makes clear it's a gap, not a real absence.


def test_failed_scan_state_propagates():
    v = feas.heuristic_verdict(_ev(products=[], raw_req=0.0), "failed")
    assert v["evaluation_state"] in ("failed", "insufficient_evidence")


# ── finalize_verdict: skill clamped to heuristic ceiling ──────────────────────

def test_finalize_falls_back_when_skill_over_promises():
    ev = _ev(products=[], raw_req=0.8, raw_join=True)  # ceiling = assemblable
    skill_row = {"spec_id": "s1", "tier": "ready", "rationale": "too optimistic"}
    v = feas.finalize_verdict(_spec(), ev, "completed", skill_row)
    assert v["tier"] == "assemblable"  # downgraded to the deterministic ceiling


def test_finalize_accepts_conservative_skill_tier_and_rationale():
    ev = _ev(products=[_product_cand(0.95)])  # ceiling = ready
    skill_row = {"spec_id": "s1", "tier": "adaptable", "rationale": "prefer caution"}
    v = feas.finalize_verdict(_spec(), ev, "completed", skill_row)
    assert v["tier"] == "adaptable"
    assert v["rationale"] == "prefer caution"


def test_finalize_rejects_green_without_product_candidate():
    ev = _ev(products=[], raw_req=0.8, raw_join=True)
    skill_row = {"spec_id": "s1", "tier": "adaptable", "rationale": "no product exists"}
    v = feas.finalize_verdict(_spec(), ev, "completed", skill_row)
    # ceiling is assemblable AND there's no product candidate → cannot be adaptable.
    assert v["tier"] == "assemblable"


def test_finalize_none_skill_returns_heuristic():
    ev = _ev(products=[_product_cand(0.95)])
    v = feas.finalize_verdict(_spec(), ev, "completed", None)
    assert v["tier"] == "ready"
    assert not v.get("used_skill")


# ── end-to-end orchestration (graph reads + skill monkeypatched) ──────────────

def test_evaluate_run_persists_scores_heuristic(monkeypatch):
    """Full run: build evidence (Cards domain) → heuristic (skill disabled) →
    persist FeasibilityScore rows. Graph reads are stubbed so no Neo4j is needed."""
    import asyncio

    from sqlmodel import Session, select
    from workbench.backend.database import engine
    from workbench.backend.models import (
        Estate, EstateScan, FeasibilityRun, FeasibilityScore,
    )

    # A published product that mirrors the real Credit Card spec's columns →
    # high coverage → ready. (The spec has ~31 attributes, so a sparse product
    # would correctly fall below the candidate floor.)
    cc_spec = next(x for x in fspec.load_corpus("Cards") if x.spec_id == "src_credit_card")

    def _fake_products(session):
        cols = [{"name": a.name, "type": a.type, "concept": a.concept, "is_key": a.is_key}
                for a in cc_spec.attributes]
        return [{"uri": "cards:v1", "contract_id": "cards-contract", "version": "1.0",
                 "product_kind": "source", "domain": "Cards", "name": "Cards", "columns": cols}]

    def _fake_datasets(session, scan_ids):
        # evaluate_run now spans a SET of scans (multi-catalog) → read_estate_datasets.
        assert isinstance(scan_ids, list)
        return [{"uri": "estatedataset:1:1:db.public.cards", "table": "cards", "schema": "public",
                 "columns": [{"name": "card_id", "data_type": "bigint"},
                             {"name": "issue_date", "data_type": "date"}]}]

    async def _no_skill(bundle):
        return None  # force the deterministic heuristic

    # evaluate_run now reads specs live from the graph; route the graph loader to
    # the file corpus so the run still evaluates the 3 Cards specs (no Neo4j).
    monkeypatch.setattr(feas.template_corpus, "load_specs_from_graph",
                        lambda session, domain=None: fspec.load_corpus(domain))
    monkeypatch.setattr(feas, "enumerate_product_candidates", _fake_products)
    monkeypatch.setattr(feas.estate_mod, "read_estate_datasets", _fake_datasets)
    monkeypatch.setattr(feas, "_run_evaluator_skill", _no_skill)

    with Session(engine, expire_on_commit=False) as s:
        estate = Estate(name="E", domain="Cards", created_by="po@x.com")
        s.add(estate); s.commit(); s.refresh(estate)
        scan = EstateScan(estate_id=estate.id, source_id=1, scan_version=1, state="completed")
        s.add(scan); s.commit(); s.refresh(scan)
        run = FeasibilityRun(estate_id=estate.id, scan_id=scan.id, domain="Cards", state="queued")
        s.add(run); s.commit(); s.refresh(run)
        run_id, estate_id = run.id, estate.id

    with Session(engine) as s:
        asyncio.run(feas.evaluate_run(s, run_id))

    with Session(engine) as s:
        run = s.get(FeasibilityRun, run_id)
        assert run.state == "completed"
        assert run.used_skill is False
        assert run.corpus_version and run.skill_version
        scores = s.exec(select(FeasibilityScore).where(FeasibilityScore.run_id == run_id)).all()
        assert len(scores) == 3  # the 3 Cards specs
        cc = next(x for x in scores if x.spec_id == "src_credit_card")
        # A product mirroring the spec → a real product candidate → green.
        assert cc.tier in ("ready", "adaptable")
        assert cc.best_product_uri == "cards:v1"
        assert cc.evaluation_state == "completed"
        # cleanup
        for x in scores:
            s.delete(x)
        s.delete(s.get(FeasibilityRun, run_id))
        s.delete(s.get(EstateScan, run.scan_id))
        s.delete(s.get(Estate, estate_id))
        s.commit()


# ── compose modernization submission seeds a confirm-only consumer schema ─────

def test_sanitized_consumer_odcs_strips_template_identity():
    """The shared FS→consumer-ODCS bridge: schema mirrors the spec attributes and
    the template identity (id / sourceSpecId) is stripped so the save is
    project-bound (not a `template:` contract_id link_contract_to_project refuses)."""
    spec = fspec.FeasibilitySpec(
        spec_id="cust_360", name="Customer 360", domain="Sales", product_kind="consumer",
        description="A unified customer profile.",
        attributes=[_attr("customer_id"), _attr("email", required=False, type="varchar")],
    )
    odcs = feas.sanitized_consumer_odcs(spec)
    assert "id" not in odcs and "sourceSpecId" not in odcs
    assert odcs["status"] == "draft"
    assert odcs["productKind"] == "consumer"
    assert [p["name"] for p in odcs["schema"][0]["properties"]] == [a.name for a in spec.attributes]
    # overlays win when supplied
    odcs2 = feas.sanitized_consumer_odcs(spec, name="Override", purpose="P")
    assert odcs2["name"] == "Override" and odcs2["purpose"] == "P"


def test_compose_modernization_seeds_consumer_odcs(monkeypatch):
    """Part B: the /act (assemblable) path attaches a project-bound consumer ODCS to
    the consumer candidate so the scaffold seeds the CF wizard's schema faithfully
    (confirm-only Step 4) instead of leaving an empty draft the advisor over-fills.
    Asserted at the parse_blueprint dict level (SQLite-only; no graph)."""
    import json
    from sqlmodel import Session
    from workbench.backend.database import engine
    from workbench.backend.models import (
        Estate, EstateScan, FeasibilityRun, FeasibilityScore, IntakeSubmission,
    )

    spec = fspec.FeasibilitySpec(
        spec_id="cust_360", name="Customer 360", domain="Sales", product_kind="consumer",
        description="A unified customer profile.",
        attributes=[_attr("customer_id"), _attr("email", required=False, type="varchar"),
                    _attr("lifetime_value", required=False, type="numeric")],
    )
    # compose reads specs live from the graph — route to our single spec (no Neo4j).
    monkeypatch.setattr(feas.template_corpus, "load_specs_from_graph",
                        lambda session, domain=None: [spec])

    evidence = {"raw_candidates": {"datasets": [
        {"uri": "estatedataset:1:1:db.public.customers", "table": "customers", "schema": "public"},
    ]}}

    with Session(engine, expire_on_commit=False) as s:
        estate = Estate(name="E", domain="Sales", created_by="po@x.com")
        s.add(estate); s.commit(); s.refresh(estate)
        scan = EstateScan(estate_id=estate.id, source_id=1, scan_version=1, state="completed")
        s.add(scan); s.commit(); s.refresh(scan)
        run = FeasibilityRun(estate_id=estate.id, scan_id=scan.id, domain="Sales", state="completed")
        s.add(run); s.commit(); s.refresh(run)
        score = FeasibilityScore(
            run_id=run.id, estate_id=estate.id, spec_id="cust_360", spec_name="Customer 360",
            domain="Sales", tier="assemblable", evidence_json=json.dumps(evidence),
            rationale="assemblable from one dataset",
        )
        s.add(score); s.commit(); s.refresh(score)
        run_id, estate_id, scan_id, score_id = run.id, estate.id, scan.id, score.id

    try:
        with Session(engine) as s:
            result = feas.compose_modernization_submission(s, run_id, "cust_360", "po@x.com")
            assert result.get("action") == "assemble"
            sub = s.get(IntakeSubmission, result["intake_id"])
            bp = json.loads(sub.blueprint_json)
            con = bp["consumer_aligned"][0]
            odcs = con.get("odcs")
            assert odcs is not None, "consumer candidate should carry a seeded ODCS"
            # Schema names equal the spec attribute names — the agreed set, verbatim.
            assert [p["name"] for p in odcs["schema"][0]["properties"]] == [a.name for a in spec.attributes]
            # Sanitized + project-bound: no template identity, drafted, consumer kind.
            assert "id" not in odcs and "sourceSpecId" not in odcs
            assert odcs["status"] == "draft"
            assert odcs["productKind"] == "consumer"
            sub_id = sub.id
    finally:
        with Session(engine) as s:
            for model, pk in ((IntakeSubmission, locals().get("sub_id")),
                              (FeasibilityScore, score_id), (FeasibilityRun, run_id),
                              (EstateScan, scan_id), (Estate, estate_id)):
                if pk is not None:
                    row = s.get(model, pk)
                    if row is not None:
                        s.delete(row)
            s.commit()


# ── multi-catalog feasibility: latest scan per source ─────────────────────────

def test_run_scan_ids_prefers_json_falls_back_to_scan_id():
    from workbench.backend.models import FeasibilityRun
    r = FeasibilityRun(estate_id=1, scan_id=9, scan_ids_json="[3, 4]", state="queued")
    assert feas._run_scan_ids(r) == [3, 4]
    legacy = FeasibilityRun(estate_id=1, scan_id=9, scan_ids_json="[]", state="queued")
    assert feas._run_scan_ids(legacy) == [9]


def test_latest_scans_for_estate_one_per_enabled_source():
    from sqlmodel import Session
    from workbench.backend.database import engine
    from workbench.backend.models import (
        Estate, EstateScan, EstateSource, PlatformConnection,
    )
    with Session(engine, expire_on_commit=False) as s:
        conn = PlatformConnection(connection_name="feas-multi", platform_type="databricks",
                                  host="h", port=443, database="")
        s.add(conn); s.commit(); s.refresh(conn)
        est = Estate(name="E", created_by="po@x.com"); s.add(est); s.commit(); s.refresh(est)
        # src_a (enabled): an older completed scan + a newer completed scan → newest wins.
        src_a = EstateSource(estate_id=est.id, connection_id=conn.id, platform="databricks",
                             catalog="samples", enabled=True)
        # src_b (enabled): only a failed scan → contributes nothing.
        src_b = EstateSource(estate_id=est.id, connection_id=conn.id, platform="databricks",
                             catalog="workspace", enabled=True)
        # src_c (DISABLED): a completed scan → excluded.
        src_c = EstateSource(estate_id=est.id, connection_id=conn.id, platform="databricks",
                             catalog="other", enabled=False)
        for src in (src_a, src_b, src_c):
            s.add(src)
        s.commit(); s.refresh(src_a); s.refresh(src_b); s.refresh(src_c)
        old_a = EstateScan(estate_id=est.id, source_id=src_a.id, scan_version=1, state="completed")
        new_a = EstateScan(estate_id=est.id, source_id=src_a.id, scan_version=2, state="completed")
        fail_b = EstateScan(estate_id=est.id, source_id=src_b.id, scan_version=1, state="failed")
        done_c = EstateScan(estate_id=est.id, source_id=src_c.id, scan_version=1, state="completed")
        for sc in (old_a, new_a, fail_b, done_c):
            s.add(sc)
        s.commit(); s.refresh(new_a)

        got = feas.latest_scans_for_estate(s, est.id)
        assert got == [new_a.id]  # newest completed of the only qualifying source

        for sc in (old_a, new_a, fail_b, done_c):
            s.delete(s.get(EstateScan, sc.id))
        for src in (src_a, src_b, src_c):
            s.delete(s.get(EstateSource, src.id))
        s.delete(s.get(Estate, est.id)); s.delete(s.get(PlatformConnection, conn.id))
        s.commit()


def test_evaluate_estate_wide_spans_all_sources_and_dedups():
    from fastapi import HTTPException
    from sqlmodel import Session
    from workbench.backend.auth import AuthUser
    from workbench.backend.database import engine
    from workbench.backend.models import (
        Estate, EstateScan, EstateSource, FeasibilityRun, FeasibilityScore,
        PlatformConnection,
    )
    from workbench.backend.routers import feasibility as feas_router

    owner = AuthUser(email="po@x.com", name="PO", role="owner")
    with Session(engine, expire_on_commit=False) as s:
        conn = PlatformConnection(connection_name="feas-ew", platform_type="databricks",
                                  host="h", port=443, database="")
        s.add(conn); s.commit(); s.refresh(conn)
        est = Estate(name="E", created_by="po@x.com"); s.add(est); s.commit(); s.refresh(est)
        src1 = EstateSource(estate_id=est.id, connection_id=conn.id, platform="databricks",
                            catalog="samples")
        src2 = EstateSource(estate_id=est.id, connection_id=conn.id, platform="databricks",
                            catalog="workspace")
        s.add(src1); s.add(src2); s.commit(); s.refresh(src1); s.refresh(src2)
        sc1 = EstateScan(estate_id=est.id, source_id=src1.id, scan_version=1, state="completed")
        sc2 = EstateScan(estate_id=est.id, source_id=src2.id, scan_version=1, state="completed")
        s.add(sc1); s.add(sc2); s.commit(); s.refresh(sc1); s.refresh(sc2)

        # Estate-wide evaluate (scan_id omitted) → run spans BOTH sources' scans.
        body = feas_router.EvaluateRequest(estate_id=est.id, domain="Cards")
        row = feas_router.evaluate(body, session=s, user=owner, _role=None)
        run = s.get(FeasibilityRun, row["id"])
        import json as _json
        assert set(_json.loads(run.scan_ids_json)) == {sc1.id, sc2.id}
        assert run.scan_id == max(sc1.id, sc2.id)
        assert set(row["scan_ids"]) == {sc1.id, sc2.id}

        # De-dup is per (estate, domain): a second same-domain run is rejected.
        with pytest.raises(HTTPException) as exc:
            feas_router.evaluate(body, session=s, user=owner, _role=None)
        assert exc.value.status_code == 409

        for x in s.exec(__import__("sqlmodel").select(FeasibilityScore).where(
                FeasibilityScore.run_id == run.id)).all():
            s.delete(x)
        s.delete(s.get(FeasibilityRun, run.id))
        for sc in (sc1, sc2):
            s.delete(s.get(EstateScan, sc.id))
        for src in (src1, src2):
            s.delete(s.get(EstateSource, src.id))
        s.delete(s.get(Estate, est.id)); s.delete(s.get(PlatformConnection, conn.id))
        s.commit()


# ── tiered matching: schema shortlist → scoped column mapping ──────────────────

def _emp_spec():
    return fspec.FeasibilitySpec(
        spec_id="emp_dir", name="Employee Directory", domain="HR",
        attributes=[_attr("employee_id", concept="employee identifier"),
                    _attr("employee_name", type="varchar", concept="employee name"),
                    _attr("department_name", type="varchar", concept="department name")],
    )


def _sales_and_hr_datasets():
    return [
        {"uri": "estatedataset:1:1:db.sales.orders", "database": "db", "schema": "sales", "table": "orders",
         "columns": [{"name": "order_id", "data_type": "bigint"},
                     {"name": "product_id", "data_type": "bigint"},
                     {"name": "amount", "data_type": "numeric"}]},
        {"uri": "estatedataset:1:1:db.employees.employee", "database": "db", "schema": "employees", "table": "employee",
         "columns": [{"name": "employee_id", "data_type": "bigint"},
                     {"name": "employee_name", "data_type": "varchar"}]},
        {"uri": "estatedataset:1:1:db.employees.department", "database": "db", "schema": "employees", "table": "department",
         "columns": [{"name": "department_id", "data_type": "bigint"},
                     {"name": "department_name", "data_type": "varchar"}]},
    ]


def _group_by_key(datasets):
    by_key = {}
    for d in datasets:
        by_key.setdefault((d.get("database", ""), d.get("schema", "")), []).append(d)
    return by_key


def test_schema_shortlist_restricts_to_relevant_schema(monkeypatch):
    # Force the token-Jaccard path so scoring is deterministic regardless of
    # whether the embedding model is installed in the test environment.
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = _emp_spec()
    datasets = _sales_and_hr_datasets()
    by_key = _group_by_key(datasets)

    # cap=1 keeps only the single most-relevant schema; the employee spec must
    # pick 'employees', not 'sales' (which merely shares a 'product_id' token).
    shortlist = feas._schema_shortlist(spec, by_key, {}, floor=0, cap=1)
    included = [r for r in shortlist if r["included"]]
    assert len(included) == 1
    assert included[0]["schema"] == "employees"
    emp_row = next(r for r in shortlist if r["schema"] == "employees")
    sales_row = next(r for r in shortlist if r["schema"] == "sales")
    assert emp_row["score"] > sales_row["score"]

    # Scoped evidence: the raw assignment only pulls columns from 'employees'.
    ev = feas.build_spec_evidence(spec, [], datasets, shortlist)
    used_schemas = {d["schema"] for d in ev["raw_candidates"]["datasets"]}
    assert used_schemas <= {"employees"}
    for m in ev["raw_candidates"]["assignment"]:
        assert m.get("dataset_schema") == "employees"
    assert ev["schema_scope_empty"] is False


def test_empty_scope_yields_absent_with_reason(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = _emp_spec()
    datasets = _sales_and_hr_datasets()
    by_key = _group_by_key(datasets)

    # A floor above any achievable score → nothing clears → empty scope.
    shortlist = feas._schema_shortlist(spec, by_key, {}, floor=101, cap=3)
    assert all(not r["included"] for r in shortlist)

    ev = feas.build_spec_evidence(spec, [], datasets, shortlist)
    ev["schema_relevance_floor"] = 101
    assert ev["schema_scope_empty"] is True
    assert ev["raw_candidates"]["assignment"] == []

    v = feas.heuristic_verdict(ev, "completed")
    assert v["tier"] == "absent"
    assert "relevance floor" in v["rationale"]
    # finalize keeps the deterministic empty-scope absence even if a skill row exists.
    fv = feas.finalize_verdict(spec, ev, "completed", {"spec_id": "emp_dir", "tier": "assemblable", "rationale": "x"})
    assert fv["tier"] == "absent"
    assert "relevance floor" in fv["rationale"]


def test_empty_scope_still_greens_on_product_match(monkeypatch):
    # Schema scoping restricts only the ESTATE (raw) pool. A governed product that
    # covers the spec must still win ready/adaptable even when no schema clears.
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = _emp_spec()
    products = [{
        "uri": "emp-contract:v1", "version": "1.0", "contract_id": "emp-contract",
        "product_kind": "source", "domain": "HR", "name": "Employees",
        "columns": [{"name": "employee_id", "type": "bigint", "concept": "employee identifier"},
                    {"name": "employee_name", "type": "varchar", "concept": "employee name"},
                    {"name": "department_name", "type": "varchar", "concept": "department name"}],
    }]
    datasets = _sales_and_hr_datasets()
    shortlist = feas._schema_shortlist(spec, _group_by_key(datasets), {}, floor=101, cap=3)
    ev = feas.build_spec_evidence(spec, products, datasets, shortlist)
    ev["schema_relevance_floor"] = 101
    assert ev["schema_scope_empty"] is True
    v = feas.heuristic_verdict(ev, "completed")
    assert v["tier"] in ("ready", "adaptable")
    assert v["best_product"]["uri"] == "emp-contract:v1"


def test_levers_round_trip_through_run():
    from sqlmodel import Session
    from workbench.backend.auth import AuthUser
    from workbench.backend.database import engine
    from workbench.backend.models import (
        Estate, EstateScan, EstateSource, FeasibilityRun, PlatformConnection,
    )
    from workbench.backend.routers import feasibility as feas_router
    import json as _json

    owner = AuthUser(email="po@x.com", name="PO", role="owner")
    with Session(engine, expire_on_commit=False) as s:
        conn = PlatformConnection(connection_name="feas-lever", platform_type="databricks",
                                  host="h", port=443, database="")
        s.add(conn); s.commit(); s.refresh(conn)
        est = Estate(name="E", created_by="po@x.com"); s.add(est); s.commit(); s.refresh(est)
        src = EstateSource(estate_id=est.id, connection_id=conn.id, platform="databricks", catalog="samples")
        s.add(src); s.commit(); s.refresh(src)
        sc = EstateScan(estate_id=est.id, source_id=src.id, scan_version=1, state="completed")
        s.add(sc); s.commit(); s.refresh(sc)

        body = feas_router.EvaluateRequest(estate_id=est.id, domain="Cards",
                                           scope_to_schemas=True, schema_relevance_floor=55,
                                           schema_shortlist_threshold=70,
                                           max_schemas_per_spec=2)
        row = feas_router.evaluate(body, session=s, user=owner, _role=None)
        run = s.get(FeasibilityRun, row["id"])
        scoping = _json.loads(run.schema_scoping_json)
        assert {k: scoping[k] for k in ("enabled", "floor", "threshold", "cap")} == {
            "enabled": True, "floor": 55, "threshold": 70, "cap": 2}
        assert scoping.get("include") == [] and scoping.get("exclude") == []
        assert row["schema_scoping"]["floor"] == 55
        assert row["schema_scoping"]["threshold"] == 70

        s.delete(s.get(FeasibilityRun, run.id))
        s.delete(s.get(EstateScan, sc.id)); s.delete(s.get(EstateSource, src.id))
        s.delete(s.get(Estate, est.id)); s.delete(s.get(PlatformConnection, conn.id))
        s.commit()


def test_recommend_heuristic_ranks_on_domain_highest(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    estate_text = ("hr.employees: employee master with department and salary. "
                   "hr.departments: department reference. "
                   "Tables: hr.employees.employee; hr.departments.department")
    specs = [
        {"spec_id": "hr1", "name": "Employee Roster", "domain": "HR",
         "description": "employees and departments with salary"},
        {"spec_id": "cards1", "name": "Credit Card Ledger", "domain": "Cards",
         "description": "credit card transactions and balances"},
    ]
    ranked = feas.heuristic_recommend(estate_text, specs)
    ranked.sort(key=lambda r: -r["score"])
    assert ranked[0]["spec_id"] == "hr1"
    assert next(r for r in ranked if r["spec_id"] == "hr1")["score"] > \
           next(r for r in ranked if r["spec_id"] == "cards1")["score"]


# ── R2: camelCase FK-carrier authority (name-based) ───────────────────────────

def test_infer_fk_carrier_camelcase_matrix():
    tables = {"sales_transactions", "sales_customers"}
    for col in ("customerID", "customerId", "CustomerID", "customer_id", "customerIdentifier"):
        res = feas._infer_fk_carrier(col, "sales_transactions", tables)
        assert res.get("is_fk_carrier") is True, col
        assert res.get("fk_target_table") == "sales_customers", col
    # The authoritative table's OWN key is not self-labeled as a FK carrier.
    assert feas._infer_fk_carrier("customer_id", "sales_customers", tables) == {}
    # A bare id is the table's own key, never a carrier.
    assert feas._infer_fk_carrier("id", "sales_transactions", tables) == {}
    # Singular/plural target resolution.
    assert feas._infer_fk_carrier("customer_id", "orders", {"orders", "customer"})["fk_target_table"] == "customer"
    assert feas._infer_fk_carrier("customer_id", "orders", {"orders", "customers"})["fk_target_table"] == "customers"
    # No referenced table in scope → not a carrier.
    assert feas._infer_fk_carrier("customer_id", "orders", {"orders"}) == {}


# ── R2: entity/authority-aware matching (labeled fixture) ─────────────────────

def _c360_datasets():
    """Customer-360-shaped estate: a customers dimension, a transactions fact
    (with a FK carrier `customerID`), and a suppliers dimension that shares the
    ambiguous `name` column. Table descriptions make token-Jaccard discriminate so
    the fixture is deterministic with embeddings OFF."""
    return [
        {"uri": "estatedataset:1:1:db.sales.sales_customers", "database": "db",
         "schema": "sales", "table": "sales_customers",
         "description": "Customer master records with names, emails and addresses.",
         "columns": [{"name": "customer_id", "data_type": "bigint"},
                     {"name": "name", "data_type": "varchar"},
                     {"name": "email", "data_type": "varchar"}]},
        {"uri": "estatedataset:1:1:db.sales.sales_transactions", "database": "db",
         "schema": "sales", "table": "sales_transactions",
         "description": "Sales transaction ledger with dates and amounts.",
         "columns": [{"name": "transaction_id", "data_type": "bigint"},
                     {"name": "customerID", "data_type": "bigint"},
                     {"name": "transaction_date", "data_type": "date"},
                     {"name": "amount", "data_type": "numeric"}]},
        {"uri": "estatedataset:1:1:db.sales.sales_suppliers", "database": "db",
         "schema": "sales", "table": "sales_suppliers",
         "description": "Supplier reference directory of vendors.",
         "columns": [{"name": "supplier_id", "data_type": "bigint"},
                     {"name": "name", "data_type": "varchar"},
                     {"name": "phone", "data_type": "varchar"}]},
    ]


def _c360_spec():
    return fspec.FeasibilitySpec(
        spec_id="cust360", name="Customer 360", domain="Customers",
        grain=fspec.GrainSpec(keys=["customer_id"]),
        attributes=[
            fspec.SpecAttribute(name="customer_id", type="bigint", concept="customer identifier", is_key=True),
            fspec.SpecAttribute(name="name", type="varchar", concept="customer name"),
            fspec.SpecAttribute(name="transaction_id", type="bigint", concept="transaction identifier"),
            fspec.SpecAttribute(name="transaction_date", type="date", concept="transaction date"),
        ],
    )


def test_c360_entity_affinity_and_fk_authority(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = _c360_spec()
    raw = feas._raw_evidence(spec, _c360_datasets())
    picks = {r["spec_attr"]: (r["dataset_table"], r["column"]) for r in raw["assignment"]}
    # Dimension key wins over the fact-table FK carrier; wrong-entity `name` lands
    # on customers (not suppliers); transaction fields land on the fact table.
    assert picks["customer_id"] == ("sales_customers", "customer_id")
    assert picks["name"] == ("sales_customers", "name")
    assert picks["transaction_id"] == ("sales_transactions", "transaction_id")
    assert picks["transaction_date"] == ("sales_transactions", "transaction_date")
    # The previous wrong pick (the fact-table FK) survives as a ranked, reasoned
    # alternative — not silently dropped.
    cid_alts = raw["alternatives"][feas._attr_id(0, "customer_id")]
    fk_alt = next(a for a in cid_alts if a["table"] == "sales_transactions" and a["column"] == "customerID")
    assert fk_alt["is_fk_carrier"] and not fk_alt["chosen"]
    assert fk_alt["reason"] == "fk_carrier"
    # Separate score components are retained for the tooltip/report.
    chosen = next(r for r in raw["assignment"] if r["spec_attr"] == "customer_id")
    assert {"column_semantic", "table_affinity", "adjusted", "fk_role"} <= set(chosen)


def test_gap_alternatives_carry_reason(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = fspec.FeasibilitySpec(
        spec_id="nm", name="No Match", domain="X",
        attributes=[fspec.SpecAttribute(name="iban", type="varchar",
                                        concept="international bank account number")])
    raw = feas._raw_evidence(spec, _c360_datasets())
    assert raw["assignment"] == []  # nothing clears the match threshold
    gap = raw["gaps"][0]
    assert gap["spec_attr"] == "iban" and gap["status"] == "missing"
    assert gap["reason"] in ("below_threshold", "insufficient_evidence")
    alts = raw["alternatives"][feas._attr_id(0, "iban")]
    assert alts and all("reason" in a and not a["chosen"] for a in alts)


def test_alternatives_keyed_by_stable_id_dup_names(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = fspec.FeasibilitySpec(
        spec_id="dup", name="Dup Names", domain="X",
        attributes=[fspec.SpecAttribute(name="status", concept="order status"),
                    fspec.SpecAttribute(name="amount", type="numeric", concept="amount"),
                    fspec.SpecAttribute(name="status", concept="account status")])
    raw = feas._raw_evidence(spec, _c360_datasets())
    # Two attrs named "status" get distinct stable ids — never collapsed by name.
    assert feas._attr_id(0, "status") in raw["alternatives"]
    assert feas._attr_id(2, "status") in raw["alternatives"]
    assert feas._attr_id(0, "status") != feas._attr_id(2, "status")


# ── R2: grain-key hard gate (F-lite) ──────────────────────────────────────────

def test_grain_key_gate_caps_tier(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    # required attrs fully covered (→ assemblable) but the grain key is unmatched.
    spec = fspec.FeasibilitySpec(
        spec_id="g1", name="Grain Spec", domain="HR",
        grain=fspec.GrainSpec(keys=["employee_id"]),
        attributes=[
            fspec.SpecAttribute(name="employee_id", required=False, concept="employee identifier"),
            _attr("employee_name", type="varchar", concept="employee name"),
            _attr("department_name", type="varchar", concept="department name"),
        ],
    )
    datasets = [{"uri": "estatedataset:1:1:db.hr.people", "database": "db", "schema": "hr", "table": "people",
                 "columns": [{"name": "employee_name", "data_type": "varchar"},
                             {"name": "department_name", "data_type": "varchar"}]}]
    ev = feas.build_spec_evidence(spec, [], datasets)  # whole-estate
    # Without the spec, the heuristic would call this assemblable…
    assert feas.heuristic_verdict(ev, "completed")["tier"] == "assemblable"
    # …but the grain-key gate caps it to absent (with an explicit reason).
    v = feas.heuristic_verdict(ev, "completed", spec)
    assert v["tier"] == "absent"
    assert "employee_id" in v["grain_gate"]["unmatched_essential"]
    assert v["grain_gate"]["capped_from"] == "assemblable"


# ── R2: structured schema reason codes ────────────────────────────────────────

def test_schema_shortlist_reason_codes(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = _emp_spec()
    datasets = _sales_and_hr_datasets()
    by_key = _group_by_key(datasets)
    desc_sources = {("db", "employees"): "llm", ("db", "sales"): "fallback"}
    shortlist = feas._schema_shortlist(spec, by_key, {}, floor=10, cap=1,
                                       desc_source_map=desc_sources)
    for row in shortlist:
        assert {"band", "threshold_distance", "description_source"} <= set(row)
    assert all("excluded_reason" in r for r in shortlist if not r["included"])
    # build_spec_evidence annotates matched_tables + prose on the included rows.
    ev = feas.build_spec_evidence(spec, [], datasets, shortlist)
    incl = next(r for r in ev["schema_shortlist"] if r["included"])
    assert incl["schema"] == "employees"
    assert "matched_tables" in incl and "rationale" in incl and incl["rationale"]
    assert "required" in incl["rationale"].lower()


# ── R2: schema-description fallback + provenance (Workstream A) ────────────────

def test_fallback_schema_description_and_read_time():
    from workbench.backend import estate_enrich as ee
    datasets = [{"table": "orders"}, {"table": "customers"}]
    desc = ee._fallback_schema_description("sales", {}, datasets)
    assert desc and "sales" in desc and "orders" in desc
    assert ee._fallback_schema_description("empty", {}, []) == ""
    # Read-time fallback synthesizes for a schema with no stored description.
    by_key = {("db", "sales"): [{"table": "orders", "description": "Order lines"},
                                {"table": "customers"}]}
    resolved = feas._resolve_schema_descs({}, by_key)
    assert resolved[("db", "sales")]["source"] == "fallback"
    assert "orders" in resolved[("db", "sales")]["text"]
    # A stored llm description is preserved, never overwritten by the fallback.
    stored = {("db", "sales"): {"text": "Real subject-area prose.", "source": "llm"}}
    assert feas._resolve_schema_descs(stored, by_key)[("db", "sales")] == \
        {"text": "Real subject-area prose.", "source": "llm"}


# ── R2: progress round-trip (Workstream B) ────────────────────────────────────

def test_progress_persist_round_trip():
    from sqlmodel import Session
    from workbench.backend.database import engine
    from workbench.backend.models import Estate, EstateScan, FeasibilityRun
    from workbench.backend.routers import feasibility as feas_router
    import json as _json

    with Session(engine, expire_on_commit=False) as s:
        est = Estate(name="E", created_by="po@x.com"); s.add(est); s.commit(); s.refresh(est)
        scan = EstateScan(estate_id=est.id, source_id=1, scan_version=1, state="completed")
        s.add(scan); s.commit(); s.refresh(scan)
        run = FeasibilityRun(estate_id=est.id, scan_id=scan.id, state="running")
        s.add(run); s.commit(); s.refresh(run)
        rid, sid, eid = run.id, scan.id, est.id

    feas._persist_progress(rid, {"stage": "evaluating", "specs_total": 5, "specs_done": 2})
    with Session(engine) as s:
        run = s.get(FeasibilityRun, rid)
        p = _json.loads(run.progress_json)
        assert p["stage"] == "evaluating" and p["specs_done"] == 2 and "updated_at" in p
        # _run_row exposes progress + tolerates malformed json.
        assert feas_router._run_row(run, s, include_scores=False)["progress"]["stage"] == "evaluating"
        run.progress_json = "{not valid json"
        s.add(run); s.commit()
        assert feas_router._run_row(run, s, include_scores=False)["progress"] == {}
        s.delete(s.get(FeasibilityRun, rid)); s.delete(s.get(EstateScan, sid))
        s.delete(s.get(Estate, eid)); s.commit()


def test_evaluate_estate_wide_422_when_no_scan():
    from fastapi import HTTPException
    from sqlmodel import Session
    from workbench.backend.auth import AuthUser
    from workbench.backend.database import engine
    from workbench.backend.models import Estate
    from workbench.backend.routers import feasibility as feas_router

    owner = AuthUser(email="po@x.com", name="PO", role="owner")
    with Session(engine, expire_on_commit=False) as s:
        est = Estate(name="E", created_by="po@x.com"); s.add(est); s.commit(); s.refresh(est)
        body = feas_router.EvaluateRequest(estate_id=est.id)
        with pytest.raises(HTTPException) as exc:
            feas_router.evaluate(body, session=s, user=owner, _role=None)
        assert exc.value.status_code == 422
        s.delete(s.get(Estate, est.id)); s.commit()


# ══ R3: composite-derivation engine (single-pass reconciliation) ══════════════

from workbench.backend import feasibility_derivations as fderiv  # noqa: E402


def _ws3_customers_datasets():
    """WS3 fixture: `sales_customers(first_name,last_name)` has NO `name` column,
    while `sales_suppliers(name)` does. Table descriptions are chosen so the
    token-Jaccard affinity (embeddings OFF) makes `name` compose on customers and
    beat the wrong-entity supplier column deterministically."""
    return [
        {"uri": "estatedataset:1:1:db.sales.sales_customers", "database": "db",
         "schema": "sales", "table": "sales_customers",
         "description": "Customer full name and customer contact details.",
         "columns": [{"name": "customer_id", "data_type": "bigint"},
                     {"name": "first_name", "data_type": "varchar"},
                     {"name": "last_name", "data_type": "varchar"},
                     {"name": "email", "data_type": "varchar"}]},
        {"uri": "estatedataset:1:1:db.sales.sales_suppliers", "database": "db",
         "schema": "sales", "table": "sales_suppliers",
         "description": "Supplier vendor reference directory with phone numbers.",
         "columns": [{"name": "supplier_id", "data_type": "bigint"},
                     {"name": "name", "data_type": "varchar"},
                     {"name": "phone", "data_type": "varchar"}]},
        {"uri": "estatedataset:1:1:db.sales.sales_transactions", "database": "db",
         "schema": "sales", "table": "sales_transactions",
         "description": "Sales transaction ledger with amounts.",
         "columns": [{"name": "transaction_id", "data_type": "bigint"},
                     {"name": "customerID", "data_type": "bigint"},
                     {"name": "amount", "data_type": "numeric"}]},
    ]


def _ws3_spec():
    return fspec.FeasibilitySpec(
        spec_id="cust360", name="Customer 360", domain="Customers",
        grain=fspec.GrainSpec(keys=["customer_id"]),
        attributes=[
            fspec.SpecAttribute(name="customer_id", type="bigint", concept="customer identifier", is_key=True),
            fspec.SpecAttribute(name="name", type="varchar", concept="customer full name"),
            fspec.SpecAttribute(name="transaction_id", type="bigint", concept="transaction identifier"),
        ],
    )


# ── catalog loader (fail-closed, versions) ────────────────────────────────────

def test_derivation_catalog_deployed_and_versions():
    pats = fderiv.load_patterns()
    assert len(pats) >= 3
    assert fderiv.catalog_version() != "0"
    assert fderiv.schema_version()
    ids = {p.id for p in pats}
    assert {"full_name_concat", "age_from_dob"} <= ids


def test_derivation_catalog_fail_closed_whole():
    good = {"id": "x", "target_aliases": ["x"], "kind": "concat",
            "components": [{"role": "a", "aliases": ["a"]}]}
    bad = {"id": "y", "kind": "concat"}  # missing target_aliases + components
    with pytest.raises(fderiv.DerivationCatalogError):
        fderiv.parse_patterns([good, bad])       # one bad entry rejects the whole list
    with pytest.raises(fderiv.DerivationCatalogError):
        fderiv.parse_patterns([good, dict(good)])  # duplicate id rejected
    ok = fderiv.parse_patterns([good])
    assert len(ok) == 1 and ok[0].id == "x"


def test_load_curated_patterns_surfaces_error_not_silent(monkeypatch):
    def _boom():
        raise fderiv.DerivationCatalogError("malformed pattern #2")
    monkeypatch.setattr(fderiv, "load_patterns", _boom)
    pats, err = feas._load_curated_patterns()
    assert pats == [] and "malformed pattern" in err  # surfaced, not silent-empty


# ── component resolution (injective + type gate) ──────────────────────────────

def test_resolve_components_injective(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    roles = [("a", ["value"], ""), ("b", ["value"], "")]
    one_col = [(0, "value", "varchar"), (1, "other", "varchar")]
    assert feas._resolve_components(roles, one_col) is None  # two roles, one match → fail
    two_col = [(0, "value", "varchar"), (7, "value", "varchar")]
    res = feas._resolve_components(roles, two_col)
    assert res is not None and len({r["col_j"] for r in res}) == 2  # distinct columns


def test_resolve_components_type_family_gate(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    roles = [("birth_date", ["birth_date", "dob"], "temporal")]
    assert feas._resolve_components(roles, [(0, "birth_date", "varchar")]) is None  # non-temporal
    assert feas._resolve_components(roles, [(0, "birth_date", "date")]) is not None


# ── headline: composite beats a wrong-entity direct ───────────────────────────

def test_composite_name_beats_wrong_entity_supplier(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    raw = feas._raw_evidence(_ws3_spec(), _ws3_customers_datasets())
    name_row = next(r for r in raw["assignment"] if r["spec_attr"] == "name")
    assert name_row["match_kind"] == "composite"
    assert name_row["dataset_table"] == "sales_customers"
    assert name_row["derivation"] == "concat"
    assert name_row["column"] == "first_name + last_name"
    comp = name_row["composite"]
    assert comp["operator"] == "join_with_separator" and comp["source"] == "curated"
    assert {c["role"] for c in comp["components"]} == {"given_name", "family_name"}
    # The displaced supplier direct pick survives as an alternative, reasoned.
    alts = raw["alternatives"][feas._attr_id(1, "name")]
    sup = next(a for a in alts if a["table"] == "sales_suppliers" and a["column"] == "name")
    assert sup["reason"] == "superseded_by_derivation" and not sup["chosen"]
    assert not any(a["chosen"] for a in alts)  # a composite won → no chosen direct column


# ── component columns reusable for a direct attribute in the same spec ────────

def test_component_columns_reusable_for_direct(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = fspec.FeasibilitySpec(
        spec_id="reuse", name="Reuse", domain="Customers",
        attributes=[fspec.SpecAttribute(name="name", type="varchar", concept="customer full name"),
                    fspec.SpecAttribute(name="first_name", type="varchar", concept="first name")])
    raw = feas._raw_evidence(spec, _ws3_customers_datasets())
    name_row = next(r for r in raw["assignment"] if r["spec_attr"] == "name")
    fn_row = next(r for r in raw["assignment"] if r["spec_attr"] == "first_name")
    assert name_row["match_kind"] == "composite"
    assert "first_name" in [c["column"] for c in name_row["composite"]["components"]]
    assert fn_row["match_kind"] == "direct" and fn_row["column"] == "first_name"  # reused


# ── column freed by a composite win is reconsidered (single-pass, no stale) ────

def test_freed_column_reconsidered_single_pass(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    # Two attrs named `name`: attr0 (customer full name) composes on customers and
    # would otherwise have taken suppliers.name directly; attr1 (vendor/supplier
    # name) is clearly supplier-affine and directly matches suppliers.name — proving
    # the column attr0 didn't consume stays in the pool for attr1.
    spec = fspec.FeasibilitySpec(
        spec_id="freed", name="Freed", domain="Sales",
        attributes=[fspec.SpecAttribute(name="name", type="varchar", concept="customer full name"),
                    fspec.SpecAttribute(name="name", type="varchar", concept="vendor supplier name")])
    raw = feas._raw_evidence(spec, _ws3_customers_datasets())
    by_id = {r["attr_id"]: r for r in raw["assignment"]}
    a0 = by_id[feas._attr_id(0, "name")]
    a1 = by_id[feas._attr_id(1, "name")]
    assert a0["match_kind"] == "composite" and a0["dataset_table"] == "sales_customers"
    assert a1["match_kind"] == "direct" and a1["dataset_table"] == "sales_suppliers" and a1["column"] == "name"


# ── margin: equal-authority direct not superseded; a gap is filled uncontested ─

def test_equal_authority_direct_not_superseded(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    # `display_name` has BOTH a direct column and composable first/last on ONE table
    # → same affinity → the penalty keeps the direct winning.
    spec = fspec.FeasibilitySpec(
        spec_id="disp", name="Disp", domain="People",
        attributes=[fspec.SpecAttribute(name="display_name", type="varchar", concept="display name")])
    datasets = [{"uri": "estatedataset:1:1:db.p.people", "database": "db", "schema": "p", "table": "people",
                 "description": "People with display name, first name and last name.",
                 "columns": [{"name": "person_id", "data_type": "bigint"},
                             {"name": "display_name", "data_type": "varchar"},
                             {"name": "first_name", "data_type": "varchar"},
                             {"name": "last_name", "data_type": "varchar"}]}]
    row = next(r for r in feas._raw_evidence(spec, datasets)["assignment"] if r["spec_attr"] == "display_name")
    assert row["match_kind"] == "direct" and row["column"] == "display_name"


def test_gap_filled_by_composite_uncontested(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = fspec.FeasibilitySpec(
        spec_id="gapfill", name="GapFill", domain="Customers",
        attributes=[fspec.SpecAttribute(name="name", type="varchar", concept="customer full name")])
    datasets = [{"uri": "estatedataset:1:1:db.sales.sales_customers", "database": "db",
                 "schema": "sales", "table": "sales_customers",
                 "description": "Customer full name and customer contact details.",
                 "columns": [{"name": "first_name", "data_type": "varchar"},
                             {"name": "last_name", "data_type": "varchar"},
                             {"name": "email", "data_type": "varchar"}]}]
    row = next(r for r in feas._raw_evidence(spec, datasets)["assignment"] if r["spec_attr"] == "name")
    assert row["match_kind"] == "composite"  # no competing direct → filled with no margin


# ── same-table-only: components split across tables → no composite ────────────

def test_same_table_only_no_cross_table_composite(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = fspec.FeasibilitySpec(
        spec_id="split", name="Split", domain="Customers",
        attributes=[fspec.SpecAttribute(name="name", type="varchar", concept="customer full name")])
    datasets = [
        {"uri": "estatedataset:1:1:db.c.t1", "database": "db", "schema": "c", "table": "t1",
         "description": "Customer full name first parts.",
         "columns": [{"name": "first_name", "data_type": "varchar"}]},
        {"uri": "estatedataset:1:1:db.c.t2", "database": "db", "schema": "c", "table": "t2",
         "description": "Customer full name last parts.",
         "columns": [{"name": "last_name", "data_type": "varchar"}]},
    ]
    raw = feas._raw_evidence(spec, datasets)
    assert not any(r.get("match_kind") == "composite" for r in raw["assignment"])
    assert raw["attribute_status"][feas._attr_id(0, "name")] == "missing"


# ── grain gate: composition is skipped for keys and never satisfies the gate ──

def test_grain_key_not_satisfied_by_composition(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    # `name` is the grain key (essential) AND composable — but composition is
    # skipped for keys, so it stays unmatched and the grain gate caps the tier.
    spec = fspec.FeasibilitySpec(
        spec_id="gk", name="GrainKey", domain="Customers",
        grain=fspec.GrainSpec(keys=["name"]),
        attributes=[fspec.SpecAttribute(name="name", type="varchar", concept="customer full name", required=True),
                    fspec.SpecAttribute(name="email", type="varchar", concept="email", required=True),
                    fspec.SpecAttribute(name="phone", type="varchar", concept="phone", required=True)])
    datasets = [{"uri": "estatedataset:1:1:db.sales.sales_customers", "database": "db",
                 "schema": "sales", "table": "sales_customers",
                 "description": "Customer full name, email and phone contact details.",
                 "columns": [{"name": "first_name", "data_type": "varchar"},
                             {"name": "last_name", "data_type": "varchar"},
                             {"name": "email", "data_type": "varchar"},
                             {"name": "phone", "data_type": "varchar"}]}]
    raw = feas._raw_evidence(spec, datasets)
    assert not any(r["spec_attr"] == "name" for r in raw["assignment"])  # never composed
    ev = feas.build_spec_evidence(spec, [], datasets)
    v = feas.heuristic_verdict(ev, "completed", spec)
    assert v["tier"] == "absent" and "name" in v["grain_gate"]["unmatched_essential"]


# ── compute type gate on the curated age_from_dob pattern ─────────────────────

def test_age_compute_type_gate(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = fspec.FeasibilitySpec(
        spec_id="ages", name="Ages", domain="People",
        attributes=[fspec.SpecAttribute(name="age", type="int", concept="age")])
    base = {"uri": "estatedataset:1:1:db.p.people", "database": "db", "schema": "p", "table": "people",
            "description": "People and their age and birth_date."}
    # non-temporal birth_date → rejected → gap
    ds_text = [dict(base, columns=[{"name": "person_id", "data_type": "bigint"},
                                   {"name": "birth_date", "data_type": "varchar"}])]
    assert not any(r.get("match_kind") == "composite" for r in feas._raw_evidence(spec, ds_text)["assignment"])
    # temporal birth_date → composes
    ds_date = [dict(base, columns=[{"name": "person_id", "data_type": "bigint"},
                                   {"name": "birth_date", "data_type": "date"}])]
    row = next((r for r in feas._raw_evidence(spec, ds_date)["assignment"] if r["spec_attr"] == "age"), None)
    assert row is not None and row["match_kind"] == "composite" and row["derivation"] == "compute"


# ── determinism ───────────────────────────────────────────────────────────────

def test_composite_reconciliation_deterministic(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec, ds = _ws3_spec(), _ws3_customers_datasets()
    import json as _json
    a = _json.dumps(feas._raw_evidence(spec, ds)["assignment"], sort_keys=True, default=str)
    b = _json.dumps(feas._raw_evidence(spec, ds)["assignment"], sort_keys=True, default=str)
    assert a == b


# ── LLM proposals: exact-ref validation, gaps-only, apply, audit ──────────────

def _proposal_ctx():
    spec = fspec.FeasibilitySpec(
        spec_id="lbl", name="Label", domain="X",
        attributes=[fspec.SpecAttribute(name="label", type="varchar", concept="label"),
                    fspec.SpecAttribute(name="email", type="varchar", concept="email")])
    datasets = [{"uri": "estatedataset:1:1:db.s.t", "database": "db", "schema": "s", "table": "t",
                 "columns": [{"name": "part_a", "data_type": "varchar"},
                             {"name": "part_b", "data_type": "varchar"},
                             {"name": "email", "data_type": "varchar"}]}]
    return spec, datasets


def _fresh_audit():
    return {"rejected_by_reason": {"unknown_column": 0, "mixed_tables": 0,
                                   "wrong_spec": 0, "type_incompatible": 0}}


def test_llm_proposal_reject_reasons(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec, datasets = _proposal_ctx()
    unmatched = {feas._attr_id(0, "label")}
    aid = feas._attr_id(0, "label")
    # unknown column
    a1 = _fresh_audit()
    r = feas._validate_proposals([{"spec_id": "lbl", "attribute_id": aid, "kind": "concat",
        "table_ref": "t", "components": [{"role": "a", "column_ref": "nope"},
                                         {"role": "b", "column_ref": "part_b"}]}],
        spec, datasets, unmatched, a1)
    assert r == [] and a1["rejected_by_reason"]["unknown_column"] == 1
    # wrong spec / wrong attribute id
    a2 = _fresh_audit()
    r = feas._validate_proposals([{"spec_id": "lbl", "attribute_id": "#9:ghost", "kind": "concat",
        "table_ref": "t", "components": [{"role": "a", "column_ref": "part_a"},
                                         {"role": "b", "column_ref": "part_b"}]}],
        spec, datasets, unmatched, a2)
    assert r == [] and a2["rejected_by_reason"]["wrong_spec"] == 1
    # mixed tables (a column that lives in a DIFFERENT scoped table)
    a3 = _fresh_audit()
    datasets2 = datasets + [{"uri": "estatedataset:1:1:db.s.t2", "database": "db", "schema": "s",
                             "table": "t2", "columns": [{"name": "part_c", "data_type": "varchar"}]}]
    r = feas._validate_proposals([{"spec_id": "lbl", "attribute_id": aid, "kind": "concat",
        "table_ref": "t", "components": [{"role": "a", "column_ref": "part_a"},
                                         {"role": "b", "column_ref": "part_c"}]}],
        spec, datasets2, unmatched, a3)
    assert r == [] and a3["rejected_by_reason"]["mixed_tables"] == 1


def test_llm_proposal_valid_applies_and_cannot_supersede_direct(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec, datasets = _proposal_ctx()
    unmatched = {feas._attr_id(0, "label")}
    good = [{"spec_id": "lbl", "attribute_id": feas._attr_id(0, "label"), "kind": "concat",
             "table_ref": "t", "operator": "join_with_separator", "separator": " ",
             "components": [{"role": "a", "column_ref": "part_a"},
                            {"role": "b", "column_ref": "part_b"}]}]
    audit = _fresh_audit()
    valid = feas._validate_proposals(good, spec, datasets, unmatched, audit)
    assert len(valid) == 1
    raw = feas._raw_evidence(spec, datasets, extra_proposals=valid)
    label_row = next(r for r in raw["assignment"] if r["spec_attr"] == "label")
    assert label_row["match_kind"] == "composite" and label_row["composite"]["source"] == "advisor"
    # A proposal targeting an attr that HAS a direct match is ignored (gaps-only).
    email_prop = feas.fderiv.ProposedDerivation(
        spec_id="lbl", attribute_id=feas._attr_id(1, "email"),
        kind=fspec.DerivationKind.concat, table_ref="t",
        components=[{"role": "a", "column_ref": "part_a"}, {"role": "b", "column_ref": "part_b"}])
    raw2 = feas._raw_evidence(spec, datasets, extra_proposals=[email_prop])
    email_row = next(r for r in raw2["assignment"] if r["spec_attr"] == "email")
    assert email_row["match_kind"] == "direct"


def test_composite_penalty_ramps_with_affinity_gap():
    P = float(feas.DERIVATION_PENALTY)
    # Composite table no more relevant than the best direct's → full penalty (a real
    # single column is preferred): equal table, and a strictly-less-relevant table.
    assert feas._composite_penalty(68, 68) == P
    assert feas._composite_penalty(60, 65) == P
    # Clearly more relevant (gap ≥ scale) → penalty fully removed. Mirrors the live
    # customer-360 case (customers table-fit 68 vs suppliers 62).
    assert feas._composite_penalty(68, 62) == 0.0
    assert feas._composite_penalty(50.0, 0.0) == 0.0            # a gap attribute (no direct)
    # A partial edge ramps SMOOTHLY between full and zero — no threshold cliff.
    mid = feas._composite_penalty(64, 62)                        # gap 2 of scale 6
    assert 0.0 < mid < P
    # Monotonic: a wider edge is penalised no more than a narrower one.
    assert feas._composite_penalty(66, 62) <= feas._composite_penalty(64, 62)


def test_composite_generalizes_across_domains_employee(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    # Same SHAPE as the customer/supplier case, a DIFFERENT domain — proves the fix
    # is entity-relative (compares the two competing tables' affinities), never
    # hardcoded to customers/suppliers: `name` composes from first_name+last_name on
    # the EMPLOYEE table, beating a wrong-entity departments.name.
    spec = fspec.FeasibilitySpec(
        spec_id="empdir", name="Employee Directory", domain="HR",
        grain=fspec.GrainSpec(keys=["employee_id"]),
        attributes=[fspec.SpecAttribute(name="employee_id", type="bigint", concept="employee identifier", is_key=True),
                    fspec.SpecAttribute(name="name", type="varchar", concept="employee full name")])
    datasets = [
        {"uri": "estatedataset:1:1:db.hr.employees", "database": "db", "schema": "hr", "table": "employees",
         "description": "Employee full name and employee contact records.",
         "columns": [{"name": "employee_id", "data_type": "bigint"},
                     {"name": "first_name", "data_type": "varchar"},
                     {"name": "last_name", "data_type": "varchar"},
                     {"name": "email", "data_type": "varchar"}]},
        {"uri": "estatedataset:1:1:db.hr.departments", "database": "db", "schema": "hr", "table": "departments",
         "description": "Department reference directory of organizational units.",
         "columns": [{"name": "department_id", "data_type": "bigint"},
                     {"name": "name", "data_type": "varchar"},
                     {"name": "location", "data_type": "varchar"}]},
    ]
    raw = feas._raw_evidence(spec, datasets)
    name_row = next(r for r in raw["assignment"] if r["spec_attr"] == "name")
    assert name_row["match_kind"] == "composite" and name_row["dataset_table"] == "employees"
    assert name_row["column"] == "first_name + last_name"
    dep = next(a for a in raw["alternatives"][feas._attr_id(1, "name")]
               if a["table"] == "departments" and a["column"] == "name")
    assert dep["reason"] == "superseded_by_derivation"


# ══ R4 Phase 1: reasoning-ready deterministic fixes + description visibility ═══


def test_spec_features_carries_description():
    # The spec attribute description now reaches the source ColumnFeature so the
    # semantic axis is symmetric with the estate side (which embeds its own prose).
    attrs = [fspec.SpecAttribute(name="name", type="varchar", concept="customer name",
                                 description="the customer full display name")]
    feats = feas._spec_features(attrs)
    assert feats[0].description == "the customer full display name"
    assert "full display name" in feats[0].semantic_text()


def test_s3_name_floor_only_for_equal_names():
    from workbench.backend import schema_dna as sd
    a = sd.ColumnFeature(name="name", concept="customer name")
    # Different names (n_name vs name) must NOT be floored to 1.0 by the tokenizer
    # collapsing both to {name} — the entity-blind false-positive bug. Empty cache →
    # token-Jaccard path.
    b = sd.ColumnFeature(name="n_name", description="nation name")
    assert sd._s3_semantic_pair(a, b, {}) < 1.0
    # Truly-equal names keep the 1.0 floor (a bare source column vs a described attr).
    c = sd.ColumnFeature(name="name", description="nation name")
    assert sd._s3_semantic_pair(a, c, {}) == 1.0


def _reported_c360_spec():
    return fspec.FeasibilitySpec(
        spec_id="c360", name="Customer 360", domain="Customers",
        grain=fspec.GrainSpec(keys=["customer_id"]),
        attributes=[
            fspec.SpecAttribute(name="customer_id", type="bigint", concept="customer identifier",
                                is_key=True, description="unique customer identifier"),
            fspec.SpecAttribute(name="name", type="varchar", concept="customer name",
                                description="the customer full display name"),
        ])


def _reported_c360_datasets():
    return [
        {"uri": "estatedataset:1:1:db.tpch.customer", "database": "db", "schema": "tpch",
         "table": "customer", "description": "Customer master, one row per customer.",
         "columns": [{"name": "c_customer_id", "data_type": "bigint",
                      "description": "customer identifier surrogate key"},
                     {"name": "c_name", "data_type": "varchar",
                      "description": "customer full display name"}]},
        {"uri": "estatedataset:1:1:db.tpch.nation", "database": "db", "schema": "tpch",
         "table": "nation", "description": "Reference list of nations and countries.",
         "columns": [{"name": "n_nationkey", "data_type": "bigint", "description": "nation key"},
                     {"name": "n_name", "data_type": "varchar", "description": "nation name"}]},
    ]


def test_reported_name_maps_to_customer_not_nation(monkeypatch):
    # The exact reported failure: `name` → nation.n_name. With the spec description
    # armed + the name-floor fix, it now lands on customer.c_name.
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    raw = feas._raw_evidence(_reported_c360_spec(), _reported_c360_datasets())
    picks = {r["spec_attr"]: (r["dataset_table"], r["column"]) for r in raw["assignment"]}
    assert picks["name"] == ("customer", "c_name")
    assert picks["customer_id"] == ("customer", "c_customer_id")


def _customer_type_gate_fixture():
    spec = fspec.FeasibilitySpec(
        spec_id="seg", name="Seg", domain="X",
        attributes=[fspec.SpecAttribute(name="customer_type", type="varchar",
                                        concept="customer type category",
                                        description="customer type category classification")])
    ds = [{"uri": "estatedataset:1:1:db.s.customer", "database": "db", "schema": "s",
           "table": "customer", "description": "Customer master.",
           "columns": [{"name": "customer_type_id", "data_type": "varchar",
                        "description": "customer type category classification code"}]}]
    return spec, ds


def test_identifier_gate_demotes_category_to_id(monkeypatch):
    # A category attribute matching an identifier-shaped column (score in the gray
    # band, i.e. ABOVE the match threshold) is demoted to a gap by the identifier
    # gate — not silently accepted as a direct match.
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec, ds = _customer_type_gate_fixture()
    raw = feas._raw_evidence(spec, ds)
    assert raw["assignment"] == []  # gated out → gap
    gap = raw["gaps"][0]
    assert gap["spec_attr"] == "customer_type" and gap["reason"] == "identifier_mismatch"
    alt = raw["alternatives"][feas._attr_id(0, "customer_type")][0]
    # The gated column scored ABOVE threshold (gray band) — proving it was the gate,
    # not the threshold, that demoted it.
    assert alt["reason"] == "identifier_mismatch"
    assert alt["column_score"] >= feas.schema_dna.MATCH_THRESHOLD


def test_identifier_helpers():
    A = fspec.SpecAttribute
    for name in ("customer_id", "customerID", "id", "c_customer_id", "CustomerIdentifier"):
        assert feas._is_identifier_name(name), name
    for name in ("customer_type", "name", "amount", "segment"):
        assert not feas._is_identifier_name(name), name
    assert feas._is_identifier_attr(A(name="customer_id", concept="x"))
    assert feas._is_identifier_attr(A(name="foo", concept="x", is_key=True))
    assert not feas._is_identifier_attr(A(name="customer_type", concept="x"))
    # A shape mismatch is exactly-one-side-identifier.
    assert feas._identifier_shape_mismatch(A(name="customer_type", concept="x"), "c_customer_id")
    assert feas._identifier_shape_mismatch(A(name="customer_id", concept="x"), "name")
    assert not feas._identifier_shape_mismatch(A(name="customer_id", concept="x"), "c_customer_id")
    assert not feas._identifier_shape_mismatch(A(name="name", concept="x"), "c_name")


def test_strong_id_match_survives_gate(monkeypatch):
    # An id↔id match (both identifier-shaped) is NOT gated — the gate only fires on a
    # shape MISMATCH, so genuine key matches are preserved.
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = fspec.FeasibilitySpec(
        spec_id="k", name="K", domain="X",
        attributes=[fspec.SpecAttribute(name="card_id", type="bigint", concept="card identifier",
                                        is_key=True, description="card identifier")])
    ds = [{"uri": "estatedataset:1:1:db.s.cards", "database": "db", "schema": "s", "table": "cards",
           "description": "Card master.",
           "columns": [{"name": "card_id", "data_type": "bigint", "description": "card identifier"}]}]
    row = next(r for r in feas._raw_evidence(spec, ds)["assignment"] if r["spec_attr"] == "card_id")
    assert row["column"] == "card_id"


def test_assignment_carries_evidence_quality_and_name_only(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    # Both sides described → name_only False.
    spec = fspec.FeasibilitySpec(
        spec_id="d1", name="D", domain="X",
        attributes=[fspec.SpecAttribute(name="email", type="varchar", concept="email",
                                        description="email address")])
    ds = [{"uri": "estatedataset:1:1:db.s.t", "database": "db", "schema": "s", "table": "t",
           "columns": [{"name": "email", "data_type": "varchar", "description": "contact email"}]}]
    row = next(r for r in feas._raw_evidence(spec, ds)["assignment"] if r["spec_attr"] == "email")
    assert row["name_only"] is False
    assert row["evidence_quality"]["column_described"] and row["evidence_quality"]["attribute_described"]
    # Neither side described → name_only True (the match rests on names alone).
    spec2 = fspec.FeasibilitySpec(
        spec_id="d2", name="D", domain="X",
        attributes=[fspec.SpecAttribute(name="email", type="varchar", concept="email")])
    ds2 = [{"uri": "estatedataset:1:1:db.s.t", "database": "db", "schema": "s", "table": "t",
            "columns": [{"name": "email", "data_type": "varchar"}]}]
    row2 = next(r for r in feas._raw_evidence(spec2, ds2)["assignment"] if r["spec_attr"] == "email")
    assert row2["name_only"] is True


def test_description_coverage_flags_failed_enrichment():
    datasets = [
        {"database": "db", "schema": "good", "table": "t",
         "columns": [{"name": "a", "description": "desc a"}, {"name": "b", "description": ""}]},
        {"database": "db", "schema": "accuweather", "table": "w",
         "columns": [{"name": "x", "description": ""}, {"name": "y"}]},
    ]
    src = {("db", "good"): "llm", ("db", "accuweather"): "llm"}
    cov = feas._description_coverage(datasets, src)
    by = {(s["database"], s["schema"]): s for s in cov["schemas"]}
    assert by[("db", "good")]["columns_described"] == 1
    assert by[("db", "good")]["reenrich_recommended"] is False
    # A schema whose columns are ALL undescribed = a failed enrichment group → flagged.
    assert by[("db", "accuweather")]["columns_described"] == 0
    assert by[("db", "accuweather")]["reenrich_recommended"] is True
    assert "db.accuweather" in cov["weak_schemas"]


# ══ R4 Phase 2: reasoning column-matcher (uncertain-only, exact-ref, pinned) ═══


def test_uncertain_attrs_selects_gray_ambiguous_gaps():
    threshold = feas.schema_dna.MATCH_THRESHOLD
    a0, a1, a2, a3 = (feas._attr_id(0, "seg"), feas._attr_id(1, "name"),
                      feas._attr_id(2, "email"), feas._attr_id(3, "type"))
    ev = {"raw_candidates": {
        "assignment": [
            {"attr_id": a0, "spec_attr": "seg", "match_kind": "direct", "adjusted": 74,
             "score": 74, "name_only": False},   # gray band
            {"attr_id": a1, "spec_attr": "name", "match_kind": "direct", "adjusted": 95,
             "score": 95, "name_only": False},   # confident but ambiguous runner-up
            {"attr_id": a2, "spec_attr": "email", "match_kind": "direct", "adjusted": 96,
             "score": 96, "name_only": False},   # confident + clear
        ],
        "alternatives": {
            a0: [{"chosen": True, "adjusted": 74, "reason": "chosen"}],
            a1: [{"chosen": True, "adjusted": 95, "reason": "chosen"},
                 {"chosen": False, "adjusted": 90, "reason": "wrong_entity"}],  # within margin
            a2: [{"chosen": True, "adjusted": 96, "reason": "chosen"},
                 {"chosen": False, "adjusted": 60, "reason": "below_threshold"}],
            a3: [{"chosen": False, "column_score": threshold - 5, "adjusted": 55,
                  "reason": "below_threshold"}],  # required gap near-miss
        },
        "gaps": [{"attr_id": a3, "spec_attr": "type", "required": True}],
    }}
    u = feas._uncertain_attrs(ev)
    assert a0 in u and a1 in u and a3 in u
    assert a2 not in u  # a confident, unambiguous match is NOT sent to the LLM


def test_validate_matches_fail_closed():
    spec = fspec.FeasibilitySpec(
        spec_id="s", name="S", domain="X",
        attributes=[fspec.SpecAttribute(name="a", concept="a"),
                    fspec.SpecAttribute(name="b", concept="b")])
    aid0 = feas._attr_id(0, "a")
    uncertain = {aid0}
    shown = {aid0: [("t", "col_a"), ("t", "col_b")]}

    def _audit():
        return {"rejected_by_reason": {"unknown_column": 0, "wrong_spec": 0,
                                       "duplicate": 0, "malformed": 0}}

    au = _audit()
    v = feas._validate_matches([{"spec_id": "s", "attribute_id": aid0, "decision": "match",
                                 "table_ref": "t", "column_ref": "col_a"}], spec, uncertain, shown, au)
    assert len(v) == 1 and v[0].column_ref == "col_a"
    # unknown column (not one of the shown candidates)
    au = _audit()
    v = feas._validate_matches([{"spec_id": "s", "attribute_id": aid0, "decision": "match",
                                 "table_ref": "t", "column_ref": "ghost"}], spec, uncertain, shown, au)
    assert v == [] and au["rejected_by_reason"]["unknown_column"] == 1
    # wrong spec
    au = _audit()
    v = feas._validate_matches([{"spec_id": "other", "attribute_id": aid0, "decision": "match",
                                 "table_ref": "t", "column_ref": "col_a"}], spec, uncertain, shown, au)
    assert v == [] and au["rejected_by_reason"]["wrong_spec"] == 1
    # wrong attr (not in the uncertain set)
    au = _audit()
    v = feas._validate_matches([{"spec_id": "s", "attribute_id": feas._attr_id(1, "b"),
                                 "decision": "match", "table_ref": "t", "column_ref": "col_a"}],
                               spec, uncertain, shown, au)
    assert v == [] and au["rejected_by_reason"]["wrong_spec"] == 1
    # gap is always structurally valid
    au = _audit()
    v = feas._validate_matches([{"spec_id": "s", "attribute_id": aid0, "decision": "gap"}],
                               spec, uncertain, shown, au)
    assert len(v) == 1 and v[0].decision == "gap"
    # malformed decision value
    au = _audit()
    v = feas._validate_matches([{"spec_id": "s", "attribute_id": aid0, "decision": "maybe"}],
                               spec, uncertain, shown, au)
    assert v == [] and au["rejected_by_reason"]["malformed"] == 1
    # duplicate attribute decision
    au = _audit()
    dec = {"spec_id": "s", "attribute_id": aid0, "decision": "match",
           "table_ref": "t", "column_ref": "col_a"}
    v = feas._validate_matches([dec, dict(dec)], spec, uncertain, shown, au)
    assert len(v) == 1 and au["rejected_by_reason"]["duplicate"] == 1


def test_matcher_pins_override_and_reconcile(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec, ds = _reported_c360_spec(), _reported_c360_datasets()
    base = feas._raw_evidence(spec, ds)
    assert next(r for r in base["assignment"] if r["spec_attr"] == "name")["column"] == "c_name"
    # A validated LLM decision re-points `name` onto nation.n_name (an override) and
    # the reconciliation stays 1:1 — customer_id keeps its own column.
    pins = {feas._attr_id(1, "name"): {"decision": "match", "table": "nation", "column": "n_name"}}
    pinned = feas._raw_evidence(spec, ds, pinned_matches=pins)
    name_row = next(r for r in pinned["assignment"] if r["spec_attr"] == "name")
    assert name_row["dataset_table"] == "nation" and name_row["column"] == "n_name"
    assert name_row.get("matcher_pinned") is True
    cid = next(r for r in pinned["assignment"] if r["spec_attr"] == "customer_id")
    assert cid["column"] == "c_customer_id"


def test_matcher_gap_pin_drops_match(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec, ds = _reported_c360_spec(), _reported_c360_datasets()
    pins = {feas._attr_id(1, "name"): {"decision": "gap"}}
    gapped = feas._raw_evidence(spec, ds, pinned_matches=pins)
    assert not any(r["spec_attr"] == "name" for r in gapped["assignment"])
    assert any(g["spec_attr"] == "name" for g in gapped["gaps"])


def test_matcher_pin_rescues_identifier_gated(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec, ds = _customer_type_gate_fixture()
    assert feas._raw_evidence(spec, ds)["assignment"] == []  # gated → gap
    # The reasoning matcher rescues a genuine one by pinning the shown column.
    pins = {feas._attr_id(0, "customer_type"):
            {"decision": "match", "table": "customer", "column": "customer_type_id"}}
    rescued = feas._raw_evidence(spec, ds, pinned_matches=pins)
    row = next(r for r in rescued["assignment"] if r["spec_attr"] == "customer_type")
    assert row["column"] == "customer_type_id" and row.get("matcher_pinned") is True


def test_pins_from_decisions_shape():
    decs = [
        feas.fderiv.MatchDecision(spec_id="s", attribute_id="#1:name", decision="match",
                                  table_ref="customer", column_ref="c_name"),
        feas.fderiv.MatchDecision(spec_id="s", attribute_id="#2:x", decision="gap"),
    ]
    pins = feas._pins_from_decisions(decs)
    assert pins["#1:name"] == {"decision": "match", "table": "customer", "column": "c_name"}
    assert pins["#2:x"] == {"decision": "gap"}


def test_evaluate_run_applies_matcher_gap_decision(monkeypatch):
    """End-to-end: a name-only match makes an attribute uncertain; a monkeypatched
    reasoning matcher gaps it → the persisted score drops the match and the run
    summary's matcher audit records the gap. Locks the evaluate_run wiring."""
    import asyncio
    import json as _json
    from sqlmodel import Session, select
    from workbench.backend.database import engine
    from workbench.backend.models import Estate, EstateScan, FeasibilityRun, FeasibilityScore

    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)

    # One spec, one estate column, no descriptions on either side → a name-only
    # (uncertain) match the matcher will be asked to adjudicate.
    only_spec = fspec.FeasibilitySpec(
        spec_id="mono", name="Mono", domain="MatcherTest",
        attributes=[fspec.SpecAttribute(name="email", type="varchar", concept="email")])
    monkeypatch.setattr(feas.template_corpus, "load_specs_from_graph",
                        lambda session, domain=None: [only_spec])
    monkeypatch.setattr(feas, "enumerate_product_candidates", lambda session: [])

    def _ds(session, scan_ids):
        return [{"uri": "estatedataset:1:1:db.s.t", "database": "db", "schema": "s", "table": "t",
                 "columns": [{"name": "email", "data_type": "varchar"}]}]
    monkeypatch.setattr(feas.estate_mod, "read_estate_datasets", _ds)

    async def _no_eval(bundle):
        return None
    async def _no_advisor(payload):
        return None
    async def _gap_email(payload_specs):
        # The matcher declares the (uncertain) name-only email match a genuine gap.
        aid = feas._attr_id(0, "email")
        return [{"spec_id": "mono", "attribute_id": aid, "decision": "gap",
                 "confidence": 0.9, "reason": "no real email column"}]
    monkeypatch.setattr(feas, "_run_evaluator_skill", _no_eval)
    monkeypatch.setattr(feas, "_run_derivation_advisor_skill", _no_advisor)
    monkeypatch.setattr(feas, "_run_matcher_skill", _gap_email)

    with Session(engine, expire_on_commit=False) as s:
        est = Estate(name="E", domain="MatcherTest", created_by="po@x.com")
        s.add(est); s.commit(); s.refresh(est)
        scan = EstateScan(estate_id=est.id, source_id=1, scan_version=1, state="completed")
        s.add(scan); s.commit(); s.refresh(scan)
        run = FeasibilityRun(estate_id=est.id, scan_id=scan.id, domain="MatcherTest",
                             state="queued", schema_scoping_json=_json.dumps({"enabled": False}))
        s.add(run); s.commit(); s.refresh(run)
        rid, eid, sid = run.id, est.id, scan.id

    with Session(engine) as s:
        asyncio.run(feas.evaluate_run(s, rid))

    with Session(engine) as s:
        run = s.get(FeasibilityRun, rid)
        summary = _json.loads(run.summary_json)
        assert summary["matcher"]["attempted"] is True
        assert summary["matcher"]["decisions_valid"] == 1
        assert summary["matcher"]["gaps_created"] == 1
        score = s.exec(select(FeasibilityScore).where(FeasibilityScore.run_id == rid)).first()
        matched = _json.loads(score.matched_json or "[]")
        # The name-only match was dropped by the matcher's gap decision.
        assert not any(m.get("spec_attr") == "email" for m in matched)
        # cleanup
        s.delete(score); s.delete(s.get(FeasibilityRun, rid))
        s.delete(s.get(EstateScan, sid)); s.delete(s.get(Estate, eid)); s.commit()


# ══ Phase 1: castable-type softening (cast_cost + _s2_data_type + cast_hint) ═════

from workbench.backend.platform.type_system import CastCost, cast_cost  # noqa: E402


def test_cast_cost_tiers():
    C = CastCost
    # same family, width order → widening / narrowing / none
    assert cast_cost("int", "bigint") == C.widening
    assert cast_cost("bigint", "int") == C.narrowing_lossy
    assert cast_cost("int", "integer") == C.none
    assert cast_cost("varchar", "text") == C.none
    assert cast_cost("date", "timestamp") == C.widening
    # routine cross-representation casts
    assert cast_cost("varchar", "int") == C.cross_family_castable
    assert cast_cost("int", "varchar") == C.cross_family_castable
    assert cast_cost("boolean", "int") == C.cross_family_castable
    assert cast_cost("varchar", "date") == C.cross_family_castable
    # structural mismatches stay incompatible
    assert cast_cost("json", "int") == C.incompatible
    assert cast_cost("bytea", "varchar") == C.incompatible
    assert cast_cost("array", "text") == C.incompatible
    assert cast_cost("", "int") == C.incompatible  # unknown source


def test_s2_data_type_softens_castable():
    from workbench.backend import schema_dna as sd
    A = sd.ColumnFeature
    # exact + same-family unchanged
    assert sd._s2_data_type(A(name="x", type="int"), A(name="x", type="int")) == 1.0
    assert sd._s2_data_type(A(name="x", type="bigint"), A(name="x", type="int")) == 0.75
    # cross-family CASTABLE is softened above the 0.3 floor…
    assert sd._s2_data_type(A(name="x", type="varchar"), A(name="x", type="int")) == 0.55
    # …but a structural mismatch stays at the floor.
    assert sd._s2_data_type(A(name="x", type="varchar"), A(name="x", type="json")) == 0.3


def test_cast_hint_on_direct_row(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = fspec.FeasibilitySpec(
        spec_id="ch", name="Ch", domain="X",
        attributes=[fspec.SpecAttribute(name="amount", type="int", concept="amount",
                                        description="transaction amount")])
    ds = [{"uri": "estatedataset:1:1:db.s.t", "database": "db", "schema": "s", "table": "t",
           "columns": [{"name": "amount", "data_type": "bigint", "description": "transaction amount"}]}]
    row = next(r for r in feas._raw_evidence(spec, ds)["assignment"] if r["spec_attr"] == "amount")
    assert row["cast_hint"] == {"needed": True, "from": "bigint", "to": "int", "cost": "narrowing_lossy"}
    assert row["status"] == "direct_match"  # a cast is a HINT, not a status change / demotion
    # Identical types → no hint.
    ds_same = [{"uri": "estatedataset:1:1:db.s.t", "database": "db", "schema": "s", "table": "t",
                "columns": [{"name": "amount", "data_type": "int", "description": "transaction amount"}]}]
    row2 = next(r for r in feas._raw_evidence(spec, ds_same)["assignment"] if r["spec_attr"] == "amount")
    assert row2["cast_hint"] is None


def test_r4_cases_survive_cast_softening(monkeypatch):
    # The castable softening must NOT disturb the two reported R4 fixes (it touches only
    # the data_type axis; the identifier gate + entity semantics are untouched).
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    raw = feas._raw_evidence(_reported_c360_spec(), _reported_c360_datasets())
    picks = {r["spec_attr"]: (r["dataset_table"], r["column"]) for r in raw["assignment"]}
    assert picks["name"] == ("customer", "c_name")
    spec, ds = _customer_type_gate_fixture()
    assert feas._raw_evidence(spec, ds)["assignment"] == []  # still gated → gap


# ══ Phase 2: schema-scope override ═════════════════════════════════════════════

def test_schema_shortlist_include_exclude_override(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = _emp_spec()
    by_key = _group_by_key(_sales_and_hr_datasets())
    # Force 'sales' IN (even below an impossible floor) and 'employees' OUT.
    sl = feas._schema_shortlist(spec, by_key, {}, floor=200, cap=3,
                                include_override={("db", "sales")},
                                exclude_override={("db", "employees")})
    inc = {(r["database"], r["schema"]): r["included"] for r in sl}
    assert inc[("db", "sales")] is True
    assert inc[("db", "employees")] is False
    sales = next(r for r in sl if r["schema"] == "sales")
    assert sales.get("override") == "user_included"
    emp = next(r for r in sl if r["schema"] == "employees")
    assert emp.get("excluded_reason") == "user_excluded"


def test_schema_shortlist_exclude_does_not_backfill(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    # Deterministic scores per schema text. cap=2 → auto {A,B}. Excluding A must leave
    # only {B} in scope — NOT promote the over-cap C (no "must be 2 schemas" quota).
    scores = {"ta": 90, "tb": 80, "tc": 70}
    monkeypatch.setattr(feas.schema_dna, "text_similarity", lambda a, b, cache=None: scores.get(b, 0))
    by_key = {("db", "A"): [], ("db", "B"): [], ("db", "C"): []}
    texts = {("db", "A"): "ta", ("db", "B"): "tb", ("db", "C"): "tc"}
    sl = feas._schema_shortlist(_emp_spec(), by_key, {}, floor=0, cap=2,
                                schema_texts=texts, exclude_override={("db", "A")})
    inc = {(r["database"], r["schema"]): r["included"] for r in sl}
    assert inc == {("db", "A"): False, ("db", "B"): True, ("db", "C"): False}
    c_row = next(r for r in sl if r["schema"] == "C")
    assert c_row["excluded_reason"] == "over_cap"  # C stayed out — no backfill
    a_row = next(r for r in sl if r["schema"] == "A")
    assert a_row["excluded_reason"] == "user_excluded"


def test_schema_shortlist_threshold_and_fallback(monkeypatch):
    monkeypatch.setattr(feas.schema_dna, "available_embeddings", lambda: False)
    spec = _emp_spec()

    def _shortlist(scores, *, floor, threshold, cap):
        # Deterministic score per schema, keyed by a per-schema text token.
        text_of = {name: f"t{name.lower()}" for name in scores}
        by_score = {text_of[name]: sc for name, sc in scores.items()}
        monkeypatch.setattr(feas.schema_dna, "text_similarity",
                            lambda a, b, cache=None: by_score.get(b, 0))
        by_key = {("db", name): [] for name in scores}
        texts = {("db", name): text_of[name] for name in scores}
        sl = feas._schema_shortlist(spec, by_key, {}, floor=floor, cap=cap,
                                    threshold=threshold, schema_texts=texts)
        return {r["schema"]: r for r in sl}, sl

    # Case 1: two schemas clear the threshold → both in; the sub-threshold 60 is
    # excluded as below_threshold (NOT below_floor, since it clears the floor).
    rows, sl = _shortlist({"A": 90, "B": 80, "C": 60}, floor=45, threshold=70, cap=5)
    assert rows["A"]["included"] and rows["B"]["included"] and not rows["C"]["included"]
    assert rows["C"]["excluded_reason"] == "below_threshold"
    assert not rows["A"].get("fallback") and not rows["B"].get("fallback")

    # Case 2: NONE clears the threshold but both clear the floor → best-one fallback:
    # only the top floor-passer is evaluated (low confidence), scope is NOT empty.
    rows, sl = _shortlist({"A": 65, "B": 55}, floor=45, threshold=70, cap=5)
    assert rows["A"]["included"] and rows["A"]["fallback"] is True
    assert not rows["B"]["included"]
    assert rows["B"]["excluded_reason"] == "below_threshold"
    ev = feas.build_spec_evidence(spec, [], [], sl)
    assert ev["schema_scope_empty"] is False

    # Case 3: nothing clears the floor → empty scope, no fallback, absent stands.
    rows, sl = _shortlist({"A": 40}, floor=45, threshold=70, cap=5)
    assert not rows["A"]["included"]
    assert rows["A"]["excluded_reason"] == "below_floor"
    ev = feas.build_spec_evidence(spec, [], [], sl)
    assert ev["schema_scope_empty"] is True
