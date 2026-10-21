from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query as QParam
from sqlmodel import Session

from ..database import get_session
from ..graph_ops import has_project_node
from ..models import Project
from ..neo4j_client import neo4j_session

router = APIRouter(prefix="/api/projects/{project_id}/scoring", tags=["scoring"])

# ── Scoping prefix ──────────────────────────────────────────────────────────

_PRJ_DS = (
    "MATCH (:Project {projectCode: $project_code})"
    "-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->"
)

# ── Queries: latest batch ────────────────────────────────────────────────────

LATEST_BATCH = """\
MATCH (ds:Dataset)-[:HAS_QUALITY_SCORE]->(qs:QualityScore)
WHERE qs.level = 'dataset' AND qs.dimension = 'composite'
RETURN qs.batchId AS batch_id, qs.scoredAt AS scored_at
ORDER BY qs.scoredAt DESC LIMIT 1
"""
LATEST_BATCH_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_QUALITY_SCORE]->(qs:QualityScore)
WHERE qs.level = 'dataset' AND qs.dimension = 'composite'
RETURN qs.batchId AS batch_id, qs.scoredAt AS scored_at
ORDER BY qs.scoredAt DESC LIMIT 1
"""

# ── Queries: dataset-level scores for a batch ────────────────────────────────

DATASET_SCORES = """\
MATCH (ds:Dataset)-[:HAS_QUALITY_SCORE]->(qs:QualityScore {batchId: $batch_id})
WHERE qs.level = 'dataset'
RETURN ds.uri AS dataset_uri, ds.name AS dataset_name, ds.schema AS schema,
       qs.dimension AS dimension, qs.score AS score, qs.evidence AS evidence
ORDER BY ds.schema, ds.name, qs.dimension
"""
DATASET_SCORES_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_QUALITY_SCORE]->(qs:QualityScore {{batchId: $batch_id}})
WHERE qs.level = 'dataset'
RETURN ds.uri AS dataset_uri, ds.name AS dataset_name, ds.schema AS schema,
       qs.dimension AS dimension, qs.score AS score, qs.evidence AS evidence
ORDER BY ds.schema, ds.name, qs.dimension
"""

# ── Queries: overall scores for a batch ──────────────────────────────────────

OVERALL_SCORES = """\
MATCH (cat:Catalog)-[:HAS_QUALITY_SCORE]->(qs:QualityScore {batchId: $batch_id})
WHERE qs.level = 'overall'
RETURN cat.name AS catalog_name, qs.dimension AS dimension, qs.score AS score,
       qs.evidence AS evidence
ORDER BY qs.dimension
"""
OVERALL_SCORES_S = f"""\
MATCH (:Project {{projectCode: $project_code}})-[:HAS_CATALOG]->(cat:Catalog)
MATCH (cat)-[:HAS_QUALITY_SCORE]->(qs:QualityScore {{batchId: $batch_id}})
WHERE qs.level = 'overall'
RETURN cat.name AS catalog_name, qs.dimension AS dimension, qs.score AS score,
       qs.evidence AS evidence
ORDER BY qs.dimension
"""

# ── Queries: column-level scores ─────────────────────────────────────────────

COLUMN_SCORES = """\
MATCH (ds:Dataset {uri: $dataset_uri})-[:HAS_COLUMN]->(col:Column)
       -[:HAS_QUALITY_SCORE]->(qs:QualityScore {batchId: $batch_id})
WHERE qs.level = 'column'
RETURN col.uri AS col_uri, col.name AS col_name, col.ordinal AS ordinal,
       qs.dimension AS dimension, qs.score AS score
ORDER BY col.ordinal, qs.dimension
"""

# ── Queries: trend ───────────────────────────────────────────────────────────

TREND = """\
MATCH (ds:Dataset)-[:HAS_QUALITY_SCORE]->(qs:QualityScore)
WHERE qs.level = 'dataset' AND qs.dimension = 'composite'
RETURN qs.batchId AS batch_id, qs.scoredAt AS scored_at,
       ds.name AS dataset_name, qs.score AS score
ORDER BY qs.scoredAt ASC, ds.name
"""
TREND_S = f"""\
{_PRJ_DS}(ds:Dataset)-[:HAS_QUALITY_SCORE]->(qs:QualityScore)
WHERE qs.level = 'dataset' AND qs.dimension = 'composite'
RETURN qs.batchId AS batch_id, qs.scoredAt AS scored_at,
       ds.name AS dataset_name, qs.score AS score
ORDER BY qs.scoredAt ASC, ds.name
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _neo4j(project: Project):
    return neo4j_session(
        project.neo4j_host, project.neo4j_port,
        project.neo4j_user, project.neo4j_password, project.neo4j_database,
    )


def _pick(scoped: bool, q_scoped: str, q_global: str) -> str:
    return q_scoped if scoped else q_global


def _run_query(project, query, **params):
    with _neo4j(project) as ns:
        result = ns.run(query, **params)
        return [dict(r) for r in result]


def _pivot_dataset_scores(rows: list[dict]) -> list[dict]:
    """Pivot dimension rows into one dict per dataset with scores + evidence."""
    datasets: dict[str, dict] = {}
    for row in rows:
        uri = row["dataset_uri"]
        if uri not in datasets:
            datasets[uri] = {
                "uri": uri,
                "name": row["dataset_name"],
                "schema": row["schema"],
                "evidence": {},
            }
        datasets[uri][row["dimension"]] = row["score"]
        if row.get("evidence"):
            datasets[uri]["evidence"][row["dimension"]] = row["evidence"]
    return list(datasets.values())


def _pivot_overall_scores(rows: list[dict]) -> tuple[dict, dict]:
    """Pivot overall dimension rows into (scores, evidence) dicts."""
    scores: dict = {}
    evidence: dict = {}
    for row in rows:
        scores[row["dimension"]] = row["score"]
        if row.get("evidence"):
            evidence[row["dimension"]] = row["evidence"]
    return scores, evidence


def _compute_tier(overall: dict, evidence: dict) -> int:
    """Tier 3 iff grounding dimension populated; Tier 2 iff documentation;
    Tier 1 iff any test-evidenced dimension; else 1 as implicit baseline."""
    if (overall.get("grounding") or 0) > 0:
        return 3
    if (overall.get("documentation") or 0) > 0:
        return 2
    if "test" in evidence.values():
        return 1
    return 1


_TIER_LABELS = {
    1: "Internal consistency",
    2: "Shared meaning",
    3: "External grounding",
}


def _pivot_column_scores(rows: list[dict]) -> list[dict]:
    """Pivot column dimension rows into one dict per column."""
    columns: dict[str, dict] = {}
    for row in rows:
        uri = row["col_uri"]
        if uri not in columns:
            columns[uri] = {
                "uri": uri,
                "name": row["col_name"],
                "ordinal": row["ordinal"],
            }
        columns[uri][row["dimension"]] = row["score"]
    return sorted(columns.values(), key=lambda c: c.get("ordinal", 0))


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("")
def get_scores(project_id: int, session: Session = Depends(get_session)):
    """Get the latest scoring batch with dataset-level and overall scores."""
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}

    try:
        # Find latest batch
        batch_rows = _run_query(
            project, _pick(scoped, LATEST_BATCH_S, LATEST_BATCH), **pc
        )
        if not batch_rows:
            return {"batch_id": None, "scored_at": None, "overall": {}, "datasets": []}

        batch_id = batch_rows[0]["batch_id"]
        scored_at = batch_rows[0]["scored_at"]

        # Dataset scores
        ds_rows = _run_query(
            project,
            _pick(scoped, DATASET_SCORES_S, DATASET_SCORES),
            batch_id=batch_id, **pc,
        )
        datasets = _pivot_dataset_scores(ds_rows)

        # Overall scores
        overall_rows = _run_query(
            project,
            _pick(scoped, OVERALL_SCORES_S, OVERALL_SCORES),
            batch_id=batch_id, **pc,
        )
        overall, overall_evidence = _pivot_overall_scores(overall_rows)
        tier = _compute_tier(overall, overall_evidence)

        return {
            "batch_id": batch_id,
            "scored_at": str(scored_at) if scored_at else None,
            "overall": overall,
            "overall_evidence": overall_evidence,
            "datasets": datasets,
            "tier": tier,
            "tier_label": _TIER_LABELS[tier],
        }
    except Exception as e:
        raise HTTPException(500, f"Neo4j query failed: {e}")


@router.get("/dataset/{dataset_uri:path}")
def get_dataset_scores(
    project_id: int,
    dataset_uri: str,
    session: Session = Depends(get_session),
):
    """Get column-level scores for a specific dataset."""
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}

    try:
        batch_rows = _run_query(
            project, _pick(scoped, LATEST_BATCH_S, LATEST_BATCH), **pc
        )
        if not batch_rows:
            return {"batch_id": None, "columns": []}

        batch_id = batch_rows[0]["batch_id"]

        col_rows = _run_query(
            project, COLUMN_SCORES,
            dataset_uri=dataset_uri, batch_id=batch_id,
        )
        columns = _pivot_column_scores(col_rows)

        return {
            "batch_id": batch_id,
            "dataset_uri": dataset_uri,
            "columns": columns,
        }
    except Exception as e:
        raise HTTPException(500, f"Neo4j query failed: {e}")


@router.get("/trend")
def get_trend(project_id: int, session: Session = Depends(get_session)):
    """Get composite scores across all batches for trend tracking."""
    project = _get_project(project_id, session)
    scoped = has_project_node(project)
    pc = {"project_code": project.project_code} if scoped else {}

    try:
        rows = _run_query(
            project, _pick(scoped, TREND_S, TREND), **pc
        )

        # Group by batch
        batches: dict[str, dict] = {}
        for row in rows:
            bid = row["batch_id"]
            if bid not in batches:
                batches[bid] = {
                    "batch_id": bid,
                    "scored_at": str(row["scored_at"]) if row["scored_at"] else None,
                    "datasets": {},
                }
            batches[bid]["datasets"][row["dataset_name"]] = row["score"]

        # Calculate average composite per batch
        result = []
        for batch in batches.values():
            scores = list(batch["datasets"].values())
            batch["avg_composite"] = round(sum(scores) / len(scores), 6) if scores else 0.0
            result.append(batch)

        return {"batches": result}
    except Exception as e:
        raise HTTPException(500, f"Neo4j query failed: {e}")
