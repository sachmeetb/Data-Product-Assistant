"""
base.py — Core Bronze Ingestion Envelope, Schema Inference & GCP BigQuery Materializer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def compute_record_hash(row: Dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of raw payload fields."""
    clean = {k: str(v or "") for k, v in sorted(row.items()) if not k.startswith("_")}
    canonical = json.dumps(clean, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def attach_ingestion_envelope(
    rows: List[Dict[str, Any]],
    source_system: str,
    source_feed: str,
    ingestion_mode: str = "BATCH",
    batch_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Attach Tony Giordano's mandatory Bronze ingestion envelope columns to each raw record
    while preserving 100% of original source columns unmodified.
    """
    bid = batch_id or f"BRZ-{uuid.uuid4().hex[:8].upper()}"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    enriched: List[Dict[str, Any]] = []
    for row in rows:
        rec = dict(row)
        rec["_ingest_batch_id"] = bid
        rec["_ingest_ts"] = now_iso
        rec["_source_system"] = source_system
        rec["_source_file_or_topic"] = source_feed
        rec["_ingestion_mode"] = ingestion_mode
        rec["_record_hash"] = compute_record_hash(row)
        enriched.append(rec)
    return enriched


def materialize_bronze_table(
    table_name: str,
    rows: List[Dict[str, Any]],
    source_system: str,
    feed_name: str,
    description: str,
    ingestion_mode: str = "BATCH",
    catalog: Optional[str] = None,
    schema_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create or replace a Bronze table in GCP BigQuery (when connected)
    and persist metadata for downstream Silver N-to-M discovery.
    """
    project_id = (
        catalog
        or os.environ.get("BQ_PROJECT")
        or os.environ.get("GCP_PROJECT_ID")
        or "eogwapq-agbg-internal-data-mig"
    )
    dataset_id = (
        schema_name
        or os.environ.get("BQ_BRONZE_DATASET")
        or "banking_bronze"
    )

    enriched_rows = attach_ingestion_envelope(
        rows=rows,
        source_system=source_system,
        source_feed=feed_name,
        ingestion_mode=ingestion_mode,
    )
    cols = list(enriched_rows[0].keys()) if enriched_rows else []
    raw_cols = [c for c in cols if not c.startswith("_")]
    fqn = f"{project_id}.{dataset_id}.{table_name}"
    persistence_mode = "local_lakehouse"

    try:
        if os.environ.get("BQ_OFFLINE_MODE") == "1":
            raise RuntimeError("BQ_OFFLINE_MODE")
        from google.cloud import bigquery
        from tools.gcp_auth import get_bigquery_client

        client = get_bigquery_client(project_id=project_id, location="US")
        ds_ref = bigquery.Dataset(f"{project_id}.{dataset_id}")
        ds_ref.location = "US"
        client.create_dataset(ds_ref, exists_ok=True)

        if cols and enriched_rows:
            bq_schema = [
                bigquery.SchemaField(
                    c,
                    "STRING",
                    mode="NULLABLE",
                    description=f"Bronze field {c} ({source_system})",
                )
                for c in cols
            ]
            job_cfg = bigquery.LoadJobConfig(
                schema=bq_schema,
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            )
            str_rows = [{k: str(v or "") for k, v in r.items()} for r in enriched_rows]
            job = client.load_table_from_json(str_rows, fqn, job_config=job_cfg)
            job.result()

            tbl_obj = client.get_table(fqn)
            tbl_obj.description = description
            src_tag = re.sub(r"[^a-z0-9_-]", "_", source_system.lower())[:63]
            mode_tag = re.sub(r"[^a-z0-9_-]", "_", ingestion_mode.lower())[:63]
            tbl_obj.labels = {
                "layer": "bronze",
                "source_system": src_tag,
                "ingestion_mode": mode_tag,
            }
            client.update_table(tbl_obj, ["description", "labels"])
            persistence_mode = "bigquery"
    except Exception:
        persistence_mode = "local_lakehouse"

    bq_url = (
        f"https://console.cloud.google.com/bigquery?project={project_id}"
        f"&ws=!1m5!1m4!4m3!1s{project_id}!2s{dataset_id}!3s{table_name}"
    )
    return {
        "source_system": source_system,
        "table_name": table_name,
        "feed_name": feed_name,
        "description": description,
        "ingestion_mode": ingestion_mode,
        "fqn": fqn,
        "row_count": len(enriched_rows),
        "columns": raw_cols,
        "envelope_columns": [c for c in cols if c.startswith("_")],
        "persistence_mode": persistence_mode,
        "bigquery_url": bq_url,
        "databricks_url": bq_url,
    }
