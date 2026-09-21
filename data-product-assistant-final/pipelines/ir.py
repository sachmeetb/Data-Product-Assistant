"""Platform-agnostic pipeline intermediate representation.

Both predecessors had an LLM emit BigQuery SQL directly, which is why GOLD
needed `_rewrite_sql_to_bq` to undo Databricks table references after the fact.
Generating IR and rendering per target makes the engine a choice instead of a
rewrite.

No SQL and no engine syntax appears in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from core.model.tree import Layer
from core.model.types import CanonicalType


class SourceKind(str, Enum):
    OBJECT_STORE = "OBJECT_STORE"     # GCS, S3, ADLS
    RELATIONAL = "RELATIONAL"
    EXTERNAL_TABLE = "EXTERNAL_TABLE"
    STREAM = "STREAM"
    UPLOAD = "UPLOAD"
    SYNTHETIC = "SYNTHETIC"           # generated bronze, see bronze/synth


class Format(str, Enum):
    CSV = "CSV"
    JSON = "JSON"
    JSONL = "JSONL"
    PARQUET = "PARQUET"
    AVRO = "AVRO"
    ORC = "ORC"
    TABLE = "TABLE"


class WriteMode(str, Enum):
    APPEND = "APPEND"
    OVERWRITE = "OVERWRITE"
    MERGE = "MERGE"
    SCD2 = "SCD2"


class StepKind(str, Enum):
    CAST = "CAST"
    RENAME = "RENAME"
    DEDUPE = "DEDUPE"
    FILTER = "FILTER"
    JOIN = "JOIN"
    DERIVE = "DERIVE"
    EXPLODE = "EXPLODE"
    PIVOT = "PIVOT"
    AGGREGATE = "AGGREGATE"
    UNION = "UNION"
    SCD2 = "SCD2"
    QUARANTINE_SPLIT = "QUARANTINE_SPLIT"


class Severity(str, Enum):
    WARN = "WARN"
    QUARANTINE = "QUARANTINE"
    FAIL = "FAIL"


class ScheduleKind(str, Enum):
    MANUAL = "MANUAL"
    CRON = "CRON"
    EVENT = "EVENT"


@dataclass
class FieldSpec:
    name: str
    canonical_type: CanonicalType
    nullable: bool = True


@dataclass
class Source:
    id: str
    kind: SourceKind
    format: Format
    location: str                       # URI, table ref, or topic
    fields: list[FieldSpec] = field(default_factory=list)
    watermark_field: str | None = None
    options: dict[str, str] = field(default_factory=dict)


@dataclass
class Target:
    id: str
    layer: Layer
    dataset: str
    table: str
    write_mode: WriteMode = WriteMode.APPEND
    partition_by: str | None = None
    cluster_by: list[str] = field(default_factory=list)
    fields: list[FieldSpec] = field(default_factory=list)


@dataclass
class Step:
    id: str
    kind: StepKind
    inputs: list[str]                   # source/step ids
    output: str
    #: Kind-specific, deliberately untyped: renderers interpret per StepKind.
    #: e.g. DEDUPE {keys, order_by}; JOIN {left_on, right_on, how}
    params: dict = field(default_factory=dict)


@dataclass
class DQRule:
    id: str
    target: str                         # step or target id
    column: str | None
    check: str                          # canonical check name, e.g. "not_null"
    severity: Severity = Severity.WARN
    params: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class Schedule:
    kind: ScheduleKind = ScheduleKind.MANUAL
    expression: str | None = None       # cron string, or event topic


@dataclass
class LineageEdge:
    """Attribute-level lineage. Lets the UI answer "where did this come from"."""

    from_layer: Layer
    from_table: str
    from_attribute: str
    to_layer: Layer
    to_table: str
    to_attribute: str
    via_step: str | None = None


@dataclass
class PipelineSpec:
    id: str
    name: str
    sources: list[Source] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    dq: list[DQRule] = field(default_factory=list)
    schedule: Schedule = field(default_factory=Schedule)
    lineage: list[LineageEdge] = field(default_factory=list)

    def validate(self) -> list[str]:
        """Referential problems, as human-readable complaints."""
        problems: list[str] = []

        ids: set[str] = set()
        for collection, label in (
            (self.sources, "source"),
            (self.targets, "target"),
            (self.steps, "step"),
        ):
            for item in collection:
                if item.id in ids:
                    problems.append(f"duplicate id {item.id!r} ({label})")
                ids.add(item.id)

        available = {s.id for s in self.sources}
        for step in self.steps:
            for ref in step.inputs:
                if ref not in available:
                    problems.append(
                        f"step {step.id!r} consumes {ref!r} before it is produced"
                    )
            available.add(step.output)

        for rule in self.dq:
            if rule.target not in available and rule.target not in {t.id for t in self.targets}:
                problems.append(f"dq rule {rule.id!r} targets unknown {rule.target!r}")

        if not self.targets:
            problems.append("pipeline has no targets")

        return problems


class Renderer:
    """Base for engine-specific renderers. See pipelines/renderers/."""

    target_name: str = ""

    def render(self, spec: PipelineSpec) -> dict[str, str]:
        """Return {filename: contents}."""
        raise NotImplementedError
