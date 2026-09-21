"""
datacontract_generator.py — Data Contract & STTM Export Generator.

Generates:
  1. OpenDataContract / Standard Data Contract specification in YAML format.
  2. Structured Data Contract JSON dictionary for UI rendering.
  3. Source-to-Target Mapping (STTM) in CSV format.
"""

from __future__ import annotations

import csv
import io
import json
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
    domain = structured_req.get("domain") or structured_req.get("banking_domain") or "deposits"
    use_case = structured_req.get("use_case_name") or f"{domain.replace('_', ' ').title()} Silver Schema"

    models = {}
    tables = product_plan.get("tables", [])
    if not tables:
        # Fallback default tables if product_plan is simplified
        tables = [
            {
                "table_name": "slv_customer",
                "entity_type": "dimension",
                "description": "Conformed Customer 360 Dimension",
                "columns": [
                    {"name": "surrogate_key", "type": "STRING", "primary_key": True, "nullable": False, "description": "Primary Surrogate Key"},
                    {"name": "customer_id", "type": "STRING", "primary_key": False, "nullable": False, "description": "Source System Customer ID"},
                    {"name": "customer_name", "type": "STRING", "primary_key": False, "nullable": False, "pii": True, "description": "Full Legal Name"},
                    {"name": "kyc_status", "type": "STRING", "primary_key": False, "nullable": True, "description": "KYC Status"},
                    {"name": "load_ts", "type": "TIMESTAMP", "primary_key": False, "nullable": False, "description": "Load Timestamp"},
                ],
            },
            {
                "table_name": "slv_account",
                "entity_type": "dimension",
                "description": "Conformed Deposit Account Dimension",
                "columns": [
                    {"name": "surrogate_key", "type": "STRING", "primary_key": True, "nullable": False, "description": "Primary Surrogate Key"},
                    {"name": "account_id", "type": "STRING", "primary_key": False, "nullable": False, "description": "Account Identifier"},
                    {"name": "customer_id", "type": "STRING", "primary_key": False, "nullable": False, "references": "slv_customer.customer_id", "description": "FK to Customer"},
                    {"name": "account_type_cd", "type": "STRING", "primary_key": False, "nullable": False, "description": "Account Type Code"},
                    {"name": "currency_code", "type": "STRING", "primary_key": False, "nullable": False, "description": "ISO 4217 Currency"},
                    {"name": "current_balance", "type": "NUMERIC", "primary_key": False, "nullable": False, "description": "Ledger Balance"},
                    {"name": "load_ts", "type": "TIMESTAMP", "primary_key": False, "nullable": False, "description": "Load Timestamp"},
                ],
            },
        ]

    for t in tables:
        tname = t.get("table_name", "slv_entity")
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
            if c.get("references") or c.get("fk_ref"):
                field_entry["references"] = c.get("references") or c.get("fk_ref")
            if c.get("pii"):
                field_entry["pii"] = True
                field_entry["classification"] = "PII"
            fields[cname] = field_entry

        models[tname] = {
            "description": t.get("description", f"Silver conformed entity {tname}"),
            "type": "table",
            "physicalName": f"banking_silver.{tname}",
            "fields": fields,
        }

    quality_rules = [
        {
            "type": "custom",
            "description": "Surrogate primary key must be unique and non-null",
            "mustBe": "surrogate_key IS NOT NULL",
        },
        {
            "type": "custom",
            "description": "Currency code must follow ISO 4217 3-letter standard",
            "mustBe": "currency_code MATCHES '^[A-Z]{3}$'",
        },
        {
            "type": "custom",
            "description": "Data Quality Status must be VALID before consuming downstream",
            "mustBe": "dq_status = 'VALID'",
        },
    ]

    user_frameworks = (
        bank_profile.get("regulatory_frameworks")
        or bank_profile.get("data_standards")
        or structured_req.get("regulatory_drivers")
        or []
    )
    if isinstance(user_frameworks, str):
        user_frameworks = [user_frameworks]

    standards_list = [str(f) for f in user_frameworks]
    standards_desc = (
        f"aligned with {', '.join(standards_list)} standards"
        if standards_list
        else "governance baseline"
    )

    contract = {
        "dataContractSpecification": "0.9.3",
        "id": f"urn:datacontract:banking_silver:{domain}",
        "info": {
            "title": f"{use_case} — Data Contract",
            "version": "1.0.0",
            "status": "ACTIVE",
            "description": f"Official Silver Layer Data Contract for {bank_name} {standards_desc}.",
            "domain": domain,
            "owner": f"{bank_name} Data Governance & Architecture Team",
            "standards": standards_list,
            "target_dataset": "banking_silver",
        },
        "servicelevels": {
            "freshness": {
                "cron": "0 6 * * *",
                "maxLag": "24h",
                "schedule": "DAILY_BATCH",
            },
            "availability": {
                "percentage": "99.9%",
            },
            "retention": {
                "period": "7 years",
                "policy": "GDPR Compliant Archival",
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
    writer.writerow(["SOURCE_TABLE", "SOURCE_COLUMN", "TRANSFORMATION_LOGIC", "TARGET_TABLE", "TARGET_COLUMN", "DATA_TYPE", "BIAN_ALIGNMENT"])

    for m in mappings:
        src_col = m.get("source_column", "")
        src_parts = src_col.split(".")
        src_tbl = ".".join(src_parts[:-1]) if len(src_parts) > 1 else "raw_core"
        src_c = src_parts[-1] if src_parts else src_col

        tgt_tbl = m.get("target_table", "banking_silver.slv_entity")
        tgt_col = m.get("target_column", "id")
        transform = m.get("transform") or m.get("notes") or "DIRECT_PASS"
        datatype = "STRING"
        if "NUMERIC" in transform.upper() or "DOUBLE" in transform.upper():
            datatype = "NUMERIC"
        elif "TIMESTAMP" in transform.upper():
            datatype = "TIMESTAMP"
        elif "DATE" in transform.upper():
            datatype = "DATE"

        writer.writerow([src_tbl, src_c, transform, tgt_tbl, tgt_col, datatype, "BIAN_CONFORMED"])

    return output.getvalue()
