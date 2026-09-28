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
    ingestion-metadata block flattening for Bronze layer.
    """
    defs = schema.get("$defs", {})
    columns = []

    columns.append({
        "name": "bronze_id",
        "bq_type": "STRING",
        "mode": "REQUIRED",
        "description": "Unique identifier for this raw record ingestion.",
        "block": "ingestion-metadata",
    })

    ingest_map = {
        "ingest_ts": ("TIMESTAMP", "REQUIRED", "Timestamp when the record landed in Bronze."),
        "source_system": ("STRING", "REQUIRED", "Name of the source system."),
        "source_file_name": ("STRING", "NULLABLE", "Originating file name if applicable."),
        "batch_id": ("STRING", "NULLABLE", "Batch identifier for the load."),
    }
    for col, (bq_t, mode, desc) in ingest_map.items():
        columns.append({"name": col, "bq_type": bq_t, "mode": mode, "description": desc, "block": "ingestion-metadata"})

    return columns


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
        "ingestion-metadata": "Embed in EVERY Bronze table. Tracks load time, batch ID, and source.",
        "source-identifier": "Tracks natural keys from source systems.",
        "file-metadata": "Used for file-based ingestion sources (e.g. CSV, JSON, XML).",
        "raw-payload": "Stores the entire unprocessed raw message or record (e.g., JSON string).",
        "quality-flags": "Basic DQ indicators at the Bronze level (e.g. schema validation passed).",
        "lineage-tracking": "Traces back to upstream source paths or message offsets.",
        "temporal": "Business or system time extracted directly from raw data.",
    }
    return notes.get(name, "")


# ── Source Type → blocks map ───────────────────────────────────────────────────────

_DOMAIN_BLOCK_MAP: dict[str, list[str]] = {
    # Default: every table must start with ingestion-metadata and raw-payload
    "_all": ["ingestion-metadata", "raw-payload"],

    # Source type recommendations
    "file":               ["ingestion-metadata", "file-metadata", "raw-payload", "quality-flags"],
    "api":                ["ingestion-metadata", "source-identifier", "raw-payload", "quality-flags"],
    "streaming":          ["ingestion-metadata", "lineage-tracking", "temporal", "raw-payload"],
    "database_cdc":       ["ingestion-metadata", "source-identifier", "lineage-tracking", "raw-payload"],
    
    # Banking specific raw sources
    "core_banking":       ["ingestion-metadata", "source-identifier", "raw-payload", "lineage-tracking"],
    "payment_gateway":    ["ingestion-metadata", "source-identifier", "temporal", "raw-payload"],
    "crm":                ["ingestion-metadata", "source-identifier", "raw-payload"],
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
        },
        indent=2,
        default=str,
    )
