"""
kafka_connector.py — Dual-Mode Real-Time / Kafka Streaming Bronze Ingestion Connector.

Supports:
  1. Live Kafka Consumer mode when `KAFKA_BOOTSTRAP_SERVERS` is configured and `confluent_kafka` is installed.
  2. Deterministic Event-Stream Replay mode from `.jsonl` stream files for zero-infrastructure demos.
  3. Dead-Letter Queue (DLQ) routing for malformed stream events.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import materialize_bronze_table


class KafkaStreamingConnector:
    """Real-time Kafka / event stream connector for Bronze tables with offset & partition lineage."""

    def __init__(self, samples_dir: Optional[Path] = None):
        base = Path(__file__).resolve().parent.parent
        self.samples_dir = samples_dir or (base / "samples" / "banking_multi_table")
        self.bootstrap_servers = (os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or "").strip()

    def consume_or_replay(
        self,
        topic: str,
        jsonl_file: str = "brz_tsys_auth_events.jsonl",
        custom_events: Optional[List[Dict[str, Any]]] = None,
        max_messages: int = 100,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
        """
        Return (valid_events, dlq_events, stream_mode) where stream_mode is
        'live_kafka_broker' or 'kafka_stream_replay'.
        """
        raw_events: List[Any] = []
        stream_mode = "kafka_stream_replay"

        if custom_events is not None:
            raw_events = list(custom_events)
            stream_mode = "kafka_custom_injection"
        elif self.bootstrap_servers:
            try:
                from confluent_kafka import Consumer  # type: ignore

                conf = {
                    "bootstrap.servers": self.bootstrap_servers,
                    "group.id": "bronze-lakehouse-ingestor",
                    "auto.offset.reset": "earliest",
                }
                consumer = Consumer(conf)
                consumer.subscribe([topic])
                for _ in range(max_messages):
                    msg = consumer.poll(0.25)
                    if msg is None or msg.error():
                        break
                    payload = json.loads(msg.value().decode("utf-8"))
                    payload["kafka_topic"] = msg.topic()
                    payload["kafka_partition"] = str(msg.partition())
                    payload["kafka_offset"] = str(msg.offset())
                    raw_events.append(payload)
                consumer.close()
                if raw_events:
                    stream_mode = "live_kafka_broker"
            except Exception:
                raw_events = []

        if not raw_events:
            fpath = self.samples_dir / jsonl_file
            if fpath.exists():
                with open(fpath, "r", encoding="utf-8") as f:
                    for idx, line in enumerate(f):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw_events.append(json.loads(line))
                        except json.JSONDecodeError:
                            raw_events.append({"_corrupt_payload": line, "_line": idx + 1})

        valid_events: List[Dict[str, Any]] = []
        dlq_events: List[Dict[str, Any]] = []
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for idx, ev in enumerate(raw_events):
            if not isinstance(ev, dict) or "_corrupt_payload" in ev or not ev.get("auth_event_id"):
                dlq_events.append({
                    "dlq_id": f"DLQ-{idx + 1:04d}",
                    "kafka_topic": topic,
                    "error_reason": "MISSING_REQUIRED_KEY_OR_CORRUPT_JSON",
                    "raw_payload": json.dumps(ev) if isinstance(ev, dict) else str(ev),
                    "failed_at": now_ts,
                })
                continue

            row = dict(ev)
            row.setdefault("kafka_topic", topic)
            row.setdefault("kafka_partition", str(idx % 3))
            row.setdefault("kafka_offset", str(950000 + idx))
            row.setdefault("event_ts", now_ts)
            valid_events.append(row)

        return valid_events, dlq_events, stream_mode

    def ingest_stream_table(
        self,
        source_system: str,
        table_name: str,
        topic: str,
        feed_name: str,
        description: str,
        jsonl_file: str = "brz_tsys_auth_events.jsonl",
        custom_events: Optional[List[Dict[str, Any]]] = None,
        catalog: Optional[str] = None,
        schema_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        valid_events, dlq_events, stream_mode = self.consume_or_replay(
            topic=topic,
            jsonl_file=jsonl_file,
            custom_events=custom_events,
        )
        res = materialize_bronze_table(
            table_name=table_name,
            rows=valid_events,
            source_system=source_system,
            feed_name=f"{feed_name} [{topic}]",
            description=description,
            ingestion_mode="KAFKA_STREAM",
            catalog=catalog,
            schema_name=schema_name,
        )
        res["kafka_topic"] = topic
        res["stream_mode"] = stream_mode
        res["dlq_count"] = len(dlq_events)
        if dlq_events:
            dlq_res = materialize_bronze_table(
                table_name="brz_dlq_events",
                rows=dlq_events,
                source_system=source_system,
                feed_name=f"DLQ:{topic}",
                description="Dead-Letter Queue for malformed Kafka streaming payloads",
                ingestion_mode="KAFKA_DLQ",
                catalog=catalog,
                schema_name=schema_name,
            )
            res["dlq_table"] = dlq_res["fqn"]
        return res
