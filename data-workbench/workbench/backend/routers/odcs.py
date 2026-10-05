"""ODCS (Open Data Contract Standard v3.1) integration.

Handles ODCS template management, v3.1 spec persistence to Neo4j,
graph-to-YAML materialisation, and ODCS-to-dprod transformation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, NamedTuple, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException

from ..authz import require_role
from pydantic import BaseModel
from sqlmodel import Session

from ..config import BASE_DIR
from ..database import get_session
from ..models import Project
from ..neo4j_client import neo4j_session
from .._contract_versioning import (
    ConsumesBindingError,
    DiscardNotAllowed,
    acquire_topology_lock,
    discard_draft_version,
    save_contract_head,
    save_contract_substructure,
    sync_consumes_edges,
    validate_consumes_bindings,
)
from .._graph_helpers import (
    DRAFT_STATES,
    current_version_filter,
    schema_uri as _schema_uri,
)

router = APIRouter(prefix="/api/odcs", tags=["odcs"])

# Top-level v3.1 keys the canonicalizer handles explicitly. Anything else that
# survives canonicalization is persisted as JSON under ``:DataContract.extras``
# so imports round-trip without silent loss.
_KNOWN_TOP_LEVEL = {
    "apiVersion", "kind", "id", "name", "version", "status",
    "domain", "dataProduct",
    "description", "purpose", "limitations",
    "owners", "stewards", "team", "roles", "servers",
    "schema", "quality", "slaProperties", "terms",
    "tags", "support", "customProperties",
    "inputs",  # consumer-aligned: refs to consumed source-aligned products
    "scoringRubric",  # selectable readiness rubric ('osi' | 'ai_ready' | ...)
    "extras",
}


# ── Template endpoints ─────────────────────────────────────────────────────


class TemplateInfo(BaseModel):
    name: str
    filename: str
    domain: str | None
    description: str


@router.get("/templates")
def list_templates(session: Session = Depends(get_session)):
    """List available templates for the editor picker — from the **Blueprint
    Library** (published), the single source of truth. (The legacy file-based
    ODCS templates were retired.)"""
    from .. import template_store  # lazy: template_store imports from this module
    try:
        rows = [r for r in template_store.list_templates_raw(session)
                if r.get("status") == "published"]
    except Exception:
        rows = []
    return [
        TemplateInfo(
            name=r.get("name") or r["id"],
            filename=r["id"],  # the template id doubles as the picker key
            domain=r.get("domain"),
            description=(r.get("description") or "").strip(),
        )
        for r in rows
    ]


@router.get("/templates/{filename}")
def get_template(filename: str, session: Session = Depends(get_session)):
    """Fetch a Blueprint-Library template as parsed YAML, keyed by its
    ``template:`` id (the picker key returned by ``GET /templates``)."""
    from .. import template_store
    spec = template_store._read_template_from_graph(filename, session)
    if not spec or not spec.get("isTemplate"):
        raise HTTPException(404, f"Template not found: {filename}")
    export = {k: v for k, v in spec.items()
              if k not in template_store._TEMPLATE_META_KEYS}
    return {"filename": filename, "spec": export,
            "yaml": yaml.safe_dump(export, sort_keys=False, allow_unicode=True)}


class ODCSYAMLInput(BaseModel):
    yaml: str


# ── Helpers ───────────────────────────────────────────────────────────────

def _coerce_string(v) -> str:
    """Return v as a string, dumping objects/arrays to YAML when needed."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    try:
        return yaml.dump(v, default_flow_style=False, sort_keys=False).strip()
    except Exception:
        return str(v)


def _to_json_str(v: Any) -> str:
    """Serialise objects/arrays to a JSON string (for Neo4j-safe storage)."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, default=str)
    except Exception:
        return str(v)


def _product_kind_for_archetype(archetype: str | None) -> str:
    """Map a project archetype to the DEFAULT marketplace productKind tag."""
    if archetype == "dpe-sa":
        return "source"
    if archetype == "dpe-cf":
        return "consumer"
    return ""


def _resolve_product_kind(spec: dict, archetype: str | None) -> str:
    """Resolve the marketplace ``productKind`` for a save.

    productKind is now three-valued (``source`` | ``aggregate`` | ``consumer``)
    and DECOUPLED from archetype: ``aggregate`` and ``consumer`` both ride the
    ``dpe-cf`` machinery and differ only by the PO's late-binding intent flag.

    - ``dpe-sa`` is architecturally special → always ``source``.
    - ``dpe-cf`` → honour an explicit ``spec.productKind`` of ``aggregate`` or
      ``consumer`` (the wizard's Product-Details intent step); default
      ``consumer`` when unset/invalid so legacy saves stay ``consumer``.
    - anything else → the archetype default (usually empty).
    """
    default = _product_kind_for_archetype(archetype)
    if archetype != "dpe-cf":
        return default
    explicit = (spec.get("productKind") or "").strip().lower()
    if explicit in ("aggregate", "consumer"):
        return explicit
    return default  # 'consumer'


def _from_json_str(s: Any) -> Any:
    """Parse a JSON string back into Python. Returns {} / [] / None as fallback."""
    if s in (None, ""):
        return None
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except Exception:
        return s


# ── Canonicalization ──────────────────────────────────────────────────────

# Reserved field set for the schema-level `transform` block introduced by
# Phase 2 of implementingdatatransformations.md §3.1. Phase 2 activates the
# first two (filter + dedupe); the rest are accepted and round-tripped now so
# that contracts authored for later phases survive a save/reload cycle without
# loss. Each field maps to a Neo4j property on :DatasetTransform.
_DATASET_TRANSFORM_FIELDS = (
    "filter",            # str: SQL fragment for the WHERE clause on the base CTE
    "dedupe",            # dict: {keys: [...], order_by: ..., direction: 'asc'|'desc'}
    "joins",             # list (reserved, Phase 4): structured multi-source joins
    "grouping_keys",     # list (reserved, Phase 3): GROUP BY columns
    "window_specs",      # dict (reserved, Phase 6): named window definitions
    "scd_policy",        # dict (reserved, Phase 5): {type, effective_column, ...}
    "suppressed_columns",  # list (reserved, Phase 5): columns to drop from output
    "grain_prose",       # str: PO-authored "one row per X" prose (Shape wizard
                         # step). Captured for context; column-level
                         # grouping_keys is the load-bearing field.
)


_VALID_JOIN_KINDS = {"inner", "left", "right", "full", "cross", "anti", "semi"}


def _canonicalize_dataset_transform(raw: dict) -> dict | None:
    """Normalize a schema-level transform block. Returns a dict with only
    the known keys present, or None if every recognised key is empty.

    Accepts camelCase aliases (`groupingKeys`, `windowSpecs`, `scdPolicy`,
    `suppressedColumns`, `orderBy`, `datasetUri`) that some YAML authors
    emit, and lowers them to the canonical snake_case form persisted to
    Neo4j. Phase 2 active: filter + dedupe. Phase 3 active: grouping_keys.
    Phase 4 active: joins[]. The rest stay reserved for later phases.
    """
    if not isinstance(raw, dict):
        return None

    # Alias dispatch: prefer canonical, fall back to camelCase.
    canon = {
        "filter":              raw.get("filter") or raw.get("filterPredicate") or "",
        "filter_intent":       raw.get("filter_intent") or raw.get("filterIntent") or "",
        "dedupe":              raw.get("dedupe") or None,
        "joins":               raw.get("joins") or [],
        "grouping_keys":       raw.get("grouping_keys") or raw.get("groupingKeys") or [],
        "window_specs":        raw.get("window_specs") or raw.get("windowSpecs") or {},
        "scd_policy":          raw.get("scd_policy") or raw.get("scdPolicy") or None,
        "suppressed_columns":  raw.get("suppressed_columns") or raw.get("suppressedColumns") or [],
        "grain_prose":         raw.get("grain_prose") or raw.get("grainProse") or "",
    }

    # Normalize dedupe sub-keys.
    if canon["dedupe"] and isinstance(canon["dedupe"], dict):
        d = canon["dedupe"]
        canon["dedupe"] = {
            "keys":      list(d.get("keys") or []),
            "order_by":  d.get("order_by") or d.get("orderBy") or "",
            "direction": (d.get("direction") or "desc").lower(),
        }
        if canon["dedupe"]["direction"] not in ("asc", "desc"):
            canon["dedupe"]["direction"] = "desc"
        if not canon["dedupe"]["keys"]:
            canon["dedupe"] = None  # malformed dedupe — drop

    # Filter must be a string (raw SQL fragment).
    if canon["filter"] and not isinstance(canon["filter"], str):
        canon["filter"] = str(canon["filter"])

    # filter_intent is the PO's plain-language prose; the view-DDL NEVER reads
    # it. We deliberately do NOT derive one of filter/filter_intent from the
    # other — a legacy product has `filter` (SQL) and no intent and must round-
    # trip unchanged; the safety gate is the backstop for prose that slipped in.
    if canon["filter_intent"] and not isinstance(canon["filter_intent"], str):
        canon["filter_intent"] = str(canon["filter_intent"])

    # Phase 3: grouping_keys is a list of product-column-name strings. Drop
    # non-string entries and de-dup while preserving order so the view-DDL's
    # GROUP BY clause is deterministic.
    if canon["grouping_keys"]:
        seen = set()
        cleaned_keys = []
        for k in canon["grouping_keys"]:
            if not isinstance(k, str):
                continue
            k = k.strip()
            if k and k not in seen:
                cleaned_keys.append(k)
                seen.add(k)
        canon["grouping_keys"] = cleaned_keys

    # Phase 4: joins[] is an ordered list of {alias, dataset_uri, kind,
    # predicate}. The first entry is the FROM table (predicate may be empty);
    # subsequent entries are JOINs. Skip malformed entries silently so a
    # partially-valid block still survives the round-trip.
    if canon["joins"]:
        cleaned_joins = []
        for j in canon["joins"]:
            if not isinstance(j, dict):
                continue
            alias = (j.get("alias") or "").strip()
            ds_uri = (j.get("dataset_uri") or j.get("datasetUri") or "").strip()
            if not alias or not ds_uri:
                continue
            kind = (j.get("kind") or "left").strip().lower()
            if kind not in _VALID_JOIN_KINDS:
                kind = "left"
            predicate = (j.get("predicate") or j.get("on") or "").strip()
            cleaned_join = {
                "alias":       alias,
                "dataset_uri": ds_uri,
                "kind":        kind,
                "predicate":   predicate,
            }
            # Pure-bridge flag (accepts camelCase alias); kept only when true
            # so pre-existing contracts round-trip byte-identical.
            if j.get("bridge_only") or j.get("bridgeOnly"):
                cleaned_join["bridge_only"] = True
            cleaned_joins.append(cleaned_join)
        canon["joins"] = cleaned_joins

    # Drop empties so the saved JSON / round-tripped YAML stays terse.
    cleaned = {k: v for k, v in canon.items() if v not in (None, "", [], {})}
    return cleaned or None


def _clean_tags(value: Any) -> list[str]:
    """Normalize a tags value → trimmed, non-empty, case-insensitively deduped
    (first-seen display casing preserved). Shared by the ODCS canonicalizer and
    the lightweight tags endpoint so both apply identical hygiene."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for t in value:
        if t is None:
            continue
        label = str(t).strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(label)
    return cleaned


def _canonicalize_v3_1(raw: dict) -> dict:
    """Normalize common ODCS v3.1 author conventions into the canonical shape
    the editor and graph layer expect. Recognised sections:

      - ``schema[]`` elements may use ``fields``/``columns`` instead of ``properties``;
        ``type`` backfills missing ``physicalType``; schema-level ``primaryKeys``
        (list of names or ``{name, position}``) promotes per-property flags;
        ``foreignKeys`` is preserved at schema level as-is.
      - ``properties[]`` carry ``primaryKey``/``required`` (falling back from
        ``isPrimaryKey``/``isNullable``/``nullable``); ``physicalType`` falls back
        to ``dataType``. Extras preserved: ``pii``, ``classification``,
        ``examples``, ``logicalTypeOptions``.
      - ``slaProperties`` accepts ``sla`` alias; items accept ``metric``/``objective``
        /``window`` fallbacks for ``property``/``value``/``unit``.
      - ``quality[].rule`` falls back from ``type``; ``dataset``/``field``/``column``
        hints get appended to the description.
      - ``terms.{usage,limitations,billing,noticePeriod}`` are coerced to strings;
        if the top-level block is missing, ``customProperties.terms`` is lifted.
      - ``tags[]``, ``stewards[]``, ``team[]``, ``roles[]``, ``servers[]``,
        ``support`` and the remainder of ``customProperties`` are preserved.

    Unknown top-level fields are gathered into ``extras`` so imports round-trip
    losslessly through the graph.
    """
    spec = dict(raw)

    # ── Schemas ─────────────────────────────────────────────────────────
    schemas_out = []
    for s in (spec.get("schema") or []):
        if not isinstance(s, dict):
            continue
        props_in = s.get("properties") or s.get("fields") or s.get("columns") or []

        # Schema-level primaryKeys: list of strings OR list of {name, position}.
        pk_names: set[str] = set()
        for pk in (s.get("primaryKeys") or []):
            if isinstance(pk, dict) and pk.get("name"):
                pk_names.add(pk["name"])
            elif isinstance(pk, str):
                pk_names.add(pk)

        props_out = []
        for p in props_in:
            if not isinstance(p, dict):
                continue
            if "primaryKey" in p:
                is_pk = bool(p.get("primaryKey"))
            elif "isPrimaryKey" in p:
                is_pk = bool(p.get("isPrimaryKey"))
            else:
                is_pk = p.get("name") in pk_names

            if "required" in p:
                is_required = bool(p.get("required"))
            elif "nullable" in p:
                is_required = not bool(p.get("nullable"))
            elif "isNullable" in p:
                is_required = not bool(p.get("isNullable"))
            else:
                is_required = False

            examples = p.get("examples")
            if examples is not None and not isinstance(examples, list):
                examples = [examples]

            # transform: structured PO derivation hint. Accept either the
            # canonical `transform` block or aliases `derivedFrom` / `derive`
            # that some authoring tools emit. Pass through as-is — the wizard
            # and engineer side define the schema.
            transform_hint = p.get("transform") or p.get("derivedFrom") or p.get("derive") or None
            if transform_hint is not None and not isinstance(transform_hint, dict):
                transform_hint = None

            prop_out = {
                "name": p.get("name", ""),
                "physicalName": p.get("physicalName") or p.get("name", ""),
                "logicalName": p.get("logicalName", ""),
                "logicalType": p.get("logicalType", ""),
                "physicalType": p.get("physicalType") or p.get("dataType", ""),
                "description": p.get("description", ""),
                "primaryKey": is_pk,
                "required": is_required,
                "criticalDataElement": bool(p.get("criticalDataElement", False)),
                "pii": bool(p.get("pii", False)),
                "classification": p.get("classification", ""),
                "examples": examples or [],
                "logicalTypeOptions": p.get("logicalTypeOptions") or {},
            }
            if transform_hint:
                prop_out["transform"] = transform_hint
            props_out.append(prop_out)

        # Schema-level `transform` block — Phase 2 of
        # implementingdatatransformations.md introduces dataset-level shape
        # (filter / dedupe / joins / grouping / window / SCD / suppressed
        # columns). Phase 2 activates `filter` and `dedupe`; other keys are
        # accepted now so authoring tools can forward-write before later
        # phases land. Aliased form `x-workbench-transform` is accepted for
        # users who want to keep the ODCS top-level field-name space clean.
        ds_transform_raw = s.get("transform") or s.get("x-workbench-transform") or None
        if ds_transform_raw is not None and not isinstance(ds_transform_raw, dict):
            ds_transform_raw = None
        ds_transform = _canonicalize_dataset_transform(ds_transform_raw) if ds_transform_raw else None

        schema_out = {
            "name": s.get("name", ""),
            "physicalName": s.get("physicalName") or s.get("name", ""),
            "physicalType": s.get("physicalType") or s.get("type") or "table",
            "description": s.get("description", ""),
            "properties": props_out,
            "foreignKeys": s.get("foreignKeys") or [],
        }
        if ds_transform:
            schema_out["transform"] = ds_transform
        schemas_out.append(schema_out)
    if "schema" in spec:
        spec["schema"] = schemas_out

    # ── SLA properties ──────────────────────────────────────────────────
    sla_in = spec.get("slaProperties") or spec.get("sla") or []
    sla_out = []
    for s in sla_in:
        if not isinstance(s, dict):
            continue
        prop = s.get("property") or s.get("metric") or s.get("name") or ""
        val = s.get("value")
        if val is None:
            val = s.get("objective")
        if val is None and isinstance(s.get("threshold"), dict):
            val = s["threshold"].get("value")
        if val is None:
            val = ""
        unit = s.get("unit") or s.get("window") or ""
        sla_out.append({
            "property": str(prop),
            "value": str(val),
            "unit": str(unit),
        })
    if "slaProperties" in spec or "sla" in spec:
        spec["slaProperties"] = sla_out
    spec.pop("sla", None)

    # ── Quality rules ───────────────────────────────────────────────────
    # Column / dataset references on the input rule are preserved as
    # first-class fields on the canonical rule (in addition to the
    # human-readable [column=X, dataset=Y] hint baked into description)
    # so the materialiser can later anchor :PropertyShape {ruleSource='spec'}
    # to the matching :DProdColumn without re-parsing the description.
    quality_out = []
    for q in (spec.get("quality") or []):
        if not isinstance(q, dict):
            continue
        rule = q.get("rule") or q.get("type") or ""
        desc = q.get("description", "")
        col = q.get("field") or q.get("column") or q.get("property") or ""
        dataset = q.get("dataset") or q.get("schema") or ""
        hint_bits = []
        if dataset:
            hint_bits.append(f"dataset={dataset}")
        if col:
            hint_bits.append(f"column={col}")
        if hint_bits:
            hint = "[" + ", ".join(hint_bits) + "]"
            desc = f"{desc} {hint}".strip() if desc else hint
        canonical = {
            "rule": str(rule),
            "name": q.get("name", ""),
            "description": desc,
            "severity": q.get("severity", "warning"),
            "dimension": q.get("dimension", ""),
            "businessImpact": q.get("businessImpact", ""),
        }
        if col:
            canonical["column"] = str(col)
        if dataset:
            canonical["dataset"] = str(dataset)
        quality_out.append(canonical)
    if "quality" in spec:
        spec["quality"] = quality_out

    # ── Terms ───────────────────────────────────────────────────────────
    cp = spec.get("customProperties")
    if not spec.get("terms") and isinstance(cp, dict) and isinstance(cp.get("terms"), dict):
        spec["terms"] = cp["terms"]

    terms = spec.get("terms")
    if isinstance(terms, dict):
        spec["terms"] = {
            "usage": _coerce_string(terms.get("usage")),
            "limitations": _coerce_string(terms.get("limitations")),
            "billing": _coerce_string(terms.get("billing")),
            "noticePeriod": _coerce_string(terms.get("noticePeriod")),
        }

    # ── Tags ────────────────────────────────────────────────────────────
    # Trim + case-insensitively dedupe (first-seen casing wins) — keeps
    # "Finance"/"finance" from fragmenting the marketplace group-by-tag buckets.
    cleaned_tags = _clean_tags(spec.get("tags"))
    if cleaned_tags:
        spec["tags"] = cleaned_tags
    else:
        spec.pop("tags", None)

    # ── Stewards ────────────────────────────────────────────────────────
    stewards = []
    for st in (spec.get("stewards") or []):
        if isinstance(st, dict):
            stewards.append({
                "name": st.get("name", ""),
                "email": st.get("email", ""),
                "role": st.get("role", ""),
                "username": st.get("username", ""),
            })
    if "stewards" in spec:
        spec["stewards"] = stewards

    # ── Team ────────────────────────────────────────────────────────────
    team = []
    for t in (spec.get("team") or []):
        if isinstance(t, dict):
            team.append({
                "username": t.get("username") or t.get("name", ""),
                "role": t.get("role", ""),
                "name": t.get("name", ""),
                "email": t.get("email", ""),
            })
    if "team" in spec:
        spec["team"] = team

    # ── Roles ───────────────────────────────────────────────────────────
    roles = []
    for r in (spec.get("roles") or []):
        if not isinstance(r, dict):
            continue
        perms = r.get("permissions") if isinstance(r.get("permissions"), dict) else {}
        access = r.get("access") or perms.get("access") or ""
        datasets = r.get("datasets") or perms.get("datasets") or []
        if not isinstance(datasets, list):
            datasets = [datasets]
        roles.append({
            "role": r.get("role", ""),
            "description": r.get("description", ""),
            "access": str(access),
            "datasets": [str(d) for d in datasets],
        })
    if "roles" in spec:
        spec["roles"] = roles

    # ── Inputs (consumer-aligned: consumed source-aligned products) ─────
    # Each entry references a published :DProdDataProduct by its dprod_uri
    # (the cross-project namespace allowed for :CONSUMES edges). We accept
    # contract_id as a hint so the wizard can reuse it without re-deriving;
    # name is for display only. Anything else is dropped.
    inputs_in = spec.get("inputs") or []
    inputs_out: list[dict] = []
    if isinstance(inputs_in, list):
        for inp in inputs_in:
            if not isinstance(inp, dict):
                continue
            dprod_uri = (inp.get("dprod_uri") or inp.get("dprodUri") or "").strip()
            if not dprod_uri:
                continue
            inputs_out.append({
                "dprod_uri": dprod_uri,
                "contract_id": (inp.get("contract_id") or inp.get("contractId") or "").strip(),
                "name": (inp.get("name") or "").strip(),
            })
    if inputs_out:
        spec["inputs"] = inputs_out
    else:
        spec.pop("inputs", None)

    # ── Servers ─────────────────────────────────────────────────────────
    servers = []
    for sv in (spec.get("servers") or []):
        if not isinstance(sv, dict):
            continue
        ds = sv.get("datasets") or sv.get("tables") or []
        servers.append({
            "name": sv.get("name") or sv.get("server", ""),
            "environment": sv.get("environment", ""),
            "type": sv.get("type", ""),
            "account": sv.get("account", ""),
            "database": sv.get("database", ""),
            "schema": sv.get("schema", ""),
            "datasets": ds,
        })
    if "servers" in spec:
        spec["servers"] = servers

    # ── Support ─────────────────────────────────────────────────────────
    support = spec.get("support")
    if isinstance(support, dict):
        # Normalise flat shape into contacts/documentation lists if present.
        contacts = list(support.get("contacts") or [])
        docs = list(support.get("documentation") or [])
        if not contacts:
            for key, ctype in (("contactEmail", "email"), ("contactSlack", "slack"),
                               ("contactPhone", "phone")):
                if support.get(key):
                    contacts.append({"type": ctype, "value": str(support[key])})
        if not docs:
            for key, dtype in (("documentationUrl", "docs"),
                               ("runbookUrl", "runbook")):
                if support.get(key):
                    docs.append({"type": dtype, "url": str(support[key])})
        normalised = {
            "contacts": contacts,
            "documentation": docs,
        }
        escalation = support.get("escalationPolicy") or support.get("escalation")
        if escalation:
            normalised["escalationPolicy"] = _coerce_string(escalation)
        spec["support"] = normalised

    # ── customProperties: keep everything except the already-lifted terms ─
    if isinstance(cp, dict):
        remaining = {k: v for k, v in cp.items() if k != "terms"}
        if remaining:
            spec["customProperties"] = remaining
        else:
            spec.pop("customProperties", None)

    # ── Gather anything else into ``extras`` so nothing is silently dropped.
    extras = {k: v for k, v in spec.items() if k not in _KNOWN_TOP_LEVEL}
    for k in list(extras):
        spec.pop(k, None)
    if extras:
        spec["extras"] = extras

    return spec


@router.post("/parse")
def parse_odcs_yaml(body: ODCSYAMLInput):
    """Parse an ODCS v3.1 YAML string and return the spec dict for form pre-population."""
    try:
        raw = yaml.safe_load(body.yaml)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"Invalid YAML: {e}")
    if not isinstance(raw, dict):
        raise HTTPException(400, "YAML must be a mapping at the top level")
    return {"spec": _canonicalize_v3_1(raw)}


# ── Project-scoped ODCS endpoints ─────────────────────────────────────────

def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    )


# ── Temporal-edge query templates ─────────────────────────────────────────
#
# The contract is a stable :DataContract node keyed on `id`. Substructure
# edges (HAS_OWNER, HAS_SCHEMA, etc.) carry fromVersion / toVersion validity
# ranges so historical states can be reconstructed without cloning the
# subtree per version. Every read query is a TEMPLATE with the WHERE
# fragment left as `{version_filter}` — _scope_read swaps in either the
# current-version filter or a `$version`-pinned filter at call time.

_VERSION_FILTER_PLACEHOLDER = "{version_filter}"


def _temporal_pattern(rel_var: str = "r") -> str:
    """Default version filter: pin to the contract's currentVersion."""
    return current_version_filter(rel_var)


def _temporal_pattern_pinned(rel_var: str = "r") -> str:
    """Filter pinned to a specific version passed as ``$version``."""
    return (
        f"{rel_var}.fromVersion <= $version "
        f"AND ({rel_var}.toVersion IS NULL OR {rel_var}.toVersion >= $version)"
    )


READ_DATASET_TRANSFORM_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})-[r1:HAS_SCHEMA]->(s:DataContractSchema {physicalName: $schema_name})
       -[r2:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
WHERE {version_filter_r1}
  AND {version_filter_r2}
RETURN dt LIMIT 1
"""


class SaveResult(NamedTuple):
    """Return of `_save_odcs_to_graph`. `contract_id` for back-compat callers;
    `save_mode` ∈ {initial, in_place, patch, branch} and `save_version` let the
    caller (and MCP agents) know whether the save branched a new version."""
    contract_id: str
    save_mode: str
    save_version: int


def _save_odcs_to_graph(
    spec: dict,
    project: Project,
    submitted_by: str = "",
    change_kind: str = "auto",
    revision_notes: str = "",
) -> SaveResult:
    """Persist an ODCS v3.1 spec dict to the Neo4j knowledge graph.

    The contract lives as a stable :DataContract node keyed on `id`. Each
    save updates the current version's values in place when the head is in
    a drafty state, or branches a new :ContractVersion sidecar when the
    head has progressed past drafting. Substructure (owners, schemas,
    properties, quality rules, etc.) uses stable nodes keyed on logical
    URIs with temporal edges (fromVersion / toVersion) from :DataContract.
    """
    contract_id = spec.get("id") or f"{project.project_code}-contract"

    product_kind = _resolve_product_kind(spec, project.archetype)

    # Scoring rubric is a property of the product (not a contract version) —
    # persisted on the stable :DataContract head. Defaults to 'osi' for
    # back-compat; the wizard's Step 1 picker is the authoring surface.
    scoring_rubric = (spec.get("scoringRubric") or "").strip().lower() or "osi"

    contract_props = dict(
        name=spec.get("name", ""),
        version=spec.get("version", "1.0.0"),
        status=spec.get("status", "draft"),
        apiVersion=spec.get("apiVersion", "v3.1.0"),
        kind=spec.get("kind", "DataContract"),
        domain=spec.get("domain", ""),
        dataProduct=spec.get("dataProduct", ""),
        description=spec.get("description", ""),
        purpose=spec.get("purpose", ""),
        limitations=spec.get("limitations", ""),
        productKind=product_kind,
        scoringRubric=scoring_rubric,
        tags=[str(t) for t in (spec.get("tags") or [])],
        support=_to_json_str(spec.get("support") or {}),
        customProperties=_to_json_str(spec.get("customProperties") or {}),
        extras=_to_json_str(spec.get("extras") or {}),
    )

    incoming_inputs = spec.get("inputs") or []

    # One explicit write transaction so the head + substructure + :CONSUMES sync
    # commit or roll back together. The DAG guard validates the COMPLETE incoming
    # edge set BEFORE any mutation, so a rejected binding (self / cycle /
    # ineligible target) leaves the contract head and edges byte-unchanged.
    with _neo4j(project) as ns:
        tx = ns.begin_transaction()
        try:
            if incoming_inputs:
                # Serialise topology mutations across workers, then validate.
                acquire_topology_lock(tx)
                validate_consumes_bindings(tx, contract_id, incoming_inputs)

            save_mode, save_version = save_contract_head(
                tx,
                contract_id=contract_id,
                contract_props=contract_props,
                submitted_by=submitted_by,
                change_kind=change_kind,
                revision_notes=revision_notes,
            )

            # 'patch' mode is cosmetic-only: by classifier rules the substructure
            # is unchanged, so skip the full sync. Skipping keeps the audit
            # trail clean — no spurious "edge fromVersion bumped" events.
            if save_mode != "patch":
                save_contract_substructure(
                    tx,
                    contract_id=contract_id,
                    save_version=save_version,
                    spec=spec,
                    to_json_str=_to_json_str,
                )

                sync_consumes_edges(
                    tx,
                    contract_id=contract_id,
                    save_version=save_version,
                    inputs=incoming_inputs,
                )
            tx.commit()
        except Exception:
            tx.rollback()
            raise

    try:
        from ..graph_ops import ensure_project_node, link_contract_to_project
        ensure_project_node(project)
        link_contract_to_project(project, contract_id)
    except Exception:
        pass

    return SaveResult(contract_id, save_mode, save_version)


# ── Read ODCS v3.1 spec from graph ────────────────────────────────────────

# READ_* queries — temporal-edge pattern. See the comment block above
# `_VERSION_FILTER_PLACEHOLDER` near the save-side templates for the full
# rationale; in short, each template has a `{version_filter}` placeholder
# that _scope_read substitutes with either the current-version filter or a
# $version-pinned filter.


READ_CONTRACT_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})
OPTIONAL MATCH (dc)-[r:HAS_TERMS]->(terms:DataContractTerms)
  WHERE """ + _VERSION_FILTER_PLACEHOLDER + """
RETURN dc, terms
"""

READ_OWNERS_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})-[r:HAS_OWNER]->(o:DataContractOwner)
WHERE """ + _VERSION_FILTER_PLACEHOLDER + """
RETURN o ORDER BY o.role, o.email
"""

READ_STEWARDS_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})-[r:HAS_STEWARD]->(st:DataContractSteward)
WHERE """ + _VERSION_FILTER_PLACEHOLDER + """
RETURN st ORDER BY st.name
"""

READ_TEAM_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})-[r:HAS_TEAM_MEMBER]->(tm:DataContractTeamMember)
WHERE """ + _VERSION_FILTER_PLACEHOLDER + """
RETURN tm ORDER BY tm.username
"""

READ_ROLES_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})-[r:HAS_ROLE]->(ro:DataContractRole)
WHERE """ + _VERSION_FILTER_PLACEHOLDER + """
RETURN ro ORDER BY ro.role
"""

READ_INPUTS_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})-[r:CONSUMES]->(src:DProdDataProduct)
WHERE """ + _VERSION_FILTER_PLACEHOLDER + """
RETURN src.uri AS dprod_uri, coalesce(src.name, '') AS name
ORDER BY src.uri
"""

READ_SERVERS_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})-[r:HAS_SERVER]->(srv:DataContractServer)
WHERE """ + _VERSION_FILTER_PLACEHOLDER + """
RETURN srv ORDER BY srv.environment, srv.name
"""

READ_SCHEMAS_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})-[r:HAS_SCHEMA]->(s:DataContractSchema)
WHERE """ + _VERSION_FILTER_PLACEHOLDER + """
RETURN s ORDER BY s.name
"""

# Schema-scoped property reads — walk via the contract→property edge
# (which is properly version-scoped) and filter by p.schemaName since
# the property node already carries its schema. Walking via the schema
# node's HAS_PROPERTY edge would require maintaining two parallel
# temporal edges; one source of truth is simpler.
READ_PROPERTIES_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})-[r:HAS_PROPERTY]->(p:DataContractProperty)
WHERE {version_filter}
  AND p.schemaName = $schema_name
RETURN p ORDER BY p.name
"""

READ_QUALITY_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})-[r:HAS_QUALITY_RULE]->(q:DataContractQuality)
WHERE """ + _VERSION_FILTER_PLACEHOLDER + """
RETURN q
"""

READ_SLA_TEMPLATE = """\
MATCH (dc:DataContract {id: $contract_id})-[r:HAS_SLA_PROPERTY]->(sla:DataContractSLAProperty)
WHERE """ + _VERSION_FILTER_PLACEHOLDER + """
RETURN sla
"""


def _scope_read(query_template: str, version: int | None) -> str:
    """Materialize a READ_*_TEMPLATE by substituting the version filter.

    Supports two placeholder shapes:
      - ``{version_filter}`` for templates with a single rel var named ``r``
      - ``{version_filter_rN}`` for templates with multiple rel vars (r1, r2)

    Default reads pin to the contract's currentVersion. When a specific
    version int is passed, the filter switches to ``$version`` so older
    versions can be materialized (diff viewer, marketplace history, etc.).
    """
    def filt(rel_var: str) -> str:
        if version is None:
            return current_version_filter(rel_var)
        return (
            f"{rel_var}.fromVersion <= $version "
            f"AND ({rel_var}.toVersion IS NULL OR {rel_var}.toVersion >= $version)"
        )

    out = query_template
    out = out.replace("{version_filter}", filt("r"))
    out = out.replace("{version_filter_r1}", filt("r1"))
    out = out.replace("{version_filter_r2}", filt("r2"))
    return out


# Concrete query strings for the current-version case — module-level cached
# so we don't re-format on every call. Historical-version reads still call
# _scope_read at runtime with the version parameter.
READ_CONTRACT = _scope_read(READ_CONTRACT_TEMPLATE, None)
READ_OWNERS = _scope_read(READ_OWNERS_TEMPLATE, None)
READ_STEWARDS = _scope_read(READ_STEWARDS_TEMPLATE, None)
READ_TEAM = _scope_read(READ_TEAM_TEMPLATE, None)
READ_ROLES = _scope_read(READ_ROLES_TEMPLATE, None)
READ_INPUTS = _scope_read(READ_INPUTS_TEMPLATE, None)
READ_SERVERS = _scope_read(READ_SERVERS_TEMPLATE, None)
READ_SCHEMAS = _scope_read(READ_SCHEMAS_TEMPLATE, None)
READ_PROPERTIES = _scope_read(READ_PROPERTIES_TEMPLATE, None)
READ_QUALITY = _scope_read(READ_QUALITY_TEMPLATE, None)
READ_SLA = _scope_read(READ_SLA_TEMPLATE, None)


def _read_odcs_from_graph(
    contract_id: str,
    project: Project | None = None,
    version: int | None = None,
    *,
    ns_factory=None,
) -> dict | None:
    """Reconstruct an ODCS v3.1 spec dict from the graph.

    When ``version`` is None, returns the current view — substructure edges
    are filtered by ``dc.currentVersion``. When a specific version int is
    passed, walks substructure edges valid at that version (used by
    ``GET /odcs/versions/{n}`` and the diff viewer).

    ``ns_factory`` is an injectable zero-arg callable returning a Neo4j session
    context manager. It defaults to ``lambda: _neo4j(project)`` (per-project
    graph). The Blueprint Library passes ``lambda: estate_graph_session(session)``
    so templates are read from the global AppSettings graph with **zero query
    duplication**. When ``ns_factory`` is supplied, ``project`` may be None.
    """
    params: dict[str, object] = {"contract_id": contract_id}
    if version is not None:
        params["version"] = version

    factory = ns_factory or (lambda: _neo4j(project))
    with factory() as ns:
        def run_read(template: str, **extra):
            return ns.run(_scope_read(template, version), **params, **extra)

        row = run_read(READ_CONTRACT_TEMPLATE).single()
        if not row:
            return None

        dc = dict(row["dc"])
        terms_node = dict(row["terms"]) if row["terms"] else {}

        spec: dict = {
            "apiVersion": dc.get("apiVersion", "v3.1.0"),
            "kind": dc.get("kind", "DataContract"),
            "id": dc.get("id", ""),
            "name": dc.get("name", ""),
            "version": dc.get("version", "1.0.0"),
            "status": dc.get("status", "draft"),
            "domain": dc.get("domain", ""),
            "dataProduct": dc.get("dataProduct", ""),
            "description": dc.get("description", ""),
            "purpose": dc.get("purpose", ""),
            "limitations": dc.get("limitations", ""),
            # scoringRubric coalesces to 'osi' for contracts that predate
            # the rubric field. Surfaced so wizard edit-mode hydration sees
            # the right rubric in Step 1.
            "scoringRubric": dc.get("scoringRubric") or "osi",
        }

        tags = dc.get("tags") or []
        if tags:
            spec["tags"] = list(tags)

        support = _from_json_str(dc.get("support"))
        if support:
            spec["support"] = support

        custom_props = _from_json_str(dc.get("customProperties"))
        if custom_props:
            spec["customProperties"] = custom_props

        extras = _from_json_str(dc.get("extras"))
        if extras:
            spec["extras"] = extras

        owners = [dict(r["o"]) for r in run_read(READ_OWNERS_TEMPLATE)]
        spec["owners"] = [
            {
                "username": o.get("username", ""),
                "name": o.get("name", ""),
                "role": o.get("role", ""),
                "email": o.get("email", ""),
            }
            for o in owners
        ]

        stewards = [dict(r["st"]) for r in run_read(READ_STEWARDS_TEMPLATE)]
        if stewards:
            spec["stewards"] = [
                {k: st.get(k, "") for k in ("name", "email", "role", "username")}
                for st in stewards
            ]

        team = [dict(r["tm"]) for r in run_read(READ_TEAM_TEMPLATE)]
        if team:
            spec["team"] = [
                {k: tm.get(k, "") for k in ("username", "role", "name", "email")}
                for tm in team
            ]

        roles = [dict(rr["ro"]) for rr in run_read(READ_ROLES_TEMPLATE)]
        if roles:
            spec["roles"] = [
                {
                    "role": rr.get("role", ""),
                    "description": rr.get("description", ""),
                    "access": rr.get("access", ""),
                    "datasets": list(rr.get("datasets") or []),
                }
                for rr in roles
            ]

        servers = [dict(rr["srv"]) for rr in run_read(READ_SERVERS_TEMPLATE)]
        if servers:
            spec["servers"] = [
                {
                    "name": srv.get("name", ""),
                    "environment": srv.get("environment", ""),
                    "type": srv.get("type", ""),
                    "account": srv.get("account", ""),
                    "database": srv.get("database", ""),
                    "schema": srv.get("schema", ""),
                    "datasets": _from_json_str(srv.get("datasets")) or [],
                }
                for srv in servers
            ]

        schemas = [dict(rr["s"]) for rr in run_read(READ_SCHEMAS_TEMPLATE)]
        spec["schema"] = []
        for s in schemas:
            props = [dict(rr["p"])
                     for rr in run_read(READ_PROPERTIES_TEMPLATE, schema_name=s["physicalName"])]
            schema_entry = {
                "name": s.get("name", ""),
                "physicalName": s.get("physicalName", ""),
                "description": s.get("description", ""),
                "physicalType": s.get("physicalType", "table"),
                "properties": [
                    {
                        "name": p.get("name", ""),
                        "physicalName": p.get("physicalName", ""),
                        "logicalName": p.get("logicalName", ""),
                        "logicalType": p.get("logicalType", ""),
                        "physicalType": p.get("physicalType", ""),
                        "description": p.get("description", ""),
                        "primaryKey": bool(p.get("primaryKey", False)),
                        "required": bool(p.get("required", False)),
                        "criticalDataElement": bool(p.get("criticalDataElement", False)),
                        "pii": bool(p.get("pii", False)),
                        "classification": p.get("classification", ""),
                        # Phase 1: sensitivity enum (none|internal|confidential|
                        # pii|phi). Only emit when non-default so default
                        # contracts stay terse in YAML.
                        **({"sensitivity": p.get("sensitivity")}
                           if (p.get("sensitivity") or "").strip() and p.get("sensitivity") != "none"
                           else {}),
                        "examples": list(p.get("examples") or []),
                        "logicalTypeOptions": _from_json_str(p.get("logicalTypeOptions")) or {},
                        # transform hint: only emit when present so contracts
                        # without a derivation hint stay clean
                        **({"transform": _from_json_str(p.get("transformHint"))}
                           if p.get("transformHint")
                           else {}),
                    }
                    for p in props
                ],
            }
            fks = _from_json_str(s.get("foreignKeys"))
            if fks:
                schema_entry["foreignKeys"] = fks

            # Phase 2: read the schema-level :DatasetTransform if present and
            # re-emit the `transform` block. The Cypher properties are
            # JSON-stringified — we decode here and drop empties so the
            # YAML stays terse (matches save-time canonicalisation).
            dt_row = run_read(READ_DATASET_TRANSFORM_TEMPLATE, schema_name=s.get("physicalName", "")).single()
            if dt_row:
                dt = dict(dt_row["dt"])
                xform = {
                    "filter":              dt.get("filterPredicate") or "",
                    "filter_intent":       dt.get("filterIntent") or "",
                    "dedupe":              _from_json_str(dt.get("dedupeJson")) or None,
                    "joins":               _from_json_str(dt.get("joinsJson")) or [],
                    "grouping_keys":       _from_json_str(dt.get("groupingKeysJson")) or [],
                    "window_specs":        _from_json_str(dt.get("windowSpecsJson")) or {},
                    "scd_policy":          _from_json_str(dt.get("scdPolicyJson")) or None,
                    "suppressed_columns":  _from_json_str(dt.get("suppressedColumnsJson")) or [],
                    "grain_prose":         dt.get("grainProse") or "",
                }
                cleaned = {k: v for k, v in xform.items() if v not in (None, "", [], {})}
                if cleaned:
                    schema_entry["transform"] = cleaned
            spec["schema"].append(schema_entry)

        quality_rows = [dict(rr["q"]) for rr in run_read(READ_QUALITY_TEMPLATE)]
        spec["quality"] = [
            {
                "rule": q.get("rule", ""),
                "name": q.get("name", ""),
                "description": q.get("description", ""),
                "severity": q.get("severity", "warning"),
                "dimension": q.get("dimension", ""),
                "businessImpact": q.get("businessImpact", ""),
            }
            for q in quality_rows
        ]

        sla_rows = [dict(rr["sla"]) for rr in run_read(READ_SLA_TEMPLATE)]
        spec["slaProperties"] = [
            {"property": sr.get("property", ""), "value": sr.get("value", ""), "unit": sr.get("unit", "")}
            for sr in sla_rows
        ]

        if terms_node:
            spec["terms"] = {
                "usage": terms_node.get("usage", ""),
                "limitations": terms_node.get("limitations", ""),
                "billing": terms_node.get("billing", ""),
                "noticePeriod": terms_node.get("noticePeriod", ""),
            }

        # ── Inputs (consumer-aligned) ───────────────────────────────────
        # Materialised from :CONSUMES edges. contract_id is derivable from
        # the dprod_uri because :DProdDataProduct.uri = 'dprod:' + contract_id
        # (see DPROD_UPSERT_PRODUCT). Skip emitting the block when empty so
        # source-aligned and dpe-cf-without-inputs contracts stay clean.
        input_rows = list(run_read(READ_INPUTS_TEMPLATE))
        if input_rows:
            inputs = []
            for ir in input_rows:
                dprod_uri = ir["dprod_uri"] or ""
                contract_id_in = (
                    dprod_uri[len("dprod:"):] if dprod_uri.startswith("dprod:") else dprod_uri
                )
                inputs.append({
                    "dprod_uri": dprod_uri,
                    "contract_id": contract_id_in,
                    "name": ir["name"] or "",
                })
            spec["inputs"] = inputs

        # ── Blueprint-Library template metadata (only for :ProductTemplate) ──
        # These head props live outside the ODCS spec proper; emit them only
        # when present so ordinary project contracts stay byte-unchanged.
        if dc.get("isTemplate"):
            for k in (
                "isTemplate", "templateStatus", "templateOrigin",
                "templateOwnerEmail", "templateDomain", "sourceSpecId",
                "clonedFrom",
            ):
                v = dc.get(k)
                if v is not None:
                    spec[k] = v

    return spec


# ── ODCS → dprod transformation ────────────────────────────────────────────
# DProdColumn properties (dataType / isPrimaryKey) follow the DPROD spec, not
# ODCS, so their names stay — they are populated from v3.1 ODCS fields below.
# One :DProdOutputDataset per :DataContractSchema (so multi-schema contracts
# produce multiple datasets, not a single collapsed one).

DPROD_WIPE = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(port:DProdOutputPort)
OPTIONAL MATCH (port)-[:DPROD_OUTPUT_DATASET]->(ds:DProdOutputDataset)
OPTIONAL MATCH (ds)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (ds)-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
// DETACH DELETE on `ds` cascades to any :REFERENCES edges DPROD_PROPAGATE_FK
// mirrored onto these nodes, so they get cleared automatically.
DETACH DELETE port, ds, pc, dt
"""

# Mirror the project's catalog :Dataset-[:REFERENCES]->:Dataset edges onto
# the just-built :DProdOutputDataset nodes for this contract. Lets the
# consumer-side view-DDL FK-discover joins across the source product's
# per-dataset views without each consumer re-deriving the source schema.
#
# Match by physicalName == ds.name: source-aligned products' schema
# physicalName equals the catalog :Dataset.name (synthesize_odcs_from_graph
# uses the catalog table name verbatim). Consumer-aligned source products
# whose schemas don't mirror catalog datasets see an empty match — the query
# is a no-op for them.
#
# FK COLUMN RENAME (load-bearing): the catalog FK edge carries the ORIGINAL
# column names (e.g. `employee_id`), but the product's :DProdColumn / served
# table columns use the approved `recommendedName` (e.g. `hr_employee_id` after
# column-name standardization). Copying the FK columns verbatim makes the
# consumer view-DDL emit `ON t2.employee_id = t1.employee_id` against columns
# that no longer exist → an UNRESOLVED_COLUMN deploy failure. So translate each
# comma-joined column token through its :Column effective name (approved
# recommendedName else original — mirrors sa_pipeline._resolve_physical_name),
# preserving the comma-joined string form the consumer split logic expects.
DPROD_PROPAGATE_FK = """\
MATCH (:Project {projectCode: $project_code})-[:HAS_CATALOG]->(:Catalog)
      -[:DCAT_DATASET]->(ds_from:Dataset)
      -[r:REFERENCES]->(ds_to:Dataset)
MATCH (ods_from:DProdOutputDataset {uri: 'dprod:ds:' + $contract_id + ':' + ds_from.name})
MATCH (ods_to:DProdOutputDataset   {uri: 'dprod:ds:' + $contract_id + ':' + ds_to.name})
WITH r, ods_from, ods_to, ds_from, ds_to,
     [tok IN split(coalesce(r.columns, ''), ',') WHERE trim(tok) <> '' |
        coalesce(
          head([(ds_from)-[:HAS_COLUMN]->(c:Column)
                WHERE c.name = trim(tok)
                  AND c.recommendedNameStatus = 'approved'
                  AND coalesce(c.recommendedName, '') <> '' | c.recommendedName]),
          trim(tok))] AS fk_mapped,
     [tok IN split(coalesce(r.referencedColumns, ''), ',') WHERE trim(tok) <> '' |
        coalesce(
          head([(ds_to)-[:HAS_COLUMN]->(c:Column)
                WHERE c.name = trim(tok)
                  AND c.recommendedNameStatus = 'approved'
                  AND coalesce(c.recommendedName, '') <> '' | c.recommendedName]),
          trim(tok))] AS pk_mapped
MERGE (ods_from)-[new_r:REFERENCES]->(ods_to)
SET new_r.columns           = reduce(s = '', x IN fk_mapped | CASE WHEN s = '' THEN x ELSE s + ',' + x END),
    new_r.referencedColumns = reduce(s = '', x IN pk_mapped | CASE WHEN s = '' THEN x ELSE s + ',' + x END)
RETURN count(new_r) AS propagated
"""

# Copy the approved :TableDescription text + relationshipKind from each
# catalog :Dataset onto its corresponding :DProdOutputDataset. Lets consumer
# view-DDL bridge-ranker read the classification via the cheap ods.* path
# rather than walking back through the contract to the source project's
# :Dataset graph. Matches by physicalName (same convention as
# DPROD_PROPAGATE_FK). Only approved descriptions propagate so PO-rejected
# text never leaks into downstream products.
DPROD_PROPAGATE_TABLE_DESCRIPTION = """\
MATCH (:Project {projectCode: $project_code})-[:HAS_CATALOG]->(:Catalog)
      -[:DCAT_DATASET]->(ds:Dataset)
      -[:HAS_TABLE_DESCRIPTION]->(td:TableDescription)
WHERE td.isCurrent = true AND td.status = 'approved'
MATCH (ods:DProdOutputDataset {uri: 'dprod:ds:' + $contract_id + ':' + ds.name})
SET ods.description      = td.text,
    ods.relationshipKind = coalesce(td.relationshipKind, 'unknown')
RETURN count(ods) AS propagated
"""

# Phase 2: copy the schema-side :DatasetTransform onto the freshly-built
# :DProdOutputDataset so the view-DDL generator can read it without joining
# back through the contract. Source of truth is the schema-side node, which
# survives _generate_dprod; the ods-side copy is rebuilt on every run.
DPROD_COPY_DATASET_TRANSFORM = """\
MATCH (dc:DataContract {id: $contract_id})-[r1:HAS_SCHEMA]->(s:DataContractSchema {physicalName: $schema_physical_name})
       -[r2:HAS_DATASET_TRANSFORM]->(src:DatasetTransform)
WHERE """ + current_version_filter("r1") + """
  AND """ + current_version_filter("r2") + """
MATCH (ds:DProdOutputDataset {uri: 'dprod:ds:' + $contract_id + ':' + $schema_physical_name})
CREATE (ds)-[:HAS_DATASET_TRANSFORM]->(:DatasetTransform {
    outputDatasetUri:      ds.uri,
    contractId:            src.contractId,
    schemaPhysicalName:    src.schemaPhysicalName,
    filterPredicate:       src.filterPredicate,
    filterIntent:          coalesce(src.filterIntent, ''),
    dedupeJson:            src.dedupeJson,
    joinsJson:             src.joinsJson,
    groupingKeysJson:      src.groupingKeysJson,
    windowSpecsJson:       src.windowSpecsJson,
    scdPolicyJson:         src.scdPolicyJson,
    suppressedColumnsJson: src.suppressedColumnsJson,
    grainProse:            coalesce(src.grainProse, '')
})
"""

DPROD_UPSERT_PRODUCT = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (dp:DProdDataProduct {uri: 'dprod:' + dc.id})
SET dp.name = dc.name,
    dp.description = COALESCE(dc.description, dc.name, ''),
    dp.status = dc.status,
    dp.productKind = COALESCE(dc.productKind, ''),
    dp.createdAt = datetime()
MERGE (dc)-[:MATERIALISES_AS]->(dp)
MERGE (dp)-[:DPROD_OUTPUT_PORT]->(port:DProdOutputPort {uri: 'dprod:port:' + dc.id})
RETURN dp.uri AS dprod_uri, dp.name AS dprod_name, port.uri AS port_uri
"""

# One dataset per schema, keyed by physicalName to disambiguate multiple schemas.
DPROD_CREATE_DATASET = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})-[:DPROD_OUTPUT_PORT]->(port:DProdOutputPort)
MERGE (port)-[:DPROD_OUTPUT_DATASET]->(ds:DProdOutputDataset {
    uri: 'dprod:ds:' + $contract_id + ':' + $schema_physical_name
})
SET ds.name = $schema_name,
    ds.physicalName = $schema_physical_name,
    ds.description = $schema_description
RETURN ds.uri AS ds_uri
"""

# Columns scoped to their dataset — physical name qualified by schema so two
# schemas that share a column name don't collide.
DPROD_CREATE_COLUMN = """\
MATCH (ds:DProdOutputDataset {uri: 'dprod:ds:' + $contract_id + ':' + $schema_physical_name})
MATCH (p:DataContractProperty {
    contractId: $contract_id,
    schemaName: $schema_physical_name,
    physicalName: $property_physical_name
})
MERGE (ds)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn {
    uri: 'dprod:col:' + $contract_id + ':' + $schema_physical_name + ':' + $property_physical_name
})
SET pc.name = p.name,
    pc.searchName = toLower(p.name),
    pc.logicalName = p.logicalName,
    pc.dataType = p.physicalType,
    pc.logicalType = p.logicalType,
    pc.description = p.description,
    pc.isPrimaryKey = p.primaryKey,
    pc.datasetPhysicalName = $schema_physical_name,
    pc.transformHint = coalesce(p.transformHint, ''),
    pc.pii = coalesce(p.pii, false),
    pc.classification = coalesce(p.classification, ''),
    pc.sensitivity = coalesce(p.sensitivity,
                              CASE WHEN p.pii THEN 'pii' ELSE 'none' END),
    pc.sourceColumnUri = coalesce(p.sourceColumnUri, '')
"""

# Spec quality rules carry through to engineering as :PropertyShape nodes so
# the existing rule-review / DQ-testing surfaces (which key on PropertyShape)
# can see them. Tagged ruleSource='spec' and status='approved' since the
# ingested contract is the authoritative artifact — no PO review needed.
PERSIST_SPEC_RULE = """\
MATCH (ods:DProdOutputDataset {uri: $dataset_uri})
MATCH (pc:DProdColumn {uri: $col_uri})
// Split node-MERGE from path-MERGE so the shape is keyed by URI, not by a
// path from a freshly-created `ods`. DPROD_WIPE deletes `ods` (not `ns`), so a
// path-MERGE here would fail to match the surviving shape and CREATE a
// duplicate every rebuild — the bug that triplicated :DProdNodeShape. Keyed on
// URI, the shape survives the wipe and the rebuild just re-attaches HAS_SHAPE.
MERGE (ns:DProdNodeShape {uri: 'shape:' + $dataset_uri})
MERGE (ods)-[:HAS_SHAPE]->(ns)
MERGE (ps:PropertyShape {uri: $rule_uri})
ON CREATE SET ps.ruleSource = 'spec',
              ps.ruleTarget = 'dprod-column',
              ps.status = 'approved',
              ps.ruleType = $rule_type,
              ps.severity = $severity,
              ps.description = $description,
              ps.dimension = $dimension,
              ps.businessImpact = $business_impact,
              ps.confidence = 1.0,
              ps.suggestedAt = datetime()
ON MATCH SET  ps.severity = $severity,
              ps.description = $description,
              ps.dimension = $dimension,
              ps.businessImpact = $business_impact
MERGE (ns)-[:PROPERTY]->(ps)
MERGE (ps)-[:ON_DPROD_COLUMN]->(pc)
"""

# Read spec quality rules with their column/dataset refs back from the graph.
# We use the materialised :DataContractQuality nodes rather than re-parsing
# the YAML so this works for any contract version (ingested or wizard-built).
READ_SPEC_QUALITY_RULES = """\
MATCH (dc:DataContract {id: $contract_id})-[r:HAS_QUALITY_RULE]->(q:DataContractQuality)
WHERE """ + current_version_filter("r") + """
RETURN coalesce(q.rule, '')           AS rule_type,
       coalesce(q.name, '')           AS name,
       coalesce(q.description, '')    AS description,
       coalesce(q.severity, '')       AS severity,
       coalesce(q.dimension, '')      AS dimension,
       coalesce(q.businessImpact, '') AS business_impact,
       coalesce(q.column, '')         AS col_name,
       coalesce(q.dataset, '')        AS dataset_name
"""


# Phase 3 rename detection — capture the prior :DProdColumn snapshot
# (URI + schema + name) BEFORE DPROD_WIPE deletes them. After rebuild we
# diff the new set against this snapshot and write renamedFromUri on any
# new column that's a likely rename target. Consumer mappings whose source
# URI no longer resolves can use this property to suggest a rebind.
_READ_PRIOR_DPROD_COLUMNS = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
RETURN pc.uri AS uri,
       coalesce(pc.datasetPhysicalName, '') AS schema,
       coalesce(pc.name, '') AS name
"""


# Phase 3 stale-mapping preservation — before DPROD_WIPE deletes this
# source product's :DProdColumn nodes, find every consumer :ColumnMapping
# whose :MAPS_SOURCE_COLUMN edge points at one of those columns and copy
# the source URI / name / schema onto the mapping node as cached
# properties. The cross-project edge will be deleted when its target node
# is wiped; the cached properties survive so the consumer-side stale-
# mapping endpoint can recover what the mapping used to point at and
# suggest a rebind via the new column's renamedFromUri.
_CAPTURE_PRIOR_SOURCE_URIS = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
      <-[:MAPS_SOURCE_COLUMN]-(cm:ColumnMapping)
SET cm.priorSourceColumnUri = pc.uri,
    cm.priorSourceColumnName = coalesce(pc.name, ''),
    cm.priorSourceColumnSchema = coalesce(pc.datasetPhysicalName, '')
RETURN count(cm) AS captured
"""


# Phase 3 stale-mapping auto-reconnect — after DPROD_WIPE+rebuild, every
# consumer :ColumnMapping with a cached priorSourceColumnUri that matches
# a now-existing :DProdColumn URI gets its edge restored automatically.
# This handles the no-op case (rebuild created a column with the same URI)
# where the rename detector wouldn't apply. Mappings restored this way
# clear their cached "prior" properties so they fall out of the stale
# list. Mappings whose prior URI doesn't resolve to a current column
# stay stale and surface in /reviews/stale_mappings for rebind review.
_RECONNECT_UNCHANGED_MAPPINGS = """\
MATCH (cm:ColumnMapping)
WHERE cm.priorSourceColumnUri IS NOT NULL
  AND cm.priorSourceColumnUri STARTS WITH 'dprod:col:' + $contract_id + ':'
  AND NOT EXISTS { MATCH (cm)-[:MAPS_SOURCE_COLUMN]->() }
MATCH (pc:DProdColumn {uri: cm.priorSourceColumnUri})
MERGE (cm)-[:MAPS_SOURCE_COLUMN]->(pc)
REMOVE cm.priorSourceColumnUri,
       cm.priorSourceColumnName,
       cm.priorSourceColumnSchema
RETURN count(cm) AS reconnected
"""


# Phase 7: pre-wipe capture for the consumer's OWN mappings (the ones
# targeting our :DProdColumns via :MAPS_TO_PRODUCT_COLUMN). Mirrors
# Phase 3's _CAPTURE_PRIOR_SOURCE_URIS but caches the product-side URI
# instead of source-side. Survives DPROD_WIPE so the reconciliation
# (Phase 7.2) can identify which mappings to deactivate when a product
# column is removed in v(N+1), and the auto-reactivate (below) can
# re-establish the edge when the same URI gets rebuilt.
_CAPTURE_PRIOR_PRODUCT_URIS = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
      <-[:MAPS_TO_PRODUCT_COLUMN]-(cm:ColumnMapping)
WHERE cm.isCurrent = true
SET cm.priorProductColumnUri = pc.uri,
    cm.priorProductColumnName = coalesce(pc.name, ''),
    cm.priorProductColumnSchema = coalesce(pc.datasetPhysicalName, '')
RETURN count(cm) AS captured
"""


# Phase 7 auto-reactivate — after DPROD_WIPE+rebuild, any deactivated
# mapping (isCurrent=false) whose cached priorProductColumnUri matches
# a freshly-rebuilt :DProdColumn gets reactivated: flip isCurrent=true,
# rebuild the :MAPS_TO_PRODUCT_COLUMN edge, clear the cached props.
# Emits a :ProvActivity {activityType: 'mapping_reactivated'} sidecar
# attached via :PROV_USED so the full lifecycle history is preserved.
# Pattern follows reviews.py:APPROVE_MAPPING_QUERY for consistency with
# the existing mapping_review PROV pattern.
_REACTIVATE_REMATCHED_MAPPINGS = """\
MATCH (cm:ColumnMapping)
WHERE cm.isCurrent = false
  AND cm.priorProductColumnUri IS NOT NULL
  AND cm.priorProductColumnUri STARTS WITH 'dprod:col:' + $contract_id + ':'
MATCH (pc:DProdColumn {uri: cm.priorProductColumnUri})
SET cm.isCurrent = true
MERGE (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
WITH cm
MERGE (agent:ProvAgent {uri: 'prov:agent:system:reconciliation'})
  ON CREATE SET agent.agentType = 'system', agent.name = 'reconciliation'
CREATE (act:ProvActivity {
    uri:             'prov:activity:mapping-reactivated:' + replace(cm.uri, 'mapping:', '') + ':' + toString(timestamp()),
    activityType:    'mapping_reactivated',
    reason:          'product_column_readded',
    occurredAt:      datetime()
})
CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
CREATE (act)-[:PROV_USED]->(cm)
REMOVE cm.priorProductColumnUri,
       cm.priorProductColumnName,
       cm.priorProductColumnSchema
RETURN count(cm) AS reactivated
"""


# Post-rebuild cleanup: any active mapping whose target :DProdColumn was
# also part of the rebuild (same URI restored) should have its cached
# priorProductColumnUri cleared. The capture step set the props on all
# active mappings; for ones still active post-rebuild we don't need the
# cache. Saves us from re-clearing on every save.
# Phase 7 fix: when DPROD_WIPE deletes the consumer's own :DProdColumn
# nodes during odcs_to_dprod, every :MAPS_TO_PRODUCT_COLUMN edge from
# the consumer's active mappings to those columns is also deleted (Cypher
# DETACH DELETE takes adjacent edges). The rebuild creates NEW
# :DProdColumns with the same URIs for surviving columns, but the edges
# from the still-active mappings are gone — leaving the mappings active
# but orphaned. Result: serving DDL gen reports zero mappings even though
# isCurrent=true on every row.
#
# Fix: for every active mapping whose cached priorProductColumnUri matches
# a freshly-rebuilt :DProdColumn (surviving column case), MERGE the edge
# back, then clear the cache. Distinct from _REACTIVATE_REMATCHED_MAPPINGS
# which handles isCurrent=false (deactivated → re-added column case).
_RECONNECT_ACTIVE_MAPPINGS_BY_PRODUCT = """\
MATCH (cm:ColumnMapping)
WHERE cm.isCurrent = true
  AND cm.priorProductColumnUri IS NOT NULL
  AND cm.priorProductColumnUri STARTS WITH 'dprod:col:' + $contract_id + ':'
MATCH (pc:DProdColumn {uri: cm.priorProductColumnUri})
MERGE (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
REMOVE cm.priorProductColumnUri,
       cm.priorProductColumnName,
       cm.priorProductColumnSchema
RETURN count(cm) AS reconnected
"""


# Defensive fallback for legacy active mappings that lost their
# :MAPS_TO_PRODUCT_COLUMN edge (DPROD_WIPE) AND lack the
# priorProductColumnUri cache (the mapping was created before the
# Phase 7 capture logic existed, or the cache was already cleared).
# The mapping URI itself encodes the product column URI as a suffix
# after the final ":col:" separator: rebuild the edge by parsing.
# Scoped to the current contract so cross-project repair is impossible.
_RECONNECT_ACTIVE_MAPPINGS_BY_URI_SUFFIX = """\
MATCH (cm:ColumnMapping)
WHERE cm.isCurrent = true
  AND NOT (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(:DProdColumn)
  AND cm.uri CONTAINS ':col:'
WITH cm, 'dprod:col:' + split(cm.uri, ':col:')[-1] AS target_uri
WHERE target_uri STARTS WITH 'dprod:col:' + $contract_id + ':'
MATCH (pc:DProdColumn {uri: target_uri})
MERGE (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
RETURN count(cm) AS reconnected
"""


# PO-authored DQ rules preservation — before DPROD_WIPE deletes the
# product's :DProdColumn nodes, cache the target column URI onto every
# domain/user :PropertyShape anchored to one of them. DPROD_WIPE's
# DETACH DELETE on `pc` takes the :ON_DPROD_COLUMN edge with it, leaving
# the PropertyShape node itself intact but disconnected. Without this
# capture + the reconnect below, the PO's wizard-authored quality rules
# float orphaned after the engineer runs odcs_to_dprod and never appear
# in the marketplace Quality tab, the domain_rules review queue, or the
# DQ-rule summary counts (all of which traverse :ON_DPROD_COLUMN).
#
# Scoped to domain/user: `spec` PropertyShapes are re-materialised from
# :DataContractQuality by _materialise_spec_rules on every rebuild, and
# `observation` rules anchor to operational :Column (untouched by the
# dprod wipe), so neither needs this preservation path.
_CAPTURE_PRIOR_RULE_COLUMN_URIS = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
      <-[:ON_DPROD_COLUMN]-(ps:PropertyShape)
WHERE coalesce(ps.ruleSource, '') IN ['domain', 'user']
SET ps.priorColumnUri = pc.uri
RETURN count(ps) AS captured
"""


# Reconnect — after the column rebuild, every cached domain/user
# :PropertyShape whose priorColumnUri matches a freshly-rebuilt
# :DProdColumn (same deterministic URI = unchanged column) gets its
# :ON_DPROD_COLUMN edge restored and the cache cleared. Mirrors the
# :ColumnMapping reconnect (_RECONNECT_UNCHANGED_MAPPINGS). Rules whose
# prior column was renamed/removed don't resolve and stay orphaned with
# the cache retained, so a later re-add of the same-named column auto-
# reconnects them on the next rebuild.
_RECONNECT_RULES_BY_COLUMN_URI = """\
MATCH (ps:PropertyShape)
WHERE coalesce(ps.ruleSource, '') IN ['domain', 'user']
  AND ps.priorColumnUri IS NOT NULL
  AND ps.priorColumnUri STARTS WITH 'dprod:col:' + $contract_id + ':'
  AND NOT EXISTS { MATCH (ps)-[:ON_DPROD_COLUMN]->(:DProdColumn) }
MATCH (pc:DProdColumn {uri: ps.priorColumnUri})
MERGE (ps)-[:ON_DPROD_COLUMN]->(pc)
REMOVE ps.priorColumnUri
RETURN count(ps) AS reconnected
"""


# Guarded, idempotent collapse of exact-duplicate-URI :DProdNodeShape for this
# contract into a single survivor. The pre-fix path-MERGE (which MERGEd a shape
# on a path from a freshly-created `ods`) created a new shape every rebuild,
# orphaning the prior one — so a single contract could accumulate several shape
# nodes sharing one URI. With the split node-MERGE now in place, a residual
# duplicate would make `MERGE (ns:DProdNodeShape {uri})` raise "merge matched
# more than one node"; this heals that BEFORE the MERGE runs. All :PROPERTY
# edges from the duplicates are folded onto the survivor (MERGE dedups — the
# rules are the same :PropertyShape nodes), then the duplicates are deleted.
# A no-op once each URI is unique — this self-heals a product the moment it is
# next materialized or has its rules re-persisted (no global data sweep).
_COLLAPSE_DUP_SHAPES = """\
MATCH (ns:DProdNodeShape)
WHERE ns.uri STARTS WITH 'shape:dprod:ds:' + $contract_id + ':'
WITH ns.uri AS shape_uri, collect(ns) AS shapes
WHERE size(shapes) > 1
WITH head(shapes) AS keep, tail(shapes) AS dups
UNWIND dups AS dup
OPTIONAL MATCH (dup)-[:PROPERTY]->(ps:PropertyShape)
FOREACH (_ignore IN CASE WHEN ps IS NULL THEN [] ELSE [1] END |
  MERGE (keep)-[:PROPERTY]->(ps))
DETACH DELETE dup
"""


# Restore the :HAS_SHAPE edge for every rebuilt :DProdOutputDataset whose
# URI-keyed :DProdNodeShape survives. DPROD_WIPE deletes the old `ods`, which
# severs its outgoing :HAS_SHAPE (the shape node + its :PROPERTY edges survive);
# _materialise_spec_rules re-attaches HAS_SHAPE for `spec` shapes, but domain/
# user shapes are only re-attached at rule-authoring time — so without this
# they float with 0 incoming HAS_SHAPE after every rebuild. Idempotent (MERGE);
# a no-op when a dataset has no shape (no rules).
_RECONNECT_SHAPES_BY_DATASET = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
MATCH (ns:DProdNodeShape {uri: 'shape:' + ods.uri})
MERGE (ods)-[:HAS_SHAPE]->(ns)
RETURN count(ns) AS reconnected
"""


def _collapse_dup_shapes(ns, contract_id: str) -> None:
    """Best-effort collapse of duplicate :DProdNodeShape for a contract.

    Run before any shape node-MERGE (materialization + rule persistence) so the
    keyed MERGE can never match more than one node. Idempotent + guarded, so a
    clean graph is untouched. Never raises into the caller."""
    try:
        ns.run(_COLLAPSE_DUP_SHAPES, contract_id=contract_id).consume()
    except Exception:
        pass


# Deterministic re-anchor for a domain/user :PropertyShape that ended up with
# NO :ON_DPROD_COLUMN edge (a legacy orphan whose priorColumnUri capture was
# missed — the reconnect above only fires when priorColumnUri is present). The
# rule URI encodes its target column: domain = rule:{code}:dprod:{ds}:{col}:…
# and user = rule:{code}:user:{ds}:{col}:… — so the :DProdColumn URI is
# 'dprod:col:{contract}:{ds}:{col}'. Re-anchor only when that exact column
# exists (renamed/removed columns stay orphaned, by design). Idempotent; a
# no-op once every rule is anchored. Complements the priorColumnUri reconnect
# for rules that predate it, and is a durable safety net for future misses.
_RECONNECT_ORPHANED_RULES_BY_URI = """\
MATCH (ps:PropertyShape)
WHERE coalesce(ps.ruleSource, '') IN ['domain', 'user']
  AND ps.uri IS NOT NULL
  AND size(split(ps.uri, ':')) >= 5
  AND split(ps.uri, ':')[1] = $project_code
  AND split(ps.uri, ':')[2] IN ['dprod', 'user']
  AND NOT EXISTS { MATCH (ps)-[:ON_DPROD_COLUMN]->(:DProdColumn) }
WITH ps, 'dprod:col:' + $contract_id + ':'
        + split(ps.uri, ':')[3] + ':' + split(ps.uri, ':')[4] AS target_uri
MATCH (pc:DProdColumn {uri: target_uri})
MERGE (ps)-[:ON_DPROD_COLUMN]->(pc)
RETURN count(ps) AS reconnected
"""


_SET_RENAMED_FROM = """\
MATCH (pc:DProdColumn {uri: $new_uri})
SET pc.renamedFromUri = $prior_uri,
    pc.renamedFromName = $prior_name
"""


def _detect_renames_per_schema(
    prior_cols: list[dict],
    new_cols_by_schema: dict[str, list[str]],
) -> dict[str, dict[str, str]]:
    """Per-schema rename pairings.

    Returns ``{schema: {new_name: prior_uri}}`` for likely renames.

    Heuristic: within each schema, the columns in the prior set that aren't
    in the new set are "removed candidates"; columns in the new set that
    weren't in the prior are "added candidates". When the counts match
    1-to-1 we treat the lone added as a rename of the lone removed. With
    multiple of each, we pair by Jaccard token similarity (split on ``_``).
    Pairs below a similarity floor are skipped — the column wasn't renamed,
    it was just dropped and a different one was added.
    """
    prior_by_schema: dict[str, dict[str, str]] = {}
    for r in prior_cols:
        prior_by_schema.setdefault(r["schema"], {})[r["name"]] = r["uri"]

    pairings: dict[str, dict[str, str]] = {}
    for schema, new_names in new_cols_by_schema.items():
        prior_in_schema = prior_by_schema.get(schema, {})
        if not prior_in_schema:
            continue
        new_set = set(new_names)
        prior_set = set(prior_in_schema.keys())
        added = list(new_set - prior_set)
        removed = list(prior_set - new_set)
        if not added or not removed:
            continue

        def tokens(name: str) -> set[str]:
            return {t for t in name.lower().split("_") if t}

        def sim(a: str, b: str) -> float:
            ta, tb = tokens(a), tokens(b)
            if not ta or not tb:
                return 0.0
            return len(ta & tb) / len(ta | tb)

        # Greedy best-match pairing. Compute every pair's similarity, sort
        # descending, claim each pair exactly once. Skip pairs below a
        # 0.34 floor (one shared token in a typical 2-3-token name).
        candidates: list[tuple[float, str, str]] = []
        for n in added:
            for o in removed:
                s = sim(n, o)
                if s >= 0.34:
                    candidates.append((s, n, o))
        candidates.sort(reverse=True)
        used_new: set[str] = set()
        used_old: set[str] = set()
        for s, n, o in candidates:
            if n in used_new or o in used_old:
                continue
            pairings.setdefault(schema, {})[n] = prior_in_schema[o]
            used_new.add(n)
            used_old.add(o)
        # If exactly one added + one removed and zero similarity threshold
        # hits, still flag the pair — most likely an unrelated-looking
        # rename (e.g. "value" → "amount") where the engineer expects to
        # see the suggestion even at low confidence.
        if len(added) == 1 and len(removed) == 1 and added[0] not in used_new:
            pairings.setdefault(schema, {})[added[0]] = prior_in_schema[removed[0]]
    return pairings


def _generate_dprod(contract_id: str, project: Project) -> dict:
    """Run the ODCS→DPROD transformation. Creates one DProdOutputDataset per
    ODCS schema, with DProdColumns scoped to each dataset, then materialises
    any spec-embedded quality rules as :PropertyShape nodes anchored to the
    matching DProdColumns."""
    # Blueprint-Library templates are never materialised into a data product —
    # they carry no :DProdDataProduct. Refuse a template id defensively so the
    # isolation invariant holds even if a stray caller reaches here.
    if (contract_id or "").startswith("template:"):
        return {"skipped": "product_template", "contract_id": contract_id}
    with _neo4j(project) as ns:
        # Phase 3: snapshot the prior :DProdColumn set before the wipe so we
        # can write renamedFromUri on any new column that's a likely rename.
        prior_dprod_cols = [dict(r) for r in ns.run(_READ_PRIOR_DPROD_COLUMNS, contract_id=contract_id)]

        # Phase 3: also cache the source URI on every consumer :ColumnMapping
        # pointing at any of our :DProdColumns. The :MAPS_SOURCE_COLUMN edge
        # is deleted by DPROD_WIPE (it crosses project boundaries to the
        # source's :DProdColumn, and DETACH DELETE on the node takes the
        # edge with it). The cached property survives so the consumer's
        # stale-mapping endpoint can offer a rebind suggestion via the
        # source's new :DProdColumn.renamedFromUri.
        ns.run(_CAPTURE_PRIOR_SOURCE_URIS, contract_id=contract_id)

        # Phase 7: cache the product-side URI on every OWN active :ColumnMapping
        # (the consumer's mappings targeting their own :DProdColumns).
        # DPROD_WIPE deletes the :MAPS_TO_PRODUCT_COLUMN edge too; this cache
        # lets the post-rebuild step reactivate deactivated mappings when
        # their original target is restored (re-added column).
        ns.run(_CAPTURE_PRIOR_PRODUCT_URIS, contract_id=contract_id)

        # Cache the target column URI on the PO's domain/user DQ rules
        # before the wipe deletes the :ON_DPROD_COLUMN edges (see the
        # query comment). Reconnected below after the rebuild.
        ns.run(_CAPTURE_PRIOR_RULE_COLUMN_URIS, contract_id=contract_id)

        # Heal any duplicate :DProdNodeShape this contract accumulated under the
        # pre-fix path-MERGE, BEFORE the split node-MERGE runs (in
        # _materialise_spec_rules) — otherwise a keyed MERGE against residual
        # duplicates raises "merge matched more than one node". Idempotent no-op
        # on a clean graph; self-heals the product on this materialization.
        _collapse_dup_shapes(ns, contract_id)

        # Clear out any prior DPROD substructure so schema renames/removals
        # don't leave orphaned datasets or columns behind.
        ns.run(DPROD_WIPE, contract_id=contract_id)

        row = ns.run(DPROD_UPSERT_PRODUCT, contract_id=contract_id).single()
        if not row:
            return {}

        result = {
            "dprod_uri": row["dprod_uri"],
            "dprod_name": row["dprod_name"],
            "datasets": [],
        }

        # Track new (schema → list of property names) so we can run rename
        # detection across the rebuilt set.
        new_cols_by_schema: dict[str, list[str]] = {}

        # Fetch schemas + their property physicalNames, then build one dataset
        # per schema with its columns.
        schema_rows = list(ns.run(READ_SCHEMAS, contract_id=contract_id))
        for sr in schema_rows:
            s = dict(sr["s"])
            phys = s.get("physicalName") or s.get("name", "")
            if not phys:
                continue
            ns.run(DPROD_CREATE_DATASET,
                   contract_id=contract_id,
                   schema_physical_name=phys,
                   schema_name=s.get("name", ""),
                   schema_description=s.get("description", ""))

            # Phase 2: propagate the schema-side :DatasetTransform onto the
            # ods. No-op if the schema has no transform block (no match → no
            # CREATE). The DPROD_WIPE earlier already cleared any prior copy.
            ns.run(DPROD_COPY_DATASET_TRANSFORM,
                   contract_id=contract_id,
                   schema_physical_name=phys)

            prop_count = 0
            for pr in ns.run(READ_PROPERTIES, contract_id=contract_id, schema_name=phys):
                p = dict(pr["p"])
                p_phys = p.get("physicalName") or p.get("name", "")
                if not p_phys:
                    continue
                ns.run(DPROD_CREATE_COLUMN,
                       contract_id=contract_id,
                       schema_physical_name=phys,
                       property_physical_name=p_phys)
                prop_count += 1
                new_cols_by_schema.setdefault(phys, []).append(p_phys)
            result["datasets"].append({
                "name": s.get("name", ""),
                "physicalName": phys,
                "column_count": prop_count,
            })

        # Phase 3: write renamedFromUri on new columns that are likely
        # renames of removed prior columns. Same-schema constraint avoids
        # cross-schema confusion. Surfaced via the consumer-side stale-mapping
        # endpoint as rebind suggestions.
        rename_pairings = _detect_renames_per_schema(prior_dprod_cols, new_cols_by_schema)
        rename_count = 0
        for schema, name_to_prior_uri in rename_pairings.items():
            for new_name, prior_uri in name_to_prior_uri.items():
                new_uri = f"dprod:col:{contract_id}:{schema}:{new_name}"
                prior_name = prior_uri.rsplit(":", 1)[-1]
                ns.run(_SET_RENAMED_FROM, new_uri=new_uri, prior_uri=prior_uri, prior_name=prior_name)
                rename_count += 1
        if rename_count:
            result["renames_detected"] = rename_count

        # Phase 3: auto-reconnect any consumer mappings whose cached prior
        # source URI matches a freshly-rebuilt :DProdColumn with the same
        # URI (no-op rebuild for that column). Mappings restored this way
        # clear their "prior" caches and disappear from the stale-mapping
        # surface. Those whose prior URI no longer resolves stay cached
        # so the consumer can rebind via the rename suggestion or manually.
        reconnect_row = ns.run(_RECONNECT_UNCHANGED_MAPPINGS, contract_id=contract_id).single()
        if reconnect_row and reconnect_row.get("reconnected"):
            result["mappings_reconnected"] = reconnect_row["reconnected"]

        # Phase 7: auto-reactivate any deactivated mappings whose cached
        # priorProductColumnUri matches a freshly-rebuilt :DProdColumn.
        # This handles the "removed in vN then re-added in vN+1" case:
        # the deactivated mapping is brought back as the active mapping
        # without the engineer re-picking the source. Emits a
        # :ProvActivity {activityType: 'mapping_reactivated'} for audit.
        reactivate_row = ns.run(_REACTIVATE_REMATCHED_MAPPINGS, contract_id=contract_id).single()
        if reactivate_row and reactivate_row.get("reactivated"):
            result["mappings_reactivated"] = reactivate_row["reactivated"]

        # Phase 7 fix: for active mappings whose target :DProdColumn was
        # wiped+rebuilt with the same URI (the surviving-columns case in a
        # column-removal edit), restore the :MAPS_TO_PRODUCT_COLUMN edge
        # and clear the cache. DPROD_WIPE's DETACH DELETE took those edges
        # with the deleted columns; without this rebuild, the mappings end
        # up orphaned (active but with no edge) and serving DDL gen reports
        # zero mappings.
        active_reconnect_row = ns.run(_RECONNECT_ACTIVE_MAPPINGS_BY_PRODUCT, contract_id=contract_id).single()
        if active_reconnect_row and active_reconnect_row.get("reconnected"):
            result["active_mappings_reconnected"] = active_reconnect_row["reconnected"]

        # Defensive fallback: any active mapping STILL orphaned at this
        # point predates the Phase 7 capture logic. The mapping URI
        # encodes the product column URI as the suffix after the final
        # ":col:" — parse and rebuild the edge so the consumer's serving
        # DDL gen has mappings to walk.
        legacy_reconnect_row = ns.run(
            _RECONNECT_ACTIVE_MAPPINGS_BY_URI_SUFFIX, contract_id=contract_id
        ).single()
        if legacy_reconnect_row and legacy_reconnect_row.get("reconnected"):
            result["legacy_mappings_reconnected"] = legacy_reconnect_row["reconnected"]

        # Restore the PO's domain/user DQ-rule edges that DPROD_WIPE severed
        # when it deleted the prior :DProdColumn nodes. Unchanged columns are
        # rebuilt with the same deterministic URI, so the cached rules
        # reconnect; renamed/removed columns leave their rules orphaned.
        rule_reconnect_row = ns.run(
            _RECONNECT_RULES_BY_COLUMN_URI, contract_id=contract_id
        ).single()
        if rule_reconnect_row and rule_reconnect_row.get("reconnected"):
            result["rules_reconnected"] = rule_reconnect_row["reconnected"]

        spec_count = _materialise_spec_rules(ns, contract_id, result["datasets"])
        if spec_count:
            result["spec_rules_persisted"] = spec_count

        # Deterministically re-anchor any domain/user rule still orphaned after
        # the priorColumnUri reconnect — re-derives the target :DProdColumn from
        # the rule URI. Heals legacy orphans (e.g. a domain allowedValues rule
        # whose capture was missed) so it reappears in the review queue,
        # marketplace Quality tab, and DQ counts.
        orphan_rule_row = ns.run(
            _RECONNECT_ORPHANED_RULES_BY_URI,
            contract_id=contract_id,
            project_code=project.project_code,
        ).single()
        if orphan_rule_row and orphan_rule_row.get("reconnected"):
            result["orphan_rules_reanchored"] = orphan_rule_row["reconnected"]

        # Restore :HAS_SHAPE for domain/user shapes whose edge DPROD_WIPE severed
        # when it deleted the old ods. The surviving shape (+ its :PROPERTY edges)
        # is re-attached to the rebuilt ods by URI. Idempotent; no-op when a
        # dataset carries no rules. Spec shapes were already re-attached above.
        shape_reconnect_row = ns.run(
            _RECONNECT_SHAPES_BY_DATASET, contract_id=contract_id
        ).single()
        if shape_reconnect_row and shape_reconnect_row.get("reconnected"):
            result["shapes_reconnected"] = shape_reconnect_row["reconnected"]

        # Mirror catalog :Dataset-[:REFERENCES]->:Dataset edges onto the just-
        # built :DProdOutputDataset nodes so any downstream consumer product
        # that CONSUMES this one can FK-discover joins across the per-dataset
        # views. No-op for ingested-ODCS / consumer-aligned source products
        # whose schemas don't mirror catalog datasets — the MATCH finds
        # nothing and the MERGE doesn't fire.
        fk_row = ns.run(
            DPROD_PROPAGATE_FK,
            contract_id=contract_id,
            project_code=project.project_code,
        ).single()
        if fk_row and fk_row.get("propagated"):
            result["fk_edges_propagated"] = fk_row["propagated"]

        # Propagate approved :TableDescription text + relationshipKind from
        # catalog :Dataset onto the corresponding :DProdOutputDataset. Lets
        # downstream consumer view-DDL ranker (and marketplace surfaces)
        # read the classification + description directly off the ods node.
        td_row = ns.run(
            DPROD_PROPAGATE_TABLE_DESCRIPTION,
            contract_id=contract_id,
            project_code=project.project_code,
        ).single()
        if td_row and td_row.get("propagated"):
            result["table_descriptions_propagated"] = td_row["propagated"]

    return result


def _materialise_spec_rules(ns, contract_id: str, datasets: list[dict]) -> int:
    """Walk :DataContractQuality nodes for the contract and create
    :PropertyShape {ruleSource='spec', status='approved'} for any rule that
    has an identifiable column. Skips dataset-level rules (no column anchor)
    so engineering's PropertyShape-keyed surfaces don't get cluttered with
    free-floating rules.

    When a rule has a ``column`` but no explicit ``dataset`` and the contract
    has exactly one schema, we default to that schema. Multi-schema contracts
    require an explicit dataset on the rule.
    """
    rules = list(ns.run(READ_SPEC_QUALITY_RULES, contract_id=contract_id))
    if not rules:
        return 0

    only_dataset = datasets[0]["physicalName"] if len(datasets) == 1 else None
    valid_datasets = {d["physicalName"] for d in datasets}

    persisted = 0
    for i, r in enumerate(rules):
        col = (r.get("col_name") or "").strip()
        if not col:
            continue
        ds_phys = (r.get("dataset_name") or "").strip() or only_dataset
        if not ds_phys or ds_phys not in valid_datasets:
            continue

        rule_type = (r.get("rule_type") or "spec").strip() or "spec"
        rule_uri = (
            f"rule:{contract_id}:spec:{ds_phys}:{col}:{rule_type}:{i}"
        )
        col_uri = f"dprod:col:{contract_id}:{ds_phys}:{col}"
        dataset_uri = f"dprod:ds:{contract_id}:{ds_phys}"

        # Severity normalisation — ODCS uses "error"/"warning"; the rest of
        # the workbench uses "sh:Violation"/"sh:Warning".
        sev_in = (r.get("severity") or "").lower()
        severity = (
            "sh:Violation" if sev_in in ("error", "violation", "sh:violation")
            else "sh:Warning"
        )

        ns.run(
            PERSIST_SPEC_RULE,
            dataset_uri=dataset_uri,
            col_uri=col_uri,
            rule_uri=rule_uri,
            rule_type=rule_type,
            severity=severity,
            description=r.get("description") or r.get("name") or "",
            dimension=r.get("dimension") or "",
            business_impact=r.get("business_impact") or "",
        )
        persisted += 1
    return persisted


# ── REST Endpoints (project-scoped) ───────────────────────────────────────

project_router = APIRouter(prefix="/api/projects/{project_id}/odcs", tags=["odcs"])


class ODCSSpecInput(BaseModel):
    spec: dict
    submitted_by: Optional[str] = None
    # Phase 1: PO-controlled inline-vs-new-version. ``auto`` runs the
    # classifier against the prior spec; explicit values let the PO
    # override (e.g. force a cosmetic patch when the system recommended
    # a new version).
    change_kind: Optional[str] = "auto"   # 'auto' | 'cosmetic' | 'schema' | 'breaking'
    revision_notes: Optional[str] = ""


class SuggestDomainRulesInput(BaseModel):
    persist: bool = False


class TagsInput(BaseModel):
    tags: list[str] = []


@project_router.put("/tags")
def save_tags(
    project_id: int,
    body: TagsInput,
    session: Session = Depends(get_session),
    _role=Depends(require_role("owner")),
):
    """Set a product's free-form tags directly on ``:DataContract.tags``.

    A lightweight, PO-facing metadata edit that does NOT branch a contract
    version — tags are labels, not versioned contract terms (the marketplace
    reads ``dc.tags`` off the head). Trims + case-insensitively dedupes and
    returns the cleaned list. This is the "add tags after the fact" surface the
    marketplace product-detail Tags editor calls; the wizard authors tags into
    the full spec instead.
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    tags = _clean_tags(body.tags)
    with _neo4j(project) as ns:
        row = ns.run(
            "MATCH (dc:DataContract {id: $id}) SET dc.tags = $tags RETURN dc.id AS id",
            id=contract_id, tags=tags,
        ).single()
    if not row:
        raise HTTPException(404, f"No data contract for project '{project.project_code}' — save the product spec first.")
    return {"contract_id": contract_id, "tags": tags}


class UserRuleInput(BaseModel):
    column: str
    rule_type: str
    severity: Optional[str] = "sh:Warning"
    description: Optional[str] = ""
    params: Optional[dict] = None


class PersistUserRulesInput(BaseModel):
    rules: list[UserRuleInput]
    created_by: Optional[str] = None


READ_LIFECYCLE_META = """\
MATCH (dc:DataContract {id: $contract_id})
RETURN coalesce(dc.currentLifecycleState, 'draft') AS lifecycle_state,
       coalesce(dc.currentVersion, 1) AS current_version,
       dc.lastRejectionCategory AS rejection_category,
       dc.lastRejectionReason AS rejection_reason,
       dc.lastRejectedBy AS rejection_by,
       dc.lastRejectedAt AS rejection_at
"""


@project_router.get("")
def get_odcs(project_id: int, session: Session = Depends(get_session)):
    """Get current ODCS spec from graph for this project, plus lifecycle
    metadata the wizard uses (notably rejection details for rejected
    contracts, so the PO sees why a request came back to them)."""
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    spec = _read_odcs_from_graph(contract_id, project)
    meta: dict[str, Any] = {"lifecycle_state": "draft"}
    try:
        with _neo4j(project) as ns:
            row = ns.run(READ_LIFECYCLE_META, contract_id=contract_id).single()
            if row:
                v = row["current_version"]
                meta = {
                    "lifecycle_state": row["lifecycle_state"] or "draft",
                    "current_version": v,
                    # Backwards-compat synthesis so the frontend's existing
                    # versioned_id usage keeps working.
                    "versioned_id": f"{contract_id}:v{v}" if v else None,
                    "rejection": {
                        "category": row["rejection_category"],
                        "reason": row["rejection_reason"],
                        "by": row["rejection_by"],
                        "at": str(row["rejection_at"]) if row["rejection_at"] else None,
                    } if row["rejection_category"] or row["rejection_reason"] else None,
                }
    except Exception:
        pass
    if not spec:
        return {"spec": None, "contract_id": contract_id, **meta}
    return {"spec": spec, "contract_id": contract_id, **meta}


@project_router.put("")
def save_odcs(project_id: int, body: ODCSSpecInput, session: Session = Depends(get_session)):
    """Save/update ODCS spec to graph.

    Three save paths, picked by the head's lifecycleState and the
    classifier-resolved ``change_kind``:
      - Head in {draft, ingesting} → in-place update on the current cv.
      - Head non-draft AND change_kind=='cosmetic' (or 'auto' → cosmetic) →
        patch the current cv's snapshot fields + emit :ProvActivity
        ContractPatch. No version bump.
      - Otherwise → branch a new :ContractVersion (currentVersion++) with
        :PROV_WAS_DERIVED_FROM lineage to the prior cv.

    Returns the resolved ``change_kind`` so the UI can confirm whether the
    save patched in place or created a new version.
    """
    project = _get_project(project_id, session)
    spec = body.spec
    # Always use project-scoped contract ID so all endpoints agree
    spec["id"] = f"{project.project_code}-contract"
    resolved_kind = body.change_kind or "auto"
    if resolved_kind == "auto":
        # Read the current view to feed the classifier. None when this is
        # the initial save (no prior state); classifier handles that case
        # and returns 'schema'.
        prior = _read_odcs_from_graph(spec["id"], project)
        from ._change_classify import classify_diff
        resolved_kind = classify_diff(prior, spec)["kind"]
    try:
        saved = _save_odcs_to_graph(
            spec, project,
            submitted_by=body.submitted_by or "",
            change_kind=resolved_kind,
            revision_notes=body.revision_notes or "",
        )
    except ConsumesBindingError as e:
        # A refused upstream binding (self / cycle / ineligible target) is a
        # 409 the wizard can render inline; nothing was persisted.
        raise HTTPException(
            409,
            {"message": str(e), "reason": e.reason, "offending_uri": e.offending_uri},
        )
    # The consumer's :CONSUMES edges just synced — flip any matching intake
    # pending-dependencies to 'bound'. Best-effort; a dpe-sa save is a no-op.
    try:
        from ..intake_scaffold import resolve_pending_dependencies
        resolve_pending_dependencies(session, project, spec.get("inputs"))
    except Exception:
        pass
    return {
        "contract_id": saved.contract_id,
        "status": "saved",
        "change_kind": resolved_kind,
        # save_mode ∈ {initial, in_place, patch, branch}; `branched` + `new_version`
        # tell the caller (esp. MCP agents) when this save cut a new contract version.
        "save_mode": saved.save_mode,
        "new_version": saved.save_version,
        "branched": saved.save_mode == "branch",
    }


LIST_VERSIONS_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_VERSION]->(cv:ContractVersion)
RETURN cv.version AS version,
       cv.uri AS versioned_id,
       coalesce(cv.lifecycleState, 'draft') AS lifecycle_state,
       coalesce(cv.lifecycleState, 'draft') AS status,
       cv.occurredAt AS created_at,
       cv.occurredAt AS updated_at,
       (cv.version = dc.currentVersion) AS is_current
ORDER BY cv.version DESC
"""


@project_router.get("/versions")
def list_odcs_versions(project_id: int, session: Session = Depends(get_session)):
    """List every lifecycle version of this project's ODCS contract, newest first."""
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    with _neo4j(project) as ns:
        rows = [dict(r) for r in ns.run(LIST_VERSIONS_QUERY, contract_id=contract_id)]
    return {"versions": rows, "contract_id": contract_id}


@project_router.get("/versions/{version}")
def get_odcs_version(project_id: int, version: int, session: Session = Depends(get_session)):
    """Materialise a specific lifecycle version as an ODCS spec dict."""
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    spec = _read_odcs_from_graph(contract_id, project, version=version)
    if not spec:
        raise HTTPException(404, f"Version {version} not found for this contract")
    return {"spec": spec, "version": version, "contract_id": contract_id}


def _odcs_diff(old: dict, new: dict, prefix: str = "") -> dict:
    """Recursive JSON diff keyed by stable dotted paths.

    Lists are compared positionally (simple but sufficient for the ODCS shape
    where most arrays are dicts keyed by name/physicalName). Returns
    ``{added, removed, changed}`` each mapping path → value (or
    old/new pair for changed).
    """
    added: dict = {}
    removed: dict = {}
    changed: dict = {}

    def walk(a: object, b: object, path: str) -> None:
        if a == b:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for k in a:
                sub = f"{path}.{k}" if path else k
                if k not in b:
                    removed[sub] = a[k]
                else:
                    walk(a[k], b[k], sub)
            for k in b:
                if k not in a:
                    sub = f"{path}.{k}" if path else k
                    added[sub] = b[k]
            return
        if isinstance(a, list) and isinstance(b, list):
            for i in range(max(len(a), len(b))):
                sub = f"{path}[{i}]"
                if i >= len(a):
                    added[sub] = b[i]
                elif i >= len(b):
                    removed[sub] = a[i]
                else:
                    walk(a[i], b[i], sub)
            return
        changed[path] = {"from": a, "to": b}

    walk(old, new, prefix)
    return {"added": added, "removed": removed, "changed": changed}


@project_router.get("/diff")
def diff_odcs_versions(
    project_id: int,
    from_version: int = 0,
    to_version: int = 0,
    session: Session = Depends(get_session),
):
    """Return a JSON diff between two lifecycle versions."""
    if from_version <= 0 or to_version <= 0:
        raise HTTPException(400, "from_version and to_version query params are required")
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    old = _read_odcs_from_graph(contract_id, project, version=from_version)
    new = _read_odcs_from_graph(contract_id, project, version=to_version)
    if not old or not new:
        raise HTTPException(404, "One or both versions not found")
    return {
        "from_version": from_version,
        "to_version": to_version,
        "contract_id": contract_id,
        "diff": _odcs_diff(old, new),
    }


READ_DPROD_COLUMNS = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
RETURN pc.uri AS uri,
       pc.name AS name,
       pc.dataType AS data_type,
       ods.physicalName AS dataset_physical_name,
       ods.uri AS dataset_uri
"""

PERSIST_SUGGESTED_RULE = """\
MATCH (ods:DProdOutputDataset {uri: $dataset_uri})
MATCH (pc:DProdColumn {uri: $col_uri})
// Split node-MERGE from path-MERGE so the shape is keyed by URI, not by a
// path from a freshly-created `ods`. DPROD_WIPE deletes `ods` (not `ns`), so a
// path-MERGE here would fail to match the surviving shape and CREATE a
// duplicate every rebuild — the bug that triplicated :DProdNodeShape. Keyed on
// URI, the shape survives the wipe and the rebuild just re-attaches HAS_SHAPE.
MERGE (ns:DProdNodeShape {uri: 'shape:' + $dataset_uri})
MERGE (ods)-[:HAS_SHAPE]->(ns)
MERGE (ps:PropertyShape {uri: $rule_uri})
ON CREATE SET ps.ruleSource = 'domain',
              ps.ruleTarget = 'dprod-column',
              ps.status = 'pending_review',
              ps.ruleType = $rule_type,
              ps.severity = $severity,
              ps.description = $description,
              ps.params = $params,
              ps.confidence = $confidence,
              ps.suggestedAt = datetime()
MERGE (ns)-[:PROPERTY]->(ps)
MERGE (ps)-[:ON_DPROD_COLUMN]->(pc)
RETURN ps.uri AS rule_uri, ps.status AS status
"""

# User-authored rules surface from chat-driven rule_create suggestions or
# (later) a manual "Add rule" composer. They share the same DProdColumn
# anchor as domain rules but get ruleSource='user' so the engineer can
# tell them apart, and stamp createdBy for provenance.
PERSIST_USER_RULE = """\
MATCH (ods:DProdOutputDataset {uri: $dataset_uri})
MATCH (pc:DProdColumn {uri: $col_uri})
// Split node-MERGE from path-MERGE so the shape is keyed by URI, not by a
// path from a freshly-created `ods`. DPROD_WIPE deletes `ods` (not `ns`), so a
// path-MERGE here would fail to match the surviving shape and CREATE a
// duplicate every rebuild — the bug that triplicated :DProdNodeShape. Keyed on
// URI, the shape survives the wipe and the rebuild just re-attaches HAS_SHAPE.
MERGE (ns:DProdNodeShape {uri: 'shape:' + $dataset_uri})
MERGE (ods)-[:HAS_SHAPE]->(ns)
MERGE (ps:PropertyShape {uri: $rule_uri})
ON CREATE SET ps.ruleSource = 'user',
              ps.ruleTarget = 'dprod-column',
              ps.status = 'pending_review',
              ps.ruleType = $rule_type,
              ps.severity = $severity,
              ps.description = $description,
              ps.params = $params,
              ps.confidence = 1.0,
              ps.createdBy = $created_by,
              ps.suggestedAt = datetime()
MERGE (ns)-[:PROPERTY]->(ps)
MERGE (ps)-[:ON_DPROD_COLUMN]->(pc)
RETURN ps.uri AS rule_uri, ps.status AS status
"""


def _load_domain_catalog(domain: str) -> list[dict]:
    """Load the union of common.yaml and the domain-specific catalog.

    Rule matching in suggest-domain-rules is keyed on column name alone,
    so common canonicals (id, created_at, updated_at, is_deleted) also
    need their recommended_rules surfaced — otherwise a product built
    mostly from the common starter set gets zero suggestions.
    """
    catalog_dir = BASE_DIR / "playbook" / "domain_catalogs"
    merged: list[dict] = []
    for path in (catalog_dir / "common.yaml", catalog_dir / f"{domain}.yaml"):
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception:
            continue
        cols = data.get("columns") if isinstance(data, dict) else []
        if isinstance(cols, list):
            merged.extend(c for c in cols if isinstance(c, dict))
    return merged


def _derive_rule_description(col_name: str, rule: dict) -> str:
    rt = rule.get("type", "")
    if rt == "notNull":
        return f"{col_name} must not be null."
    if rt == "unique":
        return f"{col_name} must be unique across the dataset."
    if rt == "allowedValues":
        values = rule.get("values") or rule.get("allowed") or []
        return f"{col_name} must be one of: {', '.join(str(v) for v in values)}."
    if rt == "range":
        lo, hi = rule.get("min"), rule.get("max")
        if lo is not None and hi is not None:
            return f"{col_name} must be between {lo} and {hi}."
        if lo is not None:
            return f"{col_name} must be ≥ {lo}."
        if hi is not None:
            return f"{col_name} must be ≤ {hi}."
    if rt == "regex":
        return f"{col_name} must match pattern {rule.get('pattern', '')}."
    if rt == "maxLength":
        return f"{col_name} must be at most {rule.get('value', '')} characters."
    return f"{rt} constraint on {col_name}"


@project_router.post("/suggest-domain-rules")
def suggest_domain_rules(
    project_id: int,
    body: SuggestDomainRulesInput,
    session: Session = Depends(get_session),
):
    """Suggest domain-quality rules for the draft contract's :DProdColumn nodes.

    Rule source: ``playbook/domain_catalogs/{domain}.yaml``. For each
    :DProdColumn whose name matches a catalog column with
    ``recommended_rules``, we return a candidate :PropertyShape. When
    ``persist`` is true, candidates are written as
    ``ruleSource='domain', status='pending_review'`` so the PO can
    approve/reject them via the existing ``domain_rules`` review endpoints.

    A richer LLM-driven suggestion path (the refactored
    ``domain-rule-enhancement`` skill) is a Phase 3 enhancement. For the demo,
    catalog-driven suggestions are deterministic, cheap, and sufficient to
    exercise the end-to-end flow.
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    domain = (project.domain or "").strip().lower()
    if not domain:
        return {
            "contract_id": contract_id,
            "domain": "",
            "rules": [],
            "columns_without_match": [],
            "persisted": False,
            "note": "Project has no domain set — cannot load a catalog.",
        }

    catalog = _load_domain_catalog(domain)
    catalog_by_name = {c.get("name"): c for c in catalog if isinstance(c, dict) and c.get("name")}

    with _neo4j(project) as ns:
        # Heal any residual duplicate shapes before PERSIST_SUGGESTED_RULE's
        # keyed node-MERGE runs (only relevant when persisting).
        if body.persist:
            _collapse_dup_shapes(ns, contract_id)
        columns = [dict(r) for r in ns.run(READ_DPROD_COLUMNS, contract_id=contract_id)]
        rules: list[dict] = []
        unmatched: list[str] = []

        for col in columns:
            catalog_entry = catalog_by_name.get(col["name"])
            if not catalog_entry:
                unmatched.append(col["uri"])
                continue
            for i, rule in enumerate(catalog_entry.get("recommended_rules") or []):
                if not isinstance(rule, dict):
                    continue
                rule_type = rule.get("type", "unknown")
                rule_uri = (
                    f"rule:{project.project_code}:dprod:{col['dataset_physical_name']}:"
                    f"{col['name']}:{rule_type}:{i}"
                )
                severity = rule.get("severity", "sh:Warning")
                description = _derive_rule_description(col["name"], rule)
                params = {k: v for k, v in rule.items() if k != "type"}

                # Persist first (when requested) so we can read back the
                # current status — ON CREATE sets pending_review, ON MATCH
                # leaves status alone. Returning the live status lets the
                # wizard hydrate approvedRules / rejectedRules from prior
                # PO decisions on edit-mode re-entry.
                status = "pending_review"
                if body.persist:
                    persist_row = ns.run(
                        PERSIST_SUGGESTED_RULE,
                        dataset_uri=col["dataset_uri"],
                        col_uri=col["uri"],
                        rule_uri=rule_uri,
                        rule_type=rule_type,
                        severity=severity,
                        description=description,
                        params=_to_json_str(params),
                        confidence=0.9,
                    ).single()
                    if persist_row and persist_row.get("status"):
                        status = persist_row["status"]

                rule_payload = {
                    "rule_uri": rule_uri,
                    "dprod_col_uri": col["uri"],
                    "col_name": col["name"],
                    "dataset_physical_name": col["dataset_physical_name"],
                    "rule_type": rule_type,
                    "severity": severity,
                    "description": description,
                    "params": params,
                    "source": f"playbook/domain_catalogs/{domain}.yaml",
                    "status": status,
                }
                rules.append(rule_payload)

    return {
        "contract_id": contract_id,
        "domain": domain,
        "rules": rules,
        "columns_without_match": unmatched,
        "persisted": bool(body.persist),
    }


@project_router.post("/persist-user-rules")
def persist_user_rules(
    project_id: int,
    body: PersistUserRulesInput,
    session: Session = Depends(get_session),
):
    """Persist PO-authored rules as :PropertyShape with ruleSource='user'.

    The wizard calls this when the PO accepts a chat-driven ``rule_create``
    suggestion (or, later, manually composes a rule). Rules are anchored to
    :DProdColumn nodes the same way ``suggest-domain-rules`` anchors domain
    rules, but tagged ``ruleSource='user'`` and stamped with ``createdBy``
    so engineering can see who authored them.

    Status is ``pending_review`` on creation; the wizard's existing
    finalize() loop walks ``approvedRules`` and calls the standard
    ``/reviews/domain_rules`` endpoint to flip them to ``approved`` —
    same lifecycle as catalog-suggested rules.
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"

    persisted: list[dict] = []
    skipped: list[dict] = []
    with _neo4j(project) as ns:
        # Heal any residual duplicate shapes before PERSIST_USER_RULE's keyed
        # node-MERGE runs.
        _collapse_dup_shapes(ns, contract_id)
        cols = [dict(r) for r in ns.run(READ_DPROD_COLUMNS, contract_id=contract_id)]
        cols_by_name = {c["name"]: c for c in cols}

        for i, rule in enumerate(body.rules):
            col_info = cols_by_name.get(rule.column)
            if not col_info:
                skipped.append({"column": rule.column, "reason": "no DProdColumn match"})
                continue
            rule_type = rule.rule_type or "custom"
            rule_uri = (
                f"rule:{project.project_code}:user:{col_info['dataset_physical_name']}:"
                f"{rule.column}:{rule_type}:{i}:{int(time.time())}"
            )
            ns.run(
                PERSIST_USER_RULE,
                dataset_uri=col_info["dataset_uri"],
                col_uri=col_info["uri"],
                rule_uri=rule_uri,
                rule_type=rule_type,
                severity=rule.severity or "sh:Warning",
                description=rule.description or f"User-authored {rule_type} rule on {rule.column}",
                params=_to_json_str(rule.params or {}),
                created_by=body.created_by or "",
            )
            persisted.append({
                "rule_uri": rule_uri,
                "dprod_col_uri": col_info["uri"],
                "col_name": rule.column,
                "dataset_physical_name": col_info["dataset_physical_name"],
                "rule_type": rule_type,
                "severity": rule.severity or "sh:Warning",
                "description": rule.description or "",
                "params": rule.params or {},
                "source": "user",
            })

    return {"contract_id": contract_id, "persisted": persisted, "skipped": skipped}


# The old POST /api/projects/{id}/odcs/ingest-existing-product endpoint
# was retired with the Ingest redesign. Use POST /api/ingest-products/from-odcs
# (in routers/ingest_products.py) — that flow scaffolds a fresh DPE-CF
# project from an uploaded/pasted ODCS spec rather than requiring a
# pre-existing project_id.


@project_router.get("/materialise")
def materialise_odcs(project_id: int, session: Session = Depends(get_session)):
    """Reconstruct ODCS YAML from graph on demand."""
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    spec = _read_odcs_from_graph(contract_id, project)
    if not spec:
        raise HTTPException(404, "No ODCS spec found in graph for this project")
    yaml_str = yaml.dump(spec, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return {"yaml": yaml_str, "spec": spec}


# Walk CONSUMES → source product → source contract → SLA properties. A
# consumer-aligned product is a view over its sources with no independent
# refresh cycle, so its sensible default SLA is inherited from the sources it
# consumes. Filters the source SLA edge on the source's own currentVersion;
# the consumer's CONSUMES edges are filtered to the currently-active set
# (toVersion IS NULL) so an expired binding no longer contributes an SLA.
SUGGEST_SLA_FROM_SOURCES = """\
MATCH (dc:DataContract {id: $contract_id})
WHERE coalesce(dc.isCurrent, true) = true
MATCH (dc)-[r_c:CONSUMES]->(src_dp:DProdDataProduct)<-[:MATERIALISES_AS]-(src_dc:DataContract)
WHERE r_c.toVersion IS NULL
MATCH (src_dc)-[r_sla:HAS_SLA_PROPERTY]->(sla:DataContractSLAProperty)
WHERE r_sla.fromVersion <= coalesce(src_dc.currentVersion, 1)
  AND (r_sla.toVersion IS NULL OR r_sla.toVersion >= coalesce(src_dc.currentVersion, 1))
RETURN coalesce(src_dc.name, src_dp.name, '') AS source_name,
       coalesce(sla.property, '')             AS property,
       coalesce(sla.value, '')                AS value,
       coalesce(sla.unit, '')                 AS unit
"""


def _suggest_sla_from_sources(project: Project, contract_id: str) -> list[dict]:
    """Return SLA properties inherited from CONSUMES'd source products.

    De-duped by ``property`` (first source wins). Each entry carries
    ``inherited_from`` so the wizard can badge where it came from.
    """
    out: list[dict] = []
    seen: set[str] = set()
    with _neo4j(project) as ns:
        for r in ns.run(SUGGEST_SLA_FROM_SOURCES, contract_id=contract_id):
            prop = (r["property"] or "").strip()
            if not prop or prop in seen:
                continue
            seen.add(prop)
            out.append({
                "property": prop,
                "value": r["value"],
                "unit": r["unit"],
                "inherited_from": r["source_name"],
            })
    return out


@project_router.get("/suggest-sla")
def suggest_sla(project_id: int, session: Session = Depends(get_session)):
    """Suggest a default SLA for a consumer product, inherited from sources."""
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    slas = _suggest_sla_from_sources(project, contract_id)
    sources = sorted({s["inherited_from"] for s in slas if s["inherited_from"]})
    return {"slas": slas, "sources": sources}


@project_router.get("/deploy-checklist")
def deploy_checklist(project_id: int, session: Session = Depends(get_session)):
    """Pre-deploy readiness checklist for the PO.

    Reports per-section status for the operational ODCS fields so the PO gets
    a non-blocking reminder (with sensible defaults) before publishing.
    Statuses: ``set`` (authored), ``derivable`` (a default is available), or
    ``empty`` (no value and no default).
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    spec = _read_odcs_from_graph(contract_id, project) or {}

    owners = spec.get("owners") or []
    owner_email = ""
    for o in owners:
        if isinstance(o, dict) and o.get("email"):
            owner_email = o["email"]
            break

    sections: list[dict] = []

    # Servers — auto-derived from the deployment on publish.
    has_servers = bool(spec.get("servers"))
    sections.append({
        "key": "servers",
        "label": "Servers",
        "status": "set" if has_servers else "derivable",
        "detail": "Declared" if has_servers
                  else "Will be auto-populated from where your product is deployed.",
    })

    # SLA — inherited from consumed sources when not authored.
    has_sla = bool(spec.get("slaProperties"))
    inherited_sla = [] if has_sla else _suggest_sla_from_sources(project, contract_id)
    if has_sla:
        sla_status, sla_detail, sla_default = "set", "Declared", None
    elif inherited_sla:
        srcs = sorted({s["inherited_from"] for s in inherited_sla if s["inherited_from"]})
        sla_status = "derivable"
        sla_detail = f"Can inherit {len(inherited_sla)} SLA(s) from {', '.join(srcs) or 'sources'}."
        sla_default = inherited_sla
    else:
        sla_status, sla_detail, sla_default = "empty", "No SLA declared and none inheritable.", None
    sections.append({
        "key": "sla", "label": "SLA", "status": sla_status,
        "detail": sla_detail, "default_payload": sla_default,
    })

    # Team / contacts — default to the owner.
    has_team = bool(spec.get("team"))
    team_default = ([{"name": owner_email.split("@")[0] or "Owner",
                      "role": "owner", "email": owner_email}]
                    if (not has_team and owner_email) else None)
    sections.append({
        "key": "team", "label": "Team / contacts",
        "status": "set" if has_team else ("derivable" if team_default else "empty"),
        "detail": "Declared" if has_team
                  else ("Default to the product owner." if team_default else "No contacts declared."),
        "default_payload": team_default,
    })

    # Roles — optional, no default.
    has_roles = bool(spec.get("roles"))
    sections.append({
        "key": "roles", "label": "Roles",
        "status": "set" if has_roles else "empty",
        "detail": "Declared" if has_roles else "No access roles declared (optional).",
    })

    return {"sections": sections}


@project_router.post("/generate-dprod")
def generate_dprod(project_id: int, session: Session = Depends(get_session)):
    """Generate dprod representation from the ODCS spec in the graph.

    Produces one :DProdOutputDataset per :DataContractSchema, so multi-schema
    contracts fan out into separate datasets (not a single collapsed one).
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"

    try:
        result = _generate_dprod(contract_id, project)
        if not result:
            raise HTTPException(404, "No ODCS contract found to transform")
        result["status"] = "generated"
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"dprod generation failed: {e}")


# Delete the dprod columns for the discarded version's added properties (+ their
# SHACL PropertyShapes). Surgical — leaves the surviving columns and their
# :MAPS_TO_PRODUCT_COLUMN mapping edges untouched (unlike generate-dprod's full
# DPROD_WIPE), so a rollback can't orphan good mappings.
DISCARD_DPROD_RECONCILE = """\
MATCH (col:DProdColumn)
WHERE col.uri STARTS WITH 'dprod:col:' + $contract_id AND col.name IN $removed_names
OPTIONAL MATCH (ps:PropertyShape)-[:ON_DPROD_COLUMN]->(col)
DETACH DELETE ps, col
"""


@project_router.post("/discard-draft")
def discard_draft(project_id: int, session: Session = Depends(get_session)):
    """Discard an unintended draft contract version and restore the prior one.

    Inverse of a version branch. Only permitted when the head version is a
    *draft* branched from a prior version — never destroys published/approved
    history (returns 409 otherwise). Reconciles dprod surgically so existing
    :ColumnMapping edges survive.
    """
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    try:
        with _neo4j(project) as ns:
            outcome = discard_draft_version(ns, contract_id)
            if outcome.removed_property_names:
                ns.run(
                    DISCARD_DPROD_RECONCILE,
                    contract_id=contract_id,
                    removed_names=outcome.removed_property_names,
                )
    except DiscardNotAllowed as e:
        raise HTTPException(409, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"discard-draft failed: {e}")
    return {
        "ok": True,
        "status": "discarded",
        "discarded_version": outcome.discarded_version,
        "restored_version": outcome.restored_version,
        "restored_lifecycle": outcome.restored_lifecycle,
        "columns_removed": outcome.removed_property_names,
        "columns_remaining": outcome.active_property_names,
        **({"warning": outcome.scalar_warning} if outcome.scalar_warning else {}),
    }


PUBLISH_QUERY = """\
MATCH (dp:DProdDataProduct)
WHERE dp.uri STARTS WITH 'dprod:' + $project_code
SET dp.status = 'published',
    dp.publishedAt = datetime(),
    dp.publishedBy = $user
RETURN dp.uri AS uri, dp.name AS name
"""

# Side-effect of publishing: stamp lifecycleState on the current contract
# and supersede any previously-published :ContractVersion sidecars of the
# same contract_id. Marketplace queries filter by lifecycleState and pin
# to either the latest published/superseded version or to a specific
# version (via ?version=N).
PUBLISH_CONTRACT_LIFECYCLE = """\
// Anchor on :DataContract first so the query always returns at least one
// row even on a first deploy (no prior published cv exists). Previously
// this anchored on cv_old and short-circuited when no prior published
// version was found, leaving the current cv stuck at 'draft' even though
// dc.currentLifecycleState got flipped to 'published'.
MATCH (dc:DataContract {id: $contract_id})
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_old:ContractVersion)
WHERE cv_old.version <> dc.currentVersion
  AND coalesce(cv_old.lifecycleState, '') = 'published'
SET cv_old.lifecycleState = 'superseded'
WITH dc
MATCH (dc)-[:HAS_VERSION]->(cv:ContractVersion {version: dc.currentVersion})
SET cv.lifecycleState = 'published',
    cv.publishedAt = datetime(),
    dc.currentLifecycleState = 'published'
"""


@project_router.post("/publish")
def publish_data_product(
    project_id: int,
    session: Session = Depends(get_session),
    user: str = "Data Product Owner",
    _role=Depends(require_role("owner")),
):
    """Mark this project's data product as published."""
    project = _get_project(project_id, session)
    contract_id = f"{project.project_code}-contract"
    try:
        with _neo4j(project) as ns:
            # Reflect the real deployment target into an ODCS server entry so
            # the contract is self-describing on publish. Best-effort — a
            # derivation failure (e.g. not yet deployed) must never block the
            # publish itself.
            try:
                from ..server_reflect import reflect_server_from_deployment
                reflect_server_from_deployment(ns, session, project, contract_id)
            except Exception:
                pass
            results = [dict(r) for r in ns.run(PUBLISH_QUERY,
                                                project_code=project.project_code,
                                                user=user)]
            ns.run(PUBLISH_CONTRACT_LIFECYCLE, contract_id=contract_id)
        if not results:
            raise HTTPException(404, "No data product found for this project")
        return {"published": results, "count": len(results)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Publish failed: {e}")
