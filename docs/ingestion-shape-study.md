# Ingestion Shape Study

**Date:** 2026-10-05
**Scope:** BRONZE DATA PRODUCT (deployed) · data-product-assistant-final (unified) · Workbench skills

---

## Current state — ingestion by data shape

Legend: ✅ implemented · 🟡 partial / prompt-only · ❌ absent

| Shape | `BRONZE DATA PRODUCT` (deployed) | `data-product-assistant-final` | Verdict |
|---|---|---|---|
| **Structured** (RDBMS, core-banking, CDC, API, CSV) | ✅ Catalog entries for `hogan`, `sap`, `temenos`. `_DOMAIN_BLOCK_MAP` recipes. Formats: CSV, JSON, Parquet, XML, AVRO. Four patterns: batch, streaming, CDC, API extract. | 🟡 `SourceKind.RELATIONAL`, `EXTERNAL_TABLE`, `UPLOAD` exist. `bronze/ingest/` is empty. | Done in principle. |
| **Semi-structured** (SWIFT MX, ISO 20022, XML/JSON) | 🟡 Dead branch. `classify_source_modality()` can return `SEMI_STRUCTURED` but is never called. No catalog tag, no block-map recipe, no prompt rule for this shape. | ❌ `Format.JSON/JSONL/AVRO/ORC` exist as wire formats. No shape concept. | Largest gap. |
| **Unstructured** (PDF, TIFF, audio, KYC docs) | ✅ `unstructured_docs` catalog entry. `unstructured-object-metadata.yaml` fields: `file_uri`, `mime_type`, `ocr_confidence_score`, `extracted_text_payload`. Target: BigLake Object Table on GCS with Document AI OCR. | ❌ `SourceKind.OBJECT_STORE` exists. No OCR or unstructured handling. | Done in BRONZE only. |
| **Streaming** (Kafka, Pub/Sub, FPS) | ✅ Modelled as a cadence, not a structure. `STREAMING_REALTIME`. Catalog entries for `streaming_fraud` and `faster_payments`. `lineage-tracking` block: `source_topic`, `source_offset`, `kafka_partition`. Target: Iceberg. SLA: 5 minutes. | 🟡 `SourceKind.STREAM`, `watermark_field`, `ScheduleKind.EVENT` exist. `bronze/ingest/` is empty. | Modelled, not built. |

### Important: the two-axis model

Streaming is a **cadence** (`STREAMING_REALTIME`), not a structure value. The BRONZE codebase uses two orthogonal axes:

- **Structure axis:** `STRUCTURED` · `SEMI_STRUCTURED` · `UNSTRUCTURED`
- **Cadence axis:** `STREAMING_REALTIME` · `BATCH_SCHEDULED` · `MICRO_BATCH` (documented but never emitted)

The user-facing "four shapes" framing conflates both axes. Keep them separate in implementation.

### On the "~7 ingestion skills"

The number reconciles in three ways.

- The Workbench repo has 7 discovery/ingestion skills: 5 RDBMS variants (`data-discovery`, `data-discovery-mysql`, `data-discovery-snowflake`, `data-discovery-databricks`, `data-discovery-parquet`) plus `data-export-parquet` and `data-profile-parquet`.
- The deployed BRONZE catalog has 7 distinct `ingestion_pattern` / `cadence` values:
  1. `BATCH_FILE_COPYBOOK` (hogan)
  2. `CDC_OR_BATCH_RFC` (sap)
  3. `STREAMING_CDC_OR_BULK_API` (salesforce)
  4. `BATCH_EXTRACT_OR_EVENT_STREAM` (temenos)
  5. `STREAMING_REALTIME` (faster_payments, streaming_fraud)
  6. `STREAMING_OR_HOURLY_BATCH` (swift)
  7. `BATCH_AND_EVENT_DRIVEN` (unstructured_docs)
- The `-final` roadmap lists 5 connector specs plus `SYNTHETIC` (6 total, unbuilt): OBJECT_STORE, RELATIONAL, EXTERNAL_TABLE, STREAM, UPLOAD, SYNTHETIC.

The clearest forward taxonomy is `SourceKind` in `data-product-assistant-final/pipelines/ir.py`.

---

## Key architectural finding

`data-product-assistant-final` already contains the design for three-assistant integration, edit capability, and Knowledge Catalog integration.

**Three-in-one model.** A single spine holds all three layers: `DataProductDesign → Domain → Table (layer ∈ BRONZE | SILVER | GOLD) → Attribute`. The repo merges GOLD (CPG) and SILVER (BFSI). It does not use three separate agents.

**Edit capability.** `core/catalog/reconcile.py` is the edit engine. It produces a `Delta` of addable columns, tables, domains, and type mismatches. `core/model/provenance.py` marks each value as `HUMAN` or `LIVE_CATALOG`. Regeneration does not overwrite human edits. `core/artifacts/graph.py` propagates staleness to downstream items when a value changes.

**Google Knowledge Catalog.** `core/publish/knowledge_catalog.py` calls the Dataplex API (`dataplex.googleapis.com/v1`). Bronze tables are never published (`PUBLISHED_LAYERS = (Layer.SILVER, Layer.GOLD)`). The live publish path is implemented as of 2026-10-05.

**Datalake.** The codebase does not use the word "datalake." Object storage (GCS, S3, ADLS) maps to `SourceKind.OBJECT_STORE`. The primary target engine is BigQuery. Planned renderers add Dataform, Airflow, dbt, and Spark in that order.

**Workflow call site.** `KnowledgeCatalogClient.apply()` is not yet wired into any pipeline or API endpoint. The natural trigger point is after `BigQueryPublisher.publish()` returns `publish_status: "published"`. This is an open item on the roadmap (`docs/06-roadmap.md`, Phase 8).

---

## Gap backlog (prioritized)

**P0 — Ingestion core**

1. Implement `bronze/ingest/`. The directory exists but contains only a module docstring. The required flow is: register → profile → land → quarantine → register into catalog. Build one handler for each `SourceKind` value.
2. Call the modality classifier. `classify_source_modality()` in `BRONZE DATA PRODUCT/tools/schema_loader.py` exists but is never called. Port it into `-final` and route each shape to the correct handler.

**P1 — Close the shape gaps**

3. **Semi-structured.** Add `data_structure: SEMI_STRUCTURED` to the SWIFT MX and ISO 20022 entries in `source_systems_catalog.json`. Add a `semi_structured` recipe to `_DOMAIN_BLOCK_MAP`. Add a selection rule and target format (shredded JSON / VARIANT-style) to `source_scoping.md`.
4. **Streaming.** Wire `SourceKind.STREAM` to an Iceberg / Pub/Sub landing path using BRONZE's `lineage-tracking` block (`source_topic`, `source_offset`, `kafka_partition`). Decide whether to produce `MICRO_BATCH` cadence — it is documented in `classify_source_modality()` but never returned.

**P2 — Integration seams**

5. Wire `KnowledgeCatalogClient.apply(dry_run=False)` after `BigQueryPublisher.publish()` succeeds for silver/gold layers. Bronze stays internal (enforced by `PUBLISHED_LAYERS`).
6. Confirm `_BRONZE` envelope columns (`bronze_ingest_ts`, `source_extract_ts`, `source_system_id`, `source_record_id`, `dq_status`, `dq_failed_rules`, `ingest_batch_id`) are marked `Origin.LIVE_CATALOG` and locked. Verify staleness propagation in `core/artifacts/graph.py` covers ingested bronze columns.

**P3 — Consistency fixes**

7. Fix the `streaming_fraud` catalog entry. It carries `data_structure: "STRUCTURED"` but is the primary streaming example. Add a comment that states streaming is a cadence value, not a structure value.
8. Update `supported_file_formats` in `BRONZE DATA PRODUCT/agents/source_scoping_agent.py`. The current list is `["CSV", "JSON", "Parquet", "XML", "AVRO"]`. Add PDF, TIFF, audio (WAV), and SWIFT FIN to match the shapes the agent claims to handle.

---

## GCP / IAM reference

| Setting | Value |
|---|---|
| Project | `eogwapq-agbg-internal-data-mig` |
| Region | `us-central1` |
| Runtime SA | `dp-assistant-sa@eogwapq-agbg-internal-data-mig.iam.gserviceaccount.com` |
| Proxy SA | `auth-proxy-sa@eogwapq-agbg-internal-data-mig.iam.gserviceaccount.com` |
| Bronze dataset | `banking_bronze` |
| KC publish layers | SILVER, GOLD (bronze never published) |
| KC API root | `https://dataplex.googleapis.com/v1` |
