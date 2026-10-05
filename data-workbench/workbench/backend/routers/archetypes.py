from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..archetypes import (
    ARCHETYPE_REGISTRY,
    enrich_workflow,
    get_default_workflow,
    validate_workflow,
)

router = APIRouter(prefix="/api/archetypes", tags=["archetypes"])


@router.get("")
def list_archetypes():
    return [
        {"slug": slug, **info}
        for slug, info in ARCHETYPE_REGISTRY.items()
    ]


@router.get("/{slug}/workflow")
def get_archetype_workflow(slug: str):
    if slug not in ARCHETYPE_REGISTRY:
        raise HTTPException(404, f"Unknown archetype: {slug}")
    workflow = get_default_workflow(slug)
    return {"archetype": slug, "workflow": enrich_workflow(workflow)}


class WorkflowValidation(BaseModel):
    workflow: list[dict]


@router.post("/validate")
def validate_workflow_endpoint(body: WorkflowValidation):
    errors = validate_workflow(body.workflow)
    return {"valid": len(errors) == 0, "errors": errors}
