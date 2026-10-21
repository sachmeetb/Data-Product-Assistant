"""The lakehouse runner's manifest must be a valid TransferBatch v1 document.

The old lakehouse_export manifest silently violated the model (invalid
``operation_encoding: "transformed"``, nested ``files`` list). The runner now
emits a real TransferBatch-v1-shaped dict; this test loads a representative
manifest through the actual Pydantic model to guard against drift, and exercises
the runner's reconciliation-status logic.
"""
from __future__ import annotations

import importlib.util
import json
import sys

from workbench.backend import serving_runtime as sr
from workbench.backend.platform.transfer_batch import (
    TransferBatch, OperationEncoding, ReconciliationStatus,
)


def _load_runner():
    # The runner does a sibling `from _wb_runresult import ...` (resolved when run
    # as a subprocess with cwd=package). Put the runners dir on sys.path so the
    # module imports cleanly here too.
    if str(sr.SERVING_RUNNERS_DIR) not in sys.path:
        sys.path.insert(0, str(sr.SERVING_RUNNERS_DIR))
    spec = importlib.util.spec_from_file_location(
        "_run_lakehouse_ut", sr.SERVING_RUNNERS_DIR / "run_lakehouse.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sample_manifest() -> dict:
    """Mirror the dict the runner writes for one model (see run_lakehouse._export)."""
    return {
        "contract_version": "1",
        "run_id": "abc123", "batch_id": "abc123:vw_employees",
        "source_asset_ref": {
            "kind": "relational_relation", "platform_instance_id": "mysql",
            "asset_id": "public.employees", "schema": "public",
            "relation": "employees", "relation_kind": "view",
        },
        "schema_version": 1, "schema_fingerprint": "fp1",
        "canonical_schema": {"id": {"type": "int64", "nullable": True}},
        "file_format": "parquet", "compression": "snappy",
        "file_uris": ["file:///tmp/data/vw_employees.parquet"],
        "file_checksums": ["deadbeef"],
        "row_count": 160, "byte_count": 4096,
        "column_statistics": [{"column_name": "id"}],
        "operation_encoding": "full_load",
        "completion_marker": "complete",
        "created_at": "2026-07-29T00:00:00+00:00",
        "prior_run_reconciliation": None,
        "lineage_evidence": {"serving_mode": "lakehouse_local"},
    }


def test_manifest_loads_as_transfer_batch_v1():
    tb = TransferBatch.model_validate_json(json.dumps(_sample_manifest()))
    assert tb.contract_version == "1"
    assert tb.operation_encoding == OperationEncoding.full_load  # NOT "transformed"
    assert tb.source_asset_ref.kind == "relational_relation"
    assert tb.source_asset_ref.schema_name == "public"
    assert tb.file_uris == ["file:///tmp/data/vw_employees.parquet"]
    assert tb.file_checksums == ["deadbeef"]
    assert tb.row_count == 160


def test_manifest_with_reconciliation_loads():
    m = _sample_manifest()
    m["prior_run_reconciliation"] = {
        "status": "idempotent", "prior_batch_id": "old:vw_employees",
        "prior_schema_fingerprint": "fp1", "prior_row_count": 160,
        "prior_file_checksum": "deadbeef", "details": {},
    }
    tb = TransferBatch.model_validate_json(json.dumps(m))
    assert tb.prior_run_reconciliation.status == ReconciliationStatus.idempotent


# ── runner reconciliation-status logic ─────────────────────────────────────────


def test_reconcile_status_transitions():
    rl = _load_runner()
    assert rl._reconcile(None, "b", 10, "fp", "cs") is None
    prior = {"batch_id": "b0", "schema_fingerprint": "fp", "row_count": 10,
             "file_checksums": ["cs"]}
    # identical → idempotent
    assert rl._reconcile(prior, "b1", 10, "fp", "cs")["status"] == "idempotent"
    # schema drift
    assert rl._reconcile(prior, "b1", 10, "fp2", "cs")["status"] == "schema_changed"
    # row delta
    assert rl._reconcile(prior, "b1", 11, "fp", "cs")["status"] == "row_count_changed"
    # same schema+rows, different bytes
    assert rl._reconcile(prior, "b1", 10, "fp", "cs2")["status"] == "data_changed"
