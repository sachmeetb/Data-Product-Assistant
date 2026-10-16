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
    generate_object_table_ddl,
    generate_dataplex_manifest_json,
)
from tools.schema_loader import (
    get_common_block_catalog,
    get_source_systems_catalog,
    get_domain_block_map,
    _flatten_ingestion_metadata,
    classify_source_modality,
)


def test_common_blocks():
    print("\n--- 1. Testing Common Blocks Catalog ---")
    catalog = get_common_block_catalog()
    assert len(catalog) >= 12, f"Expected at least 12 common blocks, found {len(catalog)}"
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
        "unstructured-object-metadata",
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
    assert "unstructured_docs" in sources, "Unstructured docs missing"
    assert "streaming_fraud" in sources, "Streaming fraud missing"

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


def test_unstructured_object_tables():
    print("\n--- 5. Testing Unstructured Data Ingestion (BigLake Object Tables) ---")
    mod = classify_source_modality("KYC Document Store", "customer passports and contract pdf scans")
    assert mod["structure"] == "UNSTRUCTURED", f"Expected UNSTRUCTURED, got {mod['structure']}"
    assert "OBJECT TABLE" in mod["modality_label"] or "UNSTRUCTURED" in mod["modality_label"]
    print(f"  [OK] Modality classified correctly: {mod['modality_label']}")

    sample_plan = {
        "tables": [
            {
                "table_name": "brz_unstr_kyc_documents",
                "source_system": "kyc_repo",
                "description": "Unstructured KYC Customer Verification Documents",
                "columns": [
                    {"name": "file_uri", "type": "STRING", "primary_key": True, "nullable": False},
                    {"name": "mime_type", "type": "STRING", "primary_key": False, "nullable": False},
                    {"name": "file_size_bytes", "type": "INT64", "primary_key": False, "nullable": False},
                    {"name": "sha256_hash", "type": "STRING", "primary_key": False, "nullable": False},
                    {"name": "document_type", "type": "STRING", "primary_key": False, "nullable": False},
                    {"name": "customer_id_ref", "type": "STRING", "primary_key": False, "nullable": False},
                    {"name": "classification", "type": "STRING", "primary_key": False, "nullable": False},
                    {"name": "extracted_text_payload", "type": "STRING", "primary_key": False, "nullable": True},
                    {"name": "ocr_confidence_score", "type": "FLOAT64", "primary_key": False, "nullable": True},
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
        "source_system": "unstr_kyc",
        "use_case_name": "Unstructured KYC Verification Document Storage",
    }

    # BigLake Object Table DDL
    object_ddl = generate_object_table_ddl(sample_plan, sample_bank)
    assert "object_metadata = 'DIRECTORY'" in object_ddl, "Missing object_metadata = 'DIRECTORY' in DDL"
    assert "WITH CONNECTION `eu.biglake-connection`" in object_ddl, "Missing BigLake connection in DDL"
    assert "table_retention_days = 2555" in object_ddl, "Missing 7-year retention in Object Table DDL"
    assert "gs://aib-data-lake-bronze/unstructured/kyc_repo/*" in object_ddl, "Incorrect unstructured GCS prefix"
    print("  [OK] BigLake Object Table DDL validated with directory metadata & 7-year retention")

    # Contract verification
    contract = generate_data_contract_dict(sample_plan, sample_bank, sample_req)
    model = contract["models"]["brz_unstr_kyc_documents"]
    assert model["tableFormat"] == "OBJECT_TABLE", f"Expected OBJECT_TABLE format, got {model['tableFormat']}"
    assert model["modality"]["structure"] == "UNSTRUCTURED"
    assert "biglake_object_table" in contract["servers"], "Missing biglake_object_table server in contract"
    
    dq_names = [q["name"] for q in contract["quality"]]
    assert "BRZ-DQ-007-OBJECT-TABLE-INTEGRITY" in dq_names, "Missing BRZ-DQ-007 rule"
    print("  [OK] ODCS v2.2 Contract correctly models OBJECT_TABLE and DQ assertion 007")

    # Dataplex Manifest verification
    manifest = json.loads(generate_dataplex_manifest_json(sample_plan, sample_bank))
    entity = manifest["entities"][0]
    assert entity["asset_type"] == "BIGLAKE_OBJECT_TABLE", f"Expected BIGLAKE_OBJECT_TABLE, got {entity['asset_type']}"
    assert "unstructured" in entity["lakehouse_storage_uri"]
    print("  [OK] Dataplex manifest classifies asset as BIGLAKE_OBJECT_TABLE")
    print("Unstructured Object Tables test PASSED.")


def test_streaming_modality_and_slas():
    print("\n--- 6. Testing Real-Time Streaming Ingestion & Sub-5m SLAs ---")
    mod = classify_source_modality("Kafka Event Mesh", "real-time card authorization and fraud stream")
    assert mod["cadence"] == "STREAMING_REALTIME", f"Expected STREAMING_REALTIME, got {mod['cadence']}"
    print(f"  [OK] Modality classified correctly: {mod['modality_label']}")

    sample_plan = {
        "tables": [
            {
                "table_name": "brz_stream_card_auth_events",
                "source_system": "streaming_fraud",
                "description": "Real-time card authorization fraud events stream",
                "columns": [
                    {"name": "ingest_batch_id", "type": "STRING", "primary_key": False, "nullable": False},
                    {"name": "ingest_ts", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                    {"name": "source_topic", "type": "STRING", "primary_key": False, "nullable": False},
                    {"name": "source_offset", "type": "INT64", "primary_key": False, "nullable": False},
                    {"name": "kafka_partition", "type": "INT64", "primary_key": False, "nullable": False},
                    {"name": "auth_request_id", "type": "STRING", "primary_key": True, "nullable": False},
                ],
            }
        ]
    }
    sample_bank = {
        "bank_name": "Apex International Bank",
        "bank_code": "AIB",
        "region": "eu",
        "regulatory_frameworks": ["PSR", "PRA", "FCA", "PCI-DSS"],
    }
    sample_req = {
        "source_system": "streaming_fraud",
        "use_case_name": "Real-time Card Authorization Fraud Detection",
    }

    contract = generate_data_contract_dict(sample_plan, sample_bank, sample_req)
    model = contract["models"]["brz_stream_card_auth_events"]
    assert model["modality"]["cadence"] == "STREAMING_REALTIME"
    assert contract["servicelevels"]["freshness"]["schedule"] == "STREAMING_REALTIME"
    assert contract["servicelevels"]["freshness"]["maxLag"] == "5m"
    print(f"  [OK] Streaming Data Contract enforces sub-5-minute SLA: maxLag = {contract['servicelevels']['freshness']['maxLag']}")

    manifest = json.loads(generate_dataplex_manifest_json(sample_plan, sample_bank))
    entity = manifest["entities"][0]
    assert entity["governance_aspects"]["sla_monitoring"]["freshness_max_lag"] == "5m"
    print("  [OK] Dataplex manifest enforces 5m freshness monitoring for streaming assets")
    print("Streaming Modality & SLA test PASSED.")


if __name__ == "__main__":
    test_common_blocks()
    test_source_systems_catalog()
    test_mandatory_envelope()
    test_odcs_contract_and_iceberg()
    test_unstructured_object_tables()
    test_streaming_modality_and_slas()
    print("\n========================================================")
    print("ALL MULTI-SOURCE & MULTI-MODAL BRONZE TESTS PASSED!")
    print("========================================================")

