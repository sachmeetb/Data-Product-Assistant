"""
schema_loader.py — Common Block Catalog loader for BFSI-Silver-Agent.

Architecture: common/ blocks are the single source of truth.
  - entities/ is NOT used — agents compose tables from common blocks.
  - Every Silver table is built by assembling common blocks as column primitives.
  - The standards-crosswalk.csv attaches ISO 20022 / FDX / FIBO / BIAN alignment
    to every block so agents produce standards-cited DDL.

Common blocks loaded:
  technical-metadata  identifier     money          code-value
  postal-address      party-name     contact-point  temporal
  rate                quantity

Flattening strategy:
  - Simple scalar properties (string, integer, number, boolean) → individual BQ columns
  - $defs nested objects (e.g. Lineage, Versioning, DataQuality in technical-metadata)
    → flattened into individual columns with a logical prefix
  - Repeated / array properties → JSON type in BigQuery
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_AGENT_ROOT     = Path(__file__).parent.parent          # BFSI-Silver-Agent/
_COMMON_DIR     = _AGENT_ROOT / "common"                # BFSI-Silver-Agent/common/
_MAPPINGS_DIR   = _AGENT_ROOT / "mappings"              # BFSI-Silver-Agent/mappings/

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
        # Exact-decimal pattern signals money/rate amounts → NUMERIC in BigQuery
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

    Args:
        props:    JSON Schema properties dict.
        required: List of required property names at this level.
        defs:     $defs from the root schema for resolving $ref.
        prefix:   Column name prefix for nested blocks.

    Returns:
        List of column definition dicts with:
          name, bq_type, mode, description, standard_note (if any).
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
                # Flatten nested object with name as prefix
                nested = _flatten_properties(
                    sub_props, sub_required, defs, prefix=f"{col_name}_"
                )
                for c in nested:
                    if c["mode"] == "REQUIRED":
                        c["mode"] = "NULLABLE"   # nested required → nullable at column level
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


# ── technical-metadata special flattening ────────────────────────────────────

def _flatten_technical_metadata(schema: dict) -> list[dict]:
    """
    technical-metadata uses $defs (Lineage, Versioning, DataQuality).
    Flatten each $def into concrete Silver columns with clear naming.
    """
    defs = schema.get("$defs", {})
    columns = []

    # surrogate_key — top-level
    columns.append({
        "name": "surrogate_key",
        "bq_type": "STRING",
        "mode": "REQUIRED",
        "description": "Stable, system-generated primary key for this Silver entity instance (UUID or hash).",
        "block": "technical-metadata / surrogate_key",
    })

    # Lineage block
    lineage_map = {
        "source_system":         ("STRING",    "REQUIRED", "Canonical code of the originating system (e.g. CORE_BANKING, CRM, CARD_SWITCH)."),
        "source_record_id":      ("STRING",    "REQUIRED", "Natural/business key of the record in the source system."),
        "source_extract_ts":     ("TIMESTAMP", "NULLABLE", "When the record was extracted from source (event time at Bronze boundary)."),
        "bronze_ingest_ts":      ("TIMESTAMP", "NULLABLE", "When the raw record landed in the Bronze layer."),
        "silver_load_ts":        ("TIMESTAMP", "REQUIRED", "When this conformed record was written to the Silver layer."),
        "source_file":           ("STRING",    "NULLABLE", "Bronze file / topic / offset for replay traceability."),
        "pipeline_run_id":       ("STRING",    "NULLABLE", "Orchestration run identifier (e.g. Dataflow / Airflow run ID)."),
        "source_schema_version": ("STRING",    "NULLABLE", "Version of the source contract (e.g. 'ISO20022:pain.001.001.09', 'FDX:6.4')."),
    }
    for col, (bq_t, mode, desc) in lineage_map.items():
        columns.append({"name": col, "bq_type": bq_t, "mode": mode, "description": desc, "block": "technical-metadata / Lineage"})

    # Versioning block (SCD Type-2)
    versioning_map = {
        "effective_from_ts": ("TIMESTAMP", "REQUIRED", "Start of the validity window for this version (SCD Type-2)."),
        "effective_to_ts":   ("TIMESTAMP", "NULLABLE", "End of the validity window; NULL for the current/open version."),
        "is_current":        ("BOOL",      "REQUIRED", "True for the currently active version of this record."),
        "version_number":    ("INT64",     "NULLABLE", "Monotonically increasing version counter starting at 1."),
        "is_deleted":        ("BOOL",      "NULLABLE", "Soft-delete tombstone propagated from source deletes."),
        "record_hash":       ("STRING",    "REQUIRED", "Deterministic hash of all business columns for change detection (CDC)."),
    }
    for col, (bq_t, mode, desc) in versioning_map.items():
        columns.append({"name": col, "bq_type": bq_t, "mode": mode, "description": desc, "block": "technical-metadata / Versioning"})

    # DataQuality block
    dq_map = {
        "dq_status":    ("STRING",  "REQUIRED", "PASS = conformed; WARN = usable with issues; QUARANTINE = held from Gold."),
        "dq_score":     ("NUMERIC", "NULLABLE", "Completeness/accuracy score 0.0–1.0 from Silver DQ rules."),
        "failed_rules": ("JSON",    "NULLABLE", "JSON array of rule codes that failed (e.g. [\"IBAN_CHECKSUM\",\"CCY_NOT_ISO4217\"])."),
        "is_enriched":  ("BOOL",    "NULLABLE", "True if reference-data enrichment (LEI lookup, MCC decode) was applied."),
    }
    for col, (bq_t, mode, desc) in dq_map.items():
        columns.append({"name": col, "bq_type": bq_t, "mode": mode, "description": desc, "block": "technical-metadata / DataQuality"})

    return columns


# ── Load common blocks ────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_common_block(yaml_file: Path, crosswalk_map: dict) -> dict:
    """Load one common YAML block and resolve it to a fully typed column spec."""
    schema = _load_yaml(yaml_file)
    block_name = yaml_file.stem  # e.g. "money", "identifier"

    # Special handling for technical-metadata (uses $defs, not flat properties)
    if block_name == "technical-metadata":
        columns = _flatten_technical_metadata(schema)
    else:
        props    = schema.get("properties", {})
        required = schema.get("required", [])
        defs     = schema.get("$defs", {})
        columns  = _flatten_properties(props, required, defs)

    # Attach block source reference to every column
    for col in columns:
        col.setdefault("block", block_name)

    # Standards crosswalk alignment
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
        "technical-metadata": "Embed in EVERY Silver table. Provides the full SCD Type-2 envelope, lineage, and DQ tracking.",
        "money":    "Use for any monetary amount. Prefix columns: e.g. principal_amount NUMERIC, principal_currency STRING.",
        "identifier": "Use for any industry identifier (IBAN, LEI, ISIN, BIC, etc.). Prefix: e.g. party_lei_id_scheme STRING, party_lei_id_value STRING.",
        "code-value": "Use for any controlled enumeration that must preserve source code. e.g. account_type_code STRING, account_type_code_list STRING.",
        "postal-address": "Use for structured postal addresses. ISO 20022 PostalAddress24 aligned. Prefix: e.g. registered_address_street_name.",
        "party-name": "Use for party name fields. Supports both natural persons and organisations.",
        "contact-point": "Use for email, phone, web. Repeat as ARRAY<STRUCT> or JSON for multiple contacts.",
        "temporal": "Use for date ranges (from_date/to_date) and durations (period_unit/period_count).",
        "rate": "Use for interest rates, FX rates, yields. rate_value is NUMERIC (exact decimal).",
        "quantity": "Use for share quantities, notional amounts, units. qty_value is NUMERIC.",
    }
    return notes.get(name, "")


# ── Domain → blocks map ───────────────────────────────────────────────────────

_DOMAIN_BLOCK_MAP: dict[str, list[str]] = {
    # Default: every table must start with technical-metadata
    "_all": ["technical-metadata"],

    # Table-level block recommendations
    "party":               ["technical-metadata", "party-name", "identifier", "postal-address", "contact-point", "code-value"],
    "account":             ["technical-metadata", "identifier", "money", "code-value"],
    "transaction":         ["technical-metadata", "money", "code-value", "temporal", "identifier"],
    "payment":             ["technical-metadata", "money", "identifier", "code-value", "temporal"],
    "loan":                ["technical-metadata", "money", "rate", "temporal", "code-value", "identifier"],
    "balance":             ["technical-metadata", "money", "code-value", "temporal"],
    "position-holding":    ["technical-metadata", "quantity", "money", "code-value", "temporal", "identifier"],
    "financial-instrument":["technical-metadata", "identifier", "code-value", "money", "quantity"],
    "agreement":           ["technical-metadata", "temporal", "money", "code-value", "identifier"],
    "product":             ["technical-metadata", "code-value", "identifier"],
    "party-relationship":  ["technical-metadata", "code-value", "temporal"],

    # Banking domain → suggested blocks
    "retail_banking":    ["technical-metadata", "party-name", "identifier", "money", "code-value"],
    "payments":          ["technical-metadata", "money", "identifier", "code-value", "temporal"],
    "deposits":          ["technical-metadata", "money", "code-value", "temporal", "identifier"],
    "lending":           ["technical-metadata", "money", "rate", "temporal", "code-value", "identifier"],
    "cards":             ["technical-metadata", "money", "code-value", "identifier", "temporal"],
    "trade_finance":     ["technical-metadata", "money", "code-value", "identifier", "temporal"],
    "cash_management":   ["technical-metadata", "money", "code-value", "temporal"],
    "investment_banking":["technical-metadata", "quantity", "money", "identifier", "code-value"],
    "wealth_management": ["technical-metadata", "quantity", "money", "identifier", "code-value", "temporal"],
    "aml_kyc":           ["technical-metadata", "code-value", "temporal", "identifier"],
    "credit_risk":       ["technical-metadata", "money", "rate", "code-value", "temporal"],
    "compliance_risk":   ["technical-metadata", "code-value", "temporal", "identifier"],
}


def get_domain_block_map() -> dict[str, list[str]]:
    """Return the domain/table-type → common blocks mapping."""
    return _DOMAIN_BLOCK_MAP


# ── Public API ────────────────────────────────────────────────────────────────

def _load_crosswalk_map() -> dict[str, dict]:
    """Load standards-crosswalk.csv as {silver_component: row_dict}."""
    path = _MAPPINGS_DIR / "standards-crosswalk.csv"
    if not path.is_file():
        log.warning("standards-crosswalk.csv not found at %s", path)
        return {}
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {row["silver_component"]: row for row in rows}


def get_common_block_catalog() -> dict[str, dict]:
    """
    Load and cache all 10 common blocks with full column specs, BQ types, and standards alignment.

    Returns:
        {
          "money":       { block_name, title, description, columns: [{name, bq_type, mode, description}], standards_alignment, usage_note },
          "identifier":  { ... },
          ...
        }
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


def get_blocks_for_table(table_type: str) -> list[dict]:
    """
    Return the recommended common block definitions for a given table type.
    Falls back to just technical-metadata if table_type is not in the map.

    Args:
        table_type: e.g. "party", "account", "loan", "payment"

    Returns:
        List of full block definition dicts.
    """
    catalog = get_common_block_catalog()
    block_names = _DOMAIN_BLOCK_MAP.get(table_type, ["technical-metadata"])
    return [catalog[n] for n in block_names if n in catalog]


def catalog_to_agent_context() -> str:
    """
    Compact but complete JSON string of the common block catalog for agent prompt injection.
    Each block includes: title, description, usage_note, columns (with bq_type + mode), standards_alignment.
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
