import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from sqlmodel import Session

from ..config import BASE_PROJECT_DIR
from ..database import get_session
from ..models import Project

router = APIRouter(prefix="/api/projects/{project_id}/artifacts", tags=["artifacts"])


@router.get("/results")
def get_results(
    project_id: int,
    session: Session = Depends(get_session),
):
    """Find and return structured result files from the project directory.

    Searches common result locations (results/, dq_tests_gx/results/, etc.)
    and returns the most recent JSON result files parsed as structured data.
    """
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = BASE_PROJECT_DIR / project.project_code
    result_dirs = [
        project_dir / "results",
        project_dir / "dq_tests_gx" / "results",
        project_dir / "dq_tests_python" / "results",
        # dprod suites (product_dq_testing) land in `_dprod`-suffixed dirs.
        project_dir / "dq_tests_gx_dprod" / "results",
        project_dir / "dq_tests_python_dprod" / "results",
    ]

    results = []
    for rdir in result_dirs:
        if not rdir.exists():
            continue
        for f in sorted(rdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(f.read_text())
                results.append({
                    "filename": f.name,
                    "path": str(f.relative_to(project_dir)),
                    "modified": f.stat().st_mtime,
                    "data": data,
                })
            except Exception:
                continue

    return {"results": results, "count": len(results)}


@router.get("")
def list_artifacts(
    project_id: int,
    path: str = Query("", description="Subdirectory path relative to project root"),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = (BASE_PROJECT_DIR / project.project_code).resolve()
    target_dir = (project_dir / path).resolve()

    # Prevent path traversal (real containment, not string-prefix — a sibling
    # like ``customer-copy`` shares the ``customer`` prefix but isn't contained).
    try:
        target_dir.relative_to(project_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if not target_dir.exists():
        return {"files": [], "path": path}

    files = []
    for entry in sorted(target_dir.iterdir()):
        stat = entry.stat()
        files.append({
            "name": entry.name,
            "type": "directory" if entry.is_dir() else "file",
            "size": stat.st_size if entry.is_file() else None,
            "modified": stat.st_mtime,
            "path": str(entry.relative_to(project_dir)),
        })

    return {"files": files, "path": path}


@router.get("/content")
def get_artifact_content(
    project_id: int,
    path: str = Query(..., description="File path relative to project root"),
    session: Session = Depends(get_session),
):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_dir = (BASE_PROJECT_DIR / project.project_code).resolve()
    file_path = (project_dir / path).resolve()

    try:
        file_path.relative_to(project_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    suffix = file_path.suffix.lower()
    if suffix in (".yaml", ".yml", ".json", ".cypher", ".py", ".txt", ".md", ".csv"):
        return PlainTextResponse(file_path.read_text(errors="replace"))

    return FileResponse(file_path)
