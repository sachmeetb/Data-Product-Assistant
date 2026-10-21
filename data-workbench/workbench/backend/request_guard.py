"""Shared acceptance-lifecycle guard for engineer-owned mutations.

A :class:`ProductRequest` is the governance object that hands a product from
the Product Workbench to Engineering. Until the engineer *accepts* the
request (``submitted -> accepted``), no engineering work should mutate the
product. Historically this rule was enforced only in the frontend
(``Pipeline.tsx`` ``blockedOnPo``) and surfaced as dpe-sa-only advice in the
MCP ``get_plan_summary`` — so an MCP client (or any direct REST caller) could
run stages, complete stages, change serving mode, or submit reviews while the
request was still ``submitted``.

This module is the single source of truth for that rule, shared by the MCP
handlers and the REST routes so the two front doors enforce identically. The
rule is **universal** — it applies to both ``dpe-sa`` and ``dpe-cf``.

Rollout is gated by ``WB_ENFORCE_ACCEPT_GATE``:

* unset / ``0`` (default)  → **warn-only**: the action proceeds, but a
  non-fatal warning is attached so behaviour is observable before the gate is
  made strict. Nothing breaks for existing automation.
* ``1`` / ``true``         → **hard block**: callers must refuse the action
  (MCP returns a structured error; REST raises 409) until the request is
  accepted or rejected.

Read-only inspection and the accept/reject *decision* tools are never guarded
— callers simply don't invoke this for those.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session, select

from .models import Project, ProductRequest, ProductRequestStatus


def enforcement_enabled() -> bool:
    """True when WB_ENFORCE_ACCEPT_GATE selects hard-block mode.

    Read live (not cached at import) so tests and operators can flip the gate
    without restarting the process.
    """
    return os.environ.get("WB_ENFORCE_ACCEPT_GATE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def active_request_status(
    project_id: int, session: Session
) -> tuple[Optional[ProductRequestStatus], Optional[int]]:
    """Return the (status, id) of the project's latest non-rejected request.

    Mirrors the "latest non-rejected request" query already used in
    ``mcp_server.accept_request`` and ``product_requests.latest_product_request``
    so the guard keys off the same row the engineer's queue acts on. Returns
    ``(None, None)`` when the project has no governing request (e.g. an
    engineer-only ``dq``/``dd`` project) — those are never gated.
    """
    request = session.exec(
        select(ProductRequest)
        .where(ProductRequest.project_id == project_id)
        .where(ProductRequest.status != ProductRequestStatus.rejected)
        .order_by(ProductRequest.submitted_at.desc())
    ).first()
    if request is None:
        return None, None
    return request.status, request.id


@dataclass
class GuardResult:
    """Outcome of an acceptance-gate check.

    ``allowed``  — whether the caller should let the action proceed *now*
                   (always True in warn-only mode; False only when the gate is
                   both tripped and enforcement is on).
    ``blocked``  — whether the gate is tripped (request still ``submitted``),
                   independent of enforcement mode. Drives the warning even
                   when ``allowed`` is True.
    ``enforced`` — whether hard-block mode is active.
    """

    allowed: bool
    blocked: bool
    enforced: bool
    status: Optional[str] = None
    request_id: Optional[int] = None
    message: Optional[str] = None
    required_action: Optional[str] = None

    def as_dict(self) -> dict:
        """Compact JSON-able shape for MCP responses / warning payloads."""
        out: dict = {
            "blocked": self.blocked,
            "enforced": self.enforced,
            "allowed": self.allowed,
        }
        if self.status is not None:
            out["request_status"] = self.status
        if self.request_id is not None:
            out["request_id"] = self.request_id
        if self.message:
            out["message"] = self.message
        if self.required_action:
            out["required_action"] = self.required_action
        return out


def check_engineer_mutation_allowed(
    project: Project, session: Session
) -> GuardResult:
    """Decide whether an engineer-owned mutation may proceed for ``project``.

    Blocked iff the latest non-rejected ProductRequest is still ``submitted``.
    Applies to both archetypes. Honours :func:`enforcement_enabled` for the
    warn-only → hard-block rollout.
    """
    status, request_id = active_request_status(project.id, session)
    enforced = enforcement_enabled()

    if status != ProductRequestStatus.submitted:
        # No request, or already accepted/complete — nothing to gate.
        return GuardResult(
            allowed=True,
            blocked=False,
            enforced=enforced,
            status=status.value if status else None,
            request_id=request_id,
        )

    message = (
        "Product request is still 'submitted' — accept it (accept_request) "
        "before running, completing, or reviewing engineering work. "
        + ("Blocked by WB_ENFORCE_ACCEPT_GATE." if enforced
           else "Warn-only: proceeding, but this will be blocked once the gate is enforced.")
    )
    return GuardResult(
        allowed=not enforced,
        blocked=True,
        enforced=enforced,
        status=status.value,
        request_id=request_id,
        message=message,
        required_action="accept_request",
    )


_log = logging.getLogger("workbench.request_guard")


def guard_rest_mutation(project: Project, session: Session) -> GuardResult:
    """REST-route convenience: enforce the acceptance gate.

    Hard-block mode → raises ``HTTPException(409)`` with an actionable detail.
    Warn-only mode → logs the warning and returns the :class:`GuardResult` so
    the caller may surface it (e.g. on a response payload) without failing.
    """
    guard = check_engineer_mutation_allowed(project, session)
    if guard.blocked:
        if not guard.allowed:
            from fastapi import HTTPException
            raise HTTPException(
                409,
                {"message": guard.message, "required_action": guard.required_action,
                 "request_id": guard.request_id},
            )
        _log.warning(
            "acceptance-gate (warn-only): project=%s request=%s still submitted",
            getattr(project, "project_code", project.id), guard.request_id,
        )
    return guard
