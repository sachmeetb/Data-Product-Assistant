"""Producer-side readiness translator + validator + scorer + persister.

Walks the producer-side graph (``:DataContract`` → ``:DProdDataProduct`` →
``:DProdOutputDataset`` → ``:DProdColumn``) and emits an OSI semantic-model
dict. Validates it against the vendored JSON Schema plus uniqueness,
reference, and SQL-parseability checks. Scores conformance + completeness
against the contract's selected **rubric** (``OSI`` by default; alternative
rubrics like ``AI-Ready`` live in ``playbook/scoring_rubrics/``) and
produces a Red/Amber/Green band. Persists the result as an
``:OsiEvaluation`` node attached to the specific ``:DataContract`` version
that was scored, mirroring the ``:QualityScore`` append-only ``batchId``
pattern.

The LLM advisor (which writes the narrative analysis and Apply cards for
new metrics / relationships / ai_context) lives separately in
``routers/osi.py`` and is only invoked for rubrics that ship one
(currently OSI only); this module is purely deterministic.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from jsonschema import Draft202012Validator
from sqlglot import parse_one
from sqlglot.errors import ParseError

from .config import BASE_DIR, BASE_PROJECT_DIR
from .models import Project
from .neo4j_client import neo4j_session


# ── Constants ────────────────────────────────────────────────────────────


OSI_VERSION = "0.1.1"
# Default rubric used when a :DataContract has no scoringRubric set. Kept
# stable through the rubric refactor so existing :OsiEvaluation trend lines
# don't break — pre-rubric evaluations carried this string verbatim.
EVALUATOR_VERSION = "osi-v0.1.1"
DEFAULT_RUBRIC = "osi"
DIALECT_FALLBACK = "ANSI_SQL"

# Maps sqlglot-recognised dialect names to OSI dialect enum values. sqlglot
# uses lowercase names; OSI uses uppercase enum strings.
SQLGLOT_DIALECT = {
    "ANSI_SQL": "",  # empty string = generic / ANSI in sqlglot
    "SNOWFLAKE": "snowflake",
    "DATABRICKS": "databricks",
    "TABLEAU": "",  # treat as generic for parseability
    "MDX": "",      # MDX isn't a SQL dialect; sqlglot can't validate, fall back
}

# Legacy WEIGHTS dict — preserved as the canonical OSI weights for any
# downstream code that imports it. The rubric YAML at
# ``playbook/scoring_rubrics/osi.yaml`` mirrors these values; if you change
# them, change the YAML too.
WEIGHTS = {
    "dataset_descriptions": 15,
    "field_descriptions": 25,
    "primary_keys": 10,
    "rich_expressions": 20,
    "relationships": 10,
    "metrics": 10,
    "ai_context": 10,
}

# Legacy RAG thresholds — defaults when a rubric doesn't override.
GREEN_THRESHOLD = 80
AMBER_THRESHOLD = 50


_PLAYBOOK_OSI = BASE_DIR / "playbook" / "osi"
_SCHEMA_PATH = _PLAYBOOK_OSI / "osi-schema.json"
_DIALECT_MAPPING_PATH = _PLAYBOOK_OSI / "dialect_mapping.yaml"
_PLAYBOOK_RUBRICS = BASE_DIR / "playbook" / "scoring_rubrics"


# ── Loaders ──────────────────────────────────────────────────────────────


_schema_cache: Optional[dict] = None
_dialect_map_cache: Optional[dict[str, str]] = None
_rubric_cache: dict[str, dict] = {}


def _load_schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        with _SCHEMA_PATH.open("r", encoding="utf-8") as fh:
            _schema_cache = json.load(fh)
    return _schema_cache


def _load_dialect_mapping() -> dict[str, str]:
    global _dialect_map_cache
    if _dialect_map_cache is None:
        try:
            with _DIALECT_MAPPING_PATH.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            data = {}
        _dialect_map_cache = {str(k).lower(): str(v) for k, v in data.items() if v}
    return _dialect_map_cache


def load_rubric(name: Optional[str]) -> dict:
    """Load a scoring rubric config by id from ``playbook/scoring_rubrics``.

    Falls back to the OSI rubric if ``name`` is None / blank / unknown.
    Caches on first read. Validates that every referenced predicate exists
    in PREDICATE_REGISTRY at load time so a typo in YAML fails loudly.
    """
    key = (name or DEFAULT_RUBRIC).strip().lower() or DEFAULT_RUBRIC
    if key in _rubric_cache:
        return _rubric_cache[key]
    path = _PLAYBOOK_RUBRICS / f"{key}.yaml"
    if not path.exists():
        # Unknown rubric → fall back to OSI to avoid breaking scoring for
        # contracts whose recorded rubric was renamed/removed. Log via the
        # returned config's 'id' so callers can distinguish.
        path = _PLAYBOOK_RUBRICS / f"{DEFAULT_RUBRIC}.yaml"
        key = DEFAULT_RUBRIC
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    # Validate predicate references — fail loudly on misconfig rather than
    # silently scoring a criterion as 0.
    for crit in data.get("criteria") or []:
        pred = crit.get("predicate")
        if pred not in PREDICATE_REGISTRY:
            raise ValueError(
                f"Rubric '{key}' references unknown predicate '{pred}' "
                f"for criterion '{crit.get('id', '?')}'. Register it in "
                "osi.PREDICATE_REGISTRY before referencing it from YAML."
            )
    _rubric_cache[key] = data
    return data


def list_rubrics() -> list[dict]:
    """Return all rubric configs found on disk. Used by the API endpoint
    that drives the wizard's rubric picker."""
    out: list[dict] = []
    if not _PLAYBOOK_RUBRICS.exists():
        return out
    for path in sorted(_PLAYBOOK_RUBRICS.glob("*.yaml")):
        try:
            out.append(load_rubric(path.stem))
        except (ValueError, yaml.YAMLError):
            continue
    return out


def _platform_to_dialect(platform: Optional[str]) -> str:
    if not platform:
        return DIALECT_FALLBACK
    return _load_dialect_mapping().get(platform.strip().lower(), DIALECT_FALLBACK)


# ── Neo4j helpers ────────────────────────────────────────────────────────


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host,
        project.neo4j_port,
        project.neo4j_user,
        project.neo4j_password,
        project.neo4j_database,
    )


# Read the contract head (isCurrent=true) so we always evaluate the version
# the wizard / engineer is currently editing. Marketplace pinning is the
# *read* concern (handled by marketplace.py); this is the *write* concern.
#
# scoringRubric coalesces to 'osi' for back-compat with contracts that
# predate the rubric field.
READ_CONTRACT_HEAD = """\
MATCH (dc:DataContract {id: $contract_id})
RETURN (dc.id + ':v' + toString(dc.currentVersion)) AS versioned_id,
       dc.currentVersion AS lifecycle_version,
       dc.currentLifecycleState AS lifecycle_state,
       dc.name AS name,
       dc.description AS description,
       dc.purpose AS purpose,
       dc.aiContextJson AS ai_context_json,
       coalesce(dc.scoringRubric, 'osi') AS scoring_rubric
"""


# Read just the rubric. Used by the evaluate() composite entrypoint before
# deciding whether to translate / validate / advise.
READ_CONTRACT_RUBRIC = """\
MATCH (dc:DataContract {id: $contract_id})
RETURN coalesce(dc.scoringRubric, 'osi') AS scoring_rubric
"""


READ_DPROD_DATASETS = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WITH ods, pc
ORDER BY ods.physicalName, coalesce(pc.ordinal, 0), pc.name
WITH ods, collect(pc) AS columns
RETURN ods.uri              AS dataset_uri,
       ods.name             AS dataset_name,
       ods.physicalName     AS physical_name,
       ods.description      AS description,
       columns
ORDER BY ods.physicalName
"""


# Pull foreign keys declared on ODCS schemas as a JSON blob. Multi-schema
# contracts may have FKs that cross datasets; we surface them as OSI
# relationships when both endpoints resolve to known datasets/fields.
READ_SCHEMA_FOREIGN_KEYS = """\
MATCH (dc:DataContract {id: $contract_id})
      -[:HAS_SCHEMA]->(s:DataContractSchema)
RETURN coalesce(s.physicalName, s.name, '') AS schema_phys,
       coalesce(s.foreignKeys, '[]')        AS foreign_keys_json
"""


# Advisor-applied OSI nodes (additive; live alongside :DataContract). Read
# them in the second pass so any Apply card the PO has accepted shows up
# in the next evaluation. Both keyed by contract_id (NOT versionedId) for
# now since they're not yet versioned with the contract.
READ_OSI_METRICS = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_METRIC]->(m:OsiMetric)
RETURN m.name        AS name,
       m.expression  AS expression,
       m.dialect     AS dialect,
       m.description AS description
"""


READ_OSI_RELATIONSHIPS = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_RELATIONSHIP]->(r:OsiRelationship)
RETURN r.name         AS name,
       r.fromDataset  AS from_dataset,
       r.toDataset    AS to_dataset,
       r.fromColumns  AS from_columns,
       r.toColumns    AS to_columns
"""


READ_SERVING_PLATFORM = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})-[:SERVED_BY]->(sd:ServingDefinition)
RETURN sd.targetPlatform AS platform
ORDER BY sd.servingMode
LIMIT 1
"""


# Latest engineer-authored mapping per :DProdColumn. The translator uses
# transformExpression / transformKind / transformParams / transformDecorators
# to upgrade the field's OSI expression beyond a bare column reference when
# the engineer has expressed an actual transform.
READ_MAPPINGS_FOR_CONTRACT = """\
MATCH (cm:ColumnMapping {isCurrent: true})-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
WHERE pc.uri STARTS WITH 'dprod:col:' + $contract_id + ':'
RETURN pc.uri                          AS pc_uri,
       cm.transformKind                AS transform_kind,
       cm.transformExpression          AS transform_expression,
       coalesce(cm.transformParams, '')     AS transform_params_json,
       coalesce(cm.transformDecorators, '') AS transform_decorators_json
"""


# ── Translator ───────────────────────────────────────────────────────────


def _parse_ai_context(raw: Any) -> Any:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw or None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw  # OSI accepts ai_context as a bare string
        if isinstance(parsed, (dict, str)):
            return parsed or None
    return None


def _is_trivial_expression(expression: str, column_name: str) -> bool:
    """A field expression is "trivial" if it's just the bare column name —
    i.e., no transform / cast / case / function applied. Used by the rich-
    expressions completeness criterion."""
    expr = (expression or "").strip()
    return expr.lower() == (column_name or "").strip().lower()


# Mapping kinds the engineer's :ColumnMapping can carry. ``direct`` is a
# 1:1 copy and counts as trivial unless decorators wrap it. Every other
# kind is a semantically meaningful transform — credit the field.
_NON_TRIVIAL_KINDS = {
    "cast", "format", "concat", "split", "substring",
    "case", "arithmetic", "lookup", "literal", "expression",
}


def _build_field_expression(
    col: dict,
    mapping: Optional[dict],
    target_col_name: str,
) -> str:
    """Resolve the OSI ``field.expression`` text for a single product column.

    Order of precedence:
    1. Engineer's ``transformExpression`` (canonical SQL the mapping editor
       persists for cast/concat/case/arithmetic/lookup/expression/etc.).
    2. Literal value for ``transformKind='literal'`` (e.g. ``'USD'``).
    3. Decorator chain wrapped around the target column for direct+decorators.
    4. Synthesised ``CAST(<col> AS <type>)`` when the kind is ``cast`` and
       ``transformParams.target_type`` is known.
    5. Sentinel ``<col> /* osi: <kind> */`` for any other non-trivial kind
       lacking authored SQL — keeps the field rich (different from the bare
       target name) while staying sqlglot-parseable so conformance passes.
    6. PO's ``transformHint``.
    7. Bare target column name (trivial — direct mapping or no mapping yet).
    """
    target_name = target_col_name or ""

    if mapping is None:
        hint = (col.get("transformHint") or "").strip()
        return hint or target_name

    kind = (mapping.get("transform_kind") or "direct").lower()
    transform_expr = (mapping.get("transform_expression") or "").strip()

    # 1. Authored SQL wins — for any kind, if the engineer / AI mapping
    #    populated transformExpression, that's the canonical text.
    if transform_expr:
        return transform_expr

    # 2. Literal kind — emit the literal value verbatim. transformParams is
    #    stored as a JSON string with the literal already SQL-quoted by the
    #    engineer (e.g. 'USD', 42, NULL).
    if kind == "literal":
        try:
            params = json.loads(mapping.get("transform_params_json") or "{}")
            lv = params.get("literal_value") if isinstance(params, dict) else None
            if lv is not None and str(lv).strip():
                return str(lv).strip()
        except json.JSONDecodeError:
            pass
        # Malformed params — fall through.

    decorators: list[str] = []
    try:
        raw_dec = mapping.get("transform_decorators_json") or "[]"
        parsed_dec = json.loads(raw_dec) if isinstance(raw_dec, str) else raw_dec
        if isinstance(parsed_dec, list):
            decorators = [str(d).upper().strip() for d in parsed_dec if str(d).strip()]
    except json.JSONDecodeError:
        decorators = []

    # 3. direct + decorators → wrap target column. Direct without decorators
    #    falls through to bare-name.
    if kind == "direct" and decorators:
        wrapped = target_name
        for dec in decorators:
            wrapped = f"{dec}({wrapped})"
        return wrapped

    # 4. Cast with known target type → emit a real CAST.
    if kind == "cast":
        try:
            params = json.loads(mapping.get("transform_params_json") or "{}")
            target_type = (params.get("target_type") or "").strip() if isinstance(params, dict) else ""
            if target_type:
                return f"CAST({target_name} AS {target_type})"
        except json.JSONDecodeError:
            pass

    # 5. Non-trivial kind without authored SQL — emit a sentinel that's
    #    distinct from the bare target name (so the rich-expression scorer
    #    counts it) and sqlglot-parseable (sqlglot strips SQL comments,
    #    leaving a valid identifier).
    if kind in _NON_TRIVIAL_KINDS:
        return f"{target_name} /* osi: {kind} */"

    # 6. PO hint, if present.
    hint = (col.get("transformHint") or "").strip()
    if hint:
        return hint

    # 7. Trivial — direct copy / rename / no mapping authored yet.
    return target_name


def translate_to_osi(project: Project, contract_id: str) -> dict:
    """Build an OSI v0.1.1 dict for the given contract's *current* version.

    Returns a dict shaped like ``{version, semantic_model: [{...}]}``. The
    dict is well-formed enough to feed straight into ``validate_osi``.
    Empty arrays (e.g. no metrics) are omitted to keep the YAML download
    clean; the validator treats their absence as scoring signal, not as a
    schema error.
    """
    osi: dict[str, Any] = {"version": OSI_VERSION, "semantic_model": []}

    with _neo4j(project) as ns:
        head = ns.run(READ_CONTRACT_HEAD, contract_id=contract_id).single()
        if not head:
            # No contract in the graph yet — return a stub the validator
            # will mark non-conformant (missing required datasets[]).
            osi["semantic_model"].append({
                "name": contract_id or "unknown",
                "datasets": [],
            })
            return osi

        sm: dict[str, Any] = {
            "name": head["name"] or contract_id,
            "datasets": [],
        }
        if head["description"]:
            sm["description"] = head["description"]
        ai_context = _parse_ai_context(head["ai_context_json"])
        if ai_context:
            sm["ai_context"] = ai_context

        platform_row = ns.run(READ_SERVING_PLATFORM, contract_id=contract_id).single()
        dialect = _platform_to_dialect(platform_row["platform"] if platform_row else None)

        # Build a per-:DProdColumn lookup of the engineer's latest authored
        # mapping. Lets the field expression reflect actual transforms (CAST,
        # CONCAT, literal values, decorators) rather than always falling back
        # to the bare column name.
        mapping_by_pc: dict[str, dict] = {}
        for m_row in ns.run(READ_MAPPINGS_FOR_CONTRACT, contract_id=contract_id):
            pc_uri = m_row["pc_uri"]
            if pc_uri:
                mapping_by_pc[pc_uri] = {
                    "transform_kind": m_row["transform_kind"],
                    "transform_expression": m_row["transform_expression"],
                    "transform_params_json": m_row["transform_params_json"],
                    "transform_decorators_json": m_row["transform_decorators_json"],
                }

        ds_rows = list(ns.run(READ_DPROD_DATASETS, contract_id=contract_id))
        dataset_lookup: dict[str, set[str]] = {}  # dataset_name → field names
        for row in ds_rows:
            phys = row["physical_name"] or row["dataset_name"] or ""
            if not phys:
                continue
            dataset: dict[str, Any] = {
                "name": phys,
                "source": phys,
            }
            if row["description"]:
                dataset["description"] = row["description"]

            primary_key: list[str] = []
            fields: list[dict[str, Any]] = []
            field_names: set[str] = set()
            for pc in row["columns"] or []:
                if pc is None:
                    continue
                col = dict(pc)
                col_name = col.get("name") or ""
                if not col_name:
                    continue
                if col_name in field_names:
                    continue
                field_names.add(col_name)
                if col.get("isPrimaryKey"):
                    primary_key.append(col_name)

                # Expression: derive from the engineer's :ColumnMapping when
                # present (CAST / CONCAT / literal / decorator-wrapped /
                # authored SQL), otherwise fall back to the PO's
                # transformHint, then bare name. _build_field_expression
                # handles every kind in the workbench's mapping DSL.
                expr_text = _build_field_expression(
                    col, mapping_by_pc.get(col.get("uri")), col_name
                )
                field: dict[str, Any] = {
                    "name": col_name,
                    "expression": {
                        "dialects": [
                            {"dialect": dialect, "expression": expr_text}
                        ]
                    },
                }
                if col.get("description"):
                    field["description"] = col["description"]
                fields.append(field)

            if primary_key:
                dataset["primary_key"] = primary_key
            if fields:
                dataset["fields"] = fields
            dataset_lookup[phys] = field_names
            sm["datasets"].append(dataset)

        # Foreign keys → relationships. ODCS stores FKs as a JSON blob on
        # :DataContractSchema; expand each row into one OSI relationship if
        # both endpoints resolve. Skip silently when malformed — better to
        # under-emit than to fail validation with junk references.
        relationships: list[dict[str, Any]] = []
        rel_names: set[str] = set()
        for fk_row in ns.run(READ_SCHEMA_FOREIGN_KEYS, contract_id=contract_id):
            schema_phys = fk_row["schema_phys"] or ""
            try:
                fks = json.loads(fk_row["foreign_keys_json"] or "[]")
            except json.JSONDecodeError:
                continue
            if not isinstance(fks, list):
                continue
            for i, fk in enumerate(fks):
                if not isinstance(fk, dict):
                    continue
                from_cols = fk.get("from_columns") or fk.get("columns") or []
                to_dataset = fk.get("to") or fk.get("references_table") or ""
                to_cols = fk.get("to_columns") or fk.get("references_columns") or []
                if not (isinstance(from_cols, list) and isinstance(to_cols, list)):
                    continue
                if not from_cols or not to_cols or len(from_cols) != len(to_cols):
                    continue
                if to_dataset not in dataset_lookup:
                    continue
                if not all(c in dataset_lookup.get(schema_phys, set()) for c in from_cols):
                    continue
                if not all(c in dataset_lookup[to_dataset] for c in to_cols):
                    continue
                base_name = fk.get("name") or f"{schema_phys}_to_{to_dataset}"
                name = base_name
                n = 1
                while name in rel_names:
                    n += 1
                    name = f"{base_name}_{n}"
                rel_names.add(name)
                relationships.append({
                    "name": name,
                    "from": schema_phys,
                    "to": to_dataset,
                    "from_columns": [str(c) for c in from_cols],
                    "to_columns": [str(c) for c in to_cols],
                })

        # Advisor-applied :OsiRelationship nodes (additive; the PO can keep
        # piling them on via Apply cards without touching the ODCS spec).
        for r in ns.run(READ_OSI_RELATIONSHIPS, contract_id=contract_id):
            name = r["name"] or ""
            if not name or name in rel_names:
                continue
            from_cols = r["from_columns"] or []
            to_cols = r["to_columns"] or []
            if not from_cols or not to_cols or len(from_cols) != len(to_cols):
                continue
            from_ds = r["from_dataset"] or ""
            to_ds = r["to_dataset"] or ""
            if from_ds not in dataset_lookup or to_ds not in dataset_lookup:
                continue
            rel_names.add(name)
            relationships.append({
                "name": name,
                "from": from_ds,
                "to": to_ds,
                "from_columns": [str(c) for c in from_cols],
                "to_columns": [str(c) for c in to_cols],
            })

        if relationships:
            sm["relationships"] = relationships

        # Advisor-applied :OsiMetric nodes.
        metrics: list[dict[str, Any]] = []
        metric_names: set[str] = set()
        for m in ns.run(READ_OSI_METRICS, contract_id=contract_id):
            name = m["name"] or ""
            expr_text = (m["expression"] or "").strip()
            if not name or name in metric_names or not expr_text:
                continue
            metric_names.add(name)
            metric_dialect = (m["dialect"] or dialect or DIALECT_FALLBACK).strip().upper()
            entry: dict[str, Any] = {
                "name": name,
                "expression": {
                    "dialects": [
                        {"dialect": metric_dialect, "expression": expr_text}
                    ]
                },
            }
            if m["description"]:
                entry["description"] = m["description"]
            metrics.append(entry)
        if metrics:
            sm["metrics"] = metrics

        osi["semantic_model"].append(sm)

    return osi


# ── Validator ────────────────────────────────────────────────────────────


def _sqlglot_dialect(osi_dialect: str) -> str:
    return SQLGLOT_DIALECT.get(osi_dialect.upper(), "")


def validate_osi(osi_dict: dict) -> list[dict]:
    """Run the OSI v0.1.1 validation chain. Returns a list of error dicts
    of the form ``{kind, path, message}``. An empty list means conformance
    passes.

    Chain order (matches upstream OSI validate.py):
    1. JSON Schema
    2. Uniqueness (within scope)
    3. References (relationships resolve to existing datasets/fields)
    4. SQL parseability (every expression parses for its declared dialect)
    """
    errors: list[dict] = []

    # 1. JSON Schema
    validator = Draft202012Validator(_load_schema())
    for err in sorted(validator.iter_errors(osi_dict), key=lambda e: list(e.absolute_path)):
        errors.append({
            "kind": "schema",
            "path": "/" + "/".join(str(p) for p in err.absolute_path),
            "message": err.message,
        })

    # If the doc isn't even shape-valid, the rest of the checks will trip
    # over missing keys. Bail.
    if errors:
        return errors

    semantic_models = osi_dict.get("semantic_model", []) or []

    # 2. Uniqueness + 3. References + 4. SQL parseability
    for sm_idx, sm in enumerate(semantic_models):
        sm_path = f"/semantic_model/{sm_idx}"
        datasets = sm.get("datasets") or []
        # Dataset name uniqueness
        seen_ds: set[str] = set()
        for ds_idx, ds in enumerate(datasets):
            ds_name = ds.get("name") or ""
            ds_path = f"{sm_path}/datasets/{ds_idx}"
            if ds_name in seen_ds:
                errors.append({
                    "kind": "uniqueness",
                    "path": ds_path,
                    "message": f"Duplicate dataset name '{ds_name}'",
                })
            seen_ds.add(ds_name)

            # Field name uniqueness within dataset
            fields = ds.get("fields") or []
            seen_f: set[str] = set()
            for f_idx, field in enumerate(fields):
                f_name = field.get("name") or ""
                f_path = f"{ds_path}/fields/{f_idx}"
                if f_name in seen_f:
                    errors.append({
                        "kind": "uniqueness",
                        "path": f_path,
                        "message": f"Duplicate field name '{f_name}' in dataset '{ds_name}'",
                    })
                seen_f.add(f_name)

                # SQL parseability for every field expression
                for d_idx, de in enumerate((field.get("expression", {}) or {}).get("dialects") or []):
                    _check_sql(de, f"{f_path}/expression/dialects/{d_idx}", errors)

        # Metric name uniqueness + SQL parseability
        seen_m: set[str] = set()
        for m_idx, metric in enumerate(sm.get("metrics") or []):
            m_name = metric.get("name") or ""
            m_path = f"{sm_path}/metrics/{m_idx}"
            if m_name in seen_m:
                errors.append({
                    "kind": "uniqueness",
                    "path": m_path,
                    "message": f"Duplicate metric name '{m_name}'",
                })
            seen_m.add(m_name)
            for d_idx, de in enumerate((metric.get("expression", {}) or {}).get("dialects") or []):
                _check_sql(de, f"{m_path}/expression/dialects/{d_idx}", errors)

        # Relationship resolution + uniqueness
        seen_r: set[str] = set()
        ds_index = {ds.get("name", ""): ds for ds in datasets}
        for r_idx, rel in enumerate(sm.get("relationships") or []):
            r_name = rel.get("name") or ""
            r_path = f"{sm_path}/relationships/{r_idx}"
            if r_name in seen_r:
                errors.append({
                    "kind": "uniqueness",
                    "path": r_path,
                    "message": f"Duplicate relationship name '{r_name}'",
                })
            seen_r.add(r_name)
            from_ds = rel.get("from") or ""
            to_ds = rel.get("to") or ""
            if from_ds not in ds_index:
                errors.append({
                    "kind": "reference",
                    "path": f"{r_path}/from",
                    "message": f"Relationship '{r_name}' references unknown dataset '{from_ds}'",
                })
            if to_ds not in ds_index:
                errors.append({
                    "kind": "reference",
                    "path": f"{r_path}/to",
                    "message": f"Relationship '{r_name}' references unknown dataset '{to_ds}'",
                })
            from_cols = rel.get("from_columns") or []
            to_cols = rel.get("to_columns") or []
            if len(from_cols) != len(to_cols):
                errors.append({
                    "kind": "reference",
                    "path": r_path,
                    "message": f"Relationship '{r_name}' from_columns/to_columns length mismatch",
                })
            from_field_names = {f.get("name") for f in (ds_index.get(from_ds, {}).get("fields") or [])}
            to_field_names = {f.get("name") for f in (ds_index.get(to_ds, {}).get("fields") or [])}
            for c in from_cols:
                if from_ds in ds_index and c not in from_field_names:
                    errors.append({
                        "kind": "reference",
                        "path": f"{r_path}/from_columns",
                        "message": f"Column '{c}' not present in dataset '{from_ds}'",
                    })
            for c in to_cols:
                if to_ds in ds_index and c not in to_field_names:
                    errors.append({
                        "kind": "reference",
                        "path": f"{r_path}/to_columns",
                        "message": f"Column '{c}' not present in dataset '{to_ds}'",
                    })

    return errors


def _check_sql(dialect_expr: dict, path: str, errors: list[dict]) -> None:
    expr = (dialect_expr or {}).get("expression") or ""
    dialect = (dialect_expr or {}).get("dialect") or DIALECT_FALLBACK
    sgd = _sqlglot_dialect(dialect)
    if not expr:
        return  # JSON Schema would have caught the empty case
    try:
        # parse_one tolerates expressions without FROM clauses; we just need
        # it to be syntactically resolvable for the dialect.
        parse_one(expr, dialect=sgd) if sgd else parse_one(expr)
    except ParseError as e:
        errors.append({
            "kind": "sql",
            "path": path,
            "message": f"Unparseable {dialect} expression: {str(e).splitlines()[0]}",
        })
    except Exception as e:
        # sqlglot occasionally raises non-ParseError on malformed input.
        errors.append({
            "kind": "sql",
            "path": path,
            "message": f"Unparseable {dialect} expression: {e}",
        })


# ── Scorer ───────────────────────────────────────────────────────────────


@dataclass
class ScoringContext:
    """Bundle of inputs every predicate may need.

    OSI predicates read ``osi_dict`` / ``sm`` / ``datasets``. Graph-direct
    predicates (AI-Ready) read via ``project`` + ``contract_id`` and open
    their own short-lived Neo4j sessions.
    """
    osi_dict: dict
    sm: dict
    datasets: list[dict]
    project: Optional[Project] = None
    contract_id: Optional[str] = None
    # Cached per-predicate results so multiple criteria in one rubric can
    # share intermediate computations without repeating Cypher.
    cache: dict[str, Any] = field(default_factory=dict)


# Each predicate returns a normalized result dict. The dispatcher converts
# ``proportion`` (in [0, 1]) into the earned weight using the criterion's
# configured weight; ``status='na'`` removes the slot from the available
# total (so remaining weights re-normalise).
PredicateResult = dict[str, Any]


def _pred_osi_dataset_descriptions(ctx: ScoringContext, _crit: dict) -> PredicateResult:
    datasets = ctx.datasets
    ds_total = len(datasets)
    if not ds_total:
        return {"status": "na", "proportion": 0.0, "reason": "No datasets to evaluate yet"}
    ds_with_desc = sum(1 for d in datasets if (d.get("description") or "").strip())
    prop = ds_with_desc / ds_total
    return {
        "status": "pass" if prop >= 0.999 else ("fail" if prop == 0 else "partial"),
        "proportion": prop,
        "reason": f"{ds_with_desc}/{ds_total} datasets have a description"
        + ("" if prop >= 0.999 else " — fill in the rest to raise the score"),
    }


def _pred_osi_field_descriptions(ctx: ScoringContext, _crit: dict) -> PredicateResult:
    datasets = ctx.datasets
    fields_total = sum(len(d.get("fields") or []) for d in datasets)
    if not fields_total:
        return {"status": "na", "proportion": 0.0, "reason": "No fields to evaluate yet"}
    fields_with_desc = sum(
        1 for d in datasets for f in (d.get("fields") or [])
        if (f.get("description") or "").strip()
    )
    prop = fields_with_desc / fields_total
    return {
        "status": "pass" if prop >= 0.999 else ("fail" if prop == 0 else "partial"),
        "proportion": prop,
        "reason": f"{fields_with_desc}/{fields_total} fields have a description"
        + (
            " — approve generated descriptions in the Reviews tab to lift this slot"
            if prop < 0.999 else ""
        ),
    }


def _pred_osi_primary_keys(ctx: ScoringContext, _crit: dict) -> PredicateResult:
    datasets = ctx.datasets
    ds_total = len(datasets)
    if not ds_total:
        return {"status": "na", "proportion": 0.0, "reason": "No datasets to evaluate yet"}
    ds_with_pk = sum(1 for d in datasets if (d.get("primary_key") or []))
    prop = ds_with_pk / ds_total
    return {
        "status": "pass" if prop >= 0.999 else ("fail" if prop == 0 else "partial"),
        "proportion": prop,
        "reason": f"{ds_with_pk}/{ds_total} datasets declare a primary key",
    }


def _pred_osi_rich_expressions(ctx: ScoringContext, _crit: dict) -> PredicateResult:
    rich_total = 0
    rich_count = 0
    for d in ctx.datasets:
        for f in (d.get("fields") or []):
            rich_total += 1
            for de in (f.get("expression", {}) or {}).get("dialects") or []:
                if not _is_trivial_expression(de.get("expression", ""), f.get("name", "")):
                    rich_count += 1
                    break
    if not rich_total:
        return {"status": "na", "proportion": 0.0, "reason": "No fields to evaluate yet"}
    # Coverage-graded, like every other proportion predicate (proportion maps
    # linearly to earned weight — see the conversion comment above). 1/100 no
    # longer earns the same credit as 100/100.
    prop = rich_count / rich_total
    return {
        "status": "pass" if prop >= 0.999 else ("fail" if prop == 0 else "partial"),
        "proportion": prop,
        "reason": (
            f"{rich_count}/{rich_total} fields have a non-trivial SQL expression"
            + ("" if prop >= 0.999 else " — author more transforms to raise the score")
            if prop > 0
            else f"0/{rich_total} fields have a non-trivial SQL expression"
            " — bare column references mean no transforms have been authored yet"
        ),
    }


def _pred_osi_relationships(ctx: ScoringContext, _crit: dict) -> PredicateResult:
    if len(ctx.datasets) < 2:
        return {
            "status": "na",
            "proportion": 0.0,
            "reason": "Single-dataset model — relationships not applicable",
        }
    rels = ctx.sm.get("relationships") or []
    has_rel = bool(rels)
    return {
        "status": "pass" if has_rel else "fail",
        "proportion": 1.0 if has_rel else 0.0,
        "reason": (
            f"{len(rels)} relationship(s) declared"
            if has_rel else
            "No relationships declared between your datasets — declare FKs so"
            " consumers can join across them"
        ),
    }


def _pred_osi_metrics(ctx: ScoringContext, _crit: dict) -> PredicateResult:
    metrics = ctx.sm.get("metrics") or []
    has_metric = bool(metrics)
    return {
        "status": "pass" if has_metric else "fail",
        "proportion": 1.0 if has_metric else 0.0,
        "reason": (
            f"{len(metrics)} metric(s) defined"
            if has_metric else
            "No metrics defined — add a couple of business KPIs (sums, counts, ratios)"
            " to make this product useful for BI/AI agents out of the box"
        ),
    }


def _pred_osi_ai_context(ctx: ScoringContext, _crit: dict) -> PredicateResult:
    has_ai_ctx = bool(ctx.sm.get("ai_context"))
    return {
        "status": "pass" if has_ai_ctx else "fail",
        "proportion": 1.0 if has_ai_ctx else 0.0,
        "reason": (
            "ai_context populated"
            if has_ai_ctx else
            "No ai_context set — adding instructions / synonyms / examples helps"
            " AI agents know how to use this product"
        ),
    }


# ── AI-Ready predicates (graph-direct) ──────────────────────────────────


# Counts of :DProdColumn under a given contract, used as the denominator
# for description-quality / lineage / DQ-rule density. Cached per-context
# so multiple AI-Ready criteria don't re-issue the same Cypher.
_PRODUCT_COLUMN_TOTAL_QUERY = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
RETURN count(DISTINCT pc) AS total
"""


def _product_column_total(ctx: ScoringContext) -> int:
    if "product_column_total" in ctx.cache:
        return ctx.cache["product_column_total"]
    if not (ctx.project and ctx.contract_id):
        ctx.cache["product_column_total"] = 0
        return 0
    with _neo4j(ctx.project) as ns:
        row = ns.run(_PRODUCT_COLUMN_TOTAL_QUERY, contract_id=ctx.contract_id).single()
    total = int(row["total"]) if row and row["total"] is not None else 0
    ctx.cache["product_column_total"] = total
    return total


_AI_DESCRIPTION_QUALITY_QUERY = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (pc)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
  WHERE cd.status = 'approved'
    AND coalesce(cd.isCurrent, true) = true
RETURN count(DISTINCT pc) AS total,
       count(DISTINCT CASE WHEN cd IS NULL THEN null ELSE pc END) AS with_desc
"""


def _pred_ai_description_quality(ctx: ScoringContext, _crit: dict) -> PredicateResult:
    if not (ctx.project and ctx.contract_id):
        return {"status": "na", "proportion": 0.0, "reason": "No product columns to evaluate yet"}
    with _neo4j(ctx.project) as ns:
        row = ns.run(_AI_DESCRIPTION_QUALITY_QUERY, contract_id=ctx.contract_id).single()
    total = int(row["total"]) if row and row["total"] is not None else 0
    approved = int(row["with_desc"]) if row and row["with_desc"] is not None else 0
    if total == 0:
        return {"status": "na", "proportion": 0.0, "reason": "No product columns to evaluate yet"}
    prop = approved / total
    return {
        "status": "pass" if prop >= 0.999 else ("fail" if prop == 0 else "partial"),
        "proportion": prop,
        "reason": f"{approved}/{total} product columns have an approved description"
        + ("" if prop >= 0.999 else " — approve more in Reviews → Descriptions"),
    }


_AI_LINEAGE_QUERY = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
  WHERE coalesce(cm.isCurrent, true) = true
RETURN count(DISTINCT pc) AS total,
       count(DISTINCT CASE WHEN cm IS NULL THEN null ELSE pc END) AS mapped
"""


def _pred_ai_lineage_traceability(ctx: ScoringContext, _crit: dict) -> PredicateResult:
    if not (ctx.project and ctx.contract_id):
        return {"status": "na", "proportion": 0.0, "reason": "No product columns to evaluate yet"}
    with _neo4j(ctx.project) as ns:
        row = ns.run(_AI_LINEAGE_QUERY, contract_id=ctx.contract_id).single()
    total = int(row["total"]) if row and row["total"] is not None else 0
    mapped = int(row["mapped"]) if row and row["mapped"] is not None else 0
    if total == 0:
        return {"status": "na", "proportion": 0.0, "reason": "No product columns to evaluate yet"}
    prop = mapped / total
    return {
        "status": "pass" if prop >= 0.999 else ("fail" if prop == 0 else "partial"),
        "proportion": prop,
        "reason": f"{mapped}/{total} product columns have a current source mapping"
        + ("" if prop >= 0.999 else " — engineer to author mappings for the rest"),
    }


_AI_DQ_DENSITY_QUERY = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (pc)-[:HAS_PROPERTY_SHAPE]->(ps:PropertyShape)
  WHERE coalesce(ps.status, 'approved') = 'approved'
RETURN count(DISTINCT pc) AS total,
       count(ps) AS approved_rule_count
"""


def _pred_ai_dq_rule_density(ctx: ScoringContext, _crit: dict) -> PredicateResult:
    """Density = approved rules / product columns. Target ≥1.0 → full credit.
    0.0 → fail. Anything in between scales linearly."""
    if not (ctx.project and ctx.contract_id):
        return {"status": "na", "proportion": 0.0, "reason": "No product columns to evaluate yet"}
    with _neo4j(ctx.project) as ns:
        row = ns.run(_AI_DQ_DENSITY_QUERY, contract_id=ctx.contract_id).single()
    total = int(row["total"]) if row and row["total"] is not None else 0
    rules = int(row["approved_rule_count"]) if row and row["approved_rule_count"] is not None else 0
    if total == 0:
        return {"status": "na", "proportion": 0.0, "reason": "No product columns to evaluate yet"}
    density = rules / total
    prop = min(1.0, density)
    if prop >= 0.999:
        status = "pass"
    elif prop == 0:
        status = "fail"
    else:
        status = "partial"
    return {
        "status": status,
        "proportion": prop,
        "reason": (
            f"{rules} approved rule(s) across {total} column(s) "
            f"(density {density:.2f}; target ≥1.0)"
        ),
    }


_AI_EXAMPLE_QUESTIONS_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_QA_EVAL]->(qa:QAEvaluation)
WITH qa ORDER BY qa.evaluatedAt DESC LIMIT 1
RETURN coalesce(qa.questionsJson, '[]') AS questions_json
"""


def _pred_ai_example_questions(ctx: ScoringContext, _crit: dict) -> PredicateResult:
    """Latest :QAEvaluation question count. ≥3 → pass; 1-2 → partial; 0 → fail."""
    if not (ctx.project and ctx.contract_id):
        return {"status": "fail", "proportion": 0.0, "reason": "Run Question Analysis to seed example questions"}
    with _neo4j(ctx.project) as ns:
        row = ns.run(_AI_EXAMPLE_QUESTIONS_QUERY, contract_id=ctx.contract_id).single()
    count = 0
    if row and row["questions_json"]:
        try:
            qs = json.loads(row["questions_json"])
            if isinstance(qs, list):
                count = len(qs)
        except json.JSONDecodeError:
            count = 0
    if count >= 3:
        return {"status": "pass", "proportion": 1.0, "reason": f"{count} example question(s) on the latest QA evaluation"}
    if count == 0:
        return {"status": "fail", "proportion": 0.0, "reason": "No example questions yet — run Question Analysis in Step 7"}
    return {
        "status": "partial",
        "proportion": count / 3.0,
        "reason": f"{count} example question(s); add {3 - count} more to reach the recommended threshold",
    }


# Maps YAML rubric.criteria[].predicate strings to evaluator functions.
# Registering a new predicate here makes it referenceable from any rubric
# YAML. Each callable takes (ctx, criterion_config) → PredicateResult.
PREDICATE_REGISTRY: dict[str, Callable[[ScoringContext, dict], PredicateResult]] = {
    "osi_dataset_descriptions": _pred_osi_dataset_descriptions,
    "osi_field_descriptions": _pred_osi_field_descriptions,
    "osi_primary_keys": _pred_osi_primary_keys,
    "osi_rich_expressions": _pred_osi_rich_expressions,
    "osi_relationships": _pred_osi_relationships,
    "osi_metrics": _pred_osi_metrics,
    "osi_ai_context": _pred_osi_ai_context,
    "ai_description_quality": _pred_ai_description_quality,
    "ai_lineage_traceability": _pred_ai_lineage_traceability,
    "ai_dq_rule_density": _pred_ai_dq_rule_density,
    "ai_example_questions": _pred_ai_example_questions,
}


def _make_context(
    osi_dict: dict,
    project: Optional[Project] = None,
    contract_id: Optional[str] = None,
) -> ScoringContext:
    semantic_models = osi_dict.get("semantic_model", []) or []
    sm = semantic_models[0] if semantic_models else {}
    return ScoringContext(
        osi_dict=osi_dict,
        sm=sm,
        datasets=sm.get("datasets") or [],
        project=project,
        contract_id=contract_id,
    )


def score_rubric(
    osi_dict: dict,
    errors: list[dict],
    rubric: dict,
    *,
    project: Optional[Project] = None,
    contract_id: Optional[str] = None,
) -> dict:
    """Predicate-driven scorer. Iterates ``rubric['criteria']`` and dispatches
    each to its named predicate.

    Returns the same shape as the legacy ``score_osi`` so the frontend payload
    contract stays byte-identical for the OSI rubric:
    ``{band, completeness, conformance_pass, checklist[], errors[]}``.

    Banding rules:
    - If ``rubric.require_osi_conformance`` is true AND ``errors`` is non-empty,
      band short-circuits to ``red`` (OSI's hard-fail behavior).
    - Otherwise, band derives from ``completeness`` vs the rubric's
      ``band_thresholds`` (defaults: red <50, amber 50-79, green ≥80).
    """
    ctx = _make_context(osi_dict, project=project, contract_id=contract_id)
    conformance_pass = len(errors) == 0

    checklist: list[dict[str, Any]] = []
    earned = 0.0
    available = 0.0

    for crit in rubric.get("criteria") or []:
        pred_name = crit.get("predicate")
        pred = PREDICATE_REGISTRY.get(pred_name)
        if pred is None:
            # Should never happen — load_rubric() validates this. Keep a
            # defensive fallback so a typo doesn't crash scoring.
            checklist.append({
                "criterion": crit.get("label") or crit.get("id") or "?",
                "status": "fail",
                "weight": int(crit.get("weight") or 0),
                "reason": f"Predicate '{pred_name}' is not registered",
            })
            continue

        result = pred(ctx, crit)
        weight = int(crit.get("weight") or 0)
        status = result.get("status") or "fail"
        proportion = float(result.get("proportion") or 0.0)
        reason = result.get("reason") or ""

        if status == "na":
            checklist.append({
                "criterion": crit.get("label") or crit.get("id") or "?",
                "status": "na",
                "weight": 0,
                "reason": reason,
            })
            continue

        earned += proportion * weight
        available += weight
        checklist.append({
            "criterion": crit.get("label") or crit.get("id") or "?",
            "status": status,
            "weight": weight,
            "reason": reason,
        })

    completeness = round((earned / available) * 100) if available else 0

    thresholds = rubric.get("band_thresholds") or {}
    green_t = int(thresholds.get("green", GREEN_THRESHOLD))
    amber_t = int(thresholds.get("amber", AMBER_THRESHOLD))
    require_conf = bool(rubric.get("require_osi_conformance", False))

    if require_conf and not conformance_pass:
        band = "red"
    elif completeness < amber_t:
        band = "red"
    elif completeness >= green_t:
        band = "green"
    else:
        band = "amber"

    return {
        "band": band,
        "completeness": completeness,
        "conformance_pass": conformance_pass,
        "checklist": checklist,
        "errors": errors,
    }


def score_osi(osi_dict: dict, errors: list[dict]) -> dict:
    """Back-compat shim: score against the default (OSI) rubric.

    Existing callers (routers/osi.py, tests, the no-project advise endpoint)
    invoke this without contract context. The OSI predicates don't need a
    Neo4j session, so we can score the rubric directly from the translated
    dict.
    """
    return score_rubric(osi_dict, errors, load_rubric(DEFAULT_RUBRIC))


# ── Persistence ──────────────────────────────────────────────────────────


PERSIST_EVAL_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})
CREATE (oe:OsiEvaluation {
    uri:               'osi:eval:' + $versioned_id + ':' + $batch_id,
    band:              $band,
    completeness:      $completeness,
    conformancePass:   $conformance_pass,
    errors:            $errors_json,
    checklist:         $checklist_json,
    narrative:         $narrative,
    triggeredBy:       $triggered_by,
    evaluatorVersion:  $evaluator_version,
    rubric:            $rubric,
    rubricLabel:       $rubric_label,
    evaluatedAt:       datetime(),
    batchId:           $batch_id
})
CREATE (dc)-[:HAS_OSI_EVAL]->(oe)
RETURN oe.uri AS uri
"""


def _new_batch_id() -> str:
    """Sortable, append-only id. Mirrors the ``:QualityScore`` style of
    timestamp + random suffix without taking a hard dep on ULID."""
    return f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def persist_evaluation(
    project: Project,
    contract_id: str,
    versioned_id: Optional[str],
    score: dict,
    narrative: Optional[str],
    triggered_by: str,
    batch_id: Optional[str] = None,
    rubric: Optional[dict] = None,
) -> dict:
    """Write the eval as an :OsiEvaluation node attached to the contract
    version that was scored. Returns ``{batch_id, uri}`` on success or
    ``{batch_id, uri: None}`` if the contract isn't found.

    The narrative is OK to be ``None`` (advisor may have timed out); the
    deterministic part of the eval should still land. The ``batch_id`` is
    accepted as a parameter so callers that orchestrate translate→eval→advise
    can use the same id throughout. ``contract_id`` is the stable
    :DataContract.id used for the MATCH; ``versioned_id`` is the
    ``<id>:v<n>`` string baked into the OsiEvaluation URI so eval lineage
    stays version-bound even though the lookup itself doesn't need it.

    ``rubric`` is the rubric config dict (from ``load_rubric``); when None
    the eval defaults to OSI for back-compat. The rubric ID and label are
    persisted on the node so historical evals stay attributable after a
    rubric switch.
    """
    if not contract_id or not versioned_id:
        return {"batch_id": batch_id or _new_batch_id(), "uri": None}
    bid = batch_id or _new_batch_id()
    rubric_id = (rubric or {}).get("id") or DEFAULT_RUBRIC
    rubric_label = (rubric or {}).get("label") or "OSI Readiness"
    evaluator_version = (rubric or {}).get("evaluator_version") or EVALUATOR_VERSION
    with _neo4j(project) as ns:
        row = ns.run(
            PERSIST_EVAL_QUERY,
            contract_id=contract_id,
            versioned_id=versioned_id,
            band=score["band"],
            completeness=score["completeness"],
            conformance_pass=score["conformance_pass"],
            errors_json=json.dumps(score.get("errors") or []),
            checklist_json=json.dumps(score.get("checklist") or []),
            narrative=narrative or "",
            triggered_by=triggered_by,
            evaluator_version=evaluator_version,
            rubric=rubric_id,
            rubric_label=rubric_label,
            batch_id=bid,
        ).single()
    return {"batch_id": bid, "uri": row["uri"] if row else None}


def read_contract_rubric(project: Project, contract_id: str) -> str:
    """Return the rubric id recorded on the contract, defaulting to 'osi'."""
    with _neo4j(project) as ns:
        row = ns.run(READ_CONTRACT_RUBRIC, contract_id=contract_id).single()
    return (row["scoring_rubric"] if row else None) or DEFAULT_RUBRIC


# ── Project-folder artifact ──────────────────────────────────────────────


def write_analysis_artifact(
    project_code: str,
    score: dict,
    narrative: Optional[str],
    rubric: Optional[dict] = None,
) -> Path:
    """Write the rendered analysis to ``{project_code}/osi/analysis.md`` for
    the engineer's Results tab. Overwrites; the graph carries the history
    via batchId.

    Path stays at ``osi/analysis.md`` regardless of rubric to avoid breaking
    existing file watchers; the header copy + evaluator line reflect the
    actual rubric in use.
    """
    out_dir = BASE_PROJECT_DIR / project_code / "osi"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "analysis.md"

    band = score.get("band", "?")
    completeness = score.get("completeness", 0)
    conformance = score.get("conformance_pass")
    errors = score.get("errors") or []
    rubric_label = (rubric or {}).get("label") or "OSI Readiness"
    evaluator_version = (rubric or {}).get("evaluator_version") or EVALUATOR_VERSION

    lines: list[str] = []
    lines.append(f"# {rubric_label} Analysis — {band.upper()}")
    lines.append("")
    lines.append(
        f"**Completeness:** {completeness}%  •  **Conformance:** "
        f"{'pass' if conformance else 'fail'}  •  **Evaluator:** {evaluator_version}"
    )
    lines.append("")
    if narrative:
        lines.append("## Summary")
        lines.append("")
        lines.append(narrative.strip())
        lines.append("")
    lines.append("## Checklist")
    lines.append("")
    lines.append("| Criterion | Status | Weight | Detail |")
    lines.append("|---|---|---|---|")
    icon = {"pass": "✓", "partial": "⚠", "fail": "✗", "na": "—"}
    for row in score.get("checklist") or []:
        lines.append(
            f"| {row.get('criterion','')} "
            f"| {icon.get(row.get('status','na'), '?')} {row.get('status','')} "
            f"| {row.get('weight', 0)} "
            f"| {row.get('reason','').replace('|', '\\|')} |"
        )
    if errors:
        lines.append("")
        lines.append("## Conformance errors")
        lines.append("")
        for e in errors:
            lines.append(f"- **{e.get('kind','?')}** at `{e.get('path','')}` — {e.get('message','')}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


# ── Composite entrypoint ─────────────────────────────────────────────────


def evaluate(project: Project, contract_id: str, triggered_by: str, narrative: Optional[str] = None) -> dict:
    """Translate + validate + score + persist in one shot.

    Reads the contract's selected ``scoringRubric`` (defaults to ``'osi'``)
    and dispatches the matching rubric config. The narrative is optional;
    callers that drive the LLM advisor pass it in. Auto-trigger hooks that
    don't wait for the advisor pass ``None`` so the deterministic eval lands
    immediately. Returns a dict the API layer can return to the frontend
    directly.
    """
    rubric = load_rubric(read_contract_rubric(project, contract_id))

    if rubric.get("requires_translation", True):
        osi = translate_to_osi(project, contract_id)
        errors = validate_osi(osi)
    else:
        osi = {"version": OSI_VERSION, "semantic_model": []}
        errors = []

    score = score_rubric(osi, errors, rubric, project=project, contract_id=contract_id)

    # Resolve the *current* contract version we just scored against.
    versioned_id: Optional[str] = None
    with _neo4j(project) as ns:
        row = ns.run(READ_CONTRACT_HEAD, contract_id=contract_id).single()
        if row:
            versioned_id = row["versioned_id"]

    persisted = persist_evaluation(
        project, contract_id, versioned_id, score, narrative, triggered_by, rubric=rubric
    )
    artifact_path = write_analysis_artifact(project.project_code, score, narrative, rubric=rubric)

    return {
        "band": score["band"],
        "completeness": score["completeness"],
        "conformance_pass": score["conformance_pass"],
        "checklist": score["checklist"],
        "errors": score["errors"],
        "narrative": narrative or None,
        "batch_id": persisted["batch_id"],
        "evaluation_uri": persisted["uri"],
        "versioned_id": versioned_id,
        "artifact_path": str(artifact_path),
        "osi": osi,
        "rubric": rubric.get("id"),
        "rubric_label": rubric.get("label"),
        "evaluator_version": rubric.get("evaluator_version"),
    }
