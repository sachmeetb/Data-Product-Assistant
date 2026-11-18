"""
sttm_builder.py — Multi-Source Bronze-to-Silver STTM Builder.

Discovers the 4 heterogeneous Bronze banking tables (TSYS Card, Hogan Deposit,
AFS Commercial Loan, ACLS Retail Loan) from live BigQuery `banking_bronze`
(falling back to local sample CSVs if not yet landed) and constructs an
inspectable 4-to-1 Multi-Source STTM specification for Silver's STTMCard.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


_BRONZE_4_SOURCES = [
    ("HOGAN", "brz_hogan_ci_customer", "Hogan Core Deposits (CIS)"),
    ("TSYS", "brz_tsys_cardholder", "TSYS / FDR Credit Cards"),
    ("AFS", "brz_afs_borrower", "AFS Commercial Lending"),
    ("ACLS", "brz_acls_applicant", "ACLS Retail Consumer Loans"),
]


def discover_bronze_tables(dataset_id: str = "banking_bronze") -> Dict[str, Any]:
    """Inspect live Unity Catalog banking_bronze tables; fall back to local CSVs if unavailable."""
    catalog = os.environ.get("DATABRICKS_CATALOG") or "main"
    schema_name = os.environ.get("DATABRICKS_BRONZE_SCHEMA") or dataset_id
    discovered: List[Dict[str, Any]] = []
    source_mode = "databricks"

    try:
        from tools.databricks_auth import get_sql_connection
        with get_sql_connection(catalog=catalog, schema=schema_name) as conn, conn.cursor() as cur:
            for sys_code, tbl_name, label in _BRONZE_4_SOURCES:
                fqn = f"{catalog}.{schema_name}.{tbl_name}"
                quoted = f"`{catalog}`.`{schema_name}`.`{tbl_name}`"
                cur.execute(f"SELECT COUNT(*) FROM {quoted}")
                row_count = cur.fetchone()[0]
                cur.execute(f"DESCRIBE TABLE {quoted}")
                cols = [r[0] for r in cur.fetchall() if r[0] and not str(r[0]).startswith("#")]
                discovered.append({
                    "source_system": sys_code,
                    "label": label,
                    "table_name": tbl_name,
                    "fqn": fqn,
                    "row_count": row_count,
                    "columns": cols,
                })
    except Exception:
        source_mode = "local_samples"
        discovered = []
        base_dir = Path(__file__).parent.parent
        samples_dir = base_dir.parent / "BRONZE DATA PRODUCT" / "samples" / "banking_4_sources"
        if not samples_dir.exists():
            samples_dir = base_dir / "samples" / "banking_4_sources"
        for sys_code, tbl_name, label in _BRONZE_4_SOURCES:
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
                "source_system": sys_code,
                "label": label,
                "table_name": tbl_name,
                "fqn": f"{project_id}.{dataset_id}.{tbl_name}",
                "row_count": row_count,
                "columns": cols,
            })

    return {
        "project_id": project_id,
        "dataset_id": dataset_id,
        "source_mode": source_mode,
        "tables": discovered,
    }


def build_multi_source_sttm_view(dataset_id: str = "banking_bronze", target_dataset: str = "banking_silver") -> Dict[str, Any]:
    """Construct a silver_transform_view payload for STTMCard showing 4-to-1 Bronze -> Silver mappings."""
    meta = discover_bronze_tables(dataset_id=dataset_id)

    mappings: List[Dict[str, str]] = [
        # Common Customer Surrogate Key & XREF
        {
            "source_system": "HOGAN | TSYS | AFS | ACLS",
            "source_column": f"{dataset_id}.brz_*.{{CI_CUST_NUM, cardholder_id, BORROWER_ID, APPLICANT_ID}}",
            "transform": "IDENTITY_MATCH(R1: Exact Tax ID, R2: DOB + E.164 Phone) -> SHA256_CLUSTER_KEY('CUST-')",
            "target_table": f"{target_dataset}.slv_customer_360",
            "target_column": "customer_key",
        },
        # Legal Name (4-to-1)
        {
            "source_system": "HOGAN (Deposit)",
            "source_column": f"{dataset_id}.brz_hogan_ci_customer.CI_LEGAL_NAME",
            "transform": "PARSE_LAST_FIRST_COMMA -> TITLE_CASE | SURVIVORSHIP_PRIORITY(1)",
            "target_table": f"{target_dataset}.slv_customer_360",
            "target_column": "legal_name",
        },
        {
            "source_system": "AFS (Comm Loan)",
            "source_column": f"{dataset_id}.brz_afs_borrower.PRIMARY_CONTACT_NAME",
            "transform": "TRIM_TITLE_CASE | SURVIVORSHIP_PRIORITY(2)",
            "target_table": f"{target_dataset}.slv_customer_360",
            "target_column": "legal_name",
        },
        {
            "source_system": "ACLS (Retail Loan)",
            "source_column": f"{dataset_id}.brz_acls_applicant.FIRST_NAME || LAST_NAME",
            "transform": "CONCAT_WS(' ', FIRST_NAME, MIDDLE_NAME, LAST_NAME) | SURVIVORSHIP_PRIORITY(3)",
            "target_table": f"{target_dataset}.slv_customer_360",
            "target_column": "legal_name",
        },
        {
            "source_system": "TSYS (Card)",
            "source_column": f"{dataset_id}.brz_tsys_cardholder.embossed_name",
            "transform": "TRIM_TITLE_CASE | SURVIVORSHIP_PRIORITY(4)",
            "target_table": f"{target_dataset}.slv_customer_360",
            "target_column": "legal_name",
        },
        # Tax ID / SSN
        {
            "source_system": "HOGAN | TSYS | ACLS",
            "source_column": f"{dataset_id}.brz_*.{{CI_TAX_ID, tax_ssn, NATIONAL_ID}}",
            "transform": "STRIP_HYPHENS_VALIDATE_9DIGITS -> MASK_LAST4('***-**-XXXX')",
            "target_table": f"{target_dataset}.slv_customer_360",
            "target_column": "tax_id_masked",
        },
        # Date of Birth
        {
            "source_system": "HOGAN | ACLS",
            "source_column": f"{dataset_id}.brz_*.{{CI_BIRTH_DT, DOB}}",
            "transform": "PARSE_DATE_MULTI_FORMAT('YYYYMMDD', 'MM/DD/YYYY') -> ISO_8601('YYYY-MM-DD')",
            "target_table": f"{target_dataset}.slv_customer_360",
            "target_column": "date_of_birth",
        },
        # Phone E.164
        {
            "source_system": "TSYS | ACLS | HOGAN | AFS",
            "source_column": f"{dataset_id}.brz_*.{{mobile_phone, CELL_PHONE, CI_PHONE, CONTACT_PHONE}}",
            "transform": "STANDARDIZE_E164('+1XXXXXXXXXX') | SURVIVORSHIP(TSYS > ACLS > HOGAN > AFS)",
            "target_table": f"{target_dataset}.slv_customer_360",
            "target_column": "primary_phone_e164",
        },
        # Email
        {
            "source_system": "ACLS | AFS",
            "source_column": f"{dataset_id}.brz_*.{{EMAIL, CONTACT_EMAIL}}",
            "transform": "LOWER_TRIM_VALIDATE_EMAIL | SURVIVORSHIP(ACLS > AFS)",
            "target_table": f"{target_dataset}.slv_customer_360",
            "target_column": "primary_email",
        },
        # Address / Postal Code
        {
            "source_system": "HOGAN | TSYS | ACLS | AFS",
            "source_column": f"{dataset_id}.brz_*.{{CI_POST_CODE, billing_postal_code, POSTAL_CODE, BUSINESS_ADDRESS}}",
            "transform": "EXTRACT_5DIGIT_ZIP | SURVIVORSHIP(HOGAN > TSYS > ACLS > AFS)",
            "target_table": f"{target_dataset}.slv_customer_360",
            "target_column": "primary_postal_code",
        },
        # Crosswalk Customer Type & Relationship Status
        {
            "source_system": "ALL 4 SOURCES",
            "source_column": f"{dataset_id}.brz_*.{{CI_CUST_TYPE, card_type, BORROWER_CLASS, CI_STATUS_CD, card_status, APP_STATUS, STATUS}}",
            "transform": "CROSSWALK_LOOKUP(ref_crosswalk_customer_type, ref_crosswalk_status) -> IND/ORG, ACTIVE/DORMANT",
            "target_table": f"{target_dataset}.slv_customer_360",
            "target_column": "customer_type / relationship_status",
        },
        # XREF Lineage
        {
            "source_system": "ALL 4 SOURCES",
            "source_column": f"{dataset_id}.brz_*.{{CI_CUST_NUM, cardholder_id, BORROWER_ID, APPLICANT_ID}}",
            "transform": "EMIT_XREF_RECORD(source_system, source_customer_id, match_method, match_score)",
            "target_table": f"{target_dataset}.slv_customer_source_xref",
            "target_column": "source_customer_id",
        },
        # Many-to-One Banking Arrangements
        {
            "source_system": "HOGAN -> DEPOSIT | TSYS -> CREDIT_CARD | ACLS -> RETAIL_LOAN | AFS -> COMMERCIAL_LOAN",
            "source_column": f"{dataset_id}.brz_*.{{credit_line, ORIGINATION_AMT, FACILITY_LIMIT}}",
            "transform": "MAP_ARRANGEMENT_FK(customer_key) + NORMALIZE_PRODUCT_SUBTYPE",
            "target_table": f"{target_dataset}.slv_customer_arrangement",
            "target_column": "arrangement_type / limit_or_amount",
        },
    ]

    bronze_tables = [t["fqn"] for t in meta["tables"]]
    total_bronze_rows = sum(t["row_count"] for t in meta["tables"])

    return {
        "multi_source": True,
        "bronze_source_mode": meta["source_mode"],
        "bronze_tables": meta["tables"],
        "title": "Multi-Source Bronze → Silver Customer STTM (4-to-1 Integration)",
        "step_label": "Tony Giordano Conforming Logic — Card + Deposit + Commercial Loan + Retail Loan → Silver Customer",
        "summary": (
            f"Discovered 4 heterogeneous Bronze tables ({total_bronze_rows} records via {meta['source_mode']}) "
            f"and generated {len(mappings)} multi-source STTM rules feeding `slv_customer_360`, `slv_customer_source_xref`, and `slv_customer_arrangement`."
        ),
        "silver_sources": bronze_tables,
        "silver_tables": [
            f"{target_dataset}.slv_customer_360",
            f"{target_dataset}.slv_customer_source_xref",
            f"{target_dataset}.slv_customer_arrangement",
        ],
        "lineage_summary": [
            f"{t['fqn']} ({t['row_count']} rows) → {target_dataset}.slv_customer_360 ({t['label']})"
            for t in meta["tables"]
        ],
        "header": "Multi-Source Bronze → Silver STTM Mappings",
        "mappings": mappings,
        "mapping_count": len(mappings),
    }
