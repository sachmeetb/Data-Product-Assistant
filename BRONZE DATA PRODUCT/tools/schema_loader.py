"""
schema_loader.py — Common Block Catalog loader for BFSI-Bronze-Agent.

Architecture: common/ blocks are the single source of truth.
  - Every Bronze table is built by assembling common blocks as column primitives.
  - The standards-crosswalk.csv attaches standard mappings.

Common blocks loaded:
  ingestion-metadata  source-identifier  file-metadata  raw-payload
  quality-flags       lineage-tracking   temporal

Flattening strategy:
  - Simple scalar properties (string, integer, number, boolean) → individual BQ columns
  - Nested objects → flattened into individual columns with a logical prefix
  - Repeated / array properties → JSON type in BigQuery
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_AGENT_ROOT     = Path(__file__).parent.parent          # BFSI-Bronze-Agent/
_COMMON_DIR     = _AGENT_ROOT / "common"                # BFSI-Bronze-Agent/common/
_MAPPINGS_DIR   = _AGENT_ROOT / "mappings"              # BFSI-Bronze-Agent/mappings/

_cache: dict = {}


# ── JSON Schema type → BigQuery type ─────────────────────────────────────────

def _bq_type(prop: dict, name: str = "") -> str:
    """
    Resolve a JSON Schema property definition to a BigQuery column type.
    Handles nullable unions like [type, "null"] correctly.
    """
    raw = prop.get("type", "string")
    types = [t for t in (raw if isinstance(raw, list) else [raw]) if t != "null"]
    base_type = types[0] if types else "string"
    fmt = prop.get("format", "")
    pattern = prop.get("pattern", "")

    if base_type == "object" or base_type == "array":
        return "JSON"
    if base_type == "boolean":
        return "BOOL"
    if base_type == "integer":
        return "INT64"
    if base_type == "number":
        return "NUMERIC"
    if base_type == "string":
        if fmt in ("date-time",):
            return "TIMESTAMP"
        if fmt == "date":
            return "DATE"
        if pattern and pattern.startswith("^-?[0-9]+"):
            return "NUMERIC"
        return "STRING"
    return "STRING"


def _is_nullable(prop: dict) -> bool:
    raw = prop.get("type", "string")
    if isinstance(raw, list):
        return "null" in raw
    return False


def _bq_mode(prop_name: str, required_list: list[str], prop: dict) -> str:
    if prop_name in required_list:
        return "REQUIRED"
    return "NULLABLE"


# ── Flatten a block's properties into column defs ─────────────────────────────

def _flatten_properties(
    props: dict,
    required: list[str],
    defs: dict,
    prefix: str = "",
) -> list[dict]:
    """
    Recursively flatten JSON Schema properties into a list of BigQuery column defs.
    """
    columns = []
    for name, prop in props.items():
        col_name = f"{prefix}{name}" if prefix else name

        # Resolve $ref to $defs
        if "$ref" in prop:
            ref_key = prop["$ref"].split("/")[-1]
            if ref_key in defs:
                sub = defs[ref_key]
                sub_props = sub.get("properties", {})
                sub_required = sub.get("required", [])
                nested = _flatten_properties(
                    sub_props, sub_required, defs, prefix=f"{col_name}_"
                )
                for c in nested:
                    if c["mode"] == "REQUIRED":
                        c["mode"] = "NULLABLE"
                columns.extend(nested)
                continue

        raw_type = prop.get("type", "string")
        types = [t for t in (raw_type if isinstance(raw_type, list) else [raw_type]) if t != "null"]
        base_type = types[0] if types else "string"

        # Nested object without $ref — represent as JSON
        if base_type == "object":
            columns.append({
                "name": col_name,
                "bq_type": "JSON",
                "mode": "NULLABLE",
                "description": prop.get("description", f"Nested {col_name} structure (JSON)"),
            })
            continue

        bq_t = _bq_type(prop, col_name)
        mode = _bq_mode(name, required, prop)

        columns.append({
            "name": col_name,
            "bq_type": bq_t,
            "mode": mode,
            "description": prop.get("description", ""),
            "enum_values": prop.get("enum", []),
            "pattern": prop.get("pattern", ""),
        })

    return columns


# ── ingestion-metadata special flattening ────────────────────────────────────

def _flatten_ingestion_metadata(schema: dict) -> list[dict]:
    """
    Mandatory Ingestion Metadata Envelope for Bronze layer.
    Directly aligns with Tony D. Giordano's 4-field core envelope:
      - ingest_batch_id
      - ingest_ts
      - source_file_name
      - raw_payload_hash (SHA-256)
    Plus unique row landing key (bronze_id) and source_system.
    """
    return [
        {
            "name": "bronze_id",
            "bq_type": "STRING",
            "mode": "REQUIRED",
            "description": "Unique deterministic identifier (UUID) for this Bronze landing row.",
            "block": "ingestion-metadata",
        },
        {
            "name": "ingest_batch_id",
            "bq_type": "STRING",
            "mode": "REQUIRED",
            "description": "Unique ingestion batch / job execution identifier.",
            "block": "ingestion-metadata",
        },
        {
            "name": "ingest_ts",
            "bq_type": "TIMESTAMP",
            "mode": "REQUIRED",
            "description": "UTC timestamp when the record landed in Bronze storage.",
            "block": "ingestion-metadata",
        },
        {
            "name": "source_file_name",
            "bq_type": "STRING",
            "mode": "NULLABLE",
            "description": "Originating source file name, GCS object URI, or Kafka topic.",
            "block": "ingestion-metadata",
        },
        {
            "name": "raw_payload_hash",
            "bq_type": "STRING",
            "mode": "REQUIRED",
            "description": "SHA-256 cryptographic hash of the raw payload for audit and deduplication.",
            "block": "ingestion-metadata",
        },
        {
            "name": "source_system",
            "bq_type": "STRING",
            "mode": "REQUIRED",
            "description": "Originating source system identifier (e.g. HOGAN, SAP, SFDC, T24, SWIFT, FPS).",
            "block": "ingestion-metadata",
        },
    ]


# ── Load common blocks ────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_common_block(yaml_file: Path, crosswalk_map: dict) -> dict:
    """Load one common YAML block and resolve it to a fully typed column spec."""
    schema = _load_yaml(yaml_file)
    block_name = yaml_file.stem  # e.g. "raw-payload", "ingestion-metadata"

    if block_name == "ingestion-metadata":
        columns = _flatten_ingestion_metadata(schema)
    elif "columns" in schema and isinstance(schema["columns"], list):
        columns = [dict(c) for c in schema["columns"]]
    else:
        props    = schema.get("properties", {})
        required = schema.get("required", [])
        defs     = schema.get("$defs", {})
        columns  = _flatten_properties(props, required, defs)

    for col in columns:
        col.setdefault("block", block_name)

    xwalk_key = f"common/{block_name}"
    standards = crosswalk_map.get(xwalk_key, {})

    return {
        "block_name": block_name,
        "block_id":   schema.get("$id", ""),
        "title":      schema.get("title", block_name),
        "description": schema.get("description", "").strip(),
        "columns": columns,
        "standards_alignment": {
            "iso_20022": standards.get("iso_20022", ""),
            "fdx":       standards.get("fdx_open_finance", ""),
            "fibo":      standards.get("fibo", ""),
            "bian":      standards.get("bian", ""),
            "other":     standards.get("other_standards", ""),
        },
        "usage_note": _usage_note(block_name),
    }


def _usage_note(name: str) -> str:
    notes = {
        "ingestion-metadata": "Embed in EVERY Bronze table. Tracks ingest_batch_id, ingest_ts, source_file_name, and raw_payload_hash.",
        "source-identifier": "Tracks natural keys, customer numbers, and entity IDs from source systems.",
        "file-metadata": "Used for file-based ingestion sources (e.g. CSV, JSON, XML, Parquet).",
        "raw-payload": "Stores the entire unprocessed raw message, row, or JSON string.",
        "quality-flags": "Technical DQ indicators at Bronze level (parse_error_flag, malformed_record_flag).",
        "lineage-tracking": "Traces back to upstream source paths, message offsets, or Kafka partitions.",
        "temporal": "Business or extraction time extracted directly from raw data.",
        "mainframe-copybook": "Used for Hogan and mainframe fixed-width/EBCDIC records with COBOL copybook layouts.",
        "swift-envelope": "Used for SWIFT MT (MT103/940), SWIFT MX (pacs.008/camt.053), and Faster Payments (FPS) streams.",
        "sap-idoc-metadata": "Used for SAP ERP, S/4HANA, FI-CO General Ledger, IDoc, and SLT replication.",
        "api-crm-metadata": "Used for Salesforce CRM REST API, Bulk API, and Change Data Capture (CDC) events.",
        "unstructured-object-metadata": "Used for BigLake Object Tables landing PDFs, KYC scans, TIFFs, audio transcripts, and loan contracts.",
    }
    return notes.get(name, "")


# ── Source Type → blocks map ───────────────────────────────────────────────────────

_DOMAIN_BLOCK_MAP: dict[str, list[str]] = {
    # Default: every table must start with ingestion-metadata and raw-payload
    "_all": ["ingestion-metadata", "raw-payload"],

    # Core source systems from Tony D. Giordano discussion
    "hogan":              ["ingestion-metadata", "mainframe-copybook", "quality-flags", "temporal"],
    "sap":                ["ingestion-metadata", "sap-idoc-metadata", "quality-flags", "temporal"],
    "salesforce":         ["ingestion-metadata", "api-crm-metadata", "temporal", "quality-flags"],
    "temenos":            ["ingestion-metadata", "source-identifier", "raw-payload", "file-metadata"],
    "swift":              ["ingestion-metadata", "swift-envelope", "quality-flags", "temporal"],
    "faster_payments":    ["ingestion-metadata", "swift-envelope", "lineage-tracking", "temporal"],
    "csv":                ["ingestion-metadata", "file-metadata", "raw-payload", "quality-flags"],

    # Multi-modal ingestion categories
    "unstructured":       ["ingestion-metadata", "unstructured-object-metadata", "quality-flags", "temporal"],
    "unstructured_docs":  ["ingestion-metadata", "unstructured-object-metadata", "quality-flags", "temporal"],
    "streaming":          ["ingestion-metadata", "lineage-tracking", "temporal", "quality-flags", "raw-payload"],
    "streaming_fraud":    ["ingestion-metadata", "lineage-tracking", "temporal", "quality-flags", "raw-payload"],
    "batch":              ["ingestion-metadata", "file-metadata", "raw-payload", "temporal"],

    # Generic technical source types
    "file":               ["ingestion-metadata", "file-metadata", "raw-payload", "quality-flags"],
    "api":                ["ingestion-metadata", "source-identifier", "raw-payload", "quality-flags"],
    "database_cdc":       ["ingestion-metadata", "source-identifier", "lineage-tracking", "raw-payload"],
    "core_banking":       ["ingestion-metadata", "source-identifier", "raw-payload", "lineage-tracking"],
    "payment_gateway":    ["ingestion-metadata", "source-identifier", "temporal", "raw-payload"],
    "crm":                ["ingestion-metadata", "api-crm-metadata", "temporal", "quality-flags"],
}


def classify_source_modality(source_name: str, hint: str = "") -> dict[str, str]:
    """
    Classify incoming requirement or feed into the 4-quadrant Lakehouse matrix:
      Structure: STRUCTURED | UNSTRUCTURED | SEMI_STRUCTURED
      Cadence: STREAMING_REALTIME | BATCH_SCHEDULED | MICRO_BATCH
    """
    s_lower = f"{source_name} {hint}".lower()
    
    # 1. Structure Detection
    if any(k in s_lower for k in ["pdf", "tiff", "jpeg", "png", "document", "scan", "passport", "id card", "audio", "contract", "deed", "unstructured"]):
        structure = "UNSTRUCTURED"
    elif any(k in s_lower for k in ["xml", "json", "swift mx", "pacs", "camt", "pain", "semi-structured"]):
        structure = "SEMI_STRUCTURED"
    else:
        structure = "STRUCTURED"
        
    # 2. Cadence Detection
    if any(k in s_lower for k in ["kafka", "pubsub", "pub/sub", "streaming", "real-time", "realtime", "event-driven", "cdc", "fps", "faster payment", "sub-minute"]):
        cadence = "STREAMING_REALTIME"
    else:
        cadence = "BATCH_SCHEDULED"
        
    return {
        "structure": structure,
        "cadence": cadence,
        "modality_label": f"[{cadence.replace('_', ' ')} · {structure.replace('_', ' ')}]",
    }



def get_domain_block_map() -> dict[str, list[str]]:
    """Return the source-type → recommended blocks mapping."""
    return _DOMAIN_BLOCK_MAP


# Alias for Bronze layer source scoping and product engine
get_source_block_map = get_domain_block_map


# ── Public API ────────────────────────────────────────────────────────────────

def _load_crosswalk_map() -> dict[str, dict]:
    """Load standards-crosswalk.csv as {bronze_component: row_dict}."""
    path = _MAPPINGS_DIR / "standards-crosswalk.csv"
    if not path.is_file():
        log.warning("standards-crosswalk.csv not found at %s", path)
        return {}
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {row["bronze_component"]: row for row in rows if "bronze_component" in row}


def get_common_block_catalog() -> dict[str, dict]:
    """
    Load and cache Bronze common blocks.
    """
    if "common_catalog" in _cache:
        return _cache["common_catalog"]

    if not _COMMON_DIR.is_dir():
        log.error("Common directory not found: %s", _COMMON_DIR)
        return {}

    crosswalk_map = _load_crosswalk_map()
    catalog: dict[str, dict] = {}

    for yaml_file in sorted(_COMMON_DIR.glob("*.yaml")):
        try:
            block = _load_common_block(yaml_file, crosswalk_map)
            catalog[yaml_file.stem] = block
        except Exception as exc:
            log.warning("Could not load %s: %s", yaml_file, exc)

    log.info("Common block catalog loaded: %d blocks (%s)", len(catalog), list(catalog.keys()))
    _cache["common_catalog"] = catalog
    return catalog


def get_common_block(name: str) -> Optional[dict]:
    """Return a single common block definition by name, or None."""
    return get_common_block_catalog().get(name)


def get_crosswalk() -> list[dict]:
    """Return the full standards crosswalk as a list of dicts."""
    if "crosswalk" in _cache:
        return _cache["crosswalk"]
    crosswalk_map = _load_crosswalk_map()
    rows = list(crosswalk_map.values())
    _cache["crosswalk"] = rows
    return rows


def get_blocks_for_table(source_type: str) -> list[dict]:
    """
    Return the recommended common block definitions for a given source type.
    Falls back to defaults if source_type is not in the map.
    """
    catalog = get_common_block_catalog()
    block_names = _DOMAIN_BLOCK_MAP.get(source_type, _DOMAIN_BLOCK_MAP["_all"])
    return [catalog[n] for n in block_names if n in catalog]


def get_source_systems_catalog() -> dict:
    """Return the source systems and banking standards catalog."""
    if "source_systems_catalog" in _cache:
        return _cache["source_systems_catalog"]
    
    path = _AGENT_ROOT / "knowledge" / "source_systems_catalog.json"
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                _cache["source_systems_catalog"] = data
                return data
        except Exception as exc:
            log.warning("Could not load source_systems_catalog.json: %s", exc)
    return {}


def catalog_to_agent_context() -> str:
    """
    Compact but complete JSON string of the common block catalog for agent prompt injection.
    """
    catalog = get_common_block_catalog()
    return json.dumps(
        {
            "common_block_catalog": catalog,
            "domain_block_map": _DOMAIN_BLOCK_MAP,
            "standards_crosswalk": get_crosswalk(),
            "source_systems_catalog": get_source_systems_catalog().get("source_systems", {}),
        },
        indent=2,
        default=str,
    )
