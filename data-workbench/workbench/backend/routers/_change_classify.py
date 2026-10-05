"""Change classifier for the PO-controlled inline-vs-new-version decision.

Compares two ODCS spec dicts ("old" = currently-deployed view, "new" =
incoming save) and returns a structured diff plus a recommended
``change_kind``:

- ``cosmetic`` — no consumer impact. Description tweaked on existing text,
  product/column purpose, owner email, rule rationale, etc. Save path can
  patch the current :ContractVersion in place (no version bump) and emit
  a :ProvActivity {activityType: 'ContractPatch'} attached via :HAS_PATCH.

- ``schema`` — additive / non-breaking. Column added, rule added or
  loosened, description filled in where previously empty, PII flag toggled
  but no consumer 1:1 mapping yet exists for the column. Save path
  branches a new :ContractVersion; consumers see a drift banner but can
  stay on v(n).

- ``breaking`` — consumers' existing mappings or serving DDL would break.
  Column removed, renamed, type narrowed, rule tightened in a way that
  would fail existing data, shape filter narrowed, PII flag added where a
  consumer has a 1:1 (unmasked) mapping. Save path still branches; the
  PO sees the per-consumer impact preview before publishing.

The classifier is invoked from two places:
- `_save_odcs_to_graph` in routers/odcs.py — when ``change_kind == 'auto'``,
  decide which save mode to use.
- `GET /edit-impact` in routers/edits.py — the wizard's ImpactPreviewPanel
  uses the kind + the per-consumer impact to render the recommendation.

Same rules everywhere so the PO sees the same recommendation in both
surfaces.
"""

from __future__ import annotations

from typing import Any, Literal

ChangeKind = Literal["cosmetic", "schema", "breaking"]


def _norm(v: Any) -> str:
    return (v or "").strip() if isinstance(v, str) else ""


def _flatten_properties(spec: dict) -> list[dict]:
    """Walk schema[].properties[] into a flat list with the schema name
    embedded as `_schema` so diffs can collate by (schema, column) pairs."""
    out: list[dict] = []
    for s in spec.get("schema") or []:
        if not isinstance(s, dict):
            continue
        schema_name = s.get("physicalName") or s.get("name", "")
        for p in s.get("properties") or []:
            if not isinstance(p, dict):
                continue
            entry = dict(p)
            entry["_schema"] = schema_name
            entry.setdefault("name", entry.get("physicalName", ""))
            out.append(entry)
    return out


def _column_key(p: dict) -> str:
    """Stable identity for a column across versions: schema + physicalName."""
    schema = p.get("_schema", "")
    phys = p.get("physicalName") or p.get("name", "")
    return f"{schema}.{phys}"


def diff_columns(old_spec: dict, new_spec: dict) -> dict:
    """Compute the column-level diff: added / removed / type_changed /
    description_changed / pii_added / pii_removed.

    Returns a dict with each bucket as a list of structured entries:
      added:                [{schema, name, physical_type, pii, ...}]
      removed:              [{schema, name}]
      type_changed:         [{schema, name, from, to}]
      description_changed:  [{schema, name, from, to, from_empty}]
      pii_added:            [{schema, name}]
      pii_removed:          [{schema, name}]
    """
    old_props = {_column_key(p): p for p in _flatten_properties(old_spec)}
    new_props = {_column_key(p): p for p in _flatten_properties(new_spec)}

    added: list[dict] = []
    removed: list[dict] = []
    type_changed: list[dict] = []
    description_changed: list[dict] = []
    pii_added: list[dict] = []
    pii_removed: list[dict] = []

    for k, p in new_props.items():
        if k in old_props:
            continue
        added.append({
            "schema": p.get("_schema", ""),
            "name": p.get("name", ""),
            "physical_type": p.get("physicalType", "") or p.get("logicalType", ""),
            "pii": bool(p.get("pii", False)),
        })

    for k, p in old_props.items():
        if k in new_props:
            continue
        removed.append({
            "schema": p.get("_schema", ""),
            "name": p.get("name", ""),
        })

    for k, new_p in new_props.items():
        old_p = old_props.get(k)
        if not old_p:
            continue
        old_type = _norm(old_p.get("physicalType")) or _norm(old_p.get("logicalType"))
        new_type = _norm(new_p.get("physicalType")) or _norm(new_p.get("logicalType"))
        if old_type and new_type and old_type != new_type:
            type_changed.append({
                "schema": new_p.get("_schema", ""),
                "name": new_p.get("name", ""),
                "from": old_type,
                "to": new_type,
            })
        old_desc = _norm(old_p.get("description"))
        new_desc = _norm(new_p.get("description"))
        if old_desc != new_desc:
            description_changed.append({
                "schema": new_p.get("_schema", ""),
                "name": new_p.get("name", ""),
                "from": old_desc,
                "to": new_desc,
                "from_empty": not old_desc,
            })

        # PII is signaled by either the legacy pii bool OR the sensitivity
        # enum (pii / phi). Unify so the classifier picks up either path.
        def _is_pii(p: dict) -> bool:
            if p.get("pii"):
                return True
            sens = (p.get("sensitivity") or "").strip().lower()
            return sens in ("pii", "phi")
        old_pii = _is_pii(old_p)
        new_pii = _is_pii(new_p)
        if old_pii != new_pii:
            entry = {"schema": new_p.get("_schema", ""), "name": new_p.get("name", "")}
            if new_pii:
                pii_added.append(entry)
            else:
                pii_removed.append(entry)

    return {
        "added": sorted(added, key=lambda x: (x["schema"], x["name"])),
        "removed": sorted(removed, key=lambda x: (x["schema"], x["name"])),
        "type_changed": sorted(type_changed, key=lambda x: (x["schema"], x["name"])),
        "description_changed": sorted(description_changed, key=lambda x: (x["schema"], x["name"])),
        "pii_added": sorted(pii_added, key=lambda x: (x["schema"], x["name"])),
        "pii_removed": sorted(pii_removed, key=lambda x: (x["schema"], x["name"])),
    }


def diff_metadata(old_spec: dict, new_spec: dict) -> dict:
    """Top-level metadata fields: name, description, purpose, limitations."""
    pairs = [
        ("name", "Product name"),
        ("description", "Description"),
        ("purpose", "Purpose"),
        ("limitations", "Limitations"),
    ]
    changed: list[dict] = []
    for key, label in pairs:
        before = _norm(old_spec.get(key))
        after = _norm(new_spec.get(key))
        if before != after:
            changed.append({
                "field": label,
                "key": key,
                "from": before,
                "to": after,
                "from_empty": not before,
            })
    return {"changed_fields": changed}


def diff_inputs(old_spec: dict, new_spec: dict) -> dict:
    """Diff :CONSUMES edges (consumer-aligned only)."""
    by_uri_old = {i["dprod_uri"]: i for i in (old_spec.get("inputs") or []) if isinstance(i, dict) and i.get("dprod_uri")}
    by_uri_new = {i["dprod_uri"]: i for i in (new_spec.get("inputs") or []) if isinstance(i, dict) and i.get("dprod_uri")}
    added = [
        {"dprod_uri": uri, "name": by_uri_new[uri].get("name", "")}
        for uri in sorted(by_uri_new) if uri not in by_uri_old
    ]
    removed = [
        {"dprod_uri": uri, "name": by_uri_old[uri].get("name", "")}
        for uri in sorted(by_uri_old) if uri not in by_uri_new
    ]
    return {"added": added, "removed": removed}


def diff_quality_rules(old_spec: dict, new_spec: dict) -> dict:
    """Quality rules added / removed / changed at the spec level.

    Rules are matched by (column, dataset, rule_type, name) since ODCS has
    no first-class rule identifier. Description / severity / dimension are
    treated as the rule body — changes here are cosmetic except for severity
    which can be schema (tighter severity may fail tests).
    """
    def key(q: dict) -> str:
        return "::".join((
            (q.get("column") or ""),
            (q.get("dataset") or ""),
            (q.get("rule") or ""),
            (q.get("name") or ""),
        ))

    old_q = {key(q): q for q in (old_spec.get("quality") or []) if isinstance(q, dict)}
    new_q = {key(q): q for q in (new_spec.get("quality") or []) if isinstance(q, dict)}

    added = [new_q[k] for k in sorted(new_q) if k not in old_q]
    removed = [old_q[k] for k in sorted(old_q) if k not in new_q]
    severity_changed: list[dict] = []
    for k, new_v in new_q.items():
        old_v = old_q.get(k)
        if not old_v:
            continue
        if _norm(old_v.get("severity")) != _norm(new_v.get("severity")):
            severity_changed.append({
                "name": new_v.get("name", ""),
                "column": new_v.get("column", ""),
                "from": _norm(old_v.get("severity")),
                "to": _norm(new_v.get("severity")),
            })
    return {"added": added, "removed": removed, "severity_changed": severity_changed}


# ── Classification rules ───────────────────────────────────────────────────


# Severities ranked from least-to-most strict so we can detect tightening.
_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def _is_severity_tightening(s: dict) -> bool:
    """severity_changed entries become 'breaking' when severity went from
    a lower rank to a higher one (warning → error breaks tests that pass
    today; error → warning doesn't break anything)."""
    return _SEVERITY_RANK.get(s.get("to", ""), 0) > _SEVERITY_RANK.get(s.get("from", ""), 0)


def classify_diff(old_spec: dict | None, new_spec: dict) -> dict:
    """Top-level classifier. Returns:

    {
      kind: 'cosmetic' | 'schema' | 'breaking',
      diff: {
        columns: {...},
        metadata: {...},
        inputs: {...},
        quality: {...},
      },
      reasons: [str, ...]   # human-readable bullets explaining the classification
    }

    A None ``old_spec`` (initial save, no prior state) classifies as 'schema'
    by default — there's nothing to be "breaking" against. Callers can
    override by passing change_kind explicitly.
    """
    if old_spec is None:
        return {
            "kind": "schema",
            "diff": {
                "columns": {"added": [], "removed": [], "type_changed": [],
                            "description_changed": [], "pii_added": [], "pii_removed": []},
                "metadata": {"changed_fields": []},
                "inputs": {"added": [], "removed": []},
                "quality": {"added": [], "removed": [], "severity_changed": []},
            },
            "reasons": ["initial save"],
        }

    cols = diff_columns(old_spec, new_spec)
    meta = diff_metadata(old_spec, new_spec)
    inputs = diff_inputs(old_spec, new_spec)
    quality = diff_quality_rules(old_spec, new_spec)

    diff = {"columns": cols, "metadata": meta, "inputs": inputs, "quality": quality}
    reasons: list[str] = []

    # ── Breaking signals ──────────────────────────────────────────────────
    if cols["removed"]:
        reasons.append(f"{len(cols['removed'])} column(s) removed")
    if cols["type_changed"]:
        # All type changes are flagged breaking — narrowing detection is
        # too heuristic to ship reliably (varchar(64) → varchar(32) is a
        # narrowing, but int → bigint is widening). The PO can override.
        reasons.append(f"{len(cols['type_changed'])} column type change(s)")
    if cols["pii_added"]:
        # A new PII flag becomes "breaking" only when a consumer mapping
        # exists for the column without a masking transform. That requires
        # graph context that the classifier doesn't have. Surface it as
        # 'schema' here; the impact-preview endpoint upgrades to 'breaking'
        # when consumer mappings are inspected.
        pass
    if inputs["removed"]:
        reasons.append(f"{len(inputs['removed'])} consumed source product(s) removed")
    severity_tightenings = [s for s in quality["severity_changed"] if _is_severity_tightening(s)]
    if severity_tightenings:
        reasons.append(f"{len(severity_tightenings)} quality rule(s) tightened (warning→error)")

    breaking = bool(
        cols["removed"]
        or cols["type_changed"]
        or inputs["removed"]
        or severity_tightenings
    )
    if breaking:
        return {"kind": "breaking", "diff": diff, "reasons": reasons}

    # ── Schema signals (non-breaking, but contract-shape change) ──────────
    schema_signals: list[str] = []
    if cols["added"]:
        schema_signals.append(f"{len(cols['added'])} column(s) added")
    if cols["pii_added"]:
        schema_signals.append(f"{len(cols['pii_added'])} column(s) flagged PII")
    if cols["pii_removed"]:
        schema_signals.append(f"{len(cols['pii_removed'])} column(s) un-flagged PII")
    if inputs["added"]:
        schema_signals.append(f"{len(inputs['added'])} consumed source product(s) added")
    if quality["added"]:
        schema_signals.append(f"{len(quality['added'])} quality rule(s) added")
    if quality["removed"]:
        schema_signals.append(f"{len(quality['removed'])} quality rule(s) removed")
    # Description previously-empty → schema (consumers might now act on the
    # new docs). Description tweak on existing text → cosmetic.
    schema_description_fills = [d for d in cols["description_changed"] if d.get("from_empty")]
    if schema_description_fills:
        schema_signals.append(f"{len(schema_description_fills)} column description(s) added")

    if schema_signals:
        return {"kind": "schema", "diff": diff, "reasons": schema_signals}

    # ── Otherwise: cosmetic ───────────────────────────────────────────────
    cosmetic_signals: list[str] = []
    cosmetic_descriptions = [d for d in cols["description_changed"] if not d.get("from_empty")]
    if cosmetic_descriptions:
        cosmetic_signals.append(f"{len(cosmetic_descriptions)} column description(s) revised")
    if meta["changed_fields"]:
        cosmetic_signals.append(f"{len(meta['changed_fields'])} metadata field(s) revised")
    # Severity loosening (error → warning) is cosmetic in our model — it
    # never causes tests to fail, just downgrades existing alarms.
    severity_loosenings = [s for s in quality["severity_changed"] if not _is_severity_tightening(s)]
    if severity_loosenings:
        cosmetic_signals.append(f"{len(severity_loosenings)} quality rule severity loosening(s)")

    if not cosmetic_signals:
        # No detectable diff. Treat as cosmetic (no-op patch) so we don't
        # accidentally version-branch on a save with no real changes.
        cosmetic_signals.append("no detected changes")
    return {"kind": "cosmetic", "diff": diff, "reasons": cosmetic_signals}
