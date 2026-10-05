"""LLM token-usage rollups over the :class:`LlmUsageEvent` ledger.

- ``GET /api/usage/summary``                  → global totals + by-source breakdown
- ``GET /api/usage/summary?project_code=…``   → per-data-product totals
- ``GET /api/usage/semantic-qa``              → Semantic-Q&A totals grouped by
  retrieval_mode (full vs concept-guided), for the aggregate comparison

All reads are SQLite aggregations over the ledger (the single source of truth).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import Session, select

from ..database import get_session
from ..models import LlmUsageEvent, Project

router = APIRouter(prefix="/api/usage", tags=["usage"])

_TOKEN_COLS = (
    LlmUsageEvent.input_tokens,
    LlmUsageEvent.output_tokens,
    LlmUsageEvent.cache_read_tokens,
    LlmUsageEvent.cache_creation_tokens,
    LlmUsageEvent.total_tokens,
)


def _totals_row(session: Session, *filters) -> dict:
    """Aggregate token + cost totals over the ledger with optional filters."""
    stmt = select(
        func.coalesce(func.sum(LlmUsageEvent.input_tokens), 0),
        func.coalesce(func.sum(LlmUsageEvent.output_tokens), 0),
        func.coalesce(func.sum(LlmUsageEvent.cache_read_tokens), 0),
        func.coalesce(func.sum(LlmUsageEvent.cache_creation_tokens), 0),
        func.coalesce(func.sum(LlmUsageEvent.total_tokens), 0),
        func.coalesce(func.sum(LlmUsageEvent.cost_usd), 0.0),
        func.count(LlmUsageEvent.id),
    )
    for f in filters:
        stmt = stmt.where(f)
    r = session.exec(stmt).one()
    return {
        "input_tokens": int(r[0] or 0),
        "output_tokens": int(r[1] or 0),
        "cache_read_tokens": int(r[2] or 0),
        "cache_creation_tokens": int(r[3] or 0),
        "total_tokens": int(r[4] or 0),
        "cost_usd": round(float(r[5] or 0.0), 6),
        "events": int(r[6] or 0),
    }


@router.get("/summary")
def usage_summary(
    project_code: Optional[str] = None,
    domain: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """Token totals across the whole system, or scoped to a data product
    (``project_code``) or ``domain``. Includes a per-source breakdown."""
    filters = []
    if project_code:
        filters.append(LlmUsageEvent.project_code == project_code)
    if domain:
        filters.append(LlmUsageEvent.domain == domain)

    totals = _totals_row(session, *filters)

    # Per-source breakdown (same filters).
    src_stmt = select(
        LlmUsageEvent.source,
        func.coalesce(func.sum(LlmUsageEvent.total_tokens), 0),
        func.coalesce(func.sum(LlmUsageEvent.input_tokens), 0),
        func.coalesce(func.sum(LlmUsageEvent.output_tokens), 0),
        func.coalesce(func.sum(LlmUsageEvent.cost_usd), 0.0),
        func.count(LlmUsageEvent.id),
    )
    for f in filters:
        src_stmt = src_stmt.where(f)
    src_stmt = src_stmt.group_by(LlmUsageEvent.source).order_by(
        func.sum(LlmUsageEvent.total_tokens).desc()
    )
    by_source = [
        {
            "source": row[0],
            "total_tokens": int(row[1] or 0),
            "input_tokens": int(row[2] or 0),
            "output_tokens": int(row[3] or 0),
            "cost_usd": round(float(row[4] or 0.0), 6),
            "events": int(row[5] or 0),
        }
        for row in session.exec(src_stmt).all()
    ]

    return {
        "scope": {"project_code": project_code, "domain": domain},
        **totals,
        "by_source": by_source,
    }


@router.get("/by-project/{project_id}")
def usage_by_project(project_id: int, session: Session = Depends(get_session)):
    """Per-data-product token totals (resolves project_id → project_code, then
    aggregates the ledger). Includes a per-source breakdown."""
    project = session.get(Project, project_id)
    if project is None:
        return {"error": f"No project with id {project_id}", **_totals_row(session, LlmUsageEvent.id == -1)}
    code = project.project_code
    totals = _totals_row(session, LlmUsageEvent.project_code == code)
    src_stmt = (
        select(
            LlmUsageEvent.source,
            func.coalesce(func.sum(LlmUsageEvent.total_tokens), 0),
            func.coalesce(func.sum(LlmUsageEvent.cost_usd), 0.0),
            func.count(LlmUsageEvent.id),
        )
        .where(LlmUsageEvent.project_code == code)
        .group_by(LlmUsageEvent.source)
        .order_by(func.sum(LlmUsageEvent.total_tokens).desc())
    )
    by_source = [
        {"source": r[0], "total_tokens": int(r[1] or 0),
         "cost_usd": round(float(r[2] or 0.0), 6), "events": int(r[3] or 0)}
        for r in session.exec(src_stmt).all()
    ]
    return {"project_code": code, **totals, "by_source": by_source}


@router.get("/semantic-qa")
def usage_semantic_qa(
    domain: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """Semantic-Q&A token usage grouped by retrieval_mode (full vs
    concept-guided) — the aggregate version of the per-question comparison.
    Includes average tokens/question so the modes are directly comparable."""
    stmt = select(
        LlmUsageEvent.retrieval_mode,
        func.coalesce(func.sum(LlmUsageEvent.input_tokens), 0),
        func.coalesce(func.sum(LlmUsageEvent.output_tokens), 0),
        func.coalesce(func.sum(LlmUsageEvent.total_tokens), 0),
        func.coalesce(func.sum(LlmUsageEvent.cost_usd), 0.0),
        func.count(LlmUsageEvent.id),
    ).where(LlmUsageEvent.source == "semantic_qa")
    if domain:
        stmt = stmt.where(LlmUsageEvent.domain == domain)
    stmt = stmt.group_by(LlmUsageEvent.retrieval_mode)

    by_mode = []
    for row in session.exec(stmt).all():
        questions = int(row[5] or 0)
        total = int(row[3] or 0)
        by_mode.append({
            "retrieval_mode": row[0] or "unknown",
            "input_tokens": int(row[1] or 0),
            "output_tokens": int(row[2] or 0),
            "total_tokens": total,
            "cost_usd": round(float(row[4] or 0.0), 6),
            "questions": questions,
            "avg_tokens_per_question": round(total / questions, 1) if questions else 0,
        })
    return {"domain": domain, "by_retrieval_mode": by_mode}
