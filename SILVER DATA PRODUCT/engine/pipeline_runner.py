"""
pipeline_runner.py — Executable Runner for Generalized N-to-M Bronze-to-Silver Domain Conformance.

Demonstrates:
  1. 1-to-Many Bronze Ingestion per Source System (e.g., 4 Banking Systems -> 12 Bronze Tables, or 3 Water Systems -> 4 Bronze Tables)
  2. Declarative YAML Mapping & Step 4 Standardization (ISO dates, E.164 phones, reference crosswalks)
  3. Step 5 Identity Resolution (Deterministic Tier 1/2 Auto-Link + Tier 3 Probabilistic Steward Review Queue)
  4. Step 5 MDM Survivorship & Multi-Table Silver Domain Generation:
     - slv_customer_360 (Golden Master Record)
     - slv_customer_source_xref (Multi-Table Source Key Lineage)
     - slv_customer_arrangement (Deposits, Cards, Commercial Facilities, Collateral, Retail Loans / Water Assets)
     - slv_customer_contact (1:N Phones, Emails, Multi-Addresses & Kafka Verified Streams)
     - slv_steward_review_queue (Tier-3 Probabilistic Match Candidates for Data Steward Review)
"""

from __future__ import annotations

import csv
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from engine.resolver import IdentityResolver
from engine.standardizer import Standardizer
from engine.survivorship import SurvivorshipEngine
from mappings.registry import load_domain_mapping


def read_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_bq_or_csv(
    table_name: str,
    csv_path: Path,
    dataset_id: str = "banking_bronze",
) -> Tuple[List[Dict[str, Any]], str]:
    """Try reading live Bronze rows from GCP BigQuery first; fall back to local CSV."""
    if os.environ.get("BQ_OFFLINE_MODE") != "1":
        try:
            from tools.gcp_auth import get_bigquery_client

            project_id = (
                os.environ.get("BQ_PROJECT")
                or os.environ.get("GCP_PROJECT_ID")
                or "eogwapq-agbg-internal-data-mig"
            )
            schema_name = os.environ.get("BQ_BRONZE_DATASET") or dataset_id
            client = get_bigquery_client(project_id=project_id, location="US")
            query = f"SELECT * FROM `{project_id}.{schema_name}.{table_name}`"
            rows = [dict(r.items()) for r in client.query(query).result()]
            if rows:
                return rows, "bigquery"
        except Exception:
            pass
    return read_csv(csv_path), "local_csv"


def _build_arrangements_from_spec(
    clusters: List[Dict[str, Any]],
    loaded_tables: Dict[str, List[Dict[str, Any]]],
    spec: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Build `slv_customer_arrangement` records dynamically from all `ARRANGEMENT_SOURCE` tables
    across all source systems using declarative `arrangement_mapping` rules.
    """
    # Map (source_system, source_customer_id) -> customer_key
    key_lookup: Dict[Tuple[str, str], str] = {}
    for cluster in clusters:
        ckey = cluster["customer_key"]
        for mw in cluster["members"]:
            rec = mw["record"]
            key_lookup[(rec["source_system"], rec["source_customer_id"])] = ckey

    arrangements: List[Dict[str, Any]] = []
    for sys_entry in spec.get("source_systems") or []:
        scode = sys_entry["source_system"]
        for tbl in sys_entry.get("tables") or []:
            am = tbl.get("arrangement_mapping")
            if not am:
                continue
            tname = tbl["table_name"]
            parent_fk_col = tbl.get("parent_join_key") or tbl.get("primary_key") or ""
            acct_col = am.get("source_account_id") or tbl.get("primary_key") or ""
            prefix = am.get("arrangement_prefix", "ARR")
            arr_type = am.get("arrangement_type", "ARRANGEMENT")
            sub_col = am.get("subtype_column", "")
            sub_suffix = am.get("subtype_suffix", "")
            amt_col = am.get("amount_column", "")
            stat_col = am.get("status_column", "")
            stat_lit = am.get("status_literal", "ACTIVE")
            chan_col = am.get("channel_column", "")
            chan_lit = am.get("channel_literal", scode)
            date_col = am.get("date_column", "")

            for row in loaded_tables.get(tname, []):
                parent_id = str(row.get(parent_fk_col, "")).strip()
                cust_key = key_lookup.get((scode, parent_id))
                if not cust_key:
                    continue
                acct_id = str(row.get(acct_col, "")).strip()
                raw_sub = str(row.get(sub_col, "")).strip() if sub_col else ""
                subtype = f"{raw_sub} {sub_suffix}".strip() if raw_sub else sub_suffix
                raw_stat = str(row.get(stat_col, "")).strip() if stat_col else stat_lit
                if raw_stat in ("01", "A"):
                    norm_stat = "ACTIVE"
                elif raw_stat == "05":
                    norm_stat = "DORMANT"
                else:
                    norm_stat = raw_stat or "ACTIVE"
                try:
                    amt_val = float(row.get(amt_col) or 0.0) if amt_col else 0.0
                except (ValueError, TypeError):
                    amt_val = 0.0
                channel = str(row.get(chan_col, "")).strip() if chan_col else chan_lit
                open_dt = str(row.get(date_col, ""))[:10] if date_col else ""

                arrangements.append({
                    "arrangement_key": f"{prefix}-{acct_id}",
                    "customer_key": cust_key,
                    "source_system": scode,
                    "source_table": tname,
                    "source_account_id": acct_id,
                    "arrangement_type": arr_type,
                    "product_subtype": subtype,
                    "status": norm_stat,
                    "limit_or_amount": amt_val,
                    "branch_or_channel": channel,
                    "open_or_extract_date": open_dt,
                })
    return arrangements


def _build_contacts_from_spec(
    clusters: List[Dict[str, Any]],
    loaded_tables: Dict[str, List[Dict[str, Any]]],
    spec: Dict[str, Any],
    standardizer: Standardizer,
) -> List[Dict[str, Any]]:
    """
    Build `slv_customer_contact` (1:N normalized phones, emails, multi-addresses, and Kafka OTP touchpoints)
    linked to each resolved `customer_key`.
    """
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    contacts: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()
    key_lookup: Dict[Tuple[str, str], str] = {}

    for cluster in clusters:
        ckey = cluster["customer_key"]
        for mw in cluster["members"]:
            rec = mw["record"]
            scode = rec["source_system"]
            stbl = rec["source_entity"]
            sid = rec["source_customer_id"]
            key_lookup[(scode, sid)] = ckey

            phone = rec.get("primary_phone_e164")
            if phone and (ckey, "PHONE_E164", phone) not in seen:
                seen.add((ckey, "PHONE_E164", phone))
                contacts.append({
                    "contact_key": f"CNT-{uuid.uuid4().hex[:8].upper()}",
                    "customer_key": ckey,
                    "contact_type": "PHONE_E164",
                    "contact_usage": "PRIMARY_VOICE_SMS",
                    "contact_value_normalized": phone,
                    "source_system": scode,
                    "source_table": stbl,
                    "verification_status": "VERIFIED",
                    "observed_at": now_ts,
                })

            email = rec.get("primary_email")
            if email and (ckey, "EMAIL", email) not in seen:
                seen.add((ckey, "EMAIL", email))
                contacts.append({
                    "contact_key": f"CNT-{uuid.uuid4().hex[:8].upper()}",
                    "customer_key": ckey,
                    "contact_type": "EMAIL",
                    "contact_usage": "PRIMARY_ELECTRONIC",
                    "contact_value_normalized": email,
                    "source_system": scode,
                    "source_table": stbl,
                    "verification_status": "VERIFIED",
                    "observed_at": now_ts,
                })

    # Harvest dedicated child contact & streaming enrichment tables (e.g. brz_hogan_ci_address, brz_tsys_auth_events)
    for sys_entry in spec.get("source_systems") or []:
        scode = sys_entry["source_system"]
        for tbl in sys_entry.get("tables") or []:
            cm = tbl.get("contact_mapping")
            if not cm:
                continue
            tname = tbl["table_name"]
            parent_fk_col = tbl.get("parent_join_key") or ""
            ctype = cm.get("contact_type", "ADDRESS")

            for row in loaded_tables.get(tname, []):
                parent_id = str(row.get(parent_fk_col, "")).strip()
                ckey = key_lookup.get((scode, parent_id))
                if not ckey:
                    continue
                if ctype == "ADDRESS":
                    usage = str(row.get(cm.get("usage_column", ""), "RES")).strip()
                    street = str(row.get(cm.get("street_column", ""), "")).strip()
                    city = str(row.get(cm.get("city_column", ""), "")).strip()
                    state = str(row.get(cm.get("state_column", ""), "")).strip()
                    postal = str(row.get(cm.get("postal_column", ""), "")).strip()
                    formatted = f"{street}, {city}, {state} {postal}".strip(", ")
                    if formatted and (ckey, f"ADDR_{usage}", formatted) not in seen:
                        seen.add((ckey, f"ADDR_{usage}", formatted))
                        contacts.append({
                            "contact_key": f"CNT-{uuid.uuid4().hex[:8].upper()}",
                            "customer_key": ckey,
                            "contact_type": "POSTAL_ADDRESS",
                            "contact_usage": usage,
                            "contact_value_normalized": formatted,
                            "source_system": scode,
                            "source_table": tname,
                            "verification_status": "KYC_VERIFIED",
                            "observed_at": now_ts,
                        })
                elif ctype == "MOBILE_OTP_STREAM":
                    raw_mob = row.get(cm.get("phone_column", ""))
                    norm_mob = standardizer.normalize_phone_e164(raw_mob)
                    ev_ts = str(row.get(cm.get("event_ts_column", ""), now_ts))
                    if norm_mob and (ckey, "KAFKA_OTP_MOBILE", norm_mob) not in seen:
                        seen.add((ckey, "KAFKA_OTP_MOBILE", norm_mob))
                        contacts.append({
                            "contact_key": f"CNT-{uuid.uuid4().hex[:8].upper()}",
                            "customer_key": ckey,
                            "contact_type": "KAFKA_OTP_MOBILE",
                            "contact_usage": "REALTIME_3DS_ALERT",
                            "contact_value_normalized": norm_mob,
                            "source_system": scode,
                            "source_table": tname,
                            "verification_status": "3DS_STREAM_VERIFIED",
                            "observed_at": ev_ts,
                        })

    return contacts


def run_conformance_pipeline(
    industry: str = "banking",
    domain: str = "customer_360",
) -> Dict[str, Any]:
    base_dir = Path(__file__).resolve().parent.parent
    spec = load_domain_mapping(industry=industry, domain=domain)
    is_water = spec.get("industry") == "energy_industrial"
    folder_name = "water_multi_table" if is_water else "banking_multi_table"
    bronze_dataset = "water_bronze" if is_water else "banking_bronze"

    bronze_dir = base_dir / "samples" / folder_name
    if not bronze_dir.exists():
        bronze_dir = base_dir.parent / "BRONZE DATA PRODUCT" / "samples" / folder_name
    if not bronze_dir.exists():
        bronze_dir = base_dir / "samples" / "banking_4_sources"
    output_dir = base_dir / "output"

    # ── 1. INGEST 1-TO-MANY BRONZE TABLES PER SOURCE SYSTEM ───────────────────
    loaded_tables: Dict[str, List[Dict[str, Any]]] = {}
    modes_seen: List[str] = []
    source_counts: Dict[str, int] = {}
    source_table_counts: Dict[str, Dict[str, int]] = {}
    party_records_ingested = 0
    total_all_table_rows = 0

    standardizer = Standardizer(reference_dir=base_dir / "reference")
    standardized_party_records: List[Dict[str, Any]] = []

    for sys_entry in spec.get("source_systems") or []:
        scode = sys_entry["source_system"]
        source_counts[scode] = 0
        source_table_counts[scode] = {}

        for tbl in sys_entry.get("tables") or []:
            tname = tbl["table_name"]
            role = tbl.get("role", "PARTY_MASTER")
            rows, mode = _read_bq_or_csv(tname, bronze_dir / f"{tname}.csv", dataset_id=bronze_dataset)
            loaded_tables[tname] = rows
            modes_seen.append(mode)
            source_table_counts[scode][tname] = len(rows)
            total_all_table_rows += len(rows)

            if role == "PARTY_MASTER":
                source_counts[scode] += len(rows)
                party_records_ingested += len(rows)
                for r in rows:
                    standardized_party_records.append(
                        standardizer.standardize_from_spec(r, scode, tbl)
                    )
            elif role == "SECONDARY_PARTY_SOURCE":
                for r in rows:
                    standardized_party_records.append(
                        standardizer.standardize_from_spec(r, scode, tbl)
                    )

    source_mode = "bigquery" if (modes_seen and all(m == "bigquery" for m in modes_seen)) else "local_multi_table_lakehouse"

    # ── 2. IDENTITY RESOLUTION & STEWARD REVIEW QUEUE (STEP 5) ────────────────
    resolver = IdentityResolver()
    clusters = resolver.resolve(standardized_party_records)
    steward_queue = resolver.steward_review_queue

    # ── 3. MDM SURVIVORSHIP & GOLDEN RECORD GENERATION (STEP 5) ───────────────
    survivorship = SurvivorshipEngine(policies=spec.get("survivorship_policies"))
    golden_customers: List[Dict[str, Any]] = []
    all_xrefs: List[Dict[str, Any]] = []

    for cluster in clusters:
        golden, xrefs = survivorship.merge_cluster(cluster)
        golden_customers.append(golden)
        all_xrefs.extend(xrefs)

    # ── 4. MULTI-TABLE ARRANGEMENTS & 1:N CONTACTS ────────────────────────────
    all_arrangements = _build_arrangements_from_spec(clusters, loaded_tables, spec)
    all_contacts = _build_contacts_from_spec(clusters, loaded_tables, spec, standardizer)

    arr_by_cust: Dict[str, List[Dict[str, Any]]] = {}
    for arr in all_arrangements:
        arr_by_cust.setdefault(arr["customer_key"], []).append(arr)

    cnt_by_cust: Dict[str, List[Dict[str, Any]]] = {}
    for cnt in all_contacts:
        cnt_by_cust.setdefault(cnt["customer_key"], []).append(cnt)

    for gc in golden_customers:
        c_arrs = arr_by_cust.get(gc["customer_key"], [])
        c_cnts = cnt_by_cust.get(gc["customer_key"], [])
        gc["arrangement_count"] = len(c_arrs)
        gc["arrangement_types"] = sorted({a["arrangement_type"] for a in c_arrs})
        gc["contact_count"] = len(c_cnts)

    # ── 5. EXPORT ALL 5 SILVER DOMAIN TABLES ──────────────────────────────────
    customer_fields = [
        "customer_key", "customer_type", "legal_name", "first_name", "last_name",
        "date_of_birth", "tax_id_normalized", "tax_id_masked", "primary_phone_e164",
        "primary_email", "primary_postal_code", "relationship_status",
        "survivorship_source", "contributing_sources", "source_record_count",
        "arrangement_count", "arrangement_types", "contact_count",
        "effective_from_ts", "effective_to_ts", "is_current", "dq_status",
    ]
    export_customers = []
    for gc in golden_customers:
        row = dict(gc)
        row["contributing_sources"] = "|".join(gc["contributing_sources"])
        row["arrangement_types"] = "|".join(gc.get("arrangement_types", []))
        export_customers.append(row)

    xref_fields = [
        "xref_key", "customer_key", "source_system", "source_entity",
        "source_customer_id", "match_method", "match_score", "review_status", "linked_at",
    ]
    arr_fields = [
        "arrangement_key", "customer_key", "source_system", "source_table", "source_account_id",
        "arrangement_type", "product_subtype", "status", "limit_or_amount",
        "branch_or_channel", "open_or_extract_date",
    ]
    cnt_fields = [
        "contact_key", "customer_key", "contact_type", "contact_usage",
        "contact_value_normalized", "source_system", "source_table",
        "verification_status", "observed_at",
    ]
    rev_fields = [
        "review_id", "candidate_a_system", "candidate_a_entity", "candidate_a_id", "candidate_a_name",
        "candidate_b_system", "candidate_b_entity", "candidate_b_id", "candidate_b_name",
        "match_rule", "similarity_score", "shared_blocking_key", "review_status", "created_at",
    ]

    write_csv(output_dir / "silver_customer.csv", export_customers, customer_fields)
    write_csv(output_dir / "silver_customer_source_xref.csv", all_xrefs, xref_fields)
    write_csv(output_dir / "silver_customer_arrangement.csv", all_arrangements, arr_fields)
    write_csv(output_dir / "silver_customer_contact.csv", all_contacts, cnt_fields)
    write_csv(output_dir / "silver_steward_review_queue.csv", steward_queue, rev_fields)

    multi_source_entities = [g for g in golden_customers if g["source_record_count"] > 1]
    dedup_ratio = (
        ((party_records_ingested - len(golden_customers)) / party_records_ingested) * 100
        if party_records_ingested
        else 0.0
    )

    return {
        "architecture_model": "N_SOURCES_TO_MANY_TABLES_TO_DOMAIN_TO_MULTIPLE_SILVER_TABLES",
        "industry": spec.get("industry", industry),
        "domain": spec.get("domain", domain),
        "domain_display_name": spec.get("domain_display_name", "Customer 360 Domain"),
        "source_mode": source_mode,
        "source_system_count": len(source_counts),
        "total_bronze_tables": len(loaded_tables),
        "total_all_table_rows": total_all_table_rows,
        "source_counts": source_counts,
        "source_table_counts": source_table_counts,
        "total_bronze_records": party_records_ingested,
        "unified_customers": len(golden_customers),
        "deduplicated_count": party_records_ingested - len(golden_customers),
        "dedup_ratio_pct": round(dedup_ratio, 1),
        "multi_source_customers": len(multi_source_entities),
        "total_xrefs": len(all_xrefs),
        "total_arrangements": len(all_arrangements),
        "total_contacts": len(all_contacts),
        "total_steward_reviews": len(steward_queue),
        "target_silver_tables": [t["table_name"] for t in (spec.get("target_silver_tables") or [])],
        "golden_customers": golden_customers,
        "xrefs": all_xrefs,
        "arrangements": all_arrangements,
        "contacts": all_contacts,
        "steward_review_queue": steward_queue,
    }


if __name__ == "__main__":
    run_conformance_pipeline()
