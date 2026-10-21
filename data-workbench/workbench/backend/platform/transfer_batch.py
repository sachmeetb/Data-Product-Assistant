"""Versioned data-plane exchange contract — TransferBatch v1.

A TransferBatch is the durable manifest that accompanies one batch of data
as it moves from a source asset toward a destination.  It carries enough
evidence to:

- verify delivery completeness (row/byte counts, checksums);
- resume an interrupted transfer (incremental state before/after);
- reconstruct lineage (source asset ref, snapshot boundary);
- detect schema drift (fingerprint + canonical column map).

Designed to be serialized as JSON and stored alongside Parquet files.
See ADR-13 in the architecture review for the full rationale.

The ``PhysicalAssetRef`` discriminated union (§7.2 of the architecture review)
generalizes the current ``pg_connection`` string into a typed identity that
covers relational tables, Iceberg tables, and raw file collections.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# ── enums ──────────────────────────────────────────────────────────────────────

class RelationKind(str, Enum):
    table = "table"
    view = "view"
    materialized_view = "materialized_view"


class FileFormat(str, Enum):
    parquet = "parquet"
    csv = "csv"


class OperationEncoding(str, Enum):
    full_load = "full_load"
    cdc_append = "cdc_append"
    upsert = "upsert"
    delete = "delete"


class CompletionMarker(str, Enum):
    incomplete = "incomplete"
    complete = "complete"
    partial = "partial"
    failed = "failed"


class ReconciliationStatus(str, Enum):
    """Outcome of comparing a re-run against the prior batch manifest."""
    first_run = "first_run"           # no prior manifest found
    idempotent = "idempotent"         # same fingerprint + row_count + checksum
    row_count_changed = "row_count_changed"
    schema_changed = "schema_changed"
    data_changed = "data_changed"     # same schema + row_count but different checksum


# ── reconciliation evidence ───────────────────────────────────────────────────

class ReconciliationEvidence(BaseModel):
    """Structured comparison of this batch against the prior run's manifest.

    Present when re-running an export and a prior manifest existed at the same
    path.  ``status`` is the canonical verdict; the other fields give engineers
    enough context to decide whether to accept or investigate the re-run.
    """
    status: ReconciliationStatus
    prior_batch_id: str
    prior_schema_fingerprint: str = ""
    prior_row_count: Optional[int] = None
    prior_file_checksum: str = Field(
        default="",
        description="SHA-256 of the first file from the prior manifest",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Any additional context (e.g. which columns changed schema)",
    )


# ── physical asset refs ────────────────────────────────────────────────────────

class RelationalRelationRef(BaseModel):
    """A table, view, or materialized view in a JDBC/ODBC-addressable database."""
    kind: Literal["relational_relation"] = "relational_relation"
    platform_instance_id: str = Field(
        ..., description="Opaque ID for the PlatformConnection row that owns this asset"
    )
    asset_id: str = Field(
        ..., description="Fully-qualified dotted path: [catalog.]schema.relation"
    )
    display_name: str = ""
    catalog: str = ""
    schema_name: str = Field("", alias="schema")
    relation: str
    relation_kind: RelationKind = RelationKind.table

    model_config = {"populate_by_name": True}


class LakehouseTableRef(BaseModel):
    """An Iceberg table registered in a REST catalog."""
    kind: Literal["lakehouse_table"] = "lakehouse_table"
    platform_instance_id: str
    asset_id: str = Field(
        ..., description="Catalog-qualified dotted path: [catalog.]namespace.table"
    )
    display_name: str = ""
    catalog_connection_id: str
    namespace: list[str] = Field(default_factory=list)
    table: str
    table_uuid: str = ""
    format: Literal["iceberg"] = "iceberg"
    object_store_connection_id: str = ""
    base_location: str = ""


class FileCollectionRef(BaseModel):
    """A set of files in object storage (Parquet, CSV, etc.)."""
    kind: Literal["file_collection"] = "file_collection"
    platform_instance_id: str
    asset_id: str = Field(
        ..., description="URI prefix uniquely identifying the collection"
    )
    display_name: str = ""
    object_store_connection_id: str = ""
    uri_prefix: str
    format: FileFormat = FileFormat.parquet
    manifest_uri: str = ""
    schema_fingerprint: str = ""


# Discriminated union — Pydantic dispatches on the ``kind`` field.
PhysicalAssetRef = Annotated[
    Union[RelationalRelationRef, LakehouseTableRef, FileCollectionRef],
    Field(discriminator="kind"),
]


# ── supporting models ──────────────────────────────────────────────────────────

class SnapshotBoundary(BaseModel):
    """How the source snapshot was bounded (consistent-read, timestamp, etc.)."""
    kind: str = ""
    value: Optional[Any] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class IncrementalState(BaseModel):
    """Checkpoint / offset describing progress through the source stream."""
    checkpoint_kind: str = ""   # e.g. "lsn", "timestamp", "kafka_offset"
    value: Optional[Any] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ColumnStat(BaseModel):
    """Per-column statistics embedded in the batch manifest."""
    column_name: str
    null_count: Optional[int] = None
    value_count: Optional[int] = None
    distinct_count: Optional[int] = None
    lower_bound: Optional[Any] = None
    upper_bound: Optional[Any] = None


# ── transfer batch ─────────────────────────────────────────────────────────────

class TransferBatch(BaseModel):
    """Durable manifest for one batch of data moving from a source asset.

    v1 is intentionally minimal — it captures the identity, schema evidence,
    payload addresses, and lifecycle markers needed for basic delivery
    verification and resumability.  Later versions (v2+) will add CDC
    offset coordination, Iceberg commit evidence, and richer quality checks.

    Serialize to JSON for storage alongside Parquet files::

        manifest = batch.model_dump_json(indent=2)

    Parse from a stored manifest::

        batch = TransferBatch.model_validate_json(raw_json)
    """

    # ── identity ───────────────────────────────────────────────────────────
    contract_version: Literal["1"] = "1"
    run_id: str = Field(..., description="Opaque run identifier; typically a UUID")
    batch_id: str = Field(
        ..., description="Opaque batch identifier within the run; sequential or UUID"
    )

    # ── source ─────────────────────────────────────────────────────────────
    source_asset_ref: PhysicalAssetRef
    source_snapshot_boundary: Optional[SnapshotBoundary] = None
    incremental_state_before: Optional[IncrementalState] = None
    incremental_state_after: Optional[IncrementalState] = None

    # ── schema evidence ────────────────────────────────────────────────────
    schema_version: int = 1
    schema_fingerprint: str = Field(
        default="",
        description="Stable hash of canonical_schema; used to detect schema drift",
    )
    canonical_schema: dict[str, Any] = Field(
        default_factory=dict,
        description='Column name → {"type": ..., "nullable": ...} canonical descriptor',
    )

    # ── payload ────────────────────────────────────────────────────────────
    file_format: FileFormat = FileFormat.parquet
    compression: str = "zstd"
    file_uris: list[str] = Field(
        default_factory=list,
        description="Object-store URIs for the payload files (s3://, gs://, file://, …)",
    )
    file_checksums: list[str] = Field(
        default_factory=list,
        description="SHA-256 hex digests, one per file_uri in the same order",
    )
    row_count: Optional[int] = None
    byte_count: Optional[int] = None
    column_statistics: list[ColumnStat] = Field(default_factory=list)

    # ── security ───────────────────────────────────────────────────────────
    encryption_context: dict[str, str] = Field(
        default_factory=dict,
        description="KMS key alias or envelope-key reference; empty if unencrypted",
    )
    operation_encoding: OperationEncoding = OperationEncoding.full_load

    # ── key / sequencing ───────────────────────────────────────────────────
    primary_key_columns: list[str] = Field(default_factory=list)
    sequence_column: str = Field(
        default="",
        description="Column whose value monotonically tracks insertion order (e.g. created_at)",
    )

    # ── quality evidence ───────────────────────────────────────────────────
    rejected_record_summary: Optional[dict[str, Any]] = Field(
        default=None,
        description='Counts and reasons for records excluded from the batch, e.g. {"count": 3, "reasons": {…}}',
    )

    # ── lifecycle ──────────────────────────────────────────────────────────
    completion_marker: CompletionMarker = CompletionMarker.incomplete
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    retention_until: Optional[datetime] = None

    # ── re-run reconciliation ──────────────────────────────────────────────
    prior_run_reconciliation: Optional[ReconciliationEvidence] = Field(
        default=None,
        description=(
            "Populated when a prior manifest existed at the same output path. "
            "status='idempotent' means the re-run produced the same data; "
            "any other status warrants investigation before promotion."
        ),
    )

    # ── lineage ────────────────────────────────────────────────────────────
    lineage_evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form provenance links — data contract URI, stage_run_id, etc.",
    )
