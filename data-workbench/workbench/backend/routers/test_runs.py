from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import get_session
from ..graph_ops import has_project_node
from ..models import DQTestRun, Project
from ..neo4j_client import neo4j_session

router = APIRouter(prefix="/api/projects/{project_id}/test-runs", tags=["test-runs"])


# ── Neo4j queries ────────────────────────────────────────────────────────────

# Dual-anchor: catalog results link :ON_COLUMN → :Column → :Dataset; dprod results
# link :ON_DPROD_COLUMN → :DProdColumn → :DProdOutputDataset. Coalesce so a run of
# either mode surfaces the same column/dataset drill-down shape.
_TEST_RESULTS_FOR_RUN = """\
MATCH (tr:TestRun {batchId: $batch_id})-[:PRODUCED]->(res:TestResult)
OPTIONAL MATCH (res)-[:ON_COLUMN]->(col:Column)
OPTIONAL MATCH (col)<-[:HAS_COLUMN]-(ds:Dataset)
OPTIONAL MATCH (res)-[:ON_DPROD_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (pc)<-[:HAS_PRODUCT_COLUMN]-(pods:DProdOutputDataset)
WITH res,
     coalesce(col.name, pc.name) AS column_name,
     coalesce(ds.name, pods.physicalName, pods.name) AS dataset_name,
     coalesce(ds.schema, pods.schema) AS schema
RETURN res.ruleType AS rule_type,
       res.expectationType AS expectation_type,
       res.evaluated AS evaluated,
       res.successful AS successful,
       res.unsuccessful AS unsuccessful,
       res.passRate AS pass_rate,
       res.passed AS passed,
       column_name,
       dataset_name,
       schema
ORDER BY res.passed, schema, dataset_name, column_name
"""

_SUMMARY = """\
MATCH (:Project {projectCode: $project_code})-[:HAS_TEST_RUN]->(tr:TestRun)
WITH tr ORDER BY tr.executedAt DESC LIMIT 1
OPTIONAL MATCH (tr)-[:PRODUCED]->(res:TestResult)
WITH tr,
     count(res) AS n_results,
     sum(CASE WHEN res.passed THEN 1 ELSE 0 END) AS n_passed
OPTIONAL MATCH (:Project {projectCode: $project_code})
         -[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset)
         -[:HAS_SHAPE]->(:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
WITH tr, n_results, n_passed, count(DISTINCT ps) AS n_rules
RETURN tr.batchId AS batch_id,
       tr.executedAt AS executed_at,
       tr.framework AS framework,
       tr.totalExpectations AS total,
       tr.successful AS successful,
       tr.unsuccessful AS unsuccessful,
       n_results AS rules_tested,
       n_rules AS rules_total
"""


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


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("")
def list_test_runs(
    project_id: int,
    framework: Optional[str] = None,
    session: Session = Depends(get_session),
):
    _get_project(project_id, session)
    stmt = select(DQTestRun).where(DQTestRun.project_id == project_id)
    if framework:
        stmt = stmt.where(DQTestRun.framework == framework)
    stmt = stmt.order_by(DQTestRun.started_at.desc())
    runs = session.exec(stmt).all()
    return {
        "runs": [
            {
                "id": r.id,
                "stage_run_id": r.stage_run_id,
                "framework": r.framework,
                "batch_id": r.batch_id,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "total_expectations": r.total_expectations,
                "successful": r.successful,
                "unsuccessful": r.unsuccessful,
                "tables_tested": r.tables_tested,
                "pass_rate": (r.successful / r.total_expectations) if r.total_expectations else 0.0,
                "results_path": r.results_path,
                "status": r.status,
            }
            for r in runs
        ],
        "count": len(runs),
    }


@router.get("/summary")
def test_run_summary(project_id: int, session: Session = Depends(get_session)):
    """Latest test run rollup for the QualityScorePanel test-coverage strip."""
    project = _get_project(project_id, session)
    if not has_project_node(project):
        return {"has_runs": False}
    try:
        with _neo4j(project) as ns:
            row = ns.run(_SUMMARY, project_code=project.project_code).single()
    except Exception as e:
        raise HTTPException(500, f"Neo4j query failed: {e}")

    if not row or row.get("batch_id") is None:
        return {"has_runs": False}

    total = row["total"] or 0
    successful = row["successful"] or 0
    rules_total = row["rules_total"] or 0
    rules_tested = row["rules_tested"] or 0
    return {
        "has_runs": True,
        "batch_id": row["batch_id"],
        "executed_at": str(row["executed_at"]) if row["executed_at"] else None,
        "framework": row["framework"],
        "total_expectations": total,
        "successful": successful,
        "unsuccessful": row["unsuccessful"] or 0,
        "pass_rate": (successful / total) if total else 0.0,
        "rules_total": rules_total,
        "rules_tested": rules_tested,
        "coverage": (rules_tested / rules_total) if rules_total else 0.0,
    }


@router.get("/{run_id}")
def get_test_run(
    project_id: int,
    run_id: int,
    session: Session = Depends(get_session),
):
    project = _get_project(project_id, session)
    run = session.get(DQTestRun, run_id)
    if not run or run.project_id != project_id:
        raise HTTPException(404, "Test run not found")

    # Join graph for per-rule results
    results: list[dict] = []
    if has_project_node(project):
        try:
            with _neo4j(project) as ns:
                rows = ns.run(_TEST_RESULTS_FOR_RUN, batch_id=run.batch_id)
                results = [dict(r) for r in rows]
        except Exception:
            results = []

    return {
        "id": run.id,
        "framework": run.framework,
        "batch_id": run.batch_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "total_expectations": run.total_expectations,
        "successful": run.successful,
        "unsuccessful": run.unsuccessful,
        "tables_tested": run.tables_tested,
        "pass_rate": (run.successful / run.total_expectations) if run.total_expectations else 0.0,
        "results_path": run.results_path,
        "status": run.status,
        "results": results,
    }
