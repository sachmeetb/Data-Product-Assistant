from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select

from . import config
from . import telemetry
from .auth import auth_enabled, decode_token
from .database import create_db_and_tables, engine
from .graph_ops import ensure_contract_versioning
from . import mcp_server as mcp_mod
from .mcp_server import mcp as mcp_server
from . import po_mcp_server as po_mcp_mod
from .po_mcp_server import po_mcp
from .models import Project
from .routers import agent_messages, archetypes, artifacts, assembly, auth, bootstrap, chat, cli, code_migration, connections, dashboard, dataset_transform, dev, domain_catalogs, domains, dq, edits, engineer_kit, estates, export, feasibility, filter_intent, graph_integrity, ingest_products, intake, marketplace, materialization, migration, odcs, okf, osi, platforms, po_kit, preflight, product_chat, product_requests, projects, qa, reviews, scoring, semantic, serving, serving_strategy, settings, skills, source_edits, source_offline, stage_executions, stages, summary, templates, test_runs, transform_intent, transform_placement_advisor, usage, websocket, discovery


def _backfill_contract_versioning_all_projects() -> None:
    """Run ensure_contract_versioning() once per project at startup.

    Best-effort: individual Neo4j connection failures are swallowed inside
    ensure_contract_versioning(), so one unreachable project doesn't block
    boot. Writes to the per-process cache so subsequent save/read calls
    skip the wire round-trip.
    """
    try:
        with Session(engine) as session:
            projects_ = session.exec(select(Project)).all()
    except Exception:
        return
    for project in projects_:
        ensure_contract_versioning(project)


async def _seed_template_library_if_empty() -> None:
    """Bootstrap the Blueprint Library on a fresh instance (seed templates +
    regenerate the feasibility corpus). Idempotent + cheap once seeded; runs off
    the event loop and never raises into startup."""
    import asyncio

    def _run():
        from . import template_corpus
        with Session(engine) as session:
            return template_corpus.seed_if_empty(session)

    try:
        result = await asyncio.to_thread(_run)
        if result.get("seeded"):
            seeded = (result.get("seed") or {}).get("count", 0)
            corpus = (result.get("corpus") or {}).get("corpus_version", "?")
            print(f"[bootstrap] Blueprint Library seeded: {seeded} templates; "
                  f"feasibility corpus_version {corpus}")
    except Exception as e:  # noqa: BLE001 — never block startup on a graph hiccup
        print(f"[bootstrap] Blueprint Library seed skipped (non-fatal): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    _backfill_contract_versioning_all_projects()
    # Blueprint Library: seed the template Library + regenerate the feasibility
    # corpus on a fresh instance (idempotent — a no-op once seeded). Off the
    # event loop (Neo4j + file writes) and non-fatal so a graph hiccup can't
    # block startup.
    await _seed_template_library_if_empty()
    # Drive the MCP server's Streamable-HTTP session manager for the app's
    # lifetime. It's mounted at /mcp below; mounted sub-apps don't get their
    # own lifespan run automatically, so we run its session manager here.
    from . import intake_worker
    from . import estate_worker
    async with mcp_server.session_manager.run():
        async with po_mcp.session_manager.run():
            worker_task = intake_worker.start_worker()
            # Connected-Estate: one leased worker drains both the estate-scan and
            # feasibility-run queues (see estate_worker.py).
            estate_worker_task = estate_worker.start_worker()
            try:
                yield
            finally:
                await intake_worker.stop_worker(worker_task)
                await estate_worker.stop_worker(estate_worker_task)
                # Flush any buffered spans/metrics on shutdown (no-op if off).
                telemetry.shutdown_telemetry()


app = FastAPI(title="Data Workbench", version="0.2.0", lifespan=lifespan)


# ── Request-level auth + read-only middleware ───────────────────────────────
# Registered alongside _normalize_mcp_slash (below). Mirrors the request_guard
# live-read env-flag pattern and the MCP bearer model. The /mcp + /po-mcp mounts
# keep their OWN bearer middleware (they're mounted sub-apps, skipped here).

# No token required for these /api paths (login must reach the server; config
# must be readable before a token exists; health is the liveness probe).
# /api/intake/submit is the external-machine entry point: it authenticates with
# a scoped machine credential (WB_INTAKE_TOKENS) inside the route, NOT a user
# JWT, so it must skip the JWT middleware. (Read-only mode still blocks it — it's
# a mutating POST — which is the intended behaviour on a read-only box.)
_AUTH_ALLOWLIST = {"/api/health", "/api/auth/login", "/api/auth/config", "/api/intake/submit"}
# Read-only mode blocks every mutation EXCEPT login (so a user can still sign
# in on a read-only demo box). Method is the signal — a strict, total gate.
_READONLY_ALLOWLIST = {"/api/auth/login"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def _auth_and_readonly(request, call_next):
    from fastapi.responses import JSONResponse

    path = request.scope.get("path", "") or ""
    method = request.method.upper()

    # Only guard the REST surface. /mcp + /po-mcp have their own bearer auth;
    # everything else (static, docs) is untouched.
    is_api = path.startswith("/api/")

    # Read-only: block any mutating REST call up front (except login).
    if is_api and config.read_only() and method not in _SAFE_METHODS and path not in _READONLY_ALLOWLIST:
        return JSONResponse(
            status_code=403,
            content={
                "read_only": True,
                "message": "This is a read-only Data Workbench instance — mutations are disabled.",
            },
        )

    # Authentication: verify the Bearer JWT and stash the user on request.state.
    # OPTIONS is skipped — CORS preflight carries no Authorization header.
    if is_api and method != "OPTIONS" and auth_enabled() and path not in _AUTH_ALLOWLIST:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        user = decode_token(token) if token else None
        if user is None:
            return JSONResponse(
                status_code=401,
                content={"message": "Authentication required.", "auth_enabled": True},
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.user = user

    return await call_next(request)

# The MCP app is mounted at "/mcp", so a request to the bare "/mcp" (no trailing
# slash) would hit Starlette's redirect_slashes and 307 to "/mcp/". Static-bearer
# MCP clients (e.g. OpenAI Codex) commonly drop the Authorization header / POST
# body across that redirect → a spurious 401 → "Auth: Unsupported" with no tools.
# Rewrite "/mcp" → "/mcp/" INTERNALLY (same request, headers/body preserved) so
# the endpoint serves directly. Must run before routing → registered as middleware.
@app.middleware("http")
async def _normalize_mcp_slash(request, call_next):
    if request.scope.get("path") == "/mcp":
        request.scope["path"] = "/mcp/"
    elif request.scope.get("path") == "/po-mcp":
        request.scope["path"] = "/po-mcp/"
    return await call_next(request)


# CORS is registered LAST so it's the OUTERMOST middleware — otherwise the
# JSONResponse a 401/403 returned by the auth/read-only middleware above would
# skip CORS and the browser couldn't read the error. allow_origins is env-driven
# (config.cors_origins) so a hosted frontend origin works without a code change.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Mount the MCP control-plane server (read-only slice) so a data engineer's own
# Claude Code can connect to the workbench at <host>:8000/mcp — the same surface
# the Docker container will expose to "remote" engineers. See mcp_server.py;
# the trust boundary (scoping, read-only) is enforced server-side there.
# asgi_app() wraps the MCP app in plain bearer-token auth (no OAuth advertisement).
app.mount("/mcp", mcp_mod.asgi_app())
app.mount("/po-mcp", po_mcp_mod.asgi_app())


# Backend OpenTelemetry (opt-in; endpoint-gated). Initialised HERE — after the
# routers, CORS, and mounts are configured — so the OTel ASGI instrumentation
# wraps the fully-assembled app as the OUTERMOST layer (it observes the raw
# request/response; CORS still decorates the inner auth/read-only error
# responses, preserving the guarantee documented above). No-op when
# OTEL_EXPORTER_OTLP_ENDPOINT is unset or the opentelemetry packages are absent.
telemetry.init_telemetry(app)

app.include_router(auth.router)
app.include_router(agent_messages.router)
app.include_router(engineer_kit.router)
app.include_router(po_kit.router)
app.include_router(bootstrap.router)
app.include_router(archetypes.router)
app.include_router(odcs.router)
app.include_router(odcs.project_router)
app.include_router(templates.router)
app.include_router(dashboard.router)
app.include_router(domains.router)
app.include_router(graph_integrity.router)
app.include_router(marketplace.router)
app.include_router(marketplace.project_qa_router)
app.include_router(okf.router)
app.include_router(projects.router)
app.include_router(projects.catalog_router)
app.include_router(stages.router)
app.include_router(artifacts.router)
app.include_router(chat.router)
app.include_router(reviews.router)
app.include_router(scoring.router)
app.include_router(semantic.router)
app.include_router(serving.router)
app.include_router(serving.reflection_router)
app.include_router(materialization.router)
app.include_router(migration.router)
app.include_router(code_migration.router)
app.include_router(source_offline.router)
app.include_router(export.router)
app.include_router(serving_strategy.router)
app.include_router(serving_strategy.unscoped_router)
app.include_router(transform_placement_advisor.router)
app.include_router(transform_placement_advisor.unscoped_router)
app.include_router(settings.router)
app.include_router(stage_executions.router)
app.include_router(summary.router)
app.include_router(usage.router)
app.include_router(test_runs.router)
app.include_router(dq.router)
app.include_router(product_requests.router)
app.include_router(ingest_products.router)
app.include_router(intake.router)
app.include_router(domain_catalogs.router)
app.include_router(skills.router)
app.include_router(product_chat.router)
app.include_router(edits.router)
app.include_router(source_edits.router)
app.include_router(dataset_transform.router)
app.include_router(filter_intent.router)
app.include_router(transform_intent.router)
app.include_router(osi.router)
app.include_router(qa.router)
app.include_router(preflight.router)
app.include_router(platforms.router)
app.include_router(connections.router)
app.include_router(connections.project_binding_router)
app.include_router(cli.router)
app.include_router(dev.router)
# ── Pulse-backed estate discovery (bottom-up assessment hand-off) ──
app.include_router(discovery.router)
# ── Connected Estate + top-down feasibility (live DW platform scans) ──
app.include_router(estates.router)
app.include_router(feasibility.router)
app.include_router(assembly.router)
app.include_router(websocket.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
