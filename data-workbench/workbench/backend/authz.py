"""Coarse role authorization — the PO↔Engineer boundary, shared by REST + MCP.

Only two **account roles** are enforced server-side: ``owner`` (Data Product
Owner) and ``engineer``. Finer stage-level gating (which engineer *hat* —
Steward / DQA / Reviewer) stays client-side, now driven by the authenticated
account role instead of a free ``useState``.

These are small, pure helpers so both front doors (REST routes and the MCP
tools) enforce identically — UI/MCP parity is a hard requirement (see the
memory note ``feedback_ui_mcp_parity``). They mirror the ``ROLE_STAGE_PERMISSIONS``
/ ``ROLE_REVIEW_PERMISSIONS`` tables in the frontend ``types.ts``, collapsed to
the two account roles.
"""

from __future__ import annotations

from .auth import ENGINEER_ROLE, OWNER_ROLE

# The canonical stage ``owner_role`` values the Product Owner can run. Everything
# else (Data Engineer / Data Steward / Data Quality Analyst / Reviewer) is an
# engineer-side hat.
_PO_STAGE_OWNER_ROLES = {"Data Product Owner"}
_ENGINEER_STAGE_OWNER_ROLES = {
    "Data Engineer",
    "Data Steward",
    "Data Quality Analyst",
    "Reviewer",
}

# Which account role owns each review surface. PO owns the domain-rule gate and
# the source-product validation gate; the engineer account owns the rest (the
# engineer may switch hats client-side to Steward/Reviewer to clear those, but
# the server sees the single ``engineer`` account role).
_PO_REVIEW_TYPES = {
    "domain_rules",
    "source_product_validation",
    "table_descriptions",
    "relationship_descriptions",
}
_ENGINEER_REVIEW_TYPES = {
    "descriptions",
    "mappings",
    "unmapped_columns",
    "transformation_escalations",
    "semantic_discovery",
}

# The client-side hats each account role may assume (consumed by /api/auth/me,
# drives the frontend RoleContext + shell role switcher).
CLIENT_HATS_FOR_ROLE: dict[str, list[str]] = {
    OWNER_ROLE: ["Data Product Owner"],
    ENGINEER_ROLE: [
        "Data Engineer",
        "Data Steward",
        "Data Quality Analyst",
        "Reviewer",
    ],
}


def role_can_run_stage(role: str, owner_role: str | None) -> bool:
    """True if the account ``role`` may run a stage whose ``owner_role`` is given.

    ``owner`` → PO-owned stages; ``engineer`` → the four engineer-side hats.
    A stage with no/unknown owner_role is treated as engineer-owned (the
    default pipeline work).
    """
    if role == OWNER_ROLE:
        return owner_role in _PO_STAGE_OWNER_ROLES
    if role == ENGINEER_ROLE:
        return owner_role in _ENGINEER_STAGE_OWNER_ROLES or owner_role not in _PO_STAGE_OWNER_ROLES
    return False


def role_can_review(role: str, review_type: str | None) -> bool:
    """True if the account ``role`` may submit a review of ``review_type``."""
    if not review_type:
        return False
    if role == OWNER_ROLE:
        return review_type in _PO_REVIEW_TYPES
    if role == ENGINEER_ROLE:
        return review_type in _ENGINEER_REVIEW_TYPES
    return False


def client_hats(role: str) -> list[str]:
    return CLIENT_HATS_FOR_ROLE.get(role, [])


# ── FastAPI dependency factory ──────────────────────────────────────────────
def require_role(*roles: str):
    """Dependency: require the authenticated user to hold one of ``roles``.

    No-op when auth is disabled (local dev) — ``current_user`` returns the dev
    engineer, and we only enforce when auth is actually on so existing flows are
    unchanged. Use for the unambiguous single-role routes (serving = engineer;
    osi/deploy/publish = owner).
    """
    from fastapi import Depends, HTTPException

    from .auth import AuthUser, auth_enabled, current_user

    allowed = set(roles)

    def _dep(user: AuthUser = Depends(current_user)) -> AuthUser:
        if auth_enabled() and user.role not in allowed:
            raise HTTPException(
                status_code=403,
                detail={
                    "message": (
                        f"This action requires role {sorted(allowed)}; your account "
                        f"role is '{user.role}'."
                    ),
                    "required_roles": sorted(allowed),
                    "your_role": user.role,
                },
            )
        return user

    return _dep
