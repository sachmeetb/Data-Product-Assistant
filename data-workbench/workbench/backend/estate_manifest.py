"""Offline estate manifest — the "DCAT-in-YAML" contract + pure converters.

Some client environments won't grant Data Workbench live credentials. Instead the
client runs a small, vetted, dependency-light extractor (``extraction_runners/``)
in their OWN environment, produces this **structured, human-reviewable** YAML, and
uploads it. This module is the SERVER-side contract:

  * fail-closed Pydantic models (mirrors :mod:`feasibility_spec` /
    :mod:`intake_blueprint`) — one bad entry rejects the whole document with a
    precise error rather than silently loading a half-manifest.
  * **pure converters** that turn a validated manifest into exactly the
    per-relation / per-column dict shape the live provider-driven scan
    (:func:`estate_scan.run_scan`) hands to :func:`estate.write_scan_snapshot`.

Because import is a deterministic REPLAY into the same graph writer, an imported
scan is indistinguishable from a live one — no synthetic provider, no fake
connection. The manifest is platform-agnostic (the platform is just a field), so
one format covers every supported platform.

Guiding invariant: ``classification`` is DERIVED on import via
:func:`estate.classify_column` — never trusted from the (client-authored) file.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MANIFEST_SCHEMA_VERSION = "1"
SUPPORTED_MANIFEST_VERSIONS = {"1"}

# Platforms whose live scan/import DW knows how to grade. Kept in sync with the
# scannable platforms in ``estate_scan`` / the provider registry.
SUPPORTED_PLATFORMS = {"postgres", "postgresql", "mysql", "snowflake",
                       "databricks", "duckdb"}

_VALID_KINDS = {"estate", "source"}          # source = Phase 2 greenfield
_VALID_RELATION_KINDS = {"table", "view", "materialized_view"}


class EstateManifestValidationError(ValueError):
    """Raised when a manifest dict / YAML does not conform to the schema.

    Surfaced to the operator as a manifest error — we never default-fill a
    malformed manifest into a half-populated shape (fail-closed, like
    :class:`feasibility_spec.FeasibilitySpecValidationError`)."""


# ── nested shapes ────────────────────────────────────────────────────────────

class TopValue(BaseModel):
    """One enumerated value + its frequency. Emitted ONLY for low-cardinality,
    non-PII categoricals; the client's extractor nulls these for anything past a
    cardinality/length cap or a PII-classified column."""
    model_config = ConfigDict(extra="ignore")
    value: Any = None
    count: int = 0
    frequency: float = 0.0


class ColumnProfile(BaseModel):
    """Guarded per-column profile. Count-shaped fields (``null_count`` /
    ``distinct_count``) are always safe; value-bearing fields (``min`` / ``max`` /
    ``top_values``) are present-but-null when the client redacted them (PII or
    over-cap). ``redacted`` records that a value-bearing output was withheld so the
    reviewer + the importer can see it."""
    model_config = ConfigDict(extra="ignore")
    null_count: Optional[int] = None
    null_rate: Optional[float] = None
    distinct_count: Optional[int] = None
    # value-bearing (redacted for PII / over-cap → present but null)
    min: Any = None
    max: Any = None
    top_values: Optional[list[TopValue]] = None
    # aggregate stats (safe — no raw value exposed)
    mean: Optional[float] = None
    stddev: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    avg_length: Optional[float] = None
    # redaction bookkeeping
    redacted: bool = False
    redaction_reason: str = ""


class ManifestColumn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    data_type: str = ""
    nullable: bool = True
    ordinal: Optional[int] = None
    comment: Optional[str] = None
    # Captured when the platform exposes it (the live scan omits PK — a bonus,
    # esp. for Phase 2 greenfield where the discovery loader consumes it).
    primary_key: bool = False
    profile: Optional[ColumnProfile] = None

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("column name must be non-empty")
        return v

    @field_validator("data_type", "comment", mode="before")
    @classmethod
    def _none_to_empty_str(cls, v: Any) -> Any:
        # comment stays Optional (None allowed) but a null data_type → "".
        return v


class ManifestForeignKey(BaseModel):
    model_config = ConfigDict(extra="ignore")
    from_column: str
    to_schema: str = ""
    to_table: str
    to_column: str = ""

    @field_validator("from_column", "to_table")
    @classmethod
    def _fk_ends_nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("foreign key needs a from_column and a to_table")
        return v


class ManifestCodeAsset(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    asset_kind: str = "procedure"
    namespace: Optional[str] = None
    language: Optional[str] = None
    schedule: Optional[str] = None
    definition_preview: Optional[str] = None   # bounded ~4 KB — NOT full source
    definition_hash: Optional[str] = None
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("code asset name must be non-empty")
        return v


class ManifestRelation(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    # ``schema`` shadows a BaseModel attribute → python name ``schema_name`` with a
    # wire alias of ``schema`` (populate_by_name lets tests use either).
    schema_name: str = Field(alias="schema")
    table: str
    relation_kind: str = "table"
    row_count: Optional[int] = None
    row_count_is_estimate: bool = False
    size_bytes: Optional[int] = None
    last_modified: Optional[str] = None
    num_files: Optional[int] = None
    comment: Optional[str] = None
    columns: list[ManifestColumn] = Field(default_factory=list)
    foreign_keys: list[ManifestForeignKey] = Field(default_factory=list)

    @field_validator("table")
    @classmethod
    def _table_nonempty(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("relation table name must be non-empty")
        return v

    @field_validator("relation_kind")
    @classmethod
    def _kind_valid(cls, v: str) -> str:
        v = (v or "table").strip().lower()
        if v not in _VALID_RELATION_KINDS:
            raise ValueError(
                f"relation_kind must be one of {sorted(_VALID_RELATION_KINDS)}, got {v!r}")
        return v


class ExtractionMeta(BaseModel):
    """What the client chose to include (the audit trail). All booleans; the
    ``redaction`` block is a free-form ``{reason: count}`` summary the reviewer
    reads to see what was withheld."""
    model_config = ConfigDict(extra="ignore")
    metadata: bool = True
    volumetrics: bool = True
    profiling: bool = True
    values_included: bool = True
    code_assets: bool = False
    redaction: dict[str, int] = Field(default_factory=dict)


class EstateManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    manifest_version: str = MANIFEST_SCHEMA_VERSION
    kind: str = "estate"
    platform: str
    catalog: Optional[str] = None       # the {db} URI segment (nullable for 2-level)
    generated_at: Optional[str] = None
    tool_version: str = ""
    extraction: ExtractionMeta = Field(default_factory=ExtractionMeta)
    relations: list[ManifestRelation] = Field(default_factory=list)
    code_assets: list[ManifestCodeAsset] = Field(default_factory=list)

    @field_validator("manifest_version", mode="before")
    @classmethod
    def _version_supported(cls, v: Any) -> str:
        v = str(v if v is not None else MANIFEST_SCHEMA_VERSION)
        if v not in SUPPORTED_MANIFEST_VERSIONS:
            raise ValueError(
                f"unsupported manifest_version {v!r} "
                f"(this Data Workbench supports {sorted(SUPPORTED_MANIFEST_VERSIONS)})")
        return v

    @field_validator("kind")
    @classmethod
    def _kind_valid(cls, v: str) -> str:
        v = (v or "estate").strip().lower()
        if v not in _VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_KINDS)}, got {v!r}")
        return v

    @field_validator("platform")
    @classmethod
    def _platform_supported(cls, v: str) -> str:
        p = (v or "").strip().lower()
        if not p:
            raise ValueError("platform is required")
        if p not in SUPPORTED_PLATFORMS:
            raise ValueError(
                f"unsupported platform {p!r} (supported: {sorted(SUPPORTED_PLATFORMS)})")
        return p

    @property
    def db_segment(self) -> str:
        """The ``{db}`` estate-URI segment — the catalog (3-level) or empty
        (2-level, where the connection's own database is the container)."""
        return (self.catalog or "").strip()


# ── validation entry points (fail-closed) ────────────────────────────────────

def parse_manifest(data: dict[str, Any]) -> EstateManifest:
    """Validate a raw dict into a typed :class:`EstateManifest` (fail-closed)."""
    if not isinstance(data, dict):
        raise EstateManifestValidationError("manifest must be a YAML/JSON object")
    try:
        return EstateManifest.model_validate(data)
    except ValidationError as e:
        raise EstateManifestValidationError(str(e)) from e


def load_manifest(raw_yaml: str) -> EstateManifest:
    """Parse a YAML document string → validated :class:`EstateManifest`.

    A YAML syntax error or a schema violation both raise
    :class:`EstateManifestValidationError` with a precise message."""
    try:
        data = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as e:
        raise EstateManifestValidationError(f"invalid YAML: {e}") from e
    if data is None:
        raise EstateManifestValidationError("manifest is empty")
    return parse_manifest(data)


# ── pure converters (manifest → the live-scan dict shapes) ────────────────────

def to_relation_dicts(
    manifest: EstateManifest,
    classify: Optional[Callable[[str], str]] = None,
) -> list[dict[str, Any]]:
    """Emit the exact per-relation / per-column dict shape
    :func:`estate_scan.run_scan` builds and :func:`estate.write_scan_snapshot`
    consumes — so import is a deterministic replay into the SAME writer.

    ``classify`` derives each column's ``classification`` (default
    :func:`estate.classify_column`, matching the live path). It's NEVER read from
    the manifest — a client-authored file cannot forge a governance label.
    """
    if classify is None:
        from .estate import classify_column as classify  # lazy → keep module light
    database = manifest.db_segment
    out: list[dict[str, Any]] = []
    for rel in manifest.relations:
        cols = [{
            "name": c.name,
            "data_type": c.data_type or "",
            "nullable": bool(c.nullable),
            "ordinal": c.ordinal,
            "classification": classify(c.name),
        } for c in rel.columns]
        out.append({
            "database": database,
            "schema": rel.schema_name,
            "table": rel.table,
            "relation_kind": rel.relation_kind,
            "row_count": rel.row_count,
            "size_bytes": rel.size_bytes,
            "last_modified": rel.last_modified,
            "num_files": rel.num_files,
            "row_count_is_estimate": bool(rel.row_count_is_estimate),
            "columns": cols,
        })
    return out


def to_fk_tuples(
    manifest: EstateManifest, *, estate_id: int, source_id: int,
) -> list[tuple[str, str, list[str], list[str]]]:
    """Build the consolidated ``(src_uri, tgt_uri, from_cols, to_cols)`` FK pairs
    :func:`estate.write_fk_edges` consumes, mirroring the live scan's §4c
    consolidation (multiple FK columns between the same pair fold onto one edge)."""
    from .estate import dataset_uri
    database = manifest.db_segment
    consolidated: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    for rel in manifest.relations:
        for fk in rel.foreign_keys:
            src_uri = dataset_uri(estate_id, source_id, database, rel.schema_name, rel.table)
            tgt_schema = fk.to_schema or rel.schema_name
            tgt_uri = dataset_uri(estate_id, source_id, database, tgt_schema, fk.to_table)
            key = (src_uri, tgt_uri)
            if key not in consolidated:
                consolidated[key] = ([], [])
            consolidated[key][0].append(fk.from_column)
            consolidated[key][1].append(fk.to_column or fk.from_column)
    return [(s, t, cols, ref) for (s, t), (cols, ref) in consolidated.items()]


def to_enrichment_relations(manifest: EstateManifest) -> list[dict[str, Any]]:
    """The per-relation extras (profiles + source comments + PK) that
    :func:`estate.apply_manifest_enrichment` layers onto the base graph AFTER
    ``write_scan_snapshot``. ``profile_json`` is the compact guarded profile as a
    JSON string (already redacted by the client); ``None`` when the column carried
    no profile."""
    import json as _json
    out: list[dict[str, Any]] = []
    for rel in manifest.relations:
        cols = []
        for c in rel.columns:
            profile_json = None
            if c.profile is not None:
                profile_json = _json.dumps(
                    c.profile.model_dump(exclude_none=True), default=str)
            cols.append({
                "name": c.name,
                "comment": c.comment,
                "profile_json": profile_json,
                "primary_key": bool(c.primary_key),
            })
        out.append({
            "schema": rel.schema_name,
            "table": rel.table,
            "comment": rel.comment,
            "columns": cols,
        })
    return out


# ── greenfield dpe-sa converters (manifest → discovery + profile YAML docs) ───
#
# Phase 2: the SAME manifest seeds a greenfield source-aligned (dpe-sa) project's
# catalog + profiling graph, deterministically, with NO live source — by emitting
# the exact ``data_discovery/<schema>__<table>.yaml`` (extract_metadata.py shape)
# and ``data_profiling/<schema>__<table>__profile.yaml`` (profile_table.py shape)
# docs the shipped loaders consume (`source_manifest_seed.py` runs them). Reuses
# ``intake_schema_seed``'s proven seed pattern; here the profiling docs are a genuine
# add (the schema-only migration seed had no profiles).

def _discovery_column_doc(col: "ManifestColumn", ordinal: int) -> dict[str, Any]:
    """One column in the discovery doc — mirrors extract_metadata.py's keys.
    Precision/length aren't in the manifest (null); type ← ``data_type``."""
    return {
        "name": col.name,
        "ordinal": col.ordinal if col.ordinal is not None else ordinal,
        "type": col.data_type or "",
        "character_maximum_length": None,
        "numeric_precision": None,
        "numeric_scale": None,
        "nullable": bool(col.nullable),
        "default": None,
        "comment": col.comment or None,
    }


def to_discovery_docs(manifest: EstateManifest) -> dict[str, dict[str, Any]]:
    """``{"<schema>__<table>.yaml": doc}`` in the discovery loader's shape
    (`data-discovery-to-dcat-neo4j` consumes it). Schema = the relation's own
    schema (matches what live postgres/snowflake discovery emits for the project
    graph `dataset:{pc}:{schema}.{table}` URI)."""
    docs: dict[str, dict[str, Any]] = {}
    for rel in manifest.relations:
        schema = rel.schema_name
        pk_cols = [c.name for c in rel.columns if c.primary_key]
        fks = []
        for fk in rel.foreign_keys:
            fks.append({
                "constraint_name": f"{rel.table}_{fk.from_column}_fkey",
                "columns": [fk.from_column],
                "referenced_schema": fk.to_schema or schema,
                "referenced_table": fk.to_table,
                "referenced_columns": [fk.to_column] if fk.to_column else [],
                "on_delete": "NO ACTION",
                "on_update": "NO ACTION",
            })
        docs[f"{schema}__{rel.table}.yaml"] = {
            "schema": schema,
            "table": rel.table,
            "comment": rel.comment or None,
            "columns": [_discovery_column_doc(c, i + 1) for i, c in enumerate(rel.columns)],
            "primary_key": ({"constraint_name": f"{rel.table}_pkey", "columns": pk_cols}
                            if pk_cols else None),
            "foreign_keys": fks,
            "unique_constraints": [],
            "indexes": [],
            "check_constraints": [],
        }
    return docs


# Profile metrics the DQV loader recognises (mirrors generate_dqv_cypher.KNOWN_METRICS,
# minus top_values which rides separately).
_PROFILE_METRIC_FIELDS = ("null_count", "null_rate", "distinct_count", "min", "max",
                          "mean", "stddev", "min_length", "max_length", "avg_length")


def to_profile_docs(manifest: EstateManifest) -> dict[str, dict[str, Any]]:
    """``{"<schema>__<table>__profile.yaml": doc}`` in the data-profiling shape
    (`data-profiling-to-dqv-neo4j` consumes it). Only columns that carry a profile
    are emitted; redacted value-bearing fields are already null so the loader skips
    them (it only writes non-null metrics)."""
    docs: dict[str, dict[str, Any]] = {}
    for rel in manifest.relations:
        cols = []
        for c in rel.columns:
            if c.profile is None:
                continue
            entry: dict[str, Any] = {"name": c.name, "type": c.data_type or ""}
            pd = c.profile.model_dump()
            for m in _PROFILE_METRIC_FIELDS:
                if pd.get(m) is not None:
                    entry[m] = pd[m]
            tv = c.profile.top_values
            if tv:
                entry["top_values"] = [
                    {"value": t.value, "count": t.count, "frequency": t.frequency} for t in tv]
            cols.append(entry)
        if not cols:
            continue
        docs[f"{rel.schema_name}__{rel.table}__profile.yaml"] = {
            "schema": rel.schema_name,
            "table": rel.table,
            "profiled_at": manifest.generated_at or "",
            "row_count": rel.row_count if rel.row_count is not None else 0,
            "sample_size": rel.row_count if rel.row_count is not None else 0,
            "columns": cols,
        }
    return docs


def to_code_asset_summaries(manifest: EstateManifest) -> list[Any]:
    """Convert manifest code-assets → :class:`platform.interfaces.CodeAssetSummary`
    objects :func:`estate.write_code_asset_snapshot` consumes."""
    from .platform.interfaces import CodeAssetSummary, NamespaceRef
    out: list[Any] = []
    for a in manifest.code_assets:
        ns = None
        if a.namespace:
            ns = NamespaceRef(platform_instance_id="offline",
                              parts=[p for p in a.namespace.split(".") if p])
        out.append(CodeAssetSummary(
            name=a.name, asset_kind=a.asset_kind, namespace=ns,
            language=a.language, schedule=a.schedule,
            definition_preview=a.definition_preview,
            definition_hash=a.definition_hash,
            depends_on=list(a.depends_on or []),
        ))
    return out


# ── preview summary (parse + validate, no graph write) ────────────────────────

def summarize_manifest(manifest: EstateManifest) -> dict[str, Any]:
    """A side-effect-free digest for the upload PREVIEW: counts, per-schema
    breakdown, redaction summary, PII-flagged columns, and warnings. Mirrors the
    ODCS ``parse`` (no-write) half of the two-step import."""
    from .estate import classify_column
    schemas: dict[str, dict[str, int]] = {}
    total_cols = 0
    pii_columns: list[str] = []
    profiled_cols = 0
    redacted_cols = 0
    fk_edges = 0
    warnings: list[str] = []
    for rel in manifest.relations:
        s = schemas.setdefault(rel.schema_name, {"relations": 0, "columns": 0})
        s["relations"] += 1
        s["columns"] += len(rel.columns)
        total_cols += len(rel.columns)
        fk_edges += len(rel.foreign_keys)
        for c in rel.columns:
            if classify_column(c.name) == "pii":
                pii_columns.append(f"{rel.schema_name}.{rel.table}.{c.name}")
            if c.profile is not None:
                profiled_cols += 1
                if c.profile.redacted:
                    redacted_cols += 1
        if not rel.columns:
            warnings.append(f"{rel.schema_name}.{rel.table} has no columns")
    if not manifest.relations:
        warnings.append("manifest carries no relations — nothing would be imported")
    depth = "profiled" if (manifest.extraction.profiling and profiled_cols) else "metadata"
    return {
        "manifest_version": manifest.manifest_version,
        "kind": manifest.kind,
        "platform": manifest.platform,
        "catalog": manifest.catalog or "",
        "generated_at": manifest.generated_at,
        "tool_version": manifest.tool_version,
        "depth": depth,
        "counts": {
            "schemas": len(schemas),
            "relations": len(manifest.relations),
            "columns": total_cols,
            "profiled_columns": profiled_cols,
            "redacted_columns": redacted_cols,
            "fk_edges": fk_edges,
            "code_assets": len(manifest.code_assets),
            "pii_columns": len(pii_columns),
        },
        "schemas": [
            {"schema": name, **vals} for name, vals in sorted(schemas.items())
        ],
        "extraction": manifest.extraction.model_dump(),
        "redaction": dict(manifest.extraction.redaction or {}),
        "pii_columns": sorted(pii_columns)[:200],
        "warnings": warnings,
    }
