"""Transform-placement planner (Axis 2) — pure classification.

When a serving pattern crosses a boundary (``lakehouse_file`` /
``transfer_then_transform``), *where* each compiled transform op runs relative
to the move is a **planner split of one IR**, not two pipelines. This module is
the pure decision core: given a placement choice and the product's normalized
transform ops, it assigns each op to the **extract side** (run on the source
before the move) or the **target side** (run after load), and reports the
effective placement + governance-forced pushes.

No SQL, no I/O — the executor (``transfer_execution.py``) realizes the decision.
Fully unit-testable.

Placements (see ``docs/architecture/cross-platform-transfer.md`` §3):
  - ``transform_on_extract`` (ETL) — everything on the source; only shaped data moves.
  - ``transfer_then_transform`` (ELT) — move raw; everything on the target
    (except governance masking, which is always forced pre-boundary).
  - ``hybrid`` (EtLT, default) — push cheap volume-reducers + required masking to
    the source; defer heavy relational ops to the target.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Volume-reducing or governance-critical ops → cheap/safe to push to the source.
# ``projection`` / ``direct`` (select only needed columns, a straight passthrough)
# and ``partition_prune`` are implicit in any compiled SELECT; ``filter`` is the
# dataset row-filter; ``mask`` is governance (must run before data leaves the boundary).
_PUSHDOWN_KINDS = frozenset({"projection", "direct", "filter", "partition_prune", "mask"})

# Heavy relational ops → defer to the (often more elastic) target compute.
# Covers the dataset-level shape ops AND the full column-transform DSL
# (``VALID_TRANSFORM_KINDS`` in data-mapping-neo4j/scripts/write_mappings.py) so no
# authored transform falls through to the "unclassified → target + warning" branch.
_DEFER_KINDS = frozenset({
    "join", "aggregate", "grouping", "scd2", "dedup", "dedup_window", "window",
    # business-rule column transforms
    "case", "lookup", "arithmetic", "concat", "bucket", "hash", "cast", "literal",
    # scalar reshapers — cheap, runnable either side; default to target (not volume-reducers)
    "format", "split", "substring", "expression",
})

# Masking is ALWAYS forced to the extract side regardless of placement — a
# governance invariant (sensitive values must never leave the source boundary).
_GOVERNANCE_KINDS = frozenset({"mask"})

_VALID_PLACEMENTS = ("transform_on_extract", "hybrid", "transfer_then_transform")


@dataclass
class OpAssignment:
    kind: str
    side: str                      # "extract" | "target"
    reason: str
    column: str | None = None
    dataset: str | None = None
    forced: bool = False           # governance-forced regardless of placement


@dataclass
class PlacementDecision:
    requested_placement: str
    effective_placement: str
    extract_ops: list[OpAssignment] = field(default_factory=list)
    target_ops: list[OpAssignment] = field(default_factory=list)
    forced_pre_boundary: list[OpAssignment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def classify_op(kind: str, placement: str) -> tuple[str, str, bool]:
    """Return ``(side, reason, forced)`` for one op under ``placement``.

    ``forced`` marks a governance push (mask) that overrides the placement.
    """
    k = (kind or "").lower()
    if k in _GOVERNANCE_KINDS:
        return "extract", "governance: masking must run before data leaves the source", True
    if placement == "transform_on_extract":
        return "extract", "transform_on_extract: all shaping on the source", False
    if placement == "transfer_then_transform":
        return "target", "transfer_then_transform: raw move, shape on the target", False
    # hybrid
    if k in _PUSHDOWN_KINDS:
        return "extract", "hybrid: volume-reducer pushed to the source", False
    if k in _DEFER_KINDS:
        return "target", "hybrid: heavy relational op deferred to the target", False
    # Unknown op — default to the target side (safe: correctness over efficiency),
    # with a warning so new transform kinds get classified deliberately.
    return "target", f"hybrid: unclassified op '{k}' defaulted to target", False


def plan_placement(placement: str, ops: list[dict]) -> PlacementDecision:
    """Split ``ops`` into extract-side vs target-side under ``placement``.

    ``ops`` items: ``{kind, column?, dataset?}``. Returns a PlacementDecision.
    Invalid placements fall back to ``hybrid`` with a warning (fail-safe, not
    fail-closed — a bad UI value must never block serving).
    """
    requested = placement
    warnings: list[str] = []
    if placement not in _VALID_PLACEMENTS:
        warnings.append(f"unknown placement '{placement}' → defaulted to hybrid")
        placement = "hybrid"

    extract_ops: list[OpAssignment] = []
    target_ops: list[OpAssignment] = []
    forced: list[OpAssignment] = []

    for op in ops or []:
        kind = op.get("kind", "")
        side, reason, is_forced = classify_op(kind, placement)
        assignment = OpAssignment(
            kind=kind, side=side, reason=reason,
            column=op.get("column"), dataset=op.get("dataset"), forced=is_forced,
        )
        if is_forced:
            forced.append(assignment)
        if side == "extract":
            extract_ops.append(assignment)
        else:
            target_ops.append(assignment)
        if "unclassified op" in reason:
            warnings.append(reason)

    return PlacementDecision(
        requested_placement=requested,
        effective_placement=placement,
        extract_ops=extract_ops,
        target_ops=target_ops,
        forced_pre_boundary=forced,
        warnings=warnings,
    )


import re as _re

_DEFAULT_LANDING_SCHEMA = "wb_landing"


def landing_name(schema: str, table: str) -> str:
    """Deterministic safe landing-table name for a source ``schema.table``."""
    return _re.sub(r"[^a-zA-Z0-9_]", "_", f"{schema}_{table}").lower()


def rewrite_base_relations(
    body: str, tables: list[str], landing_schema: str = _DEFAULT_LANDING_SCHEMA,
) -> tuple[str, list[dict]]:
    """Rewrite a compiled target-dialect ``body``'s base relations to landed ones.

    ``tables`` are ``"schema.table"`` strings (from the compiler summary). Each
    occurrence of the quoted base relation ``"schema"."table"`` is replaced with
    ``"<landing_schema>"."<safe>"``. Returns ``(rewritten_body, landing_map)``
    where ``landing_map`` items are
    ``{source_schema, source_table, landing_relation, landing_table}``.

    Pure — the compiler always emits double-quoted ``"schema"."table"`` for base
    relations (dialect ``_quote``), so a targeted replace of that exact token is
    reliable. Relations not present in ``body`` are still reported (they may be
    bridge-only) so the executor lands them.
    """
    landing_map: list[dict] = []
    out = body
    for rel in tables or []:
        if "." not in rel:
            continue
        schema, table = rel.split(".", 1)
        safe = landing_name(schema, table)
        landing_rel = f'"{landing_schema}"."{safe}"'
        source_token = f'"{schema}"."{table}"'
        out = out.replace(source_token, landing_rel)
        landing_map.append({
            "source_schema": schema, "source_table": table,
            "landing_relation": landing_rel, "landing_table": safe,
        })
    return out, landing_map


def plan_extract_projections(body: str, tables: list[str]) -> dict[str, list[str]]:
    """Return ``{relation: [columns]}`` — the columns of each base ``schema.table``
    the compiled ``body`` references via a qualifier (alias or table name).

    Used to NARROW the ELT extract from ``SELECT *`` to only the needed columns
    (Phase 2.3). **Safe by construction**: this is best-effort — a relation whose
    columns can't be confidently attributed is simply omitted (the caller lands it
    full), and over-narrowing (missing a needed column) makes the target CREATE
    fail, which the executor catches and falls back to transform_on_extract. So a
    wrong analysis can only cost efficiency, never correctness.

    Unqualified column refs (CTE-level in the layered body) are ignored — only
    columns qualified by an alias/name that maps to a *base* relation count.
    """
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(body, read="duckdb")
    except Exception:  # noqa: BLE001 — parse failure → no narrowing (land full)
        return {}
    relset = set(tables or [])
    alias_to_rel: dict[str, str] = {}
    for tbl in tree.find_all(exp.Table):
        rel = ".".join(p.name for p in [tbl.args.get("db"), tbl.this] if p is not None)
        if rel in relset:
            if tbl.alias:
                alias_to_rel[tbl.alias] = rel
            alias_to_rel[tbl.name] = rel
    proj: dict[str, set] = {}
    for col in tree.find_all(exp.Column):
        if col.table and col.table in alias_to_rel:
            proj.setdefault(alias_to_rel[col.table], set()).add(col.name)
    return {rel: sorted(cols) for rel, cols in proj.items()}


def _base_select(tree):
    """Return the innermost 'base' SELECT of a compiled body (the CTE named
    ``base`` the compiler always emits, else the root SELECT)."""
    from sqlglot import exp
    for cte in tree.find_all(exp.CTE):
        if (cte.alias or "").lower() == "base":
            return cte.this
    return tree if hasattr(tree, "args") else None


def _split_and(pred):
    from sqlglot import exp
    if isinstance(pred, exp.And):
        return _split_and(pred.left) + _split_and(pred.right)
    return [pred]


def plan_pushdown_filters(body: str, tables: list[str]) -> dict[str, list[str]]:
    """Return ``{anchor_relation: [conjunct_sql, ...]}`` — WHERE conjuncts safe to
    pre-apply when landing the FROM-anchor base relation (Phase 2.4 row-filter
    push-down). Conjuncts are emitted **unqualified** (no table alias) so they drop
    into ``SELECT … FROM <base> WHERE <conjunct>``.

    **Correctness rule (provably safe):** the target body retains the *full* WHERE,
    so pushing a conjunct only pre-reduces rows. That can't change results *iff* the
    conjunct restricts the FROM **anchor** — the driving table, never on a nullable
    join side. So we push a top-level AND conjunct only when **every** column in it
    is qualified to the anchor. Any RIGHT/FULL join, an unqualified column, a
    non-table anchor, or a parse failure ⇒ push nothing (land full — still correct
    via the retained WHERE).
    """
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(body, read="duckdb")
    except Exception:  # noqa: BLE001
        return {}
    relset = set(tables or [])
    base = _base_select(tree)
    if base is None:
        return {}
    # Any RIGHT/FULL join in the base scope → unsafe, bail.
    for j in base.args.get("joins", []) or []:
        if (j.args.get("side") or "").upper() in ("RIGHT", "FULL"):
            return {}
    from_ = base.find(exp.From)
    if from_ is None or not isinstance(from_.this, exp.Table):
        return {}
    anchor = from_.this
    anchor_rel = ".".join(p.name for p in [anchor.args.get("db"), anchor.this] if p is not None)
    if anchor_rel not in relset:
        return {}
    anchor_names = {anchor.alias, anchor.name}
    where = base.args.get("where")
    if where is None:
        return {}
    pushable: list[str] = []
    for conj in _split_and(where.this):
        cols = list(conj.find_all(exp.Column))
        if not cols:
            continue  # constant conjunct — not worth pushing
        if all(c.table and c.table in anchor_names for c in cols):
            stripped = conj.copy()
            for c in stripped.find_all(exp.Column):
                c.set("table", None)
            pushable.append(stripped.sql(dialect="duckdb"))
    return {anchor_rel: pushable} if pushable else {}


def build_elt_plan(
    models: list[dict], landing_schema: str = _DEFAULT_LANDING_SCHEMA,
) -> dict:
    """Build an ELT execution plan from compiled models (transfer_then_transform).

    Each model item: ``{physical_name, model_name, select_body, summary}`` where
    ``summary['tables']`` lists base ``schema.table`` relations. Returns::

        {landing_schema, landing_relations: [{source_schema, source_table,
             landing_table}], datasets: [{physical_name, model_name,
             target_body}]}

    The distinct base relations across all datasets are landed once; each dataset's
    ``target_body`` reads from the landed relations. Pure — no I/O.
    """
    datasets: list[dict] = []
    landing: dict[tuple[str, str], dict] = {}
    proj_by_rel: dict[str, set] = {}
    # Per relation: the datasets that reference it, and each dataset's pushable
    # conjuncts for it. A shared landed table may only pre-filter on conjuncts
    # EVERY referencing dataset agrees on (intersection) — else a dataset that
    # needs the dropped rows would break.
    refs_by_rel: dict[str, list[frozenset]] = {}
    for m in models or []:
        tables = ((m.get("summary") or {}).get("tables")) or []
        body = m.get("select_body", "")
        # Column narrowing is computed on the ORIGINAL body (base relations still
        # named "schema"."table") and unioned across datasets — the landed table
        # must carry every column any dataset needs.
        for rel, cols in plan_extract_projections(body, tables).items():
            proj_by_rel.setdefault(rel, set()).update(cols)
        pushable = plan_pushdown_filters(body, tables)
        for rel in tables:
            refs_by_rel.setdefault(rel, []).append(frozenset(pushable.get(rel, [])))
        target_body, landing_map = rewrite_base_relations(body, tables, landing_schema)
        for lm in landing_map:
            landing[(lm["source_schema"], lm["source_table"])] = lm
        datasets.append({
            "physical_name": m.get("physical_name", ""),
            "model_name": m.get("model_name", ""),
            "target_body": target_body,
        })
    for (schema, table), lm in landing.items():
        rel = f"{schema}.{table}"
        cols = sorted(proj_by_rel.get(rel, []))
        lm["projection"] = cols or None   # None → land full (SELECT *)
        # Intersection of pushable conjuncts across all referencing datasets.
        ref_sets = refs_by_rel.get(rel, [])
        common = frozenset.intersection(*ref_sets) if ref_sets else frozenset()
        lm["filter"] = " AND ".join(sorted(common)) if common else None
    return {
        "landing_schema": landing_schema,
        "landing_relations": list(landing.values()),
        "datasets": datasets,
    }


def realized_placement(decision: PlacementDecision) -> str:
    """The placement the Phase-2.0 executor actually realizes.

    The executor computes the full product transform on the SOURCE and moves the
    shaped result (``transform_on_extract``) — a valid, conservative realization
    of ANY decision (doing shaping on the source is never *wrong*, only
    potentially less efficient than a true hybrid/ELT push-down, which is a
    Phase-2.1 optimization). This helper reports that so callers can surface an
    honest note when the recommended placement was hybrid/ELT.
    """
    return "transform_on_extract"
