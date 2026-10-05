"""Engineer-facing edit surface for :DatasetTransform fields.

Phase 4 ships explicit `:DatasetTransform.joinsJson` (verbatim FROM/JOIN graph
that bypasses FK auto-discovery in view-DDL), but the wizard is PO-facing and
the PO has no reason to think in FK-graph terms. The natural trigger for
authoring joins[] is engineer-side: they ran serving, FK BFS picked the
wrong bridge, they want to override.

These endpoints power that override flow:

  GET  /api/projects/{id}/dataset-transform?output_dataset_uri=...
       Returns the current dt fields (joins + filter + dedupe + grouping_keys
       + scd_policy + suppressed_columns + grain_prose) for one output dataset.

  PUT  /api/projects/{id}/dataset-transform/joins
       Body: {output_dataset_uri, joins: [{alias, dataset_uri, kind, predicate}, ...]}
       Validates the joins[] shape, writes to the schema-side :DatasetTransform
       (source of truth) AND mirrors to the ods-side parallel copy so view-DDL
       reads the new value immediately without waiting for _generate_dprod.

Known limitation: if the PO re-saves through the wizard, _save_odcs_to_graph
wipes+recreates the schema-side :DatasetTransform from the YAML spec — which
will clobber an engineer's joins[] override. That's acceptable for v1: the
override use case is a transient "FK auto-discovery picked wrong, override
this run" workflow, not a long-term schema decision. Engineer re-applies if
the PO edits the contract.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ..database import get_session
from ..models import Project
from ..neo4j_client import neo4j_session


router = APIRouter(tags=["dataset-transform"])


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _get_project_for_write(project_id: int, session: Session) -> Project:
    """_get_project + acceptance gate (warn-only by default; 409 when enforced).
    Used by the dataset-transform mutating PUTs; the GET keeps the plain loader."""
    project = _get_project(project_id, session)
    from ..request_guard import guard_rest_mutation
    guard_rest_mutation(project, session)
    return project


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    )


_VALID_JOIN_KINDS = {"left", "inner", "right", "full", "cross"}


def _parse_output_uri(output_dataset_uri: str) -> tuple[str, str]:
    """`dprod:ds:{contract_id}:{schema_physical_name}` → (contract_id, schema_physical_name).

    The contract_id may itself contain colons (e.g. `dpe-cf-05112026-01-contract`)
    so we split on the first three colons and treat the rest as the schema
    physical name. Empty values raise."""
    if not output_dataset_uri.startswith("dprod:ds:"):
        raise HTTPException(400, f"output_dataset_uri must start with 'dprod:ds:'; got {output_dataset_uri!r}")
    rest = output_dataset_uri[len("dprod:ds:"):]
    # Contract IDs in this codebase look like `<archetype>-<date>-<NN>-contract`
    # — they contain no colons. The schema physical name is everything after
    # the first colon. If the URI has fewer than 2 segments we reject.
    parts = rest.split(":", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(400, f"output_dataset_uri malformed: {output_dataset_uri!r}")
    return parts[0], parts[1]


def _validate_joins(payload: Any) -> tuple[list[dict], list[str]]:
    """Normalize the joins[] payload + collect human-readable validation errors.

    Returns (cleaned_list, errors). On any error we return an empty list so
    the caller can refuse the write rather than persist a half-valid shape."""
    errors: list[str] = []
    if not isinstance(payload, list):
        return [], [f"joins must be a list (got {type(payload).__name__})"]
    cleaned: list[dict] = []
    for i, entry in enumerate(payload):
        if not isinstance(entry, dict):
            errors.append(f"joins[{i}]: must be an object")
            continue
        alias = str(entry.get("alias", "")).strip()
        dataset_uri = str(entry.get("dataset_uri", "")).strip()
        kind = str(entry.get("kind", "left")).strip().lower()
        predicate = str(entry.get("predicate", "")).strip()
        local_errs = []
        if not alias:
            local_errs.append("alias is required")
        if not dataset_uri:
            local_errs.append("dataset_uri is required")
        if kind not in _VALID_JOIN_KINDS:
            local_errs.append(f"kind must be one of {sorted(_VALID_JOIN_KINDS)}; got {kind!r}")
        if not predicate and kind != "cross":
            local_errs.append("predicate is required for non-CROSS joins")
        if local_errs:
            errors.append(f"joins[{i}]: {', '.join(local_errs)}")
            continue
        cleaned_entry = {"alias": alias, "dataset_uri": dataset_uri, "kind": kind, "predicate": predicate}
        # Pure-bridge flag (junction datasets contributing no SELECT columns —
        # view-DDL emits them as a DISTINCT-projected derived table). Persisted
        # only when true so pre-existing payloads round-trip byte-identical.
        if entry.get("bridge_only"):
            cleaned_entry["bridge_only"] = True
        cleaned.append(cleaned_entry)
    if errors:
        return [], errors
    return cleaned, []


# Read the schema-side :DatasetTransform (source of truth). Scoped to the
# project's contract by joining through :HAS_CONTRACT so an engineer can't
# read another project's transform by guessing the contract_id.
READ_DT_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})
      -[:HAS_SCHEMA]->(s:DataContractSchema {physicalName: $schema_physical_name})
OPTIONAL MATCH (s)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
RETURN dt
"""


# Upsert :DatasetTransform.joinsJson on BOTH the schema-side and ods-side.
# - Schema side is the source-of-truth survived by _generate_dprod.
# - Ods side is the read side view-DDL uses; mirroring here avoids a forced
#   _generate_dprod re-run after every joins edit.
# MERGE-ON-CREATE seeds the empty defaults for the other fields so a fresh
# dt isn't half-defined.
UPSERT_JOINS_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})
      -[:HAS_SCHEMA]->(s:DataContractSchema {physicalName: $schema_physical_name})
MERGE (s)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
  ON CREATE SET dt.contractId = dc.id,
                dt.schemaPhysicalName = $schema_physical_name,
                dt.filterPredicate = '',
                dt.filterIntent = '',
                dt.dedupeJson = '',
                dt.groupingKeysJson = '',
                dt.windowSpecsJson = '',
                dt.scdPolicyJson = '',
                dt.suppressedColumnsJson = '',
                dt.grainProse = ''
SET dt.joinsJson = $joinsJson
WITH dt, dc
OPTIONAL MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
              -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
              -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
WHERE ods.uri = $output_dataset_uri
FOREACH (_ IN CASE WHEN ods IS NULL THEN [] ELSE [1] END |
  MERGE (ods)-[:HAS_DATASET_TRANSFORM]->(odsdt:DatasetTransform)
    ON CREATE SET odsdt.outputDatasetUri = ods.uri,
                  odsdt.contractId = dt.contractId,
                  odsdt.schemaPhysicalName = dt.schemaPhysicalName,
                  odsdt.filterPredicate = coalesce(dt.filterPredicate, ''),
                  odsdt.filterIntent = coalesce(dt.filterIntent, ''),
                  odsdt.dedupeJson = coalesce(dt.dedupeJson, ''),
                  odsdt.groupingKeysJson = coalesce(dt.groupingKeysJson, ''),
                  odsdt.windowSpecsJson = coalesce(dt.windowSpecsJson, ''),
                  odsdt.scdPolicyJson = coalesce(dt.scdPolicyJson, ''),
                  odsdt.suppressedColumnsJson = coalesce(dt.suppressedColumnsJson, ''),
                  odsdt.grainProse = coalesce(dt.grainProse, '')
  SET odsdt.joinsJson = $joinsJson
)
RETURN dt.joinsJson AS joinsJson
"""


# Upsert :DatasetTransform.filterPredicate (the engineer-finalized SQL) on BOTH
# the schema-side and ods-side, mirroring UPSERT_JOINS_QUERY. The PO's prose
# (filterIntent) is preserved — only the compiled predicate is written here.
UPSERT_FILTER_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})
      -[:HAS_SCHEMA]->(s:DataContractSchema {physicalName: $schema_physical_name})
MERGE (s)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
  ON CREATE SET dt.contractId = dc.id,
                dt.schemaPhysicalName = $schema_physical_name,
                dt.filterIntent = '',
                dt.dedupeJson = '',
                dt.joinsJson = '',
                dt.groupingKeysJson = '',
                dt.windowSpecsJson = '',
                dt.scdPolicyJson = '',
                dt.suppressedColumnsJson = '',
                dt.grainProse = ''
SET dt.filterPredicate = $filter
WITH dt, dc
OPTIONAL MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
              -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
              -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
WHERE ods.uri = $output_dataset_uri
FOREACH (_ IN CASE WHEN ods IS NULL THEN [] ELSE [1] END |
  MERGE (ods)-[:HAS_DATASET_TRANSFORM]->(odsdt:DatasetTransform)
    ON CREATE SET odsdt.outputDatasetUri = ods.uri,
                  odsdt.contractId = dt.contractId,
                  odsdt.schemaPhysicalName = dt.schemaPhysicalName,
                  odsdt.filterIntent = coalesce(dt.filterIntent, ''),
                  odsdt.dedupeJson = coalesce(dt.dedupeJson, ''),
                  odsdt.joinsJson = coalesce(dt.joinsJson, ''),
                  odsdt.groupingKeysJson = coalesce(dt.groupingKeysJson, ''),
                  odsdt.windowSpecsJson = coalesce(dt.windowSpecsJson, ''),
                  odsdt.scdPolicyJson = coalesce(dt.scdPolicyJson, ''),
                  odsdt.suppressedColumnsJson = coalesce(dt.suppressedColumnsJson, ''),
                  odsdt.grainProse = coalesce(dt.grainProse, '')
  SET odsdt.filterPredicate = $filter
)
RETURN dt.filterPredicate AS filterPredicate
"""


def _dt_to_dict(dt) -> dict:
    """Decode the raw Neo4j node properties into the API shape (snake_case,
    JSON fields parsed). Same projection the view-DDL generator does, so the
    UI can render exactly what view-DDL will see at materialization time."""
    if dt is None:
        return {
            "filter": "", "filter_intent": "", "dedupe": None, "joins": [],
            "grouping_keys": [], "scd_policy": None,
            "suppressed_columns": [], "grain_prose": "",
        }
    raw = dict(dt)

    def parse_json(key, default):
        s = (raw.get(key) or "").strip()
        if not s:
            return default
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return default

    dedupe = parse_json("dedupeJson", None)
    if isinstance(dedupe, dict) and not dedupe.get("keys"):
        dedupe = None
    scd = parse_json("scdPolicyJson", None)
    if isinstance(scd, dict) and not scd.get("type"):
        scd = None
    return {
        "filter":             (raw.get("filterPredicate") or "").strip(),
        "filter_intent":      (raw.get("filterIntent") or "").strip(),
        "dedupe":             dedupe if isinstance(dedupe, dict) else None,
        "joins":              parse_json("joinsJson", []) if isinstance(parse_json("joinsJson", []), list) else [],
        "grouping_keys":      parse_json("groupingKeysJson", []) if isinstance(parse_json("groupingKeysJson", []), list) else [],
        "scd_policy":         scd if isinstance(scd, dict) else None,
        "suppressed_columns": parse_json("suppressedColumnsJson", []) if isinstance(parse_json("suppressedColumnsJson", []), list) else [],
        "grain_prose":        (raw.get("grainProse") or "").strip(),
    }


class JoinsUpdate(BaseModel):
    output_dataset_uri: str
    joins: list[dict]


@router.get("/api/projects/{project_id}/dataset-transform")
def get_dataset_transform(
    project_id: int,
    output_dataset_uri: str,
    session: Session = Depends(get_session),
):
    """Read the schema-side :DatasetTransform fields for one output dataset.

    Returns the same shape view-DDL sees (snake_case, JSON fields parsed)
    plus the raw `output_dataset_uri` for client-side bookkeeping. Empty /
    missing :DatasetTransform → all-empty defaults."""
    project = _get_project(project_id, session)
    contract_id, schema_physical_name = _parse_output_uri(output_dataset_uri)
    with _neo4j(project) as sess:
        row = sess.run(
            READ_DT_QUERY,
            contract_id=contract_id,
            schema_physical_name=schema_physical_name,
        ).single()
    dt = row["dt"] if row else None
    return {
        "output_dataset_uri": output_dataset_uri,
        "contract_id": contract_id,
        "schema_physical_name": schema_physical_name,
        **_dt_to_dict(dt),
    }


@router.get("/api/projects/{project_id}/dataset-transform/observed-values")
def get_dataset_transform_observed_values(
    project_id: int,
    output_dataset_uri: str,
    session: Session = Depends(get_session),
):
    """Observed profile values for the source columns feeding one output dataset.

    Delegates to the same output-scoped lineage traversal the filter interpreter
    uses (``filter_intent._enrich_source_columns``) so the engineer's Row-filter
    panel can show the REAL stored values (e.g. employment_status: active ·
    terminated · on_leave) and click-to-insert the exact literal. Best-effort:
    a graph hiccup yields an empty list, never a 500. Values are OBSERVED top-N
    profile values, not an authoritative allow-list."""
    project = _get_project(project_id, session)
    from .filter_intent import _enrich_source_columns
    try:
        cols, _ = _enrich_source_columns(project, output_dataset_uri)
    except Exception:
        cols = []
    return {
        "output_dataset_uri": output_dataset_uri,
        "columns": [c for c in cols if c.get("top_values")],
    }


@router.put("/api/projects/{project_id}/dataset-transform/joins")
def update_dataset_transform_joins(
    project_id: int,
    payload: JoinsUpdate,
    session: Session = Depends(get_session),
):
    """Upsert :DatasetTransform.joinsJson on schema-side + ods-side.

    Validates the joins[] entries strictly — refuses to persist a list
    containing any malformed entry (returns 400 with per-index errors).
    Successful update returns the persisted joinsJson so the client can
    refresh its local state without a second GET."""
    project = _get_project_for_write(project_id, session)
    contract_id, schema_physical_name = _parse_output_uri(payload.output_dataset_uri)

    cleaned, errors = _validate_joins(payload.joins)
    if errors:
        raise HTTPException(400, {"errors": errors})

    joins_json = json.dumps(cleaned)
    with _neo4j(project) as sess:
        row = sess.run(
            UPSERT_JOINS_QUERY,
            contract_id=contract_id,
            schema_physical_name=schema_physical_name,
            output_dataset_uri=payload.output_dataset_uri,
            joinsJson=joins_json,
        ).single()

    if row is None:
        # The match failed — either the contract isn't in this project's
        # graph or the schema_physical_name doesn't exist on it. Surface as
        # 404 so the client can show a clear "no such dataset" message.
        raise HTTPException(404, f"No DataContractSchema found for {payload.output_dataset_uri}")

    # Joins[] just changed — bump the contract's schema-change marker so any
    # cached :QAEvaluation flips to stale on the next marketplace open. Uses
    # currentVersion (engineer-time edits don't branch).
    with _neo4j(project) as sess:
        sess.run(
            "MATCH (dc:DataContract {id: $contract_id}) "
            "SET dc.lastSchemaChangeVersion = coalesce(dc.currentVersion, 1)",
            contract_id=contract_id,
        )

    return {
        "output_dataset_uri": payload.output_dataset_uri,
        "joins": cleaned,
        "joins_count": len(cleaned),
    }


class FilterUpdate(BaseModel):
    output_dataset_uri: str
    filter: str  # the engineer-finalized compiled SQL predicate


@router.put("/api/projects/{project_id}/dataset-transform/filter")
def update_dataset_transform_filter(
    project_id: int,
    payload: FilterUpdate,
    session: Session = Depends(get_session),
):
    """Finalize the dataset-level filter predicate (engineer-owned SQL).

    Runs the shared safety gate (`sql_executor.validate_predicate`) before
    persisting — refuses plain-language / malformed predicates with a 400 so
    the bad value never reaches a deployed view's WHERE clause. The PO's prose
    (filterIntent) is preserved; only the compiled predicate is written."""
    from ..sql_executor import validate_predicate

    project = _get_project_for_write(project_id, session)
    contract_id, schema_physical_name = _parse_output_uri(payload.output_dataset_uri)
    new_filter = (payload.filter or "").strip()

    # Safety gate (parse-only here): catch plain-language / operator-less prose
    # before it can reach a deployed view. We don't dry-run a per-column EXPLAIN
    # at this point — the predicate runs over a possibly-joined base CTE whose
    # table context isn't a single resolvable relation; the authoritative
    # column-level check happens at deploy when the full view DDL executes.
    vr = validate_predicate(new_filter)
    if not vr.ok:
        raise HTTPException(400, {"error_class": vr.error_class, "message": vr.message,
                                  "predicate": new_filter})

    with _neo4j(project) as sess:
        row = sess.run(
            UPSERT_FILTER_QUERY,
            contract_id=contract_id,
            schema_physical_name=schema_physical_name,
            output_dataset_uri=payload.output_dataset_uri,
            filter=new_filter,
        ).single()

    if row is None:
        raise HTTPException(404, f"No DataContractSchema found for {payload.output_dataset_uri}")

    # Filter changed — bump the contract's schema-change marker (mirrors joins).
    with _neo4j(project) as sess:
        sess.run(
            "MATCH (dc:DataContract {id: $contract_id}) "
            "SET dc.lastSchemaChangeVersion = coalesce(dc.currentVersion, 1)",
            contract_id=contract_id,
        )

    return {
        "output_dataset_uri": payload.output_dataset_uri,
        "filter": new_filter,
        "warning": vr.warning,
    }


# ── Dataset-shape stages: dedupe / grouping / SCD / windows / suppression ─────
#
# Phase 4/5/6 activate these :DatasetTransform reserved fields, but only joins +
# filter had an engineer write path — the rest were authorable only in the PO
# wizard. These endpoints add the missing writes so the engineer can author the
# WHOLE dataset shape (the view-DDL already lowers every one of these). Same
# discipline as joins/filter: validate the shape, upsert schema-side (source of
# truth) + ods-side mirror, bump lastSchemaChangeVersion. Same v1 caveat too — a
# PO wizard re-save re-materialises the schema-side dt from the YAML.

# API field name → :DatasetTransform property. The property names are a fixed
# allow-list (never user input), so f-string-injecting them into Cypher is safe.
_SHAPE_FIELD_TO_PROP = {
    "dedupe": "dedupeJson",
    "grouping_keys": "groupingKeysJson",
    "scd_policy": "scdPolicyJson",
    "suppressed_columns": "suppressedColumnsJson",
    "windows": "windowSpecsJson",
}

_VALID_SCD_TYPES = {"latest_only", "scd2", "snapshot"}


def _upsert_dt_field_query(prop: str) -> str:
    """Generic single-field upsert (schema-side + ods-side mirror), mirroring
    UPSERT_JOINS_QUERY. `prop` MUST be a value of _SHAPE_FIELD_TO_PROP (allow-listed)."""
    return f"""\
MATCH (dc:DataContract {{id: $contract_id}})
      -[:HAS_SCHEMA]->(s:DataContractSchema {{physicalName: $schema_physical_name}})
MERGE (s)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
  ON CREATE SET dt.contractId = dc.id, dt.schemaPhysicalName = $schema_physical_name,
                dt.filterPredicate = '', dt.filterIntent = '', dt.dedupeJson = '',
                dt.joinsJson = '', dt.groupingKeysJson = '', dt.windowSpecsJson = '',
                dt.scdPolicyJson = '', dt.suppressedColumnsJson = '', dt.grainProse = ''
SET dt.{prop} = $value
WITH dt, dc
OPTIONAL MATCH (dc)-[:MATERIALISES_AS]->(:DProdDataProduct)
              -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
              -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
WHERE ods.uri = $output_dataset_uri
FOREACH (_ IN CASE WHEN ods IS NULL THEN [] ELSE [1] END |
  MERGE (ods)-[:HAS_DATASET_TRANSFORM]->(odsdt:DatasetTransform)
    ON CREATE SET odsdt.outputDatasetUri = ods.uri, odsdt.contractId = dt.contractId,
                  odsdt.schemaPhysicalName = dt.schemaPhysicalName,
                  odsdt.filterPredicate = coalesce(dt.filterPredicate, ''),
                  odsdt.filterIntent = coalesce(dt.filterIntent, ''),
                  odsdt.dedupeJson = coalesce(dt.dedupeJson, ''),
                  odsdt.joinsJson = coalesce(dt.joinsJson, ''),
                  odsdt.groupingKeysJson = coalesce(dt.groupingKeysJson, ''),
                  odsdt.windowSpecsJson = coalesce(dt.windowSpecsJson, ''),
                  odsdt.scdPolicyJson = coalesce(dt.scdPolicyJson, ''),
                  odsdt.suppressedColumnsJson = coalesce(dt.suppressedColumnsJson, ''),
                  odsdt.grainProse = coalesce(dt.grainProse, '')
  SET odsdt.{prop} = $value
)
RETURN dt.{prop} AS value
"""


def _encode_shape_field(field: str, val: Any) -> tuple[str | None, str | None]:
    """Validate one shape field + return (json_string, error). A null/empty value
    encodes to '' (clears the stage). Heavy validation (column existence, SCD
    column shape) stays at view-DDL/deploy time — same as joins/filter."""
    if field in ("grouping_keys", "suppressed_columns"):
        if val is None:
            return "", None
        if not isinstance(val, list):
            return None, f"{field} must be a list of column names"
        return json.dumps([str(x) for x in val if str(x).strip()]), None
    if field == "dedupe":
        if val is None or (isinstance(val, dict) and not val.get("keys")):
            return "", None
        if not isinstance(val, dict) or not isinstance(val.get("keys"), list) or not val["keys"]:
            return None, "dedupe must be {keys:[...], order_by?, direction?}"
        return json.dumps({
            "keys": [str(k) for k in val["keys"] if str(k).strip()],
            "order_by": str(val.get("order_by", "")).strip(),
            "direction": str(val.get("direction", "desc")).strip().lower() or "desc",
        }), None
    if field == "scd_policy":
        if val is None or (isinstance(val, dict) and not val.get("type")):
            return "", None
        if not isinstance(val, dict) or val.get("type") not in _VALID_SCD_TYPES:
            return None, f"scd_policy.type must be one of {sorted(_VALID_SCD_TYPES)}"
        return json.dumps(val), None
    if field == "windows":
        if val is None or (isinstance(val, dict) and not val):
            return "", None
        if not isinstance(val, dict):
            return None, "windows must be an object keyed by window name"
        return json.dumps(val), None
    return None, f"unknown shape field {field!r}"


class ShapeUpdate(BaseModel):
    output_dataset_uri: str
    dedupe: dict | None = None
    grouping_keys: list | None = None
    scd_policy: dict | None = None
    suppressed_columns: list | None = None
    windows: dict | None = None


@router.put("/api/projects/{project_id}/dataset-transform/shape")
def update_dataset_transform_shape(
    project_id: int,
    payload: ShapeUpdate,
    session: Session = Depends(get_session),
):
    """Upsert one or more dataset-shape stages (dedupe / grouping_keys / scd_policy
    / suppressed_columns / windows) on schema-side + ods-side. Only the fields the
    client actually sends are touched (partial update); a null value clears that
    stage. Returns the full re-read shape so the client can refresh in one call."""
    project = _get_project_for_write(project_id, session)
    contract_id, schema_physical_name = _parse_output_uri(payload.output_dataset_uri)

    provided = payload.model_dump(exclude_unset=True)
    provided.pop("output_dataset_uri", None)
    if not provided:
        raise HTTPException(400, "no shape fields provided")

    updates: dict[str, str] = {}
    errors: list[str] = []
    for field, val in provided.items():
        prop = _SHAPE_FIELD_TO_PROP.get(field)
        if not prop:
            continue
        encoded, err = _encode_shape_field(field, val)
        if err:
            errors.append(err)
        else:
            updates[prop] = encoded  # type: ignore[assignment]
    if errors:
        raise HTTPException(400, {"errors": errors})

    with _neo4j(project) as sess:
        matched = False
        for prop, value in updates.items():
            row = sess.run(
                _upsert_dt_field_query(prop),
                contract_id=contract_id,
                schema_physical_name=schema_physical_name,
                output_dataset_uri=payload.output_dataset_uri,
                value=value,
            ).single()
            if row is not None:
                matched = True
        if not matched:
            raise HTTPException(404, f"No DataContractSchema found for {payload.output_dataset_uri}")
        sess.run(
            "MATCH (dc:DataContract {id: $contract_id}) "
            "SET dc.lastSchemaChangeVersion = coalesce(dc.currentVersion, 1)",
            contract_id=contract_id,
        )
        row = sess.run(
            READ_DT_QUERY,
            contract_id=contract_id,
            schema_physical_name=schema_physical_name,
        ).single()

    return {
        "output_dataset_uri": payload.output_dataset_uri,
        "contract_id": contract_id,
        "schema_physical_name": schema_physical_name,
        **_dt_to_dict(row["dt"] if row else None),
    }


OUTPUT_DATASETS_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(:DProdDataProduct)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
RETURN ods.uri AS uri, coalesce(ods.physicalName, ods.name) AS name
ORDER BY name
"""


@router.get("/api/projects/{project_id}/dataset-transform/output-datasets")
def list_output_datasets(
    project_id: int,
    session: Session = Depends(get_session),
):
    """List the product's output datasets (uri + physical name) so the shape UI
    can offer a dataset picker. Scoped to the project's own `{code}-contract`."""
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    try:
        with _neo4j(project) as sess:
            rows = list(sess.run(OUTPUT_DATASETS_QUERY, contract_id=contract_id))
    except Exception:
        rows = []
    return {
        "contract_id": contract_id,
        "datasets": [{"uri": r["uri"], "name": r["name"]} for r in rows if r.get("uri")],
    }
