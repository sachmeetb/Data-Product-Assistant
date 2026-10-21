import enum
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


# Neo4j connection defaults are env-overridable so a containerized backend can
# reach the graph DB by its compose service name (WB_NEO4J_HOST=neo4j) instead
# of the hardcoded "localhost" that only works on a dev box. Falls back to the
# local-dev values when the env vars are unset.
def _neo4j_host_default() -> str:
    return os.environ.get("WB_NEO4J_HOST", "localhost")


def _neo4j_port_default() -> int:
    try:
        return int(os.environ.get("WB_NEO4J_PORT") or "7687")
    except (TypeError, ValueError):
        return 7687


def _neo4j_user_default() -> str:
    return os.environ.get("WB_NEO4J_USER", "neo4j")


def _neo4j_password_default() -> str:
    return os.environ.get("WB_NEO4J_PASSWORD", "your_password")


def _neo4j_database_default() -> str:
    return os.environ.get("WB_NEO4J_DATABASE", "neo4j")


class StageStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    awaiting_review = "awaiting_review"
    complete = "complete"
    failed = "failed"


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_code: str = Field(unique=True, index=True)
    name: str
    # A project's SOURCE is bound via SourceBinding → PlatformConnection (the
    # single connection contract) — see routers/connections.py + pg_resolver.py.
    # There is no per-project DSN field.
    neo4j_host: str = Field(default_factory=_neo4j_host_default)
    neo4j_port: int = Field(default_factory=_neo4j_port_default)
    neo4j_user: str = Field(default_factory=_neo4j_user_default)
    # Default matches the local-dev Neo4j password baked into the workbench
    # so a fresh SQLite reset boots straight into a usable connection
    # without the operator re-entering it. Production / container deployments
    # set WB_NEO4J_PASSWORD (or override per-project via AppSettings).
    neo4j_password: str = Field(default_factory=_neo4j_password_default)
    neo4j_database: str = Field(default_factory=_neo4j_database_default)
    archetype: str = Field(default="dd")
    domain: Optional[str] = Field(default=None)
    workflow_json: Optional[str] = Field(default=None)
    multi_workflow: bool = Field(default=False)
    current_stage: int = Field(default=1)
    role_assignments: str = Field(default="{}")
    published_at: Optional[datetime] = Field(default=None)
    discovery_complete_at: Optional[datetime] = Field(default=None)
    # Free-form prose the PO captured in the SA wizard ("describe the source").
    # Surfaced as a banner on the engineer's project page so they can re-read
    # the intent without bouncing back to the request notes.
    product_idea: Optional[str] = Field(default=None)
    # Email of the PO who created this project. Source-aligned synthesis
    # uses this to populate the synthesized ODCS spec's owners[] (the SA
    # wizard doesn't author the spec directly the way the consumer wizard
    # does, so without this the synthesized contract had zero owners and
    # the PO's My Products list returned empty).
    owner_email: Optional[str] = Field(default=None)
    # Display name of the PO. Flows into :DataContractOwner.name so the
    # marketplace owner chip renders "Niel Eyde" instead of the email
    # prefix that sa_pipeline used to fall back to.
    owner_name: Optional[str] = Field(default=None)
    # Forward marker for a project scaffolded from inbound intake. The source of
    # truth is :class:`IntakeSpawn.child_project_id`; this is a denormalized
    # convenience for API/UI (and the hook a future alignment-reflection loop
    # hangs off). On read the origin endpoint backfills/repairs it from
    # IntakeSpawn if they disagree (partial-scaffold safety).
    parent_intake_submission_id: Optional[int] = Field(default=None, index=True)
    # Workflow *intent* for a migration project: whether live source connectivity
    # is expected (`live`) or the assessment schema is all we have for now
    # (`schema_only`). This is intent only — execution eligibility is computed
    # separately (`migration_orchestrator.migration_execution_readiness`), which
    # additionally requires a real source + target + generated package. Deferred,
    # not permanent: a project flips schema_only → live once connectivity arrives.
    data_connectivity_mode: str = Field(default="live")  # live | schema_only
    # Pre-selected Data Discovery scope carried from a Connected-Estate assembly
    # scaffold (Bridge B): a JSON list of schema-qualified "schema.table" strings.
    # When set, the discovery stage's config-options seeds these as pre-checked
    # defaults (intersected with the live options, so a renamed/dropped table
    # can't produce a phantom pre-check). Nullable — absent for hand-created and
    # external-tool-scaffolded projects (behavior unchanged). This does NOT seed
    # the operational graph; live discovery stays authoritative.
    discovery_scope_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AppUser(SQLModel, table=True):
    """A login account for the Data Workbench.

    Two server-enforced account roles — ``owner`` (Data Product Owner) and
    ``engineer`` — designed to plug into an SSO/OIDC provider later (the
    :class:`~workbench.backend.auth.AuthProvider` seam). Passwords are stored as
    a self-describing pbkdf2 hash (see ``auth.hash_password``); never plaintext.
    Seeded out-of-band via ``scripts/seed_users.py``. New table → auto-created
    by ``create_db_and_tables()``; no ``_migrate()`` ALTER needed.
    """

    email: str = Field(primary_key=True)
    name: str = ""
    password_hash: str = ""
    role: str = Field(default="engineer")  # "owner" | "engineer"
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AppSettings(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    neo4j_host: str = Field(default_factory=_neo4j_host_default)
    neo4j_port: int = Field(default_factory=_neo4j_port_default)
    neo4j_user: str = Field(default_factory=_neo4j_user_default)
    # Default matches the local-dev Neo4j password so a fresh SQLite reset
    # boots straight into a usable connection. New projects inherit from
    # AppSettings, so this default flows through to every Project row.
    # Container/prod: set WB_NEO4J_PASSWORD.
    neo4j_password: str = Field(default_factory=_neo4j_password_default)
    neo4j_database: str = Field(default_factory=_neo4j_database_default)
    neo4j_browser_url: str = Field(
        default_factory=lambda: os.environ.get(
            "WB_NEO4J_BROWSER_URL", "http://localhost:7474"
        )
    )
    # Git integration — push a data product's serving artifacts to a repo (one
    # repo per product). All optional; unset → feature disabled. See
    # ``git_provider.py`` + ``routers/serving.py`` (push-to-git + auto-push hook).
    git_provider: Optional[str] = Field(default=None)   # "gitea" | "github" | None
    git_base_url: Optional[str] = Field(default=None)   # backend→git, e.g. "http://gitea:3000"
    # Browser-facing origin used ONLY to render "View in Git" links in the UI —
    # the backend reaches Gitea over the compose net (git_base_url), but a laptop
    # browser can't resolve "gitea:3000". Unset → default to http://localhost:3101
    # for self-hosted Gitea (GitHub URLs are already public). See git_provider.browse_url.
    git_web_base_url: Optional[str] = Field(default=None)  # e.g. "http://localhost:3101"
    git_token: Optional[str] = Field(default=None)      # PAT (Gitea) / token (GitHub)
    git_org: Optional[str] = Field(default=None)        # org/user to own the repos
    git_auto_push: bool = Field(default=False)          # push after a successful deploy


class Workflow(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    workflow_id: str = Field(index=True)
    name: str
    description: Optional[str] = None
    workflow_json: str = Field(default="[]")
    order: int = Field(default=1)
    repeatable: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StageRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    workflow_id: Optional[str] = Field(default=None, index=True)
    stage_number: int
    stage_name: str
    status: StageStatus = StageStatus.pending
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StageExecution(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    stage_run_id: int = Field(foreign_key="stagerun.id", index=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    workflow_id: Optional[str] = Field(default=None, index=True)
    stage_number: int = Field(index=True)
    run_id: str = Field(index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    status: str = "running"
    cost_usd: Optional[float] = None
    session_id: Optional[str] = None
    error_message: Optional[str] = None
    event_count: int = 0
    tool_counts_json: str = "{}"
    log_json: str = "[]"
    truncated: bool = False


class LlmUsageEvent(SQLModel, table=True):
    """Append-only ledger of LLM token usage — one row per SDK call across the
    whole system. Single source of truth for global / per-data-product /
    per-Semantic-Q&A token rollups. Writes are best-effort (accounting never
    breaks an LLM feature)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    # Which subsystem made the call: 'stage' | 'engineer_chat' | 'product_chat' |
    # 'semantic_qa' | 'osi_advisor' | 'qa_analyzer' | 'qa_executor' |
    # 'schema_advisor' | 'filter_intent' | 'serving_strategy' |
    # 'rationale_summarizer' | 'deployment_reflection' | 'semantic_recommender' |
    # 'ingest_classifier' | ...
    source: str = Field(index=True)
    # Attribution (all optional — cross-project sources like the recommender tag
    # only `domain`; stateless Q&A tags `domain` + optional `contract_id`).
    project_code: Optional[str] = Field(default=None, index=True)
    contract_id: Optional[str] = Field(default=None, index=True)
    domain: Optional[str] = Field(default=None, index=True)
    run_id: Optional[str] = Field(default=None, index=True)
    retrieval_mode: Optional[str] = None  # 'full' | 'concept_guided' (semantic_qa only)
    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Optional[float] = None


class DQTestRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    stage_run_id: Optional[int] = Field(default=None, foreign_key="stagerun.id", index=True)
    framework: str
    batch_id: str = Field(index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    total_expectations: int = 0
    successful: int = 0
    unsuccessful: int = 0
    tables_tested: int = 0
    results_path: str = ""
    status: str = "complete"


class ProductRequestKind(str, enum.Enum):
    new = "new"                                # greenfield product authored in the wizard
    edit = "edit"                              # change to a previously published product
    ingest = "ingest"                          # intake of an undocumented existing product
    source_rediscovery = "source-rediscovery"  # PO asks engineer to re-run dpe-sa discovery against a deployed source product
    consumer_pushback = "consumer-pushback"    # consumer flags an upstream change as breaking; source PO receives it
    source_candidates_needed = "source-candidates-needed"  # engineer requests PO to identify source-aligned data products for a consumer-aligned product


class ProductRequestStatus(str, enum.Enum):
    submitted = "submitted"
    accepted = "accepted"
    complete = "complete"
    rejected = "rejected"


class ProductRequest(SQLModel, table=True):
    """Handoff queue entry between the Product Workbench and Engineering.

    A row is created when a Data Product Owner submits a new or edited
    product spec, or registers an existing product for ingestion. The
    Engineering shell's Incoming queue reads status='submitted' rows and
    moves them through accepted → complete. The companion
    :DataContract.lifecycleState mirrors this on the graph side.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    contract_id: str = Field(index=True)
    contract_versioned_id: Optional[str] = None
    kind: ProductRequestKind
    status: ProductRequestStatus = Field(default=ProductRequestStatus.submitted, index=True)
    submitted_by: str = ""
    submitted_at: datetime = Field(default_factory=datetime.utcnow)
    accepted_by: Optional[str] = None
    accepted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    engineer_assigned: Optional[str] = None
    notes: Optional[str] = None
    related_workflow_id: Optional[str] = None
    # When a source ProductRequest is spawned from a consumer-aligned ingest's
    # "Create now" gap resolution, this links back to the parent IngestDraft so
    # the resume page can render "X of Y source products ready" progress and
    # the engineer can see the originating context.
    parent_ingest_draft_id: Optional[int] = Field(default=None, index=True)
    # When a project/request is spawned by the inbound-intake scaffold saga, this
    # links back to the originating IntakeSubmission (distinct from the ingest-draft
    # link above). Do NOT overload parent_ingest_draft_id — it is typed to IngestDraft.
    parent_intake_submission_id: Optional[int] = Field(default=None, index=True)
    # When the engineer escalates a `source_candidates_needed` request from a
    # column-level surface (MappingReviewPanel, UnmappedColumnsPanel), these
    # carry the specific gap context. The empty-picker variant of the same
    # kind sends just `notes` and leaves these null.
    gap_column_uri: Optional[str] = Field(default=None)
    gap_reason: Optional[str] = Field(default=None)


class MarketplaceGap(SQLModel, table=True):
    """A reported gap from the Semantic Q&A chat: a question the semantic layer
    could not answer. Marketplace-level (spans products) — unlike ProductRequest
    it isn't tied to a project. One unified log; `audience` tags who should act
    (triage by default). Status flows open → triaged → resolved/dismissed.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    domain: str = Field(index=True)
    contract_id: Optional[str] = None
    product_uri: Optional[str] = None
    question: str = ""
    refused_reason: Optional[str] = None
    analysis: Optional[str] = None              # the chat's `message`/explanation
    concepts_used_json: Optional[str] = None
    retrieval_meta_json: Optional[str] = None
    status: str = Field(default="open", index=True)   # open|triaged|resolved|dismissed
    audience: str = Field(default="triage")           # triage|po|engineer
    suggested_action: Optional[str] = None
    created_by: str = "anonymous-marketplace-chat"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    notes: Optional[str] = None


class IngestDraft(SQLModel, table=True):
    """Persisted in-flight ODCS ingest state.

    A draft is created when a PO uploads a spec on /product/ingest and is
    auto-saved on every step transition. For consumer-aligned ingests with
    unresolved source-product dependencies, the draft is what the PO returns
    to after authoring/importing the missing source products. The draft
    holds:

      • the parsed canonical spec (JSON)
      • the classifier's output (kind/confidence/rationale/signals/inferred_deps)
      • the PO's archetype choice (which may override the classifier)
      • the per-slot input selections (matched dprod_uri OR gap with spawned
        request linkage)

    On commit, the draft transitions to ``status='committed'`` with
    ``committed_project_id`` stamped — abandoned drafts can be cleaned up
    via a future TTL sweep.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_email: str = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    source_filename: Optional[str] = None
    parsed_spec_json: str = Field(default="{}")
    classification_json: Optional[str] = Field(default=None)
    archetype_choice: Optional[str] = Field(default=None)
    input_selections_json: Optional[str] = Field(default=None)
    status: str = Field(default="in_progress", index=True)
    committed_project_id: Optional[int] = Field(default=None)


class ProductChatSession(SQLModel, table=True):
    """Persisted Product Workbench chat sessions.

    Scoped by ``owner_email`` (Engineering chat scopes by ``project_id``;
    Product chat needs to work before a project exists, e.g. during the
    early steps of the New Product wizard, so we scope by user instead).
    The optional ``project_id`` is set when the session is started inside
    a wizard run that has provisioned a project, enabling per-project
    filtering later.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_email: str = Field(index=True)
    project_id: Optional[int] = Field(default=None, index=True)
    title: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ProductChatMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="productchatsession.id", index=True)
    role: str
    content: str = ""
    tool_events_json: str = "[]"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ChatSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    title: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ChatMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="chatsession.id", index=True)
    role: str
    content: str = ""
    tool_events_json: str = "[]"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SemanticChatSession(SQLModel, table=True):
    """Persisted Conversational Semantic Q&A sessions.

    The marketplace Semantic Q&A 'Conversational' mode (an orchestrator that
    decides per turn whether to query the semantic layer / clarify / reject /
    chat) is server-side stateful — unlike the stateless ``/chat`` POST whose
    history lives in browser localStorage. Scoped by ``domain`` (+ optional
    ``contract_id``). ``session_context_json`` is the deliberate rolling memory
    (focus entities, active filters, last query, established facts) the router
    reads instead of dumping the whole transcript. A new row = cleared memory.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    domain: str = Field(index=True)
    contract_id: Optional[str] = Field(default=None, index=True)
    # The retrieval strategy the agent uses WHEN it queries ('full' | 'concept_guided').
    retrieval_submode: str = "concept_guided"
    title: str = ""
    # Compact structured memory carried across turns (JSON object).
    session_context_json: str = "{}"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SemanticChatMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="semanticchatsession.id", index=True)
    role: str  # "user" | "assistant"
    content: str = ""
    # Assistant turns carry the full turn result (action, sql, rows, trace,
    # routing grounding, token_usage) so reloading a session rehydrates the UI.
    payload_json: str = "{}"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MaterializationTarget(SQLModel, table=True):
    """Per-product target connection for dbt materialization.

    Keyed by contract_id ({project_code}-contract) so each data product can
    materialize into its own database, distinct from the SOURCE connection.
    Empty / no row → materialization falls back to the source connection (today's
    same-instance behavior).

    The preferred target is a registered PlatformConnection via
    ``target_connection_id`` (the single connection contract). ``connection_json``
    holds the legacy inline target config: a transient Postgres DSN under a ``dsn``
    key, or non-Postgres public config (Snowflake account, Databricks host/
    http_path, MySQL host, etc.). Credentials are NEVER stored inline for
    non-Postgres targets — they resolve at runtime via a ``secret_ref`` env-var
    name. ``load_strategy`` is reserved for Phase B ('fdw'/'load'); 'direct' today.
    """
    contract_id: str = Field(primary_key=True)
    platform: str = ""
    load_strategy: str = "direct"
    connection_json: str = Field(default="{}")
    # Optional pointer to a registered PlatformConnection (role "target").
    # When set, it is the serving target for ALL modes (virtual view, dbt
    # materialized, lakehouse export) — the product-level serving target.
    # Unset → fall back to the :CONSUMES-borrowed source (dpe-cf) or the
    # SourceBinding (dpe-sa): today's same-instance behaviour.
    target_connection_id: Optional[int] = Field(default=None)
    # Deploy-target namespace ("catalog.schema") for 3-level platforms
    # (Databricks/Snowflake) whose source catalog is often read-only. Keyed here
    # by contract_id so it works for BOTH archetypes — dpe-cf products have no
    # SourceBinding (they borrow via :CONSUMES), so this is the universal home.
    # Empty → serving falls back to "public" (Postgres). Set by Configure Serving.
    view_target_namespace: str = ""
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ProjectArtifactStoreBinding(SQLModel, table=True):
    """Where a project PUBLISHES generated data artifacts (Parquet + manifests)
    to an object store (ADR-14, docs/architecture/object-store.md).

    Separates the BINDING (bucket + key prefix + publish policy) from the
    CONNECTION (endpoint + credentials). The connection is a registered
    ``PlatformConnection`` (platform_type 's3' today; 'gcs'/'azure_adls' later)
    resolved via ``connection_id`` — the same single-connection contract
    ``MaterializationTarget`` uses. One binding per project (project_id is the PK).
    ``bucket`` overrides the connection's default bucket; ``project_prefix`` is the
    key namespace inside the bucket (defaults to "<project_code>/" when blank).
    ``auto_publish`` is reserved (v2 — publish after a successful deploy_lakehouse,
    like git ``_maybe_auto_push``). New table → auto-created by ``create_all``;
    no ``_migrate()`` ALTER needed.
    """
    project_id: int = Field(primary_key=True)
    # FK to a registered PlatformConnection whose platform is an object store.
    connection_id: int
    # Bucket to publish into (overrides the connection's extra_config.bucket).
    bucket: str = ""
    # Key namespace within the bucket; blank → "<project_code>/" at publish time.
    project_prefix: str = ""
    # Reserved: auto-publish after a successful lakehouse deploy (v2).
    auto_publish: bool = False
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ArtifactPublishRun(SQLModel, table=True):
    """Audit row for one object-store publish (ADR-14). Analogous to how a git
    push is recorded on the :ServingDefinition — here it's a SQLite row per push
    since object publishing has no graph node. ``run_id`` is the immutable
    run-prefix segment; ``status`` ∈ {succeeded, failed, empty}. New table →
    auto-created by ``create_all``; no ``_migrate()`` ALTER needed.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int
    connection_id: int
    run_id: str
    bucket: str = ""
    run_prefix: str = ""              # <project_prefix>/runs/<run_id>
    status: str = "succeeded"
    object_count: int = 0
    bytes_uploaded: int = 0
    error: str = ""
    created_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MigrationPlanRow(SQLModel, table=True):
    """Durable store for a data-migration (dmig) project's MigrationPlan.

    One row per dmig project, keyed by project_code. The authoritative lifecycle
    model is the Pydantic ``platform.migration_plan.MigrationPlan`` serialized into
    ``plan_json``; this table is just its durable home (mirrors
    ``MaterializationTarget``'s role for dbt targets). ``status`` and
    ``landing_strategy`` are denormalized copies for cheap listing/filtering.
    New table → auto-created by ``create_all``; no ``_migrate()`` ALTER needed.
    Disjoint from ``MaterializationTarget`` / any product-graph node — migration
    projects create NO :DataContract / :DProdDataProduct / marketplace nodes.
    """
    project_code: str = Field(primary_key=True)
    # Serialized MigrationPlan (model_dump_json). Source of truth for the plan.
    plan_json: str = Field(default="{}")
    # Denormalized MigrationStatus value for quick reads / listing.
    status: str = "draft"
    # "raw" (Phase 1 lift-and-shift) | "silver" | "gold" (future medallion).
    landing_strategy: str = "raw"
    # Registered PlatformConnection id chosen as the migration TARGET.
    target_connection_id: Optional[int] = Field(default=None)
    # Cached target platform_type (e.g. "snowflake") for prompt directives + UI.
    target_platform: str = ""
    write_disposition: str = "replace"
    # Target catalog for 3-level platforms (Databricks/Snowflake) — the writable
    # `catalog` half of the catalog.schema landing namespace, chosen in Configure
    # Migration. Empty for 2-level platforms (Postgres/MySQL). Flows into
    # migration.json + WB_TARGET_CATALOG for the runner.
    target_catalog: str = ""
    # Pipeline framework used to generate/run the migration (reference impl: dlt).
    framework: str = "dlt"
    # Git versioning (parity with modernization serving packages, but keyed on the
    # migration plan since a migration has no ODCS contract version). Tag = v{count}.
    git_repo_url: str = ""
    git_last_pushed_at: Optional[datetime] = Field(default=None)
    git_push_count: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ProjectPhysicalSchema(SQLModel, table=True):
    """Confirm Physical Schema (D3) — the reviewed structured schema for a
    schema-only migration project. One row per project, keyed by project_code
    (mirrors MigrationPlanRow). ``schema_json`` is a serialized
    ``physical_schema.PhysicalSchema`` (catalog/namespace/table + physical types
    + nullability/PK/FK); the deterministic seeder (Phase E) reads it. Pre-filled
    best-effort from the originating intake blueprint, then explicitly confirmed
    (``status`` draft → confirmed). New table → auto-created by ``create_all``;
    no ``_migrate()`` ALTER. Blueprint schema is untouched — this is a separate,
    reviewed artifact, not a mutation of the parse output.
    """

    project_code: str = Field(primary_key=True)
    # Serialized physical_schema.PhysicalSchema. Named *_physical_ to avoid
    # shadowing SQLModel/pydantic's deprecated ``schema_json`` classmethod.
    physical_schema_json: str = Field(default="{}")
    status: str = Field(default="draft")  # draft | confirmed
    source_intake_submission_id: Optional[int] = Field(default=None)
    confirmed_at: Optional[datetime] = Field(default=None)
    confirmed_by: Optional[str] = Field(default=None)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CodeMigrationPlanRow(SQLModel, table=True):
    """Durable store for a code-migration (cmig) project's lifecycle.

    One row per cmig project, keyed by project_code (mirrors MigrationPlanRow's
    role for dmig). Thin, project-keyed subsystem — no product/marketplace nodes;
    the only graph writes are a single :CodeModule node + a :USES_DATASET edge to
    a linked dmig project's :Dataset nodes (built on spec approval). New table →
    auto-created by ``create_all``; no ``_migrate()`` ALTER needed.

    The DATABASE is the canonical home of the reviewed spec (``spec_json`` +
    ``approved_spec_hash``); the on-disk codespec.json is a mirror. Blocking of
    forward-engineering is enforced by
    ``code_migration_orchestrator.require_forward_ready`` against the full
    dependency set (approved spec hash, source manifest, linked migration, corpus
    revisions) — NOT by the stage's has_review flag alone.
    """

    project_code: str = Field(primary_key=True)
    # Lifecycle state machine (code_migration_orchestrator.CmigStatus):
    # linked → imported → configured → awaiting_review → approved → converting →
    # converted | converted_with_actions | failed → packaged.
    status: str = Field(default="new")

    # ── canonical reviewed spec ───────────────────────────────────────────
    # Canonical (stable-ordered) codespec.json produced by reverse-engineering.
    spec_json: str = Field(default="{}")
    # SHA-256 of the canonical spec at the moment of approval. require_forward_ready
    # refuses forward-engineering unless the CURRENT spec hash equals this.
    approved_spec_hash: str = ""
    approved_by: str = ""
    approved_at: Optional[datetime] = Field(default=None)
    spec_revision: int = 0

    # ── link to the source data-migration project ─────────────────────────
    linked_dmig_project_code: str = ""
    # Hash of the linked migration's migration.json at link time — a later dmig
    # reconfigure is detected as drift and invalidates downstream artifacts.
    linked_migration_hash: str = ""
    # SHA-256 manifest of the imported (immutable) source/ files: {path: sha256}.
    source_manifest_json: str = Field(default="{}")

    # ── source / target platform (target LOCKED from the linked dmig) ──────
    source_platform: str = ""
    source_platform_version: str = ""
    target_platform: str = ""          # locked from linked dmig at link time
    target_runtime: str = ""
    output_language: str = "sql"
    framework: str = ""
    artifact_kind: str = "sql_script"  # sql_script | pyspark_job | notebook
    target_namespace: str = ""

    # ── SME corpus provenance (kept SEPARATE — different invalidation rules) ─
    # {platform, version, files: {path: sha256}} for each corpus used per run. A
    # source-corpus change invalidates reverse-engineering + approval + downstream;
    # a target-corpus change invalidates conversion/package only.
    source_corpus_provenance_json: str = Field(default="{}")
    target_corpus_provenance_json: str = Field(default="{}")

    # ── git versioning (parity with the serving/migration packages) ────────
    git_repo_url: str = ""
    git_last_pushed_at: Optional[datetime] = Field(default=None)
    git_push_count: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ── platform-neutral connection + execution models ───────────────────────────
#
# The SINGLE connection contract: every project — Postgres included — binds its
# source via SourceBinding → PlatformConnection. There is no per-project or
# per-target pg_connection DSN field.
#
# Lifecycle: a PlatformConnection row is created when an operator registers a
# data source/target (any platform).  It carries public configuration only —
# the password is NEVER stored here; it is referenced by `secret_ref` which
# is resolved by the secret backend (env var, Vault path, etc.).


class PlatformConnection(SQLModel, table=True):
    """A registered platform instance (Postgres, MySQL, Snowflake, …).

    Public configuration only — passwords are NEVER stored here.
    `secret_ref` is an opaque pointer resolved at runtime by the secret
    backend.  Treat this table as a DSN minus the credential.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    # Unique name chosen by the operator, e.g. "prod-mysql" or "analytics-dw".
    connection_name: str = Field(unique=True, index=True)
    platform_type: str = Field(index=True)   # "postgres" | "mysql" | "snowflake" …
    host: str = ""
    port: int = 5432
    database: str = ""
    username: str = ""
    # Opaque secret reference — never a plaintext password.
    # During Phase 1 dev/single-tenant mode this may be an env-var name
    # (e.g. "WB_MYSQL_PASSWORD"); Phase 2 targets Vault / AWS SSM paths.
    secret_ref: str = ""
    # JSON blob for platform-specific extras (warehouse, role, account_id …).
    extra_config_json: str = "{}"
    # JSON array of intended roles for this connection: "source" and/or "target".
    # "source"  → discovery, profiling, view deployment source
    # "target"  → dbt materialization write target
    # Default is ["source"] — set explicitly on create/update.
    connection_roles_json: str = '["source"]'
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ExecutionProfile(SQLModel, table=True):
    """Named execution sizing profile for a PlatformConnection.

    Used for DuckDB workload-envelope sizing (memory limit, thread count)
    and for future Spark / Databricks cluster profiles.  One connection
    can have multiple named profiles (e.g. "small", "large").  Phase 1
    only stores the record; the DuckDB executor reads it in Phase 3.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    connection_id: int = Field(foreign_key="platformconnection.id", index=True)
    profile_name: str = "default"
    # JSON blob; schema is platform-specific. For DuckDB:
    # {"memory_limit": "4GB", "threads": 4}.
    profile_config_json: str = "{}"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SourceBinding(SQLModel, table=True):
    """Binds a Project to its source via a PlatformConnection — for EVERY
    platform, Postgres included (the single connection contract; there is no
    per-project DSN field).

    Only ONE active SourceBinding per project_id; the UNIQUE constraint on
    (project_id) enforces this.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", unique=True, index=True)
    connection_id: int = Field(foreign_key="platformconnection.id", index=True)
    # Optional schema/catalog scoping within the connection.
    default_schema: str = ""
    # Optional SQL dialect override for serving stages. When set, overrides
    # the platform-map inference so an engineer can target a different dialect
    # than the source platform (e.g. source=MySQL, target=PostgreSQL for a
    # cross-platform virtual view). Set by ConnectionPickerDialog / configure_serving.
    target_dialect: str = ""
    # Optional target namespace for the DEPLOYED view, for platforms whose write
    # target can't be inferred from the (often read-only) source. On Databricks
    # this is a Unity Catalog "catalog.schema" (e.g. "workspace.default") — the
    # source catalog (e.g. "samples") is frequently read-only, so the view must
    # land elsewhere. Empty → the serving path falls back to "public" (Postgres).
    # Set by configure_serving; consumed by build_prompt (--view-schema) + deploy.
    view_target_namespace: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ── Inbound intake (external-tool → scaffolded projects) ─────────────────────
# A lightweight staging aggregate (modelled on IngestDraft): an external
# assessment tool POSTs a loose envelope, an isolated parser normalizes it into
# a strict confidence-graded blueprint (see intake_blueprint.py), a practitioner
# reviews/edits it, and only then does the scaffold saga create real projects.
# These tables hold NO graph nodes — scaffolding writes those via create_project.


class IntakeSubmission(SQLModel, table=True):
    """One inbound submission from an external system, staged for review.

    Idempotent on ``(source_system, external_ref)`` — a re-send updates the same
    row rather than duplicating. ``source_system`` is set from the authenticated
    machine principal, never trusted from the request body. The parse worker
    claims ``received`` rows via a compare-and-set lease (``lease_owner`` /
    ``lease_expires_at``) so a crash mid-parse is reclaimable after restart.
    """

    __table_args__ = (
        UniqueConstraint("source_system", "external_ref", name="uq_intake_source_ref"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_system: str = Field(index=True)
    external_ref: str = Field(index=True)
    scenario: str = Field(index=True)  # migration | modernization (V1: explicit)
    # received → parsing → proposed → reviewing → approved → scaffolding →
    # scaffolded  (+ parse_failed, rejected)
    status: str = Field(default="received", index=True)
    raw_payload_json: str = Field(default="{}")  # the envelope, verbatim
    blueprint_json: Optional[str] = Field(default=None)  # validated blueprint
    blueprint_revision: int = Field(default=0)  # optimistic-concurrency token
    parse_meta_json: Optional[str] = Field(default=None)  # model/usage/warnings
    lease_owner: Optional[str] = Field(default=None)
    lease_expires_at: Optional[datetime] = Field(default=None)
    parse_attempts: int = Field(default=0)
    # Migration-only intent captured at review (a toggle on IntakeReviewPage):
    # scaffold a live project or an offline schema-only one. `approve_and_scaffold`
    # propagates this to Project.data_connectivity_mode. Editable later on the
    # project. Ignored for modernization (dpe-cf is contract-first; dpe-sa
    # schema-only is a fast-follow, not V1).
    execution_mode: str = Field(default="live")  # live | schema_only
    # How the blueprint was produced. `parsed` (default) = the LLM parser turned an
    # unstructured envelope into a blueprint (the classic inbound path). `structured`
    # = a producer (Product Assembly) POSTed an already-structured, high-confidence
    # blueprint (native ODCS carried in the aggregate candidate's `odcs` slot); the
    # parse worker skips it and the review UI renders a light "confirm" instead of the
    # per-field confidence-grading review.
    ingestion_mode: str = Field(default="parsed")  # parsed | structured
    reviewed_by: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class IntakeEvent(SQLModel, table=True):
    """Append-only audit of an IntakeSubmission's state transitions."""

    id: Optional[int] = Field(default=None, primary_key=True)
    intake_submission_id: int = Field(foreign_key="intakesubmission.id", index=True)
    at: datetime = Field(default_factory=datetime.utcnow)
    actor: str = ""  # principal / user email
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    detail: Optional[str] = None


class IntakeSpawn(SQLModel, table=True):
    """One child scaffolded (or to-be-scaffolded) by the approve saga.

    Unique on ``(intake_submission_id, candidate_id)`` so the fan-out is
    idempotent — a resumed/retried approval never duplicates a project. Status
    advances pending → created (compare-and-set) with the child ids stamped.
    """

    __table_args__ = (
        UniqueConstraint(
            "intake_submission_id", "candidate_id", name="uq_intake_spawn_candidate"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    intake_submission_id: int = Field(foreign_key="intakesubmission.id", index=True)
    candidate_id: str = Field(index=True)  # stable blueprint candidate id
    kind: str = ""  # dmig | dpe-sa | dpe-cf | consumes_bind
    status: str = Field(default="pending", index=True)  # pending | created | failed
    child_project_id: Optional[int] = Field(default=None)
    child_request_id: Optional[int] = Field(default=None)
    detail: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class IntakePendingDependency(SQLModel, table=True):
    """A modernization consumer→source dependency awaiting source materialization.

    A dpe-cf candidate cannot bind ``:CONSUMES`` until its source product is
    published (the edge is MATCH-only). We record the dependency keyed by stable
    candidate ids; the PO confirms the binding once the source materializes.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    intake_submission_id: int = Field(foreign_key="intakesubmission.id", index=True)
    consumer_project_id: int = Field(index=True)
    consumer_candidate_id: str = ""
    source_candidate_id: Optional[str] = None  # new source candidate in same intake
    source_external_uri: Optional[str] = None  # already-published source product
    source_project_id: Optional[int] = Field(default=None)  # filled once scaffolded
    status: str = Field(default="pending", index=True)  # pending | bound
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ── Connected Estate + top-down data-product feasibility ─────────────────────
# A SEPARATE bounded context from the Pulse-backed estate discovery
# (``routers/discovery.py`` + ``playbook/discovery/estate.yaml``): here Data
# Workbench connects to live platforms itself, scans the estate, and evaluates
# top-down whether a catalog of desired reference data-product specs is buildable
# (a stoplight: ready | adaptable | assemblable | absent). Independent SQL models,
# independent ``/api/estates/*`` + ``/api/feasibility/*`` routes, independent
# graph anchoring (:Estate / :EstateScan). The Pulse routes/files/UI stay
# UNCHANGED — any future Pulse convergence is an adapter INTO this model, never a
# prerequisite. See the "Connected Estate" plan + docs/connected-estate.md.


class Estate(SQLModel, table=True):
    """A stable business scope the PO wants to assess for data-product feasibility.

    Identity + connection + scan-state are deliberately split across three tables
    (Estate / EstateSource / EstateScan) so a rescan is a new snapshot row, not a
    mutation of the estate's identity. Graph-anchored as ``(:Estate {uri:
    "estate:{id}"})``. New table → auto-created by ``create_all``; no ``_migrate``
    ALTER needed.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    domain: Optional[str] = Field(default=None, index=True)
    description: str = ""
    status: str = Field(default="active", index=True)  # active | archived
    created_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class EstateSource(SQLModel, table=True):
    """A connection + namespace policy contributing to an Estate.

    Modelled as a separate table now (v1 permits one enabled source per estate)
    because multi-source / multi-platform aggregate is the near-term extension.
    ``namespace_policy_json`` is ``{"mode": "all"|"include"|"exclude",
    "namespaces": ["schema", ...]}`` — a right-aligned, per-platform namespace
    filter applied over what the provider's ``list_namespaces`` returns. v1 scopes
    a source to the ONE connected database/catalog the providers already
    enumerate; a database/catalog-enumeration layer is the explicit multi-DB
    extension.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    estate_id: int = Field(foreign_key="estate.id", index=True)
    name: str = ""
    platform: str = ""  # platform_type, denormalized from the connection for display
    # ``live`` = DW connects + scans itself (needs a connection); ``offline`` = the
    # client runs the extraction kit in their own environment and uploads a manifest
    # DW replays into an :EstateScan. An offline source needs only a name + platform
    # (+ optional catalog) — no PlatformConnection creds — so connection_id is nullable.
    ingest_mode: str = Field(default="live")
    connection_id: Optional[int] = Field(default=None, foreign_key="platformconnection.id", index=True)
    # Unity Catalog / database container this source is scoped to (one source per
    # catalog). Empty for 2-level platforms (Postgres/MySQL), where the
    # connection's own database is the container. Threaded into the live connect
    # (extra_config.catalog) + the estate dataset URIs by
    # ``estate.resolve_source_connection_ref``. Immutable once the source has scans.
    catalog: str = Field(default="")
    namespace_policy_json: str = Field(default='{"mode": "all", "namespaces": []}')
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class EstateScan(SQLModel, table=True):
    """One discovery snapshot of an EstateSource — a claimable, leased worker row.

    A rescan is a NEW row (``scan_version`` monotonically increases per
    estate+source); prior snapshots are retained. The broad metadata scan is
    DETERMINISTIC (provider ``list_relations`` + column introspection → the
    estate-scoped graph loader), NOT the LLM discovery skill. Graph-anchored as
    ``(:EstateScan {uri: "estatescan:{id}", version})``. ``stats_json`` records
    objects found/changed/deleted; per-namespace outcomes live in
    :class:`EstateScanNamespace`. Leased like ``IntakeSubmission`` so a crash
    mid-scan is reclaimable.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    estate_id: int = Field(foreign_key="estate.id", index=True)
    source_id: int = Field(foreign_key="estatesource.id", index=True)
    scan_version: int = Field(default=1)
    depth: str = Field(default="metadata")  # metadata | profiled (selective deeper pass)
    # queued → running → completed | partial | failed | cancelled
    state: str = Field(default="queued", index=True)
    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)
    stats_json: str = Field(default="{}")   # {namespaces, relations, columns, new, changed, deleted}
    error_json: str = Field(default="{}")   # aggregate error summary
    # Live scan progress (updated per-namespace during the metadata scan; mirrors
    # enrichment_progress_json): {namespaces_total, namespaces_done, current_namespace,
    # relations_found, columns_found, updated_at, stage?}. Written from its own
    # short-lived session so a progress write never entangles with the scan txn.
    scan_progress_json: str = Field(default="{}")
    initiated_by: str = ""
    # Leased-worker fields (mirrors IntakeSubmission).
    lease_owner: Optional[str] = Field(default=None)
    lease_expires_at: Optional[datetime] = Field(default=None)
    attempts: int = Field(default=0)
    # Metadata enrichment state: None (unenriched) | "enriching" | "enriched" | "failed"
    enrichment_state: Optional[str] = Field(default=None)
    enriched_at: Optional[datetime] = Field(default=None)
    # Live enrichment progress (updated per-namespace during the enriching loop):
    # {schemas_total, schemas_done, current_schema, current_database, tables_total,
    #  tables_done, columns_done, errors}. Namespace-granular (one LLM call per schema).
    enrichment_progress_json: str = Field(default="{}")
    # Schema-level rollup descriptions keyed by the bare `schema` value (the last
    # identity part — a database name on MySQL): {schema_descriptions: {<schema>: <desc>}}.
    enrichment_summary_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class EstateScanNamespace(SQLModel, table=True):
    """Per-namespace selection + outcome for one EstateScan.

    Combines "which namespaces were selected" with "what happened to each" — the
    load-bearing scan-outcome states are persisted here so downstream feasibility
    evaluation can READ the scan state and NEVER emit ``absent`` from a failed /
    partial / inaccessible scan (it returns ``insufficient_evidence`` instead).
    ``outcome`` ∈ {selected, scanned, successful_empty, inaccessible_namespace,
    partial_scan, connection_failure}.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    scan_id: int = Field(foreign_key="estatescan.id", index=True)
    estate_id: int = Field(foreign_key="estate.id", index=True)
    namespace: str = ""  # dotted container path, e.g. "public" or "catalog.schema"
    outcome: str = Field(default="selected", index=True)
    relation_count: int = 0
    column_count: int = 0
    detail: str = ""  # error message / note (untrusted DB text is NOT stored here)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FeasibilityRun(SQLModel, table=True):
    """One top-down feasibility evaluation of a reference-spec catalog against a
    pinned EstateScan — a claimable, leased worker row.

    Pins the ``scan_id`` it read so results are reproducible; stamps every version
    that shaped the verdict (corpus / evaluator / skill / embedding model) for
    audit. ``summary_json`` holds tier counts + evaluation-state counts. Per-spec
    detail lives in :class:`FeasibilityScore`.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    estate_id: int = Field(foreign_key="estate.id", index=True)
    # Back-compat pin: the newest contributing scan. The full evidence set the run
    # read is ``scan_ids_json`` — the latest scan of EACH enabled source, so one
    # run assesses every catalog in the estate (multi-catalog feasibility).
    scan_id: int = Field(foreign_key="estatescan.id", index=True)
    scan_ids_json: str = Field(default="[]")
    spec_ids_json: str = Field(default="[]")   # [] = no filter (evaluate all specs)
    # Tiered schema-scoping levers for this run: {enabled, floor, cap}. Empty ({})
    # → defaults (enabled, SCHEMA_RELEVANCE_FLOOR, MAX_SCHEMAS_PER_SPEC).
    schema_scoping_json: str = Field(default="{}")
    domain: Optional[str] = Field(default=None, index=True)  # None = all domains
    corpus_version: str = ""
    evaluator_version: str = ""
    skill_version: str = ""
    embedding_model: str = ""
    used_skill: bool = Field(default=False)  # False → deterministic heuristic fallback
    # queued → running → completed | partial | failed
    state: str = Field(default="queued", index=True)
    summary_json: str = Field(default="{}")
    # Live evaluation progress (own short-lived session commits — never entangled
    # with score persistence): {stage, specs_total, specs_done, domains_total,
    # domains_done, current_spec, current_domain, updated_at}. stage advances
    # shortlisting → building_evidence → evaluating → finalizing (or `failed`).
    progress_json: str = Field(default="{}")
    error_json: str = Field(default="{}")
    created_by: str = ""
    # Leased-worker fields.
    lease_owner: Optional[str] = Field(default=None)
    lease_expires_at: Optional[datetime] = Field(default=None)
    attempts: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FeasibilityScore(SQLModel, table=True):
    """Per-reference-spec feasibility verdict within a FeasibilityRun.

    The business ``tier`` (ready|adaptable|assemblable|absent) is DISTINCT from
    the ``evaluation_state`` (completed|partial|failed|insufficient_evidence) so a
    scan gap or skill failure is legible and never masquerades as ``absent``. The
    skill decides the tier bounded by backend-enforced invariants (no green
    without a real product candidate; ``absent`` only when evidence is complete
    AND empty). ``evidence_json`` carries the full drill-down bundle (bipartite
    assignment, schema_dna axes, derivations, join plan).
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="feasibilityrun.id", index=True)
    estate_id: int = Field(foreign_key="estate.id", index=True)
    spec_id: str = Field(index=True)
    spec_name: str = ""
    domain: Optional[str] = Field(default=None, index=True)
    tier: str = Field(default="absent", index=True)  # ready|adaptable|assemblable|absent
    # completed | partial | failed | insufficient_evidence
    evaluation_state: str = Field(default="completed", index=True)
    confidence: float = 0.0
    required_coverage: float = 0.0   # fraction of REQUIRED spec attributes covered
    total_coverage: float = 0.0      # fraction of ALL spec attributes covered
    best_product_uri: Optional[str] = Field(default=None)
    best_product_version: Optional[str] = Field(default=None)
    adaptation_notes: str = ""
    rationale: str = ""
    matched_json: str = Field(default="[]")
    gaps_json: str = Field(default="[]")
    derivations_json: str = Field(default="[]")
    join_plan_json: str = Field(default="{}")
    evidence_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FeasibilityCandidate(SQLModel, table=True):
    """A PO-flagged feasibility spec saved as a candidate for future work.

    Created when a PO clicks "Save" on a score card in the Feasibility page —
    lighter than immediately committing to the intake/wizard flow. The candidate
    shows up in My Products as a pipeline of ideas. Dismissed via soft-delete
    (status='dismissed'). De-duped by (score_id, saved_by) so re-saving the
    same score is idempotent.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    score_id: int = Field(foreign_key="feasibilityscore.id", index=True)
    run_id: int = Field(foreign_key="feasibilityrun.id", index=True)
    estate_id: int = Field(foreign_key="estate.id", index=True)
    spec_id: str = Field(index=True)
    spec_name: str = ""
    domain: Optional[str] = Field(default=None)
    tier: str = ""                       # ready|adaptable|assemblable at time of save
    required_coverage: float = 0.0
    confidence: float = 0.0
    rationale: str = ""
    adaptation_notes: str = ""
    notes: str = ""                      # PO-authored free-text annotation
    status: str = Field(default="saved", index=True)   # saved | dismissed
    saved_by: str = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ProductAssembly(SQLModel, table=True):
    """A PO's interactive 'Work this product' session over one FeasibilityScore.

    The durable editing state of the Product Assembly workspace: the user refines the
    evaluation (remap attributes, curate the template, cluster the source tables) into
    a shippable portfolio. It is a **thin overlay** on the immutable
    ``FeasibilityScore.evidence_json`` — ``plan_json`` stores only the user's decisions
    (per-attribute overrides, the cluster plan, any schema-scope override), never a
    copy of the evidence, so re-opening replays evidence + overlay.

    On commit the overlay compiles to an enriched ``ModernizationBlueprint`` staged as a
    ``structured`` ``IntakeSubmission`` (``intake_submission_id``) that flows through the
    existing intake review + scaffold saga. De-duped per ``(score_id, owner_email)``.
    """

    __table_args__ = (
        UniqueConstraint("score_id", "owner_email", name="uq_assembly_score_owner"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    estate_id: int = Field(foreign_key="estate.id", index=True)
    run_id: int = Field(foreign_key="feasibilityrun.id", index=True)
    score_id: int = Field(foreign_key="feasibilityscore.id", index=True)
    spec_id: str = Field(index=True)
    spec_name: str = ""
    domain: Optional[str] = Field(default=None)
    # draft → committed → scaffolded → archived
    status: str = Field(default="draft", index=True)
    owner_email: str = Field(index=True)
    plan_json: str = Field(default="{}")   # overlay: {attributes, clusters, shortlist_override}
    plan_revision: int = Field(default=0)  # optimistic-concurrency token
    intake_submission_id: Optional[int] = Field(default=None)  # set on commit
    # Deterministic, socialize-ready functional report (markdown + mermaid), built on
    # demand and cached here so it re-appears on reopen. Regenerated on ?regenerate=true.
    report_md: Optional[str] = Field(default=None)
    report_generated_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class SpecAttributeGroups(SQLModel, table=True):
    """Cached AI theme-grouping of a reference spec's attributes (per spec_id).

    The grouping is over the FIXED spec (same for every assembly of that spec) and the
    LLM call is slow (200+ attributes), so it's computed once in the background and
    reused. ``status ∈ {computing, ready, failed}``; ``groups_json`` is the validated
    partition. Independent of any assembly's editable plan."""

    spec_id: str = Field(primary_key=True)
    status: str = Field(default="computing")   # computing | ready | failed
    groups_json: str = Field(default="[]")
    corpus_version: str = ""
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class EmbeddingCache(SQLModel, table=True):
    """Persistent content-addressed cache of text embeddings.

    A transparent backing store for :func:`embeddings.embed_documents` — one row
    per (model, text) pair, keyed by ``text_hash = sha256(MODEL_NAME + "\\0" +
    text)`` so a text embedded once is never re-embedded on a later run OR after a
    restart. Covers the static feasibility spec corpus, the per-run schema/affinity
    texts, and value-resolution — cross-run and cross-restart. The estate COLUMN
    vectors live on ``:EstateColumn.embedding`` in the graph instead (queryable via
    the ``estate_column_embedding`` vector index); this table is the residual cache
    for everything that isn't a first-class graph node. Deliberately unbounded to
    start — a 384-float vector is a few KB/row; a size/age prune is a later option.
    """

    text_hash: str = Field(primary_key=True)   # sha256(model + "\0" + text)
    model: str = Field(index=True)
    dim: int = 0
    vec_json: str = Field(default="[]")
    created_at: datetime = Field(default_factory=datetime.utcnow)
