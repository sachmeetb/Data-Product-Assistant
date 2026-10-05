"""Admin graph-integrity endpoint (`/api/admin/graph-integrity`).

Read-only. Runs the detect-only invariant engine in ``graph_integrity.py``
over the global graph (or one project when ``?project_code=`` is supplied) and
returns the structured scorecard. Never writes, never blocks a pipeline write.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .. import graph_integrity
from ..database import get_session
from ..models import AppSettings

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _get_neo4j_settings(session: Session) -> AppSettings:
    settings = session.exec(select(AppSettings)).first()
    if not settings:
        raise HTTPException(500, "Global Neo4j settings not configured. Go to Settings first.")
    return settings


@router.get("/graph-integrity")
def graph_integrity_check(
    project_code: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """Run the graph-integrity invariant catalog and return the scorecard.

    Global by default; pass ``?project_code=<code>`` to scope the code-bearing
    checks to a single project. The report includes clean checks (``count: 0``)
    so a caller can assert "zero leaks / zero orphans", not just enumerate
    failures.
    """
    settings = _get_neo4j_settings(session)
    try:
        return graph_integrity.build_scorecard(settings, project_code=project_code)
    except Exception as e:
        raise HTTPException(500, f"Graph integrity scan failed: {e}")
