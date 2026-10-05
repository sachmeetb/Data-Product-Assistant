import json
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from ..archetypes import STAGE_REGISTRY
from ..database import get_session
from ..models import Project, StageRun, StageStatus
from ..pipeline import STAGES

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

ROLE_DESCRIPTIONS = {
    "Data Product Owner": "Define data products, initiate projects, and review final specifications.",
    "Data Engineer": "Run discovery, profiling, and mapping stages across data products.",
    "Data Steward": "Enrich metadata with descriptions and review AI-generated content.",
    "Data Quality Analyst": "Generate and execute data quality rules and tests.",
    "Reviewer": "Review and approve descriptions and column mappings.",
}

# Which roles own which stage_ids
ROLE_STAGE_IDS = {
    "Data Product Owner": {"initiate", "odcs_specification", "odcs_to_dprod", "publish"},
    "Data Engineer": {"data_discovery", "data_discovery_composite", "load_schema", "data_profiling", "data_profiling_composite", "load_profiles", "data_mapping"},
    "Data Steward": {"metadata_enrichment", "reflect_on_reviews"},
    "Data Quality Analyst": {"dq_rule_generation", "dq_testing_gx", "dq_testing_python"},
    "Reviewer": set(),
}

ROLE_REVIEW_TYPES = {
    "Data Steward": {"descriptions"},
    "Reviewer": {"descriptions", "mappings"},
}


@router.get("")
def get_dashboard(
    role: str = Query(..., description="Current persona role"),
    session: Session = Depends(get_session),
):
    projects = session.exec(select(Project).order_by(Project.created_at.desc())).all()

    allowed_stage_ids = ROLE_STAGE_IDS.get(role, set())
    allowed_review_types = ROLE_REVIEW_TYPES.get(role, set())

    ready_tasks = []
    upcoming_tasks = []
    pending_reviews = []
    total_completed = 0
    total_cost = 0.0

    for project in projects:
        stages = session.exec(
            select(StageRun).where(StageRun.project_id == project.id).order_by(StageRun.stage_number)
        ).all()

        # Parse workflow for stage_id resolution
        workflow = None
        if project.workflow_json:
            workflow = [s for s in json.loads(project.workflow_json) if s.get("enabled", True)]

        # Build status map for prerequisite checking
        stage_statuses = {s.stage_number: s.status for s in stages}

        for stage in stages:
            stage_id = None
            if workflow and stage.stage_number <= len(workflow):
                stage_id = workflow[stage.stage_number - 1].get("stage_id")

            stage_def = STAGE_REGISTRY.get(stage_id, {}) if stage_id else {}

            if stage.cost_usd:
                total_cost += stage.cost_usd
            if stage.status == StageStatus.complete:
                total_completed += 1

            # Check if this role owns this stage
            is_my_stage = stage_id and stage_id in allowed_stage_ids

            if stage.status == StageStatus.pending and is_my_stage:
                # Check if previous stage is complete (prerequisite)
                prev_complete = stage.stage_number == 1 or stage_statuses.get(stage.stage_number - 1) in (StageStatus.complete, "complete")
                task_info = {
                    "project_id": project.id,
                    "project_name": project.name,
                    "project_code": project.project_code,
                    "stage_number": stage.stage_number,
                    "stage_name": stage.stage_name,
                    "stage_id": stage_id,
                }
                if prev_complete:
                    ready_tasks.append(task_info)
                else:
                    upcoming_tasks.append(task_info)

            # Pending reviews for this role
            if stage.status == StageStatus.awaiting_review:
                review_type = stage_def.get("review_type")
                if review_type and review_type in allowed_review_types:
                    pending_reviews.append({
                        "project_id": project.id,
                        "project_name": project.name,
                        "project_code": project.project_code,
                        "stage_number": stage.stage_number,
                        "stage_name": stage.stage_name,
                        "review_type": review_type,
                    })

    return {
        "persona": {
            "role": role,
            "description": ROLE_DESCRIPTIONS.get(role, ""),
        },
        "ready_tasks": ready_tasks,
        "upcoming_tasks": upcoming_tasks,
        "pending_reviews": pending_reviews,
        "stats": {
            "total_projects": len(projects),
            "completed_stages": total_completed,
            "total_cost_usd": round(total_cost, 4),
        },
    }
