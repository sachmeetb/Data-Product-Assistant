"""Pre-flight coherence checks for a consumer-aligned data product.

Two heuristic checks that surface drift between authoring surfaces BEFORE
the engineer materialises + deploys the product. Both are pure-Python
heuristics (no LLM); they run cheaply and deterministically off the
contract graph state. Designed to mount on the wizard's Step 7
(Readiness Review) alongside the OSI + QA panels.

Checks:

1. **Grain coherence** — does the dataset's grainProse name concepts that
   actually have corresponding schema columns? Catches the failure mode
   where the PO writes "weekly roster with units sold, weekly revenue,
   intra-category rank" but the schema authoring step doesn't end up
   with columns for those concepts. Reflection skill catches this
   post-deploy; this catches it pre-deploy.

2. **Grouping coherence** — when ``:DatasetTransform.groupingKeysJson``
   is non-empty, every non-grouping non-PK non-suppressed column MUST
   carry an aggregateFunction (either on ``:DProdColumn`` or on its
   ``:ColumnMapping``). Without it, ``generate_view_ddl.py``'s grouped
   CTE falls back to ``MAX(...)`` per `_assemble_view_ddl:2491-2517` and
   silently emits the wrong aggregate — wrong numbers, no error.

Both checks degrade gracefully when the contract hasn't been
materialised yet (no :DProdColumn rows) — the response notes the
unmateralised state instead of failing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from ..database import get_session
from ..models import Project
from ..neo4j_client import neo4j_session


router = APIRouter(tags=["preflight"])


# ── Helpers ──────────────────────────────────────────────────────────────


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _contract_id(project: Project) -> str:
    return f"{project.project_code}-contract"


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host,
        project.neo4j_port,
        project.neo4j_user,
        project.neo4j_password,
        project.neo4j_database,
    )


# ── Tokenisation (shared between checks) ─────────────────────────────────


# Mirrors ingest_products._SIMILARITY_STOPWORDS but kept local so this
# router has no cross-router import dependency. Narrow on purpose —
# domain-meaningful nouns like "product"/"customer" stay in the token
# stream because at column granularity they're disambiguating signal
# (`product.name` vs `customer.name`).
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "to", "from", "with",
    "by", "on", "in", "at", "is", "are", "was", "were", "be", "been",
    "this", "that", "these", "those", "as", "it", "its", "we", "our",
    "all", "any", "row", "rows", "one", "per", "each", "every", "some",
    "many", "no", "not", "has", "have", "had", "will", "should", "would",
    "can", "may", "shown", "show", "include", "includes", "including",
})

# Words inside a noun phrase that signal an aggregate / windowed / period
# concept. Their presence implies the schema column they describe should
# also carry an aggregateFunction or grouping role — used by grain
# coherence to emit `matched_with_drift` when the matched column doesn't
# back the implication.
_AGGREGATE_HINT_WORDS = frozenset({
    "weekly", "monthly", "daily", "yearly", "quarterly", "annual",
    "annually", "rolling", "trailing", "cumulative", "running", "total",
    "sum", "average", "mean", "count", "distinct", "rank", "percentile",
    "growth", "change", "delta", "ytd", "mtd", "ytd-over-ytd",
    "year-over-year", "yoy", "mom", "wow",
})


def _tokenise(text: Any) -> set[str]:
    """Word-boundary tokens minus stopwords; camelCase + snake_case both
    split. Minimum length 2 keeps short tokens like ``id``."""
    if not text:
        return set()
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text))
    tokens = re.findall(r"[a-z][a-z0-9]{1,}", s.lower())
    return {t for t in tokens if t not in _STOPWORDS}


def _snake_case(phrase: str) -> str:
    """``"weekly revenue"`` → ``"weekly_revenue"``. Used to test for
    direct snake_case column-name matches before falling back to
    token-overlap. Hyphens become underscores so ``"intra-category
    rank"`` → ``"intra_category_rank"``."""
    s = re.sub(r"[^a-z0-9]+", "_", phrase.lower())
    return s.strip("_")


# ── Grain coherence check ────────────────────────────────────────────────


# Phrase extraction: walks grainProse and pulls consecutive 1-3 token
# runs that aren't broken by punctuation, stopwords, or parenthetical
# asides. Approximates noun-phrase extraction without a parser. The
# tuned heuristics here (max 4 tokens, parentheses stripped, hyphenated
# tokens kept whole) match the shape of grain prose the wizard actually
# produces.
_PHRASE_SPLIT_RE = re.compile(r"[.,;:()\[\]{}!?]")


def _extract_noun_phrases(prose: str) -> list[str]:
    """Pull candidate noun phrases from grain prose. Each phrase is up
    to 4 consecutive non-stopword tokens (hyphens preserved within a
    token, so ``"intra-category rank"`` stays as a two-token phrase).
    Deduplicated case-insensitively; ordered by first appearance."""
    if not prose:
        return []
    out: list[str] = []
    seen: set[str] = set()
    # Strip parenthetical asides first — they're usually meta-context
    # like "(pre-resolved from SCD-2 price history)" that doesn't name
    # a schema concept.
    cleaned = re.sub(r"\([^)]*\)", " ", prose)
    for fragment in _PHRASE_SPLIT_RE.split(cleaned):
        tokens = re.findall(r"[a-z][a-z0-9\-]*", fragment.lower())
        # Slide a window over the fragment, keeping runs of non-stopword
        # tokens up to length 4.
        run: list[str] = []
        for t in tokens + [""]:  # sentinel triggers flush at end
            if t and t not in _STOPWORDS and len(t) >= 2:
                run.append(t)
                continue
            # Flush all 1..min(4,len) length sub-phrases anchored at the
            # END of the run — so "the weekly revenue" yields "revenue",
            # "weekly revenue", but not "weekly" alone (less useful).
            n = len(run)
            for size in range(min(n, 4), 0, -1):
                phrase = " ".join(run[n - size : n])
                key = phrase.lower()
                if key not in seen and size >= 1:
                    # Skip single-token phrases that are pure aggregate
                    # hints — "weekly" alone is meaningless; "weekly
                    # revenue" is the useful unit.
                    if size == 1 and phrase in _AGGREGATE_HINT_WORDS:
                        continue
                    seen.add(key)
                    out.append(phrase)
            run = []
    return out


def _match_phrase_to_columns(
    phrase: str,
    columns: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Find the best-matching column for one grain-prose phrase, or
    None. Two tiers:

    Tier A (direct): snake_cased phrase matches a column's name exactly.
    Tier B (loose): phrase token set ⊆ column name+description token
                    set, with ≥1 token overlap on meaningful (non-stop,
                    non-aggregate-hint) words.

    For ties the column with more overlapping tokens wins; further ties
    prefer the column whose own token set is shorter (more specific).
    """
    target_norm = _snake_case(phrase)
    for col in columns:
        if (col.get("name") or "").lower() == target_norm:
            return {"column": col, "tier": "A"}

    phrase_tokens = _tokenise(phrase)
    meaningful = phrase_tokens - _AGGREGATE_HINT_WORDS
    if not meaningful:
        return None

    # Track (score, -col_size, idx) so the comparison never reaches the
    # column dict itself (dicts aren't orderable in Python 3).
    best: Optional[tuple[int, int, int]] = None
    for idx, col in enumerate(columns):
        col_tokens = _tokenise(f"{col.get('name') or ''} {col.get('description') or ''}")
        overlap = phrase_tokens & col_tokens
        meaningful_overlap = overlap - _AGGREGATE_HINT_WORDS
        if not meaningful_overlap:
            continue
        score = len(meaningful_overlap)
        col_size = len(col_tokens)
        candidate = (score, -col_size, idx)
        if best is None or candidate > best:
            best = candidate

    if best is None:
        return None
    return {"column": columns[best[2]], "tier": "B"}


def _check_grain_coherence(
    grain_prose: str,
    columns: list[dict[str, Any]],
) -> dict[str, Any]:
    """For each noun phrase in ``grain_prose``, decide whether the
    schema covers it. Tagged outputs:

      - ``matched``            — exact-name column match
      - ``matched_with_drift`` — column found but its aggregate-role
                                 metadata contradicts the phrase's
                                 implication (e.g. phrase says "weekly
                                 X" but the column has no
                                 aggregateFunction and isn't grouped)
      - ``unmatched``          — no plausible column
    """
    if not (grain_prose or "").strip():
        return {
            "verdict": "no_grain_prose",
            "concepts": [],
            "summary": "No grain prose authored — coherence check skipped.",
        }
    phrases = _extract_noun_phrases(grain_prose)
    if not phrases:
        return {
            "verdict": "coherent",
            "concepts": [],
            "summary": "Grain prose has no extractable noun phrases.",
        }

    concepts: list[dict[str, Any]] = []
    matched_count = 0
    drift_count = 0
    unmatched_count = 0
    for phrase in phrases:
        match = _match_phrase_to_columns(phrase, columns)
        if match is None:
            concepts.append({
                "phrase": phrase,
                "status": "unmatched",
                "matched_column": None,
                "rationale": "No schema column shares meaningful tokens with this phrase.",
            })
            unmatched_count += 1
            continue
        col = match["column"]
        col_name = col.get("name") or ""
        # Drift detection: phrase implies an aggregate / grouping role
        # but the matched column doesn't carry one.
        phrase_tokens = set(re.findall(r"[a-z][a-z0-9\-]*", phrase.lower()))
        implies_aggregate = bool(phrase_tokens & _AGGREGATE_HINT_WORDS)
        has_aggregate = bool(
            (col.get("aggregate_function") or "").strip()
            or col.get("grouping_key")
        )
        if implies_aggregate and not has_aggregate and not col.get("is_primary_key"):
            concepts.append({
                "phrase": phrase,
                "status": "matched_with_drift",
                "matched_column": col_name,
                "rationale": (
                    f"Matched column '{col_name}' but the phrase implies an aggregate / "
                    f"period role and the column has no aggregateFunction and is not a "
                    f"grouping key. The grouped CTE will fall back to MAX(...)."
                ),
            })
            drift_count += 1
        else:
            concepts.append({
                "phrase": phrase,
                "status": "matched",
                "matched_column": col_name,
                "rationale": (
                    "Direct name match." if match["tier"] == "A"
                    else f"Token overlap with column '{col_name}'."
                ),
            })
            matched_count += 1

    if unmatched_count or drift_count:
        verdict = "drift"
    else:
        verdict = "coherent"

    parts = [f"{matched_count} matched"]
    if drift_count:
        parts.append(f"{drift_count} drift")
    if unmatched_count:
        parts.append(f"{unmatched_count} unmatched")
    summary = " · ".join(parts) + f" (of {len(phrases)} concept phrases)."
    return {
        "verdict": verdict,
        "concepts": concepts,
        "summary": summary,
    }


# ── Grouping coherence check ─────────────────────────────────────────────


def _check_grouping_coherence(
    dataset_transform: dict[str, Any],
    columns: list[dict[str, Any]],
) -> dict[str, Any]:
    """When ``grouping_keys`` is non-empty, every non-grouping non-PK
    non-suppressed column MUST carry an aggregateFunction (either on
    ``:DProdColumn`` or on its mapping). Without it the grouped CTE
    silently emits ``MAX(...)`` and the view returns wrong aggregates.
    """
    grouping_keys = dataset_transform.get("grouping_keys") or []
    if not grouping_keys:
        return {
            "verdict": "no_grouping",
            "issues": [],
            "summary": "Dataset has no grouping keys — check skipped.",
        }

    suppressed = set(dataset_transform.get("suppressed_columns") or [])
    grouping_set = set(grouping_keys)

    issues: list[dict[str, Any]] = []
    ok_count = 0
    for col in columns:
        name = col.get("name") or ""
        if name in grouping_set or name in suppressed or col.get("is_primary_key"):
            ok_count += 1
            continue
        agg_pc = (col.get("aggregate_function") or "").strip()
        agg_cm = (col.get("mapping_aggregate_function") or "").strip()
        if agg_pc or agg_cm:
            ok_count += 1
            continue
        issues.append({
            "column": name,
            "missing": "aggregate_function",
            "rationale": (
                f"Dataset has grouping_keys={list(grouping_set)} but column '{name}' "
                f"is neither a grouping key, a primary key, nor in suppressedColumns, "
                f"and has no aggregateFunction on either :DProdColumn or its "
                f":ColumnMapping. The grouped CTE will default to MAX(...) and produce "
                f"the wrong aggregate."
            ),
            "remediation": (
                f"Set aggregateFunction (SUM / COUNT / COUNT_DISTINCT / AVG / MIN / MAX) "
                f"on the column in wizard Step 4, or add '{name}' to suppressedColumns "
                f"if it shouldn't be in the projected view."
            ),
        })

    if issues:
        return {
            "verdict": "drift",
            "issues": issues,
            "summary": (
                f"{len(issues)} non-grouping column(s) missing aggregateFunction "
                f"(of {len(columns)} total)."
            ),
        }
    return {
        "verdict": "coherent",
        "issues": [],
        "summary": (
            f"All {ok_count} columns are either grouping keys, PKs, suppressed, or "
            f"carry an aggregateFunction."
        ),
    }


# ── Graph read ───────────────────────────────────────────────────────────


# One query per output dataset: pulls the dataset's :DatasetTransform
# fields (grain / grouping / suppression) AND all :DProdColumn metadata
# (name, type, PK flag, column-level aggregate role) AND the most recent
# :ColumnMapping per column (for the mapping-level aggregateFunction
# fallback). One row per (dataset, column), collected per-dataset in the
# caller.
_READ_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $product_uri})
      -[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (dt:DatasetTransform {schemaPhysicalName: coalesce(ods.physicalName, ods.name),
                                     contractId: $contract_id})
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (cm:ColumnMapping {isCurrent: true})-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
RETURN coalesce(ods.physicalName, ods.name)            AS dataset_name,
       ods.uri                                         AS dataset_uri,
       coalesce(dt.grainProse, '')                     AS grain_prose,
       coalesce(dt.groupingKeysJson, '[]')             AS grouping_keys_json,
       coalesce(dt.suppressedColumnsJson, '[]')        AS suppressed_columns_json,
       coalesce(dt.scdPolicyJson, '{}')                AS scd_policy_json,
       pc.name                                         AS col_name,
       pc.dataType                                     AS col_data_type,
       coalesce(pc.description, '')                    AS col_description,
       coalesce(pc.isPrimaryKey, false)                AS col_is_pk,
       coalesce(pc.aggregateFunction, '')              AS col_aggregate_function,
       coalesce(pc.groupingKey, false)                 AS col_grouping_key,
       coalesce(cm.aggregateFunction, '')              AS mapping_aggregate_function
ORDER BY dataset_name, col_name
"""


def _read_preflight_state(project: Project, contract_id: str) -> list[dict[str, Any]]:
    """Return one dict per output dataset, each carrying its
    :DatasetTransform fields + the list of :DProdColumns + per-column
    mapping aggregate fallback. Empty list when the product isn't
    materialised yet."""
    product_uri = f"dprod:{contract_id}"
    with _neo4j(project) as ns:
        rows = [dict(r) for r in ns.run(
            _READ_QUERY, product_uri=product_uri, contract_id=contract_id,
        )]
    # Each (dataset, column) can appear multiple times when a column has
    # multiple mapping rows (e.g. margin_pct ← list_price + cost is two
    # rows). Dedupe per (dataset_name, col_name); if any mapping carries
    # an aggregateFunction, keep that signal — coherence cares whether
    # the column has an aggregate role anywhere, not on every row.
    by_dataset: dict[str, dict[str, Any]] = {}
    seen_cols: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        name = r.get("dataset_name") or ""
        if not name:
            continue
        if name not in by_dataset:
            by_dataset[name] = {
                "dataset_name": name,
                "dataset_uri": r.get("dataset_uri"),
                "grain_prose": r.get("grain_prose") or "",
                "grouping_keys": _safe_json_list(r.get("grouping_keys_json")),
                "suppressed_columns": _safe_json_list(r.get("suppressed_columns_json")),
                "scd_policy": _safe_json_dict(r.get("scd_policy_json")),
                "columns": [],
            }
        col_name = r.get("col_name")
        if not col_name:
            continue
        key = (name, col_name)
        existing = seen_cols.get(key)
        if existing is None:
            entry = {
                "name": col_name,
                "data_type": r.get("col_data_type") or "",
                "description": r.get("col_description") or "",
                "is_primary_key": bool(r.get("col_is_pk")),
                "aggregate_function": r.get("col_aggregate_function") or "",
                "grouping_key": bool(r.get("col_grouping_key")),
                "mapping_aggregate_function": r.get("mapping_aggregate_function") or "",
            }
            seen_cols[key] = entry
            by_dataset[name]["columns"].append(entry)
        else:
            # Promote aggregate signal if a later mapping row carries one
            # the first didn't. Conservative — we want the strongest
            # available signal so a column doesn't look under-aggregated
            # just because the first joined mapping row was empty.
            if not existing["mapping_aggregate_function"]:
                existing["mapping_aggregate_function"] = r.get("mapping_aggregate_function") or ""
    return list(by_dataset.values())


def _safe_json_list(s: Any) -> list[Any]:
    try:
        v = json.loads(s) if s else []
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _safe_json_dict(s: Any) -> dict[str, Any]:
    try:
        v = json.loads(s) if s else {}
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# ── Endpoint ─────────────────────────────────────────────────────────────


@router.post("/api/projects/{project_id}/preflight")
def run_preflight(project_id: int, session: Session = Depends(get_session)):
    """Run both coherence checks for every output dataset on the
    project's contract head.

    Returns ``{datasets: [{dataset_name, grain_coherence, grouping_coherence}],
    overall_verdict: 'coherent' | 'drift' | 'unmateralised'}``.

    Graceful degradation: when the contract hasn't been materialised
    yet (no :DProdColumn rows), returns ``overall_verdict =
    'unmateralised'`` with an empty datasets list instead of failing.
    """
    project = _get_project(project_id, session)
    contract_id = _contract_id(project)
    datasets = _read_preflight_state(project, contract_id)
    if not datasets:
        return {
            "overall_verdict": "unmateralised",
            "datasets": [],
            "summary": (
                "Contract has no materialised output datasets yet. Run "
                "odcs_to_dprod (or publish for the first time) before running "
                "pre-flight checks."
            ),
        }

    out: list[dict[str, Any]] = []
    any_drift = False
    for ds in datasets:
        grain = _check_grain_coherence(ds["grain_prose"], ds["columns"])
        grouping = _check_grouping_coherence(
            {
                "grouping_keys": ds["grouping_keys"],
                "suppressed_columns": ds["suppressed_columns"],
            },
            ds["columns"],
        )
        if grain["verdict"] == "drift" or grouping["verdict"] == "drift":
            any_drift = True
        out.append({
            "dataset_name": ds["dataset_name"],
            "dataset_uri": ds["dataset_uri"],
            "column_count": len(ds["columns"]),
            "grouping_keys": ds["grouping_keys"],
            "suppressed_columns": ds["suppressed_columns"],
            "grain_coherence": grain,
            "grouping_coherence": grouping,
        })

    return {
        "overall_verdict": "drift" if any_drift else "coherent",
        "datasets": out,
        "summary": (
            f"Checked {len(out)} dataset(s); "
            f"{'drift detected' if any_drift else 'no drift detected'}."
        ),
    }
