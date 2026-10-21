"""Tests for the TransferBatch v1 data-plane contract.

Covers:
- PhysicalAssetRef discriminated union round-trips
- TransferBatch JSON serialization / deserialization
- Completion lifecycle
- Schema fingerprint round-trip
- Incremental-state preservation
- File-collection and lakehouse refs
"""
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from workbench.backend.platform.transfer_batch import (
    ColumnStat,
    CompletionMarker,
    FileCollectionRef,
    FileFormat,
    IncrementalState,
    LakehouseTableRef,
    OperationEncoding,
    RelationalRelationRef,
    SnapshotBoundary,
    TransferBatch,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _relational_ref(**kwargs) -> RelationalRelationRef:
    defaults = {
        "platform_instance_id": "pg-1",
        "asset_id": "public.orders",
        "relation": "orders",
    }
    return RelationalRelationRef(**{**defaults, **kwargs})


def _minimal_batch(**kwargs) -> TransferBatch:
    defaults = {
        "run_id": "run-abc",
        "batch_id": "batch-001",
        "source_asset_ref": _relational_ref(),
    }
    return TransferBatch(**{**defaults, **kwargs})


# ── PhysicalAssetRef discriminated union ───────────────────────────────────────

class TestPhysicalAssetRef:
    def test_relational_ref_roundtrip(self):
        ref = _relational_ref(catalog="mydb", schema="public", relation="orders")
        raw = ref.model_dump()
        assert raw["kind"] == "relational_relation"
        assert raw["relation"] == "orders"
        restored = RelationalRelationRef.model_validate(raw)
        assert restored.relation == "orders"
        assert restored.schema_name == "public"

    def test_relational_ref_schema_alias(self):
        ref = RelationalRelationRef(
            platform_instance_id="pg-1",
            asset_id="public.orders",
            relation="orders",
            schema="public",  # alias
        )
        assert ref.schema_name == "public"
        # model_dump with by_alias=True should use 'schema'
        raw = ref.model_dump(by_alias=True)
        assert "schema" in raw

    def test_lakehouse_ref_roundtrip(self):
        ref = LakehouseTableRef(
            platform_instance_id="iceberg-1",
            asset_id="warehouse.sales.orders",
            catalog_connection_id="cat-1",
            namespace=["sales"],
            table="orders",
            object_store_connection_id="s3-1",
            base_location="s3://my-bucket/warehouse/sales/orders",
        )
        raw = ref.model_dump()
        assert raw["kind"] == "lakehouse_table"
        assert raw["format"] == "iceberg"
        restored = LakehouseTableRef.model_validate(raw)
        assert restored.table == "orders"
        assert restored.namespace == ["sales"]

    def test_file_collection_ref_roundtrip(self):
        ref = FileCollectionRef(
            platform_instance_id="s3-1",
            asset_id="s3://my-bucket/exports/orders/",
            uri_prefix="s3://my-bucket/exports/orders/",
            format=FileFormat.parquet,
            schema_fingerprint="abc123",
        )
        raw = ref.model_dump()
        assert raw["kind"] == "file_collection"
        assert raw["format"] == "parquet"

    def test_discriminated_union_dispatches_on_kind(self):
        batch = _minimal_batch(
            source_asset_ref={
                "kind": "file_collection",
                "platform_instance_id": "s3-1",
                "asset_id": "s3://bucket/prefix/",
                "uri_prefix": "s3://bucket/prefix/",
            }
        )
        assert isinstance(batch.source_asset_ref, FileCollectionRef)

    def test_unknown_kind_raises(self):
        with pytest.raises(ValidationError):
            _minimal_batch(
                source_asset_ref={
                    "kind": "unknown_future_kind",
                    "platform_instance_id": "x",
                    "asset_id": "x",
                }
            )


# ── TransferBatch construction ─────────────────────────────────────────────────

class TestTransferBatchConstruction:
    def test_minimal_batch_defaults(self):
        batch = _minimal_batch()
        assert batch.contract_version == "1"
        assert batch.file_format == FileFormat.parquet
        assert batch.compression == "zstd"
        assert batch.operation_encoding == OperationEncoding.full_load
        assert batch.completion_marker == CompletionMarker.incomplete
        assert batch.file_uris == []
        assert batch.file_checksums == []
        assert batch.column_statistics == []
        assert batch.primary_key_columns == []
        assert batch.row_count is None
        assert batch.byte_count is None

    def test_full_batch(self):
        batch = TransferBatch(
            run_id="run-xyz",
            batch_id="batch-1",
            source_asset_ref=_relational_ref(),
            source_snapshot_boundary=SnapshotBoundary(
                kind="consistent_read", value="2026-07-27T10:00:00Z"
            ),
            incremental_state_before=IncrementalState(
                checkpoint_kind="lsn", value="0/1AB2CD3"
            ),
            incremental_state_after=IncrementalState(
                checkpoint_kind="lsn", value="0/1AB2CD4"
            ),
            schema_version=2,
            schema_fingerprint="sha256:deadbeef",
            canonical_schema={"id": {"type": "int64", "nullable": False}},
            file_format=FileFormat.parquet,
            compression="snappy",
            file_uris=["s3://bucket/run-xyz/batch-1.parquet"],
            file_checksums=["abc123"],
            row_count=1000,
            byte_count=50000,
            column_statistics=[
                ColumnStat(
                    column_name="id",
                    null_count=0,
                    value_count=1000,
                    distinct_count=1000,
                    lower_bound=1,
                    upper_bound=1000,
                )
            ],
            operation_encoding=OperationEncoding.upsert,
            primary_key_columns=["id"],
            sequence_column="created_at",
            completion_marker=CompletionMarker.complete,
            lineage_evidence={"stage_run_id": "42"},
        )
        assert batch.row_count == 1000
        assert batch.completion_marker == CompletionMarker.complete
        assert batch.schema_fingerprint == "sha256:deadbeef"
        assert batch.incremental_state_after.value == "0/1AB2CD4"

    def test_created_at_is_utc_aware(self):
        batch = _minimal_batch()
        assert batch.created_at.tzinfo is not None

    def test_contract_version_is_always_1(self):
        batch = _minimal_batch(contract_version="1")
        assert batch.contract_version == "1"
        # Passing a different value should fail (Literal["1"])
        with pytest.raises(ValidationError):
            _minimal_batch(contract_version="2")


# ── JSON round-trip ────────────────────────────────────────────────────────────

class TestTransferBatchJsonRoundtrip:
    def test_serialize_and_parse_minimal(self):
        batch = _minimal_batch()
        raw = batch.model_dump_json()
        restored = TransferBatch.model_validate_json(raw)
        assert restored.run_id == batch.run_id
        assert restored.batch_id == batch.batch_id
        assert isinstance(restored.source_asset_ref, RelationalRelationRef)

    def test_serialize_and_parse_lakehouse(self):
        batch = TransferBatch(
            run_id="r1",
            batch_id="b1",
            source_asset_ref=LakehouseTableRef(
                platform_instance_id="iceberg-1",
                asset_id="warehouse.sales.orders",
                catalog_connection_id="cat-1",
                namespace=["sales"],
                table="orders",
                object_store_connection_id="s3-1",
                base_location="s3://bucket/warehouse/",
            ),
            completion_marker=CompletionMarker.complete,
            row_count=500,
        )
        raw_json = batch.model_dump_json(indent=2)
        parsed = json.loads(raw_json)
        assert parsed["source_asset_ref"]["kind"] == "lakehouse_table"
        assert parsed["completion_marker"] == "complete"

        restored = TransferBatch.model_validate_json(raw_json)
        assert isinstance(restored.source_asset_ref, LakehouseTableRef)
        assert restored.source_asset_ref.namespace == ["sales"]
        assert restored.row_count == 500

    def test_serialize_and_parse_file_collection(self):
        batch = TransferBatch(
            run_id="r2",
            batch_id="b1",
            source_asset_ref=FileCollectionRef(
                platform_instance_id="s3-1",
                asset_id="s3://bucket/prefix/",
                uri_prefix="s3://bucket/prefix/",
                format=FileFormat.csv,
            ),
        )
        raw_json = batch.model_dump_json()
        restored = TransferBatch.model_validate_json(raw_json)
        assert isinstance(restored.source_asset_ref, FileCollectionRef)
        assert restored.source_asset_ref.format == FileFormat.csv

    def test_column_stats_roundtrip(self):
        stat = ColumnStat(
            column_name="amount",
            null_count=5,
            value_count=995,
            distinct_count=200,
            lower_bound=0.01,
            upper_bound=9999.99,
        )
        batch = _minimal_batch(column_statistics=[stat])
        raw = batch.model_dump_json()
        restored = TransferBatch.model_validate_json(raw)
        assert len(restored.column_statistics) == 1
        cs = restored.column_statistics[0]
        assert cs.column_name == "amount"
        assert cs.lower_bound == 0.01
        assert cs.null_count == 5

    def test_incremental_state_roundtrip(self):
        before = IncrementalState(checkpoint_kind="lsn", value="0/AABBCC")
        after = IncrementalState(checkpoint_kind="lsn", value="0/AABBEE")
        batch = _minimal_batch(
            incremental_state_before=before,
            incremental_state_after=after,
        )
        raw = batch.model_dump_json()
        restored = TransferBatch.model_validate_json(raw)
        assert restored.incremental_state_before.value == "0/AABBCC"
        assert restored.incremental_state_after.value == "0/AABBEE"


# ── validation guards ──────────────────────────────────────────────────────────

class TestTransferBatchValidation:
    def test_missing_run_id_raises(self):
        with pytest.raises(ValidationError):
            TransferBatch(
                batch_id="b1",
                source_asset_ref=_relational_ref(),
            )

    def test_missing_batch_id_raises(self):
        with pytest.raises(ValidationError):
            TransferBatch(
                run_id="r1",
                source_asset_ref=_relational_ref(),
            )

    def test_missing_source_asset_ref_raises(self):
        with pytest.raises(ValidationError):
            TransferBatch(run_id="r1", batch_id="b1")
