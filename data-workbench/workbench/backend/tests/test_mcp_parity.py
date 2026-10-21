"""UI/MCP parity tests (P1) — the subset exercisable without a live Neo4j.

Asserts that the MCP wrappers and the REST handlers they wrap converge on the
same SQLite-side state (request status, workflow graph), and that the
acceptance-gate recommendation surfaces for dpe-cf too. Graph-backed parity
(mapping graph, dataset transform, materialization) is covered by the manual
verification steps in the plan, since those require Neo4j/Postgres.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from workbench.backend.database import engine
from workbench.backend.models import (
    ProductRequest,
    ProductRequestStatus,
    Workflow,
)


# ── accept / reject parity: REST and MCP reach the same status ─────────────

def test_accept_parity_rest_vs_mcp(make_project_with_request):
    from workbench.backend.routers.product_requests import (
        accept_product_request, AcceptRequestBody,
    )
    from workbench.backend.mcp_server import accept_request

    # REST path
    p_rest, r_rest = make_project_with_request(status=ProductRequestStatus.submitted)
    with Session(engine) as s:
        accept_product_request(r_rest.id, AcceptRequestBody(engineer="eng"), s)

    # MCP path
    p_mcp, r_mcp = make_project_with_request(status=ProductRequestStatus.submitted)
    accept_request(p_mcp.project_code)

    with Session(engine) as s:
        rest_status = s.get(ProductRequest, r_rest.id).status
        mcp_status = s.get(ProductRequest, r_mcp.id).status
    assert rest_status == mcp_status == ProductRequestStatus.accepted


def test_reject_parity_rest_vs_mcp(make_project_with_request):
    from workbench.backend.routers.product_requests import (
        reject_product_request, RejectRequestBody,
    )
    from workbench.backend.mcp_server import reject_request

    p_rest, r_rest = make_project_with_request(status=ProductRequestStatus.submitted)
    with Session(engine) as s:
        reject_product_request(
            r_rest.id, RejectRequestBody(reason="x", category="too_broad", engineer="e"), s)

    p_mcp, r_mcp = make_project_with_request(status=ProductRequestStatus.submitted)
    reject_request(r_mcp.id, category="too_broad", reason="x")

    with Session(engine) as s:
        assert s.get(ProductRequest, r_rest.id).status == ProductRequestStatus.rejected
        assert s.get(ProductRequest, r_mcp.id).status == ProductRequestStatus.rejected


# ── workflow add / remove parity (SQLite-only) ─────────────────────────────

def test_workflow_add_list_remove_via_mcp(make_project_with_request):
    from workbench.backend.mcp_server import (
        list_available_workflows, add_workflow, remove_workflow,
    )
    # accepted so the acceptance gate never interferes
    project, _ = make_project_with_request(
        archetype="dpe-cf", status=ProductRequestStatus.accepted)

    avail = list_available_workflows(project.project_code)
    assert "catalog" in avail and avail["catalog"], "catalog should list addable workflows"
    wf_id = avail["catalog"][0]["workflow_id"]

    added = add_workflow(project.project_code, wf_id)
    assert added.get("status") == "added" and added.get("workflow_id") == wf_id

    with Session(engine) as s:
        rows = s.exec(
            select(Workflow).where(
                Workflow.project_id == project.id, Workflow.workflow_id == wf_id)
        ).all()
    assert len(rows) == 1, "MCP add_workflow must create exactly one Workflow row"

    removed = remove_workflow(project.project_code, wf_id)
    assert removed.get("status") == "removed"
    with Session(engine) as s:
        rows = s.exec(
            select(Workflow).where(
                Workflow.project_id == project.id, Workflow.workflow_id == wf_id)
        ).all()
    assert len(rows) == 0, "MCP remove_workflow must delete the Workflow row"


def test_add_duplicate_workflow_errors(make_project_with_request):
    from workbench.backend.mcp_server import (
        list_available_workflows, add_workflow,
    )
    project, _ = make_project_with_request(
        archetype="dpe-cf", status=ProductRequestStatus.accepted)
    wf_id = list_available_workflows(project.project_code)["catalog"][0]["workflow_id"]
    add_workflow(project.project_code, wf_id)
    dup = add_workflow(project.project_code, wf_id)
    assert "error" in dup


# ── uniform serving Build → Deploy lifecycle (registry + MCP tools) ─────────

def test_serving_build_deploy_registry_wiring():
    """Each serving mode has a Build member with a coupled Deploy dependent, and
    the deploy stages depend on their build stage. Guards the exclusive-group
    switching that carries Deploy along with its Build."""
    from workbench.backend.archetypes import (
        STAGE_REGISTRY, DEPENDENCY_GRAPH, EXCLUSIVE_GROUP_DEPENDENTS,
    )

    # The two new deploy stages exist and are non-LLM.
    for sid in ("deploy_lakehouse", "deploy_physical_copy"):
        assert sid in STAGE_REGISTRY, f"{sid} missing from STAGE_REGISTRY"
        assert STAGE_REGISTRY[sid]["requires_llm"] is False

    # Deploy stages depend on their Build stage.
    assert DEPENDENCY_GRAPH["deploy_physical_copy"] == ["serving_physical_copy"]
    assert DEPENDENCY_GRAPH["deploy_lakehouse"] == ["serving_lakehouse_export"]

    # Exclusive-group dependents pair each Build member with its Deploy.
    serving = EXCLUSIVE_GROUP_DEPENDENTS["serving"]
    assert "deploy_virtual_view" in serving["serving_virtual_view"]
    assert "deploy_physical_copy" in serving["serving_physical_copy"]
    assert "deploy_lakehouse" in serving["serving_lakehouse_export"]
    # profile_parquet stays a coupled lakehouse verify.
    assert "profile_parquet" in serving["serving_lakehouse_export"]
    # transfer follows the same Build → Run split: the Build member carries a
    # coupled Run/Deploy (deploy_transfer) plus the secondary placement config.
    assert "deploy_transfer" in serving["serving_transfer"]
    assert "configure_transfer_placement" in serving["serving_transfer"]


def test_serving_build_deploy_in_templates():
    """New projects' serving templates carry all three deploy stages (only the
    selected mode's deploy is enabled — the switch flips the others)."""
    from workbench.backend.archetypes import (
        _WF_INTEGRATION, _WF_PRODUCT_MATERIALIZATION_SA,
    )
    for wf in (_WF_INTEGRATION, _WF_PRODUCT_MATERIALIZATION_SA):
        ids = {s["stage_id"] for s in wf["stages"]}
        assert {"deploy_virtual_view", "deploy_physical_copy", "deploy_lakehouse"} <= ids
        by_id = {s["stage_id"]: s for s in wf["stages"]}
        # virtual deploy is the default-enabled one; the alternatives ship disabled.
        assert by_id["deploy_virtual_view"]["enabled"] is True
        assert by_id["deploy_physical_copy"]["enabled"] is False
        assert by_id["deploy_lakehouse"]["enabled"] is False


def test_serving_build_mcp_tools_exist():
    """The build-package MCP tools (parity with the new build stages) are exposed."""
    from workbench.backend import mcp_server
    assert callable(getattr(mcp_server, "build_lakehouse_package", None))
    assert callable(getattr(mcp_server, "build_dbt_package", None))


# ── dual-mode DQ: product_dq_testing group + archetype-conditional catalog ───

def test_product_dq_workflow_group_registered():
    """The dprod DQ add-on reuses the DQ stage IDs, drops rule generation, and
    tags every stage source_mode='dprod'."""
    from workbench.backend.archetypes import _ALL_WORKFLOW_GROUPS
    pdq = next((w for w in _ALL_WORKFLOW_GROUPS if w["workflow_id"] == "product_dq_testing"), None)
    assert pdq is not None, "product_dq_testing not in _ALL_WORKFLOW_GROUPS"
    sids = [s["stage_id"] for s in pdq["stages"]]
    assert "dq_rule_generation" not in sids, "dprod DQ must not include rule generation"
    assert {"dq_test_generation_gx", "dq_test_generation_python", "dq_test_execution"} <= set(sids)
    assert all(s.get("source_mode") == "dprod" for s in pdq["stages"])


def test_dq_catalog_visibility_by_archetype():
    """Catalog-side DQ is hidden for consumer products; product-side DQ is shown
    only for products; dataset archetypes keep catalog DQ and hide product DQ."""
    from workbench.backend.archetypes import get_workflow_catalog

    def ids(arch):
        return {c["workflow_id"] for c in get_workflow_catalog(arch)}

    cf, sa, dq, unscoped = ids("dpe-cf"), ids("dpe-sa"), ids("dq"), ids(None)
    # consumer-aligned: product DQ only
    assert "product_dq_testing" in cf
    assert "dq_testing" not in cf and "baseline_dq_rules" not in cf
    # source-aligned: both (product primary, catalog optional pre-check)
    assert {"product_dq_testing", "dq_testing", "baseline_dq_rules"} <= sa
    # dataset archetype: catalog DQ only
    assert "product_dq_testing" not in dq and "dq_testing" in dq
    # unscoped (back-compat): everything
    assert {"product_dq_testing", "dq_testing", "baseline_dq_rules"} <= unscoped


def test_resolve_dq_source_mode():
    """The dispatcher keys catalog-vs-dprod off the workflow the stage runs in."""
    from workbench.backend.stage_execution import resolve_dq_source_mode

    class _P:
        project_code = "x-01"

    assert resolve_dq_source_mode(_P(), "product_dq_testing") == "dprod"
    assert resolve_dq_source_mode(_P(), "dq_testing") == "catalog"
    assert resolve_dq_source_mode(_P(), None) == "catalog"


# ── Phase 4: Configure DQ stage + Build/Run naming ───────────────────────────

def test_configure_dq_stage_and_build_run_naming():
    """configure_dq exists (non-LLM checkpoint), leads both DQ workflow groups,
    gates the Build stage, and the DQ stages read as Configure → Build → Run."""
    from workbench.backend.archetypes import (
        STAGE_REGISTRY, DEPENDENCY_GRAPH, _ALL_WORKFLOW_GROUPS,
    )
    cd = STAGE_REGISTRY.get("configure_dq")
    assert cd is not None and cd["requires_llm"] is False and cd["review_type"] is None
    # Configure → Build → Run naming
    assert STAGE_REGISTRY["dq_test_generation_gx"]["name"].startswith("Build DQ Package")
    assert STAGE_REGISTRY["dq_test_generation_python"]["name"].startswith("Build DQ Package")
    assert STAGE_REGISTRY["dq_test_execution"]["name"] == "Run DQ Tests"
    # Build depends on Configure (present-if-in-workflow)
    assert "configure_dq" in DEPENDENCY_GRAPH["dq_test_generation_gx"]
    assert "configure_dq" in DEPENDENCY_GRAPH["dq_test_generation_python"]
    # configure_dq leads both DQ groups
    for wf_id in ("dq_testing", "product_dq_testing"):
        g = next(w for w in _ALL_WORKFLOW_GROUPS if w["workflow_id"] == wf_id)
        assert g["stages"][0]["stage_id"] == "configure_dq"


def test_configure_dq_completable_via_mcp():
    """configure_dq is a mechanical checkpoint (not backend-driven / LLM / review),
    so the MCP complete_stage guard admits it — no new tool needed (count stable)."""
    from workbench.backend.mcp_server import _BACKEND_DRIVEN_STAGE_IDS
    from workbench.backend.archetypes import STAGE_REGISTRY
    cd = STAGE_REGISTRY["configure_dq"]
    assert "configure_dq" not in _BACKEND_DRIVEN_STAGE_IDS
    assert not cd["requires_llm"] and not cd["review_type"]


# ── plan-summary surfaces the acceptance gate for dpe-cf too (P0.6) ─────────

@pytest.mark.parametrize("archetype", ["dpe-sa", "dpe-cf"])
def test_plan_summary_recommends_accept_when_submitted(make_project_with_request, archetype):
    from workbench.backend.mcp_server import get_plan_summary
    project, _ = make_project_with_request(
        archetype=archetype, status=ProductRequestStatus.submitted)
    summary = get_plan_summary(project.project_code)
    rec = summary.get("recommended_next") or {}
    assert rec.get("action") == "accept_request", (
        f"{archetype}: plan summary should recommend accept_request while submitted"
    )
    assert any("Accept request" in b for b in summary.get("blocked", []))


# ── MCP auth hardening: 4-field tokens, role binding, read-only (P1) ─────────

def test_parse_tokens_backcompat_and_4field():
    from workbench.backend.mcp_server import _parse_tokens

    t = _parse_tokens("bare")
    assert t["bare"] == {"principal": "engineer", "projects": None, "role": None, "read_only": False}

    t = _parse_tokens("tok:alice")
    assert t["tok"]["principal"] == "alice" and t["tok"]["role"] is None

    t = _parse_tokens("tok:alice:p1|p2")
    assert t["tok"]["projects"] == {"p1", "p2"} and t["tok"]["role"] is None

    t = _parse_tokens("tok:alice:*:engineer")
    assert t["tok"]["projects"] is None and t["tok"]["role"] == "engineer" and t["tok"]["read_only"] is False

    t = _parse_tokens("tok:bob:p1:owner")
    assert t["tok"]["role"] == "owner" and t["tok"]["projects"] == {"p1"}

    t = _parse_tokens("tok:carol:*:viewer")
    assert t["tok"]["read_only"] is True and t["tok"]["role"] is None


def test_role_guard_token_role_overrides_declared_param():
    from workbench.backend import mcp_server as m

    # Bound owner token: declaring "Data Engineer" (which would pass for mappings
    # under the legacy param path) must NOT let an owner approve a mapping.
    reset = m._auth_ctx.set({"principal": "po", "projects": None, "role": "owner", "read_only": False})
    try:
        assert m._role_guard("mappings", "Data Engineer") is not None  # refused
        assert m._role_guard("domain_rules", "Data Engineer") is None  # owner owns domain_rules
    finally:
        m._auth_ctx.reset(reset)

    # Bound engineer token: can do mappings, cannot do domain_rules regardless of param.
    reset = m._auth_ctx.set({"principal": "eng", "projects": None, "role": "engineer", "read_only": False})
    try:
        assert m._role_guard("mappings", "Data Product Owner") is None
        assert m._role_guard("domain_rules", "Data Product Owner") is not None
    finally:
        m._auth_ctx.reset(reset)


def test_role_guard_falls_back_to_param_without_bound_role():
    from workbench.backend import mcp_server as m
    # No ctx (dev/legacy token) → the self-declared param is trusted.
    assert m._role_guard("mappings", "Data Engineer") is None
    assert m._role_guard("mappings", "Data Product Owner") is not None


def test_deny_write_viewer_token():
    from workbench.backend import mcp_server as m
    reset = m._auth_ctx.set({"principal": "v", "projects": None, "role": None, "read_only": True})
    try:
        assert m._deny_write() is not None
    finally:
        m._auth_ctx.reset(reset)
    # A non-viewer token with global read-only off → writes allowed.
    assert m._deny_write() is None


def test_deny_write_global_read_only(monkeypatch):
    from workbench.backend import mcp_server as m
    monkeypatch.setenv("WB_READ_ONLY", "1")
    assert m._deny_write() is not None
    monkeypatch.setenv("WB_READ_ONLY", "0")
    assert m._deny_write() is None


def test_run_stage_refuses_owner_token_on_engineer_stage(make_project_with_request):
    """An owner-bound MCP token can't run an engineer-owned stage (PO↔Engineer)."""
    import asyncio
    from workbench.backend import mcp_server as m

    project, _ = make_project_with_request(
        archetype="dpe-cf", status=ProductRequestStatus.accepted)
    reset = m._auth_ctx.set({"principal": "po", "projects": None, "role": "owner", "read_only": False})
    try:
        res = asyncio.run(m.run_stage(project.project_code, 1))
    finally:
        m._auth_ctx.reset(reset)
    assert "error" in res and "cannot run" in res["error"].lower()


def test_po_mcp_scan_datasets_delegates_to_router():
    """get_estate_scan_datasets MCP tool returns 404 for a missing scan — same as the REST handler."""
    from workbench.backend import po_mcp_server as po
    result = po.get_estate_scan_datasets(scan_id=9999999)
    assert "error" in result


def test_po_mcp_scan_assets_delegates_to_router():
    """get_estate_scan_assets MCP tool returns 404 for a missing scan — same as the REST handler."""
    from workbench.backend import po_mcp_server as po
    result = po.get_estate_scan_assets(scan_id=9999999)
    assert "error" in result


def test_po_mcp_save_feasibility_candidate_returns_error_for_missing_score():
    """save_feasibility_candidate returns an error for a non-existent score_id."""
    from workbench.backend import po_mcp_server as po
    result = po.save_feasibility_candidate(
        score_id=9999999, owner_email="test@example.com", notes="test note"
    )
    assert "error" in result


def test_po_mcp_list_feasibility_candidates_returns_empty_for_unknown_owner():
    """list_feasibility_candidates returns an empty list for an owner with no candidates."""
    from workbench.backend import po_mcp_server as po
    result = po.list_feasibility_candidates(owner_email="nobody@example.com")
    assert "candidates" in result
    assert result["candidates"] == []
