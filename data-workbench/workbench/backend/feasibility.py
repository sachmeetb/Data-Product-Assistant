"""Top-down data-product feasibility: evidence → skill-evaluated stoplight verdict.

The pipeline per reference spec:

1. **Deterministic candidate evidence** (this module, no LLM):
   - Enumerate PUBLISHED products (version-pinned) as ``ready``/``adaptable``
     candidates AND estate raw datasets as ``assemblable`` candidates — never
     conflate the two.
   - A **one-to-one (bipartite) assignment** of spec attributes → candidate
     columns via ``schema_dna.score_matrix`` so a single column can't satisfy
     several requirements (fixes the per-source best-match double-counting).
   - Multi-dataset coverage + a **joinability** check (shared identity keys) for
     the assemblable path; derivability flags for spec-allowed adaptations.
2. **Skill decides the tier**, bounded by backend-enforced **invariants**: the
   deterministic heuristic tier is the CEILING; the skill may pick it or lower it
   (with a better rationale), but can never emit a greener tier than the evidence
   supports. A malformed/over-promising row falls back to the heuristic.
3. **Evaluation state** (``completed | partial | insufficient_evidence``) is
   DISTINCT from the business tier so a scan gap never masquerades as ``absent``.

The evaluator skill runs **one grounded call per domain batch** (not per spec) to
bound cost. Everything is persisted (``FeasibilityRun`` + ``FeasibilityScore``)
with corpus/evaluator/skill/embedding versions stamped for audit.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlmodel import Session, select

from . import embeddings
from . import estate as estate_mod
from . import feasibility_derivations as fderiv
from . import feasibility_map
from . import feasibility_spec as fspec
from . import schema_dna
from . import template_corpus
from .platform.type_system import CastCost, cast_cost
from .config import SKILLS_DIR
from .database import engine
from .models import (
    Estate,
    EstateScan,
    EstateSource,
    FeasibilityRun,
    FeasibilityScore,
    PlatformConnection,
)

logger = logging.getLogger(__name__)


def latest_scans_for_estate(session: Session, estate_id: int) -> list[int]:
    """The scan set a multi-catalog feasibility run reads: the latest
    completed|partial scan of EVERY ENABLED source in the estate (one per source =
    one per catalog). Sources with only failed/queued/running scans contribute
    nothing (their catalog is simply absent from the evidence)."""
    sources = session.exec(
        select(EstateSource).where(
            EstateSource.estate_id == estate_id,
            EstateSource.enabled == True,  # noqa: E712 — SQLModel needs ==
        )
    ).all()
    out: list[int] = []
    for src in sources:
        scan = session.exec(
            select(EstateScan).where(
                EstateScan.source_id == src.id,
                EstateScan.state.in_(["completed", "partial"]),  # type: ignore[attr-defined]
            ).order_by(EstateScan.scan_version.desc())  # type: ignore[union-attr]
        ).first()
        if scan is not None:
            out.append(scan.id)
    return out


def _run_scan_ids(run: FeasibilityRun) -> list[int]:
    """The scan set a run read — ``scan_ids_json`` (multi-catalog), falling back to
    the legacy single ``scan_id`` for runs created before that column shipped."""
    try:
        ids = json.loads(getattr(run, "scan_ids_json", "") or "[]")
        if ids:
            return [int(x) for x in ids]
    except (TypeError, ValueError):
        pass
    return [run.scan_id] if run.scan_id else []

EVALUATOR_VERSION = "1.0"
SKILL_NAME = "data-product-feasibility-evaluator"
RECOMMENDER_SKILL_NAME = "data-product-feasibility-recommender"
DERIVATION_ADVISOR_SKILL_NAME = "data-product-derivation-advisor"
MATCHER_SKILL_NAME = "data-product-feasibility-column-matcher"
BOUNDARY_ADVISOR_SKILL_NAME = "data-product-boundary-advisor"
ATTRIBUTE_GROUPER_SKILL_NAME = "data-product-attribute-grouper"

# ── R4 reasoning column-matcher levers (Phase 2) ──────────────────────────────
# A NEW, tool-less skill reasons over the estate's generated descriptions for the
# UNCERTAIN subset of a spec's attributes (a cost boundary — confident deterministic
# matches are kept as-is) and returns validated attribute→column decisions the
# deterministic engine pins. It never invents a column: it picks one it was SHOWN or
# says gap. Fully fail-safe — SDK absent / any failure → the deterministic result
# stands unchanged.
MATCHER_VERSION = "1.0"
# A direct match whose adjusted score is below this (but ≥ MATCH_THRESHOLD) is in the
# gray band → uncertain (the deterministic pick is plausible but not confident).
MATCHER_HIGH_CONF = 85
# Two candidates whose adjusted scores are within this are an ambiguous entity choice.
MATCHER_AMBIGUOUS_MARGIN = 8
# A required gap whose best near-miss is within this many points BELOW the threshold
# is a rescue candidate (the reasoning matcher may confirm it as a real match).
MATCHER_RESCUE_BAND = 12
# Bounds: at most this many uncertain attrs per spec, each with this many candidates.
_MATCHER_MAX_ATTRS_PER_SPEC = 12
_MATCHER_MAX_CANDS = 6

# ── R2 scoring levers (entity/authority-aware matching) ───────────────────────
# The scoped raw path re-ranks a spec attribute's candidate columns by a blend of
# the column's own semantic match and the affinity of its TABLE to the attribute's
# entity (so `customer_id` prefers `customers.customer_id` over a fact table's FK).
SCORING_VERSION = "r2"
# Weight on the attribute↔table affinity in the adjusted score (0..1). The rest
# rides the column's own semantic match. Kept modest so a strong column match
# still dominates; affinity only breaks ties / demotes wrong-entity carriers.
TABLE_AFFINITY_WEIGHT = 0.25
# Points (on the 0–100 adjusted scale) docked from an inferred FK-carrier column
# when the authoritative table it points to is in scope AND itself carries a
# matching column — so the dimension's own key wins over a fact table's FK.
FK_CARRIER_PENALTY = 15
# How many ranked alternatives to retain per attribute (matched runners-up + gap
# near-misses) for the drill-down.
MAX_ALTERNATIVES = 5

# ── R3 composite-derivation levers (single-pass reconciliation) ───────────────
# A spec attribute with no single-column match may be satisfied by COMPOSING ≥2
# columns of the SAME table (name = first_name + last_name). Composites are
# first-class candidates in the ONE reconciliation pass — never a post-hoc mutation.
# Minimum per-component match (alias-equality is 100; schema_dna similarity is the
# secondary) for a role to resolve to a column.
COMPONENT_MATCH_THRESHOLD = 70
# A table is composite-eligible for an attribute when its entity affinity clears
# this floor (else the single top-affinity table is used) — so a composition only
# forms on a table whose entity plausibly owns the attribute.
COMPOSITE_TABLE_AFFINITY_FLOOR = 10
# Points docked from a composite's adjusted score when it competes with a direct on
# a COMPARABLE-entity table — so a real single column is preferred over composing.
# It is DROPPED when the composite's table is clearly more entity-relevant than the
# best direct's table (the direct is a wrong-entity match): the modest table-affinity
# blend (TABLE_AFFINITY_WEIGHT) alone can't overcome a perfect wrong-entity column
# name, so a flat penalty would wrongly keep e.g. `suppliers.name` over the ideal
# `customers.first_name + last_name`. See _composite_penalty.
DERIVATION_PENALTY = 6
# The affinity gap (composite table − best-direct table) over which the penalty
# ramps LINEARLY from full → zero. A gap ≤ 0 (composite table no more relevant) keeps
# the full penalty (a real single column wins); a gap ≥ this scale removes it (the
# direct is a clear wrong-entity match); in between it degrades smoothly (no cliff).
# Kept modest since embedding table-fit gaps are compressed to a few points.
AFFINITY_PREFERENCE_SCALE = 6


def _attr_index_for_id(attrs: list[fspec.SpecAttribute], attr_id: str) -> Optional[int]:
    """Recover the 0-based attribute index from a stable id (``#3:customer_id``),
    bounds-checked against the current attribute list."""
    m = re.match(r"^#(\d+):", attr_id or "")
    if not m:
        return None
    idx = int(m.group(1))
    return idx if 0 <= idx < len(attrs) else None


def _load_curated_patterns() -> tuple[list[fderiv.DerivationPattern], str]:
    """Load the curated derivation catalog, converting a fail-closed
    :class:`DerivationCatalogError` into a surfaced ``(., error)`` warning so the
    run stamps ``derivation_catalog_error`` instead of silently seeing no patterns."""
    try:
        return fderiv.load_patterns(), ""
    except fderiv.DerivationCatalogError as exc:
        logger.warning("derivation catalog rejected (fail-closed): %s", exc)
        return [], str(exc)[:300]


def _component_score(role_aliases: list[str], col_name: str) -> int:
    """0–100 match of a component role (its alias set) to a candidate column name.
    Alias-equality is a perfect 100 (a curated alias is an exact-intent claim);
    otherwise the best schema_dna text similarity across the aliases."""
    targets = {fderiv.norm_alias(a) for a in role_aliases if a}
    if fderiv.norm_alias(col_name) in targets:
        return 100
    best = 0
    cn = (col_name or "").replace("_", " ")
    for a in role_aliases:
        if a:
            best = max(best, schema_dna.text_similarity(a.replace("_", " "), cn))
    return best


def _resolve_components(
    roles: list[tuple[str, list[str], str]],
    table_cols: list[tuple[int, str, str]],
) -> Optional[list[dict[str, Any]]]:
    """Injectively assign each role → a DISTINCT column of ONE table.

    ``roles`` = ``[(role, aliases, type_family)]``; ``table_cols`` =
    ``[(flat_col_index, col_name, col_type)]``. Returns one resolved dict per role
    (``{role, col_j, col_name, score}``), each column DISTINCT and scoring
    ≥ ``COMPONENT_MATCH_THRESHOLD``, honouring the per-role ``type_family`` gate —
    or ``None`` if any role can't resolve (→ no composite)."""
    triples: list[tuple[int, int, int]] = []  # (score, role_idx, col_pos)
    for ri, (role, aliases, tfam) in enumerate(roles):
        for ci, (_j, cname, ctype) in enumerate(table_cols):
            if tfam and schema_dna.type_family(ctype) != tfam:
                continue  # type-family gate (e.g. birth_date must be temporal)
            sc = _component_score([role] + list(aliases), cname)
            if sc >= COMPONENT_MATCH_THRESHOLD:
                triples.append((sc, ri, ci))
    triples.sort(key=lambda t: (-t[0], t[1], t[2]))  # deterministic greedy
    role_to: dict[int, tuple[int, int]] = {}
    used_cols: set[int] = set()
    for sc, ri, ci in triples:
        if ri in role_to or ci in used_cols:
            continue
        role_to[ri] = (ci, sc)
        used_cols.add(ci)
    if len(role_to) != len(roles):
        return None  # a role could not resolve to a distinct column
    out: list[dict[str, Any]] = []
    for ri, (role, _aliases, _tfam) in enumerate(roles):
        ci, sc = role_to[ri]
        j, cname, _ctype = table_cols[ci]
        out.append({"role": role, "col_j": j, "col_name": cname, "score": sc})
    return out


def _eligible_composite_tables(
    i: int, unique_tables: list[str], affinity: dict[tuple[int, str], float], floor: float,
) -> list[str]:
    """Tables whose entity affinity for attribute ``i`` clears ``floor`` — else the
    single top-affinity table (deterministic), never a score-boosted free-for-all."""
    elig = [t for t in unique_tables if affinity.get((i, t), 0.0) >= floor]
    if elig:
        return elig
    if not unique_tables:
        return []
    return sorted(unique_tables, key=lambda t: (-affinity.get((i, t), 0.0), t))[:1]


def _composite_penalty(composite_affinity: float, best_direct_affinity: float) -> float:
    """The replacement penalty for a composite, ramped by how much more
    entity-relevant its table is than the best same-attr direct's table.

    ``gap = composite_affinity − best_direct_affinity``: ``gap ≤ 0`` (composite table
    no more relevant) keeps the full ``DERIVATION_PENALTY`` so a real single column
    wins; ``gap ≥ AFFINITY_PREFERENCE_SCALE`` (a clear wrong-entity direct) removes it;
    between, the penalty degrades linearly — no threshold cliff. ``best_direct_affinity``
    is 0 for a gap attribute (no direct), so a gap is filled uncontested."""
    gap = composite_affinity - best_direct_affinity
    if gap <= 0:
        return float(DERIVATION_PENALTY)
    frac = min(1.0, gap / AFFINITY_PREFERENCE_SCALE)
    return float(DERIVATION_PENALTY) * (1.0 - frac)


def _build_composite_candidate(
    i: int, pattern_id: str, kind_name: str, operator: str, separator: str,
    dataset: dict, resolved: list[dict], affinity_it: float, *, source: str,
    penalty: float = DERIVATION_PENALTY,
) -> dict[str, Any]:
    """Shape one composite candidate (curated pattern OR validated LLM proposal)
    with the component-quality blend and the (conditional) derivation penalty."""
    scores = [r["score"] for r in resolved]
    cmin = float(min(scores)) if scores else 0.0
    cmean = (sum(scores) / len(scores)) if scores else 0.0
    quality = 0.7 * cmin + 0.3 * cmean
    adjusted = (1 - TABLE_AFFINITY_WEIGHT) * quality + TABLE_AFFINITY_WEIGHT * affinity_it - penalty
    return {
        "attr": i, "kind": "composite", "adjusted": adjusted, "tiebreak": cmin,
        "kind_rank": 1, "pattern_id": pattern_id, "col": None,
        "table": dataset.get("table", ""), "dataset_uri": dataset.get("uri", ""),
        "dataset_schema": dataset.get("schema", ""), "dataset_database": dataset.get("database", ""),
        "dataset_platform": dataset.get("platform", ""),
        "components": resolved, "component_min": cmin, "component_mean": cmean,
        "affinity": affinity_it, "kind_name": kind_name, "operator": operator,
        "separator": separator, "source": source,
        "sort_ref": dataset.get("table", ""),
        "sort_cols": "+".join(sorted(r["col_name"] for r in resolved)),
    }

# ── tiered schema-scoping levers (Stage 1: product → schema shortlist) ─────────
# A schema (keyed by (database, schema)) must clear the relevance FLOOR to count as
# relevant at all; a schema at/above the SHORTLIST THRESHOLD is "confidently
# relevant" and is shortlisted (up to MAX_SCHEMAS_PER_SPEC of them). When NOTHING
# reaches the threshold but some schema clears the floor, the single best
# floor-passer is still evaluated (low confidence) so a weakly-relevant estate
# grades instead of a false 'absent'. All three are 0–100 on the same scale as
# MATCH_THRESHOLD; overridable per run via the request → schema_scoping_json.
SCHEMA_RELEVANCE_FLOOR = 45
SCHEMA_SHORTLIST_THRESHOLD = 70
MAX_SCHEMAS_PER_SPEC = 5
# Above this estate/spec similarity a recommended spec is a 'strong' band (pre-
# checked); below is 'tentative'. Mirrors ResolveAndBind's confidence bands.
RECOMMEND_FLOOR = 55
# Cap the column sample folded into a schema's relevance text so the blob stays
# bounded regardless of how wide the schema is.
_SCHEMA_TEXT_COL_SAMPLE = 40

# Tier ranks (green→red). The deterministic heuristic tier is the ceiling.
TIER_RANK = {"absent": 0, "assemblable": 1, "adaptable": 2, "ready": 3}
RANK_TIER = {v: k for k, v in TIER_RANK.items()}

# Required-attribute coverage floors per tier.
FLOOR_READY = 0.90
FLOOR_ADAPTABLE = 0.60
FLOOR_ASSEMBLABLE = 0.60
# A product must clear this to be considered a green/adaptable candidate at all.
PRODUCT_CANDIDATE_FLOOR = 0.30


# ── product-candidate enumeration (version-pinned published products) ─────────

_FEASIBILITY_PRODUCTS = """\
MATCH (dp:DProdDataProduct)
OPTIONAL MATCH (dp)<-[:MATERIALISES_AS]-(dc:DataContract)
OPTIONAL MATCH (dc)-[:HAS_VERSION]->(cv_pub:ContractVersion)
WHERE cv_pub.lifecycleState IN ['published','superseded']
WITH dp, dc, cv_pub ORDER BY cv_pub.version DESC
WITH dp, dc, head(collect(cv_pub)) AS cv
WHERE cv IS NOT NULL
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
WITH dp, dc, cv, collect(DISTINCT CASE WHEN pc IS NULL THEN null ELSE {
    name: pc.name,
    type: coalesce(pc.dataType, pc.logicalType, ''),
    concept: coalesce(pc.logicalName, pc.name),
    is_key: coalesce(pc.isPrimaryKey, false)
} END) AS columns
RETURN dp.uri AS uri, dc.id AS contract_id, cv.version AS version,
       coalesce(dc.productKind,'') AS product_kind,
       coalesce(cv.snapshotDomain, dc.domain) AS domain,
       dp.name AS name, columns
"""


def enumerate_product_candidates(session: Session) -> list[dict[str, Any]]:
    """All published products (version-pinned) with their output columns.

    The ``ready``/``adaptable`` candidate pool — governed products only, never
    raw estate data.
    """
    out: list[dict[str, Any]] = []
    with estate_mod.estate_graph_session(session) as ns:
        for rec in ns.run(_FEASIBILITY_PRODUCTS):
            cols = [c for c in (rec["columns"] or []) if c and c.get("name")]
            out.append({
                "uri": rec["uri"], "contract_id": rec["contract_id"],
                "version": str(rec["version"]) if rec["version"] is not None else "",
                "product_kind": rec["product_kind"] or "",
                "domain": rec["domain"] or "", "name": rec["name"] or "",
                "columns": cols,
            })
    return out


# ── bipartite one-to-one assignment ──────────────────────────────────────────

def _spec_features(attrs: list[fspec.SpecAttribute]) -> list[schema_dna.ColumnFeature]:
    # Carry the spec attribute's DESCRIPTION onto the source ColumnFeature so the
    # semantic axis is symmetric with the estate side (whose columns embed their own
    # descriptions). Without it the dominant 0.42-weight semantic axis is starved of
    # the disambiguating prose that separates e.g. a "customer name" from a "nation
    # name". No embed-cache impact: estate (target) keys are unchanged; only the small
    # spec-attribute set is (re-)embedded per run.
    return [schema_dna.ColumnFeature(name=a.name, type=a.type, concept=a.concept,
                                     description=a.description)
            for a in attrs]


def _attr_id(index: int, name: str) -> str:
    """Stable per-attribute id — index-based so a 210-field spec that repeats
    ``id`` / ``name`` / ``status`` keys distinctly (never the bare name)."""
    return f"#{index}:{name}"


def _bipartite_assign(
    spec_attrs: list[fspec.SpecAttribute],
    candidate_cols: list[dict[str, Any]],
    variant: str = schema_dna.DEFAULT_VARIANT,
) -> dict[str, Any]:
    """Greedy uniqueness assignment of spec attributes → candidate columns.

    Sorts every above-threshold pair by score and assigns each spec attribute to
    at most one column and each column to at most one attribute — so one column
    can never cover two requirements. Returns coverage split by required/optional.
    """
    src = _spec_features(spec_attrs)
    tgt = [schema_dna.column_from_dict(c) for c in candidate_cols]
    if not src or not tgt:
        return {
            "assignment": [], "required_coverage": 0.0, "total_coverage": 0.0,
            "gaps": [{"attr_id": _attr_id(idx, a.name), "spec_attr": a.name,
                      "required": a.required, "is_key": a.is_key, "status": "missing"}
                     for idx, a in enumerate(spec_attrs)],
            "embeddings_available": schema_dna.available_embeddings(),
        }
    matrix = schema_dna.score_matrix(src, tgt, variant)
    cells = matrix["cells"]
    threshold = matrix["match_threshold"]

    pairs = sorted(
        (
            (cells[i][j]["overall"], i, j)
            for i in range(len(src)) for j in range(len(tgt))
            if cells[i][j]["overall"] >= threshold
        ),
        reverse=True,
    )
    taken_src: set[int] = set()
    taken_tgt: set[int] = set()
    assignment: list[dict[str, Any]] = []
    for score, i, j in pairs:
        if i in taken_src or j in taken_tgt:
            continue
        taken_src.add(i)
        taken_tgt.add(j)
        attr = spec_attrs[i]
        col = candidate_cols[j]
        deriv = _infer_derivation(attr, col, cells[i][j]["axes"])
        row: dict[str, Any] = {
            "attr_id": _attr_id(i, attr.name),
            "spec_attr": attr.name,
            "required": attr.required,
            "is_key": attr.is_key,
            "column": col.get("name", ""),
            "dataset_uri": col.get("dataset_uri"),
            "dataset_table": col.get("dataset_table"),
            "score": score,
            "axes": cells[i][j]["axes"],
            "derivation": deriv,
            "status": "derivable" if deriv else "direct_match",
        }
        # Pass through provenance fields when present (set by callers).
        for _k in ("dataset_schema", "dataset_database", "dataset_platform", "product_name",
                   "is_fk_carrier", "fk_target_table"):
            if _k in col:
                row[_k] = col[_k]
        assignment.append(row)

    req_total = sum(1 for a in spec_attrs if a.required)
    req_covered = sum(1 for r in assignment if r["required"])
    gaps = [
        {"attr_id": _attr_id(idx, a.name), "spec_attr": a.name, "required": a.required,
         "is_key": a.is_key, "status": "missing"}
        for idx, a in enumerate(spec_attrs) if idx not in taken_src
    ]
    return {
        "assignment": assignment,
        "required_coverage": (req_covered / req_total) if req_total else 1.0,
        "total_coverage": (len(assignment) / len(spec_attrs)) if spec_attrs else 0.0,
        "gaps": gaps,
        "embeddings_available": matrix["embeddings_available"],
    }


def _infer_derivation(attr: fspec.SpecAttribute, col: dict, axes: dict) -> Optional[str]:
    """Name a spec-allowed derivation for an assigned pair, grounded in the axes.

    Conservative: a strong-meaning / weak-name match under an allowed ``rename`` is
    a rename; a numeric monetary attribute the spec allows to be currency-
    normalized surfaces that. Returns None when no allowed derivation applies.
    """
    allowed = {d.value for d in (attr.derivations or [])}
    char = axes.get("character")
    semantic = axes.get("semantic")
    if "rename" in allowed and (col.get("name", "").lower() != attr.name.lower()) \
            and (char is not None and char < 60) and (semantic is not None and semantic >= 70):
        return "rename"
    if "currency_normalize" in allowed and (attr.type in ("numeric", "decimal", "float")):
        return "currency_normalize"
    return None


# ── FK-carrier heuristic ─────────────────────────────────────────────────────

def _split_words(name: str) -> list[str]:
    """camelCase / snake_case / digit-boundary → ordered lowercase words.

    ``customerID`` → ``["customer", "id"]``; ``customer_id`` → same;
    ``CustomerIdentifier`` → ``["customer", "identifier"]``. Order-preserving
    (unlike :func:`schema_dna.tokenize`, which returns a set)."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    spaced = re.sub(r"([A-Za-z])([0-9])", r"\1 \2", spaced)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def _singular(word: str) -> str:
    """Naive singularizer for table/entity token matching (customers→customer)."""
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


_FK_SUFFIX_WORDS = {"id", "identifier"}

# ── identifier-shape gate (R4) ────────────────────────────────────────────────
# A category/label spec attribute matching an identifier-shaped column (or the
# reverse) is almost always a false positive (`customer_type` → `c_customer_id`,
# `name` → `nation_id`). We don't hard-drop it — a raised admission margin turns a
# marginal shape-mismatch into a gap while preserving strong id matches; Phase 2's
# reasoning matcher can still rescue a genuine one.
IDENT_MISMATCH_MARGIN = 15


def _is_identifier_name(name: str) -> bool:
    """Name-only identifier check (camelCase/snake_case aware): a column/attr whose
    final word is ``id``/``identifier`` (incl. a bare ``id``). More robust than
    :func:`_is_key_name` (which misses camelCase ``customerID``), so the gate below
    treats ``customerID`` and ``customer_id`` alike."""
    words = _split_words(name)
    return bool(words) and words[-1] in _FK_SUFFIX_WORDS


def _is_identifier_attr(attr: fspec.SpecAttribute) -> bool:
    """A spec attribute is an identifier when it is a declared key or its name is
    id-shaped (``customer_id``, ``card_id``)."""
    return bool(attr.is_key) or _is_identifier_name(attr.name)


def _identifier_shape_mismatch(attr: fspec.SpecAttribute, col_name: str) -> bool:
    """True when exactly ONE of (attribute, column) is identifier-shaped — a
    category↔id or id↔category mismatch that the admission margin should gate."""
    return _is_identifier_attr(attr) != _is_identifier_name(col_name)


def _infer_fk_carrier(col_name: str, table_name: str, all_table_names: set[str]) -> dict:
    """Heuristic (name-only, camelCase-aware): is this column a FK carrier?

    A column whose name is ``<entity>Id`` / ``<entity>_id`` / ``<entity>Identifier``
    (any case, digit/acronym tolerant) carries a reference to ``<entity>``'s table.
    Returns ``is_fk_carrier=True, fk_target_table=<table>`` when:
    - The name splits into ≥2 words ending in ``id`` / ``identifier`` (a bare
      ``id`` is the table's OWN key → not a carrier; the authoritative table's key
      is never self-labeled).
    - The entity stem does NOT match the column's own table (singular/plural
      aware) — otherwise it's that table's own key.
    - Some OTHER table in scope carries the entity token (singular/plural aware).
    Returns an empty dict when the heuristic doesn't apply. Databricks exposes no
    FK/PK metadata, so this name signal is the only authority hint available.
    """
    words = _split_words(col_name)
    if len(words) < 2 or words[-1] not in _FK_SUFFIX_WORDS:
        return {}
    stem = _singular(words[-2])  # the entity immediately before the id suffix
    self_tokens = {_singular(w) for w in _split_words(table_name)}
    if stem in self_tokens:
        return {}  # the table's own key — not a foreign reference
    for t in sorted(all_table_names):
        if t == (table_name or "").lower():
            continue
        if stem in {_singular(w) for w in _split_words(t)}:
            return {"is_fk_carrier": True, "fk_target_table": t}
    return {}


# ── joinability (shared identity keys) for the assemblable path ───────────────

def _is_key_name(name: str) -> bool:
    low = (name or "").lower()
    return low == "id" or low.endswith("_id")


def joinability(datasets: list[dict[str, Any]]) -> dict[str, Any]:
    """Can these estate datasets be joined? Name-based shared-identity-key check.

    The deterministic metadata scan carries no FK constraints, so joinability is
    inferred from shared identity-key column names (customer_id, account_id, …) —
    the same signal ``join_preflight`` ranks bridges on. Returns whether the used
    datasets form ONE connected component + the discovered join paths + missing
    links (disconnected islands).
    """
    n = len(datasets)
    if n <= 1:
        return {"joinable": True, "single_dataset": True, "paths": [], "missing": []}
    key_sets = []
    for d in datasets:
        keys = {c.get("name", "").lower() for c in d.get("columns", [])
                if _is_key_name(c.get("name", ""))}
        key_sets.append(keys)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    paths: list[dict[str, Any]] = []
    for a in range(n):
        for b in range(a + 1, n):
            shared = sorted(key_sets[a] & key_sets[b])
            if shared:
                pa, pb = find(a), find(b)
                if pa != pb:
                    parent[pa] = pb
                paths.append({
                    "left": datasets[a].get("table", ""),
                    "right": datasets[b].get("table", ""),
                    "on": shared,
                })
    roots = {find(i) for i in range(n)}
    joinable = len(roots) == 1
    missing = []
    if not joinable:
        # Report the islands that couldn't be bridged.
        by_root: dict[int, list[str]] = {}
        for i in range(n):
            by_root.setdefault(find(i), []).append(datasets[i].get("table", ""))
        missing = list(by_root.values())
    return {"joinable": joinable, "single_dataset": False, "paths": paths,
            "missing": missing}


# ── per-spec evidence bundle ──────────────────────────────────────────────────

def _product_evidence(spec: fspec.FeasibilitySpec, products: list[dict]) -> list[dict]:
    """Rank published products as green/adaptable candidates for a spec."""
    out = []
    for p in products:
        # Stamp product_name on each column so it flows through the assignment rows.
        cols = [{**c, "product_name": p["name"]} for c in p["columns"]]
        assign = _bipartite_assign(spec.attributes, cols)
        if assign["required_coverage"] < PRODUCT_CANDIDATE_FLOOR and assign["total_coverage"] < PRODUCT_CANDIDATE_FLOOR:
            continue
        out.append({
            "uri": p["uri"], "version": p["version"], "contract_id": p["contract_id"],
            "product_kind": p["product_kind"], "domain": p["domain"], "name": p["name"],
            "required_coverage": round(assign["required_coverage"], 3),
            "total_coverage": round(assign["total_coverage"], 3),
            "assignment": assign["assignment"],
            "gaps": assign["gaps"],
        })
    out.sort(key=lambda c: (c["required_coverage"], c["total_coverage"]), reverse=True)
    return out


# ── Stage 1: schema-level relevance shortlist ─────────────────────────────────
#
# A scan's schemas are effectively unrelated functional areas (a `sales` schema
# and an `employees` schema may both carry a `product_id`). Before the column
# assignment runs, each spec is matched against the estate's SCHEMA-level text so
# only the relevant functional areas contribute columns — no false-positive
# matches from an unrelated schema. Keyed by (database, schema) since schema names
# can collide across catalogs. Degrades gracefully to table/column names when the
# estate is unenriched (embeddings → token_jaccard fallback).

def _schema_descriptions_with_source(
    session: Session, scans: list[EstateScan],
) -> dict[tuple[str, str], dict[str, str]]:
    """Map (database, schema) → ``{"text": desc, "source": "llm"|"fallback"}`` across
    a run's contributing scans.

    Reads each scan's ``enrichment_summary_json`` (``{schema_descriptions: {<bare
    schema>: desc}, schema_description_source: {<bare schema>: source}}``) and
    resolves that scan's database (the catalog for 3-level platforms) via
    :func:`estate_mod.resolve_source_connection_ref` so the key aligns with the
    dataset's ``(d.database, d.schema)``. The ``source`` (missing on pre-R2 scans →
    ``"llm"``) lets the read-time layer down-weight synthesized prose."""
    out: dict[tuple[str, str], dict[str, str]] = {}
    for scan in scans:
        try:
            summary = json.loads(getattr(scan, "enrichment_summary_json", "") or "{}")
        except (TypeError, ValueError):
            summary = {}
        descs = summary.get("schema_descriptions") if isinstance(summary, dict) else None
        if not isinstance(descs, dict) or not descs:
            continue
        sources = summary.get("schema_description_source") if isinstance(summary, dict) else None
        if not isinstance(sources, dict):
            sources = {}
        database = ""
        source = session.get(EstateSource, scan.source_id)
        if source is not None:
            try:
                _platform, ref = estate_mod.resolve_source_connection_ref(session, source)
                database = (ref.get("database") or "").strip()
            except Exception:
                database = ""
        for sch, desc in descs.items():
            if desc:
                out[(database, str(sch))] = {
                    "text": str(desc), "source": str(sources.get(sch) or "llm"),
                }
    return out


def _schema_descriptions(session: Session, scans: list[EstateScan]) -> dict[tuple[str, str], str]:
    """Back-compat map (database, schema) → enriched description text only.
    (Used by :func:`build_estate_description`; feasibility scoping uses the
    source-aware map + read-time fallback.)"""
    return {k: v["text"] for k, v in _schema_descriptions_with_source(session, scans).items()}


def _synth_schema_desc(datasets: list[dict]) -> str:
    """Read-time schema-description synthesis from a schema's table descriptions /
    names (deterministic, sorted) — so a scan that predates enrichment provenance,
    or a schema the LLM left blank, still contributes a description without a
    re-enrichment pass. Peer of ``estate_enrich._fallback_schema_description``."""
    tables = sorted({(d.get("table") or "").strip() for d in datasets
                     if (d.get("table") or "").strip()})
    if not tables:
        return ""
    shown = tables[:8]
    more = len(tables) - len(shown)
    listing = ", ".join(shown) + (f", and {more} more" if more > 0 else "")
    base = f"Schema with {len(tables)} table(s): {listing}."
    for d in sorted(datasets, key=lambda x: x.get("table", "")):
        td = (d.get("description") or "").strip()
        if td:
            base += f" {td[:200]}"
            break
    return base


def _resolve_schema_descs(
    stored: dict[tuple[str, str], dict[str, str]],
    datasets_by_key: dict[tuple[str, str], list[dict]],
) -> dict[tuple[str, str], dict[str, str]]:
    """Fill in a read-time ``fallback`` description for any in-scope schema that has
    no stored description — so existing scans improve without re-enrichment."""
    out = dict(stored)
    for key, ds in datasets_by_key.items():
        entry = out.get(key)
        if not entry or not (entry.get("text") or "").strip():
            text = _synth_schema_desc(ds)
            if text:
                out[key] = {"text": text, "source": "fallback"}
    return out


def _schema_group_text(datasets: list[dict], schema_desc: str,
                       source: str = "llm") -> str:
    """The relevance text for one (database, schema) group: the enriched schema
    description (when present) + table names + table descriptions + a bounded
    sample of column names/descriptions. Everything degrades to bare identifiers
    when the estate is unenriched.

    When ``source == "fallback"`` the description was SYNTHESIZED from the same
    table names/descriptions that already appear below, so folding it in as if it
    were authoritative LLM prose would double-count that signal — we skip it and
    let the real table/column identifiers carry the relevance."""
    parts: list[str] = []
    if schema_desc and source != "fallback":
        parts.append(schema_desc)
    table_names: list[str] = []
    for ds in datasets:
        tbl = (ds.get("table") or "").strip()
        if tbl:
            table_names.append(tbl)
        tdesc = (ds.get("description") or "").strip()
        if tdesc:
            parts.append(tdesc)
    if table_names:
        parts.append("tables: " + ", ".join(table_names))
    col_bits: list[str] = []
    for ds in datasets:
        for c in ds.get("columns", []) or []:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            cdesc = (c.get("description") or "").strip()
            col_bits.append(f"{name}: {cdesc}" if cdesc else name)
            if len(col_bits) >= _SCHEMA_TEXT_COL_SAMPLE:
                break
        if len(col_bits) >= _SCHEMA_TEXT_COL_SAMPLE:
            break
    if col_bits:
        parts.append("columns: " + ", ".join(col_bits))
    return ". ".join(p for p in parts if p)


def _spec_text(spec: fspec.FeasibilitySpec) -> str:
    """The relevance text for a spec: name + description + domain + top attribute
    concepts (what the desired product is about)."""
    parts = [spec.name or "", spec.description or "", spec.domain or ""]
    concepts: list[str] = []
    for a in spec.attributes:
        c = (a.concept or a.name or "").strip()
        if c:
            concepts.append(c)
        if len(concepts) >= 24:
            break
    if concepts:
        parts.append("attributes: " + ", ".join(concepts))
    return ". ".join(p for p in parts if p)


def _text_embed_cache(texts: list[str]) -> dict[str, list[float]]:
    """Embed a batch of texts ONCE (text → vector) for reuse across many
    :func:`schema_dna.text_similarity` calls. Empty when embeddings are
    unavailable — callers then fall through to the token-Jaccard path."""
    if not schema_dna.available_embeddings():
        return {}
    uniq = sorted({t for t in texts if t and t.strip()})
    if not uniq:
        return {}
    vecs = embeddings.embed_documents(uniq)
    if not vecs or len(vecs) != len(uniq):
        return {}
    return {t: v for t, v in zip(uniq, vecs)}


def _schema_shortlist(
    spec: fspec.FeasibilitySpec,
    datasets_by_key: dict[tuple[str, str], list[dict]],
    schema_desc_map: dict[tuple[str, str], str],
    floor: float,
    cap: int,
    *,
    threshold: float = SCHEMA_SHORTLIST_THRESHOLD,
    schema_texts: Optional[dict[tuple[str, str], str]] = None,
    embed_cache: Optional[dict[str, list[float]]] = None,
    desc_source_map: Optional[dict[tuple[str, str], str]] = None,
    include_override: Optional[set[tuple[str, str]]] = None,
    exclude_override: Optional[set[tuple[str, str]]] = None,
) -> list[dict[str, Any]]:
    """Rank each (database, schema) group by relevance to the spec.

    Returns EVERY schema (so the UI can show near-misses) as
    ``{database, schema, score, included, dataset_count, band, threshold_distance,
    description_source, excluded_reason?}`` — structured reason codes the UI +
    report render as a deterministic rationale (``matched_tables`` +
    coverage codes are added later by :func:`build_spec_evidence`, which knows the
    scoped assignment). ``included`` is the top ``cap`` schemas at/above ``threshold``
    (the confident band); if none reach it, the single best ``score >= floor`` schema
    is included as a low-confidence ``fallback``. ``schema_texts`` / ``embed_cache`` let :func:`evaluate_run` precompute
    the schema texts + embeddings ONCE per run; standalone callers can omit them.
    """
    if schema_texts is None:
        schema_texts = {
            key: _schema_group_text(ds, schema_desc_map.get(key, ""))
            for key, ds in datasets_by_key.items()
        }
    desc_source_map = desc_source_map or {}
    spec_txt = _spec_text(spec)
    cache = dict(embed_cache or {})
    if schema_dna.available_embeddings():
        missing = [t for t in ([spec_txt] + list(schema_texts.values()))
                   if t and t not in cache]
        if missing:
            uniq = sorted(set(missing))
            vecs = embeddings.embed_documents(uniq)
            if vecs and len(vecs) == len(uniq):
                cache.update({t: v for t, v in zip(uniq, vecs)})

    scored: list[dict[str, Any]] = []
    for key, txt in schema_texts.items():
        score = int(schema_dna.text_similarity(spec_txt, txt, cache))
        scored.append({
            "database": key[0], "schema": key[1], "score": score,
            "dataset_count": len(datasets_by_key.get(key, [])),
            "threshold_distance": int(round(score - floor)),
            "band": "strong" if score >= threshold else ("moderate" if score >= floor else "weak"),
            "description_source": desc_source_map.get(key, "none") or "none",
        })
    scored.sort(key=lambda x: (-x["score"], x["database"], x["schema"]))
    inc_ov = include_override or set()
    exc_ov = exclude_override or set()
    # The auto-shortlist is computed ONCE, independent of the user's overrides — so
    # EXCLUDING a schema simply removes it from scope and does NOT free a cap slot for
    # a lower schema to backfill into. The cap is a default ceiling on the auto pick,
    # not a quota the scope must always hit.
    # Stage A: schemas at/above the shortlist threshold are confidently relevant —
    # keep the top `cap` of them. Also gated on the floor so a pathological
    # threshold<floor config can never admit a below-floor schema (the floor is the
    # hard 'is anything relevant' gate; the threshold is the confident band).
    auto_included: set[tuple[str, str]] = set()
    count = 0
    for row in scored:
        if row["score"] >= threshold and row["score"] >= floor and count < cap:
            auto_included.add((row["database"], row["schema"]))
            count += 1
    # Stage B (best-one fallback): nothing cleared the threshold but some schema clears
    # the floor → evaluate the single best floor-passer (low confidence) so a weakly-
    # relevant estate still grades instead of a false 'absent'. `scored` is sorted desc,
    # so the first floor-passer is the best.
    fallback_key: Optional[tuple[str, str]] = None
    if not auto_included:
        for row in scored:
            if row["score"] >= floor:
                fallback_key = (row["database"], row["schema"])
                break
    for row in scored:
        key = (row["database"], row["schema"])
        if key in exc_ov:               # user dropped it (e.g. a duplicate schema)
            row["included"] = False
            row["excluded_reason"] = "user_excluded"
        elif key in inc_ov:             # user forced it in (a below-floor schema)
            row["included"] = True
            row["override"] = "user_included"
        elif key in auto_included:      # a confident (>= threshold) pick within the cap
            row["included"] = True
        elif key == fallback_key:       # the single best floor-passer (no threshold pick)
            row["included"] = True
            row["fallback"] = True
        else:
            row["included"] = False
            if row["score"] < floor:
                row["excluded_reason"] = "below_floor"
            elif row["score"] >= threshold:
                row["excluded_reason"] = "over_cap"
            else:                       # floor <= score < threshold, not the fallback pick
                row["excluded_reason"] = "below_threshold"
    return scored


def _attr_affinity_text(attr: fspec.SpecAttribute) -> str:
    """The text that expresses WHAT ENTITY this attribute belongs to — its name,
    concept and description. Deliberately per-ATTRIBUTE (not product-level): a
    product-level affinity would bias every column toward the dominant entity and
    break cross-entity attrs like ``transaction_date`` on a Customer-360 spec."""
    return ". ".join(p for p in (attr.name or "", attr.concept or "", attr.description or "") if p)


def _table_text(table: str, description: str) -> str:
    """Relevance text for a candidate table: its (word-split) name + description."""
    return f"{(table or '').replace('_', ' ')} {description or ''}".strip()


def _alt_reason(col_ov: int, threshold: int, fk: bool, penalized: bool,
                axes: dict, *, chosen: bool, ident_gated: bool = False) -> str:
    """Rejection/role code for one alternative candidate (∈ below_threshold,
    identifier_mismatch, type_incompatible, wrong_entity, fk_carrier,
    insufficient_evidence, chosen)."""
    if chosen:
        return "chosen"
    if col_ov < threshold:
        return "below_threshold"
    if ident_gated:
        return "identifier_mismatch"
    if fk and penalized:
        return "fk_carrier"
    dt = (axes or {}).get("data_type")
    if dt is not None and dt < 40:
        return "type_incompatible"
    return "wrong_entity"


def _cast_hint(attr: fspec.SpecAttribute, col: dict) -> Optional[dict[str, Any]]:
    """If a matched pair needs a (cheap) cast for the estate column to fit the spec's
    type, describe it — an informational hint for the mapping panel + the engineer's
    later transform, NOT a status change (so a trivial int→bigint cast never demotes a
    'ready' product to 'derivable'). ``None`` when types match or a cast is structurally
    impossible."""
    a_type = (attr.type or "").strip()
    c_type = (col.get("type") or "").strip()
    if not a_type or not c_type or schema_dna.norm(a_type) == schema_dna.norm(c_type):
        return None
    cost = cast_cost(c_type, a_type)  # estate column value → the spec's declared type
    if cost in (CastCost.none, CastCost.incompatible):
        return None
    return {"needed": True, "from": c_type, "to": a_type, "cost": cost.value}


def _evidence_quality(attr: fspec.SpecAttribute, col_description: str) -> dict[str, Any]:
    """Per-assignment description-quality signal: whether the estate column and the
    spec attribute each carried a real description, and whether the match therefore
    rests on names alone (``name_only``) — the honest low-confidence flag surfaced in
    the report + UI so a weak-description match isn't read as authoritative."""
    col_has = bool((col_description or "").strip())
    attr_has = bool((attr.description or "").strip())
    return {
        "column_described": col_has,
        "attribute_described": attr_has,
        "name_only": not (col_has or attr_has),
    }


def _description_coverage(
    datasets: list[dict], desc_source_map: dict[tuple[str, str], str],
) -> dict[str, Any]:
    """Per-schema description-quality visibility for the run summary: how many of
    each in-scope schema's columns carry a real description, the schema-description
    provenance (``llm``|``fallback``|``missing``), and a ``reenrich_recommended``
    flag for a schema whose enrichment silently produced nothing (e.g. an LLM group
    that returned no output → 0 described columns) so a weak-description drag on
    matching is surfaced rather than invisible."""
    by_key: dict[tuple[str, str], dict[str, int]] = {}
    for d in datasets:
        key = (d.get("database", ""), d.get("schema", ""))
        entry = by_key.setdefault(key, {"columns_total": 0, "columns_described": 0})
        for c in d.get("columns", []) or []:
            entry["columns_total"] += 1
            if (c.get("description") or "").strip():
                entry["columns_described"] += 1
    schemas: list[dict[str, Any]] = []
    weak: list[str] = []
    for key in sorted(by_key):
        db, sch = key
        e = by_key[key]
        total, described = e["columns_total"], e["columns_described"]
        ratio = (described / total) if total else 0.0
        src = desc_source_map.get(key) or "missing"
        # A schema with columns but ZERO described ones is the tell-tale of a failed
        # enrichment group (the LLM returned nothing) — provenance-independent, so a
        # scope-disabled run (no source map) still surfaces it correctly.
        reenrich = total > 0 and described == 0
        loc = ".".join(p for p in (db, sch) if p) or sch
        schemas.append({
            "database": db, "schema": sch, "columns_total": total,
            "columns_described": described, "coverage": round(ratio, 3),
            "source": src, "reenrich_recommended": reenrich,
        })
        if reenrich or ratio < 0.5:
            weak.append(loc)
    return {"schemas": schemas, "weak_schemas": weak,
            "schema_count": len(schemas), "weak_count": len(weak)}


def _resolve_pins(
    attrs: list[fspec.SpecAttribute], flat_cols: list[dict],
    pinned_matches: Optional[dict[str, dict]],
) -> tuple[dict[int, int], set[int]]:
    """Resolve a Phase-2 matcher decision map (``attr_id → {decision, table, column}``)
    to ``({attr_index: col_index}, {forced_gap_attr_index})`` against the concrete
    flattened columns. A ``match`` whose (table, column) doesn't exist verbatim is
    dropped (belt-and-suspenders — the validator already exact-refs); a ``gap`` forces
    the attribute unmatched."""
    pin_col: dict[int, int] = {}
    forced_gap: set[int] = set()
    if not pinned_matches:
        return pin_col, forced_gap
    tblcol_to_j: dict[tuple[str, str], int] = {}
    for j, fc in enumerate(flat_cols):
        tblcol_to_j.setdefault((fc.get("dataset_table", ""), fc.get("name", "")), j)
    for aid, pin in pinned_matches.items():
        i = _attr_index_for_id(attrs, aid)
        if i is None or not isinstance(pin, dict):
            continue
        if str(pin.get("decision")) == "gap":
            forced_gap.add(i)
            continue
        j = tblcol_to_j.get((pin.get("table", ""), pin.get("column", "")))
        if j is not None:
            pin_col[i] = j
    return pin_col, forced_gap


def _raw_evidence(spec: fspec.FeasibilitySpec, datasets: list[dict],
                  extra_proposals: Optional[list] = None,
                  pinned_matches: Optional[dict[str, dict]] = None) -> dict:
    """Assemble-from-raw evidence: assign spec attrs across the (pre-filtered)
    estate datasets with **entity/authority-aware** ranking + **composite
    derivations**, in ONE candidate-merge reconciliation, then confirm the used
    datasets are joinable.

    Beyond the column's own semantic match, each *direct* candidate is ranked by an
    ``adjusted`` score that blends in the affinity of its TABLE to the attribute's
    entity (``TABLE_AFFINITY_WEIGHT``) and demotes inferred FK-carrier columns
    (``FK_CARRIER_PENALTY``). R3 adds *composite* candidates — a spec attribute may
    be satisfied by composing ≥2 columns of the SAME table per a curated
    :class:`feasibility_derivations.DerivationPattern` (or a validated run-scoped
    LLM ``ProposedDerivation`` in ``extra_proposals``, gaps-only). Direct and
    composite candidates are merged and reconciled in a **single greedy pass**: a
    direct winner consumes its physical column (1:1), a composite winner consumes
    NONE (component columns stay reusable). The ``−DERIVATION_PENALTY`` IS the
    replacement margin — a composite only supersedes a same-attr direct by
    exceeding it by the penalty; a gap takes a composite uncontested. Composition is
    skipped for key/grain attributes and never satisfies the grain gate.

    Structured ``alternatives`` (ranked runners-up; the displaced direct pick is
    retained with reason ``superseded_by_derivation``) ride the result.
    """
    attrs = spec.attributes
    if not datasets:
        # Empty scope (no schema cleared the relevance floor) or an empty estate →
        # zero coverage, no assignment. The verdict layer turns this into a clear
        # 'no relevant schema' absence rather than a spurious best-of-bad match.
        return {
            "required_coverage": 0.0, "total_coverage": 0.0,
            "assignment": [],
            "gaps": [{"attr_id": _attr_id(i, a.name), "spec_attr": a.name,
                      "required": a.required, "is_key": a.is_key, "status": "unknown",
                      "reason": "insufficient_evidence"} for i, a in enumerate(attrs)],
            "attribute_status": {_attr_id(i, a.name): "unknown" for i, a in enumerate(attrs)},
            "alternatives": {},
            "datasets": [],
            "join_plan": {"joinable": False, "single_dataset": False, "paths": [], "missing": []},
            "scoring_version": SCORING_VERSION,
        }

    all_table_names = {(d.get("table") or "").lower() for d in datasets}
    table_desc = {(d.get("table") or ""): (d.get("description") or "").strip() for d in datasets}

    # Flatten columns with provenance + FK-carrier inference.
    flat_cols: list[dict] = []
    for d in datasets:
        tbl = d.get("table", "")
        for c in d.get("columns", []):
            col_name = c.get("name", "")
            fc: dict = {
                "name": col_name, "type": c.get("data_type", ""),
                # Align the estate ColumnFeature with schema_dna.estate_column_embed_text:
                # description carries the column's description, concept stays empty, so
                # semantic_text() == "{name}. {description}" — byte-identical to the text
                # embedded at enrichment time. That's what lets the stored vector (below)
                # hit the scorer's embed cache instead of being recomputed. (Deliberately
                # drops the old duplicated-name text `desc or name` — a minor scoring tweak.)
                "description": c.get("description") or "",
                "concept": "",
                "embedding": c.get("embedding"),
                "dataset_uri": d["uri"], "dataset_table": tbl,
                "dataset_schema": d.get("schema", ""),
                "dataset_database": d.get("database", ""),
                "dataset_platform": d.get("platform", ""),
            }
            fc.update(_infer_fk_carrier(col_name, tbl, all_table_names))
            flat_cols.append(fc)

    src = _spec_features(attrs)
    tgt = [schema_dna.column_from_dict(c) for c in flat_cols]
    # Seed the embed cache with vectors persisted on :EstateColumn at enrichment
    # time (estate.read_estate_datasets carries them) so the estate (target) side
    # isn't re-embedded during a run — only the small spec-attribute set is. Keyed
    # by semantic_text(), which the alignment above makes equal to the stored text.
    estate_seed: dict[str, list[float]] = {}
    for fc, tf in zip(flat_cols, tgt):
        vec = fc.get("embedding")
        if vec:
            estate_seed[tf.semantic_text()] = vec
    matrix = schema_dna.score_matrix(src, tgt, embed_cache=estate_seed or None)
    cells = matrix["cells"]
    threshold = matrix["match_threshold"]

    # One embed batch for the affinity texts: attribute-entity texts + table texts
    # + a weak product/domain prior. (score_matrix already cached the column pairs.)
    unique_tables = sorted({d.get("table", "") for d in datasets})
    table_texts = {t: _table_text(t, table_desc.get(t, "")) for t in unique_tables}
    attr_texts = {i: _attr_affinity_text(a) for i, a in enumerate(attrs)}
    prior_text = ". ".join(p for p in (spec.name or "", spec.domain or "") if p)
    aff_cache = _text_embed_cache(list(table_texts.values()) + list(attr_texts.values()) + [prior_text])
    prior_by_table = {t: schema_dna.text_similarity(prior_text, table_texts[t], aff_cache)
                      for t in unique_tables}
    # affinity[(i, table)] in 0..100 — 85% attribute-entity, 15% weak product prior.
    affinity: dict[tuple[int, str], float] = {}
    for i in range(len(attrs)):
        at = attr_texts[i]
        for t in unique_tables:
            a_sim = schema_dna.text_similarity(at, table_texts[t], aff_cache)
            affinity[(i, t)] = 0.85 * a_sim + 0.15 * prior_by_table[t]

    # Tables that carry an above-threshold column for attr i (authority check).
    tables_with_match: dict[int, set[str]] = {i: set() for i in range(len(attrs))}
    for i in range(len(attrs)):
        for j, fc in enumerate(flat_cols):
            if cells[i][j]["overall"] >= threshold:
                tables_with_match[i].add((fc.get("dataset_table") or "").lower())

    def _adjusted(i: int, j: int) -> tuple[float, int, float, bool, bool]:
        col_ov = cells[i][j]["overall"]
        fc = flat_cols[j]
        ta = affinity.get((i, fc.get("dataset_table", "")), 0.0)
        adj = (1 - TABLE_AFFINITY_WEIGHT) * col_ov + TABLE_AFFINITY_WEIGHT * ta
        fk = bool(fc.get("is_fk_carrier"))
        penalized = False
        if fk:
            target = fc.get("fk_target_table")
            if target and target in tables_with_match[i]:
                adj -= FK_CARRIER_PENALTY
                penalized = True
        return adj, col_ov, ta, fk, penalized

    # ── R3: build direct + composite candidates for ONE reconciliation pass ──
    patterns, catalog_error = _load_curated_patterns()
    cols_by_uri: dict[str, list[tuple[int, str, str]]] = {}
    for j, fc in enumerate(flat_cols):
        cols_by_uri.setdefault(fc.get("dataset_uri") or "", []).append(
            (j, fc.get("name", ""), fc.get("type", "")))
    dataset_by_table: dict[str, dict] = {}
    for d in datasets:
        dataset_by_table.setdefault(d.get("table", ""), d)
    # Essential identifiers — composition is skipped for these (it never satisfies a key).
    essential = set(spec.grain.keys or []) | {a.name for a in attrs if a.is_key}

    # Direct candidates: every above-threshold (attr, column) pair — subject to the
    # identifier-shape gate (a category attr matching an `*_id` column, or the
    # reverse, must clear a raised admission margin or it's treated as a gap).
    direct_candidates: list[dict[str, Any]] = []
    best_direct: dict[int, tuple[float, int]] = {}
    ident_gated: dict[tuple[int, int], bool] = {}
    for i in range(len(attrs)):
        for j in range(len(flat_cols)):
            col_ov = cells[i][j]["overall"]
            if col_ov < threshold:
                continue
            if _identifier_shape_mismatch(attrs[i], flat_cols[j].get("name", "")) \
                    and col_ov < threshold + IDENT_MISMATCH_MARGIN:
                ident_gated[(i, j)] = True
                continue
            adj, _cov, _ta, _fk, _pen = _adjusted(i, j)
            direct_candidates.append({
                "attr": i, "kind": "direct", "adjusted": adj, "tiebreak": float(col_ov),
                "kind_rank": 0, "pattern_id": "", "col": j,
                "sort_ref": flat_cols[j].get("dataset_uri") or "",
                "sort_cols": flat_cols[j].get("name") or "",
            })
            if i not in best_direct or adj > best_direct[i][0]:
                best_direct[i] = (adj, j)

    # Composite candidates from the curated catalog (same-table only; non-key attrs).
    composite_candidates: list[dict[str, Any]] = []
    if patterns:
        for i, attr in enumerate(attrs):
            if attr.is_key or attr.name in essential:
                continue
            matched = [p for p in patterns if p.matches_attribute(attr.name, attr.concept)]
            if not matched:
                continue
            # The best same-attr direct's table affinity — the composite penalty is
            # dropped when the composite sits on a clearly-more-relevant table (a
            # wrong-entity direct like `suppliers.name` for a customer attribute).
            bd = best_direct.get(i)
            a_direct = affinity.get((i, (flat_cols[bd[1]].get("dataset_table") or "")), 0.0) if bd else 0.0
            elig = _eligible_composite_tables(i, unique_tables, affinity, COMPOSITE_TABLE_AFFINITY_FLOOR)
            for p in matched:
                roles = [(c.role, c.aliases, c.type_family) for c in p.components]
                for tname in elig:
                    d = dataset_by_table.get(tname)
                    if d is None:
                        continue
                    resolved = _resolve_components(roles, cols_by_uri.get(d.get("uri") or "", []))
                    if resolved is None:
                        continue
                    a_c = affinity.get((i, tname), 0.0)
                    composite_candidates.append(_build_composite_candidate(
                        i, p.id, p.kind.value, p.operator, p.separator, d, resolved,
                        a_c, source="curated", penalty=_composite_penalty(a_c, a_direct)))

    # Composite candidates from validated LLM proposals (gaps-only: no direct candidate).
    for prop in (extra_proposals or []):
        if getattr(prop, "spec_id", None) != spec.spec_id:
            continue
        i = _attr_index_for_id(attrs, getattr(prop, "attribute_id", ""))
        if i is None or i in best_direct or attrs[i].is_key or attrs[i].name in essential:
            continue
        d = dataset_by_table.get(prop.table_ref)
        if d is None:
            continue
        roles = [(c.get("role", f"c{k}"), [c.get("column_ref", "")], "")
                 for k, c in enumerate(prop.components)]
        resolved = _resolve_components(roles, cols_by_uri.get(d.get("uri") or "", []))
        if resolved is None:
            continue
        a_c = affinity.get((i, prop.table_ref), 0.0)
        # Gaps-only (i not in best_direct) → best-direct affinity is 0 → penalty drops.
        composite_candidates.append(_build_composite_candidate(
            i, prop.stable_id, prop.kind.value, prop.operator, prop.separator, d, resolved,
            a_c, source="advisor", penalty=_composite_penalty(a_c, 0.0)))

    # Single greedy reconciliation over the merged candidate set. A direct winner
    # consumes its physical column (1:1); a composite winner consumes NONE (its
    # component columns stay reusable — a freed column is reconsidered in the SAME
    # pass for any other attribute's direct candidate). Fully deterministic sort.
    merged = direct_candidates + composite_candidates
    merged.sort(key=lambda c: (-c["adjusted"], -c["tiebreak"], c["kind_rank"],
                               c["pattern_id"], c["sort_ref"], c["sort_cols"]))

    # Phase 2 reasoning-matcher pins: a validated LLM decision either forces an
    # attribute onto a specific (table, column) — consuming that column 1:1 like any
    # direct winner — or forces it to a gap. Pins are seated FIRST so the greedy pass
    # fills only the un-pinned attributes over the remaining columns (deterministic
    # result fills the rest); a pinned column can't be re-taken.
    pin_col_by_attr, forced_gap = _resolve_pins(attrs, flat_cols, pinned_matches)
    winners: dict[int, dict] = {}
    taken_tgt: set[int] = set()
    for i, j in pin_col_by_attr.items():
        if j in taken_tgt:
            continue
        adj_p, _cov_p, _ta_p, _fk_p, _pen_p = _adjusted(i, j)
        winners[i] = {"attr": i, "kind": "direct", "col": j, "adjusted": adj_p,
                      "tiebreak": float(cells[i][j]["overall"]), "pinned": True}
        taken_tgt.add(j)
    for cand in merged:
        i = cand["attr"]
        if i in winners or i in forced_gap:
            continue
        if cand["kind"] == "direct":
            if cand["col"] in taken_tgt:
                continue
            winners[i] = cand
            taken_tgt.add(cand["col"])
        else:
            winners[i] = cand  # composite: consumes no physical column

    # Build assignment rows (attr order) from the ONE reconciled result.
    assignment: list[dict[str, Any]] = []
    for i, attr in enumerate(attrs):
        w = winners.get(i)
        if w is None:
            continue
        aid = _attr_id(i, attr.name)
        if w["kind"] == "direct":
            j = w["col"]
            col = flat_cols[j]
            adj2, col_ov2, ta, fk, _pen = _adjusted(i, j)
            deriv = _infer_derivation(attr, col, cells[i][j]["axes"])
            eq = _evidence_quality(attr, col.get("description", ""))
            assignment.append({
                "attr_id": aid, "spec_attr": attr.name, "required": attr.required,
                "is_key": attr.is_key, "column": col.get("name", ""),
                "dataset_uri": col.get("dataset_uri"), "dataset_table": col.get("dataset_table"),
                "dataset_schema": col.get("dataset_schema", ""),
                "dataset_database": col.get("dataset_database", ""),
                "dataset_platform": col.get("dataset_platform", ""),
                "score": col_ov2, "column_semantic": col_ov2,
                "table_affinity": round(ta, 1), "adjusted": round(adj2, 1),
                "fk_role": fk, "is_fk_carrier": fk, "fk_target_table": col.get("fk_target_table"),
                "axes": cells[i][j]["axes"], "derivation": deriv,
                "status": "derivable" if deriv else "direct_match", "match_kind": "direct",
                "evidence_quality": eq, "name_only": eq["name_only"],
                "cast_hint": _cast_hint(attr, col),
                **({"matcher_pinned": True} if w.get("pinned") else {}),
            })
        else:
            comp_cols = [c["col_name"] for c in w["components"]]
            quality = round(0.7 * w["component_min"] + 0.3 * w["component_mean"], 1)
            assignment.append({
                "attr_id": aid, "spec_attr": attr.name, "required": attr.required,
                "is_key": attr.is_key, "column": " + ".join(comp_cols),
                "dataset_uri": w["dataset_uri"], "dataset_table": w["table"],
                "dataset_schema": w["dataset_schema"], "dataset_database": w["dataset_database"],
                "dataset_platform": w["dataset_platform"],
                "score": round(quality), "column_semantic": round(quality),
                "table_affinity": round(w["affinity"], 1), "adjusted": round(w["adjusted"], 1),
                "fk_role": False, "is_fk_carrier": False, "fk_target_table": None,
                "axes": {}, "derivation": w["kind_name"],
                "status": "derivable", "match_kind": "composite",
                "composite": {
                    "kind": w["kind_name"], "operator": w["operator"], "separator": w["separator"],
                    "table": w["table"], "dataset_uri": w["dataset_uri"],
                    "dataset_schema": w["dataset_schema"], "source": w["source"],
                    "components": [{"role": c["role"], "column": c["col_name"], "score": c["score"]}
                                   for c in w["components"]],
                },
            })

    # Structured alternatives per attribute (top-K by adjusted). The chosen DIRECT
    # column is 'chosen'; when a COMPOSITE won, the displaced best-direct pick is
    # retained with reason 'superseded_by_derivation' so the demotion is visible.
    alternatives: dict[str, list[dict]] = {}
    for i, attr in enumerate(attrs):
        w = winners.get(i)
        chosen_col = w["col"] if (w and w["kind"] == "direct") else None
        superseded_col = best_direct[i][1] if (w and w["kind"] == "composite" and i in best_direct) else None
        cand: list[dict] = []
        for j, fc in enumerate(flat_cols):
            adj, col_ov, ta, fk, pen = _adjusted(i, j)
            is_chosen = chosen_col == j
            if is_chosen:
                reason = "chosen"
            elif j == superseded_col:
                reason = "superseded_by_derivation"
            else:
                reason = _alt_reason(col_ov, threshold, fk, pen, cells[i][j]["axes"],
                                     chosen=False, ident_gated=ident_gated.get((i, j), False))
            cand.append({
                "column": fc.get("name", ""), "schema": fc.get("dataset_schema", ""),
                "table": fc.get("dataset_table", ""), "dataset_uri": fc.get("dataset_uri"),
                "column_score": col_ov, "table_affinity": round(ta, 1),
                "adjusted": round(adj, 1), "is_fk_carrier": fk, "chosen": is_chosen,
                "reason": reason,
                # The generated prose the Assembly workspace shows so a remap is
                # *informed* (bounded to keep the drill-down payload small).
                "column_description": (fc.get("description") or "")[:300],
                "table_description": (table_desc.get(fc.get("dataset_table", "")) or "")[:300],
            })
        cand.sort(key=lambda c: (not c["chosen"], -c["adjusted"], c["column"]))
        top = cand[:MAX_ALTERNATIVES]
        if chosen_col is not None and not any(c["chosen"] for c in top):
            top = top[:max(0, MAX_ALTERNATIVES - 1)] + [next(c for c in cand if c["chosen"])]
        if superseded_col is not None and not any(c["reason"] == "superseded_by_derivation" for c in top):
            sup = next((c for c in cand if c["reason"] == "superseded_by_derivation"), None)
            if sup is not None:
                top = top[:max(0, MAX_ALTERNATIVES - 1)] + [sup]
        alternatives[_attr_id(i, attr.name)] = top

    # Gaps (+ per-attr status) for the unmatched attributes.
    attribute_status: dict[str, str] = {}
    gaps: list[dict[str, Any]] = []
    for i, attr in enumerate(attrs):
        aid = _attr_id(i, attr.name)
        if i in winners:
            row = next(r for r in assignment if r["attr_id"] == aid)
            attribute_status[aid] = row["status"]
        else:
            attribute_status[aid] = "missing"
            alts = alternatives.get(aid, [])
            gaps.append({
                "attr_id": aid, "spec_attr": attr.name, "required": attr.required,
                "is_key": attr.is_key, "status": "missing",
                "reason": alts[0]["reason"] if alts else "insufficient_evidence",
            })

    req_total = sum(1 for a in attrs if a.required)
    req_covered = sum(1 for r in assignment if r["required"])
    used_uris = {r["dataset_uri"] for r in assignment if r.get("dataset_uri")}
    used = [d for d in datasets if d["uri"] in used_uris]
    return {
        "required_coverage": round((req_covered / req_total) if req_total else 1.0, 3),
        "total_coverage": round((len(assignment) / len(attrs)) if attrs else 0.0, 3),
        "assignment": assignment,
        "gaps": gaps,
        "attribute_status": attribute_status,
        "alternatives": alternatives,
        "datasets": [{"uri": d["uri"], "table": d.get("table", ""),
                      "schema": d.get("schema", "")} for d in used],
        "join_plan": joinability(used),
        "scoring_version": SCORING_VERSION,
        **({"derivation_catalog_error": catalog_error} if catalog_error else {}),
    }


def _annotate_shortlist(shortlist: list[dict], spec: fspec.FeasibilitySpec, raw: dict) -> None:
    """Enrich each shortlisted-schema row with matched-tables + coverage reason
    codes (from the scoped assignment) and a deterministic prose ``rationale`` — so
    the UI + report explain WHY a schema scored the way it did, consistently."""
    assignment = raw.get("assignment", [])
    req_total = len(spec.required_attributes)
    req_matched = sum(1 for r in assignment if r.get("required"))
    unmatched_required = max(0, req_total - req_matched)
    req_cov = round(req_matched / req_total, 3) if req_total else 1.0
    tables_by_key: dict[tuple[str, str], set[str]] = {}
    attrcount_by_key: dict[tuple[str, str], int] = {}
    for r in assignment:
        key = (r.get("dataset_database", ""), r.get("dataset_schema", ""))
        tables_by_key.setdefault(key, set()).add(r.get("dataset_table", ""))
        attrcount_by_key[key] = attrcount_by_key.get(key, 0) + 1
    for row in shortlist:
        key = (row.get("database", ""), row.get("schema", ""))
        src = row.get("description_source", "none")
        desc_phrase = {"llm": "enrichment-generated", "fallback": "synthesized from tables"}.get(src, "none")
        if row.get("included"):
            mt = sorted(t for t in tables_by_key.get(key, set()) if t)
            n_attrs = attrcount_by_key.get(key, 0)
            row["matched_tables"] = mt
            row["matched_attr_count"] = n_attrs
            row["required_candidate_coverage"] = req_cov
            row["unmatched_required_count"] = unmatched_required
            dist = row.get("threshold_distance", 0)
            sign = f"+{dist} over floor" if dist >= 0 else f"{dist} below floor"
            via = ", ".join(mt) if mt else "no in-scope table"
            row["rationale"] = (
                f"Shortlisted {row.get('score', 0)}/100 ({sign}): matched "
                f"{n_attrs} attribute(s) via {via}; {req_matched}/{req_total} required "
                f"have candidates, {unmatched_required} required unmatched; "
                f"description: {desc_phrase}."
            )
            if row.get("fallback"):
                row["rationale"] += (
                    " Best available schema (below the confident threshold) — low confidence."
                )
        else:
            reason = row.get("excluded_reason", "below_floor")
            if reason == "over_cap":
                row["rationale"] = (
                    f"Excluded (over max-schemas cap): scored {row.get('score', 0)}/100 "
                    f"but a more-relevant schema filled the cap; description: {desc_phrase}."
                )
            elif reason == "below_threshold":
                row["rationale"] = (
                    f"Excluded (below shortlist threshold): scored {row.get('score', 0)}/100 "
                    f"— clears the floor but isn't confidently relevant; description: {desc_phrase}."
                )
            else:
                row["rationale"] = (
                    f"Excluded (below relevance floor): scored {row.get('score', 0)}/100; "
                    f"description: {desc_phrase}."
                )


def build_spec_evidence(spec: fspec.FeasibilitySpec, products: list[dict],
                        datasets: list[dict],
                        schema_shortlist: Optional[list[dict]] = None,
                        extra_proposals: Optional[list] = None,
                        pinned_matches: Optional[dict[str, dict]] = None) -> dict[str, Any]:
    """Build the per-spec evidence bundle.

    When ``schema_shortlist`` is provided (Stage 2 of the tiered flow), the raw
    estate pool is restricted to the shortlisted (``included``) schemas' datasets
    before the column assignment runs — so an employee-data spec can't collect
    false-positive columns from an unrelated sales schema. The full shortlist
    (with near-miss scores + reason codes) rides the evidence so the UI can explain
    the scope. When ``schema_shortlist`` is ``None`` the whole-estate behavior is
    preserved. ``extra_proposals`` are validated run-scoped :class:`ProposedDerivation`s
    threaded into the composite reconciliation (gaps-only re-pass).
    """
    prod = _product_evidence(spec, products)
    scope_empty = False
    if schema_shortlist is not None:
        included_keys = {(r.get("database", ""), r.get("schema", ""))
                         for r in schema_shortlist if r.get("included")}
        scoped = [d for d in datasets
                  if (d.get("database", ""), d.get("schema", "")) in included_keys]
        raw = _raw_evidence(spec, scoped, extra_proposals=extra_proposals,
                            pinned_matches=pinned_matches)
        scope_empty = not included_keys
        _annotate_shortlist(schema_shortlist, spec, raw)
    else:
        raw = _raw_evidence(spec, datasets, extra_proposals=extra_proposals,
                            pinned_matches=pinned_matches)
    ev = {
        "spec_id": spec.spec_id, "name": spec.name, "domain": spec.domain,
        "product_kind": spec.product_kind.value,
        "required_attribute_count": len(spec.required_attributes),
        "total_attribute_count": len(spec.attributes),
        "grain_keys": spec.grain.keys, "freshness": spec.freshness.history.value,
        "product_candidates": prod[:3],
        "raw_candidates": raw,
    }
    if schema_shortlist is not None:
        ev["schema_shortlist"] = schema_shortlist
        ev["schema_scope_empty"] = scope_empty
    return ev


# ── deterministic heuristic tier (the invariant CEILING) ──────────────────────

def heuristic_verdict(ev: dict, scan_state: str,
                      spec: Optional[fspec.FeasibilitySpec] = None) -> dict[str, Any]:
    """Coarse, deterministic tier from the evidence — the fallback AND the ceiling
    the skill's verdict is clamped to. Never returns a tier greener than the
    evidence supports.

    When ``spec`` is supplied, a **grain-key hard gate** (F-lite) caps the tier to
    ``absent`` if any grain key / required identifier attribute is unmatched — an
    essential-identifier gap can't be averaged away by many optional matches. When
    ``spec`` is ``None`` (unit callers) the gate is skipped."""
    prod = ev.get("product_candidates") or []
    raw = ev.get("raw_candidates") or {}
    best_prod = prod[0] if prod else None
    prod_req = best_prod["required_coverage"] if best_prod else 0.0
    raw_req = raw.get("required_coverage", 0.0)
    raw_join = (raw.get("join_plan") or {}).get("joinable", False)

    has_any_evidence = bool(best_prod) or (raw.get("assignment"))
    tier = "absent"
    if best_prod and prod_req >= FLOOR_READY and _no_material_derivation(best_prod):
        tier = "ready"
    elif best_prod and prod_req >= FLOOR_ADAPTABLE:
        tier = "adaptable"
    elif raw_req >= FLOOR_ASSEMBLABLE and raw_join:
        tier = "assemblable"
    else:
        tier = "absent"

    # Evaluation state: a gap/partial scan must NEVER present a bare 'absent'.
    eval_state = "completed"
    if scan_state == "failed":
        eval_state = "failed"
    elif scan_state == "partial":
        eval_state = "partial"
    if tier == "absent" and scan_state != "completed" and not has_any_evidence:
        eval_state = "insufficient_evidence"

    # Empty schema scope (Stage 1): no schema in the estate cleared the relevance
    # floor. The raw/assemblable path is already zeroed; only override to a clear
    # 'no relevant schema' absence when NO governed product covers the spec either
    # (a product match is independent of the estate's schemas and still wins).
    if ev.get("schema_scope_empty") and tier == "absent":
        floor = ev.get("schema_relevance_floor", SCHEMA_RELEVANCE_FLOOR)
        return {
            "tier": "absent",
            "evaluation_state": eval_state,
            "confidence": 0.1,
            "required_coverage": 0.0,
            "total_coverage": 0.0,
            "best_product": None,
            "matched": [],
            "gaps": raw.get("gaps", []),
            "derivations": [],
            "join_plan": None,
            "adaptation_notes": "",
            "rationale": (
                f"No schema in the estate cleared the relevance floor ({floor}) for this "
                f"product; lower the schema-relevance lever and re-run, or enrich the estate "
                f"for better schema descriptions."
            ),
        }

    confidence = _confidence(tier, prod_req, raw_req, best_prod, raw)
    result = {
        "tier": tier,
        "evaluation_state": eval_state,
        "confidence": round(confidence, 3),
        "required_coverage": round(prod_req if tier in ("ready", "adaptable") else raw_req, 3),
        "total_coverage": round(
            (best_prod["total_coverage"] if best_prod else 0.0)
            if tier in ("ready", "adaptable") else raw.get("total_coverage", 0.0), 3),
        "best_product": {"uri": best_prod["uri"], "version": best_prod["version"]} if best_prod and tier in ("ready", "adaptable") else None,
        "matched": (best_prod["assignment"] if best_prod and tier in ("ready", "adaptable")
                    else raw.get("assignment", [])),
        "gaps": (best_prod["gaps"] if best_prod and tier in ("ready", "adaptable")
                 else raw.get("gaps", [])),
        "derivations": _collect_derivations(best_prod if tier in ("ready", "adaptable") else raw),
        "join_plan": raw.get("join_plan") if tier == "assemblable" else None,
        "adaptation_notes": _adaptation_notes(tier, best_prod, raw),
        "rationale": _heuristic_rationale(tier, ev, best_prod, raw_req, raw_join, eval_state),
    }
    _apply_grain_gate(result, spec)
    return result


def _apply_grain_gate(result: dict, spec: Optional[fspec.FeasibilitySpec]) -> None:
    """Cap a 'buildable' tier to ``absent`` when an essential identifier (grain key
    or required ``is_key`` attribute) is unmatched. Mutates ``result`` in place;
    matched/gaps/best_product stay so the drill-down still shows the near-match."""
    if spec is None or result.get("tier") not in ("ready", "adaptable", "assemblable"):
        return
    essential = set(spec.grain.keys or []) | {a.name for a in spec.attributes if a.is_key and a.required}
    if not essential:
        return
    # Only a DIRECT column match satisfies an essential identifier — a composite
    # (derived) match lifts coverage but never satisfies a grain/key (R3). Legacy
    # rows without ``match_kind`` are treated as direct (back-compat).
    matched_names = {r.get("spec_attr") for r in result.get("matched", [])
                     if r.get("match_kind", "direct") == "direct"}
    unmatched = sorted(n for n in essential if n and n not in matched_names)
    if not unmatched:
        return
    prior_tier = result["tier"]
    result["tier"] = "absent"
    result["grain_gate"] = {"unmatched_essential": unmatched, "capped_from": prior_tier}
    result["confidence"] = min(result.get("confidence", 0.0), 0.2)
    result["rationale"] = (
        f"Capped to absent (was {prior_tier}): essential identifier(s) "
        f"{', '.join(unmatched)} are unmatched — a grain-key gap can't be averaged "
        f"away by optional matches. The near-match evidence is retained below."
    )


def _no_material_derivation(cand: dict) -> bool:
    return not any(r.get("derivation") for r in cand.get("assignment", []))


def _collect_derivations(cand: Optional[dict]) -> list[dict]:
    if not cand:
        return []
    return [
        {"spec_attr": r["spec_attr"], "kind": r["derivation"], "column": r["column"]}
        for r in cand.get("assignment", []) if r.get("derivation")
    ]


def _confidence(tier, prod_req, raw_req, best_prod, raw) -> float:
    if tier in ("ready", "adaptable"):
        base = prod_req
        return min(1.0, 0.5 * base + 0.5 * (best_prod["total_coverage"] if best_prod else 0.0))
    if tier == "assemblable":
        return min(1.0, 0.5 * raw_req + 0.5 * raw.get("total_coverage", 0.0))
    return 0.1


def _adaptation_notes(tier, best_prod, raw) -> str:
    if tier == "adaptable" and best_prod:
        derivs = _collect_derivations(best_prod)
        if derivs:
            kinds = ", ".join(sorted({d["kind"] for d in derivs}))
            return f"Adapt the '{best_prod['name']}' product via: {kinds}."
        return f"Adapt the '{best_prod['name']}' product to cover the remaining attributes."
    if tier == "assemblable":
        n = len((raw or {}).get("datasets", []))
        return f"Assemble from {n} estate dataset(s) into a governed product."
    return ""


def _heuristic_rationale(tier, ev, best_prod, raw_req, raw_join, eval_state) -> str:
    if eval_state == "insufficient_evidence":
        return "The estate scan was incomplete, so buildability cannot be confirmed — rescan before treating this as absent."
    if tier == "ready" and best_prod:
        return f"The published product '{best_prod['name']}' already covers {round(best_prod['required_coverage']*100)}% of required attributes with no material adaptation."
    if tier == "adaptable" and best_prod:
        return f"'{best_prod['name']}' covers {round(best_prod['required_coverage']*100)}% of required attributes; a bounded, allowed adaptation closes the gap."
    if tier == "assemblable":
        raw = ev.get("raw_candidates") or {}
        n = len(raw.get("datasets", []))
        join = "joinable on shared keys" if raw_join else "with an unconfirmed join"
        return f"Raw data across {n} estate dataset(s) covers {round(raw_req*100)}% of required attributes ({join}); not a governed product yet."
    return "No published product or estate raw data covers this spec's required attributes."


# ── skill invocation (tool-less, batched per domain) ──────────────────────────

_JSON_ARRAY_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_SKILL_PATH = SKILLS_DIR / SKILL_NAME / "SKILL.md"
_skill_body_cache: dict[str, str] = {}


def _skill_body_for(skill_name: str) -> str:
    """The frontmatter-stripped SKILL.md body for ``skill_name`` (cached). Raised as
    the tool-less system prompt for a pure reason-over-JSON skill call."""
    if skill_name not in _skill_body_cache:
        text = (SKILLS_DIR / skill_name / "SKILL.md").read_text(encoding="utf-8")
        _skill_body_cache[skill_name] = _FRONTMATTER_RE.sub("", text, count=1).strip()
    return _skill_body_cache[skill_name]


def _skill_body() -> str:
    return _skill_body_for(SKILL_NAME)


def skill_version() -> str:
    """A content hash of the SKILL.md body — stamped on every run for audit."""
    import hashlib
    try:
        return hashlib.sha256(_skill_body().encode("utf-8")).hexdigest()[:12]
    except OSError:
        return "unavailable"


def _extract_json_array(text: str) -> Optional[list]:
    matches = _JSON_ARRAY_RE.findall(text or "")
    candidate = matches[-1] if matches else None
    if candidate is None:
        # Tolerate a bare array with no fence.
        stripped = (text or "").strip()
        candidate = stripped if stripped.startswith("[") else None
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _extract_json_object(text: str) -> Optional[dict]:
    matches = _JSON_OBJECT_RE.findall(text or "")
    candidate = matches[-1] if matches else None
    if candidate is None:
        stripped = (text or "").strip()
        candidate = stripped if stripped.startswith("{") else None
    if not candidate:
        return None
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _sanitize_bundle(bundle: dict) -> dict:
    """Untrusted-input hygiene: the evidence is derived from DB object names, which
    could carry injected instructions. We only ever send NAMES + numbers (never DB
    comments/descriptions) and JSON-encode the whole bundle so it reaches the model
    strictly as data, mirroring the intake tool-less-parser discipline."""
    return bundle


async def _run_evaluator_skill(bundle: dict) -> Optional[list]:
    """One tool-less SDK call over a domain batch's evidence bundle.

    Isolated exactly like ``intake_parser``: allowed_tools=[], no plugins, no
    skills, the SKILL.md body as the system prompt. Returns the parsed verdict
    array or None (→ deterministic fallback)."""
    try:
        from claude_agent_sdk import (  # type: ignore
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError:
        return None

    from .config import BASE_DIR

    try:
        instructions = _skill_body()
    except OSError:
        return None

    options = ClaudeAgentOptions(
        allowed_tools=[], permission_mode="default", cwd=str(BASE_DIR),
        max_turns=1, system_prompt=instructions,
    )
    prompt = "EVIDENCE BUNDLE:\n```json\n" + json.dumps(_sanitize_bundle(bundle), default=str) + "\n```"
    transcript: list[str] = []
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
            elif isinstance(message, ResultMessage):
                try:
                    from . import llm_usage
                    llm_usage.record_usage(
                        source="feasibility_evaluator",
                        usage=llm_usage.extract_usage(message),
                    )
                except Exception:
                    pass
                if getattr(message, "is_error", False):
                    return None
    except Exception as exc:
        logger.warning("feasibility evaluator skill failed: %s", exc)
        return None
    return _extract_json_array("".join(transcript))


# ── R3: composite-derivation LLM advisor (gaps-only, exact-ref) ───────────────

_ADVISOR_MAX_TABLES = 12
_ADVISOR_MAX_COLS = 60


def _sdk_present() -> bool:
    """True iff the Claude Agent SDK is importable (so the advisor can be attempted;
    absent → the deterministic curated catalog is the only composition source)."""
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except ImportError:
        return False


def _scoped_datasets_for_spec(ev: dict, datasets: list[dict]) -> list[dict]:
    """The datasets in a spec's in-scope schema shortlist (else the whole estate) —
    the bounded candidate-table pool the advisor is shown for THAT spec."""
    shortlist = ev.get("schema_shortlist")
    if not shortlist:
        return datasets
    included = {(r.get("database", ""), r.get("schema", "")) for r in shortlist if r.get("included")}
    return [d for d in datasets if (d.get("database", ""), d.get("schema", "")) in included]


def _derivation_advisor_spec_input(
    spec: fspec.FeasibilitySpec, ev: dict, scoped: list[dict], unmatched_req_ids: set[str],
) -> Optional[dict[str, Any]]:
    """Shape the advisor input for ONE spec: still-unmatched required attributes +
    the bounded, scoped candidate tables (top-affinity schemas' datasets)."""
    attrs = spec.attributes
    unmatched: list[dict[str, Any]] = []
    for aid in sorted(unmatched_req_ids):
        i = _attr_index_for_id(attrs, aid)
        if i is None:
            continue
        a = attrs[i]
        unmatched.append({"attribute_id": aid, "name": a.name,
                          "concept": a.concept or a.name, "type": a.type})
    if not unmatched:
        return None
    tables: list[dict[str, Any]] = []
    for d in scoped[:_ADVISOR_MAX_TABLES]:
        cols = [{"name": c.get("name", ""), "type": c.get("data_type", "")}
                for c in (d.get("columns") or [])][:_ADVISOR_MAX_COLS]
        if cols:
            tables.append({"table_ref": d.get("table", ""), "columns": cols})
    if not tables:
        return None
    return {"spec_id": spec.spec_id, "unmatched_attributes": unmatched, "candidate_tables": tables}


def _validate_proposals(
    raw_list: list, spec: fspec.FeasibilitySpec, scoped: list[dict],
    unmatched_req_ids: set[str], audit: dict,
) -> list:
    """Exact-ref validate the LLM's proposals for ONE spec, tallying reject reasons.

    A proposal is kept only if: it is scoped to THIS spec_id AND its ``attribute_id``
    is one of this spec's unmatched required attrs (else ``wrong_spec``); its
    ``table_ref`` + every ``column_ref`` exist **verbatim** in the spec's supplied
    inventory (a ref in a DIFFERENT table → ``mixed_tables``, a ref in NO table →
    ``unknown_column``, non-distinct columns → ``unknown_column``); and a ``compute``
    kind has at least one numeric/temporal source (else ``type_incompatible``)."""
    rej = audit["rejected_by_reason"]
    cols_by_table: dict[str, set[str]] = {}
    all_cols: set[str] = set()
    type_of: dict[tuple[str, str], str] = {}
    for d in scoped:
        t = d.get("table", "")
        names = {c.get("name", "") for c in (d.get("columns") or [])}
        cols_by_table.setdefault(t, set()).update(names)
        all_cols |= names
        for c in (d.get("columns") or []):
            type_of[(t, c.get("name", ""))] = schema_dna.type_family(c.get("data_type", ""))
    out: list = []
    for raw in raw_list:
        try:
            prop = fderiv.parse_proposal(raw)
        except fderiv.DerivationCatalogError:
            rej["unknown_column"] += 1  # malformed shape → not applicable
            continue
        if prop.spec_id != spec.spec_id or prop.attribute_id not in unmatched_req_ids:
            rej["wrong_spec"] += 1
            continue
        if prop.table_ref not in cols_by_table:
            rej["unknown_column"] += 1
            continue
        refs = [(c.get("column_ref") or "") for c in prop.components]
        if not refs or any(not r for r in refs) or len(set(refs)) != len(refs):
            rej["unknown_column"] += 1
            continue
        table_cols = cols_by_table[prop.table_ref]
        mixed = any(r not in table_cols and r in all_cols for r in refs)
        unknown = any(r not in table_cols and r not in all_cols for r in refs)
        if mixed:
            rej["mixed_tables"] += 1
            continue
        if unknown:
            rej["unknown_column"] += 1
            continue
        if prop.kind == fspec.DerivationKind.compute:
            fams = {type_of.get((prop.table_ref, r), "") for r in refs}
            if not (fams & {"numeric", "temporal"}):
                rej["type_incompatible"] += 1
                continue
        out.append(prop)
    return out


async def _run_derivation_advisor_skill(payload_specs: list[dict]) -> Optional[list]:
    """Invoke the ``data-product-derivation-advisor`` skill over a domain batch's
    affected specs → parsed ``proposed`` list, or None on any failure. Mirrors
    :func:`_run_recommender_skill` (isolated, Skill-tool only)."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock  # type: ignore
    except ImportError:
        return None

    from .config import BASE_DIR, PIPELINE_PLUGINS

    system_prompt = (
        f"FIRST: Load the `{DERIVATION_ADVISOR_SKILL_NAME}` skill via the Skill tool, then follow "
        "its instructions. Emit exactly one fenced JSON code block as the skill instructs. Do not "
        "write files. Do not run shell commands. Do not answer in prose outside the JSON block."
    )
    payload = {"specs": payload_specs}
    user_prompt = (
        f"FIRST: Load the {DERIVATION_ADVISOR_SKILL_NAME} skill using the Skill tool.\n\n"
        f"INPUT (JSON):\n{json.dumps(payload, default=str)}\n\n"
        "Output the single fenced JSON block as instructed by the skill."
    )
    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Skill"], permission_mode="acceptEdits", cwd=str(BASE_DIR),
        max_turns=4, skills="all", plugins=PIPELINE_PLUGINS,
        system_prompt={"type": "preset", "preset": "claude_code", "append": system_prompt},
    )
    transcript: list[str] = []
    try:
        async for message in query(prompt=user_prompt, options=options):
            if message is None:
                continue
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
            elif isinstance(message, ResultMessage):
                try:
                    from . import llm_usage
                    llm_usage.record_usage(
                        source="feasibility_derivation_advisor",
                        usage=llm_usage.extract_usage(message),
                    )
                except Exception:
                    pass
                if getattr(message, "is_error", False):
                    return None
    except Exception as exc:
        logger.warning("derivation advisor skill failed: %s", exc)
        return None
    obj = _extract_json_object("".join(transcript))
    if not isinstance(obj, dict):
        return None
    rows = obj.get("proposed")
    return rows if isinstance(rows, list) else None


# ── R4: reasoning column-matcher (hybrid, uncertain-only, exact-ref) ──────────

def _attr_sort_index(attr_id: str) -> int:
    m = re.match(r"^#(\d+):", attr_id or "")
    return int(m.group(1)) if m else 1_000_000


def _uncertain_attrs(ev: dict) -> set[str]:
    """Select the UNCERTAIN subset of a spec's attributes for the reasoning matcher —
    the cost boundary that keeps confident deterministic matches untouched.

    An attribute is uncertain when: its direct match sits in the gray band
    (``threshold ≤ adjusted < MATCHER_HIGH_CONF``); the match rests on names alone
    (``name_only``); its top runner-up is within ``MATCHER_AMBIGUOUS_MARGIN`` (an
    ambiguous entity, e.g. ``name`` on customers vs suppliers); the identifier gate
    flagged a plausible contender; OR it's a required gap with a near-miss just below
    the threshold (a rescue). Bounded to ``_MATCHER_MAX_ATTRS_PER_SPEC`` per spec."""
    raw = ev.get("raw_candidates") or {}
    assignment = raw.get("assignment") or []
    alternatives = raw.get("alternatives") or {}
    gaps = raw.get("gaps") or []
    threshold = schema_dna.MATCH_THRESHOLD
    uncertain: set[str] = set()

    for row in assignment:
        if row.get("match_kind") != "direct":
            continue  # composites are structurally grounded, not entity-ambiguous
        aid = row.get("attr_id")
        adj = row.get("adjusted", row.get("score", 0)) or 0
        alts = alternatives.get(aid) or []
        if threshold <= adj < MATCHER_HIGH_CONF or row.get("name_only"):
            uncertain.add(aid)
            continue
        runner = next((a for a in alts if not a.get("chosen")
                       and a.get("reason") != "below_threshold"), None)
        if runner is not None and (adj - (runner.get("adjusted") or 0)) <= MATCHER_AMBIGUOUS_MARGIN:
            uncertain.add(aid)
            continue
        if any(a.get("reason") == "identifier_mismatch" for a in alts):
            uncertain.add(aid)

    for g in gaps:
        if not g.get("required"):
            continue
        aid = g.get("attr_id")
        alts = alternatives.get(aid) or []
        if not alts:
            continue
        best = alts[0]
        near_miss = (best.get("reason") == "below_threshold"
                     and (best.get("column_score", 0) or 0) >= threshold - MATCHER_RESCUE_BAND)
        gated = any(a.get("reason") == "identifier_mismatch" for a in alts)
        if near_miss or gated:
            uncertain.add(aid)

    return set(sorted(uncertain, key=_attr_sort_index)[:_MATCHER_MAX_ATTRS_PER_SPEC])


def _matcher_spec_input(
    spec: fspec.FeasibilitySpec, ev: dict, scoped: list[dict], uncertain_ids: set[str],
) -> tuple[Optional[dict[str, Any]], dict[str, list[tuple[str, str]]]]:
    """Shape the reasoning-matcher input for ONE spec: each uncertain attribute with
    its rich signal (name/concept/description/type/required/is_key + its current
    deterministic pick) and its bounded candidate list drawn from the already-computed
    ``alternatives`` — each candidate carrying the estate's GENERATED column + table
    descriptions (the core reasoning signal). Returns ``(payload_or_None, shown)``
    where ``shown`` maps ``attribute_id → [(table, column)]`` for exact-ref validation."""
    raw = ev.get("raw_candidates") or {}
    alternatives = raw.get("alternatives") or {}
    by_aid = {r.get("attr_id"): r for r in (raw.get("assignment") or [])}
    col_desc: dict[tuple[str, str], str] = {}
    tbl_desc: dict[str, str] = {}
    for d in scoped:
        t = d.get("table", "")
        tbl_desc[t] = (d.get("description") or "").strip()
        for c in d.get("columns", []) or []:
            col_desc[(t, c.get("name", ""))] = (c.get("description") or "").strip()

    attrs_out: list[dict[str, Any]] = []
    shown: dict[str, list[tuple[str, str]]] = {}
    for aid in sorted(uncertain_ids, key=_attr_sort_index):
        i = _attr_index_for_id(spec.attributes, aid)
        if i is None:
            continue
        a = spec.attributes[i]
        cur = by_aid.get(aid)
        current: dict[str, Any] = {"decision": "match" if cur else "gap"}
        if cur:
            current.update({"column": cur.get("column", ""),
                            "table": cur.get("dataset_table", ""), "score": cur.get("score", 0)})
        cand_list: list[dict[str, Any]] = []
        pairs: list[tuple[str, str]] = []
        for alt in (alternatives.get(aid) or [])[:_MATCHER_MAX_CANDS]:
            col, tbl = alt.get("column", ""), alt.get("table", "")
            if not col:
                continue
            cand_list.append({
                "column": col, "table": tbl,
                "column_description": col_desc.get((tbl, col), ""),
                "table_description": tbl_desc.get(tbl, ""),
                "score": round(alt.get("adjusted", alt.get("column_score", 0)) or 0),
                "reason": alt.get("reason", ""),
            })
            pairs.append((tbl, col))
        if not cand_list:
            continue
        attrs_out.append({
            "attribute_id": aid, "name": a.name, "concept": a.concept or a.name,
            "description": a.description, "type": a.type,
            "required": a.required, "is_key": a.is_key,
            "current": current, "candidates": cand_list,
        })
        shown[aid] = pairs
    if not attrs_out:
        return None, {}
    return {"spec_id": spec.spec_id, "attributes": attrs_out}, shown


def _validate_matches(
    raw_list: list, spec: fspec.FeasibilitySpec, uncertain_ids: set[str],
    shown: dict[str, list[tuple[str, str]]], audit: dict,
) -> list:
    """Exact-ref validate the LLM's decisions for ONE spec, tallying reject reasons.

    A decision is kept only if it is scoped to THIS spec and one of its uncertain
    attributes (else ``wrong_spec``); a ``match``'s ``(table_ref, column_ref)`` exists
    verbatim in that attribute's SHOWN candidates (else ``unknown_column``); a repeat
    of an already-decided attribute is ``duplicate``; a malformed row is ``malformed``.
    A ``gap`` decision is always structurally valid (it only drops a match)."""
    rej = audit["rejected_by_reason"]
    out: list = []
    seen: set[str] = set()
    for raw in raw_list:
        try:
            dec = fderiv.parse_match(raw)
        except fderiv.DerivationCatalogError:
            rej["malformed"] += 1
            continue
        if dec.spec_id != spec.spec_id or dec.attribute_id not in uncertain_ids:
            rej["wrong_spec"] += 1
            continue
        if dec.attribute_id in seen:
            rej["duplicate"] += 1
            continue
        if dec.decision == "match" and (dec.table_ref, dec.column_ref) not in shown.get(dec.attribute_id, []):
            rej["unknown_column"] += 1
            continue
        seen.add(dec.attribute_id)
        out.append(dec)
    return out


def _pins_from_decisions(decisions: list) -> dict[str, dict]:
    """Turn validated :class:`MatchDecision`s into the ``pinned_matches`` map
    :func:`_raw_evidence` seats (``attr_id → {decision, table?, column?}``)."""
    pins: dict[str, dict] = {}
    for dec in decisions:
        if dec.decision == "gap":
            pins[dec.attribute_id] = {"decision": "gap"}
        else:
            pins[dec.attribute_id] = {"decision": "match",
                                      "table": dec.table_ref, "column": dec.column_ref}
    return pins


async def _run_matcher_skill(payload_specs: list[dict]) -> Optional[list]:
    """One tool-less SDK call over a domain batch's uncertain attributes → parsed
    ``decisions`` list, or None on any failure (→ deterministic result stands).

    Isolated exactly like :func:`_run_evaluator_skill` (variant 1a): ``allowed_tools=[]``,
    no plugins, no skills, the SKILL.md body as the system prompt, ``max_turns=1`` — the
    strongest isolation for untrusted DB identifiers; a pure reason-over-JSON task needs
    no tools."""
    try:
        from claude_agent_sdk import (  # type: ignore
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError:
        return None

    from .config import BASE_DIR

    try:
        instructions = _skill_body_for(MATCHER_SKILL_NAME)
    except OSError:
        return None

    options = ClaudeAgentOptions(
        allowed_tools=[], permission_mode="default", cwd=str(BASE_DIR),
        max_turns=1, system_prompt=instructions,
    )
    prompt = ("MATCHING TASK (JSON):\n```json\n"
              + json.dumps({"specs": payload_specs}, default=str) + "\n```")
    transcript: list[str] = []
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
            elif isinstance(message, ResultMessage):
                try:
                    from . import llm_usage
                    llm_usage.record_usage(
                        source="feasibility_column_matcher",
                        usage=llm_usage.extract_usage(message),
                    )
                except Exception:
                    pass
                if getattr(message, "is_error", False):
                    return None
    except Exception as exc:
        logger.warning("feasibility column-matcher skill failed: %s", exc)
        return None
    obj = _extract_json_object("".join(transcript))
    if not isinstance(obj, dict):
        return None
    rows = obj.get("decisions")
    return rows if isinstance(rows, list) else None


async def _run_boundary_advisor_skill(payload: dict) -> Optional[list]:
    """One tool-less SDK call that refines the deterministic source clusters by
    reasoning over the tables' generated descriptions → parsed ``clusters`` list, or
    None on any failure (→ the deterministic clusters stand). Variant 1a isolation,
    exactly like :func:`_run_matcher_skill`."""
    try:
        from claude_agent_sdk import (  # type: ignore
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError:
        return None

    from .config import BASE_DIR

    try:
        instructions = _skill_body_for(BOUNDARY_ADVISOR_SKILL_NAME)
    except OSError:
        return None

    options = ClaudeAgentOptions(
        allowed_tools=[], permission_mode="default", cwd=str(BASE_DIR),
        max_turns=1, system_prompt=instructions,
    )
    prompt = ("CLUSTERING TASK (JSON):\n```json\n"
              + json.dumps(payload, default=str) + "\n```")
    transcript: list[str] = []
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
            elif isinstance(message, ResultMessage):
                try:
                    from . import llm_usage
                    llm_usage.record_usage(
                        source="feasibility_boundary_advisor",
                        usage=llm_usage.extract_usage(message),
                    )
                except Exception:
                    pass
                if getattr(message, "is_error", False):
                    return None
    except Exception as exc:
        logger.warning("boundary-advisor skill failed: %s", exc)
        return None
    obj = _extract_json_object("".join(transcript))
    if not isinstance(obj, dict):
        return None
    rows = obj.get("clusters")
    return rows if isinstance(rows, list) else None


async def _run_attribute_grouper_skill(payload: dict) -> Optional[list]:
    """One tool-less SDK call that partitions a spec's attributes into intuitive themes
    by reasoning over their descriptions → parsed ``groups`` list, or None on failure.
    Variant 1a isolation, like :func:`_run_boundary_advisor_skill`."""
    try:
        from claude_agent_sdk import (  # type: ignore
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError:
        return None

    from .config import BASE_DIR

    try:
        instructions = _skill_body_for(ATTRIBUTE_GROUPER_SKILL_NAME)
    except OSError:
        return None

    options = ClaudeAgentOptions(
        allowed_tools=[], permission_mode="default", cwd=str(BASE_DIR),
        max_turns=1, system_prompt=instructions,
    )
    prompt = "GROUPING TASK (JSON):\n```json\n" + json.dumps(payload, default=str) + "\n```"
    transcript: list[str] = []
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
            elif isinstance(message, ResultMessage):
                try:
                    from . import llm_usage
                    llm_usage.record_usage(source="feasibility_attribute_grouper",
                                           usage=llm_usage.extract_usage(message))
                except Exception:
                    pass
                if getattr(message, "is_error", False):
                    return None
    except Exception as exc:
        logger.warning("attribute-grouper skill failed: %s", exc)
        return None
    obj = _extract_json_object("".join(transcript))
    if not isinstance(obj, dict):
        return None
    rows = obj.get("groups")
    return rows if isinstance(rows, list) else None


# ── verdict finalization (skill clamped to the heuristic ceiling) ─────────────

def finalize_verdict(spec: fspec.FeasibilitySpec, ev: dict, scan_state: str,
                     skill_row: Optional[dict]) -> dict[str, Any]:
    """Combine the deterministic heuristic (the ceiling) with the skill's verdict.

    The heuristic tier bounds the skill: the skill may pick that tier or a more
    conservative one (keeping its nicer rationale/notes), but a greener tier than
    the evidence supports is rejected and the whole row falls back to the
    heuristic. All coverage numbers stay deterministic.
    """
    base = heuristic_verdict(ev, scan_state, spec)
    # An empty schema scope is a deterministic, self-explanatory absence — don't
    # let the skill reinterpret it (its evidence bundle carries the shortlist).
    if ev.get("schema_scope_empty") and base["tier"] == "absent":
        return base
    if not skill_row or not isinstance(skill_row, dict):
        return base

    skill_tier = str(skill_row.get("tier", "")).lower()
    if skill_tier not in TIER_RANK:
        return base
    # Invariant: skill can't be greener than the heuristic ceiling.
    if TIER_RANK[skill_tier] > TIER_RANK[base["tier"]]:
        return base
    # Invariant: no green/adaptable without a real product candidate.
    if skill_tier in ("ready", "adaptable") and not base.get("best_product"):
        return base

    # Accept the skill's tier + narrative; keep deterministic evidence/coverage.
    merged = dict(base)
    merged["tier"] = skill_tier
    rationale = str(skill_row.get("rationale", "")).strip()
    if rationale:
        merged["rationale"] = rationale[:1000]
    notes = str(skill_row.get("adaptation_notes", "")).strip()
    if notes:
        merged["adaptation_notes"] = notes[:1000]
    # If the skill (correctly) flags insufficient evidence, honor it.
    if str(skill_row.get("evaluation_state", "")).lower() == "insufficient_evidence" \
            and scan_state != "completed":
        merged["evaluation_state"] = "insufficient_evidence"
    merged["used_skill"] = True
    return merged


# ── run orchestration (the leased-worker unit) ────────────────────────────────

async def evaluate_run(session: Session, run_id: int) -> dict[str, Any]:
    """Execute one feasibility run: evidence → (batched skill | heuristic) →
    invariants → persist FeasibilityScore rows + run summary."""
    run = session.get(FeasibilityRun, run_id)
    if run is None:
        return {"error": "run_not_found"}
    scans = [s for s in (session.get(EstateScan, sid) for sid in _run_scan_ids(run))
             if s is not None]
    if not scans:
        _fail_run(session, run, "scan_not_found")
        return {"error": "scan_not_found"}

    # Aggregate across the estate's sources (multi-catalog): completed ONLY when
    # every contributing scan is completed, else partial — preserving the
    # invariant that incomplete evidence never yields a bare `absent`.
    scan_state = "completed" if all(s.state == "completed" for s in scans) else "partial"

    # Apply spec_ids filter (empty list = no filter = evaluate all specs).
    try:
        _spec_ids_filter: list[str] = json.loads(getattr(run, "spec_ids_json", "") or "[]") or []
    except (TypeError, ValueError):
        _spec_ids_filter = []
    specs = template_corpus.load_specs_from_graph(session, run.domain)
    if _spec_ids_filter:
        _filter_set = set(_spec_ids_filter)
        specs = [s for s in specs if s.spec_id in _filter_set]

    # Build source → platform lookup so dataset rows carry provenance.
    _source_platform: dict[int, str] = {}
    for _src in session.exec(
        select(EstateSource).where(EstateSource.estate_id == run.estate_id)
    ).all():
        _conn = session.get(PlatformConnection, _src.connection_id)
        if _conn:
            _source_platform[_src.id] = _conn.platform_type

    products = enumerate_product_candidates(session)
    datasets = estate_mod.read_estate_datasets(session, [s.id for s in scans])
    # Annotate each dataset with its source's platform type (parsed from URI).
    for _ds in datasets:
        _parts = (_ds.get("uri") or "").split(":")
        try:
            _sid = int(_parts[2]) if len(_parts) > 2 else 0
        except (ValueError, IndexError):
            _sid = 0
        _ds["platform"] = _source_platform.get(_sid, "")

    run.state = "running"
    run.corpus_version = template_corpus.GRAPH_CORPUS_MARKER
    run.evaluator_version = EVALUATOR_VERSION
    run.skill_version = skill_version()
    run.embedding_model = "bge-small" if schema_dna.available_embeddings() else "token-jaccard"
    run.updated_at = datetime.utcnow()
    session.add(run)
    session.commit()

    # Stage 1 setup: schema shortlist levers + precomputed schema texts/embeddings
    # (once per run — they don't depend on the spec). scope_to_schemas=False is an
    # escape hatch to today's whole-estate behavior.
    try:
        _scoping = json.loads(getattr(run, "schema_scoping_json", "") or "{}") or {}
    except (TypeError, ValueError):
        _scoping = {}
    _scope_enabled = bool(_scoping.get("enabled", True))
    _floor = _scoping.get("floor", SCHEMA_RELEVANCE_FLOOR)
    _threshold = _scoping.get("threshold", SCHEMA_SHORTLIST_THRESHOLD)
    _cap = _scoping.get("cap", MAX_SCHEMAS_PER_SPEC)
    _inc_ov = {(str(k[0]), str(k[1])) for k in (_scoping.get("include") or []) if len(k) == 2}
    _exc_ov = {(str(k[0]), str(k[1])) for k in (_scoping.get("exclude") or []) if len(k) == 2}
    schema_desc_map: dict[tuple[str, str], str] = {}
    desc_source_map: dict[tuple[str, str], str] = {}
    datasets_by_key: dict[tuple[str, str], list[dict]] = {}
    schema_texts: dict[tuple[str, str], str] = {}
    embed_cache: dict[str, list[float]] = {}
    if _scope_enabled:
        for _d in datasets:
            datasets_by_key.setdefault((_d.get("database", ""), _d.get("schema", "")), []).append(_d)
        # Source-aware descriptions + a read-time fallback so a schema the LLM left
        # blank (or a pre-provenance scan) still contributes without re-enrichment.
        resolved = _resolve_schema_descs(
            _schema_descriptions_with_source(session, scans), datasets_by_key,
        )
        schema_desc_map = {k: v["text"] for k, v in resolved.items()}
        desc_source_map = {k: v["source"] for k, v in resolved.items()}
        schema_texts = {
            key: _schema_group_text(
                ds, resolved.get(key, {}).get("text", ""),
                resolved.get(key, {}).get("source", "llm"),
            )
            for key, ds in datasets_by_key.items()
        }
        embed_cache = _text_embed_cache(list(schema_texts.values()))

    # Pre-group specs by domain (batched skill calls) so progress totals are known.
    specs_by_domain: dict[str, list] = {}
    for spec in specs:
        specs_by_domain.setdefault(spec.domain, []).append(spec)

    _progress: dict[str, Any] = {
        "stage": "shortlisting", "specs_total": len(specs), "specs_done": 0,
        "domains_total": len(specs_by_domain), "domains_done": 0,
        "current_spec": None, "current_domain": None,
    }

    def _emit(**changes: Any) -> None:
        _progress.update(changes)
        _persist_progress(run_id, _progress)

    _emit(stage="shortlisting")

    # Build per-spec evidence (schema shortlist → scoped column assignment).
    _emit(stage="building_evidence", specs_done=0)
    evidence_by_spec: dict[str, dict] = {}
    for spec in specs:
        _emit(current_spec=spec.name, current_domain=spec.domain)
        if _scope_enabled:
            shortlist = _schema_shortlist(
                spec, datasets_by_key, schema_desc_map, _floor, _cap,
                threshold=_threshold,
                schema_texts=schema_texts, embed_cache=embed_cache,
                desc_source_map=desc_source_map,
                include_override=_inc_ov, exclude_override=_exc_ov,
            )
            ev = build_spec_evidence(spec, products, datasets, shortlist)
            ev["schema_relevance_floor"] = _floor
        else:
            ev = build_spec_evidence(spec, products, datasets)
        evidence_by_spec[spec.spec_id] = ev
        _progress["specs_done"] += 1
        _persist_progress(run_id, _progress)

    # ── R3: composite-derivation re-pass. The curated catalog already ran inside
    #    the first-pass reconciliation; the LLM advisor now proposes SAME-TABLE
    #    compositions for still-unmatched REQUIRED attrs (gaps-only, exact-ref
    #    validated). Best-effort — never breaks the run. Audit everything.
    derivation_audit: dict[str, Any] = {
        "attempted": False, "proposals_received": 0, "proposals_valid": 0,
        "proposals_applied": 0,
        "rejected_by_reason": {"unknown_column": 0, "mixed_tables": 0,
                               "wrong_spec": 0, "type_incompatible": 0},
        "catalog_version": fderiv.catalog_version(),
    }
    for _ev in evidence_by_spec.values():
        _cerr = (_ev.get("raw_candidates") or {}).get("derivation_catalog_error")
        if _cerr:
            derivation_audit["catalog_error"] = _cerr
            break
    attempted_derivation = False
    used_derivation = False
    # Validated advisor proposals per spec — retained so the reasoning-matcher re-pass
    # (below) rebuilds evidence WITH the composites still applied, not only the pins.
    applied_proposals_by_spec: dict[str, list] = {}
    if _sdk_present():
        affected_by_domain: dict[str, list] = {}
        for spec in specs:
            raw = (evidence_by_spec[spec.spec_id].get("raw_candidates") or {})
            if any(g.get("required") for g in raw.get("gaps", [])):
                affected_by_domain.setdefault(spec.domain, []).append(spec)
        for domain, dspecs in affected_by_domain.items():
            payload_specs: list[dict] = []
            spec_ctx: dict[str, tuple[list, set]] = {}
            for spec in dspecs:
                ev = evidence_by_spec[spec.spec_id]
                scoped = _scoped_datasets_for_spec(ev, datasets)
                unmatched_ids = {g["attr_id"] for g in (ev.get("raw_candidates") or {}).get("gaps", [])
                                 if g.get("required")}
                inp = _derivation_advisor_spec_input(spec, ev, scoped, unmatched_ids)
                if inp:
                    payload_specs.append(inp)
                    spec_ctx[spec.spec_id] = (scoped, unmatched_ids)
            if not payload_specs:
                continue
            derivation_audit["attempted"] = True
            attempted_derivation = True
            _emit(stage="deriving", current_domain=domain)
            try:
                proposals_raw = await _run_derivation_advisor_skill(payload_specs)
            except Exception as exc:  # never let the advisor break the run
                logger.warning("derivation advisor errored for domain %s: %s", domain, exc)
                proposals_raw = None
            if not proposals_raw:
                continue
            derivation_audit["proposals_received"] += len(proposals_raw)
            for spec in dspecs:
                if spec.spec_id not in spec_ctx:
                    continue
                scoped, unmatched_ids = spec_ctx[spec.spec_id]
                valid = _validate_proposals(
                    [p for p in proposals_raw if isinstance(p, dict) and p.get("spec_id") == spec.spec_id],
                    spec, scoped, unmatched_ids, derivation_audit)
                if not valid:
                    continue
                derivation_audit["proposals_valid"] += len(valid)
                shortlist = evidence_by_spec[spec.spec_id].get("schema_shortlist")
                ev2 = build_spec_evidence(spec, products, datasets, shortlist, extra_proposals=valid)
                if shortlist is not None:
                    ev2["schema_relevance_floor"] = _floor
                applied = sum(1 for r in (ev2.get("raw_candidates") or {}).get("assignment", [])
                              if (r.get("composite") or {}).get("source") == "advisor")
                derivation_audit["proposals_applied"] += applied
                if applied:
                    used_derivation = True
                evidence_by_spec[spec.spec_id] = ev2
                applied_proposals_by_spec[spec.spec_id] = valid

    # ── R4: reasoning column-matcher re-pass. Over the UNCERTAIN subset of each
    #    spec's attributes (gray-band matches, name-only matches, ambiguous entities,
    #    identifier-gated contenders, near-miss required gaps), a tool-less skill
    #    reasons over the estate's generated descriptions and returns validated
    #    attribute→column decisions. Valid decisions are PINNED and the evidence is
    #    rebuilt deterministically (composites retained). Best-effort — never breaks
    #    the run; SDK absent / any failure leaves the deterministic result unchanged.
    matcher_audit: dict[str, Any] = {
        "attempted": False, "decisions_received": 0, "decisions_valid": 0,
        "decisions_applied": 0, "overrides": 0, "gaps_created": 0,
        "rejected_by_reason": {"unknown_column": 0, "wrong_spec": 0,
                               "duplicate": 0, "malformed": 0},
        "matcher_version": MATCHER_VERSION,
    }
    if _sdk_present():
        _emit(stage="adjudicating", specs_done=0, domains_done=0, current_spec=None)
        matcher_by_domain: dict[str, list] = {}
        for spec in specs:
            if _uncertain_attrs(evidence_by_spec[spec.spec_id]):
                matcher_by_domain.setdefault(spec.domain, []).append(spec)
        for domain, dspecs in matcher_by_domain.items():
            payload_specs = []
            spec_ctx: dict[str, tuple[set, dict]] = {}
            for spec in dspecs:
                ev = evidence_by_spec[spec.spec_id]
                scoped = _scoped_datasets_for_spec(ev, datasets)
                uncertain_ids = _uncertain_attrs(ev)
                inp, shown = _matcher_spec_input(spec, ev, scoped, uncertain_ids)
                if inp:
                    payload_specs.append(inp)
                    spec_ctx[spec.spec_id] = (uncertain_ids, shown)
            if not payload_specs:
                continue
            matcher_audit["attempted"] = True
            _emit(stage="adjudicating", current_domain=domain)
            try:
                decisions_raw = await _run_matcher_skill(payload_specs)
            except Exception as exc:  # never let the matcher break the run
                logger.warning("column-matcher errored for domain %s: %s", domain, exc)
                decisions_raw = None
            if not decisions_raw:
                continue
            matcher_audit["decisions_received"] += len(decisions_raw)
            for spec in dspecs:
                if spec.spec_id not in spec_ctx:
                    continue
                uncertain_ids, shown = spec_ctx[spec.spec_id]
                valid = _validate_matches(
                    [d for d in decisions_raw if isinstance(d, dict) and d.get("spec_id") == spec.spec_id],
                    spec, uncertain_ids, shown, matcher_audit)
                if not valid:
                    continue
                matcher_audit["decisions_valid"] += len(valid)
                ev = evidence_by_spec[spec.spec_id]
                before = {r["attr_id"]: (r.get("dataset_table"), r.get("column"))
                          for r in (ev.get("raw_candidates") or {}).get("assignment", [])}
                pins = _pins_from_decisions(valid)
                shortlist = ev.get("schema_shortlist")
                ev3 = build_spec_evidence(
                    spec, products, datasets, shortlist,
                    extra_proposals=applied_proposals_by_spec.get(spec.spec_id),
                    pinned_matches=pins)
                if shortlist is not None:
                    ev3["schema_relevance_floor"] = _floor
                after = {r["attr_id"]: (r.get("dataset_table"), r.get("column"))
                         for r in (ev3.get("raw_candidates") or {}).get("assignment", [])}
                overrides = gaps_created = 0
                for dec in valid:
                    aid = dec.attribute_id
                    if dec.decision == "gap":
                        if aid in before and aid not in after:
                            gaps_created += 1
                    else:
                        if before.get(aid) != after.get(aid) and aid in after:
                            overrides += 1
                matcher_audit["decisions_applied"] += (overrides + gaps_created)
                matcher_audit["overrides"] += overrides
                matcher_audit["gaps_created"] += gaps_created
                evidence_by_spec[spec.spec_id] = ev3

    used_skill_any = False
    verdicts: dict[str, dict] = {}
    _emit(stage="evaluating", specs_done=0, domains_done=0, current_spec=None)
    for domain, domain_specs in specs_by_domain.items():
        _emit(current_domain=domain)
        bundle = {
            "scan_state": scan_state,
            "corpus_version": run.corpus_version,
            "specs": [evidence_by_spec[s.spec_id] for s in domain_specs],
        }
        skill_rows = None
        try:
            skill_rows = await _run_evaluator_skill(bundle)
        except Exception as exc:  # never let the skill break the run
            logger.warning("evaluator skill errored for domain %s: %s", domain, exc)
        by_id = {}
        if skill_rows:
            used_skill_any = True
            for r in skill_rows:
                if isinstance(r, dict) and r.get("spec_id"):
                    by_id[r["spec_id"]] = r
        for spec in domain_specs:
            _emit(current_spec=spec.name)
            verdicts[spec.spec_id] = finalize_verdict(
                spec, evidence_by_spec[spec.spec_id], scan_state, by_id.get(spec.spec_id)
            )
            _progress["specs_done"] += 1
            _persist_progress(run_id, _progress)
        _progress["domains_done"] += 1
        _persist_progress(run_id, _progress)

    _emit(stage="finalizing", current_spec=None, current_domain=None)

    # Persist per-spec scores.
    tier_counts: dict[str, int] = {}
    eval_counts: dict[str, int] = {}
    for spec in specs:
        v = verdicts[spec.spec_id]
        tier_counts[v["tier"]] = tier_counts.get(v["tier"], 0) + 1
        eval_counts[v["evaluation_state"]] = eval_counts.get(v["evaluation_state"], 0) + 1
        session.add(FeasibilityScore(
            run_id=run.id, estate_id=run.estate_id, spec_id=spec.spec_id,
            spec_name=spec.name, domain=spec.domain, tier=v["tier"],
            evaluation_state=v["evaluation_state"], confidence=v["confidence"],
            required_coverage=v["required_coverage"], total_coverage=v["total_coverage"],
            best_product_uri=(v.get("best_product") or {}).get("uri") if v.get("best_product") else None,
            best_product_version=(v.get("best_product") or {}).get("version") if v.get("best_product") else None,
            adaptation_notes=v.get("adaptation_notes", ""), rationale=v.get("rationale", ""),
            matched_json=json.dumps(v.get("matched", []), default=str),
            gaps_json=json.dumps(v.get("gaps", []), default=str),
            derivations_json=json.dumps(v.get("derivations", []), default=str),
            join_plan_json=json.dumps(v.get("join_plan") or {}, default=str),
            evidence_json=json.dumps(evidence_by_spec[spec.spec_id], default=str),
        ))

    run.used_skill = used_skill_any
    run.state = "completed" if scan_state == "completed" else "partial"
    run.summary_json = json.dumps({
        "spec_count": len(specs), "tier_counts": tier_counts,
        "evaluation_state_counts": eval_counts, "scan_state": scan_state,
        "product_candidate_pool": len(products), "estate_dataset_count": len(datasets),
        # Reproducibility (G-lite): the R2 scoring levers that shaped every verdict.
        "scoring": {
            "scoring_version": SCORING_VERSION,
            "table_affinity_weight": TABLE_AFFINITY_WEIGHT,
            "fk_carrier_penalty": FK_CARRIER_PENALTY,
            "match_threshold": schema_dna.MATCH_THRESHOLD,
            "schema_relevance_floor": _floor,
            "schema_shortlist_threshold": _threshold,
            "max_schemas_per_spec": _cap,
            # R3 composite-derivation levers.
            "component_match_threshold": COMPONENT_MATCH_THRESHOLD,
            "composite_table_affinity_floor": COMPOSITE_TABLE_AFFINITY_FLOOR,
            "derivation_penalty": DERIVATION_PENALTY,
            "affinity_preference_scale": AFFINITY_PREFERENCE_SCALE,
        },
        # R3 derivation audit: curated catalog + LLM-advisor proposal accounting.
        "derivation": derivation_audit,
        "attempted_derivation_skill": attempted_derivation,
        "used_derivation_skill": used_derivation,
        # R4 reasoning column-matcher audit (Phase 2).
        "matcher": matcher_audit,
        # Description-quality visibility: per-schema estate description coverage +
        # the spec-side description completeness — so a weak-description drag on
        # matching accuracy is surfaced, and failed-enrichment schemas are flagged.
        "description_coverage": _description_coverage(datasets, desc_source_map),
        "spec_attr_description_coverage": {
            "total": sum(len(s.attributes) for s in specs),
            "described": sum(1 for s in specs for a in s.attributes
                             if (a.description or "").strip()),
        },
    }, default=str)
    run.updated_at = datetime.utcnow()
    session.add(run)
    session.commit()
    _persist_progress(run_id, {**_progress, "stage": "completed"})
    return {"state": run.state, "tier_counts": tier_counts}


# ── single-spec re-score with a schema-scope override (interactive, heuristic) ──

def reevaluate_one_score(
    session: Session, run_id: int, spec_id: str,
    include: Optional[list[tuple[str, str]]] = None,
    exclude: Optional[list[tuple[str, str]]] = None,
) -> dict[str, Any]:
    """Recompute ONE score with an explicit per-schema include/exclude override and
    write it back — the Assembly 'toggle a schema, re-score' action. Deterministic +
    fast: reuses ``_schema_shortlist`` (with the override) → ``build_spec_evidence`` →
    the **heuristic** ``finalize_verdict`` (no LLM in the interactive path). The verdict
    invariants (ceiling, grain gate) still apply."""
    run = session.get(FeasibilityRun, run_id)
    if run is None:
        return {"error": "run_not_found"}
    score = session.exec(
        select(FeasibilityScore).where(
            FeasibilityScore.run_id == run_id, FeasibilityScore.spec_id == spec_id
        )
    ).first()
    if score is None:
        return {"error": "score_not_found"}
    spec = next((s for s in template_corpus.load_specs_from_graph(session, run.domain)
                 if s.spec_id == spec_id), None)
    if spec is None:
        return {"error": "spec_not_found"}
    scans = [s for s in (session.get(EstateScan, sid) for sid in _run_scan_ids(run)) if s is not None]
    if not scans:
        return {"error": "scan_not_found"}
    scan_state = "completed" if all(s.state == "completed" for s in scans) else "partial"

    products = enumerate_product_candidates(session)
    datasets = estate_mod.read_estate_datasets(session, [s.id for s in scans])

    try:
        _scoping = json.loads(getattr(run, "schema_scoping_json", "") or "{}") or {}
    except (TypeError, ValueError):
        _scoping = {}
    floor = _scoping.get("floor", SCHEMA_RELEVANCE_FLOOR)
    threshold = _scoping.get("threshold", SCHEMA_SHORTLIST_THRESHOLD)
    cap = _scoping.get("cap", MAX_SCHEMAS_PER_SPEC)
    inc = {(str(a), str(b)) for a, b in (include or [])}
    exc = {(str(a), str(b)) for a, b in (exclude or [])}

    datasets_by_key: dict[tuple[str, str], list[dict]] = {}
    for d in datasets:
        datasets_by_key.setdefault((d.get("database", ""), d.get("schema", "")), []).append(d)
    resolved = _resolve_schema_descs(
        _schema_descriptions_with_source(session, scans), datasets_by_key)
    schema_desc_map = {k: v["text"] for k, v in resolved.items()}
    desc_source_map = {k: v["source"] for k, v in resolved.items()}

    shortlist = _schema_shortlist(
        spec, datasets_by_key, schema_desc_map, floor, cap, threshold=threshold,
        desc_source_map=desc_source_map, include_override=inc, exclude_override=exc)
    ev = build_spec_evidence(spec, products, datasets, shortlist)
    ev["schema_relevance_floor"] = floor
    v = finalize_verdict(spec, ev, scan_state, None)  # heuristic — deterministic, fast

    score.tier = v["tier"]
    score.evaluation_state = v["evaluation_state"]
    score.confidence = v["confidence"]
    score.required_coverage = v["required_coverage"]
    score.total_coverage = v["total_coverage"]
    bp = v.get("best_product") or {}
    score.best_product_uri = bp.get("uri")
    score.best_product_version = bp.get("version")
    score.adaptation_notes = v.get("adaptation_notes", "")
    score.rationale = v.get("rationale", "")
    score.matched_json = json.dumps(v.get("matched", []), default=str)
    score.gaps_json = json.dumps(v.get("gaps", []), default=str)
    score.derivations_json = json.dumps(v.get("derivations", []), default=str)
    score.join_plan_json = json.dumps(v.get("join_plan") or {}, default=str)
    score.evidence_json = json.dumps(ev, default=str)
    session.add(score)
    session.commit()
    return {"spec_id": spec_id, "tier": score.tier, "required_coverage": score.required_coverage,
            "reevaluated": True}


# ── act-on-green (tier-differentiated) ────────────────────────────────────────

def build_action(session: Session, run_id: int, spec_id: str) -> dict[str, Any]:
    """Resolve the tier-appropriate next action for one feasibility score.

    - ``ready``      → adopt the matched published product (marketplace detail).
    - ``adaptable``  → seed a CF wizard that CONSUMES the matched product.
    - ``assemblable``→ compose a modernization portfolio (SA sources → aggregate/
      consumer) and hand it to the existing intake scaffold saga.
    - ``absent``     → log a gap.
    """
    score = session.exec(
        select(FeasibilityScore).where(
            FeasibilityScore.run_id == run_id, FeasibilityScore.spec_id == spec_id
        )
    ).first()
    if score is None:
        return {"error": "score_not_found"}
    tier = score.tier
    if tier == "ready" and score.best_product_uri:
        return {
            "tier": tier, "action": "adopt",
            "product_uri": score.best_product_uri,
            "product_version": score.best_product_version,
            "marketplace_path": f"/product/marketplace?uri={score.best_product_uri}",
            "message": "A governed product already matches — adopt/endorse it in the marketplace.",
        }
    if tier == "adaptable" and score.best_product_uri:
        return {
            "tier": tier, "action": "adapt",
            "consume_product_uri": score.best_product_uri,
            "product_version": score.best_product_version,
            "spec_id": spec_id, "adaptation_notes": score.adaptation_notes,
            "wizard_path": f"/product/new/consumer?consume={score.best_product_uri}&from_spec={spec_id}",
            "message": "Author a consumer product that CONSUMES the matched product, adapted per the notes.",
        }
    if tier == "assemblable":
        return {
            "tier": tier, "action": "assemble",
            "spec_id": spec_id,
            "next": "compose_modernization_portfolio",
            "message": "Raw data exists but isn't a governed product — compose a modernization "
                       "portfolio (source products first, then an aggregate/consumer) via intake.",
        }
    return {"tier": tier, "action": "log_gap",
            "message": "No matching data — record as a marketplace gap."}


def sanitized_consumer_odcs(
    spec: fspec.FeasibilitySpec,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    purpose: Optional[str] = None,
) -> dict[str, Any]:
    """Build a **project-bound** consumer ODCS draft from a ``FeasibilitySpec``,
    ready to seed a ``dpe-cf`` wizard's Step-4 "Confirm the Schema".

    Reuses the FS→ODCS bridge (``feasibility_map.feasibility_spec_to_odcs`` — it
    carries per-attribute type/description/required/primaryKey/classification plus a
    ``transform`` block with grain keys + ``scd_policy`` the wizard's Shape hydration
    reads) but **strips the template identity** so the save is project-bound:

    - ``id`` — a ``template:{domain}:{spec}`` id that ``_save_odcs_to_graph`` would use
      verbatim as ``contract_id`` (``routers/odcs.py``), which ``link_contract_to_project``
      refuses (isolation guard) → an orphaned draft. Dropping it falls the contract_id
      back to ``{project_code}-contract``.
    - ``sourceSpecId`` — the template's spec id, meaningless on a project contract.

    Marks the draft ``status`` and overlays PO-facing ``name``/``description``/``purpose``
    when supplied (else the bridge's spec-derived values stand).
    """
    odcs = feasibility_map.feasibility_spec_to_odcs(spec)
    odcs.pop("id", None)
    odcs.pop("sourceSpecId", None)
    odcs["status"] = "draft"
    if name is not None:
        odcs["name"] = name
    if description is not None:
        odcs["description"] = description
    if purpose is not None:
        odcs["purpose"] = purpose
    return odcs


def compose_modernization_submission(
    session: Session, run_id: int, spec_id: str, created_by: str,
) -> dict[str, Any]:
    """For an ``assemblable`` spec, compose a modernization portfolio blueprint and
    stage it as a **proposed** ``IntakeSubmission`` — the PO reviews + approves it
    in the existing Intake UI, whose scaffold saga creates the SA sources (over the
    matched estate raw tables) and the aggregate/consumer over them.

    Reuses the sanctioned intake path rather than a bespoke scaffold (load-bearing
    #4). The estate/scan provenance is recorded on the submission so the scaffolded
    SA projects can be bound back to the estate source.
    """
    from . import intake_blueprint as ibp
    from .models import IntakeSubmission

    score = session.exec(
        select(FeasibilityScore).where(
            FeasibilityScore.run_id == run_id, FeasibilityScore.spec_id == spec_id
        )
    ).first()
    if score is None:
        return {"error": "score_not_found"}
    if score.tier != "assemblable":
        return {"error": "not_assemblable", "tier": score.tier}

    run = session.get(FeasibilityRun, run_id)
    evidence = json.loads(score.evidence_json or "{}")
    raw = (evidence.get("raw_candidates") or {})
    datasets = raw.get("datasets") or []
    spec = next((s for s in template_corpus.load_specs_from_graph(session)
                 if s.spec_id == spec_id), None)
    domain = score.domain or (spec.domain if spec else "")

    # One source-aligned candidate per matched estate dataset.
    source_aligned = []
    dep_ids = []
    for i, d in enumerate(datasets):
        cid = f"src-{i}-{fspec.slug(d.get('table', ''), str(i))}"
        dep_ids.append(cid)
        source_aligned.append({
            "candidate_id": cid,
            "name": {"value": (d.get("table") or f"source-{i}").replace("_", " ").title(),
                     "confidence": "high", "why": "matched estate raw table"},
            "domain": {"value": domain, "confidence": "high", "why": "spec domain"},
            "product_idea": f"Source-align the '{d.get('table')}' table ({d.get('schema')}) "
                            f"to support the '{score.spec_name}' product.",
        })
    # One consumer-aligned candidate = the desired product. When the reference spec
    # resolved, attach a project-bound consumer ODCS built from its attributes so the
    # scaffold seeds the CF wizard's schema faithfully (exactly like the Assembly path)
    # instead of leaving an empty draft the advisor then over-fills. Best-effort — any
    # bridge failure falls back to today's no-odcs empty draft.
    con_id = f"con-0-{fspec.slug(spec_id, 'product')}"
    consumer_odcs: Optional[dict[str, Any]] = None
    if spec is not None:
        try:
            consumer_odcs = sanitized_consumer_odcs(
                spec, name=score.spec_name,
                description=spec.description or "", purpose=spec.description or "",
            )
        except Exception:
            logger.warning("compose_modernization_submission: consumer ODCS build failed "
                           "for spec %s; leaving empty draft", spec_id, exc_info=True)
            consumer_odcs = None
    consumer_aligned = [{
        "candidate_id": con_id,
        "name": {"value": score.spec_name, "confidence": "high", "why": "reference spec"},
        "domain": {"value": domain, "confidence": "high", "why": "spec domain"},
        "purpose": (spec.description if spec else score.rationale),
        **({"odcs": consumer_odcs} if consumer_odcs else {}),
    }]
    dependencies = [
        {"dependency_id": f"dep-{i}", "from_candidate_id": con_id,
         "to_candidate_id": sid, "confidence": "medium"}
        for i, sid in enumerate(dep_ids)
    ]
    blueprint = {
        "scenario": "modernization",
        "overall_confidence": "medium",
        "source_aligned": source_aligned,
        "consumer_aligned": consumer_aligned,
        "dependencies": dependencies,
        "rationale": (
            f"Composed from the Connected-Estate feasibility run #{run_id}: the '{score.spec_name}' "
            f"spec is assemblable from {len(datasets)} estate dataset(s). Build source-aligned "
            f"products over them first, then the consumer/aggregate."
        ),
    }
    # Validate fail-closed before staging (never stage a malformed blueprint).
    model = ibp.parse_blueprint(blueprint)
    ibp.normalize_ids(model)
    validated = model.model_dump(mode="json")

    external_ref = f"feasibility:{run_id}:{spec_id}"
    existing = session.exec(
        select(IntakeSubmission).where(
            IntakeSubmission.source_system == "connected-estate",
            IntakeSubmission.external_ref == external_ref,
        )
    ).first()
    provenance = {
        "estate_id": run.estate_id if run else None,
        "scan_id": run.scan_id if run else None,
        "feasibility_run_id": run_id, "spec_id": spec_id,
        "estate_dataset_uris": [d.get("uri") for d in datasets],
    }
    # FOLLOW-UP (Bridge A source-scope carry): the Bridge B assembly path
    # (routers/intake.from_assembly) stamps a per-candidate `provenance.source_scope`
    # so the scaffold pre-binds the source + pre-scopes discovery
    # (intake_scaffold._apply_source_scope). To bring the same UX here, derive a
    # `source_scope` keyed by each `src-{i}-…` candidate_id from `datasets[i]["uri"]`
    # via `estate.parse_dataset_uri` (single-table scope per candidate) and add it to
    # `provenance` below. Deferred deliberately — the demo drives Bridge B.
    if existing is not None:
        existing.blueprint_json = json.dumps(validated, default=str)
        existing.status = "proposed"
        existing.blueprint_revision += 1
        existing.raw_payload_json = json.dumps({"source": "connected-estate", "provenance": provenance}, default=str)
        existing.updated_at = datetime.utcnow()
        session.add(existing)
        session.commit()
        sub_id = existing.id
    else:
        sub = IntakeSubmission(
            source_system="connected-estate", external_ref=external_ref,
            scenario="modernization", status="proposed",
            raw_payload_json=json.dumps({"source": "connected-estate", "provenance": provenance}, default=str),
            blueprint_json=json.dumps(validated, default=str), blueprint_revision=1,
            reviewed_by=created_by,
        )
        session.add(sub)
        session.commit()
        session.refresh(sub)
        sub_id = sub.id
    return {
        "action": "assemble", "intake_id": sub_id,
        "review_path": f"/product/intake/{sub_id}",
        "source_product_count": len(source_aligned),
        "message": "A modernization portfolio was staged for review — approve it in the Intake "
                   "surface to scaffold the source products and the consumer/aggregate.",
    }


# ── recommend definitions (LLM skill + embedding-heuristic fallback) ──────────
#
# An alternative to picking specs by domain: read the enriched estate and
# recommend WHICH reference product definitions to evaluate (pre-checked in the
# UI; the PO can still edit). The heuristic fallback is always available so the
# capability works with the SDK absent.

def build_estate_description(session: Session, scans: list[EstateScan]) -> str:
    """Compose a text description of the estate from its enriched schema
    descriptions + table descriptions (+ bare identifiers when unenriched) — the
    input the recommender matches spec definitions against."""
    parts: list[str] = []
    for (db, sch), desc in _schema_descriptions(session, scans).items():
        loc = ".".join(p for p in (db, sch) if p)
        parts.append(f"{loc}: {desc}" if loc else desc)
    datasets = estate_mod.read_estate_datasets(session, [s.id for s in scans])
    table_bits: list[str] = []
    for d in datasets:
        loc = ".".join(p for p in (d.get("database", ""), d.get("schema", ""), d.get("table", "")) if p)
        tdesc = (d.get("description") or "").strip()
        if tdesc:
            table_bits.append(f"{loc}: {tdesc}" if loc else tdesc)
        elif loc:
            table_bits.append(loc)
        if len(table_bits) >= 200:
            break
    if table_bits:
        parts.append("Tables: " + "; ".join(table_bits))
    return "\n".join(p for p in parts if p)


def _spec_index_text(spec: dict) -> str:
    """Relevance text for an index-shaped spec dict (name + domain + description)."""
    return ". ".join(p for p in (
        str(spec.get("name") or ""), str(spec.get("domain") or ""),
        str(spec.get("description") or ""),
    ) if p)


def heuristic_recommend(estate_text: str, specs: list[dict]) -> list[dict[str, Any]]:
    """Deterministic fallback: rank specs by estate/spec text similarity."""
    spec_texts = {s.get("spec_id"): _spec_index_text(s) for s in specs}
    cache = _text_embed_cache([estate_text] + list(spec_texts.values()))
    ranked: list[dict[str, Any]] = []
    for s in specs:
        sid = s.get("spec_id")
        score = int(schema_dna.text_similarity(estate_text, spec_texts.get(sid, ""), cache))
        ranked.append({
            "spec_id": sid, "name": s.get("name", ""), "domain": s.get("domain", ""),
            "score": score,
            "rationale": "Ranked by estate/spec text similarity (heuristic).",
        })
    return ranked


async def _run_recommender_skill(estate_text: str, specs: list[dict]) -> Optional[list]:
    """Invoke the ``data-product-feasibility-recommender`` skill (isolated,
    tool-less-ish) → parsed ``recommended`` list, or None on any failure. Mirrors
    ``routers/ingest_products._run_classifier_skill``."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore
        from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock  # type: ignore
    except ImportError:
        return None

    from .config import BASE_DIR, PIPELINE_PLUGINS

    system_prompt = (
        f"FIRST: Load the `{RECOMMENDER_SKILL_NAME}` skill via the Skill tool, then follow its "
        "instructions. Emit exactly one fenced JSON code block as the skill instructs. Do not "
        "write files. Do not run shell commands. Do not answer in prose outside the JSON block."
    )
    payload = {"estate": estate_text, "specs": specs}
    user_prompt = (
        f"FIRST: Load the {RECOMMENDER_SKILL_NAME} skill using the Skill tool.\n\n"
        f"INPUT (JSON):\n{json.dumps(payload, default=str)}\n\n"
        "Output the single fenced JSON block as instructed by the skill."
    )
    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Skill"], permission_mode="acceptEdits", cwd=str(BASE_DIR),
        max_turns=4, skills="all", plugins=PIPELINE_PLUGINS,
        system_prompt={"type": "preset", "preset": "claude_code", "append": system_prompt},
    )
    transcript: list[str] = []
    try:
        async for message in query(prompt=user_prompt, options=options):
            if message is None:
                continue
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
            elif isinstance(message, ResultMessage):
                try:
                    from . import llm_usage
                    llm_usage.record_usage(
                        source="feasibility_recommender",
                        usage=llm_usage.extract_usage(message),
                    )
                except Exception:
                    pass
                if getattr(message, "is_error", False):
                    return None
    except Exception as exc:
        logger.warning("feasibility recommender skill failed: %s", exc)
        return None
    obj = _extract_json_object("".join(transcript))
    if not isinstance(obj, dict):
        return None
    rows = obj.get("recommended")
    return rows if isinstance(rows, list) else None


async def recommend_specs(estate_text: str, specs: list[dict],
                          *, floor: float = RECOMMEND_FLOOR) -> dict[str, Any]:
    """Recommend which reference specs to evaluate against this estate.

    Tries the LLM skill first; always falls back to the embedding heuristic. Every
    spec is banded ``strong`` (score ≥ ``floor``) / ``tentative`` (mirrors
    ResolveAndBind's confidence bands); the strong band is the pre-checked set.
    """
    by_id = {s.get("spec_id"): s for s in specs}
    skill_rows = None
    try:
        skill_rows = await _run_recommender_skill(estate_text, specs)
    except Exception as exc:  # never let the skill break the endpoint
        logger.warning("recommend_specs skill errored: %s", exc)
        skill_rows = None

    used_skill = bool(skill_rows)
    if skill_rows:
        skill_by_id: dict[str, dict] = {}
        for r in skill_rows:
            if isinstance(r, dict) and r.get("spec_id") in by_id:
                skill_by_id[r["spec_id"]] = r
        ranked: list[dict[str, Any]] = []
        for sid, spec in by_id.items():
            r = skill_by_id.get(sid)
            try:
                score = int(round(float((r or {}).get("score", 0))))
            except (TypeError, ValueError):
                score = 0
            score = max(0, min(100, score))
            ranked.append({
                "spec_id": sid, "name": spec.get("name", ""), "domain": spec.get("domain", ""),
                "score": score,
                "rationale": str((r or {}).get("rationale", "")).strip()[:500]
                             or "Not recommended for this estate.",
            })
    else:
        ranked = heuristic_recommend(estate_text, specs)

    ranked.sort(key=lambda x: (-x["score"], str(x.get("name", ""))))
    for row in ranked:
        row["band"] = "strong" if row["score"] >= floor else "tentative"
    recommended_spec_ids = [r["spec_id"] for r in ranked if r["band"] == "strong"]
    return {
        "recommended_spec_ids": recommended_spec_ids,
        "ranked": ranked,
        "used_skill": used_skill,
        "recommend_floor": floor,
    }


def _persist_progress(run_id: int, progress: dict[str, Any]) -> None:
    """Commit live evaluation progress in its OWN short-lived session so a progress
    write never entangles with (or rolls back alongside) score persistence. Emits
    a monotonic ``updated_at`` so the poller can tell a stalled run from a live one.
    Best-effort — a progress write must never break the run."""
    payload = dict(progress)
    payload["updated_at"] = datetime.utcnow().isoformat()
    try:
        with Session(engine) as s:
            row = s.get(FeasibilityRun, run_id)
            if row is None:
                return
            row.progress_json = json.dumps(payload, default=str)
            s.add(row)
            s.commit()
    except Exception:  # progress is telemetry, never load-bearing
        logger.debug("feasibility progress persist failed for run %s", run_id, exc_info=True)


def _fail_run(session: Session, run: FeasibilityRun, reason: str) -> None:
    run.state = "failed"
    run.error_json = json.dumps({"error": reason}, default=str)
    run.progress_json = json.dumps({"stage": "failed", "error": reason,
                                    "updated_at": datetime.utcnow().isoformat()}, default=str)
    run.lease_owner = None
    run.lease_expires_at = None
    run.updated_at = datetime.utcnow()
    session.add(run)
    session.commit()


# ── leased worker unit (claims queued FeasibilityRun rows) ────────────────────

async def process_one_run(worker_id: str, lease_ttl: int) -> bool:
    """Claim + evaluate one queued feasibility run. Public for deterministic tests."""
    with Session(engine) as session:
        now = datetime.utcnow()
        stale = session.exec(
            select(FeasibilityRun).where(
                FeasibilityRun.state == "running",
                FeasibilityRun.lease_expires_at.is_not(None),  # type: ignore[union-attr]
                FeasibilityRun.lease_expires_at < now,
            )
        ).all()
        for r in stale:
            r.state = "queued"
            r.lease_owner = None
            r.lease_expires_at = None
            session.add(r)
        if stale:
            session.commit()

        row = session.exec(
            select(FeasibilityRun).where(FeasibilityRun.state == "queued").order_by(FeasibilityRun.id)  # type: ignore[arg-type]
        ).first()
        if row is None:
            return False
        row.state = "running"
        row.lease_owner = worker_id
        row.lease_expires_at = now + timedelta(seconds=lease_ttl)
        row.attempts += 1
        session.add(row)
        session.commit()
        run_id = row.id

    with Session(engine) as session:
        try:
            await evaluate_run(session, run_id)
        except Exception as exc:
            logger.exception("feasibility run %s failed", run_id)
            run = session.get(FeasibilityRun, run_id)
            if run is not None:
                _fail_run(session, run, str(exc)[:500])
    return True
