"""
test_multi_source_bronze.py — Verification test suite for Bronze Data Product multi-source expansion.
"""

import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import json
from tools.schema_loader import (
    get_common_block_catalog,
    get_source_systems_catalog,
    get_domain_block_map,
    _flatten_ingestion_metadata,
)
from tools.datacontract_generator import (
    generate_data_contract_dict,
    generate_data_contract_yaml,
)
from tools.artifact_generator import (
    generate_iceberg_ddl,
    generate_dataplex_manifest_json,
)


def test_common_blocks():
    print("\n--- 1. Testing Common Blocks Catalog ---")
    catalog = get_common_block_catalog()
    assert len(catalog) >= 11, f"Expected at least 11 common blocks, found {len(catalog)}"
    expected_blocks = [
        "ingestion-metadata",
        "mainframe-copybook",
        "swift-envelope",
        "sap-idoc-metadata",
        "api-crm-metadata",
        "quality-flags",
        "raw-payload",
        "source-identifier",
        "temporal",
        "file-metadata",
        "lineage-tracking",
    ]
    for b in expected_blocks:
        assert b in catalog, f"Missing common block: {b}"
        print(f"  [OK] Common block '{b}' loaded: {len(catalog[b]['columns'])} columns")
    print("Catalog test PASSED.")


def test_source_systems_catalog():
    print("\n--- 2. Testing Source Systems & Banking Standards Catalog ---")
    data = get_source_systems_catalog()
    sources = data.get("source_systems", {})
    assert "hogan" in sources, "Hogan system missing"
    assert "sap" in sources, "SAP system missing"
    assert "salesforce" in sources, "Salesforce system missing"
    assert "swift" in sources, "SWIFT system missing"
    assert "faster_payments" in sources, "Faster Payments missing"
    assert "temenos" in sources, "Temenos system missing"

    for sys_key, profile in sources.items():
        print(f"  [OK] Source '{sys_key}': {profile['name']} | Feeds: {[e['table_name'] for e in profile['primary_entities']]}")
    print("Source Systems Catalog test PASSED.")


def test_mandatory_envelope():
    print("\n--- 3. Testing Tony D. Giordano Mandatory Ingestion Envelope ---")
    cols = _flatten_ingestion_metadata({})
    col_names = [c["name"] for c in cols]
    for req_field in ["ingest_batch_id", "ingest_ts", "source_file_name", "raw_payload_hash", "source_system"]:
        assert req_field in col_names, f"Mandatory envelope field '{req_field}' missing"
        print(f"  [OK] Ingestion envelope contains: {req_field}")
    print("Mandatory Ingestion Envelope test PASSED.")


def test_odcs_contract_and_iceberg():
    print("\n--- 4. Testing ODCS v2.2 Data Contract & BigLake Iceberg Specs ---")
    sample_plan = {
        "tables": [
            {
                "table_name": "brz_hogan_dd_account",
                "source_system": "HOGAN",
                "description": "Hogan Demand Deposit Account Master",
                "columns": [
                    {"name": "bronze_id", "type": "STRING", "primary_key": True, "nullable": False},
                    {"name": "ingest_batch_id", "type": "STRING", "primary_key": False, "nullable": False},
                    {"name": "ingest_ts", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                    {"name": "source_file_name", "type": "STRING", "primary_key": False, "nullable": True},
                    {"name": "raw_payload_hash", "type": "STRING", "primary_key": False, "nullable": False},
                    {"name": "source_system", "type": "STRING", "primary_key": False, "nullable": False},
                    {"name": "DD_ACCT_NUM", "type": "STRING", "primary_key": True, "nullable": False, "pic_clause": "PIC X(10)"},
                    {"name": "DD_CUST_NUM", "type": "STRING", "primary_key": False, "nullable": False, "pic_clause": "PIC X(12)"},
                    {"name": "DD_CURR_BAL", "type": "STRING", "primary_key": False, "nullable": False, "pic_clause": "PIC S9(11)V99 COMP-3"},
                ],
            }
        ]
    }
    sample_bank = {
        "bank_name": "Apex International Bank",
        "bank_code": "AIB",
        "region": "eu",
        "regulatory_frameworks": ["PRA", "FCA", "Basel III", "GDPR"],
    }
    sample_req = {
        "source_system": "hogan",
        "use_case_name": "Hogan Demand Deposit Core Ingestion",
    }

    contract = generate_data_contract_dict(sample_plan, sample_bank, sample_req)
    assert contract["dataContractSpecification"] == "2.2.0", f"Unexpected ODCS version: {contract['dataContractSpecification']}"
    assert contract["servicelevels"]["retention"]["period"] == "7_YEARS", "Expected 7-year retention"
    assert contract["servers"]["biglake_iceberg"]["tableFormat"] == "ICEBERG", "Expected BigLake Iceberg server format"
    assert len(contract["quality"]) >= 5, f"Expected >= 5 quality assertions, got {len(contract['quality'])}"
    print("  [OK] ODCS v2.2 Contract generated successfully")
    print(f"  [OK] Contract ID: {contract['id']}")
    print(f"  [OK] Retention: {contract['servicelevels']['retention']['days']} days ({contract['servicelevels']['retention']['period']})")
    print(f"  [OK] Lakehouse target: {contract['servers']['biglake_iceberg']['storageBucket']}")

    # Iceberg DDL
    iceberg_sql = generate_iceberg_ddl(sample_plan, sample_bank)
    assert "format = 'ICEBERG'" in iceberg_sql, "Missing format = 'ICEBERG' in DDL"
    assert "table_retention_days = 2555" in iceberg_sql, "Missing table_retention_days = 2555 in DDL"
    assert "CREATE EXTERNAL TABLE" in iceberg_sql, "Missing CREATE EXTERNAL TABLE in DDL"
    print("  [OK] BigLake Apache Iceberg DDL validated with retention clause")

    # Dataplex Manifest
    dataplex_json = generate_dataplex_manifest_json(sample_plan, sample_bank)
    manifest = json.loads(dataplex_json)
    assert manifest["zone"] == "bronze-raw-landing-zone"
    assert len(manifest["entities"]) == 1
    assert manifest["entities"][0]["asset_type"] == "BIGLAKE_ICEBERG_TABLE"
    print("  [OK] Google Cloud Dataplex & Knowledge Catalog manifest generated successfully")
    print("ODCS and Iceberg tests PASSED.")


if __name__ == "__main__":
    test_common_blocks()
    test_source_systems_catalog()
    test_mandatory_envelope()
    test_odcs_contract_and_iceberg()
    print("\nALL MULTI-SOURCE BRONZE TESTS PASSED!")
