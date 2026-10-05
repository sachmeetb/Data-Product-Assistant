"""Semantic-layer *discovery* orchestration.

A thin coordination layer on top of the three concept-building primitives —
``entity_scaffolding.scaffold`` (deterministic spine), the cross-product
recommender (``semantic_recommender`` + ``business_concepts.accept_from_recommendation``),
and ``entity_scaffolding.enrich`` (LLM polish). It adds the things the raw
primitives lack:

  * **Run tracking** — one ``:SemanticDiscoveryRun`` node per (domain, step),
    upserted on every run, recording status / timestamps / stats so the UI and
    MCP callers can answer "has this been run, and when?".
  * **Staleness** — a cheap, READ-ONLY fingerprint of the domain's data-product
    layer captured at run time. On status we recompute and compare; a step is
    *stale* when the sources changed since it last ran, or when an upstream step
    (reset / scaffold / recommend) completed after it.
  * **Stranded detection** — active concepts with no binding to a data product
    (entity w/o dataset, attribute w/o column, value w/o parent). Surfaced, not
    deleted (per the product decision: stranded is allowed but must be visible).
  * **Auto-promote** — promotes recommendation proposals at or above a
    confidence threshold into real, *bound + parented* concepts (the fix for the
    old "flat parentless attributes" behaviour); lower-confidence proposals stay
    ``pending`` (queued for human review).

The data-product layer is strictly READ-ONLY here — every write targets
``:BusinessConcept`` (+ its edges) or the ``:SemanticDiscoveryRun`` sidecar.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, Optional

from . import business_concepts as bc
from .models import AppSettings
from .neo4j_client import neo4j_session

# The canonical sequence. Order matters: scaffold lays the bound entity spine,
# recommend overlays cross-product unifications, enrich polishes names. ``reset``
# is the optional clean-slate that precedes the sequence.
STEPS = ["scaffold", "recommend", "enrich"]
ALL_STEPS = ["reset"] + STEPS

DEFAULT_CONFIDENCE_THRESHOLD = 0.7

# Relationship kinds that imply the proposed concept is an ATTRIBUTE of the
# named target entity (used to wire a HAS_ATTRIBUTE parent on auto-promote).
_ATTRIBUTE_OF_KINDS = {
    "attribute_of", "member_of", "determinant_of", "belongs_to", "part_of",
}


# ── Source fingerprint (staleness) ───────────────────────────────────────────

# READ-ONLY. Counts + the max contract version across the domain's data products.
# Counts catch add/remove of products/datasets/columns/mappings; currentVersion
# catches in-place column edits within an unchanged count.
_FINGERPRINT_QUERY = """
MATCH (p:Project)-[:HAS_CONTRACT]->(dc:DataContract)
WHERE coalesce(dc.isCurrent, true) = true AND p.domain = $domain
OPTIONAL MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
RETURN count(DISTINCT dp) AS products,
       count(DISTINCT ods) AS datasets,
       count(DISTINCT pc) AS columns,
       count(DISTINCT cm) AS mappings,
       coalesce(max(dc.currentVersion), 0) AS max_version,
       collect(DISTINCT dc.id) AS contract_ids
"""


def compute_fingerprint(settings: AppSettings, domain: str) -> tuple[dict[str, Any], str]:
    """Return ``(fingerprint_dict, hash)`` describing the domain's data-product
    layer right now. Pure read; never mutates."""
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        row = ns.run(_FINGERPRINT_QUERY, domain=domain).single()
    fp = {
        "products": (row or {}).get("products", 0) or 0,
        "datasets": (row or {}).get("datasets", 0) or 0,
        "columns": (row or {}).get("columns", 0) or 0,
        "mappings": (row or {}).get("mappings", 0) or 0,
        "max_version": (row or {}).get("max_version", 0) or 0,
        "contract_ids": sorted((row or {}).get("contract_ids", []) or []),
    }
    blob = json.dumps(fp, sort_keys=True)
    return fp, hashlib.sha1(blob.encode("utf-8")).hexdigest()


# ── Run tracking ─────────────────────────────────────────────────────────────

# One node per (domain, step). MERGE keeps the latest run state; we don't retain
# history (the product ask is "when was it last run").
_RECORD_RUN = """
MERGE (r:SemanticDiscoveryRun {uri: $uri})
ON CREATE SET r.createdAt = datetime()
SET r.domain = $domain,
    r.step = $step,
    r.status = $status,
    r.completedAt = datetime(),
    r.completedAtMs = timestamp(),
    r.durationMs = $duration_ms,
    r.triggeredBy = $triggered_by,
    r.statsJson = $stats_json,
    r.sourceFingerprintHash = $fp_hash,
    r.sourceFingerprintJson = $fp_json,
    r.error = $error
RETURN r.uri AS uri
"""

_READ_RUNS = """
MATCH (r:SemanticDiscoveryRun {domain: $domain})
RETURN r.step AS step,
       r.status AS status,
       toString(r.completedAt) AS completed_at,
       r.completedAtMs AS completed_at_ms,
       r.durationMs AS duration_ms,
       r.triggeredBy AS triggered_by,
       r.statsJson AS stats_json,
       r.sourceFingerprintHash AS fp_hash,
       r.error AS error
"""


def record_run(
    settings: AppSettings, *, domain: str, step: str, status: str,
    stats: Optional[dict[str, Any]] = None, duration_ms: int = 0,
    fp_hash: str = "", fp: Optional[dict[str, Any]] = None,
    triggered_by: str = "discovery", error: Optional[str] = None,
) -> None:
    """Upsert the run record for (domain, step)."""
    uri = f"discovery-run:{domain}:{step}"
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        ns.run(
            _RECORD_RUN,
            uri=uri, domain=domain, step=step, status=status,
            duration_ms=int(duration_ms or 0), triggered_by=triggered_by,
            stats_json=json.dumps(stats or {}),
            fp_hash=fp_hash, fp_json=json.dumps(fp or {}),
            error=error,
        ).consume()


def _read_runs(settings: AppSettings, domain: str) -> dict[str, dict[str, Any]]:
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(_READ_RUNS, domain=domain)]
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            r["stats"] = json.loads(r.pop("stats_json", None) or "{}") or {}
        except (TypeError, ValueError):
            r["stats"] = {}
        out[r["step"]] = r
    return out


# ── Concept counts + stranded detection ──────────────────────────────────────

_COUNT_CONCEPTS = """
MATCH (c:BusinessConcept)
WHERE c.domain = $domain AND coalesce(c.status, 'active') = 'active'
RETURN CASE WHEN c.level = 'super' THEN 'attribute' ELSE coalesce(c.level, 'attribute') END AS level,
       count(*) AS n
"""

# Stranded = active concept with no binding to the data-product layer:
#   entity    → no :REPRESENTED_BY to a :DProdOutputDataset
#   attribute → no :REPRESENTED_BY to a :DProdColumn
#   value     → no HAS_VALUE parent (orphan). Values bind via their parent
#               attribute's column, NOT directly — so a parented value is fine.
_STRANDED_QUERY = """
MATCH (c:BusinessConcept)
WHERE c.domain = $domain AND coalesce(c.status, 'active') = 'active'
WITH c, CASE WHEN c.level = 'super' THEN 'attribute' ELSE coalesce(c.level, 'attribute') END AS lvl
OPTIONAL MATCH (c)-[rep:REPRESENTED_BY]->()
// An ACTIVE parent (attribute's entity via HAS_ATTRIBUTE; value's attribute via
// HAS_VALUE). A concept whose only parent is deprecated is orphaned — invisible
// in the tree — exactly the shape the entity-clobber bug produced.
OPTIONAL MATCH (c)<-[:HAS_ATTRIBUTE]-(pa:BusinessConcept)
  WHERE coalesce(pa.status, 'active') = 'active'
OPTIONAL MATCH (c)<-[:HAS_VALUE]-(pv:BusinessConcept)
  WHERE coalesce(pv.status, 'active') = 'active'
WITH c, lvl, count(rep) AS reps, count(pa) AS active_attr_parents, count(pv) AS active_val_parents
WHERE (lvl = 'entity' AND reps = 0)
   OR (lvl = 'attribute' AND (reps = 0 OR active_attr_parents = 0))
   OR (lvl = 'value' AND active_val_parents = 0)
RETURN c.uri AS uri, c.name AS name, lvl AS level,
       toString(c.createdAt) AS created_at, c.createdBy AS created_by
ORDER BY CASE lvl WHEN 'entity' THEN 0 WHEN 'attribute' THEN 1 ELSE 2 END, name
"""


def count_concepts(settings: AppSettings, domain: str) -> dict[str, int]:
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(_COUNT_CONCEPTS, domain=domain)]
    out = {"entity": 0, "attribute": 0, "value": 0}
    for r in rows:
        out[r["level"]] = r["n"]
    out["total"] = sum(out.values())
    return out


def find_stranded(settings: AppSettings, domain: str) -> dict[str, Any]:
    """List active concepts with no data-product binding, with per-level counts."""
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(_STRANDED_QUERY, domain=domain)]
    counts = {"entity": 0, "attribute": 0, "value": 0}
    for r in rows:
        counts[r["level"]] = counts.get(r["level"], 0) + 1
    return {"items": rows, "counts": counts, "total": len(rows)}


# ── Status (assembled) ───────────────────────────────────────────────────────


def get_status(settings: AppSettings, domain: str) -> dict[str, Any]:
    """Assemble the discovery dashboard payload for a domain: per-step run state +
    staleness, current concept counts, and stranded concepts."""
    fp, fp_hash = compute_fingerprint(settings, domain)
    runs = _read_runs(settings, domain)

    # Completion timestamps for upstream-staleness comparison.
    def _ms(step: str) -> int:
        r = runs.get(step)
        return int((r or {}).get("completed_at_ms") or 0)

    steps_out: list[dict[str, Any]] = []
    for idx, step in enumerate(STEPS):
        run = runs.get(step)
        has_run = bool(run) and (run.get("status") in ("completed", "failed"))
        stale = False
        stale_reasons: list[str] = []
        if has_run:
            # Source data changed since this step ran.
            if run.get("fp_hash") and run["fp_hash"] != fp_hash:
                stale = True
                stale_reasons.append("sources_changed")
            # An upstream step (reset, or an earlier step in the sequence) ran
            # after this one — its output may invalidate ours.
            my_ms = _ms(step)
            upstream = ["reset"] + STEPS[:idx]
            if any(_ms(u) > my_ms for u in upstream if runs.get(u)):
                stale = True
                stale_reasons.append("upstream_rerun")
        steps_out.append({
            "step": step,
            "has_run": has_run,
            "status": (run or {}).get("status") if has_run else "never_run",
            "last_run_at": (run or {}).get("completed_at"),
            "duration_ms": (run or {}).get("duration_ms"),
            "triggered_by": (run or {}).get("triggered_by"),
            "stats": (run or {}).get("stats") or {},
            "error": (run or {}).get("error"),
            "stale": stale,
            "stale_reasons": stale_reasons,
        })

    reset_run = runs.get("reset")
    stranded = find_stranded(settings, domain)
    return {
        "domain": domain,
        "steps": steps_out,
        "concept_counts": count_concepts(settings, domain),
        "stranded": stranded,
        "source_fingerprint": fp,
        "last_reset_at": (reset_run or {}).get("completed_at"),
        "confidence_threshold": DEFAULT_CONFIDENCE_THRESHOLD,
    }


# ── Reset ────────────────────────────────────────────────────────────────────


def reset_domain(
    settings: AppSettings, domain: str, triggered_by: str = "discovery",
) -> dict[str, Any]:
    """Soft-deprecate every active concept in the domain (clean slate) and record
    the reset. Edges + nodes survive for audit; readers exclude deprecated."""
    t0 = time.time()
    deprecated = bc.deprecate_domain_concepts(
        settings, domain, reason="discovery reset: clear-out before re-running the sequence",
    )
    fp, fp_hash = compute_fingerprint(settings, domain)
    stats = {"deprecated": deprecated}
    record_run(
        settings, domain=domain, step="reset", status="completed",
        stats=stats, duration_ms=int((time.time() - t0) * 1000),
        fp_hash=fp_hash, fp=fp, triggered_by=triggered_by,
    )
    return {"domain": domain, "deprecated": deprecated}


# ── Auto-promote (the recommend acceptance fix) ──────────────────────────────

# Pending recs in the latest batch, with confidence, evidence-binding count, the
# suggested-relationship hints, and the modal source domain (for scoping).
_READ_PENDING_FOR_PROMOTE = """
MATCH (br:RecommendationBatch)
WITH br ORDER BY br.evaluatedAt DESC LIMIT 1
MATCH (br)-[:HAS_RECOMMENDATION]->(bcr:BusinessConceptRecommendation)
WHERE coalesce(bcr.status, 'pending') = 'pending'
WITH bcr
OPTIONAL MATCH (bcr)-[:EVIDENCED_BY]->(pc:DProdColumn)
      <-[:HAS_PRODUCT_COLUMN]-(:DProdOutputDataset)
      <-[:DPROD_OUTPUT_DATASET]-(:DProdOutputPort)
      <-[:DPROD_OUTPUT_PORT]-(:DProdDataProduct)
      <-[:MATERIALISES_AS]-(dc:DataContract)
WITH bcr, collect(DISTINCT dc.domain) AS domains_raw, count(DISTINCT pc) AS evidence_cols
RETURN bcr.uri AS rec_uri,
       bcr.conceptName AS name,
       coalesce(bcr.confidence, 0.0) AS confidence,
       bcr.suggestedRelationshipsJson AS suggested_relationships_json,
       evidence_cols,
       [d IN domains_raw WHERE d IS NOT NULL AND trim(d) <> ''] AS domains
ORDER BY bcr.confidence DESC, bcr.conceptName
"""

# Resolve a relationship-target name to an existing active ENTITY in the domain.
_RESOLVE_ENTITY_BY_NAME = """
MATCH (c:BusinessConcept)
WHERE c.domain = $domain
  AND toLower(c.name) = toLower($name)
  AND coalesce(c.status, 'active') = 'active'
  AND coalesce(c.level, 'attribute') = 'entity'
RETURN c.uri AS uri LIMIT 1
"""

# Primary parent resolver: the scaffolded entity that sits on the SAME dataset as
# the recommendation's evidence columns. Far more reliable than name-matching the
# advisor's free-text relationship targets — scaffold binds each entity to its
# dataset(s) (:REPRESENTED_BY) and the rec's evidence columns live in those
# datasets, so this maps a unified attribute back onto its home entity.
_RESOLVE_ENTITY_VIA_EVIDENCE = """
MATCH (bcr:BusinessConceptRecommendation {uri: $rec_uri})-[:EVIDENCED_BY]->(pc:DProdColumn)
MATCH (ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(pc)
MATCH (e:BusinessConcept {domain: $domain})-[:REPRESENTED_BY]->(ods)
WHERE coalesce(e.status, 'active') = 'active'
  AND coalesce(e.level, 'attribute') = 'entity'
RETURN e.uri AS uri, count(*) AS hits
ORDER BY hits DESC, e.uri
LIMIT 1
"""


def _resolve_parent_entity(
    ns, domain: str, rec_uri: str, suggested_relationships_json: Optional[str],
) -> Optional[str]:
    """Find the entity to parent a promoted attribute under. Strategy:

    1. The scaffolded entity bound to the dataset that holds the rec's evidence
       columns (reliable, structural).
    2. Fallback — the first 'attribute_of'-style suggested relationship whose
       target name resolves to an existing entity in the domain.
    """
    row = ns.run(_RESOLVE_ENTITY_VIA_EVIDENCE, domain=domain, rec_uri=rec_uri).single()
    if row and row.get("uri"):
        return row["uri"]
    try:
        rels = json.loads(suggested_relationships_json or "[]") or []
    except (TypeError, ValueError):
        rels = []
    for rel in rels:
        if not isinstance(rel, dict):
            continue
        kind = (rel.get("kind") or "").strip().lower()
        to_name = (rel.get("to_concept") or "").strip()
        if not to_name or kind not in _ATTRIBUTE_OF_KINDS:
            continue
        r2 = ns.run(_RESOLVE_ENTITY_BY_NAME, domain=domain, name=to_name).single()
        if r2:
            return r2["uri"]
    return None


def auto_promote_recommendations(
    settings: AppSettings, domain: str,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    triggered_by: str = "discovery",
) -> dict[str, Any]:
    """Promote pending recommendations for ``domain`` whose confidence ≥
    ``threshold`` into bound + parented attributes; queue the rest.

    A proposal is *queued* (left pending) when: confidence is below threshold;
    it has no evidence column that would bind it (promoting would strand it); or
    its evidence resolves to a different domain. Promoted attributes default
    their column bindings to the recommendation's evidence columns and are wired
    under an existing entity when a suggested relationship names one.
    """
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(_READ_PENDING_FOR_PROMOTE)]

    promoted: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for row in rows:
        inferred = bc._modal_domain(row.get("domains") or [])
        # Only act on recs whose evidence belongs to THIS domain. Others belong
        # to their own domain's discovery run.
        if inferred != domain:
            continue
        conf = float(row.get("confidence") or 0.0)
        name = row.get("name")
        rec_uri = row["rec_uri"]
        if conf < threshold:
            queued.append({"rec_uri": rec_uri, "name": name, "confidence": conf,
                           "reason": "below_threshold"})
            continue
        if int(row.get("evidence_cols") or 0) == 0:
            queued.append({"rec_uri": rec_uri, "name": name, "confidence": conf,
                           "reason": "no_evidence_binding"})
            continue
        # Resolve a parent entity (best-effort) within a short-lived session.
        with neo4j_session(
            settings.neo4j_host, settings.neo4j_port,
            settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
        ) as ns2:
            parent_uri = _resolve_parent_entity(
                ns2, domain, rec_uri, row.get("suggested_relationships_json"))
        try:
            res = bc.accept_from_recommendation(
                settings, rec_uri=rec_uri, domain=domain, level="attribute",
                parent_uri=parent_uri, if_exists="deprecate_existing",
                created_by="discovery-recommender",
            )
            promoted.append({
                "rec_uri": rec_uri, "name": name, "confidence": conf,
                "concept_uri": res.get("uri"), "parent_uri": parent_uri,
                "values_created": len(res.get("values_created") or []),
                # A promotion that coreferenced an existing higher-tier concept
                # (e.g. the 'Employee' entity) — reinforced, not clobbered.
                "coreferenced": bool(res.get("coreferenced")),
                # Any existing SAME-tier concept this promotion deprecated. Should
                # be ~0 now; a non-zero total surfaces an unexpected clobber.
                "deprecated_existing": bool(res.get("deprecated_existing_uri")),
            })
        except Exception as e:  # noqa: BLE001 — one bad rec shouldn't kill the batch
            failures.append({"rec_uri": rec_uri, "name": name, "error": str(e)})

    coreferenced = sum(1 for p in promoted if p.get("coreferenced"))
    deprecated = sum(1 for p in promoted if p.get("deprecated_existing"))
    return {
        "promoted": promoted, "queued": queued, "failures": failures,
        "threshold": threshold,
        "counts": {"promoted": len(promoted), "queued": len(queued),
                   "failures": len(failures), "coreferenced": coreferenced,
                   "deprecated_existing": deprecated},
    }


# ── Step orchestration (shared by the router + MCP) ──────────────────────────


async def run_recommender_pass(settings: AppSettings, trigger: str = "discovery") -> dict[str, Any]:
    """Gather the cross-project snapshot → call the advisor → persist a
    :RecommendationBatch + per-concept :BusinessConceptRecommendation nodes.
    Returns the persisted summary (always succeeds; advisor_error captures any
    LLM failure)."""
    from . import semantic_recommender as recommender

    inputs = recommender.gather_inputs(settings)
    if not inputs.products:
        persisted = recommender.persist_recommendations(
            settings, recommender.AdvisorPayload(
                narrative="No products with columns in the portfolio yet.", concepts=[]),
            inputs, advisor_error=None, triggered_by=trigger,
        )
        return {
            "batch_uri": persisted["batch_uri"], "batch_id": persisted["batch_id"],
            "narrative": "No products with columns in the portfolio yet.",
            "concepts": [], "products_considered": 0, "advisor_error": None,
        }
    try:
        payload, advisor_error = await asyncio.wait_for(
            recommender.run_advisor(inputs),
            timeout=recommender.ADVISOR_TIMEOUT_SECONDS + 15,
        )
    except asyncio.TimeoutError:
        payload = recommender.AdvisorPayload(narrative="", concepts=[])
        advisor_error = f"Advisor timed out after {recommender.ADVISOR_TIMEOUT_SECONDS}s"
    persisted = recommender.persist_recommendations(
        settings, payload, inputs, advisor_error, triggered_by=trigger,
    )
    return {
        "batch_uri": persisted["batch_uri"], "batch_id": persisted["batch_id"],
        "narrative": payload.narrative or "", "concepts": payload.concepts or [],
        "products_considered": len(inputs.products), "advisor_error": advisor_error,
    }


async def run_step(
    settings: AppSettings, domain: str, step: str, *,
    dry_run: bool = False, confidence_threshold: Optional[float] = None,
    triggered_by: str = "ui",
) -> dict[str, Any]:
    """Run one discovery step, recording a :SemanticDiscoveryRun (except a
    scaffold dry-run, which only previews). Raises ValueError on a bad step.

    Returns ``{step, domain, stats, result}``; for a scaffold dry-run returns the
    raw preview model instead (so callers can show the entity/attribute counts
    before applying)."""
    from . import entity_scaffolding

    domain = (domain or "").strip()
    step = (step or "").strip()
    if not domain:
        raise ValueError("domain is required")
    if step not in STEPS:
        raise ValueError(f"step must be one of {STEPS}")

    t0 = time.time()
    try:
        if step == "scaffold":
            res = entity_scaffolding.scaffold(settings, domain, dry_run=dry_run)
            if dry_run:
                return res  # preview — no run recorded
            stats = dict(res.get("counts") or {})
            stats["deprecated_existing"] = res.get("deprecated_existing", 0)

        elif step == "recommend":
            threshold = (
                confidence_threshold if confidence_threshold is not None
                else DEFAULT_CONFIDENCE_THRESHOLD
            )
            rec = await run_recommender_pass(settings, trigger="discovery")
            promo = auto_promote_recommendations(
                settings, domain, threshold=threshold, triggered_by=triggered_by)
            stats = {
                "batch_id": rec.get("batch_id"),
                "products_considered": rec.get("products_considered"),
                "advisor_error": rec.get("advisor_error"),
                "proposals": len(rec.get("concepts") or []),
                "promoted": promo["counts"]["promoted"],
                "queued": promo["counts"]["queued"],
                "failures": promo["counts"]["failures"],
                # Observability: promotions that coreferenced an existing entity
                # (reinforced, not clobbered) + any that deprecated a same-tier
                # concept. deprecated_existing should stay ~0 after the tier fix.
                "coreferenced": promo["counts"].get("coreferenced", 0),
                "deprecated_existing": promo["counts"].get("deprecated_existing", 0),
                "threshold": threshold,
            }
            res = {"recommend": rec, "auto_promote": promo}

        else:  # enrich
            res = await entity_scaffolding.enrich(settings, domain)
            stats = {"enriched": (res or {}).get("enriched", 0)}
    except Exception as e:
        fp, fp_hash = compute_fingerprint(settings, domain)
        record_run(
            settings, domain=domain, step=step, status="failed",
            duration_ms=int((time.time() - t0) * 1000),
            fp_hash=fp_hash, fp=fp, triggered_by=triggered_by, error=str(e),
        )
        raise

    fp, fp_hash = compute_fingerprint(settings, domain)
    record_run(
        settings, domain=domain, step=step, status="completed", stats=stats,
        duration_ms=int((time.time() - t0) * 1000),
        fp_hash=fp_hash, fp=fp, triggered_by=triggered_by,
    )
    return {"step": step, "domain": domain, "stats": stats, "result": res}
