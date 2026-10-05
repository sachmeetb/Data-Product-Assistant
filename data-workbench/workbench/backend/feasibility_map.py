"""Bidirectional field map between an ODCS **template** spec and a
:class:`feasibility_spec.FeasibilitySpec` (the "C" corpus schema).

The Blueprint Library stores every data-product template as an ODCS v3.1 spec
(so it reuses the ODCS read/write/versioning machinery verbatim). But the
Connected-Estate feasibility scanner grades against ``FeasibilitySpec`` YAML.
This module is the deterministic, reviewable bridge between the two shapes:

- :func:`feasibility_spec_to_odcs` — the **offline** direction, run once by
  ``scripts/convert_corpus_to_odcs_seed.py`` to mint committed ODCS seed files
  from the vendored feasibility corpus.
- :func:`odcs_to_feasibility_spec` — the **runtime** direction, run at bootstrap
  and on every template Publish by
  ``scripts/generate_feasibility_corpus_from_library.py`` to regenerate the
  baked corpus the (untouched) scanner reads.

~80% of ``FeasibilitySpec`` maps onto extended ODCS fields directly (name /
domain / description / productKind / per-attribute name/type/concept/
required/is_key/classification/description). The remainder that has no clean
ODCS home — spec-level classification, freshness cadence/history, grain keys,
composition, allowed derivations, per-attribute derivations + notes — is stored
under a single reserved ``customProperties.feasibility`` namespace so the round
trip is deterministic and reviewable in the YAML. The runtime direction reads
that namespace first and **falls back to re-deriving exactly as
``scripts/seed_feasibility_corpus.py`` does** (PK→grain keys,
classification→sensitivity) for hand-imported / authored templates that lack it.

A ``FeasibilitySpec → ODCS → FeasibilitySpec`` round trip is equal modulo the
documented lossy fields; ``tests/test_templates.py`` exercises both directions.
"""
from __future__ import annotations

from typing import Any

from . import feasibility_spec as fs

# The single reserved customProperties namespace carrying every feasibility-only
# field that has no first-class ODCS home. Kept as ONE key so the YAML stays
# reviewable and the round trip is a single lift.
CP_NAMESPACE = "feasibility"

# ── lossy enum bridges (documented) ──────────────────────────────────────────
# FeasibilitySpec.History ⇄ :DatasetTransform scd_policy.type. `current` has no
# scd_policy peer; we map it to `latest_only` (and back) explicitly.
_HISTORY_TO_SCD = {"current": "latest_only", "snapshot": "snapshot", "scd2": "scd2"}
_SCD_TO_HISTORY = {v: k for k, v in _HISTORY_TO_SCD.items()}

# FeasibilitySpec.Classification → ODCS property `sensitivity` (best-effort).
# `public`/`restricted` have no ODCS sensitivity peer, so we still keep the
# verbatim Classification string on the property's `classification` field (which
# IS a valid Classification value) — that string is what the reverse reads, so
# the round trip stays exact even though `sensitivity` is only a hint.
_CLASS_TO_SENSITIVITY = {
    "public": "none",
    "internal": "internal",
    "confidential": "confidential",
    "pii": "pii",
    "restricted": "confidential",
}


def template_id(domain: str, spec_id: str) -> str:
    """The project-independent template contract id: ``template:{domain}:{spec}``.

    Both segments are slugged for a stable, filesystem-safe id. The **original**
    (un-slugged) ``spec_id`` is preserved separately on the ``sourceSpecId`` head
    prop so the reverse direction recovers it exactly.
    """
    return f"template:{fs.slug(domain, 'misc')}:{fs.slug(spec_id, 'spec')}"


def _schema_physical_name(name: str) -> str:
    """A stable snake_case physicalName for the single template schema."""
    return fs.slug(name, "dataset").replace("-", "_") or "dataset"


# ── FeasibilitySpec → ODCS (offline seed direction) ──────────────────────────

def feasibility_spec_to_odcs(spec: fs.FeasibilitySpec | dict[str, Any]) -> dict[str, Any]:
    """Convert a validated :class:`FeasibilitySpec` into an ODCS v3.1 template
    dict. Deterministic — the reverse recovers the spec modulo documented lossy
    fields. Accepts either a parsed model or a raw dict (validated fail-closed).
    """
    s = spec if isinstance(spec, fs.FeasibilitySpec) else fs.parse_spec(spec)

    attr_derivations: dict[str, list[str]] = {}
    attr_notes: dict[str, str] = {}
    properties: list[dict[str, Any]] = []
    for a in s.attributes:
        prop: dict[str, Any] = {
            "name": a.name,
            "physicalName": a.name,
            "logicalName": a.concept or "",
            "logicalType": a.type or "",
            "physicalType": a.type or "",
            "description": a.description or "",
            "required": bool(a.required),
            "primaryKey": bool(a.is_key),
            # Verbatim Classification string — the load-bearing round-trip field.
            "classification": a.classification.value,
        }
        sens = _CLASS_TO_SENSITIVITY.get(a.classification.value)
        if sens and sens != "none":
            prop["sensitivity"] = sens
        properties.append(prop)
        if a.derivations:
            attr_derivations[a.name] = [d.value for d in a.derivations]
        if a.note:
            attr_notes[a.name] = a.note

    # Design-surface transform block (grain + SCD) — redundant with the CP bag
    # below but what the wizard's Shape step renders.
    transform: dict[str, Any] = {}
    if s.grain.keys:
        transform["grouping_keys"] = list(s.grain.keys)
    if s.grain.description:
        transform["grain_prose"] = s.grain.description
    scd_type = _HISTORY_TO_SCD.get(s.freshness.history.value)
    if scd_type:
        transform["scd_policy"] = {"type": scd_type}

    schema_entry: dict[str, Any] = {
        "name": s.name,
        "physicalName": _schema_physical_name(s.name),
        "physicalType": "table",
        "description": s.description or "",
        "properties": properties,
    }
    if transform:
        schema_entry["transform"] = transform

    # The authoritative round-trip bag for everything with no first-class ODCS
    # home. Read first by the reverse direction.
    feas_cp: dict[str, Any] = {
        "schema_version": s.schema_version,
        "spec_classification": s.classification.value,
        "cadence": s.freshness.cadence,
        "freshness_history": s.freshness.history.value,
        "freshness_description": s.freshness.description,
        "grain_keys": list(s.grain.keys),
        "grain_description": s.grain.description,
        "allowed_derivations": [d.value for d in s.allowed_derivations],
        "composition": {
            "join_keys": list(s.composition.join_keys),
            "composed_of": list(s.composition.composed_of),
            "description": s.composition.description,
        },
    }
    if attr_derivations:
        feas_cp["attr_derivations"] = attr_derivations
    if attr_notes:
        feas_cp["attr_notes"] = attr_notes

    return {
        "apiVersion": "v3.1.0",
        "kind": "DataContract",
        "id": template_id(s.domain, s.spec_id),
        "name": s.name,
        "version": "1.0.0",
        "status": "draft",
        "domain": s.domain,
        "description": s.description or "",
        "productKind": s.product_kind.value,
        "sourceSpecId": s.spec_id,
        "schema": [schema_entry],
        "customProperties": {CP_NAMESPACE: feas_cp},
    }


# ── ODCS → FeasibilitySpec (runtime corpus-generation direction) ─────────────

# Mirror of scripts/seed_feasibility_corpus.py heuristics — used ONLY to
# re-derive fields for templates that lack the customProperties.feasibility bag
# (hand-imported / authored). Kept tiny + local so the fallback is honest.
import re as _re  # noqa: E402

_KEY_RE = _re.compile(r"(^id$|_id$)")
_SENSITIVITY_TO_CLASS = {
    "none": "public",
    "internal": "internal",
    "confidential": "confidential",
    "pii": "pii",
    "phi": "pii",
}
_VALID_CLASS = {c.value for c in fs.Classification}
_VALID_DERIVATIONS = {d.value for d in fs.DerivationKind}


def _first_schema(odcs: dict[str, Any]) -> dict[str, Any]:
    schemas = odcs.get("schema") or []
    for s in schemas:
        if isinstance(s, dict):
            return s
    return {}


def _prop_classification(prop: dict[str, Any]) -> str:
    """Resolve an attribute classification: verbatim `classification` when it's a
    valid enum value, else derived from `sensitivity`, else `internal`."""
    verbatim = (prop.get("classification") or "").strip().lower()
    if verbatim in _VALID_CLASS:
        return verbatim
    sens = (prop.get("sensitivity") or "").strip().lower()
    if sens in _SENSITIVITY_TO_CLASS:
        return _SENSITIVITY_TO_CLASS[sens]
    return "internal"


def odcs_to_feasibility_spec(odcs: dict[str, Any]) -> dict[str, Any]:
    """Project an ODCS template dict into a raw ``FeasibilitySpec`` dict.

    Reads the reserved ``customProperties.feasibility`` bag first (present on
    every seed / Library-published template → exact round trip), and re-derives
    each missing field with the ``seed_feasibility_corpus.py`` heuristics for
    hand-imported / authored templates that lack it. The result is a **raw dict**
    — the caller validates it fail-closed via ``feasibility_spec.parse_spec``.
    """
    cp = odcs.get("customProperties") or {}
    feas = cp.get(CP_NAMESPACE) or {}
    schema = _first_schema(odcs)
    props = schema.get("properties") or schema.get("fields") or schema.get("columns") or []
    transform = schema.get("transform") or {}

    # spec_id: prefer the preserved sourceSpecId, else slug the name.
    spec_id = (odcs.get("sourceSpecId") or "").strip()
    if not spec_id:
        spec_id = fs.slug(odcs.get("name") or odcs.get("id") or "spec", "spec").replace("-", "_")

    # per-attr extras from the CP bag.
    attr_derivations = feas.get("attr_derivations") or {}
    attr_notes = feas.get("attr_notes") or {}

    attributes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in props:
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        dtype = (p.get("physicalType") or p.get("logicalType") or p.get("dataType") or "").strip()
        is_key = bool(p.get("primaryKey") or p.get("isPrimaryKey"))
        derivations = [
            d for d in (attr_derivations.get(name) or [])
            if d in _VALID_DERIVATIONS
        ]
        attr: dict[str, Any] = {
            "name": name,
            "type": dtype,
            "concept": p.get("logicalName") or "",
            "required": bool(p.get("required")),
            "is_key": is_key,
            "classification": _prop_classification(p),
            "description": p.get("description") or "",
        }
        if derivations:
            attr["derivations"] = derivations
        note = attr_notes.get(name)
        if note:
            attr["note"] = note
        attributes.append(attr)

    # grain keys: CP bag first, else transform grouping_keys, else derived from PKs.
    grain_keys = list(feas.get("grain_keys") or [])
    if not grain_keys:
        grain_keys = [k for k in (transform.get("grouping_keys") or []) if isinstance(k, str)]
    if not grain_keys:
        pk_names = [a["name"] for a in attributes if a["is_key"]]
        grain_keys = pk_names[:1]

    # freshness.history: CP bag first, else reverse-map the scd_policy, else derive.
    history = (feas.get("freshness_history") or "").strip().lower()
    if history not in _HISTORY_TO_SCD:
        scd = transform.get("scd_policy") or {}
        scd_type = (scd.get("type") or "").strip().lower() if isinstance(scd, dict) else ""
        history = _SCD_TO_HISTORY.get(scd_type, "current")

    # spec-level classification: CP bag first, else derive from attr PII.
    spec_class = (feas.get("spec_classification") or "").strip().lower()
    if spec_class not in _VALID_CLASS:
        spec_class = "confidential" if any(
            a["classification"] in ("pii", "restricted") for a in attributes
        ) else "internal"

    allowed = [
        d for d in (feas.get("allowed_derivations") or ["rename", "cast"])
        if d in _VALID_DERIVATIONS
    ]
    composition = feas.get("composition") or {}

    out: dict[str, Any] = {
        "spec_id": spec_id,
        "name": (odcs.get("name") or "").strip(),
        "domain": (odcs.get("domain") or "").strip(),
        "product_kind": (odcs.get("productKind") or "consumer").strip().lower() or "consumer",
        "description": odcs.get("description") or "",
        "schema_version": feas.get("schema_version") or fs.SPEC_SCHEMA_VERSION,
        "classification": spec_class,
        "grain": {
            "keys": grain_keys,
            "description": feas.get("grain_description")
            or transform.get("grain_prose") or "",
        },
        "freshness": {
            "history": history,
            "cadence": feas.get("cadence") or "",
            "description": feas.get("freshness_description") or "",
        },
        "allowed_derivations": allowed,
        "attributes": attributes,
    }
    if composition and (composition.get("composed_of") or composition.get("join_keys")
                        or composition.get("description")):
        out["composition"] = {
            "join_keys": list(composition.get("join_keys") or []),
            "composed_of": list(composition.get("composed_of") or []),
            "description": composition.get("description") or "",
        }
    return out
