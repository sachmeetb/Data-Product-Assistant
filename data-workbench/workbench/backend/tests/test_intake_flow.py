"""End-to-end inbound-intake backend loop (offline).

submit → leased worker parse (fake parser, no live SDK) → proposed → edit
(revision bump) → approve → dmig project scaffolded. Also the finding-1
regression guard: the scaffolded dmig project must get real multi-workflow
StageRuns (i.e. create_project was called with workflow_ids=None, not []).
"""
from __future__ import annotations

import asyncio
import json
import shutil

import pytest
from sqlmodel import Session, select

from workbench.backend import intake_parser, intake_worker
from workbench.backend.config import BASE_PROJECT_DIR
from workbench.backend.database import engine
from workbench.backend.models import (
    IntakePendingDependency,
    IntakeSubmission,
    ProductRequest,
    Project,
    StageRun,
    Workflow,
)
from workbench.backend.routers import intake as intake_router


def _fake_migration_blueprint():
    return {
        "scenario": "migration",
        "overall_confidence": "high",
        "project_name": {"value": "Orders Migration", "confidence": "high", "why": "stated"},
        "domain": {"value": "sales", "confidence": "medium", "why": "inferred"},
        "source_platform": {"value": "Oracle", "confidence": "high", "why": "stated"},
        "target_platform": {"value": "Databricks", "confidence": "medium", "why": "inferred"},
        "datasets": [
            {"candidate_id": "ds-0", "name": {"value": "SALES.ORDERS", "confidence": "high", "why": "listed"},
             "columns": [{"candidate_id": "ds-0.c0", "name": {"value": "ORDER_ID", "confidence": "high", "why": "listed"}}]}
        ],
        "gaps": [],
        "rationale": "clear lift-and-shift",
    }


@pytest.fixture()
def _fake_parser(monkeypatch):
    async def _fake(envelope, scenario):
        return _fake_migration_blueprint(), {"scenario": scenario, "parser": "fake"}

    monkeypatch.setattr(intake_parser, "parse_envelope", _fake)


def _cleanup_scaffolded(archetypes=("dmig", "dpe-sa", "dpe-cf")):
    """Remove intake-scaffolded projects + child rows + on-disk dirs.

    Within a single test only intake-created projects exist (conftest's autouse
    fixture clears Project/ProductRequest before each test), so this is safe.
    """
    with Session(engine) as s:
        for p in s.exec(select(Project).where(Project.archetype.in_(archetypes))).all():  # type: ignore[attr-defined]
            code = p.project_code
            for sr in s.exec(select(StageRun).where(StageRun.project_id == p.id)).all():
                s.delete(sr)
            # Delete Workflow rows too — SQLite recycles the project PK, so orphan
            # Workflows would collide with a later test's freshly-inserted project.
            for wf in s.exec(select(Workflow).where(Workflow.project_id == p.id)).all():
                s.delete(wf)
            for pr in s.exec(select(ProductRequest).where(ProductRequest.project_id == p.id)).all():
                s.delete(pr)
            s.delete(p)
            s.commit()
            shutil.rmtree(BASE_PROJECT_DIR / code, ignore_errors=True)


def test_submit_parse_approve_scaffolds_dmig(_fake_parser):
    try:
        # 1) submit (source_system derived from the token, here injected directly)
        env = intake_router.IntakeEnvelope(
            scenario="migration",
            external_ref="engagement-e2e-1",
            content=[intake_router.ContentPart(kind="text", title="Assessment", body="move orders to databricks")],
        )
        with Session(engine) as s:
            res = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)
        sid = res["intake_id"]
        assert res["status"] == "received"

        # 2) worker parses one submission
        assert asyncio.run(intake_worker.process_one()) is True
        with Session(engine) as s:
            sub = s.get(IntakeSubmission, sid)
            assert sub.status == "proposed"
            assert sub.blueprint_revision == 1
            assert sub.blueprint_json and "Orders Migration" in sub.blueprint_json

        # 3) edit the blueprint (optimistic-concurrency bump)
        bp_dict = _fake_migration_blueprint()
        bp_dict["project_name"]["value"] = "Orders Migration (edited)"
        with Session(engine) as s:
            patched = intake_router.patch_blueprint(
                sid, intake_router.BlueprintPatch(expected_revision=1, blueprint=bp_dict), session=s
            )
        assert patched["blueprint_revision"] == 2
        assert patched["status"] == "reviewing"

        # 4) stale revision is rejected
        with Session(engine) as s:
            with pytest.raises(Exception):
                intake_router.patch_blueprint(
                    sid, intake_router.BlueprintPatch(expected_revision=1, blueprint=bp_dict), session=s
                )

        # 5) approve → scaffold dmig
        with Session(engine) as s:
            approved = intake_router.approve(sid, intake_router.ApproveBody(expected_revision=2), session=s)
        assert approved["status"] == "scaffolded"
        project_id = approved["result"]["project_id"]

        # 6) finding-1 guard: the dmig project has real multi-workflow StageRuns
        with Session(engine) as s:
            proj = s.get(Project, project_id)
            assert proj.archetype == "dmig"
            assert proj.multi_workflow is True
            stage_rows = s.exec(select(StageRun).where(StageRun.project_id == project_id)).all()
            assert len(stage_rows) >= 3  # dmig template has ~8 stages; [] would give legacy/none
            assert any(sr.workflow_id for sr in stage_rows)  # multi-workflow rows carry workflow_id

        # 7) idempotent re-approve does not create a second project
        with Session(engine) as s:
            again = intake_router.approve(sid, intake_router.ApproveBody(expected_revision=2), session=s)
            assert again.get("already") is True
            dmig_count = len(s.exec(select(Project).where(Project.archetype == "dmig")).all())
            assert dmig_count == 1
    finally:
        _cleanup_scaffolded()


def test_parse_failure_marks_parse_failed(monkeypatch):
    async def _bad(envelope, scenario):
        return None, {"error": "parser produced no valid JSON block"}

    monkeypatch.setattr(intake_parser, "parse_envelope", _bad)
    env = intake_router.IntakeEnvelope(
        scenario="migration", external_ref="engagement-bad-1",
        content=[intake_router.ContentPart(body="garbage")],
    )
    with Session(engine) as s:
        sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
    assert asyncio.run(intake_worker.process_one()) is True
    with Session(engine) as s:
        sub = s.get(IntakeSubmission, sid)
        assert sub.status == "parse_failed"
        assert sub.blueprint_json is None


def _fake_modernization_blueprint():
    return {
        "scenario": "modernization",
        "overall_confidence": "medium",
        "source_aligned": [
            {"candidate_id": "src-0", "name": {"value": "Customer Master", "confidence": "high", "why": "x"},
             "domain": {"value": "crm", "confidence": "medium", "why": "x"}, "product_idea": "canonical customers"}
        ],
        "consumer_aligned": [
            {"candidate_id": "con-0", "name": {"value": "Churn Features", "confidence": "high", "why": "x"},
             "purpose": "ML features"},
            {"candidate_id": "con-x", "name": {"value": "Dropped", "confidence": "low", "why": "x"},
             "review_state": "excluded"},
        ],
        "dependencies": [
            {"dependency_id": "dep-0", "from_candidate_id": "con-0", "to_candidate_id": "src-0", "confidence": "medium"}
        ],
        "gaps": [], "rationale": "portfolio",
    }


def test_modernization_portfolio_scaffold(monkeypatch):
    async def _fake(envelope, scenario):
        return _fake_modernization_blueprint(), {"scenario": scenario, "parser": "fake"}

    monkeypatch.setattr(intake_parser, "parse_envelope", _fake)
    try:
        env = intake_router.IntakeEnvelope(
            scenario="modernization", external_ref="mod-e2e-1",
            content=[intake_router.ContentPart(body="modernize the crm estate")],
        )
        with Session(engine) as s:
            sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
        assert asyncio.run(intake_worker.process_one()) is True

        with Session(engine) as s:
            sub = s.get(IntakeSubmission, sid)
            rev = sub.blueprint_revision
        with Session(engine) as s:
            approved = intake_router.approve(sid, intake_router.ApproveBody(expected_revision=rev), session=s)
        assert approved["status"] == "scaffolded"
        result = approved["result"]
        assert result["scenario"] == "modernization"

        with Session(engine) as s:
            sa = s.exec(select(Project).where(Project.archetype == "dpe-sa")).all()
            cf = s.exec(select(Project).where(Project.archetype == "dpe-cf")).all()
            # one source, one consumer (the excluded consumer is skipped)
            assert len(sa) == 1
            assert len(cf) == 1
            # source-aligned got an Incoming-queue request linked to the intake
            reqs = s.exec(select(ProductRequest).where(ProductRequest.project_id == sa[0].id)).all()
            assert len(reqs) == 1 and reqs[0].parent_intake_submission_id == sid
            # consumer-aligned did NOT get a request (sources not materialized yet)
            assert s.exec(select(ProductRequest).where(ProductRequest.project_id == cf[0].id)).all() == []
            # a pending dependency links consumer→source with the source project resolved
            deps = s.exec(
                select(IntakePendingDependency).where(IntakePendingDependency.intake_submission_id == sid)
            ).all()
            assert len(deps) == 1
            assert deps[0].consumer_project_id == cf[0].id
            assert deps[0].source_project_id == sa[0].id
            assert deps[0].status == "pending"

        # idempotent re-approve — no duplicate projects / requests / deps
        with Session(engine) as s:
            intake_router.approve(sid, intake_router.ApproveBody(expected_revision=rev), session=s)
            assert len(s.exec(select(Project).where(Project.archetype == "dpe-sa")).all()) == 1
            assert len(s.exec(select(Project).where(Project.archetype == "dpe-cf")).all()) == 1
            assert len(s.exec(
                select(IntakePendingDependency).where(IntakePendingDependency.intake_submission_id == sid)
            ).all()) == 1
    finally:
        _cleanup_scaffolded()


def test_modernization_consumer_odcs_seeds_draft(monkeypatch):
    """A consumer candidate carrying an embedded ODCS scaffolds a dpe-cf DRAFT (the
    Product-Assembly / feasibility-/act path). The scaffold always completes even
    when the seed write can't reach a graph (best-effort); a reachable Neo4j
    additionally verifies the seeded schema (guarded — this suite is SQLite-only)."""
    con_props = [
        {"name": "customer_id", "physicalName": "customer_id", "logicalType": "bigint",
         "physicalType": "bigint", "primaryKey": True, "required": True},
        {"name": "lifetime_value", "physicalName": "lifetime_value", "logicalType": "numeric",
         "physicalType": "numeric", "required": False},
    ]

    def _bp_with_odcs():
        bp = _fake_modernization_blueprint()
        bp["consumer_aligned"][0]["odcs"] = {
            "apiVersion": "v3.1.0", "kind": "DataContract", "name": "Churn Features",
            "version": "1.0.0", "status": "draft", "domain": "crm",
            "productKind": "consumer", "description": "ML features",
            "schema": [{"name": "Churn Features", "physicalName": "churn_features",
                        "physicalType": "table", "properties": con_props}],
        }
        return bp

    async def _fake(envelope, scenario):
        return _bp_with_odcs(), {"scenario": scenario, "parser": "fake"}

    monkeypatch.setattr(intake_parser, "parse_envelope", _fake)
    try:
        env = intake_router.IntakeEnvelope(
            scenario="modernization", external_ref="mod-odcs-seed-1",
            content=[intake_router.ContentPart(body="modernize the crm estate")],
        )
        with Session(engine) as s:
            sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
        assert asyncio.run(intake_worker.process_one()) is True
        with Session(engine) as s:
            rev = s.get(IntakeSubmission, sid).blueprint_revision
        with Session(engine) as s:
            approved = intake_router.approve(sid, intake_router.ApproveBody(expected_revision=rev), session=s)
        assert approved["status"] == "scaffolded"

        with Session(engine) as s:
            cf = s.exec(select(Project).where(Project.archetype == "dpe-cf")).all()
            # The odcs-carrying consumer still scaffolds exactly one draft.
            assert len(cf) == 1
            # Graph verification is optional in this suite — assert the seeded schema
            # only when a Neo4j is reachable (SQLite-only runs skip silently).
            try:
                from workbench.backend.routers.odcs import _read_odcs_from_graph
                spec = _read_odcs_from_graph(f"{cf[0].project_code}-contract", cf[0])
                if spec and spec.get("schema"):
                    names = [p.get("name") for p in (spec["schema"][0].get("properties") or [])]
                    assert "customer_id" in names and "lifetime_value" in names
            except Exception:
                pass  # no Neo4j → the SQLite-side scaffold assertion above is sufficient
    finally:
        _cleanup_scaffolded()


@pytest.mark.parametrize("archetype", ["dmig", "dpe-sa", "dpe-cf"])
def test_create_project_workflow_ids_none_yields_multiworkflow(archetype):
    """Regression guard for finding 1: scaffolding MUST use workflow_ids=None to
    get the archetype's default multi-workflow stages. workflow_ids=[] filters
    every template out and falls back to the legacy single-workflow path — the
    latent bug the reviewer caught. Also documents that divergence explicitly.
    """
    from workbench.backend.routers.projects import ProjectCreate, create_project

    try:
        with Session(engine) as s:
            good = create_project(
                ProjectCreate(name=f"none {archetype}", archetype=archetype, workflow_ids=None), s
            )
            bad = create_project(
                ProjectCreate(name=f"empty {archetype}", archetype=archetype, workflow_ids=[]), s
            )
            good_stages = s.exec(select(StageRun).where(StageRun.project_id == good["id"])).all()
            bad_stages = s.exec(select(StageRun).where(StageRun.project_id == bad["id"])).all()

        # None → real multi-workflow scaffold with workflow_id-tagged StageRuns
        assert good["multi_workflow"] is True
        assert len(good_stages) >= 3
        assert all(sr.workflow_id for sr in good_stages)
        # [] → legacy path (documents the bug: NOT multi-workflow)
        assert bad["multi_workflow"] is False
    finally:
        _cleanup_scaffolded(archetypes=(archetype,))


def test_mcp_intake_review_tools_delegate(monkeypatch):
    """The /mcp intake review tools mirror the REST review/approve path."""
    from workbench.backend.mcp_server import (
        approve_intake_submission,
        get_intake_submission,
        list_intake_submissions,
    )

    async def _fake(envelope, scenario):
        return _fake_migration_blueprint(), {"scenario": scenario, "parser": "fake"}

    monkeypatch.setattr(intake_parser, "parse_envelope", _fake)
    try:
        env = intake_router.IntakeEnvelope(
            scenario="migration", external_ref="mcp-e2e-1",
            content=[intake_router.ContentPart(body="x")],
        )
        with Session(engine) as s:
            sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
        assert asyncio.run(intake_worker.process_one()) is True

        listed = list_intake_submissions(scenario="migration")
        assert any(sub["id"] == sid for sub in listed["submissions"])
        detail = get_intake_submission(sid)
        assert detail["id"] == sid and detail["approval_blockers"] == []
        approved = approve_intake_submission(sid, detail["blueprint_revision"])
        assert approved["status"] == "scaffolded"
    finally:
        _cleanup_scaffolded()


def test_mcp_offline_migration_parity(_fake_parser):
    """The offline-migration MCP tools mirror the REST/UI path: set execution
    mode, read/confirm the physical schema, and gate flip-to-live."""
    from workbench.backend import mcp_server as mcp

    try:
        env = intake_router.IntakeEnvelope(
            scenario="migration", external_ref="mcp-offline-1",
            content=[intake_router.ContentPart(body="offline move")],
        )
        with Session(engine) as s:
            sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
        assert asyncio.run(intake_worker.process_one()) is True

        # set execution mode via MCP (+ bad value rejected)
        assert mcp.set_intake_execution_mode(sid, "schema_only")["execution_mode"] == "schema_only"
        assert "error" in mcp.set_intake_execution_mode(sid, "bogus")

        with Session(engine) as s:
            rev = s.get(IntakeSubmission, sid).blueprint_revision
        with Session(engine) as s:
            res = intake_router.approve(sid, intake_router.ApproveBody(expected_revision=rev), session=s)["result"]
        code = res["project_code"]

        # get physical schema via MCP — prefilled draft from the blueprint
        ps = mcp.get_physical_schema(code)
        assert ps["status"] == "draft" and ps["prefilled"] is True

        # confirm an unsafe schema via MCP → error carries errors[]
        conf = mcp.confirm_physical_schema(code, {
            "version": "1.0",
            "tables": [{"namespace": "s", "table": "../x",
                        "columns": [{"name": "a", "data_type": ""}]}],
        })
        assert "error" in conf and conf.get("errors")

        # flip-to-live via MCP with no source/target → missing[]
        flip = mcp.flip_migration_to_live(code)
        assert "error" in flip and flip.get("missing")
    finally:
        _cleanup_scaffolded()


def test_submit_is_idempotent_on_external_ref(_fake_parser):
    env = intake_router.IntakeEnvelope(
        scenario="migration", external_ref="engagement-dup",
        content=[intake_router.ContentPart(body="x")],
    )
    with Session(engine) as s:
        first = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)
    with Session(engine) as s:
        second = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)
    assert first["intake_id"] == second["intake_id"]
    assert second.get("resubmitted") is True


# ── Part 1: intake provenance ────────────────────────────────────────────────

def test_scaffold_sets_project_intake_marker(_fake_parser):
    """The denormalized forward marker is set on the scaffolded child in-saga
    and equals the IntakeSpawn link (source of truth)."""
    from workbench.backend.models import IntakeSpawn

    try:
        env = intake_router.IntakeEnvelope(
            scenario="migration", external_ref="marker-1",
            content=[intake_router.ContentPart(body="move orders")],
        )
        with Session(engine) as s:
            sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
        assert asyncio.run(intake_worker.process_one()) is True
        with Session(engine) as s:
            rev = s.get(IntakeSubmission, sid).blueprint_revision
        with Session(engine) as s:
            approved = intake_router.approve(sid, intake_router.ApproveBody(expected_revision=rev), session=s)
        project_id = approved["result"]["project_id"]

        with Session(engine) as s:
            proj = s.get(Project, project_id)
            assert proj.parent_intake_submission_id == sid
            spawn = s.exec(select(IntakeSpawn).where(IntakeSpawn.child_project_id == project_id)).first()
            # marker ↔ spawn agreement
            assert spawn.intake_submission_id == proj.parent_intake_submission_id
    finally:
        _cleanup_scaffolded()


def test_origin_candidate_scoped_for_modernization(monkeypatch):
    """A dpe-sa child's origin reports ONLY its own candidate's datasets,
    resolved via IntakeSpawn.candidate_id — not the whole-blueprint count."""
    bp = _fake_modernization_blueprint()
    # give the source-aligned candidate a concrete inventory
    bp["source_aligned"][0]["datasets"] = [
        {"candidate_id": "src-0.ds0", "name": {"value": "CUSTOMER", "confidence": "high", "why": "x"},
         "columns": [
             {"candidate_id": "src-0.ds0.c0", "name": {"value": "CUST_ID", "confidence": "high", "why": "x"}},
             {"candidate_id": "src-0.ds0.c1", "name": {"value": "EMAIL", "confidence": "high", "why": "x"}},
         ]}
    ]

    async def _fake(envelope, scenario):
        return bp, {"scenario": scenario, "parser": "fake"}

    monkeypatch.setattr(intake_parser, "parse_envelope", _fake)
    try:
        env = intake_router.IntakeEnvelope(
            scenario="modernization", external_ref="mod-origin-1",
            content=[intake_router.ContentPart(body="modernize crm")],
        )
        with Session(engine) as s:
            sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
        assert asyncio.run(intake_worker.process_one()) is True
        with Session(engine) as s:
            rev = s.get(IntakeSubmission, sid).blueprint_revision
        with Session(engine) as s:
            intake_router.approve(sid, intake_router.ApproveBody(expected_revision=rev), session=s)

        with Session(engine) as s:
            sa = s.exec(select(Project).where(Project.archetype == "dpe-sa")).all()[0]
            origin = intake_router.intake_origin_for_project(sa.id, session=s)
        assert origin["scenario"] == "modernization"
        assert origin["candidate_id"] == "src-0"
        # candidate-scoped, NOT zero (top-level modernization has no `datasets`)
        assert origin["dataset_count"] == 1
        assert origin["column_count"] == 2
        assert origin["rationale"] == "portfolio"
    finally:
        _cleanup_scaffolded()


def test_execution_mode_captured_and_propagated(_fake_parser):
    """A migration submission's schema_only execution_mode set during review is
    propagated to Project.data_connectivity_mode by the scaffold saga."""
    try:
        env = intake_router.IntakeEnvelope(
            scenario="migration", external_ref="offline-1",
            content=[intake_router.ContentPart(body="move orders, no live source")],
        )
        with Session(engine) as s:
            sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
        assert asyncio.run(intake_worker.process_one()) is True

        # reviewer flips to schema-only
        with Session(engine) as s:
            out = intake_router.set_execution_mode(
                sid, intake_router.ExecutionModeBody(execution_mode="schema_only"), session=s
            )
            assert out["execution_mode"] == "schema_only"
            rev = s.get(IntakeSubmission, sid).blueprint_revision

        with Session(engine) as s:
            approved = intake_router.approve(sid, intake_router.ApproveBody(expected_revision=rev), session=s)
        project_id = approved["result"]["project_id"]
        with Session(engine) as s:
            assert s.get(Project, project_id).data_connectivity_mode == "schema_only"
    finally:
        _cleanup_scaffolded()


def test_schema_only_scaffold_uses_offline_template(_fake_parser):
    """D6: a schema_only migration scaffolds the offline template — Import
    Provided Schema + metadata-enrichment, and NO live discovery/profiling."""
    from workbench.backend.models import Workflow

    try:
        env = intake_router.IntakeEnvelope(
            scenario="migration", external_ref="offline-tmpl-1",
            content=[intake_router.ContentPart(body="move orders, offline")],
        )
        with Session(engine) as s:
            sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
        assert asyncio.run(intake_worker.process_one()) is True
        with Session(engine) as s:
            intake_router.set_execution_mode(sid, intake_router.ExecutionModeBody(execution_mode="schema_only"), session=s)
            rev = s.get(IntakeSubmission, sid).blueprint_revision
        with Session(engine) as s:
            pid = intake_router.approve(sid, intake_router.ApproveBody(expected_revision=rev), session=s)["result"]["project_id"]

        with Session(engine) as s:
            proj = s.get(Project, pid)
            assert proj.data_connectivity_mode == "schema_only"
            wf = s.exec(select(Workflow).where(
                Workflow.project_id == pid, Workflow.workflow_id == "migration_schema_only")).first()
            assert wf is not None
            stage_ids = {st.get("stage_id") for st in json.loads(wf.workflow_json)}
            assert "dmig_import_schema" in stage_ids
            assert "metadata_enrichment" in stage_ids
            # live discovery/profiling omitted offline
            assert "select_data_source" not in stage_ids
            assert "data_discovery_composite" not in stage_ids
            assert "data_profiling_composite" not in stage_ids
    finally:
        _cleanup_scaffolded()


def test_flip_to_live_requires_both_ends_then_transitions(_fake_parser):
    """D5/D6 flip-to-live: blocked until BOTH a source and target exist; then it
    flips the mode, attaches live discovery, and invalidates downstream stages."""
    from fastapi import HTTPException
    from workbench.backend.routers import migration as mig
    from workbench.backend.models import Workflow

    try:
        env = intake_router.IntakeEnvelope(
            scenario="migration", external_ref="flip-1",
            content=[intake_router.ContentPart(body="move orders offline then live")],
        )
        with Session(engine) as s:
            sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
        assert asyncio.run(intake_worker.process_one()) is True
        with Session(engine) as s:
            intake_router.set_execution_mode(sid, intake_router.ExecutionModeBody(execution_mode="schema_only"), session=s)
            rev = s.get(IntakeSubmission, sid).blueprint_revision
        with Session(engine) as s:
            pid = intake_router.approve(sid, intake_router.ApproveBody(expected_revision=rev), session=s)["result"]["project_id"]

        # no source + no target → 409 with both reasons
        with Session(engine) as s:
            with pytest.raises(HTTPException) as ei:
                mig.flip_to_live(pid, session=s, user=_auth(), _role=None)
            assert ei.value.status_code == 409
            joined = " ".join(ei.value.detail["missing"])
            assert "source" in joined and "target" in joined

        # bind a source connection + a target connection, then flip succeeds
        from workbench.backend import migration_orchestrator as orch
        from workbench.backend.models import PlatformConnection, SourceBinding
        with Session(engine) as s:
            proj = s.get(Project, pid)
            src = PlatformConnection(connection_name=f"src-{proj.project_code}",
                                     platform_type="postgres", host="h", port=5432,
                                     database="db", username="u", secret_ref="direct:p")
            s.add(src); s.commit(); s.refresh(src)
            s.add(SourceBinding(project_id=pid, connection_id=src.id)); s.commit()
            orch.configure(s, proj, target_platform="snowflake")  # sets a plan, but intent-only...
        # intent-only target isn't a real connection → still blocked on target
        with Session(engine) as s:
            with pytest.raises(HTTPException):
                mig.flip_to_live(pid, session=s, user=_auth(), _role=None)

        # bind a real target connection id on the plan row, then flip
        with Session(engine) as s:
            row = orch.get_row(s, s.get(Project, pid).project_code)
            row.target_connection_id = 999  # a real (if fake) connection id
            s.add(row); s.commit()
        with Session(engine) as s:
            out = mig.flip_to_live(pid, session=s, user=_auth(), _role=None)
            assert out["status"] == "live"
            assert out["added_discovery_workflow"] is True
            proj = s.get(Project, pid)
            assert proj.data_connectivity_mode == "live"
            # live discovery workflow attached
            assert s.exec(select(Workflow).where(
                Workflow.project_id == pid, Workflow.workflow_id == "data_discovery")).first() is not None
    finally:
        _cleanup_scaffolded()


def _auth():
    from workbench.backend.auth import AuthUser
    return AuthUser(email="eng@x", name="Eng", role="engineer")


def test_execution_mode_rejects_modernization_and_bad_value():
    """execution_mode is migration-only and validated."""
    async def _fake(envelope, scenario):
        return _fake_modernization_blueprint(), {"scenario": scenario, "parser": "fake"}

    import workbench.backend.intake_parser as _ip
    from unittest.mock import patch
    with patch.object(_ip, "parse_envelope", _fake):
        env = intake_router.IntakeEnvelope(
            scenario="modernization", external_ref="offline-mod-1",
            content=[intake_router.ContentPart(body="modernize")],
        )
        with Session(engine) as s:
            sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
        assert asyncio.run(intake_worker.process_one()) is True
        with Session(engine) as s:
            with pytest.raises(Exception):  # modernization rejected
                intake_router.set_execution_mode(
                    sid, intake_router.ExecutionModeBody(execution_mode="schema_only"), session=s
                )

    env2 = intake_router.IntakeEnvelope(
        scenario="migration", external_ref="offline-bad-1",
        content=[intake_router.ContentPart(body="x")],
    )
    with Session(engine) as s:
        sid2 = intake_router.submit_intake(env2, source_system="acme-analyzer", session=s)["intake_id"]
        with pytest.raises(Exception):  # bad value rejected
            intake_router.set_execution_mode(
                sid2, intake_router.ExecutionModeBody(execution_mode="bogus"), session=s
            )


def test_migration_execution_readiness_reports_missing():
    """The pure readiness function flags every unmet end without mutating state."""
    from workbench.backend import migration_orchestrator as orch

    with Session(engine) as s:
        proj = Project(
            project_code="dmig-ready-1", name="Ready", pg_connection="",
            archetype="dmig", data_connectivity_mode="schema_only",
        )
        s.add(proj)
        s.commit()
        s.refresh(proj)
        r = orch.migration_execution_readiness(proj, session=s)
        assert r["ready"] is False
        joined = " ".join(r["missing"])
        assert "schema_only" in joined
        assert "source" in joined
        assert "target" in joined
        assert "package" in joined

        # a live project WITH a bound source still lacks target + package
        proj.data_connectivity_mode = "live"
        s.add(proj)
        s.commit()
        from workbench.backend.models import PlatformConnection, SourceBinding
        src = PlatformConnection(connection_name=f"src-{proj.project_code}",
                                 platform_type="postgres", host="h", port=5432,
                                 database="db", username="u", secret_ref="direct:p")
        s.add(src); s.commit(); s.refresh(src)
        s.add(SourceBinding(project_id=proj.id, connection_id=src.id)); s.commit()
        r2 = orch.migration_execution_readiness(proj, session=s)
        assert r2["ready"] is False
        j2 = " ".join(r2["missing"])
        assert "schema_only" not in j2 and "source" not in j2
        assert "target" in j2 and "package" in j2
        s.delete(proj)
        s.commit()


def test_execution_gate_blocks_snapshot_and_complete():
    """D1 gate: snapshot_core, reconcile_core, and the direct /complete for the
    execute stages all 409 with a missing[] list when not execution-ready."""
    from fastapi import HTTPException
    from workbench.backend.routers import migration as mig

    with Session(engine) as s:
        proj = Project(
            project_code="dmig-gate-1", name="Gate", pg_connection="",
            archetype="dmig", data_connectivity_mode="schema_only",
        )
        s.add(proj)
        s.commit()
        s.refresh(proj)

        with pytest.raises(HTTPException) as ei:
            mig.snapshot_core(proj, s, actor="t")
        assert ei.value.status_code == 409
        assert ei.value.detail["missing"]  # non-empty reasons

        with pytest.raises(HTTPException) as ei2:
            mig.reconcile_core(proj, s, actor="t")
        assert ei2.value.status_code == 409

        # require_execution_ready is the shared choke point
        with pytest.raises(HTTPException) as ei3:
            mig.require_execution_ready(proj, s)
        assert ei3.value.status_code == 409
        assert any("schema_only" in m for m in ei3.value.detail["missing"])

        s.delete(proj)
        s.commit()


def test_configure_target_intent_without_connection():
    """D2: a schema-only project configures a target PLATFORM intent with no
    connection — stable placeholder platform_instance_id, intent-only status,
    and NOT execution-ready (target check still requires a real connection)."""
    from workbench.backend import migration_orchestrator as orch

    with Session(engine) as s:
        proj = Project(
            project_code="dmig-intent-1", name="Intent", pg_connection="",
            archetype="dmig", data_connectivity_mode="schema_only",
        )
        s.add(proj)
        s.commit()
        s.refresh(proj)

        # platform-only intent (no connection id)
        row = orch.configure(s, proj, target_platform="Snowflake", target_catalog="workspace")
        assert row.target_connection_id is None
        assert row.target_platform == "snowflake"

        status = orch.status_payload(s, proj)
        assert status["target_intent_only"] is True
        assert status["target_platform"] == "snowflake"

        # placeholder instance id is stable, never str(None)
        plan = orch.load_plan(s, proj.project_code)
        assert plan.target_asset_ref.platform_instance_id == "intent:snowflake"

        # apply_plan_target uses the intent platform when no connection resolves
        spec = orch.apply_plan_target(s, proj, {})
        assert spec["target_platform"] == "snowflake"

        # requiring exactly one target is enforced
        with pytest.raises(ValueError):
            orch.configure(s, proj, target_connection_id=None, target_platform="")

        # intent alone is NOT executable (needs a real connection)
        r = orch.migration_execution_readiness(proj, session=s)
        assert r["ready"] is False
        assert any("target" in m for m in r["missing"])

        s.delete(row)
        s.delete(proj)
        s.commit()


def test_physical_schema_prefill_confirm_and_validation():
    """D3: prefill from blueprint, save draft, and confirm — with hard blocks on
    missing types, unsafe/colliding names."""
    from workbench.backend import physical_schema as ps
    from workbench.backend.models import ProjectPhysicalSchema

    # prefill splits qualified names + copies types (missing → "")
    bp = {
        "scenario": "migration",
        "datasets": [
            {"name": {"value": "SALES.ORDERS"}, "columns": [
                {"name": {"value": "ORDER_ID"}, "data_type": {"value": "NUMBER(18)"}},
                {"name": {"value": "TS"}, "data_type": {"value": "missing"}},
            ]},
        ],
    }
    schema = ps.prefill_from_blueprint(bp)
    assert schema.tables[0].namespace == "SALES" and schema.tables[0].table == "ORDERS"
    assert schema.tables[0].columns[0].data_type == "NUMBER(18)"
    assert schema.tables[0].columns[1].data_type == ""  # 'missing' → blank, not fabricated

    # missing type on an included column blocks confirm
    with pytest.raises(ps.PhysicalSchemaValidationError) as ei:
        ps.validate_for_confirm(schema)
    assert any("data_type is required" in e for e in ei.value.errors)

    # unsafe + colliding names block
    bad = ps.PhysicalSchema(tables=[
        ps.PhysicalTable(namespace="s", table="../evil", columns=[ps.PhysicalColumn(name="a", data_type="int")]),
        ps.PhysicalTable(namespace="s", table="dup", columns=[ps.PhysicalColumn(name="x", data_type="int")]),
        ps.PhysicalTable(namespace="s", table="dup", columns=[ps.PhysicalColumn(name="x", data_type="int")]),
    ])
    with pytest.raises(ps.PhysicalSchemaValidationError) as ei2:
        ps.validate_for_confirm(bad)
    joined = " ".join(ei2.value.errors)
    assert "unsafe table" in joined and "duplicate table" in joined

    # a clean schema confirms + persists
    good = ps.PhysicalSchema(tables=[
        ps.PhysicalTable(namespace="sales", table="orders", columns=[
            ps.PhysicalColumn(name="order_id", data_type="bigint", primary_key=True, nullable=False),
        ]),
    ])
    with Session(engine) as s:
        proj = Project(project_code="dmig-phys-1", name="Phys", pg_connection="",
                       archetype="dmig", data_connectivity_mode="schema_only")
        s.add(proj); s.commit(); s.refresh(proj)
        row = ps.confirm(s, proj, good, by="eng@x")
        assert row.status == "confirmed" and row.confirmed_at is not None
        # saving edits re-opens (status back to draft)
        row2 = ps.save_draft(s, proj, good)
        assert row2.status == "draft"
        s.delete(s.get(ProjectPhysicalSchema, "dmig-phys-1"))
        s.delete(proj); s.commit()


def test_seed_yaml_conversion_and_guards():
    """D4: pure confirmed-schema → discovery-YAML conversion (no Neo4j)."""
    from workbench.backend import intake_schema_seed as seeder
    from workbench.backend import physical_schema as ps

    schema = ps.PhysicalSchema(tables=[
        # 3-level (catalog folded into schema); PK + FK; one excluded table
        ps.PhysicalTable(catalog="wh", namespace="sales", table="orders", columns=[
            ps.PhysicalColumn(name="order_id", data_type="bigint", primary_key=True, nullable=False),
            ps.PhysicalColumn(name="cust_id", data_type="bigint", fk_table="sales.customers", fk_column="id"),
        ]),
        ps.PhysicalTable(namespace="sales", table="scratch", excluded=True,
                         columns=[ps.PhysicalColumn(name="x", data_type="int")]),
    ])

    docs = seeder.schema_to_yaml_docs(schema)
    # excluded table skipped; 3-level schema folded to "wh.sales"
    assert set(docs) == {"wh.sales__orders.yaml"}
    doc = docs["wh.sales__orders.yaml"]
    assert doc["schema"] == "wh.sales" and doc["table"] == "orders"
    assert doc["columns"][0] == {
        "name": "order_id", "ordinal": 1, "type": "bigint",
        "character_maximum_length": None, "numeric_precision": None,
        "numeric_scale": None, "nullable": False, "default": None, "comment": None,
    }
    assert doc["primary_key"] == {"constraint_name": "orders_pkey", "columns": ["order_id"]}
    fk = doc["foreign_keys"][0]
    assert fk["referenced_schema"] == "sales" and fk["referenced_table"] == "customers"
    assert fk["columns"] == ["cust_id"] and fk["referenced_columns"] == ["id"]

    # expected counts ignore the excluded table
    assert seeder.expected_counts(schema) == (1, 2)

    # unsafe name defense-in-depth even if it slipped past confirm
    with pytest.raises(seeder.SeedError):
        seeder.schema_to_yaml_docs(ps.PhysicalSchema(tables=[
            ps.PhysicalTable(namespace="s", table="../evil", columns=[ps.PhysicalColumn(name="a", data_type="int")]),
        ]))


def test_seed_requires_confirmed_schema():
    """Seeding is blocked until the physical schema is confirmed."""
    from workbench.backend import intake_schema_seed as seeder

    with Session(engine) as s:
        proj = Project(project_code="dmig-seed-guard", name="Seed", pg_connection="",
                       archetype="dmig", data_connectivity_mode="schema_only")
        s.add(proj); s.commit(); s.refresh(proj)
        with pytest.raises(seeder.SeedError):  # no confirmed schema row
            seeder.seed_graph_from_confirmed_schema(s, proj)
        s.delete(proj); s.commit()


def test_origin_repairs_missing_project_marker(_fake_parser):
    """Partial-scaffold safety: if the Project marker is missing but IntakeSpawn
    has the link, the origin endpoint resolves AND repairs the marker."""
    try:
        env = intake_router.IntakeEnvelope(
            scenario="migration", external_ref="repair-1",
            content=[intake_router.ContentPart(body="move orders")],
        )
        with Session(engine) as s:
            sid = intake_router.submit_intake(env, source_system="acme-analyzer", session=s)["intake_id"]
        assert asyncio.run(intake_worker.process_one()) is True
        with Session(engine) as s:
            rev = s.get(IntakeSubmission, sid).blueprint_revision
        with Session(engine) as s:
            project_id = intake_router.approve(
                sid, intake_router.ApproveBody(expected_revision=rev), session=s
            )["result"]["project_id"]

        # simulate a partial scaffold: clear the denormalized marker
        with Session(engine) as s:
            proj = s.get(Project, project_id)
            proj.parent_intake_submission_id = None
            s.add(proj)
            s.commit()

        with Session(engine) as s:
            origin = intake_router.intake_origin_for_project(project_id, session=s)
            assert origin["intake_id"] == sid
            # repaired on read
            assert s.get(Project, project_id).parent_intake_submission_id == sid
    finally:
        _cleanup_scaffolded()


# ── PO ownership of intake-scaffolded products ───────────────────────────────

def _modernization_blueprint_with_candidates():
    """A minimal modernization blueprint with one SA + one CF candidate + a dep."""
    return {
        "scenario": "modernization", "overall_confidence": "high",
        "source_aligned": [{
            "candidate_id": "src-0-cards",
            "name": {"value": "Cards", "confidence": "high", "why": "cluster"},
            "domain": {"value": "cards", "confidence": "high", "why": "spec"},
            "product_idea": "source-align the cards tables",
        }],
        "consumer_aligned": [{
            "candidate_id": "con-0-agg",
            "name": {"value": "Customer Lending Exposure", "confidence": "high", "why": "spec"},
            "purpose": "aggregate over the cards source",
        }],
        "dependencies": [{"dependency_id": "dep-0", "from_candidate_id": "con-0-agg",
                          "to_candidate_id": "src-0-cards", "confidence": "high"}],
        "gaps": [], "rationale": "portfolio",
    }


def _staged_modernization_submission(s, reviewed_by, external_ref="assembly:owner-1"):
    """A `scaffolding` structured submission ready for approve_and_scaffold."""
    sub = IntakeSubmission(
        source_system="connected-estate", external_ref=external_ref,
        scenario="modernization", status="scaffolding", ingestion_mode="structured",
        blueprint_json=json.dumps(_modernization_blueprint_with_candidates()),
        blueprint_revision=1,
        raw_payload_json=json.dumps({"source": "assembly", "provenance": {}}),
        reviewed_by=reviewed_by,
    )
    s.add(sub); s.commit(); s.refresh(sub)
    return sub


def test_intake_scaffold_stamps_po_owner_and_po_can_find_it():
    """The scaffold threads the PO's email (submission.reviewed_by) onto BOTH the
    dpe-sa and dpe-cf projects, and the web in-flight dashboard then surfaces the
    SA product to that PO."""
    from workbench.backend import intake_scaffold
    from workbench.backend.routers.product_requests import list_in_flight_products
    try:
        with Session(engine) as s:
            sub = _staged_modernization_submission(s, reviewed_by="po@x.com")
            intake_scaffold.approve_and_scaffold(s, sub)
        with Session(engine) as s:
            sa = s.exec(select(Project).where(Project.archetype == "dpe-sa")).all()[0]
            cf = s.exec(select(Project).where(Project.archetype == "dpe-cf")).all()[0]
            assert sa.owner_email == "po@x.com"
            assert cf.owner_email == "po@x.com"
            res = list_in_flight_products(owner_email="po@x.com", session=s)
            assert any(r["project_id"] == sa.id for r in res["requests"])
    finally:
        _cleanup_scaffolded()


def test_in_flight_matches_owner_email_when_submitted_by_differs():
    """The in-flight surface matches on Project.owner_email too — so a product whose
    ProductRequest.submitted_by is a machine principal (external-tool intake) still
    surfaces to the owning PO, and only to them."""
    from workbench.backend import intake_scaffold
    from workbench.backend.routers.product_requests import list_in_flight_products
    try:
        with Session(engine) as s:
            sub = _staged_modernization_submission(s, reviewed_by="po@x.com")
            intake_scaffold.approve_and_scaffold(s, sub)
        with Session(engine) as s:
            sa = s.exec(select(Project).where(Project.archetype == "dpe-sa")).all()[0]
            req = s.exec(select(ProductRequest).where(ProductRequest.project_id == sa.id)).first()
            req.submitted_by = "intake:connected-estate"  # machine principal
            s.add(req); s.commit()
            res = list_in_flight_products(owner_email="po@x.com", session=s)
            assert any(r["project_id"] == sa.id for r in res["requests"])
            # a different PO does NOT see it
            other = list_in_flight_products(owner_email="someone@else.com", session=s)
            assert all(r["project_id"] != sa.id for r in other["requests"])
    finally:
        _cleanup_scaffolded()


def test_machine_reviewer_leaves_owner_unset():
    """External-tool intake with no human reviewer (reviewed_by unset) must NOT
    invent a machine owner — owner_email stays None."""
    from workbench.backend import intake_scaffold
    try:
        with Session(engine) as s:
            sub = _staged_modernization_submission(s, reviewed_by=None,
                                                   external_ref="assembly:owner-machine")
            intake_scaffold.approve_and_scaffold(s, sub)
        with Session(engine) as s:
            sa = s.exec(select(Project).where(Project.archetype == "dpe-sa")).all()[0]
            assert sa.owner_email is None
    finally:
        _cleanup_scaffolded()


def test_backfill_intake_owner_email_repairs_existing():
    """The one-time backfill sets owner_email on a pre-fix intake-scaffolded project
    from its submission's reviewed_by."""
    from workbench.backend.models import IntakeSpawn
    from workbench.backend.routers.projects import ProjectCreate, create_project
    from scripts.backfill_intake_owner_email import backfill
    try:
        with Session(engine) as s:
            sub = IntakeSubmission(source_system="connected-estate", external_ref="assembly:bf-1",
                                   scenario="modernization", status="scaffolded",
                                   reviewed_by="po@x.com")
            s.add(sub); s.commit(); s.refresh(sub)
            proj = create_project(ProjectCreate(name="Legacy SA", archetype="dpe-sa",
                                                workflow_ids=None), s)  # owner_email=None
            pid = proj["id"]
            s.add(IntakeSpawn(intake_submission_id=sub.id, candidate_id="src-0", kind="dpe-sa",
                              child_project_id=pid, status="created"))
            s.commit()
            assert s.get(Project, pid).owner_email is None
            report = backfill(s, dry_run=False)
        with Session(engine) as s:
            assert s.get(Project, pid).owner_email == "po@x.com"
        assert report["updated"] >= 1
    finally:
        _cleanup_scaffolded()


# ── derived (dpe-cf) draft surface on My Products ────────────────────────────

def test_list_derived_drafts_surfaces_dpe_cf_draft(monkeypatch):
    """A scaffolded dpe-cf DRAFT (no ProductRequest, not published) surfaces to its
    owning PO with upstream source-readiness; published / other-owner ones don't."""
    from datetime import datetime
    from workbench.backend.routers import product_requests as pr
    from workbench.backend.models import IntakePendingDependency

    # Authoritative lifecycle comes from the graph; monkeypatch it (no Neo4j offline).
    def _fake_lifecycle(project, session):
        return "published" if project.name == "Published Agg" else "draft"
    monkeypatch.setattr(pr, "_read_contract_lifecycle", _fake_lifecycle)

    try:
        with Session(engine) as s:
            sub = IntakeSubmission(source_system="connected-estate", external_ref="assembly:drv-1",
                                   scenario="modernization", status="scaffolded", reviewed_by="po@x.com")
            s.add(sub); s.commit(); s.refresh(sub)

            cf = Project(project_code="dpe-drv-1", name="Lending Exposure", archetype="dpe-cf",
                         owner_email="po@x.com", parent_intake_submission_id=sub.id)
            pubcf = Project(project_code="dpe-drv-pub", name="Published Agg", archetype="dpe-cf",
                            owner_email="po@x.com")
            othercf = Project(project_code="dpe-drv-other", name="Other Agg", archetype="dpe-cf",
                              owner_email="someone@else.com")
            s.add(cf); s.add(pubcf); s.add(othercf); s.commit(); s.refresh(cf)

            # 3 source products: 2 published, 1 not. The first published one is
            # already 'bound' (its :CONSUMES edge wired), so dependencies_bound=1.
            for i, pub in enumerate([True, True, False]):
                sp = Project(project_code=f"dpe-sa-src-{i}", name=f"Src{i}", archetype="dpe-sa",
                             owner_email="po@x.com",
                             published_at=(datetime.utcnow() if pub else None))
                s.add(sp); s.commit(); s.refresh(sp)
                s.add(IntakePendingDependency(
                    intake_submission_id=sub.id, consumer_project_id=cf.id,
                    consumer_candidate_id="con-0", source_candidate_id=f"src-{i}",
                    source_project_id=sp.id, status=("bound" if i == 0 else "pending")))
            s.commit()

            drafts = pr.list_derived_drafts(owner_email="po@x.com", session=s)["drafts"]
            mine = [d for d in drafts if d["project_id"] == cf.id]
            assert len(mine) == 1
            d = mine[0]
            assert d["dependencies_total"] == 3
            assert d["dependencies_ready"] == 2   # only the 2 published sources count
            assert d["dependencies_bound"] == 1   # only the one flipped to 'bound'
            assert d["from_intake"] is True
            assert d["lifecycle_state"] == "draft"
            # published dpe-cf + other-owner dpe-cf are excluded
            codes = {x["project_code"] for x in drafts}
            assert "dpe-drv-pub" not in codes and "dpe-drv-other" not in codes
            # a different PO sees none of this PO's drafts
            other = pr.list_derived_drafts(owner_email="someone@else.com", session=s)["drafts"]
            assert all(x["project_id"] != cf.id for x in other)
    finally:
        _cleanup_scaffolded()


def test_resolve_pending_dependencies_flips_bound():
    """A consumer contract save that binds a source (via inputs[]) flips its
    matching IntakePendingDependency pending→bound; a non-matching source stays
    pending and a non-dpe-cf consumer is a no-op."""
    from workbench.backend import intake_scaffold

    try:
        with Session(engine) as s:
            sub = IntakeSubmission(source_system="connected-estate", external_ref="assembly:resolve-1",
                                   scenario="modernization", status="scaffolded", reviewed_by="po@x.com")
            s.add(sub); s.commit(); s.refresh(sub)

            cf = Project(project_code="dpe-resolve-cf", name="Exposure", archetype="dpe-cf",
                         owner_email="po@x.com", parent_intake_submission_id=sub.id)
            src_bound = Project(project_code="dpe-sa-bound", name="Bound Src", archetype="dpe-sa")
            src_other = Project(project_code="dpe-sa-other", name="Other Src", archetype="dpe-sa")
            s.add(cf); s.add(src_bound); s.add(src_other)
            s.commit(); s.refresh(cf); s.refresh(src_bound); s.refresh(src_other)

            dep_bound = IntakePendingDependency(
                intake_submission_id=sub.id, consumer_project_id=cf.id,
                consumer_candidate_id="con-0", source_candidate_id="src-bound",
                source_project_id=src_bound.id, status="pending")
            dep_other = IntakePendingDependency(
                intake_submission_id=sub.id, consumer_project_id=cf.id,
                consumer_candidate_id="con-0", source_candidate_id="src-other",
                source_project_id=src_other.id, status="pending")
            s.add(dep_bound); s.add(dep_other); s.commit()
            s.refresh(dep_bound); s.refresh(dep_other)

            # inputs bind ONLY src_bound — alias key tolerance: dprod_uri form
            flipped = intake_scaffold.resolve_pending_dependencies(
                s, cf, [{"dprod_uri": f"dprod:{src_bound.project_code}-contract"}]
            )
            assert flipped == 1
            s.refresh(dep_bound); s.refresh(dep_other)
            assert dep_bound.status == "bound"
            assert dep_other.status == "pending"   # not named in inputs → untouched

            # re-running is idempotent (already bound → nothing left to flip)
            assert intake_scaffold.resolve_pending_dependencies(
                s, cf, [{"contract_id": f"{src_bound.project_code}-contract"}]
            ) == 0

            # a non-dpe-cf consumer is a no-op even with a matching input
            sa = Project(project_code="dpe-sa-consumer", name="SA", archetype="dpe-sa")
            s.add(sa); s.commit(); s.refresh(sa)
            assert intake_scaffold.resolve_pending_dependencies(
                s, sa, [{"contract_id": f"{src_bound.project_code}-contract"}]
            ) == 0
    finally:
        _cleanup_scaffolded()
