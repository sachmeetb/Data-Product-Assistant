"""The medallion landing envelope, defined exactly once.

SILVER defined its envelope twice and the two copies disagreed: validation rule
GR001 required `load_ts` / `source_system_id` / `dq_issues` while the emitter
wrote `silver_load_ts` / `source_record_id` / `failed_rules`. Nothing caught it,
because rule checking was delegated to the LLM.

Every consumer -- the DDL emitter, the pipeline IR, and the validation rule --
derives from this module. That makes the mismatch unrepresentable.
"""

from __future__ import annotations

from core.model.provenance import Origin, Provenance
from core.model.tree import Attribute, Layer
from core.model.types import CanonicalType


class DQStatus:
    PASS = "PASS"
    WARN = "WARN"
    QUARANTINE = "QUARANTINE"     # held back from the next layer

    ALL = (PASS, WARN, QUARANTINE)


def _envelope(origin_detail: str, specs: list[tuple[str, CanonicalType, bool, str]]):
    return [
        Attribute(
            name=name,
            canonical_type=ctype,
            nullable=nullable,
            description=description,
            provenance=Provenance(origin=Origin.PACK_BLOCK, detail=origin_detail),
            block="technical-metadata",
        )
        for name, ctype, nullable, description in specs
    ]


_BRONZE = [
    ("bronze_ingest_ts", CanonicalType.TIMESTAMP, False, "When this row landed in bronze"),
    ("source_extract_ts", CanonicalType.TIMESTAMP, True, "When the source system produced it"),
    ("source_file_reference", CanonicalType.STRING, True, "Origin object or table, for replay"),
    ("source_system_id", CanonicalType.STRING, False, "Identifier of the originating system"),
    ("source_record_id", CanonicalType.STRING, True, "Natural key from the source, unmodified"),
    ("dq_status", CanonicalType.STRING, False, f"One of {', '.join(DQStatus.ALL)}"),
    ("dq_failed_rules", CanonicalType.ARRAY, True, "Ids of DQ rules this row failed"),
    ("ingest_batch_id", CanonicalType.STRING, False, "Batch this row arrived in"),
]

_SILVER = [
    ("silver_load_ts", CanonicalType.TIMESTAMP, False, "When this row was written to silver"),
    ("source_system_id", CanonicalType.STRING, False, "Propagated from bronze"),
    ("source_record_id", CanonicalType.STRING, True, "Propagated from bronze"),
    ("dq_status", CanonicalType.STRING, False, f"One of {', '.join(DQStatus.ALL)}"),
    ("dq_failed_rules", CanonicalType.ARRAY, True, "Ids of DQ rules this row failed"),
    ("record_hash", CanonicalType.STRING, True, "Change-detection hash over business columns"),
]

_GOLD = [
    ("gold_load_ts", CanonicalType.TIMESTAMP, False, "When this row was written to gold"),
    ("lineage_run_id", CanonicalType.STRING, False, "Pipeline run that produced this row"),
]


def envelope_for(layer: Layer) -> list[Attribute]:
    """The required technical columns for a layer."""
    specs = {
        Layer.BRONZE: (_BRONZE, "bronze/envelope.py"),
        Layer.SILVER: (_SILVER, "bronze/envelope.py"),
        Layer.GOLD: (_GOLD, "bronze/envelope.py"),
    }[layer]
    return _envelope(specs[1], specs[0])


def required_names(layer: Layer) -> set[str]:
    """Names the validation rule checks for. Same source as the emitter."""
    return {a.name for a in envelope_for(layer)}


def apply_envelope(table) -> list[str]:
    """Add any missing envelope columns to a table. Returns what was added."""
    added: list[str] = []
    for attribute in envelope_for(table.layer):
        if not table.has(attribute.name):
            table.attributes.append(attribute)
            added.append(attribute.name)
    return added


def missing_envelope(table) -> set[str]:
    """Envelope columns absent from a table. Drives the GR001-equivalent rule."""
    return required_names(table.layer) - {a.name for a in table.attributes}
