"""Leased background worker for the Connected-Estate capability.

One asyncio task (started from the FastAPI lifespan) drives BOTH claimable
queues — estate scans (``estate_scan.process_one_scan``) and feasibility runs
(``feasibility.process_one_run``) — via compare-and-set leases, mirroring
``intake_worker``. In-process (no broker), but restart-safe: a crash mid-run
leaves a leased row that the next tick reclaims once its lease expires. Scans and
runs are drained fairly (a scan tick, then a run tick) so neither starves.
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import estate_enrich, estate_scan, feasibility

logger = logging.getLogger(__name__)

LEASE_TTL_SECONDS = 600  # a live scan can take a while
POLL_INTERVAL_SECONDS = 3.0
_WORKER_ID = f"estate-worker-{os.getpid()}"
_stop = asyncio.Event()


async def process_once() -> bool:
    """Attempt one unit of work from either queue. Returns True if any ran.

    ``process_one_scan`` is sync (a scan is I/O against a live DB, run in a thread
    so it can't block the event loop); ``process_one_run`` is async (it awaits the
    evaluator skill)."""
    did_scan = await asyncio.to_thread(
        estate_scan.process_one_scan, _WORKER_ID, LEASE_TTL_SECONDS
    )
    did_run = await feasibility.process_one_run(_WORKER_ID, LEASE_TTL_SECONDS)
    did_enrich = await estate_enrich.process_one_enrichment(_WORKER_ID, LEASE_TTL_SECONDS)
    return bool(did_scan or did_run or did_enrich)


async def _worker_loop() -> None:
    logger.info("estate worker started (%s)", _WORKER_ID)
    while not _stop.is_set():
        try:
            did_work = await process_once()
        except Exception:
            logger.exception("estate worker iteration failed")
            did_work = False
        if not did_work:
            try:
                await asyncio.wait_for(_stop.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    logger.info("estate worker stopped (%s)", _WORKER_ID)


def start_worker() -> asyncio.Task:
    _stop.clear()
    return asyncio.create_task(_worker_loop())


async def stop_worker(task: asyncio.Task) -> None:
    _stop.set()
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
