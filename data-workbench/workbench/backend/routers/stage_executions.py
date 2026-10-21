import json

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import get_session
from ..models import StageExecution

router = APIRouter(tags=["stage-executions"])


def _parse(value: str, fallback):
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _summary(row: StageExecution) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "stage_number": row.stage_number,
        "workflow_id": row.workflow_id,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "status": row.status,
        "error_message": row.error_message,
        "cost_usd": row.cost_usd,
        "event_count": row.event_count,
        "tool_counts": _parse(row.tool_counts_json, {}),
        "truncated": row.truncated,
    }


@router.get("/api/projects/{project_id}/stages/{stage_number}/executions")
def list_executions(
    project_id: int,
    stage_number: int,
    workflow_id: str | None = None,
    session: Session = Depends(get_session),
):
    q = select(StageExecution).where(
        StageExecution.project_id == project_id,
        StageExecution.stage_number == stage_number,
    )
    if workflow_id is not None:
        q = q.where(StageExecution.workflow_id == workflow_id)
    q = q.order_by(StageExecution.started_at.desc(), StageExecution.id.desc())
    return [_summary(row) for row in session.exec(q).all()]


@router.get("/api/stage-executions/{execution_id}")
def get_execution(
    execution_id: int,
    project_id: int | None = None,
    session: Session = Depends(get_session),
):
    row = session.get(StageExecution, execution_id)
    if not row:
        raise HTTPException(status_code=404, detail="Execution not found")
    # Defensive: the execution detail endpoint isn't project-scoped in the
    # URL, so the frontend passes its current project_id and we refuse to
    # return a transcript that doesn't belong to that project.
    if project_id is not None and row.project_id != project_id:
        raise HTTPException(status_code=404, detail="Execution not found")
    data = _summary(row)
    data["session_id"] = row.session_id
    data["error_message"] = row.error_message
    data["events"] = _parse(row.log_json, [])
    return data
