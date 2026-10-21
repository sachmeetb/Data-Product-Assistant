"""Acceptance-lifecycle hardening tests (P0) + assignment-tool coverage.

Covers the shared `request_guard`, the warn-only ↔ hard-block rollout, REST
accept idempotency + completion-requires-accepted, and the MCP assignment tools
(list/get/accept/reject), for BOTH dpe-sa and dpe-cf.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session

from workbench.backend.database import engine
from workbench.backend.models import Project, ProductRequest, ProductRequestStatus
from workbench.backend import request_guard
from workbench.backend.request_guard import (
    check_engineer_mutation_allowed,
    guard_rest_mutation,
)


# ── request_guard unit behaviour ───────────────────────────────────────────

@pytest.mark.parametrize("archetype", ["dpe-sa", "dpe-cf"])
def test_guard_blocks_submitted_both_archetypes(make_project_with_request, archetype):
    project, _ = make_project_with_request(archetype=archetype,
                                           status=ProductRequestStatus.submitted)
    with Session(engine) as s:
        proj = s.get(Project, project.id)
        result = check_engineer_mutation_allowed(proj, s)
    assert result.blocked is True
    assert result.required_action == "accept_request"
    # warn-only by default → still allowed
    assert result.allowed is True
    assert result.enforced is False


def test_guard_allows_accepted(make_project_with_request):
    project, _ = make_project_with_request(status=ProductRequestStatus.accepted)
    with Session(engine) as s:
        proj = s.get(Project, project.id)
        result = check_engineer_mutation_allowed(proj, s)
    assert result.blocked is False
    assert result.allowed is True


def test_guard_no_request_is_ungated(session):
    """A project with no governing request (dq/dd) is never gated."""
    project = Project(project_code="test-dq-noreq", name="n", pg_connection="", archetype="dq")
    session.add(project)
    session.commit()
    session.refresh(project)
    result = check_engineer_mutation_allowed(project, session)
    assert result.blocked is False
    assert result.allowed is True
    assert result.status is None


def test_guard_hard_block_when_enforced(make_project_with_request, monkeypatch):
    monkeypatch.setenv("WB_ENFORCE_ACCEPT_GATE", "1")
    assert request_guard.enforcement_enabled() is True
    project, _ = make_project_with_request(status=ProductRequestStatus.submitted)
    with Session(engine) as s:
        proj = s.get(Project, project.id)
        result = check_engineer_mutation_allowed(proj, s)
    assert result.blocked is True
    assert result.allowed is False     # hard block
    assert result.enforced is True


def test_guard_rest_raises_409_when_enforced(make_project_with_request, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("WB_ENFORCE_ACCEPT_GATE", "1")
    project, _ = make_project_with_request(status=ProductRequestStatus.submitted)
    with Session(engine) as s:
        proj = s.get(Project, project.id)
        with pytest.raises(HTTPException) as ei:
            guard_rest_mutation(proj, s)
    assert ei.value.status_code == 409
    assert ei.value.detail.get("required_action") == "accept_request"


def test_guard_rest_warn_only_does_not_raise(make_project_with_request):
    project, _ = make_project_with_request(status=ProductRequestStatus.submitted)
    with Session(engine) as s:
        proj = s.get(Project, project.id)
        result = guard_rest_mutation(proj, s)  # must NOT raise
    assert result.blocked is True
    assert result.allowed is True


# ── REST accept idempotency + completion gate ──────────────────────────────

def test_rest_accept_then_reaccept_is_idempotent(make_project_with_request):
    from workbench.backend.routers.product_requests import (
        accept_product_request, AcceptRequestBody,
    )
    project, request = make_project_with_request(status=ProductRequestStatus.submitted)
    with Session(engine) as s:
        first = accept_product_request(request.id, AcceptRequestBody(engineer="eng"), s)
        assert first["status"] == "accepted"
        # second accept must NOT raise — returns no-op success
        second = accept_product_request(request.id, AcceptRequestBody(engineer="eng"), s)
        assert second["status"] == "accepted"
        assert "note" in second


def test_rest_complete_requires_accepted_when_enforced(make_project_with_request, monkeypatch):
    from fastapi import HTTPException
    from workbench.backend.routers.product_requests import (
        complete_product_request, CompleteRequestBody,
    )
    monkeypatch.setenv("WB_ENFORCE_ACCEPT_GATE", "1")
    project, request = make_project_with_request(
        archetype="dpe-cf", status=ProductRequestStatus.submitted)
    with Session(engine) as s:
        with pytest.raises(HTTPException) as ei:
            complete_product_request(request.id, CompleteRequestBody(), s)
    assert ei.value.status_code == 409


def test_rest_complete_warn_only_allows_submitted(make_project_with_request):
    """Warn-only: completing from 'submitted' still works (back-compat)."""
    from workbench.backend.routers.product_requests import (
        complete_product_request, CompleteRequestBody,
    )
    project, request = make_project_with_request(
        archetype="dpe-cf", status=ProductRequestStatus.submitted)
    with Session(engine) as s:
        result = complete_product_request(request.id, CompleteRequestBody(), s)
    assert result["status"] == "complete"


# ── MCP assignment tools ───────────────────────────────────────────────────

def test_mcp_list_assignments_filters(make_project_with_request):
    from workbench.backend.mcp_server import list_assignments
    p1, _ = make_project_with_request(status=ProductRequestStatus.submitted)
    p2, _ = make_project_with_request(status=ProductRequestStatus.accepted)
    all_a = list_assignments()
    assert all_a["count"] >= 2
    submitted = list_assignments(status="submitted")
    assert all(a["status"] == "submitted" for a in submitted["assignments"])
    scoped = list_assignments(project_code=p1.project_code)
    assert scoped["count"] == 1
    assert scoped["assignments"][0]["project_code"] == p1.project_code


def test_mcp_list_assignments_bad_status(make_project_with_request):
    from workbench.backend.mcp_server import list_assignments
    make_project_with_request()
    out = list_assignments(status="bogus")
    assert "error" in out


@pytest.mark.parametrize("archetype", ["dpe-sa", "dpe-cf"])
def test_mcp_accept_request_both_archetypes(make_project_with_request, archetype):
    from workbench.backend.mcp_server import accept_request
    project, request = make_project_with_request(
        archetype=archetype, status=ProductRequestStatus.submitted)
    out = accept_request(project.project_code)
    assert out["ok"] is True
    assert out["status"] == "accepted"
    assert out["request_id"] == request.id


def test_mcp_accept_request_idempotent(make_project_with_request):
    from workbench.backend.mcp_server import accept_request
    project, _ = make_project_with_request(status=ProductRequestStatus.submitted)
    accept_request(project.project_code)
    again = accept_request(project.project_code)
    assert again["ok"] is True
    assert again["status"] == "accepted"
    assert "note" in again  # no-op path


def test_mcp_accept_request_by_id(make_project_with_request):
    from workbench.backend.mcp_server import accept_request
    project, request = make_project_with_request(status=ProductRequestStatus.submitted)
    out = accept_request(project.project_code, request_id=request.id)
    assert out["request_id"] == request.id
    # wrong id for this project → error
    bad = accept_request(project.project_code, request_id=999999)
    assert "error" in bad


def test_mcp_get_assignment(make_project_with_request):
    from workbench.backend.mcp_server import get_assignment
    project, request = make_project_with_request(status=ProductRequestStatus.submitted)
    out = get_assignment(request.id)
    assert out["id"] == request.id
    assert out["project_code"] == project.project_code
    assert get_assignment(999999).get("error")


def test_mcp_reject_request_blocks_then_acceptance(make_project_with_request):
    from workbench.backend.mcp_server import reject_request, accept_request
    project, request = make_project_with_request(status=ProductRequestStatus.submitted)
    out = reject_request(request.id, category="too_broad", reason="scope")
    assert out["ok"] is True
    assert out["status"] == "rejected"
    # a rejected request can't then be accepted (latest non-rejected is none)
    acc = accept_request(project.project_code)
    assert "error" in acc


def test_mcp_list_rejection_categories():
    from workbench.backend.mcp_server import list_rejection_categories
    out = list_rejection_categories()
    values = {c["value"] for c in out["categories"]}
    assert "too_broad" in values and "duplicate" in values
