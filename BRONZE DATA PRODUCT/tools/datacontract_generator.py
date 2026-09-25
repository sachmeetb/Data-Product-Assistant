"""
datacontract_generator.py — Data Contract & STTM Export Generator for Bronze Layer.

Generates:
  1. OpenDataContract / Standard Data Contract specification in YAML format.
  2. Structured Data Contract JSON dictionary for UI rendering.
  3. Source-to-Target Mapping (STTM) in CSV format.
"""

from __future__ import annotations

import csv
import io
import yaml
from typing import Any, Dict, List, Optional


def generate_data_contract_dict(
    product_plan: Dict[str, Any],
    bank_profile: Dict[str, Any],
    structured_req: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Build a structured Data Contract dictionary aligned with OpenDataContract Specification.
    """
    bank_name = bank_profile.get("bank_name", "BFSI Bank")
    source_sys = structured_req.get("source_system") or "core_banking"
    use_case = structured_req.get("use_case_name") or f"{source_sys.title()} Bronze Schema"

    models = {}
    tables = product_plan.get("tables", [])
    if not tables:
        tables = [
            {
                "table_name": "brz_core_data",
                "entity_type": "raw",
                "description": "Raw ingested core data",
                "columns": [
                    {"name": "bronze_id", "type": "STRING", "primary_key": True, "nullable": False, "description": "Bronze ingest ID"},
                    {"name": "ingest_ts", "type": "TIMESTAMP", "primary_key": False, "nullable": False, "description": "Ingestion Timestamp"},
                    {"name": "raw_payload", "type": "JSON", "primary_key": False, "nullable": True, "description": "Raw JSON payload"},
                ],
            },
        ]

    for t in tables:
        tname = t.get("table_name", "brz_entity")
        fields = {}
        for c in t.get("columns", []):
            cname = c.get("name", "id")
            field_entry: Dict[str, Any] = {
                "type": str(c.get("type", "STRING")).lower(),
                "description": c.get("description", f"Column {cname}"),
                "nullable": bool(c.get("nullable", True)),
            }
            if c.get("primary_key") or c.get("is_pk"):
                field_entry["primary"] = True
                field_entry["nullable"] = False
            fields[cname] = field_entry

        models[tname] = {
            "description": t.get("description", f"Bronze raw entity {tname}"),
            "type": "table",
            "physicalName": f"banking_bronze.{tname}",
            "fields": fields,
        }

    quality_rules = [
        {
            "type": "custom",
            "description": "Bronze ID must be unique and non-null",
            "mustBe": "bronze_id IS NOT NULL",
        },
        {
            "type": "custom",
            "description": "Ingestion timestamp must be captured",
            "mustBe": "ingest_ts IS NOT NULL",
        },
    ]

    contract = {
        "dataContractSpecification": "0.9.3",
        "id": f"urn:datacontract:banking_bronze:{source_sys}",
        "info": {
            "title": f"{use_case} — Data Contract",
            "version": "1.0.0",
            "status": "ACTIVE",
            "description": f"Official Bronze Layer Data Contract for {bank_name} ingestion.",
            "source_system": source_sys,
            "owner": f"{bank_name} Data Integration Team",
            "target_dataset": "banking_bronze",
        },
        "servicelevels": {
            "freshness": {
                "cron": "0 4 * * *",
                "maxLag": "24h",
                "schedule": "DAILY_BATCH",
            },
        },
        "models": models,
        "quality": quality_rules,
    }
    return contract


def generate_data_contract_yaml(contract_dict: Dict[str, Any]) -> str:
    """Convert Data Contract dictionary into a formatted YAML string."""
    return yaml.dump(contract_dict, sort_keys=False, default_flow_style=False, allow_unicode=True)


def generate_sttm_csv(mappings: List[Dict[str, Any]]) -> str:
    """Convert Source-to-Target Mappings into a CSV string."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["SOURCE_SYSTEM", "SOURCE_TABLE", "TARGET_TABLE", "INGESTION_TYPE", "CONTRACT_PREFIX"])

    for m in mappings:
        src = m.get("source_system", "CORE")
        src_tbl = m.get("source_table", "raw_data")
        tgt_tbl = m.get("target_table", "banking_bronze.brz_entity")
        ing_type = m.get("ingestion_type", "FULL_LOAD")

        writer.writerow([src, src_tbl, tgt_tbl, ing_type, "BC-"])

    return output.getvalue()
