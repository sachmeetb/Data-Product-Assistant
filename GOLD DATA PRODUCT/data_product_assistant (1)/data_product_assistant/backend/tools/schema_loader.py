"""
schema_loader.py — Multi-Industry and Canonical Domain Catalog loader for Gold Data Product Assistant.

Provides industry trees, domains, canonical entity schemas, source models, and external entity import
(DDL, ODCS, Catalog) for the Domain Navigator and Data Contract workbench.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml

log = logging.getLogger(__name__)

_BACKEND_DIR = Path(__file__).parent.parent
_DATA_DIR = _BACKEND_DIR / "data"
_MULTI_INDUSTRY_FILE = _DATA_DIR / "multi_industry_catalog.json"

_cache: dict = {}


def get_multi_industry_catalog() -> dict:
    """Load and return the multi-industry catalog."""
    if "multi_industry_catalog" in _cache:
        return _cache["multi_industry_catalog"]
    
    # Check data/ and fallback to knowledge/
    paths_to_check = [
        _MULTI_INDUSTRY_FILE,
        _BACKEND_DIR / "knowledge" / "multi_industry_catalog.json",
        _BACKEND_DIR.parent.parent / "SILVER DATA PRODUCT" / "knowledge" / "multi_industry_catalog.json",
    ]
    for p in paths_to_check:
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                    _cache["multi_industry_catalog"] = data
                    return data
            except Exception as exc:
                log.warning("Could not load %s: %s", p, exc)
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
        items = [(e.get("table_name") or e.get("name", "gold_entity"), e) for e in entities_raw]
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
            "description": edef.get("description", f"Gold Consumption entity {table_name}"),
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


def get_domains_for_industry(industry_id: str = "retail", normalized: bool = False) -> list[dict]:
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
    """Return available upstream Bronze/Silver source models that can be aggregated into a Gold star schema."""
    raw_models = [
        {
            "source_id": "silver_marketing.slv_campaign_performance_daily",
            "source_system": "Silver Campaign Data Product",
            "layer": "SILVER",
            "industry": "retail",
            "domain": "Digital Marketing & Advertising",
            "domain_name": "marketing",
            "target_table_name": "gold_fact_campaign_daily_performance",
            "entity_type": "fact",
            "grain": "one row per campaign per channel per date",
            "description": "Daily aggregated marketing impressions, clicks, conversions, spend, and ROAS",
            "columns": [
                {"name": "campaign_perf_sk", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Surrogate primary key"},
                {"name": "campaign_id", "data_type": "STRING", "is_pk": False, "fk_ref": "gold_dim_campaign.campaign_id", "nullable": False, "description": "Campaign dimension FK"},
                {"name": "channel_id", "data_type": "STRING", "is_pk": False, "fk_ref": "gold_dim_channel.channel_id", "nullable": False, "description": "Channel dimension FK"},
                {"name": "date_key", "data_type": "DATE", "is_pk": False, "nullable": False, "description": "Performance date partition"},
                {"name": "impressions", "data_type": "INT64", "is_pk": False, "nullable": False, "description": "Total ad impressions served"},
                {"name": "clicks", "data_type": "INT64", "is_pk": False, "nullable": False, "description": "Total user clicks recorded"},
                {"name": "conversions", "data_type": "INT64", "is_pk": False, "nullable": False, "description": "Total attributed conversions"},
                {"name": "spend", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "Total advertising expenditure in USD"},
                {"name": "attributed_revenue", "data_type": "NUMERIC", "is_pk": False, "nullable": True, "description": "Total gross revenue generated in USD"},
            ],
        },
        {
            "source_id": "silver_banking.slv_transaction_events",
            "source_system": "Silver Banking Core Engine",
            "layer": "SILVER",
            "industry": "banking",
            "domain": "Core Banking & Accounts",
            "domain_name": "core_banking",
            "target_table_name": "gold_fact_daily_account_metrics",
            "entity_type": "fact",
            "grain": "one row per account per posting day",
            "description": "Daily aggregated account balances, debits, credits, and liquidity metrics",
            "columns": [
                {"name": "account_metric_sk", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Surrogate primary key"},
                {"name": "account_id", "data_type": "STRING", "is_pk": False, "fk_ref": "gold_dim_account.account_id", "nullable": False, "description": "Account dimension FK"},
                {"name": "customer_id", "data_type": "STRING", "is_pk": False, "fk_ref": "gold_dim_customer.customer_id", "nullable": False, "description": "Customer dimension FK"},
                {"name": "metric_date", "data_type": "DATE", "is_pk": False, "nullable": False, "description": "Activity date"},
                {"name": "closing_balance", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "End-of-day ledger balance"},
                {"name": "daily_debit_total", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "Sum of outbound debit transactions"},
                {"name": "daily_credit_total", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "Sum of inbound credit transactions"},
            ],
        },
        {
            "source_id": "silver_industrial.slv_clean_in_place_cycle",
            "source_system": "Ecolab 3D TRASAR IoT Stream",
            "layer": "SILVER",
            "industry": "energy_industrial",
            "domain": "Water Quality & Telemetry",
            "domain_name": "water_telemetry",
            "target_table_name": "gold_fact_cip_sanitation_efficiency",
            "entity_type": "fact",
            "grain": "one row per CIP wash cycle per sanitized unit",
            "description": "Clean-In-Place chemical efficiency, water recovery, and regulatory compliance KPIs",
            "columns": [
                {"name": "cycle_fact_sk", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Cycle fact primary key"},
                {"name": "equipment_id", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "Equipment asset FK"},
                {"name": "wash_date", "data_type": "DATE", "is_pk": False, "nullable": False, "description": "Sanitation execution date"},
                {"name": "caustic_conc_pct", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "Caustic concentration percentage"},
                {"name": "water_conserved_gallons", "data_type": "NUMERIC", "is_pk": False, "nullable": False, "description": "Net gallons of water conserved"},
                {"name": "compliance_passed", "data_type": "BOOL", "is_pk": False, "nullable": False, "description": "Sanitary audit pass flag"},
            ],
        },
    ]
    out = []
    for m in raw_models:
        enriched = dict(m)
        enriched["model_id"] = m["source_id"]
        enriched["source_table"] = m["source_id"]
        enriched["target_gold_table"] = m["target_table_name"]
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
    Returns a normalized entity dictionary ready to merge into a Gold contract & dimensional model.
    """
    st = (source_type or "source_catalog").lower().strip()

    if st == "source_catalog" and source_model_id:
        for sm in get_source_models_catalog():
            if sm["source_id"] == source_model_id or sm["target_table_name"] == source_model_id:
                tname = entity_name or sm["target_table_name"]
                norm = _normalize_entity_list({tname: sm})[0]
                norm["source_table"] = sm["source_id"]
                norm["source_system"] = sm["source_system"]
                return norm
        cat = get_multi_industry_catalog()
        for ind in cat.get("industries", []):
            for dom in ind.get("domains", []):
                ents = dom.get("entities", {})
                if source_model_id in ents:
                    tname = entity_name or source_model_id
                    norm = _normalize_entity_list({tname: ents[source_model_id]})[0]
                    norm["source_table"] = f"silver.{source_model_id}"
                    return norm

    if st == "ddl" and payload.strip():
        tbl_match = re.search(r"TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?([a-zA-Z0-9_.\-]+)[`\"']?", payload, re.IGNORECASE)
        extracted_name = tbl_match.group(1).split(".")[-1] if tbl_match else "gold_custom_table"
        tname = (entity_name or extracted_name).strip()
        if not (tname.startswith("gold_") or tname.startswith("dim_") or tname.startswith("fact_")):
            tname = f"gold_{tname}"

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

                is_pk = "PRIMARY KEY" in line.upper() or cname.endswith("_sk") or cname == "surrogate_key"
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
                    "pii": any(p in cname.lower() for p in ("name", "ssn", "email", "phone", "iban")),
                    "nullable": nullable,
                    "description": col_desc,
                })
        return {
            "table_name": tname,
            "name": tname,
            "type": "fact" if "fact" in tname.lower() else "dimension",
            "entity_type": "fact" if "fact" in tname.lower() else "dimension",
            "grain": "one row per grain instance",
            "description": description or f"Gold Consumption entity imported from SQL DDL ({tname})",
            "column_count": len(cols),
            "columns": cols,
            "source_table": f"silver.{extracted_name}",
        }

    if st == "odcs" and payload.strip():
        parsed = yaml.safe_load(payload)
        if isinstance(parsed, dict):
            models_block = parsed.get("models") or parsed.get("tables") or {}
            if isinstance(models_block, dict) and models_block:
                first_key, first_model = next(iter(models_block.items()))
                tname = entity_name or first_key
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
                    "type": first_model.get("type", "fact" if "fact" in tname.lower() else "dimension"),
                    "entity_type": first_model.get("type", "fact" if "fact" in tname.lower() else "dimension"),
                    "grain": first_model.get("grain", "one row per imported record"),
                    "description": first_model.get("description", description or f"Imported ODCS Table {tname}"),
                    "column_count": len(cols),
                    "columns": cols,
                    "source_table": first_model.get("source_table") or f"silver.{tname}",
                }

    # Fallback
    fallback_name = entity_name or "gold_custom_table"
    return {
        "table_name": fallback_name,
        "name": fallback_name,
        "type": "dimension",
        "entity_type": "dimension",
        "grain": "one row per record",
        "description": description or f"Gold Entity {fallback_name}",
        "column_count": 3,
        "columns": [
            {"name": "id", "data_type": "STRING", "is_pk": True, "nullable": False, "description": "Primary identifier"},
            {"name": "name", "data_type": "STRING", "is_pk": False, "nullable": False, "description": "Entity name"},
            {"name": "created_at", "data_type": "TIMESTAMP", "is_pk": False, "nullable": False, "description": "Creation timestamp"},
        ],
    }
