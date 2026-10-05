"""MCP server for the Data Product Owner role.

Mounted at /po-mcp — completely separate from the Data Engineer MCP at /mcp.
A PO connecting to this endpoint sees only PO-relevant tools; no stage execution,
no Cypher, no DE-specific review surfaces.

Auth is shared with the DE MCP (same WB_MCP_TOKENS / WB_MCP_ALLOW_INSECURE env
vars) so a single token works across both endpoints. The separation is about
surface area, not security tiers.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlmodel import Session, select

from .database import engine
from .models import Project

# Re-use all auth infrastructure from the DE MCP — same process, same env vars,
# same ContextVar so request-scoped principal/project-scope works identically.
from .mcp_server import (
    _serving_guard,
    _deny_project,
    _deny_write,
    _principal,
    _BearerAuthMiddleware,
)

po_mcp = FastMCP(
    "data-workbench-po",
    instructions=(
        "Data Workbench — Data Product Owner MCP. "
        "ALWAYS call get_po_summary(owner_email=...) as the very first tool in every session "
        "before doing anything else — it tells you exactly what needs action across all products "
        "and what to do next. If the user's email is not known, ask for it once, then call "
        "get_po_summary immediately.\n\n"
        "BUILDING A PRODUCT IS A CONVERSATION, NOT A WIZARD. You are the "
        "conversational alternative to the web UI's numbered wizard — interview the "
        "PO in business terms, don't march them through 'Step 1, Step 2, …' or recite "
        "step names/numbers or tool names.\n"
        "• SOURCE product (dpe-sa): call create_source_product with the idea + domain + name the PO "
        "gives you. Ask for each; don't invent.\n"
        "• CONSUMER product (dpe-cf): create with start_consumer_product, then use "
        "get_authoring_plan(project_code) as YOUR PRIVATE checklist (call it after creating and after "
        "every save) to see what's still missing — never read its steps at the PO.\n\n"
        "Collect the ingredients a good product needs through natural conversation, in whatever order "
        "it flows:\n"
        "  1. Gather every input the product needs (purpose, shape/grain, schema, details, operations, "
        "rules, sources) — but as natural business questions, driven by what get_authoring_plan says is "
        "still missing, not by a fixed sequence.\n"
        "  2. When AI assist would help, OFFER the matching tool (discover_product_columns for a starter "
        "schema, check_filter for a row filter, recommend_schema for columns, suggest_domain_rules for "
        "quality rules, advise_serving_strategy / get_osi_evaluation for readiness) — propose it, let "
        "the PO accept or decline.\n"
        "  3. Where there's a choice (domain, scoring rubric [OSI recommended, or AI-Ready], columns, "
        "sources), present the options and let the PO choose — don't pick for them.\n"
        "  4. Optional things (shape, operations/SLA, rules, readiness review) aren't skipped SILENTLY — "
        "offer them and only skip when the PO says so; confirm the skip out loud.\n"
        "  5. NEVER invent a purpose, grain, filter, column set, rubric, or rule. If you don't know an "
        "input, ASK.\n"
        "Persist answers with save_product_spec, then re-read get_authoring_plan to confirm they "
        "registered. submit_product_spec is gated on the required inputs.\n\n"
        "The bar: everything the PO would be asked on the webpage, they get asked here — nothing "
        "silently defaulted or skipped without their say-so — but gathered as a conversation, not "
        "recited as a wizard."
    ),
    streamable_http_path="/",
)


def asgi_app():
    """The PO MCP ASGI app wrapped with plain bearer auth — mounted at /po-mcp."""
    return _BearerAuthMiddleware(po_mcp.streamable_http_app())


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@po_mcp.tool()
def get_po_summary(owner_email: str) -> dict:
    """THE STARTING POINT for every PO session — call this first, always.

    Returns a full picture of the PO's portfolio: what products exist, what
    lifecycle state each one is in, and a plain-English action list telling
    the agent exactly what to do next for each product that needs attention.

    The agent should read this, then immediately act on the highest-priority
    item without waiting for the user to ask. This is the PO equivalent of
    get_plan_summary on the Data Engineer side.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .models import ProductRequest
    from .config import FRONTEND_URL
    from .routers.reviews import get_source_product_validation

    with Session(engine) as session:
        projects = session.exec(
            select(Project).where(Project.owner_email == owner_email)
        ).all()
        project_ids = [p.id for p in projects]
        all_requests = (
            session.exec(
                select(ProductRequest).where(
                    ProductRequest.project_id.in_(project_ids)
                )
            ).all()
            if project_ids
            else []
        )

    # Latest request per project
    req_map: dict[int, Any] = {}
    for r in all_requests:
        prev = req_map.get(r.project_id)
        if prev is None or (
            r.submitted_at
            and (prev.submitted_at is None or r.submitted_at > prev.submitted_at)
        ):
            req_map[r.project_id] = r

    action_items = []
    portfolio = []

    for p in projects:
        req = req_map.get(p.id)
        req_status = req.status.value if req else None

        # Check pending validations for this product
        pending_count = 0
        try:
            with Session(engine) as session:
                val = get_source_product_validation(p.id, session=session)
                pending_count = val.get("count", 0)
        except Exception:
            pass

        # Determine state and what needs to happen
        if pending_count > 0:
            state = "needs_validation"
            action = (
                f"VALIDATE: '{p.name}' ({p.project_code}) has {pending_count} item(s) "
                f"pending your review — call bulk_approve_source_validation('{p.project_code}') "
                f"to approve all at once."
            )
            priority = 1
        elif req_status == "complete":
            state = "ready_to_deploy"
            action = (
                f"DEPLOY: '{p.name}' ({p.project_code}) is ready — engineering is done. "
                f"Call deploy_product('{p.project_code}') to publish it to the marketplace."
            )
            priority = 2
        elif req_status in ("submitted", "accepted"):
            state = "with_engineering"
            action = (
                f"WAITING: '{p.name}' ({p.project_code}) is with engineering (status: {req_status}). "
                f"No action needed yet — check back later with get_product_status('{p.project_code}')."
            )
            priority = 3
        elif req_status == "published":
            state = "live"
            action = None
            priority = 99
        else:
            state = "draft"
            action = (
                f"DRAFT: '{p.name}' ({p.project_code}) has no active request. "
                f"Call create_source_product to submit it to engineering."
            )
            priority = 4

        # Deep link to the exact screen this product needs: the validation gate
        # when items are pending, else the product detail. One keystroke from the
        # terminal to the rich UI view (the CLI-UX escape hatch).
        web_url = (
            f"{FRONTEND_URL}/product/validate/{p.id}"
            if pending_count > 0
            else f"{FRONTEND_URL}/product/products/{p.id}"
        )
        portfolio.append({
            "project_code": p.project_code,
            "name": p.name,
            "archetype": p.archetype,
            "domain": p.domain,
            "state": state,
            "request_status": req_status,
            "pending_validations": pending_count if pending_count > 0 else None,
            "web_url": web_url,
        })

        if action:
            action_items.append({"priority": priority, "action": action, "project_code": p.project_code})

    action_items.sort(key=lambda x: x["priority"])

    if action_items:
        next_step = action_items[0]["action"]
    elif projects:
        next_step = "All products are up to date. Use create_source_product to request a new one."
    else:
        next_step = (
            f"No products found for {owner_email}. "
            f"Call list_domains to see available domains, then create_source_product to get started."
        )

    return {
        "owner_email": owner_email,
        "total_products": len(projects),
        "portfolio": portfolio,
        "action_items": action_items,
        "next": next_step,
    }


@po_mcp.tool()
def list_my_products(owner_email: str) -> dict:
    """List all data products owned by the given email address — the PO's
    dashboard view. Returns every product with its current request status
    so the agent knows which ones need action.

    Call this first in any PO session to orient before calling get_product_status.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .models import ProductRequest

    with Session(engine) as session:
        projects = session.exec(
            select(Project).where(Project.owner_email == owner_email)
        ).all()
        project_ids = [p.id for p in projects]
        requests = (
            session.exec(
                select(ProductRequest).where(
                    ProductRequest.project_id.in_(project_ids)
                )
            ).all()
            if project_ids
            else []
        )

    req_map: dict[int, Any] = {}
    for r in requests:
        prev = req_map.get(r.project_id)
        if prev is None or (
            r.submitted_at
            and (prev.submitted_at is None or r.submitted_at > prev.submitted_at)
        ):
            req_map[r.project_id] = r

    products = []
    for p in projects:
        req = req_map.get(p.id)
        products.append({
            "project_code": p.project_code,
            "name": p.name,
            "archetype": p.archetype,
            "domain": p.domain,
            "request_status": req.status.value if req else None,
            "request_kind": req.kind.value if req else None,
        })

    needs_action = [p for p in products if p["request_status"] in ("submitted", "accepted")]
    return {
        "owner_email": owner_email,
        "products": products,
        "count": len(products),
        "needs_action": needs_action,
        "next": (
            f"{len(needs_action)} product(s) need attention — call get_product_status <project_code> for each."
            if needs_action
            else "No products need action. Use create_source_product or start_consumer_product to create a new one."
        ),
    }


@po_mcp.tool()
def get_product_status(project_code: str) -> dict:
    """The PO equivalent of get_plan_summary — tells the agent exactly what the
    PO needs to do next for this product.

    Returns lifecycle state, pending validation counts per tab, and a plain-
    language recommended_next so the agent knows whether to validate, wait for
    engineering, or deploy.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .models import ProductRequest
    from .routers.reviews import get_source_product_validation

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}

        req = session.exec(
            select(ProductRequest)
            .where(ProductRequest.project_id == project.id)
            .order_by(ProductRequest.submitted_at.desc())
        ).first()

        try:
            val = get_source_product_validation(project.id, session=session)
            pending_count = val.get("count", 0)
            pending_by_tab = {
                "names": len(val.get("column_names", [])),
                "descriptions": len(val.get("descriptions", [])),
                "tables": len(val.get("table_descriptions", [])),
                "relationships": len(val.get("relationship_descriptions", [])),
                "rules": len(val.get("rules", [])),
            }
        except Exception:
            pending_count = 0
            pending_by_tab = {}

    req_status = req.status.value if req else None

    if pending_count > 0:
        next_action = (
            f"{pending_count} item(s) pending your validation "
            f"({', '.join(k for k, v in pending_by_tab.items() if v > 0)}) — "
            f"call bulk_approve_source_validation or review_validation_item."
        )
    elif req_status == "submitted" and project.archetype == "dpe-sa":
        next_action = "Request submitted. Waiting for engineering to accept and run discovery."
    elif req_status == "accepted":
        next_action = "Engineering is running the pipeline. Check back with get_product_status."
    elif req_status == "complete":
        next_action = "Engineering complete. Call deploy_product to publish to the marketplace."
    else:
        next_action = "No pending action. Use get_product_status to re-check later."

    return {
        "project_code": project_code,
        "name": project.name,
        "archetype": project.archetype,
        "request_status": req_status,
        "pending_validations": pending_count,
        "pending_by_tab": pending_by_tab if pending_count > 0 else None,
        "next": next_action,
    }


@po_mcp.tool()
def list_domains() -> dict:
    """List all available domain catalogs — use this to pick a valid domain
    before calling create_source_product or start_consumer_product.

    Returns each domain's name and column count. The domain string goes directly
    into the domain field of product creation calls.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    import yaml
    from .config import BASE_DIR

    catalog_dir = BASE_DIR / "playbook" / "domain_catalogs"
    domains = []
    if catalog_dir.is_dir():
        for path in sorted(catalog_dir.glob("*.yaml")):
            if path.stem == "common":
                continue
            try:
                with path.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                cols = data.get("columns", []) if isinstance(data, dict) else []
                domains.append({
                    "domain": path.stem,
                    "description": data.get("description", ""),
                    "column_count": len(cols),
                })
            except Exception:
                continue

    return {
        "domains": domains,
        "count": len(domains),
        "next": "Pass the domain string to create_source_product or start_consumer_product.",
    }


@po_mcp.tool()
def list_marketplace(
    domain: str | None = None,
    product_kind: str | None = None,
    tag: str | None = None,
) -> dict:
    """Browse published data products in the marketplace.

    Use this to find source products before building a consumer product —
    the contract_id values returned are what you pass to add_product_sources.

    product_kind: 'source' or 'consumer'. Leave None for all.
    domain: filter by domain name (e.g. 'sports'). Leave None for all.
    tag: filter to products carrying this tag (case-insensitive). Leave None for all.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .routers.marketplace import list_published

    with Session(engine) as session:
        result = list_published(session=session, owned_by=None, product_kind=product_kind, tag=tag)

    products = result.get("products", [])
    if domain:
        products = [p for p in products if (p.get("domain") or "").lower() == domain.lower()]

    slim = [
        {
            "contract_id": p.get("contract_id"),
            "name": p.get("name"),
            "domain": p.get("domain"),
            "product_kind": p.get("product_kind"),
            "tags": p.get("tags") or [],
            "osi_band": p.get("osi_band"),
            "owner_email": p.get("owner_email"),
        }
        for p in products
    ]
    return {
        "products": slim,
        "count": len(slim),
        "next": (
            "Use the contract_id values as source inputs when calling add_product_sources."
            if slim else "No published products found matching the filter."
        ),
    }


@po_mcp.tool()
def get_product_lineage() -> dict:
    """Marketplace-wide PRODUCT dependency graph — how data products relate to
    one another via :CONSUMES (source → aggregate → consumer). Coarse: nodes are
    whole products, edges are product-to-product (NO tables/columns/mappings).

    Returns `{nodes, edges}`: each node is `{uri, name, product_kind, domain,
    tags, lifecycle_state}`; each edge is `{source, target, kind:"consumes"}`
    in DATA-FLOW direction (upstream source → downstream consumer). Use this to
    understand which products build on which before reclassifying or editing a
    product. The visual canvas is the marketplace Lineage view in the UI.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.marketplace import get_product_lineage as _get_product_lineage

    with Session(engine) as session:
        try:
            result = _get_product_lineage(session=session)
        except HTTPException as e:
            return {"error": str(e.detail)}
    nodes = result.get("nodes") or []
    edges = result.get("edges") or []
    return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}


@po_mcp.tool()
def create_source_product(
    owner_email: str,
    owner_name: str,
    idea: str,
    domain: str,
    name: str,
) -> dict:
    """Submit a new source-aligned data product request — the MCP equivalent of
    the 3-step source product wizard.

    The engineer binds the source database later (via the data-source picker /
    set_source_binding); no connection is collected at product-request time.

    Collects the PO's idea (plain-language description of the product), domain,
    and working name, then creates the project and submits it to the engineering
    queue in one call. No ODCS spec is authored — the engineer profiles the source
    DB and synthesizes the spec from discovery output.

    After this returns, the request is in 'submitted' state. Engineering accepts
    it, runs discovery, and the PO will need to validate. Track progress with
    get_product_status.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.projects import create_project, ProjectCreate
    from .routers.product_requests import submit_product_request, SubmitRequestBody
    from .models import ProductRequestKind, AppSettings

    with Session(engine) as session:
        settings = session.exec(select(AppSettings)).first()
        neo4j_host = settings.neo4j_host if settings else ""
        neo4j_port = settings.neo4j_port if settings else 7687
        neo4j_user = settings.neo4j_user if settings else ""
        neo4j_password = settings.neo4j_password if settings else ""
        neo4j_database = settings.neo4j_database if settings else ""

        try:
            project_resp = create_project(
                ProjectCreate(
                    name=name,
                    archetype="dpe-sa",
                    domain=domain,
                    product_idea=idea,
                    owner_email=owner_email,
                    owner_name=owner_name,
                    neo4j_host=neo4j_host,
                    neo4j_port=neo4j_port,
                    neo4j_user=neo4j_user,
                    neo4j_password=neo4j_password,
                    neo4j_database=neo4j_database,
                ),
                session,
            )
        except HTTPException as e:
            return {"error": f"Failed to create project: {e.detail}"}

        # create_project returns a dict — access defensively (matches the DE
        # create_consumer_product pattern; attribute access was the bug).
        project_id = project_resp["id"] if isinstance(project_resp, dict) else project_resp.id
        project_code = (
            project_resp["project_code"] if isinstance(project_resp, dict)
            else project_resp.project_code
        )

        try:
            req_resp = submit_product_request(
                project_id,
                SubmitRequestBody(
                    kind=ProductRequestKind.new,
                    submitted_by=owner_email,
                    notes=idea,
                ),
                session,
            )
        except HTTPException as e:
            return {"error": f"Project created ({project_code}) but request submit failed: {e.detail}"}

    return {
        "ok": True,
        "project_code": project_code,
        "request_id": req_resp["id"] if isinstance(req_resp, dict) else req_resp.id,
        "status": "submitted",
        "next": (
            "Request submitted to engineering. "
            "Call get_product_status periodically — once engineering runs discovery "
            "and you see pending_validations > 0, call bulk_approve_source_validation."
        ),
    }


# ---------------------------------------------------------------------------
# Consumer-aligned (dpe-cf) product authoring — the PO's OTHER job.
#
# A PO creates BOTH source products (create_source_product, above) and consumer
# products. The consumer path is contract-first: the PO shapes the ODCS spec
# (schema, sources) BEFORE handing it to engineering. These wrappers put that
# whole wizard flow on the PO server so the persona boundary stays clean — they
# call the SAME backend handlers the DE tools do (no logic fork).
#
# Flow: start_consumer_product → save_product_spec (shape schema) →
#       find_source_products (bind inputs) → save_product_spec → submit_product_spec
# ---------------------------------------------------------------------------

@po_mcp.tool()
def start_consumer_product(
    owner_email: str,
    owner_name: str,
    product_idea: str,
    domain: str,
    name: str,
) -> dict:
    """Create a new consumer-aligned (dpe-cf) product — the MCP equivalent of the
    PO clicking "New Product" (consumer) in the Product Workbench.

    Contract-first: this only creates the project. Author the ODCS spec next with
    save_product_spec, bind sources with find_source_products, then hand it to
    engineering with submit_product_spec. Returns the new project_code.
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
            return {"error": f"Failed to create project: {e.detail}"}
        except Exception as e:
            return {"error": f"Project creation failed: {e}"}

    project_code = result["project_code"] if isinstance(result, dict) else getattr(result, "project_code", None)
    project_id = result["id"] if isinstance(result, dict) else getattr(result, "id", None)
    return {
        "ok": True,
        "project_code": project_code,
        "project_id": project_id,
        "archetype": "dpe-cf",
        "next": (
            "Do NOT author the whole spec in one shot. Call get_authoring_plan(project_code) "
            "and walk the PO through the wizard steps ONE AT A TIME — present each step's choices "
            "(incl. the scoring rubric on step 1), offer the AI-assist where available, and for "
            "optional steps ask 'set or skip?'. Ask the PO for each input; never invent."
        ),
    }


@po_mcp.tool()
def get_product_spec(project_code: str) -> dict:
    """Read the current ODCS v3.1 contract spec for a product — the MCP
    equivalent of opening the ODCS editor. Returns `spec` (null if unsaved),
    contract_id, lifecycle_state, current_version, and any engineering rejection.
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


@po_mcp.tool()
def save_product_spec(
    project_code: str,
    spec: dict,
    submitted_by: str = "",
    change_kind: str = "auto",
    revision_notes: str = "",
    merge: bool = True,
) -> dict:
    """Save (or update) the ODCS v3.1 contract spec — the MCP equivalent of the
    wizard's Save (PUT /odcs + regenerate the product graph).

    **Merge-by-default** (`merge=True`): the top-level keys you pass WIN and every
    other key is preserved from the saved contract — so a partial save (just the
    schema, just the SLA, just the sources) no longer blanks the rest. You can
    still author the whole contract in one call; you just don't have to. To clear
    a field, pass it explicitly (e.g. `slaProperties: []`). Pass `merge=False` for
    a deliberate full replace (starting the contract over from scratch).

    Full `spec` shape you can author:
      spec.name / domain / description / purpose / scoringRubric ('osi')
      spec.tags[] = ["finance", "gold", ...]         (free-form product tags;
                       trimmed + case-insensitively deduped; drive the
                       marketplace tag filter + group-by; merge-safe to edit later)
      spec.schema[0].name / physicalName            (dataset physical name)
      spec.schema[0].properties[] = {
          name, logicalType, physicalType, description, primaryKey, required,
          transform?: {kind, inputs[], params{}, separator?, expression?}   # column-level DSL
      }
      spec.schema[0].transform = {                  # dataset-level shape (step 2 + step 4)
          grain_prose?, filter_intent?, filter?,
          scd_policy?: {type:'scd2', effective_column, expiration_column, add_is_current}
                       | {type:'latest_only'|'snapshot'|''},
          grouping_keys[]?, suppressed_columns[]?, window_specs{}?
      }
      spec.slaProperties[] = {property, value, unit}        (step 6)
      spec.team[] = {name, role, email};  spec.roles[] = {role, access, description}
      spec.inputs[] = {dprod_uri, contract_id, name}        (bound sources; step 3/9)
      spec.customProperties = {po_serving_preference, po_serving_reason?}   (step 8)

    `change_kind`: 'auto' classifies the diff, 'cosmetic' patches in place,
    'schema'/'breaking' cuts a new version. On a draft this also rebuilds the
    :DProdDataProduct graph (needed for find_source_products / rules / gap-check);
    the rebuild is SKIPPED for published/approved contracts (dprod isn't versioned).
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.odcs import (
        save_odcs as _save_odcs, ODCSSpecInput,
        get_odcs as _get_odcs, generate_dprod as _generate_dprod,
    )

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        # Pre-save lifecycle drives the regen skip rule (mirrors the wizard's
        # hydratedLifecycleState check).
        pre_lifecycle = None
        existing_spec = {}
        try:
            pre = _get_odcs(project.id, session)
            if isinstance(pre, dict):
                pre_lifecycle = pre.get("lifecycle_state")
                existing_spec = pre.get("spec") or {}
        except Exception:
            pass
        # Merge-by-default: incoming top-level keys override; keys absent from
        # `spec` are carried over from the saved contract. This stops a partial
        # save from wiping fields set on a previous save. `merge=False` (or no
        # prior spec) does a straight full replace.
        effective_spec = {**existing_spec, **spec} if (merge and existing_spec) else spec
        try:
            result = _save_odcs(
                project.id,
                ODCSSpecInput(
                    spec=effective_spec,
                    submitted_by=submitted_by or None,
                    change_kind=change_kind,
                    revision_notes=revision_notes or "",
                ),
                session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:
            return {"error": f"ODCS save failed: {e}"}

        # Sync the SQLModel Project.name (and domain) with the spec. The
        # incoming-request queue and every engineer-side view read Project.name,
        # so a rename made through the spec MUST propagate here — otherwise the
        # two drift (the PO renames "Player Roster" → "Player Info Consumer" but
        # the engineer still sees "Player Roster"). Only update when actually
        # changed, to avoid needless writes.
        renamed_to = None
        spec_name = (effective_spec.get("name") or "").strip()
        spec_domain = (effective_spec.get("domain") or "").strip()
        if (spec_name and spec_name != (project.name or "")) or (
            spec_domain and spec_domain != (project.domain or "")
        ):
            if spec_name and spec_name != (project.name or ""):
                project.name = spec_name
                renamed_to = spec_name
            if spec_domain and spec_domain != (project.domain or ""):
                project.domain = spec_domain
            session.add(project)
            session.commit()

        # Rebuild dprod so downstream (sources match / rules / gap-check / mapping)
        # sees the new schema — UNLESS published/approved (dprod nodes aren't versioned).
        regen_note = None
        if pre_lifecycle not in ("published", "approved"):
            try:
                _generate_dprod(project.id, session)
                regen_note = "product graph (dprod) regenerated"
            except HTTPException as e:
                regen_note = f"saved, but dprod regen failed: {e.detail}"
            except Exception as e:  # noqa: BLE001
                regen_note = f"saved, but dprod regen failed: {e}"
        else:
            regen_note = f"dprod regen skipped (lifecycle={pre_lifecycle})"

    _branched = isinstance(result, dict) and result.get("branched")
    _branch_note = (
        f"NOTE: this save BRANCHED a new contract version (v{result.get('new_version')}); "
        "the previous version is preserved. If unintended, call discard_draft_version. "
        if _branched else ""
    )
    return {
        "ok": True,
        **(result if isinstance(result, dict) else {}),
        "regen": regen_note,
        **({"renamed_to": renamed_to} if renamed_to else {}),
        "next": (
            _branch_note
            + "Spec saved. Recommend columns (recommend_schema), bind sources "
            "(find_source_products → add to spec.inputs), suggest rules "
            "(suggest_domain_rules), gap-check (run_gap_analysis), then submit_product_spec."
        ),
    }


@po_mcp.tool()
def discard_draft_version(project_code: str) -> dict:
    """Discard an unintended draft contract version and roll back to the prior one.

    Undo a save that BRANCHED a new version (see `save_product_spec`'s
    `branched`/`new_version`) when the branch was not intended — e.g. an edit
    opened just to review a published product. Only works when the head version
    is a *draft* branched from a prior version; never destroys published/approved
    history. Surviving columns keep their existing mappings.
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
        except Exception as e:  # noqa: BLE001
            return {"error": f"discard-draft failed: {e}"}

    return {**(result if isinstance(result, dict) else {}),
            "next": "The prior version is restored as head. Re-open the product to review it."}


@po_mcp.tool()
def get_stage_output(project_code: str, stage_id: str) -> dict:
    """Reviewable markdown 'Step Report' of what an engineering stage PRODUCED —
    output visibility for the PO. After the engineer runs a stage (mapping,
    serving, etc.), call this to SEE what the system produced (column mappings,
    generated view SQL, deployed views, DQ rules) before you deploy_product.
    `stage_id` e.g. data_mapping / serving_virtual_view / deploy_virtual_view.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .stage_output import build_stage_report
    from .config import FRONTEND_URL
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        pid = project.id
        try:
            report = build_stage_report(project, stage_id)
        except Exception as e:  # noqa: BLE001
            return {"error": f"Stage report failed: {e}"}
    report["web_url"] = f"{FRONTEND_URL}/product/products/{pid}"
    return report


@po_mcp.tool()
def get_product_report(project_code: str) -> dict:
    """Generate the full **Markdown report** for one of your data products — the
    same report as the UI's "Generate Report" button: overview, purpose, people,
    the schema with a **Mermaid ERD**, a **Mermaid lineage** graph, the mapping
    table, quality rules, OSI, and serving details. **Offer this once the product
    is deployed/published** so the owner has shareable documentation (paste into a
    wiki). Rendered fresh each call.

    Returns `{report: <markdown>}` — write it to a `.md` file to keep it.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.marketplace import generate_product_report
    from .config import FRONTEND_URL
    contract_id = f"{project_code}-contract"
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
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
        "web_url": f"{FRONTEND_URL}/product/products/{pid}",
        "next": "Write `report` to a .md file (Mermaid ERD + lineage included) and share it with the owner.",
    }


@po_mcp.tool()
def get_authoring_plan(project_code: str) -> dict:
    """YOUR PRIVATE COMPLETENESS CHECKLIST for a consumer product — for your eyes,
    not the PO's. Call it to see what the product still needs before submitting.

    **This is NOT a script to read aloud.** Do NOT announce "Step 1 / Step 2 / …",
    do NOT walk the PO through numbered steps, and do NOT expose these step
    names/numbers or tool names. Those are UI-wizard scaffolding; you are the
    *conversational* alternative to the wizard. Use the `captured` / `done` /
    `blocking` fields to figure out what's missing, then ASK for it as a natural
    business question — in whatever order the conversation flows. Never invent an
    input (purpose, columns, filter, rule); propose and confirm, or ask.

    Reads the actual saved spec (not claims), so status is ground truth; the
    required/optional gating mirrors the submit gate.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .routers.odcs import get_odcs as _get_odcs

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            doc = _get_odcs(project.id, session)
        except Exception as e:  # noqa: BLE001
            return {"error": f"Could not read spec: {e}"}
        _project_idea = (project.product_idea or "").strip()  # captured before session closes

    # Defensive extraction — a spec field that isn't the expected shape (e.g. a
    # schema entry that's a string, or a non-dict input) must degrade to empty,
    # never throw. This is a read-only status view; a malformed field should not
    # crash the whole plan.
    spec = (doc or {}).get("spec") or {}
    if not isinstance(spec, dict):
        spec = {}
    lifecycle = (doc or {}).get("lifecycle_state")
    schema_list = spec.get("schema") if isinstance(spec.get("schema"), list) else []
    sch = schema_list[0] if schema_list and isinstance(schema_list[0], dict) else {}
    props = sch.get("properties") if isinstance(sch.get("properties"), list) else []
    xform = sch.get("transform") if isinstance(sch.get("transform"), dict) else {}
    inputs = spec.get("inputs") if isinstance(spec.get("inputs"), list) else []
    slas = spec.get("slaProperties") if isinstance(spec.get("slaProperties"), list) else []

    # Pre-computed safe projections (guard against non-dict list elements).
    bound_inputs = [i.get("contract_id") for i in inputs if isinstance(i, dict)]
    col_names = [p.get("name") for p in props if isinstance(p, dict)]

    def _has(*vals):
        return any(bool(v) for v in vals)

    # Ordered steps mirroring NewProductWizard, with the UI's required/optional gating.
    steps = [
        {
            "n": 1, "name": "Describe & Choose Domain", "required": True,
            "captured": {"description": spec.get("description"), "domain": spec.get("domain"),
                         "rubric": spec.get("scoringRubric")},
            "done": _has(spec.get("description"), spec.get("purpose"), _project_idea) and _has(spec.get("domain")),
            "ask": "Present ALL of: (1) what the product is for (idea/description); (2) the DOMAIN — "
                   "show options via list_domains; (3) the SCORING RUBRIC — OSI (recommended) or "
                   "AI-Ready, show via list_scoring_rubrics. Let the PO pick each; don't default silently.",
            "assist": "Offer discover_product_columns to suggest a starter schema from the idea (the UI's ✨ Guide me).",
            "tools": ["list_domains", "list_scoring_rubrics", "discover_product_columns", "save_product_spec"],
        },
        {
            "n": 2, "name": "Shape", "required": False,
            "captured": {"grain_prose": xform.get("grain_prose"), "scd_policy": xform.get("scd_policy"),
                         "filter_intent": xform.get("filter_intent")},
            "done": _has(xform.get("grain_prose"), xform.get("scd_policy"), xform.get("filter_intent")),
            "ask": "One row per WHAT (grain)? History or current-only (SCD)? Any row filter? "
                   "(optional — but ASK; don't assume '1:1' without confirming)",
            "tools": ["check_filter", "save_product_spec (schema[0].transform)"],
        },
        {
            "n": 3, "name": "Suggest candidate sources", "required": False,
            "captured": {"bound_inputs": bound_inputs},
            "done": len(inputs) > 0,
            "ask": "Which source product(s) should this consume? (can also confirm at step 9)",
            "tools": ["find_source_products"],
        },
        {
            "n": 4, "name": "Shape the Schema", "required": True,
            "captured": {"columns": col_names, "count": len(props)},
            "done": len(props) > 0,
            "ask": "Which columns does the product expose? (use recommend_schema, then confirm with the PO)",
            "tools": ["recommend_schema", "save_product_spec (schema[0].properties)"],
        },
        {
            "n": 5, "name": "Product Details", "required": True,
            "captured": {"name": spec.get("name"), "dataset": sch.get("name"),
                         "purpose": spec.get("purpose")},
            "done": _has(spec.get("name")),
            "ask": "Product name, dataset name, purpose? (ASK the PO the purpose — do NOT invent it)",
            "tools": ["save_product_spec"],
        },
        {
            "n": 6, "name": "Operations & Support", "required": False,
            "captured": {"slas": len(slas), "team": len(spec.get("team") or []),
                         "roles": len(spec.get("roles") or [])},
            "done": len(slas) > 0,
            "ask": "SLA (freshness/availability/retention), contacts, access roles? (optional)",
            "tools": ["suggest_sla", "save_product_spec (slaProperties/team/roles)"],
        },
        {
            "n": 7, "name": "Rule Coach", "required": False,
            "captured": {},  # rules live on :PropertyShape, not the spec — not detected here
            "done": None,    # unknown from spec; treat as offered-not-verified
            "ask": "Any data-quality rules? (suggest_domain_rules → review_domain_rule; or create_user_rules)",
            "tools": ["suggest_domain_rules", "create_user_rules", "review_domain_rule"],
        },
        {
            "n": 8, "name": "Readiness Review", "required": False,
            "captured": {"serving_preference": (spec.get("customProperties") or {}).get("po_serving_preference")},
            "done": None,
            "ask": "Check serving strategy + AI-readiness before submit? (optional)",
            "tools": ["advise_serving_strategy", "get_osi_evaluation"],
        },
        {
            "n": 9, "name": "Confirm candidate sources", "required": True,
            "captured": {"bound_inputs": bound_inputs, "count": len(inputs)},
            "done": len(inputs) > 0,
            "ask": "Confirm the bound source(s); run a gap check before submitting.",
            "tools": ["find_source_products", "run_gap_analysis", "submit_product_spec"],
        },
    ]

    # Required gates (mirror the wizard's actual submit gate). Phrased as plain
    # missing-ingredient descriptions — NOT "Step N" — so they never get parroted
    # at the PO as wizard scaffolding.
    blocking = []
    if not (_has(spec.get("description"), spec.get("purpose"), _project_idea) and _has(spec.get("domain"))):
        blocking.append("purpose/description and domain not set")
    if len(props) == 0:
        blocking.append("no columns defined yet")
    if not _has(spec.get("name")):
        blocking.append("product not named yet")
    if len(inputs) == 0:
        blocking.append("no source product bound yet")

    # recommended_next = first required step not done, else first optional not done.
    nxt = next((s for s in steps if s["required"] and not s["done"]), None)
    if nxt is None:
        nxt = next((s for s in steps if s["done"] is False), None)

    return {
        "project_code": project_code,
        "archetype": "dpe-cf",
        "lifecycle_state": lifecycle,
        "how_to_use": (
            "PRIVATE checklist — do NOT read this to the PO. Do NOT say 'Step 1/2/3', "
            "do NOT recite these step names, numbers, or tool names. Scan `done`/`captured`/`blocking` "
            "to see what's still missing, then draw it out with natural business questions in whatever "
            "order the conversation flows. Optional items (shape, ops/SLA, rules, readiness) are offers, "
            "not mandatory steps — weave them in where they fit, or skip if the PO isn't interested; "
            "never default silently or invent an input. Required items must all be filled before submit."
        ),
        "steps": steps,
        "recommended_next": (
            {"step": nxt["n"], "name": nxt["name"], "ask": nxt["ask"], "tools": nxt["tools"],
             "required": nxt["required"]} if nxt else None
        ),
        "ready_to_submit": len(blocking) == 0,
        "blocking": blocking,
        "next": (
            "Ready — run a final run_gap_analysis, then submit_product_spec."
            if not blocking else
            f"{len(blocking)} required item(s) left: " + "; ".join(blocking)
            + ". Do the recommended_next step — ASK the PO for its inputs, don't invent."
        ),
    }


@po_mcp.tool()
def find_source_products(spec: dict, inferred_dependencies: list | None = None) -> dict:
    """Rank published UPSTREAM products against your derived-product spec — the
    MCP equivalent of the wizard's source-matching step. Candidates span ALL
    published kinds (source-aligned, aggregate, and consumer-aligned): a
    consumer/aggregate may build on any of them, forming multi-hop chains.

    `spec` is the partial ODCS dict (at least name + domain; optionally inputs[]).
    Returns per-slot ranked candidates (each with its `product_kind`) + a
    preselected match + a confidence band, or a gap suggestion when nothing
    matches. Put the chosen contract_ids into spec.inputs[] and save_product_spec.
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


@po_mcp.tool()
def submit_product_spec(
    project_code: str,
    submitted_by: str,
    notes: str = "",
    kind: str = "new",
    force: bool = False,
) -> dict:
    """Submit the consumer product's ODCS spec to engineering — the MCP
    equivalent of clicking Submit in the wizard.

    `kind` ∈ 'new' | 'edit'. Creates a ProductRequest (visible in engineering's
    Incoming queue) and flips the contract to 'submitted'. The engineer then
    accepts it and runs the mapping → serving pipeline.

    STRUCTURED GATE: refuses if the wizard's required steps aren't complete
    (domain+idea, ≥1 schema column, product name, ≥1 bound source) — the same
    gate the UI enforces. Returns the missing items; call get_authoring_plan to
    see the full step status. Pass force=True only to bypass deliberately.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.product_requests import submit_product_request, SubmitRequestBody
    from .routers.odcs import get_odcs as _get_odcs
    from .models import ProductRequestKind

    kind_map = {"new": ProductRequestKind.new, "edit": ProductRequestKind.edit}
    req_kind = kind_map.get(kind.strip().lower())
    if req_kind is None:
        return {"error": f"kind must be 'new' or 'edit' (got {kind!r})."}

    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}

        # Required-completeness gate (mirrors the wizard's submit gate). Skippable
        # with force=True. Reads actual saved spec, not caller claims.
        if not force:
            try:
                _doc = _get_odcs(project.id, session)
                _spec = (_doc or {}).get("spec") or {}
                _sch = (_spec.get("schema") or [{}])[0] if _spec.get("schema") else {}
                # The product's intent can live in description, purpose, OR the
                # project's product_idea — any one satisfies "what is this for".
                _has_intent = bool(
                    _spec.get("description") or _spec.get("purpose")
                    or (project.product_idea or "").strip()
                )
                _missing = []
                if not (_has_intent and _spec.get("domain")):
                    _missing.append("a purpose/description and a domain")
                if not (_sch.get("properties") or []):
                    _missing.append("at least one column")
                if not _spec.get("name"):
                    _missing.append("a product name")
                if not (_spec.get("inputs") or []):
                    _missing.append("at least one bound source")
                if _missing:
                    return {
                        "error": "Not ready to submit — still missing: " + "; ".join(_missing) + ".",
                        "missing": _missing,
                        "next": "Fill the missing item(s) via save_product_spec (bind a source with "
                                "find_source_products → spec.inputs), then submit. Bypass with force=True "
                                "only if intentional.",
                    }
            except HTTPException:
                pass  # if the spec can't be read, fall through to the submit attempt

        try:
            result = submit_product_request(
                project.id,
                SubmitRequestBody(kind=req_kind, submitted_by=submitted_by, notes=notes or None),
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
        "next": (
            "Handed to engineering. Track with get_product_status — the engineer accepts "
            "the request, runs mapping + serving, then you deploy_product."
        ),
    }


# ---------------------------------------------------------------------------
# Consumer wizard ASSIST tools — the "intelligence" behind the wizard steps.
#
# Data entry (name/desc/SLA/columns) rides in save_product_spec; these tools give
# the agent the same guided help the UI wizard gets from its skills, so a PO can
# author a consumer product with full parity from /po-mcp. Each wraps the exact
# endpoint the wizard calls. Role asserted = Data Product Owner where gated.
# ---------------------------------------------------------------------------

@po_mcp.tool()
def list_scoring_rubrics() -> dict:
    """List available scoring rubrics for step 1 (rubric picker). Pass the chosen
    id as spec.scoringRubric in save_product_spec ('osi' is the default/recommended).
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .routers.osi import list_scoring_rubrics as _h
    try:
        return _h()
    except Exception as e:  # noqa: BLE001
        return {"error": f"Rubric list failed: {e}"}


@po_mcp.tool()
async def discover_product_columns(domain: str, idea: str) -> dict:
    """Step 1 discovery — given a domain + plain-language idea, return a starter
    column set plus similar existing products and matching templates. Use before
    the product exists to seed the schema.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.domain_catalogs import discover_schema as _h, RecommendRequest
    with Session(engine) as session:
        try:
            return await _h(domain, RecommendRequest(idea=idea), session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"Discovery failed: {e}"}


@po_mcp.tool()
async def recommend_schema(
    domain: str,
    idea: str,
    name: str = "",
    description: str = "",
    purpose: str = "",
    grain: str = "",
    scd_policy: str = "",
    grouping_keys: list | None = None,
    source_contract_ids: list | None = None,
) -> dict:
    """Step 4 schema advisor — rank catalog columns for this product against the
    idea/description + dataset shape + the source products it will CONSUME, with
    per-column relevance / feasibility / grain-alignment. This is the guided help
    behind "Shape the Schema"; take the recommended columns into save_product_spec.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.domain_catalogs import recommend_schema as _h, RecommendRequest, ShapeContext
    shape = ShapeContext(
        grain=grain or "", scd_policy=scd_policy or "",
        grouping_keys=list(grouping_keys or []),
    )
    with Session(engine) as session:
        try:
            return await _h(
                domain,
                RecommendRequest(
                    idea=idea, name=name or None, description=description or None,
                    purpose=purpose or None, shape=shape,
                    source_contract_ids=list(source_contract_ids or []),
                ),
                session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"Recommend failed: {e}"}


@po_mcp.tool()
async def check_filter(project_code: str, intent: str, columns: list | None = None) -> dict:
    """Step 2 filter check — compile a plain-language row filter ("only active
    employees") into a SQL predicate preview + confidence + warnings. PREVIEW ONLY
    (like the UI): persist the plain-language `intent` as spec.transform.filter_intent;
    the engineer compiles the real SQL at serving time.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    # interpret_filter_intent is a keyword-only CORE function (not the HTTP
    # handler): intent + columns(list[dict]) + project(Project) + dialect.
    from .routers.filter_intent import interpret_filter_intent as _h
    cols = [{"name": c["name"]} for c in (columns or []) if isinstance(c, dict) and c.get("name")]
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = await _h(intent=intent, columns=cols, project=project, dialect="postgres")
        except Exception as e:  # noqa: BLE001
            return {"error": f"Filter check failed: {e}"}
    return {
        "readback": result.readback,
        "predicate": result.predicate,
        "confidence": result.confidence,
        "warnings": result.warnings,
        "grounded_columns": result.grounded_columns,
        "next": "Preview only — persist the plain-language intent as spec.transform.filter_intent; "
                "the engineer compiles the real SQL at serving time.",
    }


@po_mcp.tool()
def suggest_sla(project_code: str) -> dict:
    """Step 6 — suggest SLA defaults inherited from the CONSUMES'd sources
    (availability / freshness / retention). Offer them to the PO; put accepted
    ones in spec.slaProperties. Requires the project to exist (save_product_spec first).
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.odcs import suggest_sla as _h
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _h(project.id, session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"SLA suggest failed: {e}"}


@po_mcp.tool()
def suggest_domain_rules(project_code: str, persist: bool = True) -> dict:
    """Step 7 Rule Coach — suggest domain-quality rules for the product's columns
    from the domain catalog. With persist=True they land as pending_review rules
    you then approve/reject with review_domain_rule. Call after save_product_spec.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.odcs import suggest_domain_rules as _h, SuggestDomainRulesInput
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _h(project.id, SuggestDomainRulesInput(persist=persist), session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"Rule suggest failed: {e}"}


@po_mcp.tool()
def create_user_rules(project_code: str, rules: list, created_by: str = "") -> dict:
    """Step 7 — author custom (PO-defined) quality rules. `rules` is a list of
    {column, rule_type, severity?, description?, params?}. They persist as
    ruleSource='user' and are auto-approved. Call after save_product_spec.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.odcs import persist_user_rules as _h, PersistUserRulesInput, UserRuleInput
    rule_objs = []
    for r in rules:
        if isinstance(r, dict) and r.get("column") and r.get("rule_type"):
            rule_objs.append(UserRuleInput(
                column=r["column"], rule_type=r["rule_type"],
                severity=r.get("severity") or "sh:Warning",
                description=r.get("description") or "", params=r.get("params"),
            ))
    if not rule_objs:
        return {"error": "rules must be a list of {column, rule_type, severity?, description?, params?}"}
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _h(project.id, PersistUserRulesInput(rules=rule_objs, created_by=created_by or _principal()), session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"User rule create failed: {e}"}


@po_mcp.tool()
def review_domain_rule(
    project_code: str, rule_uri: str, action: str,
    quality: int = 2, category: str = "", detail: str = "",
) -> dict:
    """Step 7 — approve or reject a suggested domain rule (the finalize() rule pass).
    action='approve' (with quality 1-3) or 'reject' (with category + detail).
    Do this for each rule before submit_product_spec.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    if action not in ("approve", "reject"):
        return {"error": "action must be 'approve' or 'reject'"}
    from fastapi import HTTPException
    from .routers.reviews import review_domain_rule as _h, DomainRuleReviewAction
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        body = DomainRuleReviewAction(
            action=action, rule_uri=rule_uri, reviewer=_principal(),
            quality=quality if action == "approve" else None,
            category=(category or "other") if action == "reject" else None,
            detail=detail or ("Product Owner declined at authoring time" if action == "reject" else None),
        )
        try:
            result = _h(project.id, body, session)
            return {"ok": True, **(result if isinstance(result, dict) else {})}
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"Rule review failed: {e}"}


@po_mcp.tool()
async def advise_serving_strategy(project_code: str) -> dict:
    """Step 8 — recommend virtual-view vs dbt-materialized serving for this product,
    with rationale + considerations. Store the choice via
    spec.customProperties.po_serving_preference in save_product_spec.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.serving_strategy import advise as _h, AdviseBody
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return await _h(project.id, AdviseBody(), session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"Serving advice failed: {e}"}


@po_mcp.tool()
def get_osi_evaluation(project_code: str) -> dict:
    """Step 8 — read the product's OSI (AI-readiness) score before submitting:
    band + completeness + the failing criteria (heaviest first). Trigger scoring
    on the engineering side if none exists yet.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.osi import get_latest_evaluation as _h
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            raw = _h(project.id, session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"OSI read failed: {e}"}
    ev = raw.get("evaluation") if isinstance(raw, dict) else None
    if not ev:
        return {"scored": False, "next": "No OSI score yet — score it from engineering (trigger_osi_score)."}
    checklist = ev.get("checklist") or []
    attention = [c for c in checklist if c.get("status") in ("fail", "partial")]
    return {
        "scored": True,
        "band": ev.get("band"),
        "completeness": ev.get("completeness"),
        "needs_attention": [
            {"criterion": c.get("criterion"), "status": c.get("status"),
             "weight": c.get("weight"), "reason": (c.get("reason") or "")[:200]}
            for c in sorted(attention, key=lambda c: -(c.get("weight") or 0))
        ],
        "next": "GREEN = ready." if ev.get("band") == "green" else "Address needs_attention before submit if you want a stronger score.",
    }


@po_mcp.tool()
async def trigger_osi(project_code: str, skip_advisor: bool = True, trigger: str = "manual") -> dict:
    """Score the product's OSI (AI-readiness) NOW — the MCP equivalent of the
    Product Workbench "Score OSI now" button. Runs the deterministic
    translate→validate→score pipeline; pass skip_advisor=False to also run the
    LLM advisor for a narrative + Apply-card suggestions (~30-60s). Persists the
    :OsiEvaluation; read it back with get_osi_evaluation. get_osi_evaluation is
    read-only — use this when there's no score yet, or to refresh after edits.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.osi import evaluate as _evaluate, EvaluateBody
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            raw = await _evaluate(
                project.id, EvaluateBody(skip_advisor=skip_advisor, trigger=trigger),
                session=session,
            )
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"OSI evaluation failed: {e}"}
    checklist = raw.get("checklist") or []
    attention = [c for c in checklist if c.get("status") in ("fail", "partial")]
    return {
        "scored": True,
        "band": raw.get("band"),
        "completeness": raw.get("completeness"),
        "conformance_pass": raw.get("conformance_pass"),
        "needs_attention": [
            {"criterion": c.get("criterion"), "status": c.get("status"),
             "weight": c.get("weight"), "reason": (c.get("reason") or "")[:200]}
            for c in sorted(attention, key=lambda c: -(c.get("weight") or 0))
        ],
        "narrative": raw.get("narrative"),
        "advisor_error": raw.get("advisor_error"),
        "next": "GREEN = ready to submit." if raw.get("band") == "green"
                else "Address needs_attention to raise the score.",
    }


@po_mcp.tool()
def get_discovery_inventory(project_code: str) -> dict:
    """Estate Discovery — the object-grain inventory for an estate-discovery
    project: every discovered object with its disposition (migrate / modernize /
    retire / remain), lineage edges, published source-product nodes, and slicer
    facets. This is the read behind the Product Workbench's Discovery table +
    lineage DAG. `source` = 'imported' (a real estate was ingested) or 'fixture'
    (sample). Acting on an object (send to intake) is currently a UI action.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.discovery import get_estate_inventory as _h
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            return _h(project.id, session)
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"Discovery inventory read failed: {e}"}


@po_mcp.tool()
def run_gap_analysis(
    project_code: str,
    consumer_columns: list,
    candidate_contract_ids: list | None = None,
    consumer_idea: str = "",
    consumer_description: str = "",
) -> dict:
    """Pre-flight gap check — compare the product's columns against the bound
    source products; returns per-column covered / derivable / ambiguous / gap with
    evidence. `consumer_columns` = [{name, logical_type, description}]. Run before
    submit; unresolved gaps are a soft warning, not a hard block.

    `candidate_contract_ids` is optional: when omitted, the bound sources are read
    straight from the saved spec (`inputs[]`), so the check just works once
    sources are bound — no need to re-pass them.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.ingest_products import gap_analysis_for_project as _h, GapAnalysisBody, GapAnalysisColumnInput
    with Session(engine) as session:
        project = session.exec(select(Project).where(Project.project_code == project_code)).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        # Fall back to the sources already bound on the saved spec when the caller
        # didn't pass any explicitly — mirrors what the wizard's step-9 gap check does.
        cand = list(candidate_contract_ids or [])
        if not cand:
            try:
                from .routers.odcs import get_odcs as _get_odcs
                _doc = _get_odcs(project.id, session)
                _inputs = ((_doc or {}).get("spec") or {}).get("inputs") or []
                cand = [i.get("contract_id") for i in _inputs
                        if isinstance(i, dict) and i.get("contract_id")]
            except Exception:
                cand = []
        cols = [
            GapAnalysisColumnInput(
                name=c["name"], logical_type=c.get("logical_type") or c.get("type"),
                description=c.get("description"),
            )
            for c in consumer_columns if isinstance(c, dict) and c.get("name")
        ]
        try:
            result = _h(
                project.id,
                GapAnalysisBody(
                    consumer_idea=consumer_idea, consumer_description=consumer_description,
                    consumer_domain=project.domain or "", consumer_columns=cols,
                    candidate_contract_ids=cand,
                ),
                session,
            )
            return result if isinstance(result, dict) else {"gaps": result}
        except HTTPException as e:
            return {"error": f"{e.detail}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"Gap analysis failed: {e}"}


# ---------------------------------------------------------------------------
# Triage layer for the PO validation gate.
#
# Philosophy (agent-as-UI): the terminal is a DECISION surface, not a browsing
# surface. Don't uniformly truncate 39 items — TRIAGE them. Separate the routine
# (batch-approvable) from the exceptional (needs a human), using anomaly flags
# derived from data already in the graph. The agent narrates the summary, shows
# the flagged items in full, and links to the exact UI screen for the rest.
# ---------------------------------------------------------------------------

# flag -> short human phrase used to build the one-line triage summary
_FLAG_PHRASES = {
    "sensitive": "sensitive column unmasked",
    "possibly_sensitive": "possible PII/sensitive column",
    "low_confidence": "low-confidence rule",
    "missing_description": "missing description",
    "thin_description": "very short description",
    "unclassified": "unclassified table",
}

# Cheap, no-LLM PII/sensitivity heuristic on the column NAME. The graph's
# col.sensitivity property is rarely populated during discovery, so relying on
# it alone lets an obvious salary / DOB / SSN column slip through as "routine".
# This name-substring pass is the light triage-scoring step that catches them —
# it emits a *possibly_sensitive* flag (distinct from the graph-confirmed
# *sensitive*) so the PO knows it's a heuristic, not an asserted classification.
_SENSITIVE_NAME_HINTS = (
    "salary", "compensation", "wage", "income", "bonus", "payroll", "contract_total",
    "ssn", "social_security", "national_id", "passport", "license", "tax_id", "taxid",
    "email", "phone", "address", "birth_date", "birthdate", "date_of_birth", "dob",
    "credit_card", "account_number", "routing",
)


def _name_is_sensitive(name: str | None) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _SENSITIVE_NAME_HINTS)


def _as_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _triage_names(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        flags = []
        cur, rec = it.get("current_name"), it.get("recommended_name")
        sens = (it.get("sensitivity") or "none")
        if sens not in ("none", "", None):
            flags.append("sensitive")            # graph-asserted classification
        elif _name_is_sensitive(cur):
            flags.append("possibly_sensitive")   # name-heuristic catch
        out.append({
            "tab": "names",
            "item_uri": it.get("col_uri"),
            "label": f"{it.get('schema')}.{it.get('table_name')}.{cur}"
                     + (f" → {rec}" if rec and rec != cur else " (no rename)"),
            "data_type": it.get("data_type"),
            "flags": flags,
        })
    return out


def _triage_descriptions(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        text = (it.get("description_text") or "").strip()
        flags = []
        if not text:
            flags.append("missing_description")
        elif len(text) < 15:
            flags.append("thin_description")
        out.append({
            "tab": "descriptions",
            "item_uri": it.get("desc_uri"),
            "label": f"{it.get('schema')}.{it.get('table_name')}.{it.get('col_name')}",
            "preview": text[:100],
            "flags": flags,
        })
    return out


def _triage_rules(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        conf = _as_float(it.get("confidence"))
        flags = []
        if conf is not None and conf < 0.7:
            flags.append("low_confidence")
        out.append({
            "tab": "rules",
            "item_uri": it.get("rule_uri"),
            "label": f"{it.get('schema')}.{it.get('table_name')}.{it.get('col_name')}",
            "rule": f"{it.get('rule_type')} ({it.get('severity')})",
            "confidence": conf,
            "preview": (it.get("description") or "")[:100],
            "flags": flags,
        })
    return out


def _triage_tables(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        text = (it.get("description_text") or "").strip()
        flags = []
        if (it.get("relationship_kind") or "unknown") == "unknown":
            flags.append("unclassified")
        if not text:
            flags.append("missing_description")
        out.append({
            "tab": "tables",
            "item_uri": it.get("desc_uri"),
            "label": f"{it.get('schema')}.{it.get('table_name')}",
            "relationship_kind": it.get("relationship_kind"),
            "preview": text[:100],
            "flags": flags,
        })
    return out


def _triage_relationships(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        text = (it.get("description_text") or "").strip()
        flags = ["missing_description"] if not text else []
        out.append({
            "tab": "relationships",
            "item_uri": it.get("desc_uri"),
            "label": f"{it.get('from_table')} → {it.get('to_table')}",
            "nature": it.get("relationship_nature"),
            "preview": text[:100],
            "flags": flags,
        })
    return out


@po_mcp.tool()
def get_pending_validations(project_code: str) -> dict:
    """Return items pending PO review in the source validation gate — TRIAGED
    for a terminal, not dumped.

    Instead of a flat wall of 39 items, this separates the ROUTINE (safe to
    bulk-approve) from the ones that NEED YOUR EYES (a sensitive column left
    unmasked, a low-confidence rule, a missing/thin description, an unclassified
    table). The `needs_attention` list is shown in full so you can decide on each;
    the routine items are summarised with a small sample. `web_url` deep-links to
    the exact validation screen in the UI for anything you'd rather see there.

    Recommended flow: review the `needs_attention` items (approve/reject each via
    review_validation_item), then bulk_approve_source_validation for the rest.
    """
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from .config import FRONTEND_URL
    from .routers.reviews import get_source_product_validation

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        result = get_source_product_validation(project.id, session=session)
        project_id = project.id

    web_url = f"{FRONTEND_URL}/product/validate/{project_id}"
    total = result.get("count", 0)

    if total == 0:
        return {
            "project_code": project_code,
            "total_pending": 0,
            "web_url": web_url,
            "next": "No pending items — validation gate is clear. Engineering can proceed.",
        }

    triaged = (
        _triage_names(result.get("column_names", []))
        + _triage_descriptions(result.get("descriptions", []))
        + _triage_rules(result.get("rules", []))
        + _triage_tables(result.get("table_descriptions", []))
        + _triage_relationships(result.get("relationship_descriptions", []))
    )

    needs_attention = [t for t in triaged if t["flags"]]
    routine = [t for t in triaged if not t["flags"]]

    # per-tab breakdown
    by_tab: dict[str, dict] = {}
    for t in triaged:
        b = by_tab.setdefault(t["tab"], {"total": 0, "routine": 0, "needs_attention": 0})
        b["total"] += 1
        b["needs_attention" if t["flags"] else "routine"] += 1

    # one-line summary the agent can read aloud
    flag_counts: dict[str, int] = {}
    for t in needs_attention:
        for f in t["flags"]:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    if flag_counts:
        reasons = ", ".join(
            f"{n} {_FLAG_PHRASES.get(f, f)}" + ("s" if n > 1 else "")
            for f, n in sorted(flag_counts.items(), key=lambda x: -x[1])
        )
        summary = (
            f"{total} items: {len(routine)} routine, {len(needs_attention)} need "
            f"your eyes ({reasons})."
        )
        next_step = (
            f"Review the {len(needs_attention)} flagged item(s) in needs_attention — "
            f"approve or reject each with review_validation_item. Then clear the "
            f"remaining {len(routine)} routine item(s) with bulk_approve_source_validation."
        )
    else:
        summary = f"{total} items, all routine — nothing flagged for special attention."
        next_step = (
            "Nothing flagged. Safe to clear the whole gate with "
            "bulk_approve_source_validation(project_code)."
        )

    return {
        "project_code": project_code,
        "total_pending": total,
        "web_url": web_url,
        "triage": {
            "routine": len(routine),
            "needs_attention": len(needs_attention),
            "summary": summary,
        },
        "by_tab": by_tab,
        # SHOW these in full — they carry a decision.
        "needs_attention": needs_attention,
        # A small sample so the agent can characterize the routine bucket
        # ("18 standard renames") without dumping all of them.
        "routine_sample": routine[:5],
        "next": next_step,
    }


@po_mcp.tool()
def review_validation_item(
    project_code: str,
    review_type: str,
    action: str,
    item_uri: str,
    quality: int = 2,
    reason: str = "",
) -> dict:
    """Approve or reject a single validation item in the PO source validation gate.

    review_type: names | descriptions | tables | relationships | rules
    action: approve | reject
    item_uri: the col_uri, desc_uri, or rule_uri from get_pending_validations
    quality: 1=Acceptable, 2=Good, 3=Excellent (for approve only)
    reason: required for reject

    Prefer bulk_approve_source_validation when you want to approve everything —
    use this tool only when you need to reject a specific item.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.reviews import review_source_product, SourceProductValidationAction

    _ACTION_MAP = {
        ("names",         "approve"): ("approve_name",         "col_uri"),
        ("names",         "reject"):  ("reject_name",          "col_uri"),
        ("descriptions",  "approve"): ("approve_description",  "desc_uri"),
        ("descriptions",  "reject"):  ("reject_description",   "desc_uri"),
        ("tables",        "approve"): ("approve_table",        "desc_uri"),
        ("tables",        "reject"):  ("reject_table",         "desc_uri"),
        ("relationships", "approve"): ("approve_relationship", "desc_uri"),
        ("relationships", "reject"):  ("reject_relationship",  "desc_uri"),
        ("rules",         "approve"): ("approve_rule",         "rule_uri"),
        ("rules",         "reject"):  ("reject_rule",          "rule_uri"),
    }
    key = (review_type, action)
    if key not in _ACTION_MAP:
        return {"error": f"Unknown review_type/action: {review_type}/{action}. "
                         f"review_type ∈ names|descriptions|tables|relationships|rules, action ∈ approve|reject"}

    action_str, uri_field = _ACTION_MAP[key]
    kwargs: dict[str, Any] = {
        "action": action_str,
        "reviewer": _principal(),
        uri_field: item_uri,
    }
    if action == "approve":
        kwargs["quality"] = quality
    elif action == "reject":
        if not reason:
            return {"error": "reason is required when action=reject"}
        kwargs["detail"] = reason

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            review_source_product(
                project.id, SourceProductValidationAction(**kwargs), session
            )
        except HTTPException as e:
            return {"error": f"Review failed: {e.detail}"}

    return {
        "ok": True,
        "action": action,
        "review_type": review_type,
        "item_uri": item_uri,
        "next": "Call get_pending_validations to check remaining items.",
    }


@po_mcp.tool()
def bulk_approve_source_validation(
    project_code: str,
    tab: str = "all",
    quality: int = 2,
) -> dict:
    """Approve all pending PO validation items in one call.

    tab: names | descriptions | tables | relationships | rules | all (default: all)
    quality: 1=Acceptable, 2=Good, 3=Excellent (default: 2=Good)

    Clears the validation gate so engineering can proceed to materialization.
    Use review_validation_item first if you need to reject specific items.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.reviews import approve_all_source_product, SourceProductValidationBulkApprove

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = approve_all_source_product(
                project.id,
                SourceProductValidationBulkApprove(
                    tab=tab, quality=quality, reviewer=_principal()
                ),
                session,
            )
        except HTTPException as e:
            return {"error": f"Bulk approve failed: {e.detail}"}

    approved = result.get("approved", {})
    total = sum(approved.values())
    return {
        "ok": True,
        "approved_by_tab": approved,
        "total_approved": total,
        "next": (
            "Validation gate cleared. Engineering can now run the materialization stages."
            if total > 0
            else "No pending items to approve — gate was already clear."
        ),
    }


# ---------------------------------------------------------------------------
# PO ↔ Engineer feedback loops.
#
# Two engineer→PO escalations that were previously only resolvable in the UI:
#   • consumer-pushback         — a consumer engineer pushes back on an upstream
#                                 source change; the SOURCE PO accepts (will
#                                 revise) or dismisses (keep as-is).
#   • source-candidates-needed  — a consumer engineer can't pick source tables;
#                                 the CONSUMER PO acknowledges (will refine the
#                                 candidate-sources list) or dismisses.
# Both surface on the PO's dashboard as orange cards — these tools are the CLI
# equivalent so the loop closes without opening the UI.
# ---------------------------------------------------------------------------

@po_mcp.tool()
def list_incoming_pushbacks(owner_email: str = "") -> dict:
    """List pending consumer-pushback requests waiting on you (the source PO).

    A consumer engineer flagged an upstream change to one of your source
    products. Each item carries the consumer's note. Resolve with resolve_pushback.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .routers.edits import list_incoming_pushbacks as _h

    with Session(engine) as session:
        result = _h(owner_email=owner_email or None, session=session)

    items = result.get("items", [])
    return {
        "items": items,
        "count": len(items),
        "next": (
            f"{len(items)} pushback(s) waiting — call resolve_pushback(request_id, "
            f"action='accept'|'dismiss') for each."
            if items else "No incoming pushbacks. Nothing to resolve."
        ),
    }


@po_mcp.tool()
def resolve_pushback(request_id: int, action: str, resolution_note: str = "") -> dict:
    """Resolve a consumer-pushback: 'accept' (you'll revise the source) or
    'dismiss' (keep the source as-is). request_id comes from list_incoming_pushbacks.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.edits import resolve_pushback as _h, PushbackResolveInput

    if action not in ("accept", "dismiss"):
        return {"error": "action must be 'accept' or 'dismiss'"}
    with Session(engine) as session:
        try:
            result = _h(
                PushbackResolveInput(
                    request_id=request_id, action=action,
                    resolution_note=resolution_note or None,
                ),
                session=session,
            )
        except HTTPException as e:
            return {"error": f"Resolve failed: {e.detail}"}

    return {
        "ok": True,
        "request_id": result.get("request_id"),
        "status": result.get("status"),
        "next": (
            "Accepted — revise the source product (edit its spec) so the consumer's "
            "concern is addressed."
            if action == "accept"
            else "Dismissed — the source stays as-is; the consumer engineer is notified."
        ),
    }


@po_mcp.tool()
def list_source_candidate_requests(owner_email: str = "") -> dict:
    """List pending source-candidates-needed requests waiting on you (the
    consumer PO).

    A consumer engineer can't find source tables to map and needs you to identify
    (or create) source-aligned products. Each item may carry a gap_column_uri +
    gap_reason. Resolve with resolve_source_candidate_request.
    """
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .routers.edits import list_source_candidate_requests as _h

    with Session(engine) as session:
        result = _h(owner_email=owner_email or None, session=session)

    items = result.get("items", [])
    return {
        "items": items,
        "count": len(items),
        "next": (
            f"{len(items)} request(s) waiting — for each, either point the engineer at an "
            f"existing source product (list_marketplace) or create one (create_source_product), "
            f"then resolve_source_candidate_request(request_id, action='accept'|'dismiss')."
            if items else "No source-candidate requests. Nothing to resolve."
        ),
    }


@po_mcp.tool()
def resolve_source_candidate_request(
    request_id: int, action: str, resolution_note: str = ""
) -> dict:
    """Resolve a source-candidates-needed request: 'accept' (you'll refine the
    candidate-sources list) or 'dismiss' (no action). request_id comes from
    list_source_candidate_requests.
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.edits import (
        resolve_source_candidate_request as _h,
        SourceCandidateRequestResolveInput,
    )

    if action not in ("accept", "dismiss"):
        return {"error": "action must be 'accept' or 'dismiss'"}
    with Session(engine) as session:
        try:
            result = _h(
                SourceCandidateRequestResolveInput(
                    request_id=request_id, action=action,
                    resolution_note=resolution_note or None,
                ),
                session=session,
            )
        except HTTPException as e:
            return {"error": f"Resolve failed: {e.detail}"}

    return {
        "ok": True,
        "request_id": result.get("request_id"),
        "status": result.get("status"),
        "next": (
            "Accepted — add the source product(s) the engineer needs (create_source_product "
            "if none exist yet), then the consumer product can bind them."
            if action == "accept"
            else "Dismissed — no source candidates will be added."
        ),
    }


@po_mcp.tool()
def deploy_product(project_code: str) -> dict:
    """Publish the data product to the marketplace — the final PO action after
    engineering marks the product complete.

    Sets the contract lifecycleState to 'published', reflects the deployment
    target into the ODCS server entry, and makes the product discoverable in
    the marketplace. Call get_product_status first to confirm engineering is done
    (request_status should be 'complete').
    """
    if (_ro := _deny_write()) is not None:
        return _ro
    if (denied := _deny_project(project_code)) is not None:
        return denied
    from fastapi import HTTPException
    from .routers.odcs import publish_data_product

    with Session(engine) as session:
        project = session.exec(
            select(Project).where(Project.project_code == project_code)
        ).first()
        if project is None:
            return {"error": f"No project with code '{project_code}'"}
        try:
            result = publish_data_product(
                project.id, session=session, user=_principal()
            )
        except HTTPException as e:
            return {"error": f"Publish failed: {e.detail}"}

    return {
        "ok": True,
        "project_code": project_code,
        "published_count": result.get("count", 0),
        "next": (
            "Product is now live in the marketplace. "
            "Consumer products can now discover and CONSUME it. "
            "Use list_marketplace to confirm it appears."
        ),
    }


# ── Inbound intake review (modernization portfolios) ────────────────────────
# The PO's headless review/approve surface for MODERNIZATION intakes. Submission
# is REST-only (scoped machine credential); these mirror the web review UI.


@po_mcp.tool()
def list_intake_submissions(scenario: str | None = "modernization", status: str | None = None) -> dict:
    """List staged inbound-intake submissions (default scenario: modernization)
    with each one's status and confidence-graded portfolio blueprint."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from .routers.intake import list_intake
    with Session(engine) as session:
        return list_intake(scenario=scenario, status=status, session=session)


@po_mcp.tool()
def get_intake_submission(intake_id: int) -> dict:
    """Get one intake submission: scenario, status, parsed blueprint,
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


@po_mcp.tool()
def approve_intake_submission(intake_id: int, expected_revision: int) -> dict:
    """Approve a reviewed portfolio blueprint and run the scaffold saga
    (resumable, idempotent). `expected_revision` must equal the current
    blueprint_revision and all gaps must be resolved first."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.intake import ApproveBody, approve
    with Session(engine) as session:
        try:
            return approve(intake_id, ApproveBody(expected_revision=expected_revision), session=session)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
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


# ── Connected Estate + top-down feasibility (PO self-service) ─────────────────
# A separate bounded context from the Pulse-backed get_discovery_inventory above:
# here DW connects to a live platform, scans the estate, and evaluates whether a
# catalog of desired reference data-product specs is buildable (ready | adaptable
# | assemblable | absent). Every tool delegates to a router handler (zero Cypher).

def _po_owner(owner_email: str):
    """Synthesize an owner AuthUser for delegating to role-gated router handlers.
    The MCP's own _deny_write/_serving_guard already enforce the trust boundary;
    the router's require_role('owner') is a no-op under the MCP token model."""
    from .auth import AuthUser
    email = owner_email or _principal()
    return AuthUser(email=email, name=email, role="owner")


@po_mcp.tool()
def create_estate(name: str, owner_email: str, domain: str = "", description: str = "") -> dict:
    """Create a Connected Estate — a stable business scope to scan for top-down
    data-product feasibility. Then add a source with add_estate_source."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.estates import EstateCreate, create_estate as _h
    with Session(engine) as session:
        try:
            return _h(EstateCreate(name=name, domain=domain or None, description=description),
                      session=session, user=_po_owner(owner_email), _role=None)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def add_estate_source(estate_id: int, owner_email: str,
                      connection_id: int | None = None,
                      name: str = "", catalog: str = "", namespace_mode: str = "all",
                      namespaces: list[str] | None = None,
                      ingest_mode: str = "live", platform: str = "") -> dict:
    """Attach a source to an estate. Two modes:
    - live (default): pass a registered connection_id — DW connects + scans itself.
      catalog scopes 3-level platforms (Databricks/Snowflake) to one Unity Catalog /
      database (one source per catalog; discover via list_estate_catalogs).
    - offline: pass ingest_mode='offline' + platform (no connection/creds) — the
      client runs the extraction kit (get_estate_extraction_package) in their own
      environment and you import the manifest via import_estate_scan.
    namespace_mode ∈ {all, include, exclude}; namespaces filters schemas."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.estates import SourceCreate, add_source as _h
    policy = {"mode": namespace_mode, "namespaces": namespaces or []}
    with Session(engine) as session:
        try:
            return _h(estate_id, SourceCreate(connection_id=connection_id, name=name,
                                              catalog=catalog, namespace_policy=policy,
                                              ingest_mode=ingest_mode, platform=platform),
                      session=session, _role=None)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def list_estate_catalogs(estate_id: int, connection_id: int) -> dict:
    """List a connection's Unity Catalog / database catalogs (3-level platforms
    only) so you can pick one for add_estate_source(catalog=...). 2-level platforms
    return supported=false."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.estates import list_connection_catalogs as _h
    with Session(engine) as session:
        try:
            return _h(estate_id, connection_id, session=session, _role=None)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def update_estate_source(estate_id: int, source_id: int, owner_email: str,
                         name: str | None = None, enabled: bool | None = None,
                         namespace_mode: str | None = None,
                         namespaces: list[str] | None = None,
                         catalog: str | None = None) -> dict:
    """Edit a source: rename, enable/disable, or save its schema selection
    (namespace_mode ∈ {all, include, exclude} + namespaces). catalog is immutable
    once the source has been scanned (delete + re-add for a different catalog)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.estates import SourceUpdate, update_source as _h
    policy = None
    if namespace_mode is not None or namespaces is not None:
        policy = {"mode": namespace_mode or "include", "namespaces": namespaces or []}
    with Session(engine) as session:
        try:
            return _h(estate_id, source_id,
                      SourceUpdate(name=name, enabled=enabled,
                                   namespace_policy=policy, catalog=catalog),
                      session=session, _role=None)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def remove_estate_source(estate_id: int, source_id: int, owner_email: str) -> dict:
    """Hard-delete a source and cascade its scans, graph subtree, and any
    feasibility runs whose evidence included it. Refused while a scan is active."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.estates import delete_source as _h
    with Session(engine) as session:
        try:
            return _h(estate_id, source_id, session=session, _role=None)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def list_estate_namespaces(estate_id: int, source_id: int, with_counts: bool = False) -> dict:
    """Browse a source's live namespace tree (schemas within the connected
    database/catalog) before choosing a scan policy."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.estates import list_source_namespaces as _h
    with Session(engine) as session:
        try:
            return _h(estate_id, source_id, with_counts=with_counts, session=session)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def run_estate_scan(estate_id: int, source_id: int, owner_email: str,
                    depth: str = "metadata") -> dict:
    """Queue an async metadata scan of an estate source (the leased worker runs it).
    Returns the queued scan; poll get_estate_scan for its state + per-namespace
    outcomes. One active scan per source."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.estates import ScanCreate, launch_scan as _h
    with Session(engine) as session:
        try:
            return _h(estate_id, ScanCreate(source_id=source_id, depth=depth),
                      session=session, user=_po_owner(owner_email), _role=None)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def get_estate_scan(scan_id: int) -> dict:
    """Get a scan's state (queued|running|completed|partial|failed), diff stats,
    and per-namespace outcomes (scanned/empty/inaccessible/…)."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.estates import get_scan as _h
    with Session(engine) as session:
        try:
            return _h(scan_id, session=session)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def get_estate_scan_datasets(scan_id: int) -> dict:
    """List the raw datasets + columns observed by a completed estate scan.
    Use to inspect the schema inventory before requesting a feasibility evaluation."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.estates import scan_datasets as _h
    with Session(engine) as session:
        try:
            return _h(scan_id, session=session)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def get_estate_scan_assets(scan_id: int) -> dict:
    """List the code assets (tasks, notebooks, pipelines, dynamic tables) observed
    by an estate scan. Only populated for platforms with code-asset support
    (Snowflake, Databricks). Returns name, asset_kind, namespace, language,
    schedule, definition_preview, depends_on, and extra metadata."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.estates import scan_assets as _h
    with Session(engine) as session:
        try:
            return _h(scan_id, session=session)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def get_estate_extraction_package(source_id: int) -> dict:
    """Get the client-run, OFFLINE metadata extraction kit for an offline estate
    source (add one with add_estate_source(ingest_mode='offline', platform=...)).
    Returns a {files: {path: content}} map: a stdlib-only run.py, per-platform
    requirements.txt, .env.example (the WB_SOURCE_* credential blanks DW never
    sees), manifest_config.json, and a README with two diagrams. The client runs
    it read-only in THEIR environment, reviews the produced estate-manifest-<ts>.yaml,
    and hands it back for import_estate_scan. No DW dependency, no LLM inside."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.estates import get_extraction_package as _h
    with Session(engine) as session:
        try:
            return _h(source_id, format="json", session=session, _role=None)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def import_estate_scan(source_id: int, owner_email: str, manifest_yaml: str,
                       preview_only: bool = False) -> dict:
    """Import an offline extraction manifest (the estate-manifest-<ts>.yaml the client
    produced with the extraction kit) as a real :EstateScan — identical shape to a
    live scan, so enrichment + feasibility grading work unchanged. Fail-closed:
    a malformed manifest is rejected before anything touches the graph. Set
    preview_only=true to validate + summarize (counts, redaction, PII columns)
    without writing. Pass the manifest's full YAML text as manifest_yaml."""
    if not preview_only and (_ro := _deny_write()) is not None:
        return _ro
    from . import estate_ingest, estate_manifest
    from .models import EstateSource, Estate
    with Session(engine) as session:
        source = session.get(EstateSource, source_id)
        if source is None:
            return {"error": f"Source {source_id} not found"}
        estate = session.get(Estate, source.estate_id)
        if estate is None:
            return {"error": f"Estate {source.estate_id} not found"}
        try:
            if preview_only:
                return estate_ingest.preview_estate_manifest(manifest_yaml)
            manifest = estate_manifest.load_manifest(manifest_yaml)
            if source.platform and manifest.platform != source.platform.lower():
                return {"error": f"manifest platform '{manifest.platform}' does not "
                                 f"match source platform '{source.platform}'."}
            result = estate_ingest.import_estate_manifest(
                session, estate=estate, source=source, manifest=manifest,
                initiated_by=_po_owner(owner_email).email)
            return {"imported": True, **result}
        except estate_manifest.EstateManifestValidationError as e:
            return {"error": "manifest failed validation", "detail": str(e)}


@po_mcp.tool()
def list_feasibility_specs(domain: str = "") -> dict:
    """List the vendored reference data-product spec catalog (optionally one
    domain) + the corpus version — the specs a feasibility run evaluates."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.feasibility import list_specs as _h
    with Session(engine) as session:
        try:
            return _h(domain=domain or None, session=session)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def evaluate_feasibility(estate_id: int, owner_email: str,
                         scan_id: int | None = None, domain: str = "",
                         scope_to_schemas: bool = True,
                         schema_relevance_floor: float | None = None,
                         schema_shortlist_threshold: float | None = None,
                         max_schemas_per_spec: int | None = None) -> dict:
    """Queue a top-down feasibility evaluation of the reference specs. By default
    it spans the latest completed/partial scan of EVERY enabled source (all
    catalogs in the estate); pass scan_id to pin a single scan (legacy). The leased
    worker runs it; poll get_feasibility_run for the stoplight results.

    Tiered matching (default on): each spec is first matched to the estate's
    relevant SCHEMAS (functional areas), then columns are assigned only within
    those — so an employee-data spec can't collect false-positive columns from an
    unrelated sales schema. The shortlist is score-driven: schema_relevance_floor
    (0–100, default 45) is the HARD relevance cutoff; schema_shortlist_threshold
    (0–100, default 70) is the confident band — schemas at/above it are shortlisted
    up to max_schemas_per_spec (default 5), and if none reach it the single best
    above-floor schema is still evaluated (low confidence). scope_to_schemas=False
    restores the whole-estate behavior."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.feasibility import EvaluateRequest, evaluate as _h
    with Session(engine) as session:
        try:
            return _h(EvaluateRequest(
                          estate_id=estate_id, scan_id=scan_id, domain=domain or None,
                          scope_to_schemas=scope_to_schemas,
                          schema_relevance_floor=schema_relevance_floor,
                          schema_shortlist_threshold=schema_shortlist_threshold,
                          max_schemas_per_spec=max_schemas_per_spec),
                      session=session, user=_po_owner(owner_email), _role=None)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def get_feasibility_run(run_id: int) -> dict:
    """Get a feasibility run: tier counts, per-spec stoplight scores (ready |
    adaptable | assemblable | absent), evaluation states, coverage, and the
    stamped corpus/evaluator/skill/embedding versions."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.feasibility import get_run as _h
    with Session(engine) as session:
        try:
            return _h(run_id, session=session)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def create_product_from_spec(run_id: int, spec_id: str, owner_email: str) -> dict:
    """Act on a feasibility verdict: adopt (ready), seed a consumer wizard
    (adaptable), or compose+stage a modernization portfolio for intake review
    (assemblable). Returns the tier-appropriate action descriptor / intake link."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.feasibility import act as _h
    with Session(engine) as session:
        try:
            return _h(run_id, spec_id, session=session, user=_po_owner(owner_email), _role=None)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def save_feasibility_candidate(score_id: int, owner_email: str, notes: str = "") -> dict:
    """Save a feasibility score as a candidate to work on later — lighter than
    immediately committing to the intake/wizard flow. De-duped by score + owner:
    re-saving the same score is idempotent (updates the notes). Returns the saved
    candidate row. Visible in My Products as the 'Candidate pipeline' section."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.feasibility import save_candidate as _h
    with Session(engine) as session:
        try:
            return _h(score_id, {"notes": notes}, session=session,
                      user=_po_owner(owner_email), _role=None)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def list_feasibility_candidates(owner_email: str) -> dict:
    """List saved (not dismissed) feasibility candidates for this PO — the
    pipeline of ideas flagged during feasibility evaluation. Returns candidate
    rows with tier, coverage, confidence, rationale, and PO notes."""
    if (blocked := _serving_guard()) is not None:
        return blocked
    from fastapi import HTTPException
    from .routers.feasibility import list_candidates as _h
    with Session(engine) as session:
        try:
            return _h(owner_email=owner_email, session=session, user=_po_owner(owner_email))
        except HTTPException as e:
            return {"error": e.detail}


# ── Blueprint Library (data-product spec templates) ──────────────────────────
# The single source of truth for data-product spec templates. Every write
# delegates to the shared router/store helpers with an explicit owner_email
# (the MCP has no FastAPI Request to derive current_user from).

@po_mcp.tool()
def list_templates(owner_email: str, domain: str = "", status: str = "",
                   origin: str = "", product_kind: str = "", q: str = "") -> dict:
    """Browse Blueprint-Library spec templates visible to this PO (published ∪
    your own drafts). Optional filters: domain / status (draft|published) /
    origin (seed|clone|import|authored) / product_kind / free-text q."""
    from .routers.templates import _summary, _visible_rows
    ql = (q or "").strip().lower()
    with Session(engine) as session:
        out = []
        for r in _visible_rows(session, owner_email):
            if status and (r.get("status") or "draft") != status:
                continue
            if domain and (r.get("domain") or "").lower() != domain.lower():
                continue
            if origin and (r.get("origin") or "") != origin:
                continue
            if product_kind and (r.get("product_kind") or "") != product_kind:
                continue
            if ql:
                hay = f"{r.get('name') or ''} {r.get('description') or ''} {r.get('domain') or ''}".lower()
                if ql not in hay:
                    continue
            out.append(_summary(r))
        return {"templates": out, "count": len(out)}


@po_mcp.tool()
def get_template(template_id: str) -> dict:
    """Fetch a Blueprint-Library template's full ODCS spec (+ template head props)."""
    from . import template_store
    with Session(engine) as session:
        spec = template_store._read_template_from_graph(template_id, session)
        if not spec or not spec.get("isTemplate"):
            return {"error": f"Template not found: {template_id}"}
        return {"spec": spec, "contract_id": template_id}


@po_mcp.tool()
def get_template_completeness(template_id: str) -> dict:
    """Design-completeness ('what's missing') for a template — missing
    descriptions / primary+grain keys / purpose / required-flag decisions + an
    OSI-style band. Operational fields are informational (set at instantiation)."""
    from . import template_store
    from .routers.templates import _template_completeness
    with Session(engine) as session:
        spec = template_store._read_template_from_graph(template_id, session)
        if not spec or not spec.get("isTemplate"):
            return {"error": f"Template not found: {template_id}"}
        return _template_completeness(spec)


@po_mcp.tool()
def create_template(spec: dict, owner_email: str) -> dict:
    """Author a new template (ODCS spec dict) → a draft you own."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from .routers.templates import canonicalize_template, persist_new_template
    with Session(engine) as session:
        return persist_new_template(session, owner_email,
                                    canonicalize_template(spec), origin="authored")


@po_mcp.tool()
def import_template(content: str, owner_email: str, source: str = "") -> dict:
    """Import an ODCS spec (YAML or JSON text) as a new draft template (ODCS in)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.ingest_products import _parse_raw
    from .routers.templates import canonicalize_template, persist_new_template
    with Session(engine) as session:
        try:
            raw = _parse_raw(content, source or None)
        except HTTPException as e:
            return {"error": e.detail}
        return persist_new_template(session, owner_email,
                                    canonicalize_template(raw), origin="import")


@po_mcp.tool()
def clone_template(template_id: str, owner_email: str) -> dict:
    """Clone any template (incl. a read-only seed) into a fresh editable draft you
    own, with lineage back to the source."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from . import template_store
    from .routers.templates import clone_canon_from, persist_new_template
    with Session(engine) as session:
        src = template_store._read_template_from_graph(template_id, session)
        if not src or not src.get("isTemplate"):
            return {"error": f"Template not found: {template_id}"}
        return persist_new_template(session, owner_email, clone_canon_from(src),
                                    origin="clone", cloned_from=template_id)


@po_mcp.tool()
def save_template(template_id: str, spec: dict, owner_email: str) -> dict:
    """Save edits to a template (merge-by-default; rejects a read-only seed).
    Returns save_mode + new_version."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.templates import TemplateSpecBody, update_template
    with Session(engine) as session:
        try:
            return update_template(template_id, TemplateSpecBody(spec=spec), session)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def publish_template(template_id: str, owner_email: str) -> dict:
    """Publish a draft template (author-gated): flips draft→published. Feasibility
    reads the published Blueprint Library live from the graph, so the template
    becomes available in Feasibility immediately (no corpus regeneration)."""
    if (_ro := _deny_write()) is not None:
        return _ro
    from fastapi import HTTPException
    from .routers.templates import publish_template as _h
    with Session(engine) as session:
        try:
            return _h(template_id, session)
        except HTTPException as e:
            return {"error": e.detail}


@po_mcp.tool()
def export_template(template_id: str) -> dict:
    """Export a template as ODCS YAML (ODCS out) → {filename, yaml, spec}."""
    from fastapi import HTTPException
    from .routers.templates import export_template as _h
    with Session(engine) as session:
        try:
            return _h(template_id, session)
        except HTTPException as e:
            return {"error": e.detail}
