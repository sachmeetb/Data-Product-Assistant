"""Stable service provider interfaces (SPIs) for platform adapters.

Every provider is a Protocol — duck-typing, no forced inheritance.
Providers must return core contract objects (dataclasses defined here),
never vendor cursors or exceptions.

Rule: renderers are pure artifact compilers (no live connections).
Executors own sessions, limits, parameters, and error mapping.
Deployment providers own lifecycle, grants, and compensation.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


# ── capability levels ────────────────────────────────────────────────────────

class CapabilityLevel(str, enum.Enum):
    UNSUPPORTED = "unsupported"
    EXPERIMENTAL = "experimental"
    PREVIEW = "preview"
    CERTIFIED = "certified"
    DEPRECATED = "deprecated"


# ── shared contract types ─────────────────────────────────────────────────────

@dataclass
class CapabilityEvidence:
    """Result of a live capability probe against a platform instance."""
    platform_type: str
    instance_id: str
    server_version: Optional[str] = None
    adapter_version: Optional[str] = None
    capabilities: dict[str, CapabilityLevel] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class NamespaceRef:
    platform_instance_id: str
    parts: list[str]          # e.g. ["catalog", "schema"] or ["schema"]
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class RelationSummary:
    namespace: NamespaceRef
    name: str
    relation_kind: str        # "table" | "view" | "materialized_view"
    row_count: Optional[int] = None
    comment: Optional[str] = None
    size_bytes: Optional[int] = None
    last_modified: Optional[str] = None
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class ColumnInfo:
    """One column's metadata for a relation — the neutral result of
    ``DiscoveryProvider.list_columns`` (used to hydrate NL→SQL schema hints)."""
    name: str
    data_type: str
    nullable: bool = True
    ordinal: Optional[int] = None


@dataclass
class ForeignKeyInfo:
    """One FK constraint: a column in from_table references a column in to_table."""
    from_schema: str
    from_table: str
    from_column: str
    to_schema: str
    to_table: str
    to_column: str


@dataclass
class DiscoveryBundle:
    """Versioned output of describe_relations — fed to the graph loader."""
    schema_version: str = "v1"
    platform_type: str = ""
    platform_instance_id: str = ""
    server_version: Optional[str] = None
    adapter_version: Optional[str] = None
    relations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ManagedConnection:
    """Opaque handle returned by ConnectionProvider.open().
    Providers store their driver connection inside; callers never touch it."""
    platform_type: str
    instance_id: str
    purpose: str
    _internal: Any = field(default=None, repr=False)


@dataclass
class QueryLimits:
    max_rows: int = 1000
    timeout_ms: int = 30_000
    max_bytes: Optional[int] = None
    read_only: bool = True


@dataclass
class ResultSet:
    columns: list[dict[str, Any]]
    rows: list[list[Any]]
    truncated: bool = False
    row_count: int = 0
    duration_ms: int = 0


@dataclass
class CompiledQuery:
    sql: str
    dialect: str
    parameters: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DdlArtifact:
    statements: list[str]
    dialect: str
    object_names: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DeploymentChangeSet:
    artifact: DdlArtifact
    current_state_hash: Optional[str] = None
    is_no_op: bool = False
    destructive_operations: list[str] = field(default_factory=list)
    non_atomic_operations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DeploymentRun:
    run_id: str
    status: str                     # "succeeded" | "failed" | "partial"
    deployed_objects: list[str] = field(default_factory=list)
    failed_objects: list[str] = field(default_factory=list)
    duration_ms: int = 0
    error_class: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class VerificationReport:
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CompensationReport:
    compensated: bool
    residual_objects: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── cross-platform transfer (Phase 2) ──────────────────────────────────────────

@dataclass
class TransferDatasetPlan:
    """Per-output-dataset half of a TransferPlan.

    ``extract_sql`` is the source-dialect SELECT run against the source; its
    result is loaded into ``target_relation``. When ``target_model_sql`` is set,
    a target-side transform (dbt model) runs after load (ELT/hybrid); when it is
    None the extract already produced the final shape (ETL / transform_on_extract).
    """
    physical_name: str
    target_relation: str
    extract_sql: str
    write_disposition: str = "replace"     # replace | append | merge
    merge_keys: list[str] = field(default_factory=list)
    target_model_sql: Optional[str] = None
    expected_row_count: Optional[int] = None


@dataclass
class TransferSpec:
    """Inputs to plan a cross-platform transfer for one product."""
    product_uri: str
    source_platform: str
    target_platform: str
    source_connection_ref: dict[str, Any] = field(default_factory=dict)
    target_connection_ref: dict[str, Any] = field(default_factory=dict)
    target_schema: str = "public"
    placement: str = "hybrid"              # transform_on_extract | hybrid | transfer_then_transform
    write_disposition: str = "replace"


@dataclass
class TransferPlan:
    """Resolved, executable plan — pure output of plan_transfer (no I/O)."""
    spec: TransferSpec
    datasets: list[TransferDatasetPlan] = field(default_factory=list)
    placement: str = "hybrid"
    warnings: list[str] = field(default_factory=list)


@dataclass
class CodeAssetSummary:
    """One code asset (task / dynamic table / stream / procedure / notebook /
    job / pipeline) discovered by a ``CodeAssetProvider``."""
    name: str
    asset_kind: str          # task|dynamic_table|stream|procedure|notebook|job|pipeline
    namespace: Optional[NamespaceRef] = None
    language: Optional[str] = None
    schedule: Optional[str] = None
    definition_preview: Optional[str] = None   # bounded ~4 KB — NOT full source
    definition_hash: Optional[str] = None
    depends_on: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


# ── provider Protocols ────────────────────────────────────────────────────────

@runtime_checkable
class ConnectionProvider(Protocol):
    """Validates config, probes capabilities, opens managed connections."""

    def validate_config(
        self, public_config: dict[str, Any], secret_ref: str
    ) -> ValidationReport: ...

    def probe(self, connection_ref: dict[str, Any]) -> CapabilityEvidence: ...

    def open(
        self, connection_ref: dict[str, Any], purpose: str
    ) -> ManagedConnection: ...


@runtime_checkable
class DiscoveryProvider(Protocol):
    """Lists namespaces and relations; describes their metadata."""

    def list_namespaces(
        self, connection_ref: dict[str, Any], parent: Optional[str] = None
    ) -> list[NamespaceRef]: ...

    def list_relations(
        self, connection_ref: dict[str, Any], namespace: NamespaceRef
    ) -> list[RelationSummary]: ...

    def describe_relations(
        self, connection_ref: dict[str, Any], refs: list[RelationSummary]
    ) -> DiscoveryBundle: ...

    def list_columns(
        self, connection_ref: dict[str, Any], relation: RelationSummary
    ) -> list["ColumnInfo"]:
        """Return the column metadata for a single relation (table/view).

        A focused, cheap columns-only query — distinct from
        ``describe_relations`` (which returns the full PK/FK/index bundle).
        Routes the NL→SQL schema-hint hydration for marketplace Q&A /
        deployment reflection, replacing the Postgres-only
        ``_list_view_columns`` helpers.
        """
        ...

    def list_foreign_keys(
        self, connection_ref: dict[str, Any], schema: str
    ) -> list["ForeignKeyInfo"]:
        """Return FK constraints for all tables in a schema.

        Best-effort: return [] when the platform doesn't support FK introspection.
        Never raises — callers treat an empty list as 'no FK info available'.
        """
        ...


@runtime_checkable
class QueryExecutor(Protocol):
    """Runs SQL against a live connection with limits and error mapping."""

    def explain(
        self,
        connection_ref: dict[str, Any],
        query: str,
        limits: QueryLimits,
    ) -> ValidationReport: ...

    def select(
        self,
        connection_ref: dict[str, Any],
        query: str,
        limits: QueryLimits,
    ) -> ResultSet: ...

    def classify_error(self, exception: Exception) -> str: ...


@runtime_checkable
class QueryRenderer(Protocol):
    """Pure: compiles a transform IR to a target-dialect SQL artifact."""

    def render_query(
        self, transform_ir: dict[str, Any], context: dict[str, Any]
    ) -> CompiledQuery: ...

    def validate_query(self, artifact: CompiledQuery) -> ValidationReport: ...


@runtime_checkable
class DdlRenderer(Protocol):
    """Pure: wraps a compiled query in target-dialect DDL."""

    def render_view(
        self, compiled_query: CompiledQuery, target: dict[str, Any]
    ) -> DdlArtifact: ...

    def render_relation_change(
        self, change: dict[str, Any], target: dict[str, Any]
    ) -> DdlArtifact: ...

    def validate_ddl(self, artifact: DdlArtifact) -> ValidationReport: ...


@runtime_checkable
class DeploymentProvider(Protocol):
    """Owns the full view/table lifecycle: plan → apply → verify → compensate."""

    def plan(
        self, artifact: DdlArtifact, current_state: Optional[dict[str, Any]]
    ) -> DeploymentChangeSet: ...

    def apply(
        self, change_set: DeploymentChangeSet, idempotency_key: str
    ) -> DeploymentRun: ...

    def deploy_views(
        self,
        connection_ref: dict[str, Any],
        ddl: str,
        view_schema: str,
        view_names: list[str],
    ) -> DeploymentRun:
        """Deploy CREATE/DROP VIEW ``ddl`` to ``view_schema`` and smoke-test each
        view. The concrete entry point the ``sql_executor`` facade delegates to
        (the safety gate + audit stay in the facade). Owns driver execution +
        error classification: returns a ``DeploymentRun`` with ``status`` in
        {"succeeded","failed"} and, on failure, ``error_class`` / ``error_message``
        — never raises a vendor exception past this boundary. Postgres owns its
        view-recreate recovery here."""
        ...

    def verify(self, run: DeploymentRun) -> VerificationReport: ...

    def compensate(self, run: DeploymentRun) -> CompensationReport: ...


@runtime_checkable
class TransferExecutionProvider(Protocol):
    """Moves data across a platform boundary (Extract + Load) for
    ``transfer_then_transform`` serving. Emits a TransferBatch per run for
    reconciliation. The Transform half is compiled once by the shared SQL core
    and split by the placement planner — NOT owned here. (Phase 2.)"""

    def plan_transfer(self, spec: "TransferSpec") -> "TransferPlan": ...

    def execute_transfer(
        self, plan: "TransferPlan", idempotency_key: str
    ) -> "DeploymentRun": ...

    def verify_transfer(self, run: "DeploymentRun") -> VerificationReport: ...

    def compensate_transfer(self, run: "DeploymentRun") -> CompensationReport: ...


# ── object store (ADR-14) ──────────────────────────────────────────────────────

@dataclass
class ObjectRef:
    """One object listed under a prefix in an object store."""
    key: str
    size: int = 0
    etag: Optional[str] = None
    last_modified: Optional[str] = None


@runtime_checkable
class ObjectStoreProvider(Protocol):
    """Publishes / lists / presigns binary artifacts in an S3-compatible object
    store. Distinct from the SQL-oriented ConnectionProvider: no cursors, no
    SELECT — just object I/O. Resolved via ``dispatch.get_object_store_provider``.

    ``connection_ref`` mirrors the ConnectionProvider shape (host / port /
    username / resolved_password / extra_config), plus object-store keys in
    ``extra_config`` — ``endpoint_url`` (internal), ``public_endpoint_url`` (for
    presigning), ``bucket``, ``region``, ``addressing_style``, ``use_ssl``.
    A per-call ``bucket`` overrides ``extra_config.bucket`` (the publish path
    passes the binding's bucket). See docs/architecture/object-store.md (ADR-14).
    """

    def validate_config(
        self, public_config: dict[str, Any], secret_ref: str
    ) -> ValidationReport: ...

    def probe(self, connection_ref: dict[str, Any]) -> CapabilityEvidence: ...

    def ensure_bucket(
        self, connection_ref: dict[str, Any], bucket: Optional[str] = None
    ) -> None: ...

    def upload_file(
        self, connection_ref: dict[str, Any], local_path: str, key: str,
        *, bucket: Optional[str] = None, content_type: Optional[str] = None,
    ) -> str: ...

    def put_bytes(
        self, connection_ref: dict[str, Any], data: bytes, key: str,
        *, bucket: Optional[str] = None, content_type: Optional[str] = None,
    ) -> str: ...

    def list_prefix(
        self, connection_ref: dict[str, Any], prefix: str,
        *, bucket: Optional[str] = None,
    ) -> list[ObjectRef]: ...

    def presign_get(
        self, connection_ref: dict[str, Any], key: str,
        *, bucket: Optional[str] = None, ttl_seconds: int = 3600,
    ) -> str: ...


@runtime_checkable
class CodeAssetProvider(Protocol):
    """Lists code assets (tasks / notebooks / jobs / pipelines) for an estate source.

    One call per scan, passing the selected namespaces.  Best-effort: a 403 or
    import error must return ``[]``, never propagate.
    """

    def list_code_assets(
        self, connection_ref: dict[str, Any], namespaces: list[NamespaceRef]
    ) -> list[CodeAssetSummary]: ...
