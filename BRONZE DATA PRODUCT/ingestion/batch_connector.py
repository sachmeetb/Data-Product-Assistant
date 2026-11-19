"""
batch_connector.py — Automated Multi-Table Batch Ingestion Connector (CSV, Copybook, Parquet).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import materialize_bronze_table


class BatchIngestionConnector:
    """Automated batch connector that ingests 1-to-many files per source system into Bronze."""

    def __init__(self, samples_dir: Optional[Path] = None):
        base = Path(__file__).resolve().parent.parent
        self.samples_dir = samples_dir or (base / "samples" / "banking_multi_table")

    def read_batch_file(self, file_name: str, custom_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
        target_dir = custom_dir or self.samples_dir
        fpath = target_dir / file_name
        if not fpath.exists():
            fallback = Path(__file__).resolve().parent.parent / "samples" / "banking_4_sources" / file_name
            if fallback.exists():
                fpath = fallback
            else:
                return []
        with open(fpath, "r", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def ingest_table(
        self,
        source_system: str,
        table_name: str,
        file_name: str,
        feed_name: str,
        description: str,
        ingestion_mode: str = "BATCH_SCHEDULED",
        catalog: Optional[str] = None,
        schema_name: Optional[str] = None,
        custom_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        rows = self.read_batch_file(file_name, custom_dir=custom_dir)
        return materialize_bronze_table(
            table_name=table_name,
            rows=rows,
            source_system=source_system,
            feed_name=feed_name,
            description=description,
            ingestion_mode=ingestion_mode,
            catalog=catalog,
            schema_name=schema_name,
        )
