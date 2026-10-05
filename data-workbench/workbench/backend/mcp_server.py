"""MCP control-plane server for the Data Workbench.

Exposes the backend control plane as MCP tools so a data engineer's own Claude
Code (or any MCP client) can drive the workbench — locally or across a container
boundary (the Docker / remote-engineer goal). Mounted onto the FastAPI app at
``/mcp`` (see ``main.py``) so a single service/port serves both the web UI
(``/api/*``) and engineers' Claude Code (``/mcp``).

The surface now spans the full lifecycle, not just reads:
- **Read / query**: ``list_projects``, ``get_project_state``, ``get_stage_results``,
  ``get_stage_config_options``, and project-scoped read-only ``run_cypher``.
- **Stage execution**: ``run_stage`` (async), ``reset_stage``, ``complete_stage`` (the
  non-agent lifecycle stages), ``set_data_source``.
- **Interactive**: ``get_pending_questions`` / ``answer_question`` to drive stages
  that ask mid-run (e.g. discovery's "which tables?").
- **Review-write**: ``review_description``, ``review_mapping``, ``review_domain_rule``,
  ``review_table_description``, ``review_relationship_description`` — each takes a
  declared ``role`` and records PROV-O attribution to the token principal.

This server is the **trust boundary**: every tool enforces project scoping, role
permissions, and read-only Cypher server-side, regardless of what the client
sends. Client-side ``.mcp.json`` / permissions are not trusted. Auth is bearer
tokens via ``WB_MCP_TOKENS`` (``token``/``token:principal``/``token:principal:proj1|proj2``);
fail-closed unless ``WB_MCP_ALLOW_INSECURE=1`` is set for local dev.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlmodel import Session, select

from . import config
from .database import engine
from .models import Project, ProductRequest, StageRun

_log = logging.getLogger("workbench.mcp")


# ---------------------------------------------------------------------------
# Authentication (bearer token).
#
# The /mcp endpoint is the remote trust boundary. Tokens are configured via the
# WB_MCP_TOKENS env var: a comma-separated list of "token" or "token:principal"
# entries (principal is an engineer email/label used for attribution + future
# per-engineer authorization). Auth is GATED: enforced when WB_MCP_TOKENS is
# set, open when it isn't (matches the local-dev / unauthenticated-REST posture
# and the Foundry-key gating). A containerized / shared / remote deployment MUST
# set WB_MCP_TOKENS — a loud warning is logged when it doesn't.
# ---------------------------------------------------------------------------
def _parse_tokens(raw: str) -> dict[str, dict]:
    """Parse WB_MCP_TOKENS into
    ``{token: {"principal": str, "projects": set|None, "role": str|None, "read_only": bool}}``.

    Entry forms (comma-separated):
      token                        -> principal 'engineer', ALL projects, no bound role
      token:principal              -> that principal, ALL projects
      token:principal:*            -> that principal, ALL projects
      token:principal:p1|p2        -> restricted to p1, p2
      token:principal:projects:role -> 4th field binds an account role:
                                        ``owner`` | ``engineer`` | ``viewer``
                                        (``viewer``/``readonly`` ⇒ read-only token)

    Fully back-compatible: a missing 4th field → ``role=None`` (unrestricted, as
    today). ``projects is None`` means unrestricted (all projects). A bound role
    is honoured by _role_guard (ignoring the self-declared review param).
    """
    tokens: dict[str, dict] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 3)
        tok = parts[0].strip()
        principal = (parts[1].strip() if len(parts) > 1 else "") or "engineer"
        projects: set[str] | None = None
        if len(parts) > 2:
            spec = parts[2].strip()
            if spec and spec != "*":
                projects = {c.strip() for c in spec.split("|") if c.strip()}
        role: str | None = None
        read_only = False
        if len(parts) > 3:
            r = parts[3].strip().lower()
            if r in ("viewer", "readonly", "read_only", "read-only"):
                read_only = True
                role = None  # a viewer can't write anyway; no bound review role
            elif r in ("owner", "engineer"):
                role = r
        if tok:
            tokens[tok] = {
                "principal": principal,
                "projects": projects,
                "role": role,
                "read_only": read_only,
            }
    return tokens


# Per-request auth context, set by _BearerAuthMiddleware after it validates the
# bearer token. `{"principal": str, "projects": set|None}` or None when auth is
# disabled (open/local-dev mode). Read by _principal / _authorized_projects.
_auth_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "wb_mcp_auth", default=None
)


def _principal() -> str:
    """The caller's principal (token label/email) for PROV-O attribution.

    The MCP caller acts on behalf of a human data engineer, so review writes are
    recorded as that human in provenance. Falls back to a generic label when auth
    is disabled (local dev).
    """
    ctx = _auth_ctx.get()
    return (ctx.get("principal") if ctx else None) or "workbench-mcp"


def _auth_role() -> str | None:
    """The account role bound to the caller's token (``owner`` | ``engineer``),
    or ``None`` when the token has no bound role (dev / legacy 1-3-field token)."""
    ctx = _auth_ctx.get()
    return ctx.get("role") if ctx else None


def _deny_write() -> dict | None:
    """Refuse a mutating tool when serving is blocked (no WB_MCP_TOKENS + not
    opted into open mode), the instance is globally read-only, OR the caller's
    token is a viewer/read-only token. Folded into every write tool's guard
    alongside _deny_project. The _serving_guard() check ensures a project-*un*scoped
    write (e.g. create_connection, the intake-decision tools) still honours the
    fail-closed "refuse when unconfigured" posture — _deny_project embeds the same
    guard, so project-scoped writes were already covered. Returns an error dict or None."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    if config.read_only():
        return {"error": (
            "This is a read-only Data Workbench instance — mutations are disabled."
        )}
    ctx = _auth_ctx.get()
    if ctx and ctx.get("read_only"):
        return {"error": (
            "Your token is read-only (viewer) — this action is not permitted. "
            "Ask the operator for a role-bound (engineer/owner) token."
        )}
    return None


# Canonical workbench roles (match the role table in CLAUDE.md). The MCP caller
# DECLARES the role it is acting as on each review write — the engineer "switches
# role" to a steward to clear steward-gated items. Enforced server-side.
_CANON_ROLES = {
    "Data Product Owner", "Data Engineer", "Data Steward",
    "Data Quality Analyst", "Reviewer",
}


def _normalize_role(role: str | None) -> str | None:
    """Map a loosely-spelled role to its canonical name (case/format-insensitive)."""
    if not role:
        return None
    s = role.strip().lower()
    if "steward" in s:
        return "Data Steward"
    if "engineer" in s:
        return "Data Engineer"
    if "quality" in s or s in ("dqa", "dq analyst"):
        return "Data Quality Analyst"
    if "owner" in s or s in ("po", "dpo"):
        return "Data Product Owner"
    if "review" in s:
        return "Reviewer"
    return role.strip() if role.strip() in _CANON_ROLES else None


# Which roles may sign off each review surface (mirrors CLAUDE.md "Can review").
# Descriptions exclude Data Engineer on purpose — an engineer must SWITCH to
# Data Steward (or Reviewer/PO) to approve them, which is the role-switch UX.
_REVIEW_ROLES: dict[str, set[str]] = {
    "descriptions": {"Data Steward", "Reviewer", "Data Product Owner"},
    "mappings": {"Data Engineer", "Reviewer"},
    "domain_rules": {"Data Quality Analyst", "Data Product Owner", "Reviewer"},
    # Table + relationship descriptions are reviewed in the dpe-sa PO validation
    # gate (PO-owned), with Steward/Reviewer also permitted.
    "table_descriptions": {"Data Product Owner", "Data Steward", "Reviewer"},
    "relationship_descriptions": {"Data Product Owner", "Data Steward", "Reviewer"},
    # Semantic-layer discovery (scaffold / recommend / enrich / reset) is a
    # Steward activity, but engineers drive the sequence over MCP too.
    "semantic_discovery": {"Data Steward", "Data Engineer", "Data Product Owner"},
}


def _role_guard(review_type: str, role: str | None) -> dict | None:
    """Validate the caller may act on this review surface.

    When the caller's TOKEN carries a bound account role (``owner`` /
    ``engineer``), that is authoritative — the self-declared ``role`` param is
    IGNORED (closing the "surface separation, not a security tier" gap called out
    in CLAUDE.md). Only when the token has NO bound role (dev / legacy token) do
    we fall back to trusting the declared param. Returns an error dict when not
    permitted; else None.
    """
    bound = _auth_role()
    if bound is not None:
        from .authz import role_can_review
        if not role_can_review(bound, review_type):
            return {"error": (
                f"Your token's account role '{bound}' cannot approve "
                f"'{review_type}' reviews. This surface is owned by the other role."
            )}
        return None

    allowed = _REVIEW_ROLES[review_type]
    canon = _normalize_role(role)
    if canon is None:
        return {"error": (
            f"A 'role' is required to write a {review_type} review. "
            f"Declare one of: {sorted(allowed)} (you sent {role!r})."
        )}
    if canon not in allowed:
        return {"error": (
            f"Role '{canon}' cannot approve {review_type} reviews — that surface "
            f"requires one of {sorted(allowed)}. Re-call with the appropriate role "
            f"(e.g. switch from Data Engineer to Data Steward)."
        )}
    return None


def _authorized_projects() -> set[str] | None:
    """Project codes the current caller may act on.

    Returns ``None`` (unrestricted) when auth is disabled (no token in context)
    or the token is scoped to all projects; otherwise the set of allowed codes.
    """
    ctx = _auth_ctx.get()
    if ctx is None:
        return None
    return ctx.get("projects")  # None = unrestricted; else the allowed set


def _deny_project(project_code: str) -> dict | None:
    """Return an error dict if serving is blocked or the caller's token isn't
    scoped to project_code. Called at the top of every project-scoped tool."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    allowed = _authorized_projects()
    if allowed is not None and project_code not in allowed:
        return {
            "error": (
                f"Not authorized for project '{project_code}'. Your token is "
                f"scoped to: {sorted(allowed) if allowed else 'no projects'}."
            )
        }
    return None


def _deny_domain(domain: str) -> dict | None:
    """Authorize a DOMAIN-scoped action (semantic discovery spans a domain's
    products). Refuses when serving is blocked, or when the caller's token is
    project-scoped and the domain contains any product outside that scope — a
    domain-wide write could touch concepts evidenced by products the caller
    can't see. An unrestricted token passes."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    allowed = _authorized_projects()
    if allowed is None:
        return None  # unrestricted
    with Session(engine) as session:
        codes = {
            p.project_code
            for p in session.exec(select(Project)).all()
            if (p.domain or "").strip() == (domain or "").strip()
        }
    if not codes:
        return {"error": f"No projects found in domain '{domain}'."}
    outside = codes - allowed
    if outside:
        return {
            "error": (
                f"Not authorized for domain '{domain}': it includes project(s) "
                f"{sorted(outside)} outside your token scope ({sorted(allowed)}). "
                f"Semantic discovery is domain-wide; an unrestricted or "
                f"domain-complete token is required."
            )
        }
    return None


_MCP_TOKENS = _parse_tokens(os.environ.get("WB_MCP_TOKENS", ""))
if _MCP_TOKENS:
    # Auth is enforced by _BearerAuthMiddleware (plain Authorization: Bearer).
    # We deliberately do NOT use FastMCP's AuthSettings/OAuth resource-server
    # mode — it advertises RFC-9728 protected-resource metadata, which makes
    # static-bearer clients (e.g. OpenAI Codex) report "Auth: Unsupported" and
    # import no tools. Plain bearer is what every MCP HTTP client supports.
    _log.info("MCP server: bearer-token auth ENABLED (%d token(s)).", len(_MCP_TOKENS))

# Fail closed: if no tokens are configured, the tools refuse to serve UNLESS the
# operator explicitly opts into open mode (WB_MCP_ALLOW_INSECURE=1, for local
# dev). This prevents a remote/container deploy that simply forgot WB_MCP_TOKENS
# from exposing run_cypher/run_stage to the network with no auth.
_ALLOW_INSECURE = os.environ.get("WB_MCP_ALLOW_INSECURE", "").lower() in ("1", "true", "yes")
_SERVING_BLOCKED = (not _MCP_TOKENS) and not _ALLOW_INSECURE
if not _MCP_TOKENS and _ALLOW_INSECURE:
    _log.warning(
        "MCP server: auth DISABLED + WB_MCP_ALLOW_INSECURE set — /mcp is OPEN. "
        "Local dev only; never do this on a shared/remote deployment."
    )
elif _SERVING_BLOCKED:
    _log.warning(
        "MCP server: no WB_MCP_TOKENS configured — tools will REFUSE requests. "
        "Set WB_MCP_TOKENS (recommended), or WB_MCP_ALLOW_INSECURE=1 for local dev."
    )


def _serving_guard() -> dict | None:
    """Refuse all tool calls when auth is unconfigured and open mode wasn't opted into."""
    if _SERVING_BLOCKED:
        return {
            "error": (
                "Workbench MCP is not accepting requests: no WB_MCP_TOKENS are "
                "configured. Ask the operator to set WB_MCP_TOKENS (or "
                "WB_MCP_ALLOW_INSECURE=1 for local dev)."
            )
        }
    return None

# Serve the Streamable-HTTP endpoint at the app root so mounting at "/mcp" in
# main.py yields a clean "/mcp" (not "/mcp/mcp").
mcp = FastMCP(
    "data-workbench",
    instructions=(
        "Data Workbench control plane (Data Engineer). Use list_projects to discover "
        "projects, get_project_state to inspect pipeline stage statuses, run_cypher for "
        "read-only graph questions (scope every query to the project code), reset_stage "
        "to set a stage back to pending. All tools are scoped to your authorized projects.\n\n"
        "USE ONLY THESE MCP TOOLS — never shell/curl/psql/docker to inspect or drive the "
        "workbench. Everything you need is a tool: status → get_project_state / "
        "get_plan_summary; run history → get_stage_executions; results → get_stage_results / "
        "preview_serving_view. If a stage looks stuck (status 'running' but no progress), "
        "do NOT curl the backend or restart anything — call get_stage_executions, then "
        "reset_stage + run_stage to re-run it. Do NOT rebuild or restart the server.\n\n"
        "DRIVE STAGES IN ORDER via get_plan_summary's recommended_next: `mechanical` → "
        "complete_stage, `llm`/`backend` → run_stage (poll get_project_state; LLM stages "
        "take 1-2 min — that's normal, keep polling, don't assume failure), `review_gate` → "
        "the review_* tools. For a consumer product the mapping stage needs a source_tables "
        "config — fetch it with get_stage_config_options first. Consumer products do NOT run "
        "discovery/profiling/validation (they inherit those from the CONSUMES'd source) — "
        "going straight to odcs_to_dprod → data_mapping → serving → deploy is correct.\n\n"
        "REVIEW CHECKPOINT AFTER EVERY STAGE — do not auto-chain stages. When a stage reaches a "
        "terminal state (complete / awaiting_review), STOP and: (1) call get_stage_output(stage_id) "
        "to get the markdown Step Report of what it produced; (2) show it to the user; (3) ask "
        "'approve & continue, refine, or redo?'. Only proceed to the next stage on the user's OK. "
        "To REFINE/REDO: reset_stage + run_stage to re-run; review_mapping (action='replace_mapping') "
        "to fix a specific mapping; set_dataset_joins / set_dataset_filter to adjust the shape; then "
        "re-run the affected stage. The user is in control between stages — surface the output and let "
        "them decide, don't rush ahead."
    ),
    streamable_http_path="/",
)


# ── Plain bearer-token middleware (no OAuth) ────────────────────────────────
#
# Validates `Authorization: Bearer <token>` against WB_MCP_TOKENS and stashes
# the caller's principal + project scope in `_auth_ctx` for the tools to read.
# On a bad/missing token it returns a 401 with a *plain* `WWW-Authenticate:
# Bearer` challenge — NO `resource_metadata`/OAuth — so any static-bearer MCP
# client (Claude Code, Codex, …) connects. When no tokens are configured it
# passes through; the tool-level `_serving_guard` still fails closed.

async def _send_401(send) -> None:
    body = json.dumps({"error": "invalid_token", "detail": "Bearer token required"}).encode()
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"www-authenticate", b'Bearer error="invalid_token"'),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class _BearerAuthMiddleware:
    """ASGI wrapper enforcing static bearer auth in front of the MCP app."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            # Mounted at "/mcp", so a request to the bare "/mcp" arrives with an
            # empty path and Starlette would 307-redirect to "/mcp/". Static-bearer
            # clients (e.g. Codex) often drop the Authorization header / POST body
            # across that redirect → a spurious 401 → "Auth: Unsupported". Rewrite
            # the empty path to "/" so the endpoint serves directly, no redirect.
            if scope.get("path") in ("", None):
                scope = {**scope, "path": "/", "raw_path": b"/"}
        if scope.get("type") != "http" or not _MCP_TOKENS:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        info = _MCP_TOKENS.get(token)
        if info is None:
            await _send_401(send)
            return
        reset = _auth_ctx.set({
            "principal": info["principal"],
            "projects": info["projects"],
            "role": info.get("role"),
            "read_only": info.get("read_only", False),
        })
        try:
            await self.app(scope, receive, send)
        finally:
            _auth_ctx.reset(reset)


def asgi_app():
    """The MCP ASGI app wrapped with plain bearer auth — mounted at /mcp."""
    return _BearerAuthMiddleware(mcp.streamable_http_app())

# Read-only guard for run_cypher: a fast, friendly pre-check (the real
# enforcement is the READ transaction). Cypher comments: // line and /* block */.
_WRITE_CLAUSE = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|LOAD\s+CSV)\b",
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)
_MAX_ROWS = 200


def _strip_comments(query: str) -> str:
    """Remove Cypher comments so they can't satisfy the project-scope check."""
    return _COMMENT_RE.sub(" ", query)


def _jsonable(v: Any) -> Any:
    """Coerce Neo4j / arbitrary values into JSON-safe primitives."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    try:
        return {k: _jsonable(x) for k, x in dict(v).items()}  # Neo4j Node/Relationship
    except Exception:
        return str(v)


@mcp.tool()
def list_projects() -> dict:
    """List Data Workbench projects the caller's token may access.

    When the token is project-scoped, only the permitted projects are returned;
    an unrestricted token (or auth disabled) sees all. Returns
    ``{"projects": [...], "count": N}``.
    """
    if _SERVING_BLOCKED:  # fail closed: reveal nothing when auth is unconfigured
        return {"projects": [], "count": 0}
    allowed = _authorized_projects()
    with Session(engine) as session:
        rows = session.exec(select(Project)).all()
    projects = [
        {
            "project_code": p.project_code,
            "name": p.name,
            "archetype": p.archetype,
            "domain": p.domain,
            "owner_email": p.owner_email,
            "multi_workflow": p.multi_workflow,
        }
        for p in rows
        if allowed is None or p.project_code in allowed
    ]
    return {"projects": projects, "count": len(projects)}


# Non-LLM, backend-executed stages (DQ test gen/exec) — they run via run_stage's
# subprocess path, NOT the /complete endpoint. Used by both the execution-kind
# hint and complete_stage's guard.
_BACKEND_DRIVEN_STAGE_IDS = {
    "dq_test_execution", "dq_test_generation_gx", "dq_test_generation_python",
}


def _execution_kind(stage_id: str | None, stage_def: dict) -> str:
    """Which MCP tool drives this stage — so a driver picks right on the first try.

    `llm` → run_stage; `backend` → run_stage (server subprocess); `review_gate` →
    review_* tools / UI; `mechanical` → complete_stage; `unknown` if unresolved.
    """
    if not stage_def:
        return "unknown"
    if stage_def.get("requires_llm"):
        return "llm"
    if stage_id in _BACKEND_DRIVEN_STAGE_IDS:
        return "backend"
    if stage_def.get("review_type"):
        return "review_gate"
    return "mechanical"


def _stage_status_list(session, project) -> list[dict]:
    """[{workflow_id, stage_number, stage_id, stage_name, status, execution_kind}] for a
    project — orphan-resolved + workflow-order sorted (same resolution as get_project_state).
    Shared by get_project_state, get_available_actions, and the dependency gates."""
    from .routers.stages import _resolve_orphaned_stages, _resolve_stage_id
    from .archetypes import STAGE_REGISTRY
    from .models import Workflow
    _resolve_orphaned_stages(project, session)
    runs = session.exec(select(StageRun).where(StageRun.project_id == project.id)).all()
    wf_order = {
        w.workflow_id: w.order
        for w in session.exec(select(Workflow).where(Workflow.project_id == project.id)).all()
    }
    out = []
    for r in sorted(runs, key=lambda r: (wf_order.get(r.workflow_id, 10_000), r.stage_number)):
        sid = _resolve_stage_id(project, r.stage_number, r.workflow_id, session)
        sdef = STAGE_REGISTRY.get(sid, {}) if sid else {}
        out.append({
            "workflow_id": r.workflow_id,
            "stage_number": r.stage_number,
            "stage_id": sid,
            "stage_name": r.stage_name,
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "execution_kind": _execution_kind(sid, sdef),
        })
    return out


def _tool_for_kind(kind: str) -> str:
    return {"llm": "run_stage", "backend": "run_stage",
            "review_gate": "review_* tools", "mechanical": "complete_stage"}.get(kind, "run_stage")


def _dependency_gate(session, project, stage_id: str | None) -> dict | None:
    """Return an error dict if stage_id's prerequisites aren't complete, else None.
    Shared by run_stage / complete_stage so you can't run a step out of order."""
    if not stage_id:
        return None
    from .archetypes import check_stage_dependencies
    stages = _stage_status_list(session, project)
    completed = {s["stage_id"] for s in stages if s["status"] in ("complete", "awaiting_review") and s["stage_id"]}  # awaiting_review = work done, review deferred (matches UI)
    present = {s["stage_id"] for s in stages if s["stage_id"]}  # only enforce deps that exist in THIS pipeline
    unmet = check_stage_dependencies(stage_id, completed, present)
    if unmet:
        return {
            "error": f"'{stage_id}' can't run yet — it requires these to be complete first: "
                     f"{', '.join(unmet)}.",
            "blocked_by": unmet,
            "next": "Run the prerequisite(s) first (see get_available_actions), or pass force=true "
                    "to override deliberately.",
        }
    return None


def _required_config_gate(stage_id: str | None, config: dict | None) -> dict | None:
    """Refuse a stage up front when a REQUIRED config field is missing, instead of
    letting the background run stall silently at 'pending' (the data_mapping case:
    it needs `data_product` + `source_tables`; without them the run never
    registered and the stage sat at pending). Returns an actionable error naming
    the missing keys."""
    from .archetypes import STAGE_REGISTRY
    sdef = STAGE_REGISTRY.get(stage_id or "", {})
    cfg = config or {}
    # Only block a required field that has NO fallback: a field declaring a
    # `default` (e.g. serving's dialect → postgres) is applied server-side and
    # won't stall, so don't demand it.
    missing = [
        f.get("key")
        for f in sdef.get("config_fields", [])
        if f.get("required")
        and not cfg.get(f.get("key"))
        and f.get("default") in (None, "")
    ]
    if missing:
        example = "{" + ", ".join('"%s": "…"' % k for k in missing) + "}"
        return {
            "error": (
                f"Stage '{stage_id}' needs required config: {', '.join(missing)}. "
                f"Fetch valid values with get_stage_config_options, then pass them in `config` "
                f"(e.g. config={example})."
            ),
            "missing_config": missing,
            "fix": "get_stage_config_options",
        }
    return None


def _serving_mappings_gate(project, stage_id: str | None) -> dict | None:
    """Block a serving stage while column mappings are still pending_review.

    The serving-view generator only emits DDL from **approved** mappings, so
    running it with pending mappings silently produces partial-or-zero views and
    fails — with the reason buried in the server-side skill log (the user ends up
    pasting logs to find out why). Catch it up front with an actionable message.
    Respects force=true (caller can bypass)."""
    if stage_id not in ("serving_virtual_view", "serving_physical_copy", "serving_lakehouse_export", "serving_transfer"):
        return None
    try:
        from .routers.reviews import PENDING_MAPPINGS_QUERY_S
        from .neo4j_client import neo4j_session
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            pending = len(list(ns.run(PENDING_MAPPINGS_QUERY_S, project_code=project.project_code)))
    except Exception:
        return None  # never block on a preflight query failure
    if pending > 0:
        return {
            "error": (
                f"Serving needs APPROVED mappings — {pending} column mapping(s) are still "
                f"pending_review. The view generator only uses approved mappings, so running "
                f"now would produce partial/zero views and fail. Approve them first with "
                f"bulk_approve_mappings (or reject unwanted ones via review_mapping), then re-run."
            ),
            "blocked_by": f"{pending} pending mapping(s)",
            "fix": "bulk_approve_mappings",
        }
    return None


@mcp.tool()
def get_available_actions(project_code: str) -> dict:
    """Dependency-aware menu of what you can do NOW — the flexible-but-guarded view.

    Returns `runnable` (prerequisites met — run any of these, in any order you like),
    `locked` (prerequisites NOT met — each lists `blocked_by`, so you see what to run
    first), plus `in_progress`, `awaiting_review`, and `done`. This is how a DE picks
    targeted work (e.g. run only enrichment then hand off) WITHOUT running steps whose
    inputs don't exist — the "unlock as prerequisites complete" model. Each runnable
    entry names the `tool` to use.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .archetypes import check_stage_dependencies
    from .config import FRONTEND_URL
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        stages = _stage_status_list(session, project)
        project_id = project.id

    completed = {s["stage_id"] for s in stages if s["status"] in ("complete", "awaiting_review") and s["stage_id"]}  # awaiting_review = work done, review deferred (matches UI)
    present = {s["stage_id"] for s in stages if s["stage_id"]}  # only enforce deps that exist in THIS pipeline
    runnable, locked, in_progress, awaiting, done = [], [], [], [], []
    for s in stages:
        sid, st = s["stage_id"], s["status"]
        base = {
            "stage_id": sid, "stage_name": s["stage_name"], "workflow_id": s["workflow_id"],
            "stage_number": s["stage_number"], "execution_kind": s["execution_kind"],
        }
        if st == "complete":
            done.append(sid)
        elif st == "running":
            in_progress.append(base)
        elif st == "awaiting_review":
            awaiting.append({**base, "tool": "review_* tools"})
        else:  # pending / failed → gate on deps
            unmet = check_stage_dependencies(sid, completed, present) if sid else []
            if unmet:
                locked.append({**base, "blocked_by": unmet})
            else:
                runnable.append({**base, "tool": _tool_for_kind(s["execution_kind"])})

    return {
        "project_code": project_code,
        "runnable": runnable,
        "locked": locked,
        "awaiting_review": awaiting,
        "in_progress": in_progress,
        "done": done,
        "web_url": f"{FRONTEND_URL}/engineer/projects/{project_id}",
        "next": (
            f"{len(runnable)} action(s) available now; {len(locked)} locked (see blocked_by). "
            "Pick any runnable step — you don't have to run the whole workflow — but locked steps "
            "need their prerequisites first."
        ),
    }


@mcp.tool()
def get_project_state(project_code: str) -> dict:
    """Return a project's detail, the Product Owner's intent/brief, and its
    pipeline stage statuses grouped by workflow — plus a deep link to the web UI.

    Each stage carries an `execution_kind` telling you which tool to use:
    `llm`/`backend` → run_stage, `mechanical` → complete_stage, `review_gate` →
    review_* tools / UI. No probe-then-fallback needed.

    State parity with the web UI: this resolves orphaned `running` stages the
    same way `GET /api/projects/{id}/stages` does, so a stage stranded after a
    restart shows the same corrected status here as in the dashboard.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    # Imported lazily to avoid an import cycle (routers.stages → websocket).
    from .routers.stages import _resolve_orphaned_stages, _resolve_stage_id
    from .archetypes import STAGE_REGISTRY
    from .config import FRONTEND_URL

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        # Parity fix: apply the SAME orphan-resolution the UI runs on list, so
        # both surfaces read the corrected status (not a stale `running`).
        _resolve_orphaned_stages(project, session)
        runs = session.exec(
            select(StageRun).where(StageRun.project_id == project.id)
        ).all()
        # Order stages by the workflow's numeric lifecycle `order` (the SAME
        # field the web UI sorts by — routers/projects.py order_by(Workflow.order)),
        # NOT the workflow_id string. Sorting by the string mis-ordered multi-
        # workflow archetypes (e.g. dpe-sa, where "mark_discovery_complete" sorts
        # before "metadata_enrichment"), making recommended_next skip enrichment.
        from .models import Workflow
        wf_order = {
            w.workflow_id: w.order
            for w in session.exec(
                select(Workflow).where(Workflow.project_id == project.id)
            ).all()
        }
        # Resolve stage_id + execution_kind per stage (needs the session for the
        # workflow lookup), so the driver knows which tool each stage wants.
        stages = []
        for r in sorted(runs, key=lambda r: (wf_order.get(r.workflow_id, 10_000), r.stage_number)):
            sid = _resolve_stage_id(project, r.stage_number, r.workflow_id, session)
            sdef = STAGE_REGISTRY.get(sid, {}) if sid else {}
            _st = r.status.value if hasattr(r.status, "value") else str(r.status)
            _stage = {
                "workflow_id": r.workflow_id,
                "stage_number": r.stage_number,
                "stage_id": sid,
                "stage_name": r.stage_name,
                "status": _st,
                "execution_kind": _execution_kind(sid, sdef),
            }
            # Surface WHY a stage failed right here, so a failure is legible from
            # the status view — no need to go log-diving in get_stage_executions.
            if _st == "failed" and getattr(r, "error_message", None):
                _stage["error_message"] = r.error_message
            stages.append(_stage)
        # Latest product request = the PO's "why" (intent lives in SQLite, not
        # the graph). Most-recent by submission, whatever its kind/status.
        latest_req = session.exec(
            select(ProductRequest)
            .where(ProductRequest.project_id == project.id)
            .order_by(ProductRequest.submitted_at.desc())
        ).first()

    def _enum(v: Any) -> Any:
        return v.value if hasattr(v, "value") else v

    brief: dict[str, Any] = {
        "product_idea": project.product_idea,
        "owner_email": project.owner_email,
        "owner_name": project.owner_name,
    }
    if latest_req is not None:
        brief["latest_request"] = {
            "kind": _enum(latest_req.kind),
            "status": _enum(latest_req.status),
            "submitted_by": latest_req.submitted_by,
            "submitted_at": (
                latest_req.submitted_at.isoformat() if latest_req.submitted_at else None
            ),
            "notes": latest_req.notes,
            "gap_reason": latest_req.gap_reason,
            "gap_column_uri": latest_req.gap_column_uri,
        }

    return {
        "project_code": project.project_code,
        "name": project.name,
        "archetype": project.archetype,
        "domain": project.domain,
        "discovery_complete_at": (
            project.discovery_complete_at.isoformat()
            if project.discovery_complete_at
            else None
        ),
        "brief": brief,
        "web_url": f"{FRONTEND_URL}/engineer/projects/{project.id}",
        "stages": stages,
    }


_KIND_TOOL = {"llm": "run_stage", "backend": "run_stage", "mechanical": "complete_stage", "review_gate": "review_* / web UI"}


@mcp.tool()
def get_plan_summary(project_code: str) -> dict:
    """A compact, decision-oriented digest of a project's plan — built for a
    quick "where are we / what's next" status update.

    Returns counts + percent_complete, the lists of completed / running /
    awaiting_review / blocked (failed) steps, and the single `recommended_next`
    step (with the tool to run it). Stages are de-duplicated by `stage_id` in
    lifecycle order — same view as the web board's recommended-next. Prefer this
    over re-deriving status from the full `get_project_state` stage list.
    """
    state = get_project_state(project_code)
    if "error" in state:
        return state

    seen: set[str] = set()
    plan: list[dict] = []
    for s in state.get("stages", []):
        sid = s.get("stage_id") or f"#{s.get('stage_number')}"
        if sid in seen:
            continue  # de-dupe repeated stage_ids across workflows (plan view)
        seen.add(sid)
        plan.append(s)

    def _names(status: str) -> list[str]:
        return [s.get("stage_name") or s.get("stage_id") for s in plan if s.get("status") == status]

    completed = _names("complete")
    running = _names("running")
    awaiting = _names("awaiting_review")
    blocked = _names("failed")

    # Surface a failed virtual-view DEPLOYMENT in the digest. The deploy stage's
    # own status stays pending until a deploy succeeds (complete_stage owns that
    # lifecycle), so without this a failed deploy is invisible here even though
    # :ServingDefinition.deploymentStatus == 'failed'. Best-effort, read-only.
    try:
        from .routers.summary import _run_query, SERVING_QUERY_S
        with Session(engine) as _s:
            _proj = _s.exec(select(Project).where(Project.project_code == project_code)).first()
        if _proj is not None:
            for r in _run_query(_proj, SERVING_QUERY_S, project_code=project_code):
                if r.get("serving_mode") == "virtual_view" and r.get("deployment_status") == "failed":
                    msg = "Deploy Virtual View — last deployment failed"
                    if r.get("deployment_error"):
                        msg += f": {r['deployment_error']}"
                    if msg not in blocked:
                        blocked = blocked + [msg]
                    break
    except Exception:
        pass

    recommended_next = None
    for s in plan:
        if s.get("status") in ("pending", "failed"):
            recommended_next = {
                "stage_id": s.get("stage_id"),
                "stage_name": s.get("stage_name"),
                "stage_number": s.get("stage_number"),
                "workflow_id": s.get("workflow_id"),
                "execution_kind": s.get("execution_kind"),
                "tool": _KIND_TOOL.get(s.get("execution_kind"), "run_stage"),
            }
            break

    # Acceptance gate: engineering work shouldn't begin until the incoming
    # request is accepted (submitted→accepted). Recommending the next stage
    # while it's still 'submitted' is misleading — surface the accept action as
    # the real next move. UNIVERSAL across archetypes: for dpe-sa it also unlocks
    # the PO source-validation gate; for dpe-cf (and any product request) it
    # gates the engineer's first mutation. Mirrors request_guard's rule.
    needs_accept = (
        ((state.get("brief") or {}).get("latest_request") or {}).get("status") == "submitted"
    )
    if needs_accept:
        is_sa = state.get("archetype") == "dpe-sa"
        recommended_next = {
            "stage_id": None,
            "stage_name": "Accept request",
            "action": "accept_request",
            "tool": "accept_request",
            "reason": (
                "Accept the incoming request to unlock Product Owner source validation."
                if is_sa else
                "Accept the incoming request before starting engineering work."
            ),
        }
        blocked = blocked + [
            "Accept request — request not yet accepted; "
            + ("blocks PO source validation" if is_sa else "blocks engineering mutations")
        ]

    # Advisories — non-blocking "you probably want to handle this" notes. Today:
    # pre-serving reconciliation — sensitive source columns mapping into the
    # product WITHOUT a protective transform (mask/hash/suppress). Catches the
    # already-approved case before a deployed view leaks raw PII.
    advisories: list[str] = []
    try:
        from .routers.reviews import reconcile_unprotected
        with Session(engine) as _s:
            _proj = _s.exec(select(Project).where(Project.project_code == project_code)).first()
        if _proj is not None:
            _unmet = reconcile_unprotected(_proj)
            if _unmet:
                _cols = ", ".join(sorted({u["product_col_name"] for u in _unmet if u.get("product_col_name")})[:5])
                advisories.append(
                    f"{len(_unmet)} sensitive column(s) map into the product unprotected "
                    f"({_cols}) — apply the recommended mask/hash before serving."
                )
    except Exception:
        pass

    # Pre-serving advisory: dataset filters the PO declared but that haven't
    # been compiled to SQL yet. The serving stage auto-compiles them; this just
    # gives the MCP engineer a heads-up (no wizard banner on the MCP path).
    try:
        from .routers.reviews import check_unfinalized_filters
        with Session(engine) as _s:
            _proj = _s.exec(select(Project).where(Project.project_code == project_code)).first()
        if _proj is not None:
            _unfin = check_unfinalized_filters(_proj)
            if _unfin:
                _ds = ", ".join(sorted({u["schema_label"] for u in _unfin if u.get("schema_label")})[:5])
                advisories.append(
                    f"{len(_unfin)} dataset filter(s) declared but not yet compiled to SQL "
                    f"({_ds}) — they're auto-compiled when you run the serving stage; if any "
                    f"can't be grounded, finalize it in Filter Review (set_dataset_filter)."
                )
    except Exception:
        pass

    # dpe-cf: advisory to approve pending mappings and check join-preflight before serving
    if state.get("archetype") == "dpe-cf":
        _mapping_stage = next((s for s in plan if s.get("stage_id") == "data_mapping"), None)
        _serving_stage = next((s for s in plan if s.get("stage_id") == "serving_virtual_view"), None)
        if _mapping_stage and _mapping_stage.get("status") == "awaiting_review":
            advisories.append(
                "data_mapping has pending mappings awaiting review — call "
                "bulk_approve_mappings (role='Data Engineer') to approve all at once "
                "instead of one by one."
            )
        if (
            _mapping_stage and _mapping_stage.get("status") == "complete"
            and _serving_stage and _serving_stage.get("status") in ("pending", "failed")
        ):
            advisories.append(
                "Before running serving_virtual_view, call get_join_preflight to check "
                "whether cross-source-product joins need to be declared via "
                "set_dataset_joins — skipping this risks a no-FK-path error at "
                "serving time."
            )

    total = len(plan)
    done = len(completed)
    return {
        "project_code": project_code,
        "name": state.get("name"),
        "archetype": state.get("archetype"),
        "total_steps": total,
        "completed_steps": done,
        "percent_complete": round(100 * done / total) if total else 0,
        "completed": completed,
        "running": running,
        "awaiting_review": awaiting,
        "blocked": blocked,
        "advisories": advisories,
        "recommended_next": recommended_next,  # null when nothing is pending/failed
        "web_url": state.get("web_url"),
    }


@mcp.tool()
def get_stage_results(
    project_code: str,
    card: str,
    table: str | None = None,
    column: str | None = None,
    severity: str | None = None,
    source: str | None = None,
) -> dict:
    """Read a pipeline stage's structured output WITHOUT writing Cypher.

    This is the first thing to reach for when asked to "review the <X> results"
    of a project — it runs the SAME vetted, single-dataset-scopable query the web
    dashboard uses (the direct :HAS_COLUMN edge), so there is no schema guessing
    and no variable-length cross-join risk. Every row carries its review `status`
    (pending_review / approved / rejected) where applicable.

    `card` selects the output. Map the common questions to a card:
      * metadata enrichment review → "descriptions" (column descriptions + status);
        add "datasets" for per-table descriptions + relationshipKind
      * final/approved descriptions   → "final_descriptions"
      * data profiling                → "profiling"
      * column mappings (lineage)     → "mappings"
      * DQ rules                      → "dq_rules" (filter by `severity` / `source`)
      * discovered schema             → "datasets" / "columns"
      * allowed values                → "allowed_values"
      * consumer-aligned inputs       → "inputs";  serving views → "serving"
      * data product columns          → "data_products"
    Full list is returned in the error if `card` is unknown.

    Pass `table` (and optionally `column`) to scope to a SINGLE dataset — the
    cleanest way to answer "review the enrichment for the <table> table" with no
    cross-join ambiguity. Read-only.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    # Lazy import: routers.summary pulls in a lot; keep module import light.
    from .routers.summary import detail_for_project

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
    if project is None:
        return {"error": f"No project with code '{project_code}'"}
    try:
        result = detail_for_project(
            project, card, table=table, column=column, severity=severity, source=source
        )
    except ValueError as e:  # unknown card — message enumerates valid cards
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001 — surface a clean message
        return {"error": f"Query failed: {e}"}
    # Drop the echo-Cypher from the payload; the agent wants the data, not SQL.
    return {"card": result["card"], "count": result["count"], "rows": result["rows"]}


@mcp.tool()
def run_cypher(project_code: str, query: str) -> dict:
    """Run a READ-ONLY, project-scoped Cypher query against a project's Neo4j graph.

    Guardrails enforced here (the server is the trust boundary):
      * the caller's token must be authorized for the project
      * read-only is enforced by running inside a Neo4j READ transaction — any
        write (incl. via CALL procedures) is rejected by the database, not by a
        keyword regex (the regex below is only a fast, friendlier pre-check)
      * the query (with comments stripped) MUST reference ``project_code``
      * results are capped at 200 rows

    NOTE on isolation: this is project-scoping by *convention* on a single shared
    Neo4j database — it stops the easy cross-project mistakes/bypasses but is not
    a hard tenancy boundary. True multi-tenant isolation requires a Neo4j
    database (or RBAC role) per project; tracked as a follow-up.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    if _WRITE_CLAUSE.search(query):
        return {
            "error": (
                "Only read-only queries are allowed via MCP. Remove write clauses "
                "(CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH/LOAD CSV)."
            )
        }
    # Strip comments before the scope check so a `// <code>` comment can't satisfy
    # it while the executable query targets a different project.
    if project_code not in _strip_comments(query):
        return {
            "error": (
                f"Query must reference the project code '{project_code}' for "
                f"cross-project isolation — scope your MATCH to "
                f"(:Project {{projectCode: '{project_code}'}}) or include the code "
                f"in a node URI prefix."
            )
        }
    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
    if project is None:
        return {"error": f"No project with code '{project_code}'"}

    # Imported lazily so importing this module never forces a Neo4j driver import.
    from .neo4j_client import neo4j_session

    def _read(tx) -> list[dict]:
        result = tx.run(query)
        rows: list[dict] = []
        for rec in result:
            if len(rows) >= _MAX_ROWS:
                break
            rows.append({k: _jsonable(v) for k, v in rec.data().items()})
        return rows

    try:
        with neo4j_session(
            project.neo4j_host,
            project.neo4j_port,
            project.neo4j_user,
            project.neo4j_password,
            project.neo4j_database,
        ) as s:
            # READ transaction = the database rejects any write the query attempts.
            rows = s.execute_read(_read)
        return {"rows": rows, "row_count": len(rows), "truncated": len(rows) >= _MAX_ROWS}
    except Exception as e:  # noqa: BLE001 — surface a clean message to the client
        return {"error": f"Query failed (read-only transaction; writes are rejected): {e}"}


# ── Mutating tools (authz-gated) ───────────────────────────────────────────
# First mutating tool: a low-stakes, reversible state transition that reuses
# the backend's own reset handler. Higher-stakes mutations (run_stage with LLM
# execution, review approvals, deploy) are deliberate follow-ups — run_stage in
# particular needs the stage runner decoupled from the WebSocket first.
@mcp.tool()
def reset_stage(
    project_code: str, stage_number: int, workflow_id: str | None = None
) -> dict:
    """Reset a pipeline stage back to 'pending' so it can be re-run.

    Mutating: requires the caller's token to be authorized for the project.
    Reuses the backend reset handler — identical effect to the UI's Reset.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied

    from fastapi import HTTPException

    from .routers.stages import reset_stage as _reset_handler

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            _reset_handler(
                project_id=project.id,
                stage_number=stage_number,
                workflow_id=workflow_id,
                session=session,
            )
        except HTTPException as e:
            return {"error": f"{e.status_code}: {e.detail}"}
    return {
        "ok": True,
        "project_code": project_code,
        "stage_number": stage_number,
        "status": "reset",
    }


@mcp.tool()
def set_data_source(
    project_code: str,
    host: str,
    database: str,
    username: str,
    password: str,
    port: int = 5432,
    schema_name: str | None = None,
    platform: str = "postgres",
) -> dict:
    """Configure the project's SOURCE database connection — the MCP equivalent of
    the web UI's "Select Data Source".

    REQUIRED before Data Discovery: discovery enumerates schemas/tables from this
    connection, and get_stage_config_options returns `needs_data_source` until it
    is set. Also marks the `select_data_source` stage complete. Postgres only
    today. From inside the workbench container, a source DB running on the host is
    reachable as host `host.docker.internal` (not `localhost`). After setting,
    call get_stage_config_options for the discovery stage — it now lists the
    schemas/tables, or reports clearly if the connection is unreachable.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from datetime import datetime, timezone
    from .routers.projects import set_data_source as _set, DataSourceInput
    from .routers.stages import _resolve_stage_id
    from .models import StageStatus

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            saved = _set(
                project.id,
                DataSourceInput(
                    platform=platform, host=host, port=port, database=database,
                    username=username, password=password, schema_name=schema_name,
                ),
                session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        # Mirror the UI: setting the source also completes the select_data_source
        # stage (a mechanical gesture with no other side-effect).
        completed = None
        for r in session.exec(select(StageRun).where(StageRun.project_id == project.id)).all():
            if _resolve_stage_id(project, r.stage_number, r.workflow_id, session) == "select_data_source" \
                    and r.status != StageStatus.complete:
                r.status = StageStatus.complete
                r.completed_at = datetime.now(timezone.utc)
                if not r.started_at:
                    r.started_at = datetime.now(timezone.utc)
                session.add(r)
                completed = r.stage_number
        session.commit()
    return {
        "ok": True, **(saved or {}),
        "select_data_source_completed": completed,
        "next": "Call get_stage_config_options for the Data Discovery stage to list schemas/tables.",
    }


@mcp.tool()
def set_serving_mode(
    project_code: str,
    mode: str,
    workflow_id: str | None = None,
) -> dict:
    """Choose how the product is served: 'virtual' (a SQL view), 'materialized'
    (physical tables built by dbt), 'lakehouse' (Parquet files + a DuckDB catalog),
    or 'transfer' (cross-platform Extract+Load into a different target platform)
    — the MCP equivalent of the serving-mode toggle on the serving stage card.

    Swaps the active member of the serving exclusive_group, so the pipeline then
    shows the matching serving stage (and, for virtual, the separate Deploy stage;
    materialized builds + deploys in one). When workflow_id is omitted, the
    workflow that owns the serving group is auto-detected. After switching, run
    get_plan_summary, then complete_stage the serving stage (materialized) or
    run_stage + complete_stage the deploy (virtual).
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    mode_norm = (mode or "").strip().lower()
    target = {
        "virtual": "serving_virtual_view",
        "materialized": "serving_physical_copy",
        "lakehouse": "serving_lakehouse_export",
        "transfer": "serving_transfer",
    }.get(mode_norm)
    if not target:
        return {"error": "mode must be 'virtual', 'materialized', 'lakehouse', or 'transfer'"}
    from fastapi import HTTPException
    import json as _json
    from .routers.projects import switch_exclusive_group, SwitchExclusiveGroupRequest
    from .models import Workflow as _Workflow

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        wf_id = workflow_id
        if wf_id is None:
            for wf in session.exec(select(_Workflow).where(_Workflow.project_id == project.id)).all():
                try:
                    steps = _json.loads(wf.workflow_json or "[]")
                except Exception:
                    steps = []
                if any(s.get("exclusive_group") == "serving" for s in steps):
                    wf_id = wf.workflow_id
                    break
        if wf_id is None:
            return {"error": "No workflow exposes a serving choice on this project (materialization not available here)."}
        try:
            switch_exclusive_group(
                project.id, wf_id,
                SwitchExclusiveGroupRequest(group="serving", select_stage_id=target),
                session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
    return {
        "ok": True, "mode": mode_norm, "workflow_id": wf_id,
        "next": "Call get_plan_summary to see the updated serving stage, then complete_stage it (materialized) or deploy it (virtual).",
    }


@mcp.tool()
def get_serving_advice(project_code: str) -> dict:
    """Return the capability-aware serving recommendation for a product.

    Feasibility-gates the full serving-pattern taxonomy (native_virtual,
    native_materialized, lakehouse_file, and the roadmap transfer_then_transform /
    federated / warehouse_native_load) against the product's source × target
    platforms, recommends one feasible pattern (+ transform placement) with a
    rationale, and explains each alternative. Advisory only. Mirrors the web UI's
    Configure Serving advisor.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    import asyncio as _asyncio
    from .routers.serving_strategy import (
        _advise_core, _gather_from_graph, _resolve_source_platform, _resolve_target_platform,
        _norm_platform,
    )
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            datasets = _gather_from_graph(project)
        except Exception:
            datasets = []
        source_platform = _resolve_source_platform(project, session)
        target_platform = _resolve_target_platform(project, session, source_platform)
        cross_platform = _norm_platform(source_platform) != _norm_platform(target_platform)
        result = _asyncio.run(_advise_core(
            project.archetype or "", datasets, cross_platform,
            source_platform=source_platform, target_platform=target_platform,
        ))
    return {
        "recommended_pattern": result.get("recommended_pattern"),
        "recommended_placement": result.get("recommended_placement"),
        "recommended_mode": result.get("recommended_mode"),
        "source_platform": result.get("source_platform"),
        "target_platform": result.get("target_platform"),
        "patterns": result.get("patterns", []),
        "rationale": result.get("rationale"),
    }


@mcp.tool()
def get_placement_advice(project_code: str, placement: str | None = None) -> dict:
    """Return the per-op transform-placement recommendation for a cross-platform product.

    Analyzes the product's mappings + dataset transforms and assigns each transform
    op to the extract side (source, ETL) or target side (after load, ELT), with a
    recommended placement (transform_on_extract / hybrid / transfer_then_transform),
    governance-forced masking, and drivers. Optionally recompute the split for a
    specific `placement`. Mirrors the web UI's Configure Transform Placement dialog.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    import asyncio as _asyncio
    from .routers.transform_placement_advisor import _advise_core, _gather_ops_from_graph
    from .routers.serving_strategy import _resolve_source_platform, _resolve_target_platform
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            ops = _gather_ops_from_graph(project)
        except Exception:
            ops = []
        source_platform = _resolve_source_platform(project, session)
        target_platform = _resolve_target_platform(project, session, source_platform)
        return _asyncio.run(_advise_core(
            ops, source_platform=source_platform, target_platform=target_platform,
            requested_placement=placement, skip_skill=True,
        ))


@mcp.tool()
def set_transform_placement(project_code: str, placement: str) -> dict:
    """Persist the chosen transform placement for a cross-platform product.

    `placement` ∈ {transform_on_extract, hybrid, transfer_then_transform}. Recomputes
    the deterministic per-op decision from the current graph state and stores it on
    the product's transfer serving definition; the transfer pipeline consumes it at
    build time. MCP equivalent of confirming the Configure Transform Placement dialog.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .routers.transform_placement_advisor import _gather_ops_from_graph, _heuristic_placement, _recommend_placement
    from .platform.transform_placement import _VALID_PLACEMENTS
    from . import transform_placement_store
    if placement not in _VALID_PLACEMENTS:
        return {"error": f"placement must be one of {list(_VALID_PLACEMENTS)}"}
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        ops = _gather_ops_from_graph(project)
        decision = _heuristic_placement(ops, placement)
        decision["recommended_placement"] = _recommend_placement(ops)
        stored = transform_placement_store.save_placement_decision(
            project, placement=placement, decision=decision,
            decided_by=_principal(),
        )
    return {"placement": stored, "decision": decision}


_DATASET_FILTERS_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_SCHEMA]->(s:DataContractSchema)
OPTIONAL MATCH (s)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
RETURN s.physicalName AS schema_name, s.name AS schema_label,
       coalesce(dt.filterPredicate, '') AS predicate,
       coalesce(dt.filterIntent, '') AS intent
ORDER BY s.physicalName
"""


@mcp.tool()
def get_dataset_filter(project_code: str) -> dict:
    """Read the dataset-level row filter(s) on a product — the MCP equivalent of
    the engineer's Filter Review panel on the serving card.

    For each output dataset returns the PO's plain-language intent (`filter_intent`),
    the compiled SQL `predicate` the view will use, an `output_dataset_uri` to pass
    back to set_dataset_filter, and a `valid` flag + `message` (the same prose/SQL
    safety check run at deploy). A predicate that reads like plain language shows
    valid=false — finalize it with set_dataset_filter before serving.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .sql_executor import validate_predicate
    contract_id = f"{project_code}-contract"
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        from .neo4j_client import neo4j_session as _ns
        try:
            with _ns(project.neo4j_host, project.neo4j_port, project.neo4j_user,
                     project.neo4j_password, project.neo4j_database) as ns:
                rows = [dict(r) for r in ns.run(_DATASET_FILTERS_QUERY, contract_id=contract_id)]
        except Exception as e:
            return {"error": f"Graph read failed: {e}"}

    datasets = []
    for r in rows:
        pred = (r.get("predicate") or "").strip()
        vr = validate_predicate(pred)  # parse-only (no pg here) — flags prose
        datasets.append({
            "output_dataset_uri": f"dprod:ds:{contract_id}:{r['schema_name']}",
            "schema_label": r.get("schema_label") or r.get("schema_name"),
            "filter_intent": (r.get("intent") or "").strip(),
            "filter": pred,
            "valid": vr.ok,
            "message": None if vr.ok else vr.message,
        })
    return {"project_code": project_code, "count": len(datasets), "datasets": datasets}


@mcp.tool()
def set_dataset_filter(
    project_code: str,
    output_dataset_uri: str,
    filter: str,
) -> dict:
    """Finalize the dataset-level row filter (engineer-owned SQL) — the MCP
    equivalent of saving in the Filter Review panel.

    `filter` is a SQL boolean expression (no WHERE keyword), e.g.
    "employment_status = 'active'". Runs the SAME safety gate as the UI: a
    plain-language / malformed predicate is REFUSED with a clear message and
    nothing is written. Get the output_dataset_uri from get_dataset_filter.
    After a successful set, re-run the serving stage so the view picks it up.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.dataset_transform import update_dataset_transform_filter, FilterUpdate
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = update_dataset_transform_filter(
                project.id, FilterUpdate(output_dataset_uri=output_dataset_uri, filter=filter), session,
            )
        except HTTPException as e:
            detail = e.detail
            if isinstance(detail, dict):
                return {"error": detail.get("message") or "Filter rejected",
                        "error_class": detail.get("error_class"), "predicate": detail.get("predicate")}
            return {"error": f"{detail}"}
    return {
        "ok": True, "output_dataset_uri": output_dataset_uri, "filter": filter,
        "next": "Re-run the serving stage so the view picks up the finalized filter.",
    }


@mcp.tool()
def accept_request(
    project_code: str,
    engineer: str = "Data Engineer",
    request_id: int | None = None,
) -> dict:
    """Accept a product request (submitted -> accepted) — the MCP equivalent of
    the engineer's "Accept" on the Incoming queue.

    UNIVERSAL: accept (or reject) the incoming request BEFORE any engineering
    mutation — running/completing stages, setting the data source, changing
    serving mode, or submitting reviews. Applies to BOTH source-aligned (dpe-sa)
    and consumer-aligned (dpe-cf) products. For dpe-sa it additionally unlocks
    the PO's source-validation gate.

    By default accepts the project's latest non-rejected request; pass
    ``request_id`` to target a specific one (safer when a project has lifecycle
    history). Idempotent — a no-op success if the request is already accepted.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.product_requests import accept_product_request, AcceptRequestBody
    from .models import ProductRequest, ProductRequestStatus

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        if request_id is not None:
            latest = session.get(ProductRequest, request_id)
            if latest is None or latest.project_id != project.id:
                return {"error": f"Request {request_id} not found for project '{project_code}'."}
        else:
            latest = session.exec(
                select(ProductRequest)
                .where(ProductRequest.project_id == project.id)
                .where(ProductRequest.status != ProductRequestStatus.rejected)
                .order_by(ProductRequest.submitted_at.desc())
            ).first()
        if latest is None:
            return {"error": "No product request to accept for this project."}
        if latest.status != ProductRequestStatus.submitted:
            return {"ok": True, "status": latest.status.value, "request_id": latest.id,
                    "note": "already past 'submitted' — no action taken."}
        try:
            accept_product_request(latest.id, AcceptRequestBody(engineer=engineer), session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
    return {
        "ok": True, "status": "accepted", "request_id": latest.id,
        "next": "Source-aligned: the PO can now validate. Otherwise continue the pipeline.",
    }


@mcp.tool()
def list_assignments(
    project_code: str | None = None,
    status: str | None = None,
) -> dict:
    """List product requests assigned to engineering — the MCP equivalent of the
    Incoming queue. Start every project session here: if an assignment is
    'submitted', accept_request (or reject_request) it before any mutation.

    Filters: ``project_code`` to one project, ``status`` to one of
    submitted/accepted/complete/rejected. Results are restricted to the projects
    the caller's token is scoped to.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    if project_code is not None and (denied := _deny_project(project_code)) is not None:
        return denied
    from .models import ProductRequest, ProductRequestStatus
    from .routers.product_requests import _request_payload

    status_enum = None
    if status:
        try:
            status_enum = ProductRequestStatus(status)
        except ValueError:
            return {"error": f"Invalid status '{status}'. Use one of: "
                             f"{[s.value for s in ProductRequestStatus]}."}

    allowed = _authorized_projects()
    with Session(engine) as session:
        stmt = select(ProductRequest).order_by(ProductRequest.submitted_at.desc())
        if status_enum is not None:
            stmt = stmt.where(ProductRequest.status == status_enum)
        rows = session.exec(stmt).all()
        out: list[dict] = []
        cache: dict[int, Project] = {}
        for r in rows:
            project = cache.get(r.project_id) or session.get(Project, r.project_id)
            if project is None:
                continue
            cache[r.project_id] = project
            if project_code is not None and project.project_code != project_code:
                continue
            if allowed is not None and project.project_code not in allowed:
                continue
            out.append(_request_payload(r, project))
    return {"assignments": out, "count": len(out)}


@mcp.tool()
def get_assignment(request_id: int) -> dict:
    """Fetch one product request (assignment) by id, with its project context —
    status, kind, who submitted it, and any engineer→PO gap context. Authorized
    against the caller's token scope."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .models import ProductRequest
    from .routers.product_requests import _request_payload

    with Session(engine) as session:
        request = session.get(ProductRequest, request_id)
        if request is None:
            return {"error": f"Request {request_id} not found."}
        project = session.get(Project, request.project_id)
        if project is None:
            return {"error": f"Project for request {request_id} not found."}
        if (denied := _deny_project(project.project_code)) is not None:
            return denied
        payload = _request_payload(request, project)
        payload["gap_column_uri"] = request.gap_column_uri
        payload["gap_reason"] = request.gap_reason
    return payload


@mcp.tool()
def reject_request(
    request_id: int,
    category: str,
    reason: str = "",
    engineer: str = "Data Engineer",
) -> dict:
    """Reject a product request back to the PO with a structured category +
    reason — the MCP equivalent of the engineer's Reject. Like accept, this is a
    valid pre-acceptance decision (it's never blocked by the acceptance gate).
    Call list_rejection_categories for valid ``category`` values."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .models import ProductRequest
    from .routers.product_requests import reject_product_request, RejectRequestBody

    with Session(engine) as session:
        request = session.get(ProductRequest, request_id)
        if request is None:
            return {"error": f"Request {request_id} not found."}
        project = session.get(Project, request.project_id)
        if project is None:
            return {"error": f"Project for request {request_id} not found."}
        if (denied := _deny_project(project.project_code)) is not None:
            return denied
        try:
            result = reject_product_request(
                request_id,
                RejectRequestBody(reason=reason, category=category, engineer=engineer),
                session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
    return {"ok": True, "status": "rejected", "request_id": request_id, "request": result}


@mcp.tool()
def list_rejection_categories() -> dict:
    """The structured rejection categories an engineer can cite when rejecting a
    request (reject_request's ``category``). Backend is the source of truth."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .routers.product_requests import REJECTION_CATEGORIES
    return {"categories": [{"value": k, "label": v} for k, v in REJECTION_CATEGORIES.items()]}


# ── P1: UI/MCP parity tools ───────────────────────────────────────────────────
# Each wraps an existing REST handler (no new business logic). Mutating tools
# resolve the project, run _deny_project auth, and let the wrapped handler apply
# the shared acceptance gate (request_guard) so MCP and the web UI behave the
# same. The HTTPException → error-dict translation mirrors the older tools.


def _resolve_project(session, project_code: str):
    """project_code → Project row (or None). Shared by the P1 wrappers."""
    return session.exec(select(Project).where(Project.project_code == project_code)).first()


def _err(detail: Any) -> dict:
    """Normalize a FastAPI HTTPException detail (str or dict) into an error dict."""
    if isinstance(detail, dict):
        return {"error": detail.get("message") or "Request refused", **detail}
    return {"error": str(detail)}


# --- Workflow management ------------------------------------------------------

@mcp.tool()
def list_available_workflows(project_code: str) -> dict:
    """List the project's current workflows AND the catalog of workflows still
    addable to it — the MCP equivalent of the "+ Add Workflow" picker. Use the
    returned `workflow_id`s with add_workflow / remove_workflow.

    Each catalog entry includes a `category` field (e.g. "Data Quality", "Serving",
    "Discovery") to help filter relevant options. To add DQ testing to any `dd` or
    `dmig` project, look for `workflow_id` values `baseline_dq_rules` and
    `dq_testing` — add them in order (or let add_workflow auto-add the prerequisite).
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.projects import list_workflows, list_catalog
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            current = list_workflows(project.id, session)
            catalog = list_catalog(project.id, session)
        except HTTPException as e:
            return _err(e.detail)
    return {"project_code": project_code, "current": current, "catalog": catalog}


@mcp.tool()
def add_workflow(project_code: str, workflow_id: str) -> dict:
    """Add a workflow from the catalog to the project — MCP equivalent of
    "+ Add Workflow". Get valid `workflow_id`s from list_available_workflows.

    Smart dependency handling: adding `workflow_id="dq_testing"` automatically
    also adds `baseline_dq_rules` as a prerequisite if it is not already present,
    returning `{"status": "added", "workflows": [...]}` with both entries. A single
    `add_workflow(project_code, "dq_testing")` is therefore sufficient to bootstrap
    the full DQ add-on flow (rules → framework-specific test generation → execution
    → failure analysis → optional scoring).
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.projects import add_workflow as _add, AddWorkflowRequest
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _add(project.id, AddWorkflowRequest(workflow_id=workflow_id), session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def remove_workflow(project_code: str, workflow_id: str) -> dict:
    """Remove a workflow from the project (refused if it has started/completed
    stages). MCP equivalent of the workflow card's Remove."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.projects import remove_workflow as _remove
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _remove(project.id, workflow_id, session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def select_exclusive_group(project_code: str, workflow_id: str, group: str, select_stage_id: str) -> dict:
    """Pick the active member of an exclusive stage group (e.g. group="serving",
    select_stage_id="serving_physical_copy"). Generalizes set_serving_mode to any
    exclusive group. Re-keys StageRun rows so completed stages keep results."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.projects import switch_exclusive_group, SwitchExclusiveGroupRequest
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return switch_exclusive_group(
                project.id, workflow_id,
                SwitchExclusiveGroupRequest(group=group, select_stage_id=select_stage_id),
                session,
            )
        except HTTPException as e:
            return _err(e.detail)


# --- Dataset transform / joins ------------------------------------------------

@mcp.tool()
def get_dataset_transform(project_code: str, output_dataset_uri: str) -> dict:
    """Read a dataset's :DatasetTransform (joins, filter, dedupe, grouping keys,
    SCD policy, suppressed columns, grain prose) — the same shape view-DDL sees.
    Get the output_dataset_uri from get_dataset_filter or the serving card."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.dataset_transform import get_dataset_transform as _get
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _get(project.id, output_dataset_uri, session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_join_preflight(project_code: str) -> dict:
    """Check each output dataset's mapped base tables form one FK-connected
    component. Catches the cross-source-product gap BEFORE serving fails with a
    no-FK-path error; each gap carries a `recommended_joins` payload shaped for
    set_dataset_joins. Read-only."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.serving import join_preflight_check
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return join_preflight_check(project.id, session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_transform_preflight(project_code: str) -> dict:
    """Capability preflight for the product's transforms against the resolved
    serving platform. Compiles every mapping expression through the shared
    parse→validate→render→re-validate pipeline and returns the aggregated
    CompileResult — `errors[]` (each with `product_col` + `remediation`),
    `warnings[]`, `used_capabilities[]`, and the served `platform`. Surfaces
    AGE-style "unsupported on <platform>" findings at author time, not deploy.
    `validated=false` means validation was skipped — either a fallback dialect
    with no capability profile (`ansi`/`duckdb`) or a profiled platform with
    nothing compilable to check yet; `platform_has_profile` distinguishes them.
    Read-only; diagnostics only (no blocking)."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.serving import transform_preflight_check
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return transform_preflight_check(project.id, session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def set_dataset_joins(project_code: str, output_dataset_uri: str, joins: list[dict]) -> dict:
    """Author an explicit FROM/JOIN graph on a dataset (overrides FK auto-
    discovery). Each join: {alias, dataset_uri, kind, predicate}. Apply a
    get_join_preflight `recommended_joins` payload directly. Re-run serving after."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.dataset_transform import update_dataset_transform_joins, JoinsUpdate
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return update_dataset_transform_joins(
                project.id, JoinsUpdate(output_dataset_uri=output_dataset_uri, joins=joins), session,
            )
        except HTTPException as e:
            return _err(e.detail)


# --- Source candidates / upstream collaboration -------------------------------

@mcp.tool()
def request_source_candidates(
    project_code: str,
    engineer: str = "Data Engineer",
    notes: str | None = None,
    gap_column_uri: str | None = None,
    gap_reason: str | None = None,
) -> dict:
    """Ask the PO to identify/create source-aligned products for a consumer
    product that has nothing to map to — MCP equivalent of "Request from PO".
    Optionally pin the specific column gap (gap_column_uri + gap_reason)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.edits import submit_source_candidates_needed, SourceCandidatesNeededInput
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return submit_source_candidates_needed(
                project.id,
                SourceCandidatesNeededInput(
                    engineer=engineer, notes=notes,
                    gap_column_uri=gap_column_uri, gap_reason=gap_reason,
                ),
                session,
            )
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_upstream_drift(project_code: str) -> dict:
    """Drift summary for every source product this consumer CONSUMES — what
    version was pinned vs the source's current head. Read-only."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.edits import get_upstream_drift as _drift
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _drift(project.id, session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def send_upstream_pushback(
    project_code: str,
    source_contract_id: str,
    reason: str,
    severity: str = "schema",
    submitted_by: str | None = None,
) -> dict:
    """Push back on an upstream source change — files a consumer-pushback request
    onto the source product's incoming queue. severity ∈ cosmetic|schema|breaking."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.edits import submit_upstream_pushback, UpstreamPushbackInput
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return submit_upstream_pushback(
                project.id,
                UpstreamPushbackInput(
                    source_contract_id=source_contract_id, reason=reason,
                    severity=severity, submitted_by=submitted_by,
                ),
                session,
            )
        except HTTPException as e:
            return _err(e.detail)


# --- Review reads -------------------------------------------------------------

@mcp.tool()
def get_mapping_graph(project_code: str) -> dict:
    """The full mapping-graph payload (all current mappings + the complete
    product column set so unmapped columns are visible) — the data behind the
    marketplace/engineer lineage canvas. Read-only."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.reviews import get_mapping_graph as _graph
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _graph(project.id, session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_unmapped_columns(project_code: str) -> dict:
    """List the product's :DProdColumns with no current mapping — the engineer's
    unmapped-columns review queue. Read-only."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.reviews import get_unmapped_columns as _unmapped
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _unmapped(project.id, False, session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_stale_mappings(project_code: str, include_deactivated: bool = False) -> dict:
    """List mappings whose source link was wiped by an upstream DPROD rebuild,
    with rebind suggestions. Pair with rebind_stale_mapping. Read-only."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.reviews import get_stale_mappings as _stale
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _stale(project.id, include_deactivated, session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def rebind_stale_mapping(
    project_code: str,
    mapping_uri: str,
    new_source_uri: str,
    rebind_note: str | None = None,
) -> dict:
    """Rebind a stale mapping to a new source column (accept a rename suggestion
    or manually re-point). Reverts the mapping to pending_review. Get the URIs
    from get_stale_mappings."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.reviews import rebind_stale_mapping as _rebind, StaleMappingRebindInput
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _rebind(
                project.id,
                StaleMappingRebindInput(
                    mapping_uri=mapping_uri, new_source_uri=new_source_uri, rebind_note=rebind_note,
                ),
                session,
            )
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_mapping_rationale_report(project_code: str) -> dict:
    """The markdown rationale report explaining each mapping's source, transform,
    author, and reasoning. Stateless snapshot. Returns the markdown text."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.reviews import mappings_rationale_report
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            resp = mappings_rationale_report(project.id, session)
        except HTTPException as e:
            return _err(e.detail)
    body = getattr(resp, "body", None)
    markdown = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(resp)
    return {"project_code": project_code, "markdown": markdown}


# --- Lakehouse (Parquet + DuckDB) serving -------------------------------------

@mcp.tool()
def export_lakehouse(project_code: str, mode: str = "full", compression: str = "snappy") -> dict:
    """Export a product to the lakehouse (Parquet files + a DuckDB catalog) — the
    MCP equivalent of "Export to Lakehouse" on the serving stage. Runs the compiled
    DuckDB SELECT via COPY TO PARQUET, writes a TransferBatch manifest, registers a
    DuckDB catalog view, and verifies row counts. Set serving mode to 'lakehouse'
    first (set_serving_mode). mode='sample' caps rows for a quick check."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.export import export_lakehouse as _export, ExportBody
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _export(project.id, ExportBody(mode=mode, compression=compression),
                           session, _role=None)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def build_lakehouse_package(project_code: str, compression: str = "snappy") -> dict:
    """Build the runnable lakehouse package (models.json + run.py + README) WITHOUT
    running the export — the MCP equivalent of the "Build Lakehouse Package" build
    stage (Configure → Build → Deploy). Needs no live source connection, so the
    package is downloadable (get_lakehouse_package) as soon as this returns. Run the
    export itself with export_lakehouse (the Deploy step). Set serving mode to
    'lakehouse' first (set_serving_mode)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.export import build_lakehouse as _build, BuildBody
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _build(project.id, BuildBody(compression=compression), session, _role=None)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_lakehouse_status(project_code: str) -> dict:
    """Return the persisted lakehouse serving definition (build status, Parquet
    files, DuckDB catalog ref) for a product, or {configured: False}. Read-only."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.export import get_lakehouse_status as _status
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _status(project.id, session)
        except HTTPException as e:
            return _err(e.detail)


# --- Cross-platform transfer (transfer_then_transform) ------------------------

@mcp.tool()
def run_transfer(project_code: str, placement: str = "hybrid", write_disposition: str = "replace") -> dict:
    """Run a cross-platform transfer: extract the product from its source and load
    it into the configured TARGET platform (a different platform than the source).
    The MCP equivalent of "Transfer to target" on the serving stage. Set serving
    mode to 'transfer' first (set_serving_mode) and configure a target connection
    (set_materialization_target). placement ∈ {hybrid, transform_on_extract,
    transfer_then_transform}."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.export import run_transfer as _run, TransferBody
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _run(project.id, TransferBody(placement=placement, write_disposition=write_disposition),
                        session, _role=None)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_transfer_status(project_code: str) -> dict:
    """Return the persisted cross-platform transfer serving definition (build
    status, target platform, landed tables) for a product, or {configured: False}.
    Read-only."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.export import get_transfer_status as _status
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _status(project.id, session)
        except HTTPException as e:
            return _err(e.detail)


# --- Materialization verification gate ----------------------------------------

@mcp.tool()
def get_materialization_status(project_code: str, preview_limit: int = 20) -> dict:
    """The dbt-materialization verification-gate state: latest sample + full
    runs, a derived `gate_state`, and (when a sample passed) a per-model preview.
    Read-only — drives the sample→approve→full flow over MCP."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.materialization import materialization_status
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return materialization_status(project.id, preview_limit, session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def build_dbt_package(project_code: str, dialect: str = "postgres") -> dict:
    """Build (scaffold + assemble) the runnable dbt project on disk WITHOUT running
    `dbt build` — the MCP equivalent of the "Build dbt Project" build stage
    (Configure → Build → Deploy). Needs no live target, so the project is
    downloadable (get_dbt_project) as soon as this returns. Run the actual build
    (with the verification gate) via build_materialization_sample →
    approve_materialization_full (the Deploy step)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.materialization import build_dbt_package as _build, DbtBuildPackageBody
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _build(project.id, DbtBuildPackageBody(dialect=dialect),
                          session, user="Data Engineer")
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def build_materialization_sample(project_code: str, sample_limit: int = 1000) -> dict:
    """Run a capped SAMPLE dbt build into a preview schema — the first half of the
    materialization gate. Inspect the result with get_materialization_status, then
    approve_materialization_full (or reject_materialization_sample)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.materialization import materialize, MaterializeBody
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return materialize(project.id, MaterializeBody(mode="sample", sample_limit=sample_limit),
                               session, user="Data Engineer")
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def approve_materialization_full(project_code: str) -> dict:
    """Run the FULL dbt build after a sample passed — the gated second half. The
    server enforces the gate (a full build is refused unless a recent sample
    passed); this does NOT force-bypass it."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.materialization import materialize, MaterializeBody
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return materialize(project.id, MaterializeBody(mode="full", force=False),
                               session, user="Data Engineer")
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def reject_materialization_sample(project_code: str, reason: str = "") -> dict:
    """Reject the latest sample build so the gate re-blocks the full build until a
    fresh sample passes."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.materialization import reject_sample, RejectBody
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return reject_sample(project.id, RejectBody(reason=reason), session, user="Data Engineer")
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def set_materialization_target(
    project_code: str,
    host: str = "",
    database: str = "",
    username: str = "",
    password: str = "",
    port: int = 5432,
    target_connection_id: int | None = None,
) -> dict:
    """Set the per-product serving TARGET connection (all modes) — the MCP
    equivalent of the target picker in Configure Serving.

    Two ways to specify it:
      * `target_connection_id` — point at a registered PlatformConnection whose
        roles include "target" (preferred; works for any platform). The DSN fields
        are ignored in this case.
      * host/database/username/password/port — an inline Postgres DSN (legacy).

    When unset, serving falls back to the source connection (same-instance default),
    so this is only needed to direct the output elsewhere. From inside the
    container, a DB on the host is reachable as `host.docker.internal`.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.materialization import set_materialization_target as _set, MaterializationTargetInput

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            if target_connection_id is not None:
                body = MaterializationTargetInput(target_connection_id=target_connection_id)
            else:
                body = MaterializationTargetInput(
                    platform="postgres", host=host, port=port,
                    database=database, username=username, password=password,
                )
            _set(project.id, body, session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
    return {
        "ok": True, "configured": True,
        "next": "Configure serving mode (set_serving_mode) and complete the serving stage to build into this target.",
    }


@mcp.tool()
def get_dbt_project(project_code: str) -> dict:
    """Pull the generated dbt project for a materialized product so you can run
    and maintain it in your own dbt / CI.

    Returns a {files: {relative_path: content}} map — dbt_project.yml,
    profiles.yml, macros/, models/*.sql (+ schema.yml), snapshots/*.sql —
    excluding target/ and logs/. Write each file to a local directory
    (preserving paths) and `dbt build`. profiles.yml reads WB_DBT_* env vars,
    so no DB secrets are embedded; set those env vars when you run it.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .routers.materialization import collect_dbt_files

    files = collect_dbt_files(project_code)
    if not files:
        return {"error": (
            f"No dbt project for '{project_code}' — materialize it first "
            "(set_serving_mode materialized, then complete the serving stage)."
        )}
    return {
        "ok": True,
        "project_code": project_code,
        "file_count": len(files),
        "files": files,
        "next": "Write each file to a local dir (preserving relative paths), set WB_DBT_* env vars, then `dbt build`.",
    }


@mcp.tool()
def get_view_package(project_code: str) -> dict:
    """Pull the self-contained virtual-view deploy package for a product — the
    same package Data Workbench runs to deploy it.

    Returns a {files: {relative_path: content}} map: `view.sql` (the DDL), `run.py`
    (a stdlib runner that applies the DDL to a target and writes run_result.json),
    `_deploy_core.py`, `requirements.txt`, `.env.example`, README. Write the files
    to a local dir, set the `WB_TARGET_*` env vars from `.env.example`, then
    `python run.py --apply`. No DB secrets are embedded.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.serving import get_view_package as _get

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            res = _get(project.id, format="json", session=session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
    files = res.get("files", {})
    return {
        "ok": True, "project_code": project_code, "file_count": len(files), "files": files,
        "next": "Write each file to a local dir, fill in WB_TARGET_* (see .env.example), then `python run.py --apply`.",
    }


@mcp.tool()
def get_lakehouse_package(project_code: str) -> dict:
    """Pull the self-contained lakehouse (Parquet + DuckDB) package for a product —
    the same package Data Workbench ran to export it.

    Returns the package's TEXT files as a {files: {relative_path: content}} map:
    `run.py` (producer: source → Parquet; and `--mode query` reader), `query.py`,
    `explore.sql`, `models.json`, `requirements.txt`, `.env.example`, README, and
    the per-model TransferBatch v1 manifests. The binary Parquet data +
    `catalog.duckdb` are NOT inlined — download the zip
    (`GET /api/projects/{id}/serving/lakehouse-package?format=zip`) for those.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.export import get_lakehouse_package as _get

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            res = _get(project.id, format="json", session=session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
    files = res.get("files", {})
    return {
        "ok": True, "project_code": project_code, "file_count": len(files), "files": files,
        "next": "Text files returned; use the ?format=zip REST endpoint for the Parquet data + catalog.duckdb.",
    }


# --- Data migration (dmig) ----------------------------------------------------
# Parity with the Pipeline UI's dmig_* non-LLM stage buttons: configure the
# target + strategy, run the migration, reconcile source↔target, read status,
# and pull the runnable package. The LLM stages (dmig_assess_plan /
# dmig_generate_pipeline) run via the shared run_stage tool.

@mcp.tool()
def get_migration_status(project_code: str) -> dict:
    """The data-migration plan status for a dmig project: whether it's configured,
    the lifecycle state (draft→assessed→…→reconciled), the target platform, write
    disposition, and per-table reconciliation results. MCP equivalent of the
    Pipeline migration panel."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.migration import get_status as _status
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _status(project.id, session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def configure_migration(project_code: str, target_connection_id: int | None = None,
                        target_platform: str = "",
                        write_disposition: str = "replace", target_schema: str = "",
                        target_catalog: str = "", landing_strategy: str = "raw") -> dict:
    """Configure a data migration target as an intent: pass EITHER a registered
    `target_connection_id` (a live PlatformConnection — see list_connections) OR a
    `target_platform` string (e.g. "snowflake") for a schema-only project that has
    no target connection yet. Exactly one is required. Also set the write
    disposition (`replace`/`append`), an optional target schema, and (for 3-level
    targets like Databricks/Snowflake) the writable `target_catalog`. Creates the
    MigrationPlan at `draft`. A platform-only intent is NOT executable until a real
    connection is bound (run/reconcile stay gated). Only `raw` landing this phase."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.migration import configure as _configure, ConfigureBody
    from .auth import AuthUser
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        body = ConfigureBody(target_connection_id=target_connection_id,
                             target_platform=target_platform,
                             write_disposition=write_disposition, target_schema=target_schema,
                             target_catalog=target_catalog, landing_strategy=landing_strategy)
        try:
            return _configure(project.id, body, session,
                              user=AuthUser(email="", name=_principal(), role="engineer"), _role=None)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def run_migration_snapshot(project_code: str) -> dict:
    """Assemble the migration package and RUN it (extract from source, load into
    target), then mark the dmig_execute_transfer stage complete so the pipeline
    advances. MCP equivalent of the Run Migration button. Requires the
    Generate Migration Pipeline stage to have produced migration.json first."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (blocked := _serving_guard()) is not None:
        return blocked
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.migration import snapshot_core
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        if getattr(project, "archetype", "") != "dmig":
            return {"error": "Not a data-migration project."}
        try:
            result = snapshot_core(project, session, actor=_principal())
        except HTTPException as e:
            return _err(e.detail)
        except Exception as e:  # noqa: BLE001
            return {"error": f"Migration run failed: {e}"}
    _complete_dmig_stage(project_code, "dmig_execute_transfer")
    return result


@mcp.tool()
def run_migration_reconcile(project_code: str) -> dict:
    """Reconcile source↔target row counts for a completed migration (runs the
    package in verify mode) and mark the dmig_reconcile stage complete. MCP
    equivalent of the Reconcile Migration button."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.migration import reconcile_core
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        if getattr(project, "archetype", "") != "dmig":
            return {"error": "Not a data-migration project."}
        try:
            result = reconcile_core(project, session, actor=_principal())
        except HTTPException as e:
            return _err(e.detail)
        except Exception as e:  # noqa: BLE001
            return {"error": f"Reconciliation failed: {e}"}
    _complete_dmig_stage(project_code, "dmig_reconcile")
    return result


@mcp.tool()
def get_migration_package(project_code: str) -> dict:
    """Pull the self-contained data-migration package for a dmig project — the same
    DLT package Data Workbench runs. Returns the TEXT files as a
    {files: {relative_path: content}} map (`run.py`, `migration.json`,
    `requirements.txt`, `.env.example`, README). Use the
    `GET /api/projects/{id}/migration/package?format=zip` REST endpoint for a zip."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.migration import get_migration_package as _get
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            res = _get(project.id, format="json", session=session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
    files = res.get("files", {})
    return {"ok": True, "project_code": project_code, "file_count": len(files), "files": files}


@mcp.tool()
def set_intake_execution_mode(intake_id: int, execution_mode: str) -> dict:
    """Set a MIGRATION intake submission's execution intent BEFORE approval:
    'live' (a live source is expected) or 'schema_only' (offline — scaffold the
    Confirm-Physical-Schema flow instead of live discovery). Migration-only;
    frozen once the submission is approved. MCP equivalent of the offline toggle
    on the intake review page (so a headless reviewer can choose offline)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.intake import set_execution_mode as _set, ExecutionModeBody
    with Session(engine) as session:
        try:
            return _set(intake_id, ExecutionModeBody(execution_mode=execution_mode), session=session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_physical_schema(project_code: str) -> dict:
    """Get the reviewed physical schema for a schema-only migration project
    (pre-filled best-effort from the intake blueprint on first read). Returns
    `{status, schema, prefilled, ...}`; edit the `schema` payload and pass it to
    confirm_physical_schema. Schema-only dmig projects only."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.migration import get_physical_schema as _get
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _get(project.id, session=session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def confirm_physical_schema(project_code: str, schema: dict) -> dict:
    """Validate + confirm the physical schema (catalog/namespace/table + physical
    data_type/nullable/PK/FK) for a schema-only migration project. `schema` is a
    physical_schema.PhysicalSchema payload (get it via get_physical_schema, edit,
    confirm). Returns an error with `errors[]` on unsafe/colliding names or a
    missing physical type. After confirming, call seed_migration_schema."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.migration import confirm_physical_schema as _confirm, PhysicalSchemaBody
    from .auth import AuthUser
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _confirm(project.id, PhysicalSchemaBody(schema=schema), session=session,
                            user=AuthUser(email="", name=_principal(), role="engineer"), _role=None)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def seed_migration_schema(project_code: str) -> dict:
    """Deterministically materialize the CONFIRMED physical schema as the project
    graph catalog — no LLM, no live source (MCP equivalent of Import Provided
    Schema). Returns `{ok, datasets_loaded, columns_loaded, expected_*}`; `ok` is
    true only when the graph load verified against the confirmed schema (then the
    dmig_import_schema stage is marked complete). 409 if not confirmed yet."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.migration import seed_schema as _seed
    from .auth import AuthUser
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = _seed(project.id, session=session,
                           user=AuthUser(email="", name=_principal(), role="engineer"), _role=None)
        except HTTPException as e:
            return _err(e.detail)
    if isinstance(result, dict) and result.get("ok"):
        _complete_dmig_stage(project_code, "dmig_import_schema")
    return result


@mcp.tool()
def flip_migration_to_live(project_code: str) -> dict:
    """Transition a schema-only migration project to live (MCP equivalent of Flip
    to live). Requires BOTH a real source and a target connection; attaches live
    discovery, deletes the intake-seeded catalog, and invalidates the downstream
    stages built on it. Returns an error with `missing[]` when a source/target
    isn't bound yet."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.migration import flip_to_live as _flip
    from .auth import AuthUser
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _flip(project.id, session=session,
                         user=AuthUser(email="", name=_principal(), role="engineer"), _role=None)
        except HTTPException as e:
            return _err(e.detail)


def _complete_dmig_stage(project_code: str, stage_id: str) -> None:
    """Locate a dmig stage by id and mark it complete (mirrors deploy_virtual_view's
    stage flip) so the pipeline advances after an MCP-driven run. Best-effort."""
    import json as _json
    from .models import Workflow
    from .routers.stages import complete_stage as _complete_handler
    try:
        with Session(engine) as session:
            project = _resolve_project(session, project_code)
            if project is None:
                return
            for w in session.exec(select(Workflow).where(Workflow.project_id == project.id)).all():
                if not w.workflow_json:
                    continue
                stages = [s for s in _json.loads(w.workflow_json) if s.get("enabled", True)]
                for i, s in enumerate(stages, start=1):
                    if s.get("stage_id") == stage_id:
                        _complete_handler(project.id, i, w.workflow_id, session)
                        return
    except Exception:
        pass


# --- Code migration (cmig) ---------------------------------------------------
# Parity with the Pipeline UI's cmig_* non-LLM stage buttons + the code_spec
# review surface: link to a dmig project, import the legacy code, configure the
# conversion, review/approve the spec, and pull the package. The LLM stages
# (cmig_reverse_engineer / cmig_forward_engineer) run via the shared run_stage
# tool, which enforces require_forward_ready (force=true must NOT bypass it).

@mcp.tool()
def get_code_migration_status(project_code: str) -> dict:
    """The code-migration plan status for a cmig project: lifecycle state, the
    linked dmig project, source/target platforms, spec approval, and whether
    forward-engineering is ready (with the blocker list)."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from . import code_migration_orchestrator as cmo
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        return cmo.status_payload(session, project)


@mcp.tool()
def list_eligible_dmig(project_code: str) -> dict:
    """The data-migration projects this cmig project may link to (each with an
    eligible/verified flag). Use before link_code_migration."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from . import code_migration_orchestrator as cmo
    with Session(engine) as session:
        return {"projects": cmo.eligible_dmig_projects(session)}


@mcp.tool()
def link_code_migration(cmig_project_code: str, dmig_project_code: str) -> dict:
    """Bind a code-migration project to a completed data-migration project so the
    source→target schema mapping is known and the target platform is locked.
    Requires access to BOTH projects (dual-project authorization)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(cmig_project_code)) is not None:
        return denied
    if (denied := _deny_project(dmig_project_code)) is not None:
        return denied
    from . import code_migration_orchestrator as cmo
    with Session(engine) as session:
        project = _resolve_project(session, cmig_project_code)
        if project is None:
            return {"error": f"No project with code '{cmig_project_code}'"}
        try:
            cmo.link(session, project, dmig_project_code)
        except ValueError as e:
            return _err(str(e))
        _complete_cmig_stage(cmig_project_code, "cmig_link")
        return cmo.status_payload(session, project)


@mcp.tool()
def import_code(project_code: str, files: list[dict]) -> dict:
    """Import legacy code into a cmig project. `files` is a list of
    {filename, content_utf8} for text artifacts (NEVER a server-local path). The
    code lands in an immutable source/ dir with a SHA-256 manifest and is treated
    as untrusted data (never executed)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    import hashlib as _hashlib
    import os as _os
    from .routers.code_migration import _ALLOWED_EXTS, _MAX_UPLOAD_BYTES
    from . import code_migration_orchestrator as cmo
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None or getattr(project, "archetype", "") != "cmig":
            return {"error": f"'{project_code}' is not a code-migration project"}
        src = cmo.source_dir(project_code)
        if src.exists():
            for p in sorted(src.rglob("*"), reverse=True):
                try:
                    p.unlink() if p.is_file() else p.rmdir()
                except OSError:
                    pass
        src.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, str] = {}
        for f in files or []:
            raw = (f or {}).get("filename", "")
            name = _os.path.basename(str(raw).replace("\\", "/"))
            if not name or name.startswith(".") or "/" in name:
                return _err(f"unsafe filename: {raw!r}")
            ext = _os.path.splitext(name)[1].lower()
            if ext not in _ALLOWED_EXTS:
                return _err(f"disallowed file type {ext!r}")
            content = (f or {}).get("content_utf8", "")
            if not isinstance(content, str):
                return _err(f"{name}: content_utf8 must be a string")
            data = content.encode("utf-8")
            if len(data) > _MAX_UPLOAD_BYTES:
                return _err(f"{name} exceeds the size limit")
            (src / name).write_text(content, encoding="utf-8")
            manifest[name] = _hashlib.sha256(data.hex().encode("utf-8")).hexdigest()
        if not manifest:
            return _err("no files provided")
        cmo.record_import(session, project, manifest)
        _complete_cmig_stage(project_code, "cmig_import_code")
        return {"imported": sorted(manifest.keys()), "status": cmo.status_payload(session, project)}


@mcp.tool()
def configure_code_migration(project_code: str, target_runtime: str = "",
                            output_language: str = "sql", framework: str = "",
                            artifact_kind: str = "sql_script", source_platform: str = "",
                            source_platform_version: str = "",
                            source_corpus: dict | None = None,
                            target_corpus: dict | None = None) -> dict:
    """Configure the conversion: target runtime/version, output language, framework,
    artifact kind (sql_script / pyspark_job / notebook), and the source/target SME
    corpus provenance. The target PLATFORM stays locked from the linked dmig."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from . import code_migration_orchestrator as cmo
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        cmo.configure(session, project, target_runtime=target_runtime,
                      output_language=output_language, framework=framework,
                      artifact_kind=artifact_kind, source_platform=source_platform,
                      source_platform_version=source_platform_version,
                      source_corpus=source_corpus, target_corpus=target_corpus)
        _complete_cmig_stage(project_code, "cmig_configure")
        return cmo.status_payload(session, project)


@mcp.tool()
def get_code_spec(project_code: str) -> dict:
    """The reverse-engineered use-case spec + its review/approval state."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from . import code_migration_orchestrator as cmo
    with Session(engine) as session:
        return cmo.get_spec(session, project_code)


@mcp.tool()
def update_code_spec(project_code: str, spec: dict) -> dict:
    """Edit the reverse-engineered spec. Re-opens review, voids any prior approval,
    and stales the :USES_DATASET edges (they rebuild on the next approval)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .routers.code_migration import put_spec, SpecBody
    from .auth import AuthUser
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        from fastapi import HTTPException
        try:
            return put_spec(project.id, SpecBody(spec=spec), session,
                            user=AuthUser(email="", name=_principal(), role="engineer"), _role=None)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def approve_code_spec(project_code: str) -> dict:
    """Approve the current reverse-engineered spec: pin its hash, rebuild
    :USES_DATASET, unblock forward-engineering, and flip the review stage complete."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .routers.code_migration import approve_spec as _approve
    from .auth import AuthUser
    from fastapi import HTTPException
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _approve(project.id, session,
                            user=AuthUser(email="", name=_principal(), role="engineer"), _role=None)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def reopen_code_spec(project_code: str) -> dict:
    """Send the spec back for edits (voids approval + stales edges)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .routers.code_migration import reopen_spec as _reopen
    from .auth import AuthUser
    from fastapi import HTTPException
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _reopen(project.id, session,
                           user=AuthUser(email="", name=_principal(), role="engineer"), _role=None)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_code_migration_package(project_code: str) -> dict:
    """Assemble + return the downloadable code-migration package as a
    {files: {path: content}} map (old/ + new/ + codespec.json + conversion.json +
    README). Also advances the plan to packaged."""
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from . import serving_package
    from . import code_migration_orchestrator as cmo
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None or getattr(project, "archetype", "") != "cmig":
            return {"error": f"'{project_code}' is not a code-migration project"}
        pkg = serving_package.assemble_code_migration_package(
            project_code=project_code, product_name=project.name)
        cmo.record_packaged(session, project)
        _complete_cmig_stage(project_code, "cmig_package")
        files = serving_package.collect_package_files(pkg)
        return {"project_code": project_code,
                "files": {k: (v if isinstance(v, str) else "<binary>") for k, v in files.items()}}


@mcp.tool()
def backfill_migration_targets(project_code: str) -> dict:
    """Materialize target :Dataset graph nodes for a data-migration project that
    completed BEFORE the Part-A target-graph enrichment shipped, so a code-migration
    project can link to real nodes. Reads the persisted migration.json."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from . import migration_orchestrator as morch
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None or getattr(project, "archetype", "") != "dmig":
            return {"error": f"'{project_code}' is not a data-migration project"}
        return morch.backfill_target_graph(session, project)


def _complete_cmig_stage(project_code: str, stage_id: str) -> None:
    """Locate a cmig non-LLM stage by id and mark it complete so the pipeline
    advances after an MCP-driven action. Best-effort (mirrors _complete_dmig_stage)."""
    import json as _json
    from .models import Workflow
    from .routers.stages import complete_stage as _complete_handler
    try:
        with Session(engine) as session:
            project = _resolve_project(session, project_code)
            if project is None:
                return
            for w in session.exec(select(Workflow).where(Workflow.project_id == project.id)).all():
                if not w.workflow_json:
                    continue
                stages = [s for s in _json.loads(w.workflow_json) if s.get("enabled", True)]
                for i, s in enumerate(stages, start=1):
                    if s.get("stage_id") == stage_id:
                        _complete_handler(project.id, i, w.workflow_id, session)
                        return
    except Exception:
        pass


@mcp.tool()
def get_okf_bundle(contract_id: str) -> dict:
    """Export a published data product as an Open Knowledge Format (OKF) bundle —
    a set of markdown docs with YAML frontmatter (product / datasets / quality /
    reflection), cross-linked by :CONSUMES — for an external AI agent that can't
    reach this MCP/graph. It's a portable *briefing* that complements the ODCS
    contract + dbt project; lossy by design (OKF links are untyped).

    Returns a {files: {relative_path: content}} map. Write each file to a local
    dir (preserving paths) and point an agent at the folder. contract_id is
    `{project_code}-contract`; access is gated on that project.
    """
    project_code = (
        contract_id[: -len("-contract")] if contract_id.endswith("-contract") else contract_id
    )
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from . import okf_export
    from .routers.marketplace import _get_settings, _neo4j_from_settings

    with Session(engine) as session:
        settings = _get_settings(session)
        with _neo4j_from_settings(settings) as ns:
            row = ns.run(
                "MATCH (dc:DataContract {id:$cid})-[:MATERIALISES_AS]->(dp:DProdDataProduct) "
                "RETURN dp.uri AS uri",
                cid=contract_id,
            ).single()
        if not row or not row.get("uri"):
            return {"error": f"No data product for contract '{contract_id}'."}
        files = okf_export.collect_okf_files(settings, uri=row["uri"])
    if not files:
        return {"error": "Product has no exportable content."}
    return {
        "ok": True,
        "contract_id": contract_id,
        "file_count": len(files),
        "files": files,
        "next": "Write each file to a local dir (preserving relative paths), then point an agent at the folder.",
    }


@mcp.tool()
def get_product_report(project_code: str) -> dict:
    """Generate the full **Markdown deployment report** for a data product — the
    same report as the web UI's "Generate Report" button. Includes the overview /
    purpose / people, the schema with a **Mermaid ERD**, a **Mermaid lineage**
    graph (source columns → transforms → product columns), the mapping table,
    quality rules, OSI, and serving/deployment details. **Offer this once a product
    is deployed** so the engineer/owner gets portable documentation to paste into a
    wiki. Rendered fresh each call (snapshot at generation time).

    Returns `{report: <markdown>}` — write it to a `.md` file to keep it.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.marketplace import generate_product_report
    from .config import FRONTEND_URL
    contract_id = f"{project_code}-contract"
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        pid = project.id
        try:
            resp = generate_product_report(contract_id, session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"Report generation failed: {e}"}
    body = getattr(resp, "body", None)
    md = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(getattr(resp, "content", resp))
    return {
        "ok": True,
        "contract_id": contract_id,
        "format": "markdown",
        "report": md,
        "web_url": f"{FRONTEND_URL}/engineer/projects/{pid}",
        "next": "Write `report` to a .md file (it embeds Mermaid ERD + lineage) and offer it to the user as the deployment report.",
    }


@mcp.tool()
async def run_stage(
    project_code: str,
    stage_number: int,
    workflow_id: str | None = None,
    config: dict | None = None,
    instructions: str = "",
    force: bool = False,
) -> dict:
    """Start a pipeline stage running on the server (LLM execution is server-side).

    `instructions` (optional) is one-off, plain-language guidance for THIS run that
    gets injected into the stage's prompt as advisory context — e.g. "map salary from
    the compensation source", "treat jersey_no as text", "these two tables join on
    team_id, not id". This is a data-engineer capability with no UI equivalent (the
    UI's stage prompts are fixed). It's advisory — it can't override the workflow
    steps, project scoping, or data safety.

    Mutating + authz-gated. Returns a run_id immediately and keeps running in the
    background — poll get_project_state for status (running -> complete /
    awaiting_review / failed).

    DEPENDENCY-GATED: refuses if the stage's prerequisites aren't complete (e.g.
    metadata_enrichment before data_profiling) — see get_available_actions for what's
    runnable now. Pass force=true to override deliberately.

    `config` supplies a stage's configuration. Some stages REQUIRE it — notably
    **Data Discovery** needs `discovery_tables` (which tables to ingest). Fetch
    the valid options first with get_stage_config_options, then pass e.g.
    config={"discovery_tables": "public.orders, public.customers"} (and
    optionally "discovery_schemas"). Mid-run questions (if any) are handled via
    get_pending_questions / answer_question.

    Non-LLM stages that complete via a UI button return an error directing you
    elsewhere.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied

    from .stage_execution import start_stage_run
    from .routers.stages import _resolve_stage_id

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        sid = _resolve_stage_id(project, stage_number, workflow_id, session)
        # Role gate (only when the token carries a bound account role): refuse a
        # PO↔Engineer boundary violation up front with a clear message. When the
        # token has no bound role this is a no-op (start_stage_run's acting_role
        # backstop stays None too).
        _role = _auth_role()
        if _role is not None:
            from .archetypes import STAGE_REGISTRY
            from .authz import role_can_run_stage
            _owner_role = STAGE_REGISTRY.get(sid or "", {}).get("owner_role")
            if not role_can_run_stage(_role, _owner_role):
                return {"error": (
                    f"Your token's account role '{_role}' cannot run stage "
                    f"'{sid}' (owned by '{_owner_role}')."
                )}
        # Required-config check applies even with force — a missing key can't be
        # forced past; the stage would just stall.
        if (cgate := _required_config_gate(sid, config)) is not None:
            return cgate
        if not force:
            if (gate := _dependency_gate(session, project, sid)) is not None:
                return gate
            if (mgate := _serving_mappings_gate(project, sid)) is not None:
                return mgate
        project_id = project.id

    async def _noop(_msg: dict):  # headless: events aren't streamed anywhere
        return None

    # Thread the one-off engineer instructions into the stage config; build_prompt
    # appends them to the prompt as advisory context (after template.format).
    eff_config = dict(config or {})
    if instructions and instructions.strip():
        eff_config["engineer_instructions"] = instructions.strip()

    try:
        run_id = await start_stage_run(
            project_id,
            stage_number,
            workflow_id=workflow_id,
            stage_config=eff_config or None,
            event_sink=_noop,
            acting_role=_auth_role(),  # token-bound role gates PO↔Engineer; None = unbound/legacy
        )
    except KeyError as e:
        return {
            "error": (
                f"Stage {stage_number} needs a config value {e} that wasn't "
                f"provided. Fetch the options with get_stage_config_options, then "
                f"pass them in `config` (e.g. config={{\"discovery_tables\": "
                f"\"schema.table, schema.table2\"}})."
            )
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not start stage {stage_number}: {e}"}

    if run_id is None:
        return {
            "error": (
                f"Stage {stage_number} is not runnable via run_stage — it was not "
                f"found, or it's a non-LLM stage that completes via the UI's "
                f"Complete action. Check the stage number / workflow_id with "
                f"get_project_state."
            )
        }
    return {
        "ok": True,
        "project_code": project_code,
        "stage_number": stage_number,
        "run_id": run_id,
        "status": "started",
        "note": "Stage is running server-side; poll get_project_state for status.",
        "on_complete": (
            "When it reaches complete/awaiting_review, call get_stage_output(stage_id) and show the "
            "user the Step Report BEFORE continuing — let them approve, refine, or redo (REVIEW CHECKPOINT)."
        ),
    }


@mcp.tool()
def get_stage_config_options(
    project_code: str, stage_number: int, workflow_id: str | None = None
) -> dict:
    """Get the configuration options a stage needs before you run it.

    For **Data Discovery** this returns the available `discovery_schemas` and
    `discovery_tables` (each an option list with value/label) discovered from the
    source database — pick from these and pass the chosen values to run_stage's
    `config`. Returns `{}` for stages that need no config.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied

    from fastapi import HTTPException

    from .routers.stages import get_config_options as _opts

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _opts(
                project_id=project.id,
                stage_number=stage_number,
                workflow_id=workflow_id,
                session=session,
            )
        except HTTPException as e:
            return {"error": f"{e.status_code}: {e.detail}"}


@mcp.tool()
def get_pending_questions(run_id: str) -> dict:
    """List questions a running stage is waiting on (for interactive stages).

    Some stages (e.g. Data Discovery) pause mid-run to ask the engineer a
    question. Poll this after run_stage; when a question appears, present its
    prompt/options to the user and answer it with answer_question. Returns an
    empty list while the stage isn't waiting on anything.
    """
    from .message_queue import message_queue
    from .stage_execution import project_for_run

    pc = project_for_run(run_id)
    if pc is None:
        return {
            "error": (
                f"No active run '{run_id}' — it may have finished, timed out, or "
                f"never started. Check get_project_state."
            )
        }
    if (denied := _deny_project(pc)) is not None:
        return denied
    return {"run_id": run_id, "project_code": pc, "questions": message_queue.list_pending(run_id)}


@mcp.tool()
async def answer_question(run_id: str, question_id: str, value: str) -> dict:
    """Answer a question a running stage is waiting on — unblocks the stage.

    `value` matches what the question expects: free text; 'yes'/'no'; a single
    option value for multiple_choice; or comma-separated option values for a
    checklist (e.g. "public.orders, public.customers").
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    from .message_queue import message_queue
    from .stage_execution import project_for_run

    pc = project_for_run(run_id)
    if pc is None:
        return {"error": f"No active run '{run_id}'."}
    if (denied := _deny_project(pc)) is not None:
        return denied
    ok = await message_queue.post_response(run_id, question_id, value)
    if not ok:
        return {
            "error": (
                f"No pending question '{question_id}' for run '{run_id}' "
                f"(already answered, or it timed out)."
            )
        }
    return {"ok": True, "run_id": run_id, "question_id": question_id, "answered": value}


# ── Review-write tools (mutating, authz + role gated) ───────────────────────
# These wrap the SAME vetted reviews.py handlers the web UI POSTs to, so the
# PROV-O provenance and auto-stage-flip (_check_review_complete) are identical.
# The caller acts on behalf of a human engineer, so writes are attributed to the
# token principal as a human reviewer. A `role` must be declared and is enforced
# against the surface's allowed roles (the "switch role" model). Pair these with
# get_stage_results (read the queue) and get_project_state (watch the stage flip).

@mcp.tool()
def review_description(
    project_code: str,
    action: str,
    desc_uri: str,
    role: str,
    corrected_text: str | None = None,
    col_uri: str | None = None,
    category: str | None = None,
    detail: str | None = None,
    quality: int | None = None,
) -> dict:
    """Approve or edit a pending column description (clears the enrichment review).

    `action`: "approve" signs off the AI text as-is; "reject" replaces it with
    `corrected_text` (requires `col_uri`) and approves the corrected version,
    recording a rejection reason. `quality` 1=Acceptable/2=Good/3=Excellent.
    Requires `role` ∈ Data Steward / Reviewer / Data Product Owner (a Data
    Engineer must switch to one of these). Find `desc_uri`/`col_uri` via
    get_stage_results(card="descriptions").
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    if (bad := _role_guard("descriptions", role)) is not None:
        return bad
    from .routers.reviews import review_description as _h, DescriptionReviewAction
    body = DescriptionReviewAction(
        action=action, desc_uri=desc_uri, col_uri=col_uri,
        corrected_text=corrected_text, category=category, detail=detail,
        quality=quality, reviewer=_principal(),
    )
    return _call_review(_h, project_code, body)


@mcp.tool()
def review_mapping(
    project_code: str,
    action: str,
    mapping_uri: str,
    role: str,
    quality: int | None = None,
    category: str | None = None,
    detail: str | None = None,
    remap_source_uri: str | None = None,
    source_col_uris: list | None = None,
    transform_kind: str | None = None,
    transform_expression: str | None = None,
    transform_inputs: list | None = None,
    transform_params: dict | None = None,
    transform_decorators: dict | None = None,
    escalation_reason: str | None = None,
) -> dict:
    """Approve / replace / escalate a pending column mapping.

    `action`: "approve" (sign off, optional `quality`); "replace_mapping" (unified
    override — change transform fragment/params, source set via `source_col_uris`,
    and/or remap to `remap_source_uri`; requires `category`, e.g. "edited_default");
    "escalate_to_steward" (send to the Data Steward with `escalation_reason`).
    Requires `role` ∈ Data Engineer / Reviewer. Find `mapping_uri` via
    get_stage_results(card="mappings").
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    if (bad := _role_guard("mappings", role)) is not None:
        return bad
    from .routers.reviews import review_mapping as _h, MappingReviewAction
    body = MappingReviewAction(
        action=action, mapping_uri=mapping_uri, quality=quality,
        category=category, detail=detail, remap_source_uri=remap_source_uri,
        source_col_uris=source_col_uris, transform_kind=transform_kind,
        transform_expression=transform_expression, transform_inputs=transform_inputs,
        transform_params=transform_params, transform_decorators=transform_decorators,
        escalation_reason=escalation_reason, reviewer=_principal(),
    )
    return _call_review(_h, project_code, body)


@mcp.tool()
def review_domain_rule(
    project_code: str,
    action: str,
    rule_uri: str,
    role: str,
    category: str | None = None,
    detail: str | None = None,
    quality: int | None = None,
) -> dict:
    """Approve or reject a pending domain DQ rule.

    `action`: "approve" or "reject" (with optional `category`/`detail`).
    Requires `role` ∈ Data Quality Analyst / Data Product Owner / Reviewer.
    Find `rule_uri` via get_stage_results(card="dq_rules").
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    if (bad := _role_guard("domain_rules", role)) is not None:
        return bad
    from .routers.reviews import review_domain_rule as _h, DomainRuleReviewAction
    body = DomainRuleReviewAction(
        action=action, rule_uri=rule_uri, category=category,
        detail=detail, quality=quality, reviewer=_principal(),
    )
    return _call_review(_h, project_code, body)


_TABLE_ACTION = {"approve": "approve_table", "reject": "reject_table", "edit": "edit_table"}
_REL_ACTION = {"approve": "approve_relationship", "reject": "reject_relationship", "edit": "edit_relationship"}


@mcp.tool()
def review_table_description(
    project_code: str,
    action: str,
    desc_uri: str,
    role: str,
    new_text: str | None = None,
    relationship_kind: str | None = None,
    category: str | None = None,
    detail: str | None = None,
) -> dict:
    """Approve / reject / edit a pending TABLE description (dpe-sa Tables tab).

    `action` ∈ approve | reject | edit. "edit" replaces the text (needs
    `new_text`; `relationship_kind` ∈ fact / lookup_dimension / general_membership
    / specialization / audit_log / configuration / unknown). Requires `role` ∈
    Data Product Owner / Data Steward / Reviewer. Find `desc_uri` via
    get_stage_results(card="datasets") → `table_desc_uri`.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    if (bad := _role_guard("table_descriptions", role)) is not None:
        return bad
    mapped = _TABLE_ACTION.get(action)
    if mapped is None:
        return {"error": f"action must be one of {sorted(_TABLE_ACTION)} (got {action!r})"}
    from .routers.reviews import review_source_product as _h, SourceProductValidationAction
    body = SourceProductValidationAction(
        action=mapped, desc_uri=desc_uri, new_text=new_text,
        relationship_kind=relationship_kind, category=category, detail=detail,
        reviewer=_principal(),
    )
    return _call_review(_h, project_code, body)


@mcp.tool()
def review_relationship_description(
    project_code: str,
    action: str,
    desc_uri: str,
    role: str,
    new_text: str | None = None,
    relationship_nature: str | None = None,
    category: str | None = None,
    detail: str | None = None,
) -> dict:
    """Approve / reject / edit a pending RELATIONSHIP description (dpe-sa).

    `action` ∈ approve | reject | edit. "edit" replaces the text (needs
    `new_text`; `relationship_nature` ∈ belongs_to / categorises / audit_log_for
    / references). Requires `role` ∈ Data Product Owner / Data Steward / Reviewer.
    Find `desc_uri` via get_stage_results(card="relationships") → `desc_uri`.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    if (bad := _role_guard("relationship_descriptions", role)) is not None:
        return bad
    mapped = _REL_ACTION.get(action)
    if mapped is None:
        return {"error": f"action must be one of {sorted(_REL_ACTION)} (got {action!r})"}
    from .routers.reviews import review_source_product as _h, SourceProductValidationAction
    body = SourceProductValidationAction(
        action=mapped, desc_uri=desc_uri, new_text=new_text,
        relationship_nature=relationship_nature, category=category, detail=detail,
        reviewer=_principal(),
    )
    return _call_review(_h, project_code, body)


def _call_review(handler, project_code: str, body) -> dict:
    """Resolve the project and invoke a reviews.py handler as a plain function
    (passing our own Session), translating its HTTPException into an error dict."""
    from fastapi import HTTPException
    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = handler(project.id, body, session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001 — surface a clean message
            return {"error": f"Review write failed: {e}"}
    return {"ok": True, "action": body.action, "reviewer": body.reviewer, **(result or {})}


# Per-stage post-completion count queries — so complete_stage can report what it
# produced (verifiability), instead of a bare {ok}. Each is project-scoped.
_COMPLETION_SUMMARY_QUERIES: dict[str, str] = {
    "synthesize_odcs_from_graph": (
        "MATCH (:Project {projectCode:$pc})-[:HAS_CONTRACT]->(dc:DataContract) "
        "OPTIONAL MATCH (dc)-[:HAS_SCHEMA]->(sc:DataContractSchema) "
        "OPTIONAL MATCH (sc)-[:HAS_PROPERTY]->(p:DataContractProperty) "
        "RETURN count(DISTINCT sc) AS schemas, count(DISTINCT p) AS properties"
    ),
    "odcs_to_dprod": (
        "MATCH (:Project {projectCode:$pc})-[:HAS_CONTRACT]->(:DataContract)"
        "-[:MATERIALISES_AS]->(dp:DProdDataProduct) "
        "OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)"
        "-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset) "
        "OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(c:DProdColumn) "
        "RETURN count(DISTINCT dp) AS data_products, count(DISTINCT ods) AS output_datasets, "
        "count(DISTINCT c) AS product_columns"
    ),
    "auto_mapping_sa": (
        "MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn) "
        "WHERE pc.uri STARTS WITH 'dprod:col:' + $pc + '-contract:' "
        "RETURN count(cm) AS mappings"
    ),
    "deploy_virtual_view": (
        "MATCH (:Project {projectCode:$pc})-[:HAS_CONTRACT]->(:DataContract)"
        "-[:MATERIALISES_AS]->(:DProdDataProduct)-[:SERVED_BY]->(sd:ServingDefinition) "
        "RETURN coalesce(sd.deploymentStatus,'pending') AS deployment_status"
    ),
}


def _completion_summary(project, stage_id: str | None) -> dict:
    """Best-effort counts of what a just-completed stage produced. Never raises —
    a summary failure must not mask a successful completion."""
    q = _COMPLETION_SUMMARY_QUERIES.get(stage_id or "")
    if not q:
        return {}
    try:
        from .neo4j_client import neo4j_session
        with neo4j_session(
            project.neo4j_host, project.neo4j_port, project.neo4j_user,
            project.neo4j_password, project.neo4j_database,
        ) as ns:
            rec = ns.run(q, pc=project.project_code).single()
            return {k: _jsonable(v) for k, v in rec.data().items()} if rec else {}
    except Exception:  # noqa: BLE001 — summary is advisory only
        return {}


@mcp.tool()
def complete_stage(
    project_code: str,
    stage_number: int,
    workflow_id: str | None = None,
    force: bool = False,
) -> dict:
    """Complete a non-LLM *lifecycle* stage — the MCP equivalent of the UI's
    "Complete" button (e.g. Mark Discovery Complete, Mark Engineering Complete,
    ODCS→dprod, Publish, Synthesize ODCS, Auto-Map).

    These are mechanical state transitions, so they only need project authz — no
    role. The same backend side-effects fire as the UI (e.g. discovery_complete_at
    is stamped); poll get_project_state to see the flip. Companion to run_stage
    (start an LLM stage) and reset_stage (→ pending).

    DEPENDENCY-GATED: refuses if the stage's prerequisites aren't complete (see
    get_available_actions). Pass force=true to override.

    REFUSES non-mechanical stages with guidance:
      * LLM stages       → use run_stage
      * DQ test stages   → use run_stage (backend-executed)
      * review gates     → clear via the review_* tools / the web UI
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.stages import _resolve_stage_id, complete_stage as _complete_handler
    from .archetypes import STAGE_REGISTRY

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        stage_id = _resolve_stage_id(project, stage_number, workflow_id, session)
        stage_def = STAGE_REGISTRY.get(stage_id, {}) if stage_id else {}

        # Dependency gate — can't complete a step whose prerequisites aren't done.
        if not force and (gate := _dependency_gate(session, project, stage_id)) is not None:
            return gate

        # Guard: only mechanical non-LLM lifecycle stages may complete here.
        if stage_def.get("requires_llm"):
            return {"error": (
                f"Stage {stage_number} ('{stage_id}') is an LLM stage — run it "
                f"with run_stage, don't complete it directly."
            )}
        if stage_id in _BACKEND_DRIVEN_STAGE_IDS:
            return {"error": (
                f"Stage {stage_number} ('{stage_id}') is backend-executed — run it "
                f"with run_stage."
            )}
        if stage_def.get("review_type"):
            return {"error": (
                f"Stage {stage_number} ('{stage_id}') is a review gate — clear it "
                f"via the review_* tools (or the web UI), not complete_stage."
            )}

        try:
            result = _complete_handler(project.id, stage_number, workflow_id, session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001 — surface a clean message
            return {"error": f"Complete failed: {e}"}
        # Verifiability: report what the stage produced (counts), so the driver
        # can confirm success without a follow-up query.
        summary = _completion_summary(project, stage_id)
    out = {"ok": True, "stage_id": stage_id, **(result or {})}
    if summary:
        out["produced"] = summary
    return out


@mcp.tool()
async def query_semantic_layer(
    domain: str,
    question: str,
    retrieval_mode: str = "concept_guided",
    resolved_value: str = "",
    auto_disambiguate: bool = False,
) -> str:
    """Ask the marketplace semantic layer a natural-language question and get a
    markdown answer. Runs the Concept-Guided pipeline: resolves the question to
    business entities/attributes/values (and shared references like Country),
    authors SQL against the domain's deployed views, executes it, and
    synthesizes a grounded answer. `domain` is a marketplace domain slug (e.g.
    'customer', 'products_sales'); `retrieval_mode` is 'concept_guided' (default)
    or 'full'. Returns markdown (answer + result table + the SQL).

    Record-level lookups: when the question names a specific record by a
    high-cardinality value ("where does John Doe live?") the value is resolved
    against live data. If it can't be uniquely matched you'll get back a
    **candidate list**; re-call with `resolved_value` set to the exact value you
    chose. Set `auto_disambiguate=true` to have the tool auto-pick the top
    candidate in one shot (it notes which value it used)."""
    if (blocked := _serving_guard()) is not None:
        return f"**Error:** {blocked['error']}"
    if not domain or not domain.strip():
        return "**Error:** `domain` is required."
    if not question or not question.strip():
        return "**Error:** `question` is required."
    # Domain-scope the read: query_semantic_layer executes SQL across a domain's
    # deployed views, so a project-scoped token must be authorized for every
    # product in the domain (mirrors the get_semantic_discovery_status siblings).
    if (denied := _deny_domain(domain.strip())) is not None:
        return f"**Error:** {denied['error']}"

    from . import marketplace_chat as mc
    from .models import AppSettings

    with Session(engine) as session:
        settings = session.get(AppSettings, 1) or AppSettings()

    # A re-call with an exact value re-probes; the exact match resolves uniquely
    # (fuzzy 100) and grounds without further disambiguation.
    rv = None
    if resolved_value and resolved_value.strip():
        rv = [{"value": resolved_value.strip(), "mention_text": resolved_value.strip(),
               "column_name": "", "view_name": "", "view_schema": ""}]

    async def _ask(resolved=None):
        return await mc.answer_question(
            settings, domain.strip(), question.strip(), retrieval_mode=retrieval_mode,
            resolved_values=resolved,
        )

    try:
        res = await _ask(rv)
    except Exception as e:  # noqa: BLE001
        return f"**Error:** semantic query failed: {e}"

    status = res.get("status")
    auto_note = ""
    if status == "needs_disambiguation":
        dis = res.get("disambiguation") or {}
        cands = dis.get("candidates") or []
        if auto_disambiguate and cands:
            top = cands[0]
            try:
                res = await _ask([{
                    "value": top.get("value") or "", "mention_text": dis.get("mention_text") or "",
                    "column_name": top.get("column_name") or "", "view_name": top.get("view_name") or "",
                    "view_schema": top.get("view_schema") or "",
                }])
            except Exception as e:  # noqa: BLE001
                return f"**Error:** semantic query failed: {e}"
            auto_note = f"\n\n_Auto-resolved “{dis.get('mention_text')}” → {top.get('label') or top.get('value')}._"
            status = res.get("status")
        else:
            lines = [f"**Need to disambiguate “{dis.get('mention_text')}”.** "
                     + (dis.get("prompt") or "")]
            if dis.get("kind") == "attribute":
                for o in dis.get("attribute_options") or []:
                    lines.append(f"- {o.get('label')}")
                lines.append("\n_Re-ask naming which one you mean._")
            else:
                for i, c in enumerate(cands, 1):
                    lines.append(f"{i}. {c.get('label') or c.get('value')}")
                lines.append("\n_Re-call with `resolved_value` set to the exact value, "
                             "or pass `auto_disambiguate=true`._")
            return "\n".join(lines)
    if status == "refused":
        return f"**Can't answer that.** {res.get('refused_reason') or res.get('message') or ''}"
    if status != "ok":
        return f"**Error ({res.get('error_class') or 'failed'}):** {res.get('error_message') or 'query failed'}"

    parts: list[str] = ["## Answer", ""]
    parts.append((res.get("synthesized_answer") or res.get("message") or "(no answer)") + auto_note)
    parts.append("")

    cols = res.get("columns") or []
    rows = res.get("rows") or []
    if cols and rows:
        header = [c.get("name", "") for c in cols]
        parts.append("| " + " | ".join(header) + " |")
        parts.append("| " + " | ".join("---" for _ in header) + " |")
        for r in rows[:50]:
            parts.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
        rc = res.get("row_count")
        if isinstance(rc, int) and rc > 50:
            parts.append(f"\n_…{rc} rows total (showing 50)._")
        parts.append("")

    matched = [c.get("name") for c in (res.get("matched_concepts") or []) if c.get("name")]
    if matched:
        parts.append(f"_Concepts used: {', '.join(matched)}._")
    if res.get("concept_fallback_reason"):
        parts.append(f"_Note: {res['concept_fallback_reason']}._")

    if res.get("sql"):
        parts.append("\n```sql\n" + res["sql"] + "\n```")
    return "\n".join(parts)


# ── Semantic-layer discovery (domain-scoped) ────────────────────────────────
#
# Lets an engineer drive the concept-building sequence headlessly:
#   1. get_semantic_discovery_status  — what's been run, when, and is it stale?
#   2. reset_semantic_discovery       — optional clean slate
#   3. run_semantic_discovery_step    — scaffold → recommend → enrich (in order)
# The data-product layer is READ-ONLY; writes touch :BusinessConcept only.


@mcp.tool()
def get_semantic_discovery_status(domain: str) -> dict:
    """Report the semantic-layer discovery state for a domain: for each step
    (scaffold / recommend / enrich) whether it has run, when it last ran, its
    stats, and whether it's STALE (sources changed since, or an upstream step
    re-ran). Also returns current concept counts and any STRANDED concepts
    (active concepts with no binding to a data product). Read-only.

    Recommended sequence: scaffold → recommend → enrich. Run `scaffold` first to
    build the bound entity spine, then `recommend` to overlay cross-product
    concepts, then `enrich` to polish names/definitions."""
    if (denied := _deny_domain(domain)) is not None:
        return denied
    from . import semantic_discovery as disc
    with Session(engine) as session:
        settings = session.get(AppSettings, 1) or AppSettings()
    try:
        return disc.get_status(settings, domain.strip())
    except Exception as e:  # noqa: BLE001
        return {"error": f"status failed: {e}"}


@mcp.tool()
def reset_semantic_discovery(domain: str, role: str) -> dict:
    """Clear out a domain's concept layer (soft-deprecate every active concept +
    its edges to data products) so the discovery sequence can be re-run from a
    clean slate. Audit history survives. Mutating — requires `role` ∈ Data
    Steward / Data Engineer / Data Product Owner and a token scoped to the
    domain. Returns the count deprecated."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_domain(domain)) is not None:
        return denied
    if (bad := _role_guard("semantic_discovery", role)) is not None:
        return bad
    from . import semantic_discovery as disc
    with Session(engine) as session:
        settings = session.get(AppSettings, 1) or AppSettings()
    try:
        res = disc.reset_domain(settings, domain.strip(), triggered_by=_principal())
        return {"ok": True, **res}
    except Exception as e:  # noqa: BLE001
        return {"error": f"reset failed: {e}"}


@mcp.tool()
async def run_semantic_discovery_step(
    domain: str,
    step: str,
    role: str,
    confidence_threshold: float | None = None,
    dry_run: bool = False,
) -> dict:
    """Run one semantic-discovery step for a domain and record it. Mutating —
    requires `role` ∈ Data Steward / Data Engineer / Data Product Owner and a
    token scoped to the domain.

    `step` is one of:
      - `scaffold`  — deterministic entity/attribute/value spine + FK
        relationships from the domain's schema. `dry_run=true` previews the
        counts WITHOUT writing. Run this FIRST.
      - `recommend` — cross-product advisor pass, then auto-promote proposals at
        or above `confidence_threshold` (default 0.7) into bound + parented
        attributes; lower-confidence proposals stay queued for human review.
      - `enrich`    — LLM polish of names / definitions / synonyms. Run LAST.

    After running, call get_semantic_discovery_status to confirm and to check for
    stranded concepts."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_domain(domain)) is not None:
        return denied
    if (bad := _role_guard("semantic_discovery", role)) is not None:
        return bad
    from . import semantic_discovery as disc
    with Session(engine) as session:
        settings = session.get(AppSettings, 1) or AppSettings()
    try:
        return await disc.run_step(
            settings, domain.strip(), step,
            dry_run=dry_run, confidence_threshold=confidence_threshold,
            triggered_by=_principal(),
        )
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{step} failed: {e}"}


# ── Consumer-aligned (dpe-cf) authoring tools ──────────────────────────────
# These are the MCP equivalents of the 10-step NewProductWizard in the UI.
# A Data Engineer (or PO acting via MCP) can author a consumer-aligned product
# entirely via these tools without touching the browser.


@mcp.tool()
def create_consumer_product(
    name: str,
    domain: str,
    product_idea: str,
    owner_email: str,
    owner_name: str = "",
) -> dict:
    """Create a new consumer-aligned (dpe-cf) Data Workbench project — the MCP
    equivalent of the PO clicking "New Product" in the Product Workbench.

    Returns the new project's `project_code` (use it in all subsequent tools)
    and `project_id`. Does NOT submit a product request — author the ODCS spec
    first (save_odcs_spec), then call submit_product_spec to hand it to engineering.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.projects import create_project, ProjectCreate

    with Session(engine) as session:
        try:
            result = create_project(
                ProjectCreate(
                    name=name,
                    archetype="dpe-cf",
                    domain=domain.strip().lower() if domain else None,
                    product_idea=product_idea.strip() if product_idea else None,
                    owner_email=owner_email.strip() if owner_email else None,
                    owner_name=owner_name.strip() if owner_name else None,
                ),
                session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:
            return {"error": f"Project creation failed: {e}"}

    project_code = result["project_code"] if isinstance(result, dict) else getattr(result, "project_code", None)
    project_id = result["id"] if isinstance(result, dict) else getattr(result, "id", None)
    return {
        "ok": True,
        "project_code": project_code,
        "project_id": project_id,
        "archetype": "dpe-cf",
        "next": "Call save_odcs_spec to author the contract, then submit_product_spec to hand it to engineering.",
    }


@mcp.tool()
def list_marketplace_products(
    product_kind: str | None = None,
    owned_by: str | None = None,
    tag: str | None = None,
) -> dict:
    """List published data products in the marketplace.

    `product_kind` filters to 'source' (dpe-sa) or 'consumer' (dpe-cf); omit
    for all. `owned_by` filters to products owned by a given email. `tag`
    filters to products carrying that tag (case-insensitive). Returns summary
    rows with name, domain, product_kind, tags, osi_band, and column_count.
    Use this to find source products to wire into a consumer spec's `inputs[]`.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .routers.marketplace import list_published

    with Session(engine) as session:
        try:
            result = list_published(session=session, owned_by=owned_by, product_kind=product_kind, tag=tag)
        except Exception as e:
            return {"error": f"Marketplace query failed: {e}"}

    products = result.get("products") or []
    summary = [
        {
            "uri": p.get("uri"),
            "name": p.get("name"),
            "domain": p.get("domain"),
            "product_kind": p.get("product_kind"),
            "tags": p.get("tags") or [],
            "lifecycle_state": p.get("lifecycle_state"),
            "osi_band": p.get("osi_band"),
            "column_count": p.get("column_count"),
            "contract_id": p.get("contract_id"),
            "description": (p.get("description") or "")[:120],
        }
        for p in products
    ]
    return {"products": summary, "count": len(summary)}


@mcp.tool()
def get_odcs_spec(project_code: str) -> dict:
    """Read the current ODCS v3.1 spec from the graph for a project — the MCP
    equivalent of opening the ODCS editor in the Product Workbench.

    Returns the parsed `spec` dict, the `contract_id`, `lifecycle_state`,
    `current_version`, and any rejection details (if the PO last received a
    rejection from engineering). Returns `spec: null` when no spec has been
    saved yet.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.odcs import get_odcs as _get_odcs

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _get_odcs(project.id, session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:
            return {"error": f"ODCS read failed: {e}"}


@mcp.tool()
def save_odcs_spec(
    project_code: str,
    spec: dict,
    submitted_by: str | None = None,
    change_kind: str = "auto",
    revision_notes: str = "",
) -> dict:
    """Save (or update) the ODCS v3.1 contract spec to the graph — the MCP
    equivalent of the PO typing in the ODCS editor and clicking Save.

    `spec` is a dict matching the ODCS v3.1 shape (name, domain, schema[], owners[],
    quality[], `tags[]`, etc.). `tags` are free-form product labels (trimmed +
    case-insensitively deduped) that drive the marketplace tag filter + group-by
    and can be edited later. `change_kind` controls version branching: 'auto'
    (default) classifies the diff; 'cosmetic' patches in-place; 'schema'/'breaking'
    cuts a new version. After saving, call `complete_stage` for `odcs_to_dprod` to
    rebuild the :DProdDataProduct subgraph.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.odcs import save_odcs as _save_odcs, ODCSSpecInput

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = _save_odcs(
                project.id,
                ODCSSpecInput(
                    spec=spec,
                    submitted_by=submitted_by,
                    change_kind=change_kind,
                    revision_notes=revision_notes or "",
                ),
                session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:
            return {"error": f"ODCS save failed: {e}"}

    _branched = isinstance(result, dict) and result.get("branched")
    _next = "Call complete_stage for odcs_to_dprod to rebuild the product graph."
    if _branched:
        _next = (
            f"This save BRANCHED a new contract version (v{result.get('new_version')}); the "
            "previous version is preserved. If that was unintended, call discard_draft_version. "
        ) + _next
    return {"ok": True, **(result if isinstance(result, dict) else {}), "next": _next}


@mcp.tool()
def discard_draft_version(project_code: str) -> dict:
    """Discard an unintended draft contract version and roll back to the prior one.

    Use this to undo a save that BRANCHED a new version (see `save_odcs_spec`'s
    `branched`/`new_version` in its return) when that branch was not intended.
    Only works when the head version is a *draft* branched from a prior version —
    it never destroys published/approved history (returns an error otherwise).
    Existing column mappings on the surviving columns are preserved.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.odcs import discard_draft as _discard_draft

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = _discard_draft(project.id, session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:
            return {"error": f"discard-draft failed: {e}"}

    return {**(result if isinstance(result, dict) else {}),
            "next": "Call complete_stage for odcs_to_dprod if you want to fully re-sync the product graph."}


@mcp.tool()
def submit_product_spec(
    project_code: str,
    submitted_by: str,
    notes: str | None = None,
    kind: str = "new",
) -> dict:
    """Submit the project's ODCS spec to engineering — the MCP equivalent of
    the PO clicking Submit in the Product Workbench.

    `kind` ∈ 'new' | 'edit' | 'ingest' (default 'new'). `submitted_by` is the
    PO's email. Creates a ProductRequest row (visible in engineering's Incoming
    queue) and flips the contract to lifecycleState='submitted'. After submission,
    the engineer runs accept_request to start the pipeline.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.product_requests import submit_product_request, SubmitRequestBody
    from .models import ProductRequestKind

    kind_map = {"new": ProductRequestKind.new, "edit": ProductRequestKind.edit}
    req_kind = kind_map.get(kind.strip().lower())
    if req_kind is None:
        return {"error": f"kind must be 'new' or 'edit' (got {kind!r}). Ingest goes through ingest_odcs_spec."}

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = submit_product_request(
                project.id,
                SubmitRequestBody(kind=req_kind, submitted_by=submitted_by, notes=notes),
                session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:
            return {"error": f"Submit failed: {e}"}

    return {
        "ok": True,
        "request_id": result.get("id") if isinstance(result, dict) else getattr(result, "id", None),
        "kind": kind,
        "status": "submitted",
        "next": "Engineering picks this up from the Incoming queue; call accept_request to start the pipeline.",
    }


@mcp.tool()
def suggest_domain_rules(project_code: str, persist: bool = False) -> dict:
    """Suggest domain-catalog DQ rules for the project's :DProdColumn nodes.

    Matches the product's columns against the domain catalog (playbook/domain_catalogs/)
    and returns candidate :PropertyShape rules (notNull, unique, allowedValues, range,
    regex, maxLength). When `persist=True`, writes them to the graph as
    `ruleSource='domain', status='pending_review'` so the PO can approve/reject them
    via `review_domain_rule`. Safe to call multiple times — ON CREATE means
    already-created rules are not duplicated.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.odcs import suggest_domain_rules as _suggest, SuggestDomainRulesInput

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = _suggest(project.id, SuggestDomainRulesInput(persist=persist), session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:
            return {"error": f"Rule suggestion failed: {e}"}

    rules = result.get("rules") or []
    return {
        "project_code": project_code,
        "domain": result.get("domain"),
        "rule_count": len(rules),
        "rules": rules,
        "persisted": bool(result.get("persisted")),
        "note": result.get("note"),
    }


@mcp.tool()
async def interpret_filter_intent(
    intent: str,
    columns: list | None = None,
    project_code: str | None = None,
    output_dataset_uri: str | None = None,
    dialect: str = "postgres",
    notes: str = "",
) -> dict:
    """Interpret a plain-language dataset filter into a grounded SQL predicate.

    The PO says "only active employees" — this returns the compiled SQL predicate
    (e.g. `employment_status = 'active'`), a plain-language readback for the PO to
    confirm, a confidence score, and any warnings. When BOTH `project_code` and
    `output_dataset_uri` are given, the source columns feeding that output dataset
    (via its current mappings) and their observed profile values are used to ground
    the literal to the exact stored casing. `columns` is an optional list of
    {name, type, top_values} dicts for additional context.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .routers.filter_intent import interpret_filter_intent as _interpret

    project = None
    if project_code:
        if (denied := _deny_project(project_code)) is not None:
            return denied
        with Session(engine) as session:
            project = session.exec(
                select(Project).where(Project.project_code == project_code)
            ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}

    try:
        result = await _interpret(
            intent=intent,
            columns=list(columns or []),
            project=project,
            output_dataset_uri=output_dataset_uri,
            dialect=dialect,
            notes=notes,
        )
    except Exception as e:
        return {"error": f"Interpretation failed: {e}"}

    return {
        "readback": result.readback,
        "predicate": result.predicate,
        "confidence": result.confidence,
        "warnings": result.warnings,
        "grounded_columns": result.grounded_columns,
    }


@mcp.tool()
async def interpret_transform_intent(
    intent: str,
    project_code: str | None = None,
    output_dataset_uri: str | None = None,
    mapping_uri: str | None = None,
    target_column: str = "",
    source_columns: list | None = None,
    lookup_tables: list | None = None,
    dialect: str = "postgres",
    notes: str = "",
) -> dict:
    """Interpret a plain-language column derivation into a structured transform payload.

    The engineer says "combine first and last name with a space" or "mask all but the
    last 4 of ssn" — this returns the structured DSL (`transform_kind`, `transform_inputs`
    resolved to source-column URIs, `transform_params`, `transform_decorators`,
    `transform_expression`), a plain-language `readback`, a confidence, and warnings, ready
    to apply via `review_mapping` (action=replace_mapping). When BOTH `project_code` and
    `output_dataset_uri` are given, the source columns feeding that dataset (and their
    observed profile values) are used to ground the inputs to real source-column URIs.
    `source_columns` is an optional list of {name, type, top_values, uri} dicts for context.
    Only the fixed 16-kind DSL is ever emitted; a raw expression is portability-checked.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .routers.transform_intent import interpret_transform_intent as _interpret

    project = None
    if project_code:
        if (denied := _deny_project(project_code)) is not None:
            return denied
        with Session(engine) as session:
            project = session.exec(
                select(Project).where(Project.project_code == project_code)
            ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}

    try:
        result = await _interpret(
            intent=intent,
            target_column=target_column,
            source_columns=list(source_columns or []),
            lookup_tables=list(lookup_tables or []),
            dialect=dialect,
            project=project,
            output_dataset_uri=output_dataset_uri,
            mapping_uri=mapping_uri,
            notes=notes,
        )
    except Exception as e:
        return {"error": f"Interpretation failed: {e}"}

    return {
        "readback": result.readback,
        "transform_kind": result.transform_kind,
        "transform_inputs": result.transform_inputs,
        "transform_params": result.transform_params,
        "transform_decorators": result.transform_decorators,
        "transform_expression": result.transform_expression,
        "confidence": result.confidence,
        "warnings": result.warnings,
        "grounded_columns": result.grounded_columns,
    }


@mcp.tool()
def run_gap_analysis(
    project_code: str,
    consumer_columns: list,
    candidate_contract_ids: list | None = None,
    consumer_idea: str = "",
    consumer_description: str = "",
) -> dict:
    """Run a pre-flight gap analysis comparing the consumer's schema columns
    against candidate source products — the MCP equivalent of step 8 in the
    consumer wizard.

    `consumer_columns` is a list of `{name, logical_type, description}` dicts.
    `candidate_contract_ids` is the list of source contract IDs the PO has
    bound (e.g. `["dpe-sa-06232026-01-contract"]`). Returns per-column status:
    `covered` / `derivable` / `ambiguous` / `gap`, with source evidence and
    rationale. A `_fallback: true` flag means the deterministic heuristic
    was used (the LLM skill wasn't available).
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.ingest_products import gap_analysis_for_project, GapAnalysisBody, GapAnalysisColumnInput

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        cols = []
        for c in consumer_columns:
            if isinstance(c, dict) and c.get("name"):
                cols.append(GapAnalysisColumnInput(
                    name=c["name"],
                    logical_type=c.get("logical_type") or c.get("type"),
                    description=c.get("description"),
                ))
        try:
            result = gap_analysis_for_project(
                project.id,
                GapAnalysisBody(
                    consumer_idea=consumer_idea,
                    consumer_description=consumer_description,
                    consumer_domain=project.domain or "",
                    consumer_columns=cols,
                    candidate_contract_ids=list(candidate_contract_ids or []),
                ),
                session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:
            return {"error": f"Gap analysis failed: {e}"}

    return result if isinstance(result, dict) else {"gaps": result}


@mcp.tool()
def match_inputs(spec: dict, inferred_dependencies: list | None = None) -> dict:
    """Rank published UPSTREAM products (source-aligned, aggregate, OR
    consumer-aligned — a consumer may build on any of them, forming multi-hop
    chains) against a consumer spec's declared inputs and inferred dependencies —
    the MCP equivalent of the Input Matching step in the consumer wizard.

    `spec` is the partial ODCS spec dict (needs at least `name`, `domain`,
    optionally `inputs[]`). `inferred_dependencies` is an optional list of
    `{name, domain, encompasses}` dicts from the archetype classifier. Each
    returned candidate carries its `product_kind` so you can tell the kinds
    apart. The consumer's own contract is excluded (a self-loop the DAG guard
    would reject).

    Returns per-slot `candidates[]` ranked by exact-URI → exact-name → semantic
    match, a `preselected_candidate_uri`, a `confidence_band` (strong / tentative /
    gap), and a `gap_suggestion` (prefill for NewSourceProductWizard) when no
    acceptable match is found.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.ingest_products import match_inputs as _match, MatchInputsBody

    with Session(engine) as session:
        try:
            result = _match(
                MatchInputsBody(spec=spec, inferred_dependencies=list(inferred_dependencies or [])),
                session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:
            return {"error": f"Input matching failed: {e}"}

    return result if isinstance(result, dict) else {"slots": []}


@mcp.tool()
def trigger_osi_score(
    project_code: str,
    skip_advisor: bool = True,
    trigger: str = "manual",
) -> dict:
    """Trigger an OSI readiness evaluation for a project's contract — the MCP
    equivalent of "Score now" in the Product Workbench.

    Runs the deterministic translate+validate+score pipeline. When `skip_advisor`
    is False, also runs the LLM advisor for a narrative + Apply card suggestions
    (adds ~30-60s). Returns band, completeness, conformance_pass, checklist, and
    any validation errors. Safe to call at any pipeline stage; earlier = faster
    feedback on spec completeness.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}

    try:
        from . import osi as osi_engine
        contract_id = f"{project_code}-contract"
        rubric_name = osi_engine.read_contract_rubric(project, contract_id)
        rubric = osi_engine.load_rubric(rubric_name)
        if rubric.get("requires_translation", True):
            osi_dict = osi_engine.translate_to_osi(project, contract_id)
            errors = osi_engine.validate_osi(osi_dict)
        else:
            osi_dict = {"version": osi_engine.OSI_VERSION, "semantic_model": []}
            errors = []
        score = osi_engine.score_rubric(osi_dict, errors, rubric, project=project, contract_id=contract_id)
        with Session(engine) as session2:
            project2 = session2.exec(select(Project).where(Project.project_code == project_code)).first()
        persisted = osi_engine.persist_evaluation(project2 or project, contract_id, None, score, None, trigger, rubric=rubric)
    except Exception as e:
        return {"error": f"OSI evaluation failed: {e}"}

    return {
        "ok": True,
        "project_code": project_code,
        "band": score.get("band"),
        "completeness": score.get("completeness"),
        "conformance_pass": score.get("conformance_pass"),
        "checklist": score.get("checklist"),
        "errors": score.get("errors"),
        "rubric": rubric.get("id") if rubric else None,
        "note": (
            "Deterministic eval only (skip_advisor=True). Pass skip_advisor=False for "
            "narrative + Apply card suggestions via the LLM advisor."
        ) if skip_advisor else None,
    }


@mcp.tool()
def ingest_odcs_spec(
    spec_content: str,
    owner_email: str,
    archetype: str = "dpe-sa",
    notes: str | None = None,
    input_selections: list | None = None,
    submit_to_engineer: bool = True,
) -> dict:
    """Import an existing data product from an ODCS v3.1 YAML or JSON spec —
    the MCP equivalent of the Ingest flow (upload a spec → wizard → commit).

    `spec_content` is the raw YAML or JSON string of the ODCS spec.
    `archetype` is 'dpe-sa' (default, auto-publishes) or 'dpe-cf' (lands at
    'submitted', requires all source inputs bound). For 'dpe-cf', pass
    `input_selections` as a list of `{slot_id, resolution, dprod_uri, contract_id,
    name}` dicts where every slot has `resolution='matched'`. Canonicalizes and
    validates the spec before committing.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.ingest_products import ingest_from_odcs, FromOdcsBody, InputSelection, _parse_raw, _canonicalize_v3_1

    archetype = (archetype or "dpe-sa").strip().lower()
    if archetype not in ("dpe-sa", "dpe-cf"):
        return {"error": "archetype must be 'dpe-sa' or 'dpe-cf'"}

    try:
        raw = _parse_raw(spec_content, None)
        spec = _canonicalize_v3_1(raw)
    except HTTPException as e:
        return {"error": f"{e.detail}"}
    except Exception as e:
        return {"error": f"Spec parse failed: {e}"}

    selections = []
    for s in (input_selections or []):
        if isinstance(s, dict) and s.get("slot_id"):
            selections.append(InputSelection(
                slot_id=s["slot_id"],
                resolution=s.get("resolution", "matched"),
                dprod_uri=s.get("dprod_uri"),
                contract_id=s.get("contract_id"),
                name=s.get("name"),
            ))

    with Session(engine) as session:
        try:
            result = ingest_from_odcs(
                FromOdcsBody(
                    spec=spec,
                    owner_email=owner_email,
                    notes=notes,
                    submit_to_engineer=submit_to_engineer,
                    archetype_override=archetype,
                    input_selections=selections,
                ),
                session,
            )
        except HTTPException as e:
            detail = e.detail
            if isinstance(detail, dict):
                return {"error": detail.get("message", "Ingest failed"), **{k: v for k, v in detail.items() if k != "message"}}
            return {"error": f"{detail}"}
        except Exception as e:
            return {"error": f"Ingest failed: {e}"}

    return {"ok": True, **(result if isinstance(result, dict) else {}),
            "next": "Call accept_request (archetype=dpe-sa) or run the pipeline (dpe-cf) after ingest."}


@mcp.tool()
def complete_product_request(
    project_code: str,
    completion_notes: str | None = None,
) -> dict:
    """Mark engineering complete for the project's latest product request —
    the MCP equivalent of the engineer clicking "Mark Engineering Complete".

    For dpe-sa: auto-publishes the product (lifecycle → published). For dpe-cf:
    parks at 'approved' and waits for the PO to click Deploy in the marketplace.
    Triggers a final OSI re-score on the contract. Use after all pipeline stages
    are complete (descriptions approved, mappings approved, serving done).
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.product_requests import complete_product_request as _complete, CompleteRequestBody
    from .models import ProductRequest, ProductRequestStatus

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        latest = session.exec(
            select(ProductRequest)
            .where(ProductRequest.project_id == project.id)
            .where(ProductRequest.status != ProductRequestStatus.rejected)
            .order_by(ProductRequest.submitted_at.desc())
        ).first()
        if latest is None:
            return {"error": "No active product request for this project."}
        if latest.status == ProductRequestStatus.complete:
            return {"ok": True, "status": "complete", "note": "already complete — no action taken."}
        try:
            result = _complete(latest.id, CompleteRequestBody(completion_notes=completion_notes), session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:
            return {"error": f"Complete failed: {e}"}

    status = result.get("status") if isinstance(result, dict) else None
    return {
        "ok": True,
        "request_id": latest.id,
        "status": status,
        "next": (
            "Product published — visible on the marketplace."
            if status == "published"
            else "Product approved — PO can now deploy from the marketplace."
        ),
    }


@mcp.tool()
def reject_product_request(
    project_code: str,
    reason: str,
    category: str = "other",
    engineer: str | None = None,
) -> dict:
    """Reject the project's latest product request back to the Product Owner —
    the MCP equivalent of the engineer clicking Reject in the Incoming queue.

    `reason` is a human-readable explanation the PO sees. `category` ∈
    missing_context | too_broad | too_narrow | unclear_purpose |
    unclear_quality_rules | duplicate | out_of_scope | other.
    The PO can re-submit after revising their spec.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.product_requests import reject_product_request as _reject, RejectRequestBody
    from .models import ProductRequest, ProductRequestStatus

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        latest = session.exec(
            select(ProductRequest)
            .where(ProductRequest.project_id == project.id)
            .where(ProductRequest.status != ProductRequestStatus.rejected)
            .order_by(ProductRequest.submitted_at.desc())
        ).first()
        if latest is None:
            return {"error": "No active product request to reject for this project."}
        try:
            result = _reject(
                latest.id,
                RejectRequestBody(reason=reason, category=category, engineer=engineer),
                session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:
            return {"error": f"Reject failed: {e}"}

    return {
        "ok": True,
        "request_id": latest.id,
        "status": "rejected",
        "category": category,
        "reason": reason,
        "next": "The PO can revise their spec and re-submit.",
    }


@mcp.tool()
def bulk_approve_mappings(
    project_code: str,
    role: str = "Data Engineer",
    quality: int = 3,
) -> dict:
    """Approve all pending column mappings for a consumer product in one call —
    the MCP equivalent of clicking Approve on every row in the Mapping review panel.

    After run_stage data_mapping completes, the AI-suggested mappings land at
    status='pending_review'. The serving stage requires status='approved' on every
    mapping — this tool bulk-approves them so you don't need N separate
    review_mapping calls.

    `quality` ∈ 1 (Acceptable) / 2 (Good) / 3 (Excellent), default 3 (Excellent).
    `role` must be 'Data Engineer' or 'Reviewer'.

    After this returns ok=true, run get_join_preflight before serving_virtual_view
    to check for cross-product join gaps.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    if (bad := _role_guard("mappings", role)) is not None:
        return bad
    from .routers.reviews import review_mapping as _h, MappingReviewAction, PENDING_MAPPINGS_QUERY_S
    from .neo4j_client import neo4j_session

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}

    try:
        with neo4j_session(
            project.neo4j_host, project.neo4j_port,
            project.neo4j_user, project.neo4j_password, project.neo4j_database,
        ) as ns:
            rows = list(ns.run(PENDING_MAPPINGS_QUERY_S, project_code=project_code))
    except Exception as e:
        return {"error": f"Failed to fetch pending mappings: {e}"}

    if not rows:
        return {
            "ok": True, "approved": 0,
            "note": "No pending mappings — nothing to approve. If mappings exist, they may already be approved.",
        }

    principal = _principal()
    approved: list[str] = []
    failed: list[dict] = []
    for row in rows:
        mapping_uri = row.get("mapping_uri")
        if not mapping_uri:
            continue
        body = MappingReviewAction(
            action="approve", mapping_uri=mapping_uri,
            quality=quality, reviewer=principal,
        )
        result = _call_review(_h, project_code, body)
        if result.get("error"):
            failed.append({"mapping_uri": mapping_uri, "error": result["error"]})
        else:
            approved.append(mapping_uri)

    return {
        "ok": len(failed) == 0,
        "approved": len(approved),
        "failed": len(failed),
        "failed_details": failed if failed else None,
        "next": (
            "All mappings approved. Run get_join_preflight before serving_virtual_view."
            if not failed
            else f"{len(failed)} mapping(s) failed — check failed_details and approve manually via review_mapping."
        ),
    }


# --- Read-back tools: make results inspectable in the terminal ----------------
# These close the "no-UI" gap — you can already RUN the whole pipeline via MCP,
# these let you SEE the results (score, deployed data) without opening the UI.
# CLI-UX philosophy: concise subset + anomaly-first triage + web_url escape hatch.

@mcp.tool()
def get_osi_evaluation(project_code: str) -> dict:
    """Read the latest OSI (AI-readiness) evaluation — the score you trigger with
    trigger_osi_score, made readable in the terminal.

    Triage-first: returns band + completeness, then ONLY the criteria that are
    failing/partial (what's dragging the score down, heaviest weight first) —
    passing criteria are summarised as a count, not dumped. `web_url` opens the
    full checklist + Apply cards in the UI.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .config import FRONTEND_URL
    from .routers.osi import get_latest_evaluation
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            raw = get_latest_evaluation(project.id, session)
        except HTTPException as e:
            return _err(e.detail)

    web_url = f"{FRONTEND_URL}/engineer/projects/{project.id}"
    ev = raw.get("evaluation")
    if not ev:
        return {
            "project_code": project_code,
            "scored": False,
            "rubric": raw.get("rubric"),
            "web_url": web_url,
            "next": "No OSI score yet. Call trigger_osi_score to evaluate this product.",
        }

    checklist = ev.get("checklist") or []
    passing = [c for c in checklist if c.get("status") == "pass"]
    attention = [c for c in checklist if c.get("status") in ("fail", "partial")]
    na = [c for c in checklist if c.get("status") == "na"]
    return {
        "project_code": project_code,
        "scored": True,
        "band": ev.get("band"),                    # green / amber / red
        "completeness": ev.get("completeness"),    # 0-100
        "conformance_pass": ev.get("conformance_pass"),
        "rubric": ev.get("rubric_label") or raw.get("rubric_label"),
        "criteria_summary": {
            "total": len(checklist),
            "passing": len(passing),
            "needs_attention": len(attention),
            "not_applicable": len(na),
        },
        # Triage: only what's dragging the score down, heaviest weight first.
        "needs_attention": [
            {
                "criterion": c.get("criterion"),
                "status": c.get("status"),
                "weight": c.get("weight"),
                "reason": (c.get("reason") or "")[:200],
            }
            for c in sorted(attention, key=lambda c: -(c.get("weight") or 0))
        ],
        "narrative_preview": (ev.get("narrative") or "")[:300] or None,
        "evaluated_at": ev.get("evaluated_at"),
        "web_url": web_url,
        "next": (
            "Score is GREEN — product is AI-ready."
            if ev.get("band") == "green"
            else f"{len(attention)} criteria need attention (see needs_attention). "
                 "Open web_url for the full checklist + Apply cards."
        ),
    }


@mcp.tool()
def preview_serving_view(project_code: str, dataset_uri: str = "", limit: int = 10) -> dict:
    """Preview real rows from a product's deployed view — proves the product
    actually serves data, without opening the UI.

    CLI-friendly: capped at 10 rows (a spot-check, not a data dump — open web_url
    for full exploration). Returns column names + the sample rows + row_count, or
    `deployed: false` when the view hasn't been deployed yet.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .config import FRONTEND_URL
    from .routers.serving import preview_engineer, PreviewBody
    capped = max(1, min(int(limit or 10), 10))
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            raw = preview_engineer(
                project.id,
                PreviewBody(dataset_uri=dataset_uri or None, limit=capped),
                session,
            )
        except HTTPException as e:
            return _err(e.detail)

    web_url = f"{FRONTEND_URL}/engineer/projects/{project.id}"
    if raw.get("error") == "not_deployed":
        return {
            "project_code": project_code,
            "deployed": False,
            "web_url": web_url,
            "next": "View not deployed yet. Run the serving + deploy stages first.",
        }
    if raw.get("status") == "failed":
        return {
            "project_code": project_code,
            "deployed": True,
            "preview_ok": False,
            "error_class": raw.get("error_class"),
            "error_message": raw.get("error_message"),
            "web_url": web_url,
        }

    cols = [c.get("name") for c in (raw.get("columns") or [])]
    rows = raw.get("rows") or []
    views = [d.get("view_name") for d in (raw.get("available_datasets") or [])]
    return {
        "project_code": project_code,
        "deployed": True,
        "preview_ok": True,
        "view_name": raw.get("view_name"),
        "columns": cols,
        "sample_rows": rows,            # already capped to `capped`
        "returned_rows": len(rows),
        "truncated": raw.get("truncated"),
        "available_views": views if len(views) > 1 else None,
        "web_url": web_url,
        "next": (
            f"Showing {len(rows)} sample row(s) from {raw.get('view_name')}. "
            "Open web_url for full data + column descriptions."
            + (" Multiple views available — pass dataset_uri to switch." if len(views) > 1 else "")
        ),
    }


@mcp.tool()
def get_dq_score(project_code: str) -> dict:
    """Read the latest Data Quality score for a product — the DQV/scoring result,
    made readable in the terminal.

    Returns the overall per-dimension scores + tier, then a compact per-dataset
    summary (dataset name + its scores) rather than the full column-level matrix.
    `web_url` opens the full Quality panel. Distinct from OSI (AI-readiness) — this
    is the data-quality score.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .config import FRONTEND_URL
    from .routers.scoring import get_scores
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            raw = get_scores(project.id, session)
        except HTTPException as e:
            return _err(e.detail)

    web_url = f"{FRONTEND_URL}/engineer/projects/{project.id}"
    if not raw.get("batch_id"):
        return {
            "project_code": project_code,
            "scored": False,
            "web_url": web_url,
            "next": "No DQ score yet. Add + run the DQ workflows (rules → testing → scoring).",
        }
    datasets = [
        {"dataset": d.get("dataset_uri") or d.get("name"), "scores": d.get("scores") or d}
        for d in (raw.get("datasets") or [])
    ]
    return {
        "project_code": project_code,
        "scored": True,
        "tier": raw.get("tier"),
        "tier_label": raw.get("tier_label"),
        "overall": raw.get("overall"),
        "scored_at": raw.get("scored_at"),
        "dataset_count": len(datasets),
        "datasets": datasets[:20],   # compact — full column matrix lives in the UI
        "web_url": web_url,
        "next": "Open web_url for the per-column score matrix and evidence.",
    }


@mcp.tool()
def get_dq_test_runs(project_code: str) -> dict:
    """Read the latest DQ test-run rollup — pass rate + rule coverage, made
    readable in the terminal.

    Returns the most recent batch's pass_rate, coverage, and counts (the
    decision-level view). The full per-rule pass/fail breakdown lives in the UI —
    `web_url` links there.

    Counts come from the SQLite DQTestRun ledger (authoritative for any served
    platform, including warehouse targets whose :TestResult graph load is a
    graceful skip); rule_coverage is layered in from Neo4j when available.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from sqlmodel import select
    from .config import FRONTEND_URL
    from .models import DQTestRun
    from .routers.test_runs import test_run_summary
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        latest = session.exec(
            select(DQTestRun)
            .where(DQTestRun.project_id == project.id)
            .order_by(DQTestRun.started_at.desc())
        ).first()
        # Neo4j coverage is enrichment only — never gates has_runs.
        coverage = 0.0
        try:
            graph_roll = test_run_summary(project.id, session)
            if graph_roll.get("has_runs"):
                coverage = graph_roll.get("coverage") or 0.0
        except Exception:
            pass

    web_url = f"{FRONTEND_URL}/engineer/projects/{project.id}"
    if latest is None:
        return {
            "project_code": project_code,
            "has_runs": False,
            "web_url": web_url,
            "next": "No DQ test runs yet. Add + run the DQ testing workflow.",
        }
    total = latest.total_expectations or 0
    failed = latest.unsuccessful or 0
    ran_at = latest.completed_at or latest.started_at
    return {
        "project_code": project_code,
        "has_runs": True,
        "framework": latest.framework,
        "executed_at": ran_at.isoformat() if ran_at else None,
        "total_expectations": total,
        "passed": latest.successful,
        "failed": failed,
        "tables_tested": latest.tables_tested,
        "pass_rate": round((latest.successful / total) * 100, 1) if total else 0.0,  # %
        "rule_coverage": round(coverage * 100, 1),   # %
        "web_url": web_url,
        "next": (
            f"{failed} expectation(s) failed — run get_dq_package to retrieve the test code "
            "and failure_analysis.md, or open web_url for the per-rule breakdown."
            if failed else
            "All expectations passed — use get_dq_package to download the test suite or push_to_git "
            "to publish it alongside the serving artifacts."
        ),
    }


@mcp.tool()
def get_dq_package(project_code: str, source_mode: str | None = None) -> dict:
    """Return the generated DQ test package as a {files: {path: content}} map —
    the same bundle the "Download DQ Package" button streams as a zip.

    The package contains the test code (Great Expectations suite or Pandera
    validators), a framework-specific README, and helper scripts — but NOT the
    `results/` subdirectory (run-time output). Write the files to a local dir
    then follow the README to install dependencies and execute the tests.

    `source_mode` picks the suite: `catalog` (source pre-check, from the
    `dq_testing` workflow) or `dprod` (deployed-product, from `product_dq_testing`
    — the dprod suite tests the `vw_*` views against the contract rules). A `dpe-sa`
    product can hold both; omit `source_mode` to auto-pick (dprod preferred).

    Returns `{"ok": false, "next": "..."}` when the DQ test generation stage has
    not been run yet. Use `add_workflow(project_code, "dq_testing")` (catalog) or
    `add_workflow(project_code, "product_dq_testing")` (dprod), then run the DQ
    test generation stage first.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied

    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}

    try:
        from .dq_package import resolve_suite, collect_dq_files
    except Exception as e:
        return {"error": f"DQ package module unavailable: {e}"}

    framework, mode = resolve_suite(project.project_code, source_mode)
    if framework is None:
        return {
            "ok": False,
            "project_code": project_code,
            "next": (
                "No DQ test files found. Add and run a DQ testing workflow first: "
                "add_workflow(project_code, 'dq_testing') (catalog) or "
                "add_workflow(project_code, 'product_dq_testing') (dprod), then run "
                "the DQ test generation stage."
            ),
        }

    try:
        files = collect_dq_files(project.project_code, framework, mode)
    except Exception as e:
        return {"error": f"Could not collect DQ test files: {e}"}

    return {
        "ok": True,
        "project_code": project_code,
        "framework": framework,
        "source_mode": mode,
        "file_count": len(files),
        "files": files,
        "next": (
            f"Write the {len(files)} files to a local dir. "
            f"See README.md for install + run instructions ({framework} framework, {mode} suite)."
        ),
    }


@mcp.tool()
def get_dq_rules(project_code: str, table: str | None = None, approved_only: bool = True) -> dict:
    """Return a structured per-table/column DQ rule breakdown from the knowledge
    graph — the rule-level detail behind get_dq_test_runs' rollup.

    Each rule carries: table_name, column_name, rule_type, severity,
    rule_source (observation | domain | user | spec), rule_status, description.
    Optionally filter by `table` name; set `approved_only=False` to include
    pending and rejected rules.

    Returns `{"has_rules": false}` when no DQ rules have been generated yet —
    run `add_workflow(project_code, "baseline_dq_rules")` then the DQ rule
    generation stage first.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied

    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}

    try:
        from .neo4j_client import neo4j_session as _neo4j
        from .routers.summary import (
            DQ_RULES_DETAIL_S, DQ_RULES_DETAIL, DQ_RULES_DETAIL_DPROD, _pick,
        )
        scoped = bool(project.project_code)
        # Consumer-aligned products keep their rules on :DProdColumn (the contract),
        # not a :Column catalog — branch to the dprod variant so product rules are
        # visible over MCP too. (Extends this tool; no new tool → docs count stable.)
        if (getattr(project, "archetype", "") or "") == "dpe-cf":
            query = DQ_RULES_DETAIL_DPROD
        else:
            query = _pick(scoped, DQ_RULES_DETAIL_S, DQ_RULES_DETAIL)
        with _neo4j(project.neo4j_host, project.neo4j_port,
                    project.neo4j_user, project.neo4j_password,
                    project.neo4j_database) as ns:
            rows = [dict(r) for r in ns.run(
                query,
                pc=project.project_code,
                project_code=project.project_code,
                table=table,
                column=None,
                severity=None,
                source=None,
            )]
    except Exception as e:
        return {"error": f"Graph query failed: {e}"}

    if approved_only:
        rows = [r for r in rows if r.get("rule_status") in ("approved", None)]

    if not rows:
        return {
            "project_code": project_code,
            "has_rules": False,
            "next": (
                "No DQ rules found (approved). Run the DQ rule generation stage "
                "(add_workflow(project_code, 'baseline_dq_rules') if not present) "
                "and re-run data profiling to populate :PropertyShape nodes."
            ),
        }

    # Group by table for a compact overview
    by_table: dict[str, list[dict]] = {}
    for r in rows:
        tbl = r.get("table_name") or "unknown"
        by_table.setdefault(tbl, []).append({
            "column": r.get("column_name"),
            "rule_type": r.get("rule_type"),
            "severity": r.get("severity"),
            "source": r.get("rule_source"),
            "status": r.get("rule_status"),
            "description": r.get("description"),
            "rule_uri": r.get("rule_uri"),
        })

    return {
        "project_code": project_code,
        "has_rules": True,
        "total_rules": len(rows),
        "table_count": len(by_table),
        "approved_only": approved_only,
        "tables": [
            {"table": tbl, "rule_count": len(rules), "rules": rules}
            for tbl, rules in sorted(by_table.items())
        ],
        "next": (
            f"{len(rows)} rules across {len(by_table)} table(s). "
            "Use get_dq_test_runs for execution results, or get_dq_package for the test code."
        ),
    }


@mcp.tool()
def get_deployment_reflection(project_code: str) -> dict:
    """Read the latest post-deploy AI reflection — the verdict on whether the
    deployed data matches the declared shape, made readable in the terminal.

    Returns the verdict + a narrative preview, then the surprises and
    recommendations (the actionable parts) as lists. Alignment detail + full
    narrative live in the UI — `web_url` links there.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .config import FRONTEND_URL
    from .routers.marketplace import marketplace_reflection_latest
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        contract_id = f"{project.project_code}-contract"
        try:
            raw = marketplace_reflection_latest(contract_id, session)
        except HTTPException as e:
            return _err(e.detail)
        project_id = project.id

    web_url = f"{FRONTEND_URL}/engineer/projects/{project_id}"
    refl = raw.get("reflection")
    if not refl:
        return {
            "project_code": project_code,
            "has_reflection": False,
            "web_url": web_url,
            "next": "No reflection yet. Run the deployment_reflection stage after deploy.",
        }
    return {
        "project_code": project_code,
        "has_reflection": True,
        "verdict": refl.get("verdict"),
        "narrative_preview": (refl.get("narrative") or "")[:400] or None,
        "surprises": refl.get("surprises") or [],
        "recommendations": refl.get("recommendations") or [],
        "surprise_count": len(refl.get("surprises") or []),
        "recommendation_count": len(refl.get("recommendations") or []),
        "evaluated_at": refl.get("evaluated_at"),
        "web_url": web_url,
        "next": (
            "Open web_url for the full narrative + description/rule/QA alignment detail."
        ),
    }


@mcp.tool()
async def run_deployment_reflection(project_code: str, trigger: str = "manual") -> dict:
    """Generate a FRESH post-deploy AI reflection headlessly — the MCP equivalent
    of the deployment_reflection stage's Run (and the panel's Re-run button).

    Gathers the deployed product's declared shape + a sample of live rows, runs
    the data-product-deployment-reflector skill, and persists the
    :DeploymentReflection verdict. NOTE: complete_stage only *marks* the stage
    done — this tool actually produces the report. Requires a deployed product
    (a served view/tables to sample), else returns an error. Read the result back
    with get_deployment_reflection."""
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.serving import run_reflection as _run, _RunReflectionBody
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            raw = await _run(project.id, _RunReflectionBody(trigger=trigger), session)
        except HTTPException as e:
            return _err(e.detail)
        except Exception as e:  # noqa: BLE001
            return {"error": f"Reflection failed: {e}"}
    return {
        "ok": raw.get("status") == "ok",
        "project_code": project_code,
        "verdict": raw.get("verdict"),
        "narrative_preview": (raw.get("narrative") or "")[:400] or None,
        "surprises": raw.get("surprises") or [],
        "recommendations": raw.get("recommendations") or [],
        "advisor_error": raw.get("advisor_error"),
        "next": "Read the full verdict + alignment detail with get_deployment_reflection.",
    }


# --- P2: marketplace detail / gaps, usage, stage history, edit diff -----------
# Read/observability tools. Same CLI-UX rules: concise subset + counts + web_url.

@mcp.tool()
def get_marketplace_detail(uri: str, version: int | None = None) -> dict:
    """Detailed view of one published product — the concise CLI form of the
    marketplace detail page.

    `uri` is the product uri from list_marketplace_products (e.g.
    'dprod:dpe-...-contract'). Returns the header (name/domain/kind/osi), a
    per-dataset column-count summary, quality-rule count, SLAs, and CONSUMES
    cross-refs — NOT the full column matrix (that's behind web_url).
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from urllib.parse import quote
    from .config import FRONTEND_URL
    from .routers.marketplace import get_product_detail
    with Session(engine) as session:
        try:
            raw = get_product_detail(uri=uri, version=version, session=session)
        except HTTPException as e:
            return _err(e.detail)

    d = raw if isinstance(raw, dict) else {}
    datasets = [
        {"name": ds.get("name") or ds.get("physicalName"),
         "column_count": len(ds.get("columns") or [])}
        for ds in (d.get("datasets") or [])
    ]
    return {
        "uri": d.get("uri") or uri,
        "name": d.get("name"),
        "domain": d.get("domain"),
        "product_kind": d.get("product_kind"),
        "osi_band": d.get("osi_band"),
        "lifecycle_state": d.get("lifecycle_state"),
        "description": (d.get("description") or "")[:200] or None,
        "datasets": datasets,
        "column_total": sum(x["column_count"] for x in datasets),
        "quality_rule_count": len(d.get("quality_rules") or []),
        "slas": d.get("slas") or [],
        "consumes": d.get("consumes") or [],
        "consumed_by": d.get("consumed_by") or [],
        "owners": [o.get("name") or o.get("email") for o in (d.get("owners") or [])],
        "web_url": f"{FRONTEND_URL}/product/marketplace/{quote(uri, safe='')}",
        "next": "Open web_url for the full column matrix, lineage canvas, and Quality tab.",
    }


@mcp.tool()
def log_marketplace_gap(
    domain: str,
    question: str,
    product_uri: str = "",
    contract_id: str = "",
    refused_reason: str = "",
    created_by: str = "",
) -> dict:
    """Log a marketplace gap — a question the product/semantic layer couldn't
    answer — so the producer can triage it. `domain` + `question` required.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.marketplace import create_gap, _GapCreateBody
    with Session(engine) as session:
        try:
            result = create_gap(
                _GapCreateBody(
                    domain=domain, question=question,
                    product_uri=product_uri or None, contract_id=contract_id or None,
                    refused_reason=refused_reason or None,
                    created_by=created_by or _principal(),
                ),
                session,
            )
        except HTTPException as e:
            return _err(e.detail)
    return {"ok": True, "gap_id": result.get("id"), "status": result.get("status"),
            "next": "Producer triages this via list_marketplace_gaps."}


@mcp.tool()
def list_marketplace_gaps(status: str = "", audience: str = "", domain: str = "") -> dict:
    """List logged marketplace gaps (newest first). Default view = active backlog
    (open + triaged); pass status to filter. Includes web_url to the Gaps page.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .config import FRONTEND_URL
    from .routers.marketplace import list_gaps
    with Session(engine) as session:
        try:
            raw = list_gaps(status=status or None, audience=audience or None,
                            domain=domain or None, session=session)
        except HTTPException as e:
            return _err(e.detail)
    gaps = raw.get("gaps") if isinstance(raw, dict) else raw
    gaps = gaps or []
    return {
        "gaps": gaps,
        "count": len(gaps),
        "open_count": raw.get("open_count") if isinstance(raw, dict) else None,
        "web_url": f"{FRONTEND_URL}/product/gaps",
        "next": "Open web_url to triage/resolve gaps." if gaps else "No open gaps.",
    }


@mcp.tool()
def get_usage_summary(project_code: str = "", domain: str = "") -> dict:
    """LLM token + cost totals — system-wide, or scoped to a product
    (`project_code`) or `domain`. Includes a per-source breakdown. Answers
    'how much did this cost' without opening the Settings usage card.

    Scope: a project-scoped token must pass `project_code=` (or `domain=`) — it is
    authorized against that scope. System-wide totals (no filter) require an
    unrestricted token, so a scoped token can't read another project's cost.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    if project_code:
        if (denied := _deny_project(project_code)) is not None:
            return denied
    elif domain:
        if (denied := _deny_domain(domain)) is not None:
            return denied
    elif _authorized_projects() is not None:
        return {"error": (
            "Your token is project-scoped — pass project_code= (or domain=) to read "
            "usage. System-wide totals require an unrestricted token."
        )}
    from fastapi import HTTPException
    from .routers.usage import usage_summary
    with Session(engine) as session:
        try:
            return usage_summary(
                project_code=project_code or None, domain=domain or None, session=session,
            )
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_stage_executions(project_code: str, stage_number: int, workflow_id: str = "") -> dict:
    """Run history for a single stage — every execution with status, cost,
    tool-call counts, and event count. Use to debug a stage that failed or
    behaved oddly. Get the full transcript of one run in the UI (web_url).
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .config import FRONTEND_URL
    from .routers.stage_executions import list_executions
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            rows = list_executions(project.id, stage_number, workflow_id or None, session)
        except HTTPException as e:
            return _err(e.detail)
        project_id = project.id
    return {
        "project_code": project_code,
        "stage_number": stage_number,
        "executions": rows,
        "count": len(rows),
        "web_url": f"{FRONTEND_URL}/engineer/projects/{project_id}",
        "next": "Open web_url and select an execution for its full event transcript.",
    }


@mcp.tool()
def get_edit_diff(project_code: str) -> dict:
    """Diff between the deployed contract version and the current head — what an
    in-flight edit changed, plus the stages that need re-running. Returns
    `has_edit: false` when there's no in-flight edit.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .config import FRONTEND_URL
    from .routers.edits import get_edit_diff as _h
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = _h(project.id, session)
        except HTTPException as e:
            return _err(e.detail)
        project_id = project.id
    if isinstance(result, dict):
        result.setdefault("web_url", f"{FRONTEND_URL}/engineer/projects/{project_id}")
    return result


# --- Deploy the virtual view (was UI-only — required for a no-UI dpe-sa/cf flow)

@mcp.tool()
def deploy_virtual_view(project_code: str, view_schema: str = "") -> dict:
    """Deploy the authored virtual view (CREATE OR REPLACE VIEW) to the source
    Postgres — the MCP equivalent of the engineer's Deploy button. Run AFTER
    serving_virtual_view completes.

    On a successful deploy this also marks the deploy_virtual_view stage complete
    so the pipeline advances (the deploy handler updates the ServingDefinition but
    not the StageRun — the UI flips the stage after deploy; this mirrors that).
    Returns the deployed view names, or a clean {status:'failed', error_*}.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .config import FRONTEND_URL
    from .routers.serving import deploy_virtual_view as _deploy, DeployBody
    from .routers.stages import complete_stage as _complete_handler, _resolve_stage_id
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        project_id = project.id
        try:
            body = DeployBody(view_schema=view_schema) if view_schema else DeployBody()
            result = _deploy(project_id, body, session, user=_principal())
        except HTTPException as e:
            return _err(e.detail)
        except Exception as e:  # noqa: BLE001
            return {"error": f"Deploy failed: {e}"}

        stage_note = None
        if isinstance(result, dict) and result.get("status") == "deployed":
            # Find the deploy_virtual_view stage number and mark it complete so
            # downstream (reflection / mark_engineering_complete) unblocks.
            try:
                import json as _json
                from .models import Workflow
                wf_id, num = None, None
                workflows = session.exec(
                    select(Workflow).where(Workflow.project_id == project_id)
                ).all()
                for w in workflows:
                    if not w.workflow_json:
                        continue
                    stages = [s for s in _json.loads(w.workflow_json) if s.get("enabled", True)]
                    for i, s in enumerate(stages, start=1):
                        if s.get("stage_id") == "deploy_virtual_view":
                            wf_id, num = w.workflow_id, i
                            break
                    if num:
                        break
                if num is not None:
                    _complete_handler(project_id, num, wf_id, session)
                    stage_note = "deploy_virtual_view stage marked complete"
                else:
                    stage_note = "deployed, but couldn't locate the deploy stage to flip (mark it via complete_stage)"
            except Exception as e:  # noqa: BLE001
                stage_note = f"deployed, but stage-complete flip failed: {e}"

    out = {"project_code": project_code, **(result if isinstance(result, dict) else {})}
    if stage_note:
        out["stage_note"] = stage_note
    out["web_url"] = f"{FRONTEND_URL}/engineer/projects/{project_id}"
    out["next"] = (
        "View deployed. Optionally run deployment_reflection, then complete_stage "
        "mark_engineering_complete. Inspect with preview_serving_view."
        if (isinstance(result, dict) and result.get("status") == "deployed")
        else "Deploy failed — see error_message. Fix and re-run."
    )
    return out


@mcp.tool()
def push_to_git(project_code: str, mode: str | None = None) -> dict:
    """Push the product's serving artifacts (view/dbt/lakehouse code) + OKF docs
    + ODCS spec to the configured git repository — one repo per product, named
    after the project code, each push tagged with the contract version. The MCP
    equivalent of the engineer's "Push to Git" button. Requires Git integration
    configured in Settings (provider/base URL/token/org). Run AFTER a deploy or
    materialization. Returns {repo_url, commit_sha, tag, pushed_files}.

    DQ test code is included automatically when it exists: if the DQ testing
    workflow has been run, the generated test files (Great Expectations suite or
    Pandera validators) are bundled alongside the serving artifacts — no extra
    step required.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .git_provider import GitProviderError
    from .routers.serving import _push_product_to_git, _app_settings
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        settings = _app_settings(session)
        try:
            result = _push_product_to_git(project, settings, _principal(), "", session=session)
        except GitProviderError as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"error": f"Git push failed: {e}"}
    return {"project_code": project_code, **result}


@mcp.tool()
def push_to_object_store(project_code: str) -> dict:
    """Publish the project's generated DATA artifacts (Parquet + manifests) to its
    bound S3-compatible object store, under an immutable run prefix, flipping
    latest.json to the new run (ADR-14). The MCP equivalent of the engineer's
    "Publish to Object Store" button and the complement of push_to_git: git
    carries the recipe (view/dbt/lakehouse CODE + docs), the object store carries
    the DATA. Requires an artifact-store binding to an object-store connection
    (PUT /api/projects/{id}/artifact-store-binding). Run AFTER a lakehouse export
    or Parquet export has produced files. Returns {run_id, bucket, run_prefix,
    object_count, bytes_uploaded, latest_json_uri, status}.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from . import object_store_publish
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = object_store_publish.publish_project_artifacts(
                project, session, actor=_principal())
        except object_store_publish.PublishError as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"error": f"Object-store publish failed: {e}"}
    # Drop the (potentially large) presigned_urls map from the MCP payload — an
    # agent can't click them; the REST endpoint returns them for the UI.
    result.pop("presigned_urls", None)
    return {"project_code": project_code, **result}


# --- P3: lineage, QA probe/execute, ingest drafts, provenance -----------------

@mcp.tool()
def get_marketplace_lineage(uri: str) -> dict:
    """Consumer-friendly lineage for a product — which source tables/columns feed
    it, plus lookup reference tables. `uri` is the dprod uri from
    list_marketplace_products. The visual canvas is in the UI (web_url).
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from urllib.parse import quote
    from .config import FRONTEND_URL
    from .routers.marketplace import get_lineage
    with Session(engine) as session:
        try:
            raw = get_lineage(uri=uri, session=session)
        except HTTPException as e:
            return _err(e.detail)
    sources = raw.get("sources") or []
    return {
        "uri": uri,
        "sources": sources,
        "source_count": len(sources),
        "stats": raw.get("stats") or {},
        "web_url": f"{FRONTEND_URL}/product/marketplace/{quote(uri, safe='')}",
        "next": "Open web_url for the visual lineage canvas.",
    }


@mcp.tool()
def get_product_lineage() -> dict:
    """Marketplace-wide PRODUCT dependency graph — how data products relate to
    one another via :CONSUMES (source → aggregate → consumer). Coarse: nodes are
    whole products, edges are product-to-product (NO tables/columns/mappings —
    for those use get_marketplace_lineage per product).

    Returns `{nodes, edges}`: each node is `{uri, name, product_kind, domain,
    tags, lifecycle_state}`; each edge is `{source, target, kind:"consumes"}`
    in DATA-FLOW direction (upstream source → downstream consumer). The visual
    canvas is the marketplace Lineage view in the UI.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.marketplace import get_product_lineage as _get_product_lineage
    with Session(engine) as session:
        try:
            result = _get_product_lineage(session=session)
        except HTTPException as e:
            return _err(e.detail)
    nodes = result.get("nodes") or []
    edges = result.get("edges") or []
    return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}


@mcp.tool()
async def probe_qa_question(uri: str, question: str) -> dict:
    """Probe whether a free-form question CAN be answered by a product before
    executing it — returns answerable / partial / no + analysis. `uri` is the
    dprod uri. Ephemeral (never persisted). Cheaper than execute_qa_question.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.marketplace import probe_product_question, _MarketplaceProbeBody
    with Session(engine) as session:
        try:
            return await probe_product_question(
                _MarketplaceProbeBody(uri=uri, question=question), session,
            )
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
async def execute_qa_question(
    contract_id: str,
    question_text: str = "",
    question_index: int | None = None,
    limit: int = 10,
) -> dict:
    """Answer a question against a deployed product — NL→SQL, validated and run,
    returning the actual rows. Provide `question_text` (free-form) or
    `question_index` (into the curated :QAEvaluation set). `contract_id` e.g.
    'dpe-...-contract'. Rows capped for the terminal.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.marketplace import marketplace_qa_execute, _ExecuteQaBody
    capped = max(1, min(int(limit or 10), 25))
    with Session(engine) as session:
        try:
            raw = await marketplace_qa_execute(
                contract_id,
                _ExecuteQaBody(
                    question_text=question_text or None,
                    question_index=question_index,
                    limit=capped,
                ),
                session,
            )
        except HTTPException as e:
            # Typed empty state (Pattern 6): no curated QA set generated yet.
            detail = e.detail if isinstance(e.detail, str) else str(e.detail)
            if "QA evaluation" in detail or "generate questions" in detail:
                return {
                    "has_qa": False,
                    "next": "No QA questions generated for this product yet. Run the "
                            "question-analyzer (QA generate) first, or ask via "
                            "query_semantic_layer for a domain-wide NL→SQL answer.",
                }
            return _err(e.detail)
    if not isinstance(raw, dict):
        return {"error": "Unexpected QA response"}
    rows = raw.get("rows") or []
    return {
        "status": raw.get("status") or raw.get("error"),
        "question_text": raw.get("question_text") or question_text,
        "sql": raw.get("sql"),
        "columns": raw.get("columns"),
        "sample_rows": rows[:capped],
        "returned_rows": len(rows[:capped]),
        "row_count": raw.get("row_count"),
        "explanation": (raw.get("explanation") or "")[:300] or None,
        "confidence": raw.get("confidence"),
        "refused_reason": raw.get("refused_reason"),
        "error_message": raw.get("error_message"),
    }


@mcp.tool()
def list_ingest_drafts(owner_email: str, status: str = "") -> dict:
    """List in-flight ODCS ingest drafts for a PO (the /product/ingest flow),
    newest-updated first. Filter by status.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.ingest_products import list_drafts
    with Session(engine) as session:
        try:
            return list_drafts(owner_email=owner_email, status=status or None, session=session)
        except HTTPException as e:
            return _err(e.detail)


@mcp.tool()
def get_provenance(project_code: str, col_uri: str = "", mapping_uri: str = "") -> dict:
    """W3C PROV-O audit trail for a description (`col_uri`) or a mapping
    (`mapping_uri`) — who did what, when, and why. Provide exactly one uri.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    if not col_uri and not mapping_uri:
        return {"error": "Provide col_uri or mapping_uri"}
    from fastapi import HTTPException
    from .routers.summary import get_provenance as _h
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _h(project.id, col_uri=col_uri or None,
                      mapping_uri=mapping_uri or None, session=session)
        except HTTPException as e:
            return _err(e.detail)


# --- Output visibility: per-stage reviewable markdown "Step Report" -----------

@mcp.tool()
def get_stage_output(project_code: str, stage_id: str) -> dict:
    """A reviewable markdown 'Step Report' of what a stage PRODUCED — the output-
    visibility artifact. After a stage completes, call this to see (and show the
    user) a readable summary before moving on: discovered schema, profiling stats,
    descriptions, column mappings, the generated view SQL, deployed views, DQ
    rules, etc.

    Reuses the same vetted per-card queries as get_stage_results, rendered as
    markdown you can review, save to a file, or paste to the user. `stage_id`
    comes from get_project_state / get_plan_summary. Reportable stages include
    data_discovery, data_profiling, metadata_enrichment, column_name_standardization,
    odcs_to_dprod, data_mapping, serving_virtual_view, deploy_virtual_view.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .stage_output import build_stage_report
    from .config import FRONTEND_URL
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        project_id = project.id
        try:
            report = build_stage_report(project, stage_id)
        except Exception as e:  # noqa: BLE001
            return {"error": f"Stage report failed: {e}"}
    report["web_url"] = f"{FRONTEND_URL}/engineer/projects/{project_id}"
    report["next"] = (
        "Review the markdown. If it looks right, continue to the next stage; if not, "
        "reset_stage + re-run (or edit) before moving on."
    )
    return report


# --- DE analysis console: ad-hoc read-only SQL -------------------------------

@mcp.tool()
def run_readonly_sql(project_code: str, sql: str, limit: int = 50) -> dict:
    """Run an ad-hoc READ-ONLY SELECT against the product's source/served Postgres —
    an analysis console for the engineer, with no UI equivalent. Use it to profile or
    spot-check source tables and deployed views while you work (row counts, distinct
    values, sanity-checks) without leaving the session.

    SAFETY: single SELECT only (anything else refused), executed inside a read-only
    transaction and wrapped with a row LIMIT — it cannot write, and can't return
    unbounded rows. `limit` capped at 200. For product Q&A prefer query_semantic_layer;
    for a deployed view's rows, preview_serving_view.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from . import sql_executor
    from .routers.connections import resolve_source_connection_ref, build_connection_string
    from .neo4j_client import neo4j_session
    capped = max(1, min(int(limit or 50), 200))
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            _platform_type, _conn_ref = resolve_source_connection_ref(project, session)
        except Exception as e:  # noqa: BLE001
            return {"error": f"No connection: {e}"}
        proj = project
    # Transient Postgres DSN derived from the structured ref; non-Postgres rides
    # connection_ref into the executor.
    source_dsn = (build_connection_string(_platform_type, _conn_ref)
                  if _platform_type in ("postgres", "postgresql") else "")
    if _platform_type in ("postgres", "postgresql") and not source_dsn:
        return {"error": "No PostgreSQL connection available for this project."}
    try:
        with neo4j_session(proj.neo4j_host, proj.neo4j_port, proj.neo4j_user,
                           proj.neo4j_password, proj.neo4j_database) as ns:
            result = sql_executor.execute_select(
                neo4j_session=ns, project_code=project_code, pg_connection=source_dsn,
                sql=sql, executed_by=_principal(), max_rows=capped,
                platform=_platform_type,
                connection_ref=(_conn_ref if _platform_type not in ("postgres", "postgresql") else None),
            )
    except Exception as e:  # noqa: BLE001
        return {"error": f"Query failed: {e}"}
    if result.status != "ok":
        return {
            "ok": False,
            "error_class": result.error_class,
            "error_message": result.error_message,
            "next": ("Only a single SELECT is allowed." if result.error_class == "not_select"
                     else "Fix the SQL and retry."),
        }
    cols = [c.get("name") if isinstance(c, dict) else c for c in (result.columns or [])]
    return {
        "ok": True,
        "columns": cols,
        "rows": result.rows,
        "returned_rows": len(result.rows or []),
        "truncated": result.truncated,
        "duration_ms": result.duration_ms,
        "borrowed_pg_from": borrowed,
        "next": "Read-only spot-check complete. (SELECT-only, capped — safe for exploration.)",
    }


@mcp.tool()
def list_platforms() -> dict:
    """Return all registered data platforms with their capability levels.

    Use this to discover which platforms are available for connections,
    discovery, query execution, and serving.  `usable` is True only for
    PREVIEW or CERTIFIED capabilities — anything else is not yet deployable.
    """
    from .platform.registry import get_registry
    reg = get_registry()
    platforms = []
    for pid in reg.platform_ids():
        m = reg.get_manifest(pid)
        platforms.append({
            "platform_id": pid,
            "display_name": m.display_name,
            "adapter_version": m.adapter_version,
            "capabilities": {cap: lvl.value for cap, lvl in m.capabilities.items()},
            "usable": {cap: reg.is_usable(pid, cap) for cap in m.capabilities},
        })
    return {
        "platforms": platforms,
        "count": len(platforms),
        "next": "Use get_platform_capabilities to inspect a specific platform.",
    }


@mcp.tool()
def get_platform_capabilities(platform_id: str) -> dict:
    """Return the full capability manifest for one platform.

    Unknown platform → returns an error (never silently falls back to another
    platform).  Use list_platforms to enumerate what's registered.
    """
    from .platform.registry import get_registry
    reg = get_registry()
    m = reg.get_manifest(platform_id)
    if m is None:
        return {
            "error": f"Platform '{platform_id}' is not registered.",
            "known_platforms": reg.platform_ids(),
        }
    return {
        "platform_id": m.id,
        "display_name": m.display_name,
        "adapter_version": m.adapter_version,
        "adapter_kind": m.adapter_kind,
        "supported_server_versions": m.supported_server_versions,
        "namespace": m.namespace,
        "capabilities": {cap: lvl.value for cap, lvl in m.capabilities.items()},
        "usable": {cap: reg.is_usable(m.id, cap) for cap in m.capabilities},
        "notes": m.notes,
    }


@mcp.tool()
def get_transfer_batch_schema() -> dict:
    """Return the TransferBatch v1 JSON schema (ADR-13 reviewable gate).

    TransferBatch is the versioned data-plane exchange contract that will
    carry Parquet manifests and lakehouse evidence in Phase 3.  Call this
    to inspect the schema before Phase 3 implementation is started, or to
    validate a manifest produced by a future export skill.

    Fields of interest:
    - ``source_asset_ref`` — discriminated union (relational_relation |
      lakehouse_table | file_collection) identifying the data origin.
    - ``file_uris`` / ``file_checksums`` — Parquet file addresses and SHA-256.
    - ``schema_fingerprint`` — hash for detecting schema drift between batches.
    - ``completion_marker`` — incomplete | complete | partial | failed.
    - ``incremental_state_before/after`` — checkpoint for resumable transfers.

    Returns the full JSON Schema dict plus a human-readable field summary.
    """
    from .platform.transfer_batch import TransferBatch
    schema = TransferBatch.model_json_schema()
    # Build a terse field summary for the guidance hint
    fields = []
    for name, finfo in TransferBatch.model_fields.items():
        ann = str(finfo.annotation)
        desc = finfo.description or ""
        fields.append({"field": name, "type": ann, "description": desc[:120]})
    return {
        "contract_version": "1",
        "schema": schema,
        "field_summary": fields,
        "next": (
            "Phase 3 will add a data-export-parquet skill that produces a "
            "TransferBatch manifest per run.  Use this schema to validate "
            "manifests or plan downstream consumer logic."
        ),
    }


@mcp.tool()
def list_connections() -> dict:
    """List all registered platform connections (no credentials returned).

    Use this to see what data sources are available before registering a
    project's source binding.  Passwords are never returned — only the
    opaque `secret_ref` pointer.
    """
    with Session(engine) as session:
        from sqlmodel import select as sqlselect
        from .models import PlatformConnection
        from .routers.connections import _connection_row
        conns = session.exec(sqlselect(PlatformConnection)).all()
        # _connection_row masks stored secrets (Tier-0) and exposes has_password
        # + extra_config (Snowflake warehouse/role/schema) for parity with the UI.
        return {
            "connections": [_connection_row(c) for c in conns],
            "count": len(conns),
            "next": "Use test_connection to verify a live connection before binding it.",
        }


@mcp.tool()
def create_connection(
    connection_name: str,
    platform_type: str,
    host: str,
    database: str,
    port: int = 0,
    username: str = "",
    secret_ref: str = "",
    password: str = "",
    extra_config: dict | None = None,
) -> dict:
    """Register a new platform connection.

    Two ways to supply the credential (Tier-0 containment):
      * `password` — a literal password OR a Snowflake **PAT** (a PAT authenticates
        as a password). Stored contained (never returned by any tool, never sent
        to an LLM); used only for live probes + skill-script invocations.
      * `secret_ref` — a runtime *reference*: `env:<VAR>` reads it from an
        environment variable (e.g. `env:MYSQL_PASSWORD`). A non-reference value in
        `secret_ref` is treated as a literal and contained the same as `password`.

    `extra_config` carries platform-specific keys:
      * Snowflake — `{"warehouse": "COMPUTE_WH", "role": "MY_ROLE", "schema": "PUBLIC"}`
        (warehouse supplies compute; `host` is the account identifier).
      * Databricks — `{"http_path": "/sql/1.0/warehouses/abc123", "catalog": ..., "schema": ...}`.

    `port` defaults: 443 for snowflake/databricks, 3306 for mysql, else 5432.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    from .platform.registry import get_registry
    import json
    reg = get_registry()
    if reg.get_manifest(platform_type) is None:
        return {"error": f"Unknown platform_type '{platform_type}'. Known: {reg.platform_ids()}"}
    if not port:
        port = {"mysql": 3306, "snowflake": 443, "databricks": 443}.get(platform_type, 5432)
    from .routers.connections import _normalize_stored_secret
    stored_secret = _normalize_stored_secret(secret_ref, password) or ""
    with Session(engine) as session:
        from sqlmodel import select as sqlselect
        from .models import PlatformConnection
        existing = session.exec(
            sqlselect(PlatformConnection).where(
                PlatformConnection.connection_name == connection_name
            )
        ).first()
        if existing:
            return {"error": f"A connection named '{connection_name}' already exists (id={existing.id})."}
        conn = PlatformConnection(
            connection_name=connection_name,
            platform_type=platform_type,
            host=host,
            port=port,
            database=database,
            username=username,
            secret_ref=stored_secret,
            extra_config_json=json.dumps(extra_config or {}),
        )
        session.add(conn)
        session.commit()
        session.refresh(conn)
        return {
            "created": True,
            "id": conn.id,
            "connection_name": conn.connection_name,
            "platform_type": conn.platform_type,
            "has_password": bool(conn.secret_ref),
            "next": f"Run test_connection({conn.id}) to verify the credentials resolve.",
        }


@mcp.tool()
def test_connection(connection_id: int, password_override: str = "") -> dict:
    """Probe a registered connection live — returns server_version + capabilities.

    If the connection's `secret_ref` resolves (e.g. env var is set), no
    `password_override` is needed.  Pass `password_override` for a one-shot
    test without storing the password.
    """
    with Session(engine) as session:
        from .models import PlatformConnection
        from .platform.registry import get_registry
        from .platform.secrets import resolve_secret
        conn = session.get(PlatformConnection, connection_id)
        if conn is None:
            return {"error": f"Connection {connection_id} not found"}
        reg = get_registry()
        if not reg.is_usable(conn.platform_type, "connection"):
            level = reg.get_capability(conn.platform_type, "connection").value
            return {
                "ok": False,
                "error": f"Platform '{conn.platform_type}' connection capability is '{level}'.",
            }
        import json as _json
        password = password_override or resolve_secret(conn.secret_ref)
        connection_ref = {
            "connection_id": str(conn.id),
            "host": conn.host,
            "port": conn.port,
            "database": conn.database,
            "username": conn.username,
            "resolved_password": password,
            # extra_config carries platform-specific probe keys (Snowflake
            # warehouse/role/schema, Databricks http_path) — mirror the REST path.
            "extra_config": _json.loads(conn.extra_config_json or "{}"),
        }
        from .routers.connections import _get_connection_provider
        provider = _get_connection_provider(conn.platform_type)
        if provider is None:
            return {"ok": False, "error": f"No connection provider for '{conn.platform_type}'."}
        evidence = provider.probe(connection_ref)
        return {
            "ok": not bool(evidence.warnings),
            "platform_type": conn.platform_type,
            "connection_id": connection_id,
            "server_version": evidence.server_version,
            "capabilities": {k: v.value for k, v in evidence.capabilities.items()},
            "warnings": evidence.warnings,
            "next": (
                "Connection verified. Use set_source_binding to bind it to a project."
                if not evidence.warnings else
                "Connection failed. Check host/port/credentials and retry."
            ),
        }


@mcp.tool()
def get_source_binding(project_code: str) -> dict:
    """Return the source binding for a project (which platform connection it reads from).

    Every project binds its source via a SourceBinding → PlatformConnection (the
    single connection contract). An unbound project has no source yet — set one
    with set_source_binding (or the data-source picker).
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        from sqlmodel import select as sqlselect
        from .models import SourceBinding, PlatformConnection
        binding = session.exec(
            sqlselect(SourceBinding).where(SourceBinding.project_id == project.id)
        ).first()
        if binding is None:
            return {
                "bound": False,
                "project_code": project_code,
                "platform_type": None,
                "note": "No source is bound yet. Use set_source_binding or the data-source picker.",
            }
        conn = session.get(PlatformConnection, binding.connection_id)
        return {
            "bound": True,
            "project_code": project_code,
            "connection_id": binding.connection_id,
            "connection_name": conn.connection_name if conn else None,
            "platform_type": conn.platform_type if conn else None,
            "default_schema": binding.default_schema,
        }


@mcp.tool()
def set_source_binding(
    project_code: str, connection_id: int, default_schema: str = "",
    view_target_namespace: str | None = None,
) -> dict:
    """Bind a project to a registered platform connection.

    After binding, `list_source_namespaces` / `list_source_tables` and the
    discovery/profiling/serving stages all resolve the source through this
    binding — the single connection contract for every platform.

    `view_target_namespace` is the deploy target for 3-level platforms
    (Databricks "catalog.schema", e.g. "workspace.default") whose source
    catalog is often read-only — the serving stage emits the view there and
    deploy creates it there. Omit (None) to leave any existing value untouched.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    with Session(engine) as session:
        from datetime import datetime
        from sqlmodel import select as sqlselect
        from .models import SourceBinding, PlatformConnection
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        conn = session.get(PlatformConnection, connection_id)
        if conn is None:
            return {"error": f"Connection {connection_id} not found"}
        binding = session.exec(
            sqlselect(SourceBinding).where(SourceBinding.project_id == project.id)
        ).first()
        if binding is None:
            binding = SourceBinding(
                project_id=project.id,
                connection_id=connection_id,
                default_schema=default_schema,
                view_target_namespace=(view_target_namespace or ""),
            )
        else:
            binding.connection_id = connection_id
            binding.default_schema = default_schema
            if view_target_namespace is not None:
                binding.view_target_namespace = view_target_namespace
            binding.updated_at = datetime.utcnow()
        session.add(binding)
        session.commit()
        return {
            "bound": True,
            "project_code": project_code,
            "connection_id": connection_id,
            "connection_name": conn.connection_name,
            "platform_type": conn.platform_type,
            "default_schema": default_schema,
            "view_target_namespace": binding.view_target_namespace or "",
            "next": f"Run list_source_namespaces('{project_code}') to browse the source.",
        }


@mcp.tool()
def list_source_namespaces(project_code: str) -> dict:
    """List schemas (or catalogs) for a project's source data platform.

    Dispatches to the bound platform's DiscoveryProvider:
    - Postgres (legacy or explicit binding) → information_schema.schemata
    - MySQL (explicit binding) → information_schema.SCHEMATA, system schemas filtered

    System schemas are always excluded.  Use `list_source_tables` to list
    tables within a namespace.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        from .routers.connections import resolve_source_connection_ref, _get_discovery_provider
        platform_type, connection_ref = resolve_source_connection_ref(project, session)

    provider = _get_discovery_provider(platform_type)
    if provider is None:
        return {
            "error": f"No discovery provider for platform '{platform_type}'.",
            "platform_type": platform_type,
        }

    from .platform.interfaces import NamespaceRef
    try:
        namespaces = provider.list_namespaces(connection_ref)
    except Exception as exc:
        return {"error": f"Discovery failed: {exc}", "platform_type": platform_type}

    return {
        "project_code": project_code,
        "platform_type": platform_type,
        "namespaces": [
            {"parts": ns.parts, "labels": ns.labels}
            for ns in namespaces
        ],
        "count": len(namespaces),
        "next": f"Use list_source_tables('{project_code}', '<schema>') to list tables.",
    }


@mcp.tool()
def list_source_tables(project_code: str, schema: str) -> dict:
    """List tables in a schema for a project's source data platform.

    Dispatches to the bound platform's DiscoveryProvider (Postgres or MySQL).
    Returns table names and their kind (table / view / materialized_view).

    Use `list_source_namespaces` first to discover available schemas.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    with Session(engine) as session:
        project = _resolve_project(session, project_code)
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        from .routers.connections import resolve_source_connection_ref, _get_discovery_provider
        platform_type, connection_ref = resolve_source_connection_ref(project, session)

    provider = _get_discovery_provider(platform_type)
    if provider is None:
        return {
            "error": f"No discovery provider for platform '{platform_type}'.",
            "platform_type": platform_type,
        }

    from .platform.interfaces import NamespaceRef
    ns = NamespaceRef(
        platform_instance_id=connection_ref.get("connection_id", "project"),
        parts=[schema],
        labels={"schema": schema},
    )
    try:
        relations = provider.list_relations(connection_ref, ns)
    except Exception as exc:
        return {"error": f"Discovery failed: {exc}", "platform_type": platform_type}

    return {
        "project_code": project_code,
        "platform_type": platform_type,
        "schema": schema,
        "tables": [
            {"name": r.name, "kind": r.relation_kind}
            for r in relations
        ],
        "count": len(relations),
        "next": "Tables listed. Pass a subset to the data-discovery stage via set_data_source.",
    }


# ── Inbound intake review (external-tool → scaffolded projects) ─────────────
# Submission itself is REST-only (scoped machine credential, /api/intake/submit).
# These are the engineer's headless review/approve surface for MIGRATION
# intakes — the MCP mirror of the Incoming-queue actions in the web UI.


@mcp.tool()
def list_intake_submissions(scenario: str | None = "migration", status: str | None = None) -> dict:
    """List staged inbound-intake submissions (default scenario: migration) with
    each one's review status and confidence-graded blueprint. The engineer's
    headless view of the intake queue. Pass scenario=None to list all."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .routers.intake import list_intake
    with Session(engine) as session:
        return list_intake(scenario=scenario, status=status, session=session)


@mcp.tool()
def get_intake_submission(intake_id: int) -> dict:
    """Get one intake submission: scenario, status, the parsed blueprint,
    blueprint_revision (needed to approve), and approval_blockers."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.intake import get_intake
    with Session(engine) as session:
        try:
            return get_intake(intake_id, session=session)
        except HTTPException as e:
            return {"error": e.detail}


@mcp.tool()
def approve_intake_submission(intake_id: int, expected_revision: int) -> dict:
    """Approve a reviewed blueprint and run the scaffold saga (resumable,
    idempotent). `expected_revision` must equal the current blueprint_revision
    (optimistic concurrency) and all gaps must be resolved first."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.intake import ApproveBody, approve
    with Session(engine) as session:
        try:
            return approve(intake_id, ApproveBody(expected_revision=expected_revision), session=session)
        except HTTPException as e:
            return {"error": e.detail}


@mcp.tool()
def reject_intake_submission(intake_id: int, reason: str = "") -> dict:
    """Reject a staged intake submission (quarantine; no scaffold)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.intake import RejectBody, reject
    with Session(engine) as session:
        try:
            return reject(intake_id, RejectBody(reason=reason), session=session)
        except HTTPException as e:
            return {"error": e.detail}


@mcp.tool()
def check_graph_integrity(project_code: str | None = None) -> dict:
    """Run the detect-only Neo4j graph-integrity scan and return the scorecard.

    Global by default; pass `project_code` to scope the code-bearing checks to
    one project. The report lists every invariant (structural orphans, duplicate
    URIs, cross-project leakage, cross-project-edge validity, enum/null hygiene,
    isCurrent uniqueness, retired-term probes, stranded concepts) with
    `{check, severity, count, sample}` — INCLUDING clean checks so you can assert
    "zero leaks / zero orphans". Read-only; never writes, never blocks a write."""
    if project_code:
        if (denied := _deny_project(project_code)) is not None:
            return denied
    elif (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.graph_integrity import graph_integrity_check
    with Session(engine) as session:
        try:
            return graph_integrity_check(project_code=project_code, session=session)
        except HTTPException as e:
            return {"error": e.detail}


# PO tools live in po_mcp_server.py — mounted at /po-mcp.
