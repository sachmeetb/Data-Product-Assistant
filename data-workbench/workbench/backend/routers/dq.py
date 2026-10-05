"""DQ package download endpoint.

Mirrors the serving.get_view_package pattern: assembles the generated DQ test
files (excluding per-run results/) into a downloadable zip or JSON response.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from typing import Optional

from ..database import get_session
from ..dq_package import assemble_dq_package, resolve_suite
from ..models import Project
from ..serving_package import zip_response

router = APIRouter(prefix="/api/projects/{project_id}", tags=["dq"])


def _get_project(project_id: int, session: Session) -> Project:
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/dq-package")
def get_dq_package(
    project_id: int,
    format: str = "zip",
    source_mode: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """Download the generated DQ test package — test code + README, without
    per-run results. ``format=zip`` (default) streams a zip archive;
    ``format=json`` returns the text files as a dict.

    ``source_mode`` selects the suite: ``catalog`` (source pre-check) or ``dprod``
    (deployed-product) — an SA product can hold both. When omitted, the present
    suite is auto-picked (dprod preferred). The zip name is suffixed for the dprod
    suite so both packages coexist in a downloads folder.

    Returns 404 when no test files exist yet — run the 'Generate DQ Tests'
    stage first.
    """
    project = _get_project(project_id, session)
    framework, mode = resolve_suite(project.project_code, source_mode)
    if framework is None:
        raise HTTPException(
            404,
            "No DQ test files found — run the 'Generate DQ Tests' stage first.",
        )
    files = assemble_dq_package(project.project_code, framework, mode)
    if not files:
        raise HTTPException(
            404,
            "No DQ test files found — run the 'Generate DQ Tests' stage first.",
        )
    suffix = "dq-tests-dprod" if mode == "dprod" else "dq-tests"
    if format == "json":
        return {
            "project_code": project.project_code,
            "framework": framework,
            "source_mode": mode,
            "files": files,
        }
    return zip_response(
        files,
        f"{project.project_code}-{suffix}.zip",
        root_prefix=f"{project.project_code}-{suffix}",
    )
