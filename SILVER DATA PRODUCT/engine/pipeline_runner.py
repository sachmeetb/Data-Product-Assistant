"""
pipeline_runner.py — Executable runner for Bronze-to-Silver Multi-Source Conformance.

Demonstrates:
  1. Ingestion of 4 heterogeneous Bronze banking sources (Hogan, AFS, TSYS, ACLS)
  2. Step 4: Standardization (ISO dates, E.164 phones, reference crosswalks)
  3. Step 5: Identity Resolution (Blocking, Matching, Deduplication)
  4. Step 5: Survivorship & Cross-Reference Keying (Golden Record + XREF)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

from engine.resolver import IdentityResolver
from engine.standardizer import Standardizer
from engine.survivorship import SurvivorshipEngine


def read_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        print(f"[WARN] File not found: {path}")
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader)


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _build_arrangements(
    clusters: List[Dict[str, Any]],
    hogan_rows: List[Dict[str, Any]],
    afs_rows: List[Dict[str, Any]],
    tsys_rows: List[Dict[str, Any]],
    acls_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Associate Deposit (Hogan), Credit Card (TSYS), Retail Loan (ACLS),
    and Commercial Loan (AFS) arrangements with each unified Silver customer_key.
    """
    hogan_by_id = {r.get("CI_CUST_NUM", ""): r for r in hogan_rows}
    afs_by_id = {r.get("BORROWER_ID", ""): r for r in afs_rows}
    tsys_by_id = {r.get("cardholder_id", ""): r for r in tsys_rows}
    acls_by_id = {r.get("APPLICANT_ID", ""): r for r in acls_rows}

    arrangements: List[Dict[str, Any]] = []
    for cluster in clusters:
        cust_key = cluster["customer_key"]
        for mw in cluster["members"]:
            rec = mw["record"]
            src = rec["source_system"]
            src_id = rec["source_customer_id"]
            if src == "HOGAN":
                raw = hogan_by_id.get(src_id, {})
                arrangements.append({
                    "arrangement_key": f"ARR-DEP-{src_id}",
                    "customer_key": cust_key,
                    "source_system": "HOGAN",
                    "source_account_id": src_id,
                    "arrangement_type": "DEPOSIT",
                    "product_subtype": "Demand Deposit / Checking",
                    "status": "ACTIVE" if raw.get("CI_STATUS_CD") == "01" else "DORMANT",
                    "limit_or_amount": 0.0,
                    "branch_or_channel": raw.get("CI_BRANCH_NUM", "0101"),
                    "open_or_extract_date": raw.get("CI_EXTRACT_TS", "")[:10],
                })
            elif src == "TSYS":
                raw = tsys_by_id.get(src_id, {})
                arrangements.append({
                    "arrangement_key": f"ARR-CRD-{src_id}",
                    "customer_key": cust_key,
                    "source_system": "TSYS",
                    "source_account_id": src_id,
                    "arrangement_type": "CREDIT_CARD",
                    "product_subtype": f"{raw.get('card_type', 'RETAIL')} Revolving Card",
                    "status": raw.get("card_status", "OPEN"),
                    "limit_or_amount": float(raw.get("credit_line") or 0),
                    "branch_or_channel": "CARD_SERVICING",
                    "open_or_extract_date": raw.get("open_date", ""),
                })
            elif src == "ACLS":
                raw = acls_by_id.get(src_id, {})
                arrangements.append({
                    "arrangement_key": f"ARR-LON-{src_id}",
                    "customer_key": cust_key,
                    "source_system": "ACLS",
                    "source_account_id": src_id,
                    "arrangement_type": "RETAIL_LOAN",
                    "product_subtype": f"{raw.get('LOAN_PURPOSE', 'PERSONAL')} Consumer Loan",
                    "status": "APPROVED" if raw.get("APP_STATUS") == "A" else "PENDING",
                    "limit_or_amount": float(raw.get("ORIGINATION_AMT") or 0),
                    "branch_or_channel": "RETAIL_LENDING",
                    "open_or_extract_date": raw.get("APP_DATE", ""),
                })
            elif src == "AFS":
                raw = afs_by_id.get(src_id, {})
                arrangements.append({
                    "arrangement_key": f"ARR-COM-{src_id}",
                    "customer_key": cust_key,
                    "source_system": "AFS",
                    "source_account_id": src_id,
                    "arrangement_type": "COMMERCIAL_LOAN",
                    "product_subtype": f"{raw.get('BORROWER_CLASS', 'COM')} Credit Facility",
                    "status": "ACTIVE" if raw.get("STATUS") == "A" else "LIQUIDATED",
                    "limit_or_amount": float(raw.get("FACILITY_LIMIT") or 0),
                    "branch_or_channel": "COMMERCIAL_LENDING",
                    "open_or_extract_date": raw.get("LAST_REVIEW_DATE", ""),
                })
    return arrangements


def run_conformance_pipeline() -> Dict[str, Any]:
    base_dir = Path(__file__).parent.parent
    bronze_dir = base_dir.parent / "BRONZE DATA PRODUCT" / "samples" / "banking_4_sources"
    if not bronze_dir.exists():
        bronze_dir = base_dir / "samples" / "banking_4_sources"
    output_dir = base_dir / "output"

    print("================================================================================")
    print("BRONZE TO SILVER MULTI-SOURCE CONFORMANCE PIPELINE")
    print("Framework: Tony Giordano 'Conforming Logic' (Hogan + AFS + TSYS + ACLS -> Silver)")
    print("================================================================================\n")

    # ── 1. INGEST BRONZE SOURCES ──────────────────────────────────────────────
    hogan_rows = read_csv(bronze_dir / "brz_hogan_ci_customer.csv")
    afs_rows = read_csv(bronze_dir / "brz_afs_borrower.csv")
    tsys_rows = read_csv(bronze_dir / "brz_tsys_cardholder.csv")
    acls_rows = read_csv(bronze_dir / "brz_acls_applicant.csv")

    total_bronze_records = len(hogan_rows) + len(afs_rows) + len(tsys_rows) + len(acls_rows)
    print(f"[*] Ingested {len(hogan_rows)} records from Hogan Core Deposits (CIS)")
    print(f"[*] Ingested {len(afs_rows)} records from AFS Commercial Lending")
    print(f"[*] Ingested {len(tsys_rows)} records from TSYS/FDR Credit Cards")
    print(f"[*] Ingested {len(acls_rows)} records from ACLS Consumer Loans")
    print(f"--> Total Bronze Input Records: {total_bronze_records}\n")

    # ── 2. STANDARDIZATION (STEP 4) ───────────────────────────────────────────
    standardizer = Standardizer(reference_dir=base_dir / "reference")
    standardized: List[Dict[str, Any]] = []

    for r in hogan_rows:
        standardized.append(standardizer.standardize_hogan(r))
    for r in afs_rows:
        standardized.append(standardizer.standardize_afs(r))
    for r in tsys_rows:
        standardized.append(standardizer.standardize_tsys(r))
    for r in acls_rows:
        standardized.append(standardizer.standardize_acls(r))

    print(f"[OK] Step 4 Standardize complete: {len(standardized)} records normalized with E.164, ISO 8601 & Crosswalks.")

    # ── 3. IDENTITY RESOLUTION (STEP 5) ───────────────────────────────────────
    resolver = IdentityResolver()
    clusters = resolver.resolve(standardized)
    print(f"[OK] Step 5 Identity Resolution complete: Grouped into {len(clusters)} distinct customer entities.\n")

    # ── 4. SURVIVORSHIP & GOLDEN RECORD GENERATION (STEP 5) ───────────────────
    survivorship = SurvivorshipEngine()
    golden_customers: List[Dict[str, Any]] = []
    all_xrefs: List[Dict[str, Any]] = []

    for cluster in clusters:
        golden, xrefs = survivorship.merge_cluster(cluster)
        golden_customers.append(golden)
        all_xrefs.extend(xrefs)

    # ── 4b. LINK BANKING ARRANGEMENTS (Deposit, Card, Retail & Commercial Loans)
    all_arrangements = _build_arrangements(clusters, hogan_rows, afs_rows, tsys_rows, acls_rows)
    arr_by_cust: Dict[str, List[Dict[str, Any]]] = {}
    for arr in all_arrangements:
        arr_by_cust.setdefault(arr["customer_key"], []).append(arr)
    for gc in golden_customers:
        c_arrs = arr_by_cust.get(gc["customer_key"], [])
        gc["arrangement_count"] = len(c_arrs)
        gc["arrangement_types"] = sorted({a["arrangement_type"] for a in c_arrs})

    # ── 5. EXPORT SILVER RESULTS ──────────────────────────────────────────────
    customer_fields = [
        "customer_key", "customer_type", "legal_name", "first_name", "last_name",
        "date_of_birth", "tax_id_normalized", "tax_id_masked", "primary_phone_e164",
        "primary_email", "primary_postal_code", "relationship_status",
        "survivorship_source", "contributing_sources", "source_record_count",
        "arrangement_count", "arrangement_types",
        "effective_from_ts", "effective_to_ts", "is_current", "dq_status"
    ]
    # Convert array to string for CSV
    export_customers = []
    for gc in golden_customers:
        row = dict(gc)
        row["contributing_sources"] = "|".join(gc["contributing_sources"])
        row["arrangement_types"] = "|".join(gc.get("arrangement_types", []))
        export_customers.append(row)

    xref_fields = [
        "xref_key", "customer_key", "source_system", "source_entity",
        "source_customer_id", "match_method", "match_score", "review_status", "linked_at"
    ]
    arr_fields = [
        "arrangement_key", "customer_key", "source_system", "source_account_id",
        "arrangement_type", "product_subtype", "status", "limit_or_amount",
        "branch_or_channel", "open_or_extract_date"
    ]

    write_csv(output_dir / "silver_customer.csv", export_customers, customer_fields)
    write_csv(output_dir / "silver_customer_source_xref.csv", all_xrefs, xref_fields)
    write_csv(output_dir / "silver_customer_arrangement.csv", all_arrangements, arr_fields)

    # ── 6. SUMMARY REPORTING ──────────────────────────────────────────────────
    multi_source_entities = [g for g in golden_customers if g["source_record_count"] > 1]
    dedup_ratio = ((total_bronze_records - len(golden_customers)) / total_bronze_records) * 100 if total_bronze_records else 0.0

    print("================================================================================")
    print("CONFORMANCE EXECUTION SUMMARY METRICS")
    print("================================================================================")
    print(f"Total Bronze Records Ingested : {total_bronze_records}")
    print(f"Conformed Silver Golden Entities: {len(golden_customers)}")
    print(f"Total Source Cross-References   : {len(all_xrefs)}")
    print(f"Total Linked Arrangements       : {len(all_arrangements)}")
    print(f"Entities Merged Across Sources  : {len(multi_source_entities)}")
    print(f"Deduplication / Compression     : {dedup_ratio:.1f}%\n")

    return {
        "source_counts": {
            "HOGAN": len(hogan_rows),
            "AFS": len(afs_rows),
            "TSYS": len(tsys_rows),
            "ACLS": len(acls_rows),
        },
        "total_bronze_records": total_bronze_records,
        "unified_customers": len(golden_customers),
        "deduplicated_count": total_bronze_records - len(golden_customers),
        "dedup_ratio_pct": round(dedup_ratio, 1),
        "multi_source_customers": len(multi_source_entities),
        "total_xrefs": len(all_xrefs),
        "total_arrangements": len(all_arrangements),
        "golden_customers": golden_customers,
        "xrefs": all_xrefs,
        "arrangements": all_arrangements,
    }


if __name__ == "__main__":
    run_conformance_pipeline()
