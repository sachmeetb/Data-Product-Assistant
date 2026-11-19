"""
ingestion — Automated Multi-Table Bronze Ingestion Engine (Batch, Streaming & Kafka).
"""

from .base import attach_ingestion_envelope, materialize_bronze_table
from .batch_connector import BatchIngestionConnector
from .kafka_connector import KafkaStreamingConnector
from .orchestrator import AutomatedBronzeOrchestrator, MULTI_TABLE_SOURCE_SPECS

__all__ = [
    "attach_ingestion_envelope",
    "materialize_bronze_table",
    "BatchIngestionConnector",
    "KafkaStreamingConnector",
    "AutomatedBronzeOrchestrator",
    "MULTI_TABLE_SOURCE_SPECS",
]
