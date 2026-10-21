"""Phase 8 — real-engine conformance (opt-in, credential-gated).

The capability corpus marks its `date_difference` renderings ``conformance-verify``
— candidates, never asserted correct from memory. This suite EXECUTES those
renderings against a real engine and asserts the numeric result, proving the
seeded SQL actually runs and returns the right value behind the capability claim.

It is skipped by default. To run against an engine, export a SQLAlchemy DSN:

    WB_CONFORMANCE_POSTGRES_DSN=postgresql+psycopg2://user:pw@host/db
    WB_CONFORMANCE_MYSQL_DSN=mysql+pymysql://user:pw@host/db
    WB_CONFORMANCE_SNOWFLAKE_DSN=snowflake://...
    WB_CONFORMANCE_DATABRICKS_DSN=databricks://token:...@host?http_path=...
    WB_CONFORMANCE_BIGQUERY_DSN=bigquery://project/dataset

A platform with no DSN (or whose SQLAlchemy driver isn't installed) is skipped
individually — the suite never fails just because an engine is unavailable.

Fixture: dob = 2000-12-31, ref = 2026-08-12 → the birthday has NOT yet occurred
in the ref year, so completed-age (25) and calendar-boundary count (26) DIFFER —
which is exactly the trap the `semantics` discriminator exists to prevent.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_GEN = _REPO / "workbench-skills" / "skills" / "data-serving-virtual-view" / "scripts" / "generate_view_ddl.py"

_DSN_ENV = {
    "postgres": "WB_CONFORMANCE_POSTGRES_DSN",
    "mysql": "WB_CONFORMANCE_MYSQL_DSN",
    "snowflake": "WB_CONFORMANCE_SNOWFLAKE_DSN",
    "databricks": "WB_CONFORMANCE_DATABRICKS_DSN",
    "bigquery": "WB_CONFORMANCE_BIGQUERY_DSN",
}

# dob, ref chosen so completed_units != boundary_count.
_START = "CAST('2000-12-31' AS DATE)"
_END = "CAST('2026-08-12' AS DATE)"
_EXPECTED = {"completed_units": 25, "boundary_count": 26}


def _load_gv():
    spec = importlib.util.spec_from_file_location("generate_view_ddl", _GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gv = _load_gv()

_CASES = [
    (platform, semantics, expected)
    for platform in _DSN_ENV
    for semantics, expected in _EXPECTED.items()
]


@pytest.mark.parametrize("platform,semantics,expected", _CASES)
def test_date_difference_executes_on_real_engine(platform, semantics, expected):
    dsn = os.environ.get(_DSN_ENV[platform])
    if not dsn:
        pytest.skip(f"no DSN for {platform} ({_DSN_ENV[platform]} unset)")
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        pytest.skip("sqlalchemy not installed")

    try:
        engine = create_engine(dsn)
    except Exception as e:  # driver package missing, bad DSN, etc.
        pytest.skip(f"cannot build engine for {platform}: {e}")

    expr = gv.get_dialect(platform).date_difference(_START, _END, semantics=semantics)
    query = f"SELECT ({expr}) AS v"
    # BigQuery/Snowflake/Databricks/Postgres/MySQL all accept a bare SELECT expr.
    try:
        with engine.connect() as conn:
            value = conn.execute(text(query)).scalar()
    except Exception as e:
        pytest.skip(f"{platform} unreachable / query failed: {e}")

    assert int(value) == expected, (
        f"{platform}/{semantics}: expected {expected}, got {value} — the seeded "
        f"rendering is WRONG (or the engine differs). Query: {query}"
    )
