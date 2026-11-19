"""
test_n_to_m_conformance.py — Automated verification for Declarative Cross-Industry Mapping,
N-to-M STTM Generation, and 5-Table Silver Domain Mastering (Customer 360 & Water Account 360).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["BQ_OFFLINE_MODE"] = "1"

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from engine.pipeline_runner import run_conformance_pipeline
from engine.sttm_builder import build_multi_source_sttm_view
from mappings.registry import list_available_mappings, load_domain_mapping


def test_declarative_mapping_registry():
    available = list_available_mappings()
    domains = {(m["industry"], m["domain"]) for m in available}
    assert ("banking", "customer_360") in domains
    assert ("energy_industrial", "account_360") in domains

    banking_spec = load_domain_mapping("banking", "customer_360")
    assert len(banking_spec["source_systems"]) == 4
    assert sum(len(s["tables"]) for s in banking_spec["source_systems"]) == 12
    assert len(banking_spec["target_silver_tables"]) == 5


def test_n_to_m_sttm_builder_banking_and_water():
    b_sttm = build_multi_source_sttm_view(industry="banking", domain="customer_360")
    assert b_sttm["architecture_model"] == "N_SOURCES_TO_MANY_TABLES_TO_DOMAIN_TO_MULTIPLE_SILVER_TABLES"
    assert "4-to-1" not in b_sttm["title"]
    assert len(b_sttm["systems_hierarchy"]) == 4
    assert len(b_sttm["bronze_tables"]) == 12
    assert len(b_sttm["silver_tables"]) == 5
    assert b_sttm["mapping_count"] > 20

    w_sttm = build_multi_source_sttm_view(industry="water", domain="account_360")
    assert len(w_sttm["systems_hierarchy"]) == 3
    assert len(w_sttm["bronze_tables"]) == 4
    assert len(w_sttm["silver_tables"]) == 5


def test_end_to_end_conformance_banking_customer_360():
    res = run_conformance_pipeline(industry="banking", domain="customer_360")
    assert res["architecture_model"] == "N_SOURCES_TO_MANY_TABLES_TO_DOMAIN_TO_MULTIPLE_SILVER_TABLES"
    assert res["source_system_count"] == 4
    assert res["total_bronze_tables"] == 12
    assert res["total_bronze_records"] == 19
    assert res["unified_customers"] == 9
    assert res["total_xrefs"] == 22  # 19 primary party rows + 3 matched AFS guarantors
    assert res["total_arrangements"] == 29  # 7 DDA + 6 AFS Facility + 4 AFS Collateral + 6 TSYS Card + 6 ACLS Loan
    assert res["total_contacts"] >= 20  # E.164 phones + emails + CIS addresses + Kafka OTP mobiles
    assert res["total_steward_reviews"] >= 1  # Elena Rostov (AFS Guarantor) <-> Elena Rostova (TSYS)
    assert len(res["target_silver_tables"]) == 5


def test_end_to_end_conformance_water_account_360():
    res = run_conformance_pipeline(industry="water", domain="account_360")
    assert res["industry"] == "energy_industrial"
    assert res["domain"] == "account_360"
    assert res["source_system_count"] == 3
    assert res["total_bronze_tables"] == 4
    assert res["unified_customers"] == 2
    assert res["total_xrefs"] == 6
    assert res["total_arrangements"] == 4
    assert res["total_contacts"] >= 4
