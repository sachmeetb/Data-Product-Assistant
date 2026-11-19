"""
sttm_builder.py — Generalized N-to-M Multi-Source Bronze-to-Silver STTM Builder.

Models the generalized enterprise mapping architecture:
  Many Source Systems -> Many Source Tables/Files (1:N) -> Business Domain -> Multiple Silver Tables

Supports reusable declarative YAML domain mapping specifications across domains and industries
(e.g., Banking Customer 360 and Industrial Water Account 360).
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mappings.registry import load_domain_mapping


def discover_bronze_tables(
    dataset_id: str = "banking_bronze",
    industry: str = "banking",
    domain: str = "customer_360",
) -> Dict[str, Any]:
    """
    Inspect live Unity Catalog Bronze tables (or local multi-table sample CSVs)
    for all 1-to-many tables defined in the active domain mapping specification.
    """
    spec = load_domain_mapping(industry=industry, domain=domain)
    project_id = (
        os.environ.get("BQ_PROJECT")
        or os.environ.get("GCP_PROJECT_ID")
        or "eogwapq-agbg-internal-data-mig"
    )
    default_schema = "water_bronze" if spec.get("industry") == "energy_industrial" else dataset_id
    schema_name = os.environ.get("BQ_BRONZE_DATASET") or default_schema

    discovered: List[Dict[str, Any]] = []
    systems_hierarchy: List[Dict[str, Any]] = []
    source_mode = "bigquery"

    flat_table_specs: List[Dict[str, Any]] = []
    for sys_entry in spec.get("source_systems") or []:
        sys_code = sys_entry["source_system"]
        sys_name = sys_entry.get("system_name", sys_code)
        modes = sys_entry.get("ingestion_modes", ["BATCH"])
        for tbl in sys_entry.get("tables") or []:
            flat_table_specs.append({
                "source_system": sys_code,
                "system_name": sys_name,
                "ingestion_modes": modes,
                "table_name": tbl["table_name"],
                "role": tbl.get("role", "PARTY_MASTER"),
                "primary_key": tbl.get("primary_key", ""),
            })

    try:
        if os.environ.get("BQ_OFFLINE_MODE") == "1":
            raise RuntimeError("BQ_OFFLINE_MODE")
        from tools.gcp_auth import get_bigquery_client

        client = get_bigquery_client(project_id=project_id, location="US")
        for item in flat_table_specs:
            tbl_name = item["table_name"]
            fqn = f"{project_id}.{schema_name}.{tbl_name}"
            tbl_obj = client.get_table(fqn)
            cols = [f.name for f in tbl_obj.schema if not f.name.startswith("_")]
            discovered.append({
                "source_system": item["source_system"],
                "label": item["system_name"],
                "table_name": tbl_name,
                "role": item["role"],
                "ingestion_modes": item["ingestion_modes"],
                "fqn": fqn,
                "row_count": int(tbl_obj.num_rows or 0),
                "columns": cols,
            })
    except Exception:
        source_mode = "local_multi_table_lakehouse"
        discovered = []
        base_dir = Path(__file__).resolve().parent.parent
        folder_name = "water_multi_table" if spec.get("industry") == "energy_industrial" else "banking_multi_table"
        samples_dir = base_dir / "samples" / folder_name
        if not samples_dir.exists():
            samples_dir = base_dir.parent / "BRONZE DATA PRODUCT" / "samples" / folder_name
        if not samples_dir.exists():
            samples_dir = base_dir / "samples" / "banking_4_sources"

        for item in flat_table_specs:
            tbl_name = item["table_name"]
            csv_path = samples_dir / f"{tbl_name}.csv"
            cols: List[str] = []
            row_count = 0
            if csv_path.exists():
                with open(csv_path, "r", encoding="utf-8-sig") as f:
                    rows = list(csv.DictReader(f))
                    row_count = len(rows)
                    if rows:
                        cols = list(rows[0].keys())
            discovered.append({
                "source_system": item["source_system"],
                "label": item["system_name"],
                "table_name": tbl_name,
                "role": item["role"],
                "ingestion_modes": item["ingestion_modes"],
                "fqn": f"{project_id}.{schema_name}.{tbl_name}",
                "row_count": row_count,
                "columns": cols,
            })

    # Group 1-to-many tables by source system
    by_sys: Dict[str, List[Dict[str, Any]]] = {}
    for d in discovered:
        by_sys.setdefault(d["source_system"], []).append(d)

    for sys_entry in spec.get("source_systems") or []:
        scode = sys_entry["source_system"]
        stbls = by_sys.get(scode, [])
        systems_hierarchy.append({
            "source_system": scode,
            "system_name": sys_entry.get("system_name", scode),
            "ingestion_modes": sys_entry.get("ingestion_modes", ["BATCH"]),
            "table_count": len(stbls),
            "total_rows": sum(t["row_count"] for t in stbls),
            "tables": stbls,
        })

    return {
        "project_id": project_id,
        "dataset_id": schema_name,
        "source_mode": source_mode,
        "industry": spec.get("industry", industry),
        "domain": spec.get("domain", domain),
        "domain_display_name": spec.get("domain_display_name", "Customer 360 Domain"),
        "systems_hierarchy": systems_hierarchy,
        "tables": discovered,
        "spec": spec,
    }


def build_multi_source_sttm_view(
    dataset_id: str = "banking_bronze",
    target_dataset: str = "banking_silver",
    industry: str = "banking",
    domain: str = "customer_360",
) -> Dict[str, Any]:
    """
    Construct a generalized N-to-M `silver_transform_view` payload for STTMCard showing:
      Many Source Systems -> Many Bronze Tables -> Business Domain -> Multiple Silver Tables.
    """
    meta = discover_bronze_tables(dataset_id=dataset_id, industry=industry, domain=domain)
    spec = meta["spec"]
    src_schema = meta["dataset_id"]
    tgt_schema = "water_silver" if spec.get("industry") == "energy_industrial" else target_dataset

    mappings: List[Dict[str, str]] = []
    for sys_entry in spec.get("source_systems") or []:
        scode = sys_entry["source_system"]
        sname = sys_entry.get("system_name", scode)
        for tbl in sys_entry.get("tables") or []:
            tname = tbl["table_name"]
            role = tbl.get("role", "PARTY_MASTER")
            fm = tbl.get("field_mappings") or {}
            am = tbl.get("arrangement_mapping") or {}
            cm = tbl.get("contact_mapping") or {}

            if fm:
                pk_col = fm.get("source_customer_id") or tbl.get("primary_key")
                # 1. Identity Resolution -> slv_customer_360.customer_key & slv_customer_source_xref
                mappings.append({
                    "source_system": f"{scode} ({tname})",
                    "source_column": f"{src_schema}.{tname}.{pk_col}",
                    "transform": "IDENTITY_RESOLVE(R1: Exact Tax ID, R2: DOB+Phone) -> SHA256_CLUSTER_KEY('CUST-')",
                    "target_table": f"{tgt_schema}.slv_customer_360",
                    "target_column": "customer_key",
                })
                mappings.append({
                    "source_system": f"{scode} ({tname})",
                    "source_column": f"{src_schema}.{tname}.{pk_col}",
                    "transform": f"EMIT_XREF_LINEAGE(source_system='{scode}', source_entity='{tname}', match_method, match_score)",
                    "target_table": f"{tgt_schema}.slv_customer_source_xref",
                    "target_column": "source_customer_id",
                })
                # 2. Name -> slv_customer_360.legal_name & slv_steward_review_queue
                name_col = fm.get("raw_name") or fm.get("raw_last_name")
                if name_col:
                    mappings.append({
                        "source_system": f"{scode} ({tname})",
                        "source_column": f"{src_schema}.{tname}.{name_col}",
                        "transform": f"STANDARDIZE_NAME({fm.get('name_format', 'TITLE_CASE')}) | MDM_SURVIVORSHIP(legal_name)",
                        "target_table": f"{tgt_schema}.slv_customer_360",
                        "target_column": "legal_name",
                    })
                    mappings.append({
                        "source_system": f"{scode} ({tname})",
                        "source_column": f"{src_schema}.{tname}.{name_col}",
                        "transform": "TIER3_FUZZY_MATCH(SIMILARITY >= 0.80 + POSTAL_BLOCK) -> ROUTE_STEWARD_QUEUE",
                        "target_table": f"{tgt_schema}.slv_steward_review_queue",
                        "target_column": "candidate_a_name / similarity_score",
                    })
                # 3. Tax ID -> slv_customer_360.tax_id_masked
                if fm.get("raw_tax_id"):
                    mappings.append({
                        "source_system": f"{scode} ({tname})",
                        "source_column": f"{src_schema}.{tname}.{fm['raw_tax_id']}",
                        "transform": "CLEAN_9DIGIT_TIN -> MASK_PII('***-**-XXXX') | SURVIVORSHIP(tax_id)",
                        "target_table": f"{tgt_schema}.slv_customer_360",
                        "target_column": "tax_id_masked",
                    })
                # 4. Phone / Email -> slv_customer_360 & slv_customer_contact
                if fm.get("raw_phone"):
                    mappings.append({
                        "source_system": f"{scode} ({tname})",
                        "source_column": f"{src_schema}.{tname}.{fm['raw_phone']}",
                        "transform": "NORMALIZE_E164('+1XXXXXXXXXX') -> 1:N CONTACT_POINT & MDM_SURVIVORSHIP",
                        "target_table": f"{tgt_schema}.slv_customer_contact",
                        "target_column": "contact_value_normalized",
                    })
                if fm.get("raw_email"):
                    mappings.append({
                        "source_system": f"{scode} ({tname})",
                        "source_column": f"{src_schema}.{tname}.{fm['raw_email']}",
                        "transform": "LOWER_TRIM_VALIDATE_EMAIL -> 1:N CONTACT_POINT & MDM_SURVIVORSHIP",
                        "target_table": f"{tgt_schema}.slv_customer_contact",
                        "target_column": "contact_value_normalized",
                    })

            if am:
                acct_col = am.get("source_account_id") or tbl.get("primary_key")
                amt_col = am.get("amount_column", "")
                mappings.append({
                    "source_system": f"{scode} ({tname})",
                    "source_column": f"{src_schema}.{tname}.{acct_col}",
                    "transform": f"LINK_CLUSTER_FK(parent_key='{tbl.get('parent_join_key', acct_col)}') -> {am.get('arrangement_type')} ({ amt_col })",
                    "target_table": f"{tgt_schema}.slv_customer_arrangement",
                    "target_column": "arrangement_key / limit_or_amount",
                })

            if cm:
                col_ref = cm.get("street_column") or cm.get("phone_column") or tbl.get("primary_key")
                mappings.append({
                    "source_system": f"{scode} ({tname})",
                    "source_column": f"{src_schema}.{tname}.{col_ref}",
                    "transform": f"NORMALIZE_1_TO_N_CONTACT(type='{cm.get('contact_type')}', parent_fk='{tbl.get('parent_join_key')}')",
                    "target_table": f"{tgt_schema}.slv_customer_contact",
                    "target_column": "contact_value_normalized",
                })

    bronze_tables = [t["fqn"] for t in meta["tables"]]
    total_bronze_rows = sum(t["row_count"] for t in meta["tables"])
    target_silver_tables = [
        f"{tgt_schema}.{t['table_name']}" for t in (spec.get("target_silver_tables") or [])
    ]
    sys_count = len(meta["systems_hierarchy"])
    tbl_count = len(meta["tables"])
    slv_count = len(target_silver_tables)

    return {
        "multi_source": True,
        "architecture_model": "N_SOURCES_TO_MANY_TABLES_TO_DOMAIN_TO_MULTIPLE_SILVER_TABLES",
        "industry": meta["industry"],
        "domain": meta["domain"],
        "domain_display_name": meta["domain_display_name"],
        "bronze_source_mode": meta["source_mode"],
        "systems_hierarchy": meta["systems_hierarchy"],
        "bronze_tables": meta["tables"],
        "title": f"N-to-M Domain STTM: {sys_count} Source Systems ({tbl_count} Bronze Tables) → {meta['domain_display_name']} ({slv_count} Silver Tables)",
        "step_label": f"Generalized N-to-M Conforming Logic — {sys_count} Sources → {tbl_count} Source Tables → {meta['domain_display_name']} → {slv_count} Silver Tables",
        "summary": (
            f"Discovered {sys_count} source systems with {tbl_count} Bronze tables ({total_bronze_rows} total records via {meta['source_mode']}) "
            f"and generated {len(mappings)} declarative N-to-M STTM rules feeding {slv_count} Silver domain tables: "
            f"{', '.join(t.split('.')[-1] for t in target_silver_tables)}."
        ),
        "silver_sources": bronze_tables,
        "silver_tables": target_silver_tables,
        "lineage_summary": [
            f"[{s['source_system']}] {s['system_name']} ({s['table_count']} tables: {', '.join(t['table_name'] for t in s['tables'])}) → {meta['domain_display_name']} → {slv_count} Silver Tables"
            for s in meta["systems_hierarchy"]
        ],
        "header": f"Declarative N-to-M STTM Mappings ({sys_count} Systems · {tbl_count} Bronze Tables → {slv_count} Silver Tables)",
        "mappings": mappings,
        "mapping_count": len(mappings),
    }
