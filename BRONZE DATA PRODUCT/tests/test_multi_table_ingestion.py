"""
test_multi_table_ingestion.py — Automated verification for 1-to-Many Bronze Modeling,
Batch Ingestion, and Real-Time Kafka Streaming Ingestion.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["BQ_OFFLINE_MODE"] = "1"

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from ingestion.batch_connector import BatchIngestionConnector
from ingestion.kafka_connector import KafkaStreamingConnector
from ingestion.orchestrator import AutomatedBronzeOrchestrator


def test_source_systems_catalog_1_to_many():
    catalog_path = BASE_DIR / "knowledge" / "source_systems_catalog.json"
    with open(catalog_path, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    systems = catalog["source_systems"]
    assert len(systems["afs"]["primary_entities"]) == 4
    assert len(systems["hogan"]["primary_entities"]) == 3
    assert len(systems["tsys"]["primary_entities"]) == 3
    assert len(systems["acls"]["primary_entities"]) == 2


def test_kafka_connector_replay_and_dlq():
    kafka_conn = KafkaStreamingConnector()
    valid, dlq, mode = kafka_conn.consume_or_replay(
        topic="payments.tsys.card.auth.v1",
        jsonl_file="brz_tsys_auth_events.jsonl",
    )
    assert mode == "kafka_stream_replay"
    assert len(valid) == 6
    assert len(dlq) == 0
    assert valid[0]["kafka_topic"] == "payments.tsys.card.auth.v1"

    # Test DLQ routing with malformed event
    valid2, dlq2, mode2 = kafka_conn.consume_or_replay(
        topic="payments.tsys.card.auth.v1",
        custom_events=[
            {"auth_event_id": "AUTH-TEST-1", "cardholder_id": "TSYS-88201", "auth_amount": "99.50"},
            {"malformed_without_id": True},
        ],
    )
    assert mode2 == "kafka_custom_injection"
    assert len(valid2) == 1
    assert len(dlq2) == 1
    assert dlq2[0]["error_reason"] == "MISSING_REQUIRED_KEY_OR_CORRUPT_JSON"


def test_automated_bronze_orchestrator_banking_and_water():
    orchestrator = AutomatedBronzeOrchestrator()

    banking_res = orchestrator.run_automated_ingestion(industry_domain="banking")
    assert banking_res["status"] == "landed"
    assert banking_res["architecture_model"] == "1_TO_MANY_PER_SOURCE_SYSTEM"
    assert banking_res["source_system_count"] == 4
    assert banking_res["total_tables"] == 12
    assert banking_res["batch_tables_count"] == 11
    assert banking_res["streaming_tables_count"] == 1
    assert banking_res["total_rows"] > 50

    water_res = orchestrator.run_automated_ingestion(industry_domain="water")
    assert water_res["status"] == "landed"
    assert water_res["source_system_count"] == 3
    assert water_res["total_tables"] == 4
    assert water_res["total_rows"] == 8
