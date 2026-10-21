"""Parse DQ testing stage artifacts and insert DQTestRun rows.

Runs as a post-stage hook after data-quality-testing-gx or -python stages
complete. Reads the freshly-written results JSON under dq_tests_*/results/,
aggregates pass/fail counts, and persists a DQTestRun row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Session

from .config import BASE_PROJECT_DIR
from .models import DQTestRun


_SKILL_TO_FRAMEWORK = {
    "data-quality-testing-gx": "gx",
    "data-quality-testing-python": "pandera",
}

_SKILL_TO_RESULTS_DIR = {
    "data-quality-testing-gx": "dq_tests_gx/results",
    "data-quality-testing-python": "dq_tests_python/results",
}


def _find_latest_results(results_dir: Path) -> list[Path]:
    """Return the per-table JSON result files newer than 10 minutes ago.

    Batches multiple per-table result files from a single run, since
    skills may emit one JSON per table. Excludes ``run_result.json`` — that's
    the RunRecorder v1 contract (a different shape), NOT per-table detail;
    counting it would skew ``tables_tested`` and it carries no ``statistics``.
    """
    if not results_dir.exists():
        return []
    now = datetime.now().timestamp()
    recent = [
        f for f in results_dir.glob("*.json")
        if f.name != "run_result.json" and (now - f.stat().st_mtime) < 600  # 10 minutes
    ]
    return sorted(recent, key=lambda p: p.stat().st_mtime)


def _summarize_gx_results(files: list[Path]) -> dict:
    """Aggregate GX-style result JSON into a single summary dict."""
    total = successful = unsuccessful = tables = 0
    for f in files:
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        # GX-style shape: {"table": ..., "statistics": {"evaluated","successful","unsuccessful"}}
        # The generated run_gx_validations.py writes one file per run shaped
        # {"run_at": ..., "tables": [<per-table entry>, ...]} — each entry carries
        # `table` + `statistics`. Also accept a bare list, a {"results": [...]}
        # wrapper, or a single top-level-statistics entry.
        entries = []
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            if "tables" in data and isinstance(data["tables"], list):
                entries = data["tables"]
            elif "results" in data and isinstance(data["results"], list):
                entries = data["results"]
            elif "statistics" in data:
                entries = [data]
        for entry in entries:
            stats = entry.get("statistics") or entry.get("stats") or {}
            total += int(stats.get("evaluated", 0) or 0)
            successful += int(stats.get("successful", 0) or 0)
            unsuccessful += int(stats.get("unsuccessful", stats.get("unexpected", 0)) or 0)
            if entry.get("table") or entry.get("suite"):
                tables += 1
    return {
        "total_expectations": total,
        "successful": successful,
        "unsuccessful": unsuccessful,
        "tables_tested": tables or len(files),
    }


def record_dq_test_run(
    session: Session,
    project_id: int,
    project_code: str,
    stage_run_id: Optional[int],
    skill_name: str,
    started_at: datetime,
    completed_at: datetime,
    results_dir: Optional[Path] = None,
) -> Optional[DQTestRun]:
    """Create a DQTestRun row by parsing the stage's results directory.

    Pass ``results_dir`` explicitly (the executor knows the mode-aware path,
    ``dq_tests_gx[_dprod]/results``) — without it we fall back to the catalog
    layout only, which silently drops dprod runs (their results live under the
    ``_dprod`` suffix). Returns the created row, or None if no result files were
    found.
    """
    framework = _SKILL_TO_FRAMEWORK.get(skill_name)
    subdir = _SKILL_TO_RESULTS_DIR.get(skill_name)
    if not framework or not subdir:
        return None

    project_dir = BASE_PROJECT_DIR / project_code
    if results_dir is None:
        results_dir = project_dir / subdir
    files = _find_latest_results(results_dir)
    if not files:
        return None

    summary = _summarize_gx_results(files)
    newest = files[-1]
    # batch_id MUST match the Neo4j loader's :TestRun.batchId so the per-result
    # drill-down (routers/test_runs.py:_TEST_RESULTS_FOR_RUN, keyed on batchId)
    # can join SQLite → graph. The loader uses the results file's `run_at`
    # verbatim; mirror that, falling back to our own clock if absent.
    batch_id = completed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        run_at = json.loads(newest.read_text()).get("run_at")
        if run_at:
            batch_id = run_at
    except Exception:
        pass

    run = DQTestRun(
        project_id=project_id,
        stage_run_id=stage_run_id,
        framework=framework,
        batch_id=batch_id,
        started_at=started_at,
        completed_at=completed_at,
        total_expectations=summary["total_expectations"],
        successful=summary["successful"],
        unsuccessful=summary["unsuccessful"],
        tables_tested=summary["tables_tested"],
        results_path=str(newest.relative_to(project_dir)),
        status="complete",
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run
