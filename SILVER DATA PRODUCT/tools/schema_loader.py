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


# ── Multi-Industry Catalog Accessors ─────────────────────────────────────────

_MULTI_INDUSTRY_FILE = _AGENT_ROOT / "knowledge" / "multi_industry_catalog.json"


def get_multi_industry_catalog() -> dict:
    """Load and return the multi-industry catalog."""
    if "multi_industry_catalog" in _cache:
        return _cache["multi_industry_catalog"]
    if _MULTI_INDUSTRY_FILE.exists():
        try:
            with open(_MULTI_INDUSTRY_FILE, encoding="utf-8") as f:
                data = json.load(f)
                _cache["multi_industry_catalog"] = data
                return data
        except Exception as exc:
            log.warning("Could not load %s: %s", _MULTI_INDUSTRY_FILE, exc)
    return {"industries": []}


def get_industries() -> list[dict]:
    """Return summary list of all available industries."""
    data = get_multi_industry_catalog()
    return [
        {
            "id": ind["id"],
            "name": ind["name"],
            "icon": ind.get("icon", "🏢"),
            "description": ind.get("description", ""),
            "subtypes": ind.get("subtypes", []),
            "domain_count": len(ind.get("domains", [])),
        }
        for ind in data.get("industries", [])
    ]


def get_industry(industry_id: str) -> Optional[dict]:
    """Return full industry object by ID."""
    data = get_multi_industry_catalog()
    for ind in data.get("industries", []):
        if ind["id"].lower() == industry_id.lower():
            return ind
    return None


def _normalize_entity_list(entities_raw: dict | list) -> list[dict]:
    """Convert entities dict or list into a normalized list of entity dicts for UI & API consumers."""
    result = []
    if isinstance(entities_raw, dict):
        items = list(entities_raw.items())
    elif isinstance(entities_raw, list):
        items = [(e.get("table_name") or e.get("name", "slv_entity"), e) for e in entities_raw]
    else:
        items = []

    for table_name, edef in items:
        cols_raw = edef.get("columns", [])
        norm_cols = []
        for c in cols_raw:
            col_name = c.get("name") or c.get("column_name", "id")
            dtype = c.get("data_type") or c.get("type", "STRING")
            is_pk = bool(c.get("is_pk") or c.get("is_primary_key") or c.get("primary_key"))
            is_pii = bool(c.get("pii") or c.get("is_pii"))
            fk_ref = c.get("fk_ref") or c.get("references")
            norm_cols.append({
                "name": col_name,
                "column_name": col_name,
                "data_type": str(dtype).upper(),
                "type": str(dtype).upper(),
                "is_pk": is_pk,
                "is_primary_key": is_pk,
                "primary_key": is_pk,
                "pii": is_pii,
                "is_pii": is_pii,
                "nullable": bool(c.get("nullable", not is_pk)),
                "fk_ref": fk_ref,
                "references": fk_ref,
                "description": c.get("description", f"Conformed attribute {col_name}"),
            })
        result.append({
            "table_name": table_name,
            "name": table_name,
            "type": edef.get("type", "dimension"),
            "entity_type": edef.get("type", "dimension"),
            "grain": edef.get("grain", "one row per entity record"),
            "description": edef.get("description", f"Conformed Silver entity {table_name}"),
            "column_count": len(norm_cols),
            "columns": norm_cols,
        })
    return result


def _enrich_domain_dict(dom: dict, industry_id: str = "", industry_name: str = "") -> dict:
    """Return a copy of domain with both dict-style `entities_map` and normalized list `entities_list`."""
    dom_copy = dict(dom)
    raw_entities = dom.get("entities", {})
    norm_list = _normalize_entity_list(raw_entities)
    dom_id = dom.get("id") or dom.get("name", "")
    dom_label = dom.get("display_name") or dom.get("name", "")
    dom_copy["id"] = dom_id
    dom_copy["domain_id"] = dom_id
    dom_copy["domain_name"] = dom_label
    dom_copy["domain_label"] = dom_label
    dom_copy["entity_count"] = len(norm_list)
    dom_copy["entities_list"] = norm_list
    if isinstance(raw_entities, dict):
        dom_copy["entities_map"] = raw_entities
    else:
        dom_copy["entities_map"] = {e["table_name"]: e for e in norm_list}
    if industry_id:
        dom_copy["industry_id"] = industry_id
    if industry_name:
        dom_copy["industry_name"] = industry_name
    return dom_copy


def get_domains_for_industry(industry_id: str = "banking", normalized: bool = False) -> list[dict]:
    """Return domain list for a specific industry."""
    ind = get_industry(industry_id)
    if not ind:
        return []
    domains = ind.get("domains", [])
    result = []
    for dom in domains:
        enriched = _enrich_domain_dict(dom, ind["id"], ind["name"])
        if normalized:
            enriched["entities"] = enriched["entities_list"]
        result.append(enriched)
    return result


def get_domain_by_name(domain_name: str) -> Optional[dict]:
    """Search all industries for a domain by name or display_name."""
    if not domain_name:
        return None
    q = domain_name.strip().lower()
    data = get_multi_industry_catalog()
    for ind in data.get("industries", []):
        for dom in ind.get("domains", []):
            if (
                dom["name"].lower() == q
                or dom.get("display_name", "").lower() == q
                or q in dom.get("display_name", "").lower()
                or dom["name"].lower() in q
            ):
                return _enrich_domain_dict(dom, ind["id"], ind["name"])
    return None


def get_navigator_tree() -> dict:
    """Return the complete hierarchical tree (Industries -> Subtypes & Domains -> Entities -> Columns)."""
    data = get_multi_industry_catalog()
    industries_out = []
    for ind in data.get("industries", []):
        domains_out = get_domains_for_industry(ind["id"], normalized=True)
        total_entities = sum(len(d.get("entities", [])) for d in domains_out)
        industries_out.append({
            "id": ind["id"],
            "name": ind["name"],
            "icon": ind.get("icon", "🏢"),
            "description": ind.get("description", ""),
            "subtypes": ind.get("subtypes", []),
            "domain_count": len(domains_out),
            "entity_count": total_entities,
            "domains": domains_out,
        })
    return {
        "version": data.get("version", "2.1.0"),
        "industries": industries_out,
        "source_models": get_source_models_catalog(),
    }


def get_source_models_catalog() -> list[dict]:
    """Return available upstream Bronze / External Source models that can be brought into a Silver structure."""
    raw_models = [
        {
            "source_id": "raw_core.party_kyc_screening",
            "source_system": "Core Banking AML/KYC Engine",
            "layer": "BRONZE",
            "industry": "banking",
            "domain": "Core Banking & Accounts",
            "domain_name": "core_banking",
            "target_table_name": "slv_kyc_screening_event",
            "entity_type": "event",
            "grain": "one row per party compliance screening check",
            "description": "AML, Sanctions, PEP, and Adverse Media Screening Results brought from Bronze KYC feed",
            "columns": [
                {"name": "surrogate_key", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Deterministic Screening Surrogate Key"},
                {"name": "screening_id", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "Source KYC Screening Event ID"},
                {"name": "customer_id", "data_type": "STRING", "is_pk": False, "fk_ref": "slv_customer.customer_id", "nullable": False, "description": "Screened Customer Party FK"},
                {"name": "screening_provider", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "WORLD_CHECK, LEXISNEXIS, DOW_JONES"},
                {"name": "sanctions_hit_flag", "data_type": "BOOL", "is_pk": False, "nullable": False, "description": "OFAC/EU/UN Sanctions Match Indicator"},
                {"name": "pep_indicator", "data_type": "BOOL", "is_pk": False, "nullable": False, "description": "Politically Exposed Person Flag"},
                {"name": "risk_score", "data_type": "NUMERIC", "is_pk": False, "nullable": True, "description": "Composite Screening Risk Score (0-100)"},
                {"name": "screened_ts", "data_type": "TIMESTAMP", "is_pk": False, "nullable": False, "description": "Screening Execution Timestamp"}
            ],
        },
        {
            "source_id": "raw_core.overdraft_facility_master",
            "source_system": "Core Credit & Limits Platform",
            "layer": "BRONZE",
            "industry": "banking",
            "domain": "Core Banking & Accounts",
            "domain_name": "core_banking",
            "target_table_name": "slv_overdraft_facility",
            "entity_type": "dimension",
            "grain": "one row per account overdraft limit agreement",
            "description": "Conformed Account Overdraft & Sweep Credit Facility Dimension",
            "columns": [
                {"name": "surrogate_key", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Deterministic Facility Key"},
                {"name": "facility_id", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "Overdraft Line Identifier"},
                {"name": "account_id", "data_type": "STRING", "is_pk": False, "fk_ref": "slv_account.account_id", "nullable": False, "description": "Linked Deposit Account FK"},
                {"name": "approved_limit_amount", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "Authorized Overdraft Limit"},
                {"name": "utilized_amount", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "Currently Drawn Overdraft Balance"},
                {"name": "apr_rate_pct", "data_type": "NUMERIC", "is_pk": False, "nullable": True, "description": "Contractual Annual Percentage Rate"},
                {"name": "facility_status_cd", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "ACTIVE, SUSPENDED, EXPIRED"}
            ],
        },
        {
            "source_id": "raw_core.account_fee_schedule_log",
            "source_system": "Core Billing & Revenue Engine",
            "layer": "BRONZE",
            "industry": "banking",
            "domain": "Core Banking & Accounts",
            "domain_name": "core_banking",
            "target_table_name": "slv_account_fee_assessment",
            "entity_type": "event",
            "grain": "one row per assessed or waived account fee",
            "description": "Conformed Account Fee Assessment, Waiver & Rebate Event Fact",
            "columns": [
                {"name": "surrogate_key", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Deterministic Fee Event Key"},
                {"name": "fee_event_id", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "Billing Assessment Identifier"},
                {"name": "account_id", "data_type": "STRING", "is_pk": False, "fk_ref": "slv_account.account_id", "nullable": False, "description": "Assessed Account FK"},
                {"name": "fee_type_cd", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "MONTHLY_MAINT, NSF_OVERDRAFT, WIRE_FEE, ATM_SURCHARGE"},
                {"name": "assessed_amount", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "Gross Assessed Fee Amount"},
                {"name": "waived_amount", "data_type": "NUMERIC", "is_pk": False, "nullable": True, "description": "Relationship Waiver Amount"},
                {"name": "assessment_date", "data_type": "DATE", "is_pk": False, "nullable": False, "description": "Fee Posting Date"}
            ],
        },
        {
            "source_id": "raw_payments.iso20022_wire_settlement",
            "source_system": "SWIFT / FedNow Payment Gateway",
            "layer": "BRONZE",
            "industry": "banking",
            "domain": "Payments & Transfers (ISO 20022)",
            "domain_name": "payments",
            "target_table_name": "slv_wire_settlement_instruction",
            "entity_type": "event",
            "grain": "one row per wire clearing settlement message",
            "description": "Conformed ISO 20022 pacs.008 Interbank Settlement Instruction",
            "columns": [
                {"name": "surrogate_key", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Deterministic Settlement Key"},
                {"name": "uetr", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "SWIFT Unique End-to-End Transaction Reference"},
                {"name": "debtor_account_id", "data_type": "STRING", "is_pk": False, "fk_ref": "slv_account.account_id", "nullable": False, "description": "Debtor Account FK"},
                {"name": "settlement_amount", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "Interbank Settlement Amount"},
                {"name": "currency_code", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "ISO 4217 Settlement Currency"},
                {"name": "settlement_date", "data_type": "DATE", "is_pk": False, "nullable": False, "description": "Interbank Value Date"}
            ],
        },
        {
            "source_id": "raw_industrial.cip_wash_cycle_telemetry",
            "source_system": "Ecolab 3D TRASAR / CIP PLC Historian",
            "layer": "BRONZE",
            "industry": "energy_industrial",
            "domain": "Water Quality & Telemetry",
            "domain_name": "water_telemetry",
            "target_table_name": "slv_cip_wash_cycle_event",
            "entity_type": "event",
            "grain": "one row per Clean-in-Place sanitation cycle",
            "description": "Conformed Clean-In-Place (CIP) Conductivity, Thermal & Chemical Dosing Cycle",
            "columns": [
                {"name": "surrogate_key", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Deterministic CIP Cycle Key"},
                {"name": "cycle_id", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "CIP Wash Cycle Identifier"},
                {"name": "asset_id", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "Sanitized Equipment Asset ID"},
                {"name": "caustic_conc_pct", "data_type": "NUMERIC", "is_pk": False, "nullable": True, "description": "Measured Caustic Concentration (%)"},
                {"name": "water_saved_gallons", "data_type": "NUMERIC", "is_pk": False, "nullable": True, "description": "Net Water Conservation vs Baseline"},
                {"name": "cycle_end_ts", "data_type": "TIMESTAMP", "is_pk": False, "nullable": False, "description": "Wash Completion Timestamp"}
            ],
        },
    ]
    out = []
    for m in raw_models:
        enriched = dict(m)
        enriched["model_id"] = m["source_id"]
        enriched["source_table"] = m["source_id"]
        enriched["target_silver_table"] = m["target_table_name"]
        enriched["domain_label"] = m["domain"]
        out.append(enriched)
    return out


def parse_external_entity_spec(
    source_type: str,
    payload: str = "",
    entity_name: Optional[str] = None,
    source_model_id: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """
    Parse an external entity from:
      - 'source_catalog': look up from `get_source_models_catalog()` or canonical domain entities
      - 'ddl': parse SQL CREATE TABLE snippet
      - 'odcs': parse ODCS YAML/JSON model specification
    Returns a normalized entity dictionary ready to merge into a Silver contract & product plan.
    """
    import re
    import yaml as _yaml

    st = (source_type or "source_catalog").lower().strip()

    if st == "source_catalog" and source_model_id:
        for sm in get_source_models_catalog():
            if sm["source_id"] == source_model_id or sm["target_table_name"] == source_model_id:
                tname = entity_name or sm["target_table_name"]
                norm = _normalize_entity_list({tname: sm})[0]
                norm["source_table"] = sm["source_id"]
                norm["source_system"] = sm["source_system"]
                return norm
        # Also check canonical domains across all industries
        cat = get_multi_industry_catalog()
        for ind in cat.get("industries", []):
            for dom in ind.get("domains", []):
                ents = dom.get("entities", {})
                if source_model_id in ents:
                    tname = entity_name or source_model_id
                    norm = _normalize_entity_list({tname: ents[source_model_id]})[0]
                    norm["source_table"] = f"raw_bronze.{source_model_id.replace('slv_', 'src_')}"
                    return norm

    if st == "ddl" and payload.strip():
        # Extract table name if present
        tbl_match = re.search(r"TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?([a-zA-Z0-9_.\-]+)[`\"']?", payload, re.IGNORECASE)
        extracted_name = tbl_match.group(1).split(".")[-1] if tbl_match else "slv_imported_entity"
        tname = (entity_name or extracted_name).strip()
        if not tname.startswith("slv_"):
            tname = f"slv_{tname}"

        # Extract parenthesized column block
        paren_match = re.search(r"\((.*)\)", payload, re.DOTALL)
        body = paren_match.group(1) if paren_match else payload
        cols = []
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line or line.startswith("--") or line.upper().startswith(("PRIMARY KEY", "CONSTRAINT", "FOREIGN KEY", "PARTITION", "CLUSTER")):
                continue
            parts = line.split()
            if len(parts) >= 2:
                cname = parts[0].strip("`\"'")
                ctype = re.sub(r"\(.*\)", "", parts[1]).upper()
                if ctype in ("VARCHAR", "CHAR", "TEXT", "UUID"):
                    ctype = "STRING"
                elif ctype in ("DECIMAL", "FLOAT", "DOUBLE", "NUMBER", "NUMERIC"):
                    ctype = "NUMERIC"
                elif ctype in ("INT", "INTEGER", "BIGINT", "SMALLINT", "INT64"):
                    ctype = "INT64"
                elif ctype in ("BOOLEAN", "BOOL"):
                    ctype = "BOOL"
                elif ctype in ("DATETIME", "TIMESTAMP"):
                    ctype = "TIMESTAMP"
                elif ctype not in ("STRING", "NUMERIC", "INT64", "BOOL", "DATE", "TIMESTAMP", "JSON"):
                    ctype = "STRING"

                is_pk = "PRIMARY KEY" in line.upper() or cname == "surrogate_key"
                nullable = "NOT NULL" not in line.upper() and not is_pk
                desc_match = re.search(r"description\s*=\s*['\"]([^'\"]+)['\"]", line, re.IGNORECASE)
                col_desc = desc_match.group(1) if desc_match else f"Imported column {cname}"
                cols.append({
                    "name": cname,
                    "column_name": cname,
                    "data_type": ctype,
                    "type": ctype,
                    "is_pk": is_pk,
                    "is_primary_key": is_pk,
                    "primary_key": is_pk,
                    "pii": any(p in cname.lower() for p in ("name", "ssn", "email", "phone", "iban", "pan")),
                    "nullable": nullable,
                    "description": col_desc,
                })
        if not any(c["name"] == "surrogate_key" for c in cols):
            cols.insert(0, {
                "name": "surrogate_key",
                "column_name": "surrogate_key",
                "data_type": "STRING",
                "type": "STRING",
                "is_pk": True,
                "is_primary_key": True,
                "primary_key": True,
                "pii": False,
                "nullable": False,
                "description": "Deterministic SHA-256 Surrogate Primary Key",
            })
        return {
            "table_name": tname,
            "name": tname,
            "type": "dimension",
            "entity_type": "dimension",
            "grain": "one row per imported source record",
            "description": description or f"Conformed Silver entity imported from SQL DDL ({tname})",
            "column_count": len(cols),
            "columns": cols,
            "source_table": f"raw_bronze.{extracted_name}",
        }

    if st == "odcs" and payload.strip():
        parsed = _yaml.safe_load(payload)
        if isinstance(parsed, dict):
            models_block = parsed.get("models") or parsed.get("tables") or {}
            if isinstance(models_block, dict) and models_block:
                first_key, first_model = next(iter(models_block.items()))
                tname = entity_name or first_key
                if not tname.startswith("slv_"):
                    tname = f"slv_{tname}"
                fields_block = first_model.get("fields") or first_model.get("columns") or {}
                cols = []
                if isinstance(fields_block, dict):
                    for fname, fdef in fields_block.items():
                        fdef = fdef if isinstance(fdef, dict) else {}
                        dtype = str(fdef.get("type", "STRING")).upper()
                        is_pk = bool(fdef.get("primary") or fdef.get("primary_key") or fdef.get("is_pk"))
                        cols.append({
                            "name": fname,
                            "column_name": fname,
                            "data_type": dtype,
                            "type": dtype,
                            "is_pk": is_pk,
                            "is_primary_key": is_pk,
                            "primary_key": is_pk,
                            "pii": bool(fdef.get("pii")),
                            "nullable": bool(fdef.get("nullable", not is_pk)),
                            "fk_ref": fdef.get("references") or fdef.get("fk_ref"),
                            "references": fdef.get("references") or fdef.get("fk_ref"),
                            "description": fdef.get("description", f"Imported ODCS field {fname}"),
                        })
                return {
                    "table_name": tname,
                    "name": tname,
                    "type": first_model.get("type", "dimension"),
                    "entity_type": first_model.get("type", "dimension"),
                    "grain": first_model.get("grain", "one row per imported contract record"),
                    "description": description or first_model.get("description", f"Imported ODCS entity {tname}"),
                    "column_count": len(cols),
                    "columns": cols,
                    "source_table": f"raw_bronze.{first_key}",
                }

    # Fallback default custom entity
    tname = (entity_name or "slv_custom_entity").strip()
    if not tname.startswith("slv_"):
        tname = f"slv_{tname}"
    return {
        "table_name": tname,
        "name": tname,
        "type": "dimension",
        "entity_type": "dimension",
        "grain": "one row per conformed entity",
        "description": description or f"Conformed Silver Entity ({tname})",
        "column_count": 3,
        "columns": [
            {"name": "surrogate_key", "column_name": "surrogate_key", "data_type": "STRING", "type": "STRING", "is_pk": True, "is_primary_key": True, "primary_key": True, "pii": False, "nullable": False, "description": "Deterministic Surrogate Primary Key"},
            {"name": f"{tname.replace('slv_', '')}_id", "column_name": f"{tname.replace('slv_', '')}_id", "data_type": "STRING", "type": "STRING", "is_pk": False, "is_primary_key": False, "primary_key": False, "pii": False, "nullable": False, "description": "Business Natural Key"},
            {"name": "load_ts", "column_name": "load_ts", "data_type": "TIMESTAMP", "type": "TIMESTAMP", "is_pk": False, "is_primary_key": False, "primary_key": False, "pii": False, "nullable": False, "description": "Silver Commit Timestamp"},
        ],
        "source_table": f"raw_bronze.{tname.replace('slv_', 'src_')}",
    }

