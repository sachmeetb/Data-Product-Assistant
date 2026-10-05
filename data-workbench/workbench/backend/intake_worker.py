"""SQL-backed leased worker that parses staged intake submissions.

Started once from the FastAPI lifespan. It claims ``received`` submissions via a
compare-and-set lease, runs the isolated parser OUTSIDE the DB session (it's a
network call), then stores the validated blueprint and flips the row to
``proposed`` (or ``parse_failed``). Leases are reclaimable: a submission stuck in
``parsing`` past its lease expiry is returned to ``received`` on the next tick,
so a crash mid-parse recovers after restart. In-process (no broker exists), but
restart-safe by construction.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from . import intake_parser
from .database import engine
from .models import IntakeEvent, IntakeSubmission

logger = logging.getLogger(__name__)

LEASE_TTL_SECONDS = 120
POLL_INTERVAL_SECONDS = 3.0
_WORKER_ID = f"intake-worker-{os.getpid()}"
_stop = asyncio.Event()


def _add_event(session: Session, sid: int, frm: str, to: str, detail: str = "") -> None:
    session.add(
        IntakeEvent(intake_submission_id=sid, actor=_WORKER_ID, from_status=frm, to_status=to, detail=detail)
    )


def _claim_one(session: Session) -> Optional[int]:
    """Reclaim stale leases, then claim one ``received`` submission. Returns its id."""
    now = datetime.utcnow()
    stale = session.exec(
        select(IntakeSubmission).where(
            IntakeSubmission.status == "parsing",
            IntakeSubmission.lease_expires_at.is_not(None),  # type: ignore[union-attr]
            IntakeSubmission.lease_expires_at < now,
        )
    ).all()
    for r in stale:
        r.status = "received"
        r.lease_owner = None
        r.lease_expires_at = None
        _add_event(session, r.id, "parsing", "received", "stale lease reclaimed")
    if stale:
        session.commit()

    row = session.exec(
        select(IntakeSubmission)
        .where(IntakeSubmission.status == "received")
        .order_by(IntakeSubmission.id)  # type: ignore[arg-type]
    ).first()
    if row is None:
        return None
    row.status = "parsing"
    row.lease_owner = _WORKER_ID
    row.lease_expires_at = now + timedelta(seconds=LEASE_TTL_SECONDS)
    row.parse_attempts += 1
    row.updated_at = now
    _add_event(session, row.id, "received", "parsing", "claimed by worker")
    session.commit()
    return row.id


async def process_one() -> bool:
    """Process a single claimable submission. Returns True if one was handled.

    Public so tests can drive one iteration deterministically (monkeypatching
    ``intake_parser.parse_envelope``) without running the loop.
    """
    with Session(engine) as session:
        sid = _claim_one(session)
        if sid is None:
            return False
        row = session.get(IntakeSubmission, sid)
        envelope = json.loads(row.raw_payload_json or "{}")
        scenario = row.scenario

    # Network call — done outside any open session/transaction.
    blueprint, meta = await intake_parser.parse_envelope(envelope, scenario)

    with Session(engine) as session:
        row = session.get(IntakeSubmission, sid)
        if row is None:
            return True
        row.parse_meta_json = json.dumps(meta, default=str)
        row.lease_owner = None
        row.lease_expires_at = None
        if blueprint is None:
            _add_event(session, row.id, "parsing", "parse_failed", str(meta.get("error", "")))
            row.status = "parse_failed"
        else:
            row.blueprint_json = json.dumps(blueprint, default=str)
            row.blueprint_revision += 1
            _add_event(session, row.id, "parsing", "proposed", "blueprint ready for review")
            row.status = "proposed"
        row.updated_at = datetime.utcnow()
        session.add(row)
        session.commit()
    return True


async def _worker_loop() -> None:
    logger.info("intake worker started (%s)", _WORKER_ID)
    while not _stop.is_set():
        try:
            did_work = await process_one()
        except Exception:
            logger.exception("intake worker iteration failed")
            did_work = False
        if not did_work:
            try:
                await asyncio.wait_for(_stop.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    logger.info("intake worker stopped (%s)", _WORKER_ID)


def start_worker() -> asyncio.Task:
    _stop.clear()
    return asyncio.create_task(_worker_loop())


async def stop_worker(task: asyncio.Task) -> None:
    _stop.set()
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
