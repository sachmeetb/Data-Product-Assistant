"""
test_hydration.py — tests for the standalone BigQuery hydration package.

Runs two ways:
  * plain:  python tests/test_hydration.py
  * pytest: pytest tests/test_hydration.py -q

Dry-run only — no google-cloud-bigquery import, no network, no credentials.
Also exercises the REAL backend/data/utility_catalog.json so the DDL builders
are validated against the actual catalog shape (13 silver + 1 bronze tables).
"""

import os
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND / "hydration"))   # flat imports (type_mapping, ...)

from type_mapping import to_bigquery_type, UnknownTypeError          # noqa: E402
from catalog_loader import (                                          # noqa: E402
    load_catalog, load_all_tables, TableSpec, ColumnSpec,
)
from bigquery_hydrator import (                                       # noqa: E402
    dataset_for, table_fqn, create_schema_ddl, create_table_ddl,
    column_ddl, insert_ddl, build_plan, hydrate, DESIGN_CATALOG_TO_LAYER,
)

_REAL_CATALOG = _BACKEND / "data" / "utility_catalog.json"
_GOLD_CATALOG = _BACKEND / "data" / "utility_catalog_pre_ddi.json"


# ── type mapping ───────────────────────────────────────────────────────────────

def test_scalar_type_mappings():
    cases = {
        "STRING": "STRING", "BIGINT": "INT64", "INT": "INT64",
        "INTEGER": "INT64", "DOUBLE": "FLOAT64", "FLOAT": "FLOAT64",
        "BOOL": "BOOL", "BOOLEAN": "BOOL", "TIMESTAMP": "TIMESTAMP",
        "DATE": "DATE",
    }
    for src, want in cases.items():
        assert to_bigquery_type(src) == want, f"{src} → {want}"
        assert to_bigquery_type(src.lower()) == want, f"lowercase {src}"


def test_decimal_mapping():
    assert to_bigquery_type("DECIMAL(18,2)") == "NUMERIC(18, 2)"
    assert to_bigquery_type("decimal(9, 4)") == "NUMERIC(9, 4)"
    assert to_bigquery_type("NUMERIC(38,9)") == "NUMERIC(38, 9)"
    # Beyond NUMERIC limits → BIGNUMERIC
    assert to_bigquery_type("DECIMAL(50,10)") == "BIGNUMERIC(50, 10)"
    assert to_bigquery_type("DECIMAL") == "NUMERIC"


def test_unknown_type_fallback_and_strict():
    assert to_bigquery_type("SOMETHING_WEIRD") == "STRING"
    assert to_bigquery_type("") == "STRING"
    assert to_bigquery_type("ARRAY<STRING>") == "STRING"
    try:
        to_bigquery_type("SOMETHING_WEIRD", strict=True)
        assert False, "strict should raise"
    except UnknownTypeError:
        pass


# ── catalog loading against the real file ──────────────────────────────────────

def test_load_real_catalog_shape():
    tables = load_catalog(_REAL_CATALOG)
    assert tables, "real catalog should have tables"
    by_name = {t.table_name: t for t in tables}
    # Known silver + bronze tables from the current catalog.
    assert "campaign_impressions_conformed" in by_name
    assert "campaign_clicks_conformed" in by_name
    assert "raw_ad_server_events" in by_name

    silver = [t for t in tables if t.layer == "silver"]
    bronze = [t for t in tables if t.layer == "bronze"]
    assert len(silver) == 13, f"expected 13 silver tables, got {len(silver)}"
    assert len(bronze) == 1, f"expected 1 bronze table, got {len(bronze)}"

    imp = by_name["campaign_impressions_conformed"]
    assert imp.catalog == "acn_aggregated"
    assert imp.schema_name == "marketing"
    assert imp.pk_columns == ["impression_id"]
    col = {c.name: c for c in imp.columns}
    assert col["campaign_id"].data_type == "STRING"
    assert col["timestamp"].data_type == "TIMESTAMP"
    assert col["impression_id"].is_pk is True
    assert col["campaign_id"].nullable is False


def test_layer_filter():
    only_silver = load_catalog(_REAL_CATALOG, layers=("silver",))
    assert only_silver and all(t.layer == "silver" for t in only_silver)


def test_load_all_dedup_across_files():
    if not _GOLD_CATALOG.exists():
        return  # optional file; skip if absent
    merged = load_all_tables([_REAL_CATALOG, _GOLD_CATALOG])
    keys = [(t.layer, t.table_name) for t in merged]
    assert len(keys) == len(set(keys)), "load_all_tables must de-duplicate"
    # The gold catalog's fact/dim tables should now be present, name derived
    # from full_name where the entry lacked an explicit table_name.
    names = {t.table_name for t in merged}
    assert "fact_campaign_performance" in names
    assert "dim_campaign" in names


def test_column_less_stub_tables_are_skipped_not_crashed():
    if not _GOLD_CATALOG.exists():
        return
    merged = load_all_tables([_REAL_CATALOG, _GOLD_CATALOG])
    plan = build_plan(merged, "proj")
    # Stub entries (only full_name, no columns) must be skipped, not emitted.
    assert plan.skipped, "expected some column-less stub tables to be skipped"
    for stmt in plan.tables:
        assert "(\n)" not in stmt, "no empty-column CREATE TABLE should be produced"


# ── dataset resolution ─────────────────────────────────────────────────────────

def test_dataset_for_layer_and_catalog():
    t = TableSpec("acn_aggregated.marketing.x", "acn_aggregated", "marketing", "x", "silver")
    assert dataset_for(t) == "silver"
    # layer wins; falls back to catalog mapping when layer is unknown
    t2 = TableSpec("acn_consumption.marketing.y", "acn_consumption", "marketing", "y", "")
    assert dataset_for(t2) == "gold"
    assert dataset_for(t, dataset_override="staging") == "staging"
    assert DESIGN_CATALOG_TO_LAYER["acn_source"] == "bronze"


def test_dataset_for_unresolvable_raises():
    t = TableSpec("weird.x", "weird_catalog", "s", "x", "")
    try:
        dataset_for(t)
        assert False, "should raise for unresolvable dataset"
    except ValueError:
        pass


def test_table_fqn():
    t = TableSpec("acn_aggregated.marketing.x", "acn_aggregated", "marketing", "x", "silver")
    assert table_fqn("proj", t) == "proj.silver.x"


# ── DDL generation ─────────────────────────────────────────────────────────────

def _sample_table():
    return TableSpec(
        full_name="acn_consumption.marketing.fact_x",
        catalog="acn_consumption", schema_name="marketing",
        table_name="fact_x", layer="gold",
        description='Fact "x" table',
        columns=[
            ColumnSpec("id", "BIGINT", nullable=False, is_pk=True, description="PK id"),
            ColumnSpec("amount", "DECIMAL(18,2)", nullable=True, description="the amount"),
            ColumnSpec("name", "STRING", nullable=True),
        ],
    )


def test_create_schema_ddl():
    ddl = create_schema_ddl("proj", "gold", "us-central1")
    assert ddl == 'CREATE SCHEMA IF NOT EXISTS `proj.gold` OPTIONS(location="us-central1");'
    assert "IF NOT EXISTS" in create_schema_ddl("proj", "gold")


def test_column_ddl_types_notnull_desc():
    t = _sample_table()
    id_ddl = column_ddl(t.columns[0])
    assert "`id` INT64 NOT NULL" in id_ddl
    assert 'OPTIONS(description="PK id")' in id_ddl
    amt_ddl = column_ddl(t.columns[1])
    assert "`amount` NUMERIC(18, 2)" in amt_ddl
    assert "NOT NULL" not in amt_ddl


def test_create_table_ddl_idempotent_and_pk():
    t = _sample_table()
    ddl = create_table_ddl("proj", t)
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS `proj.gold.fact_x` (")
    assert "PRIMARY KEY (`id`) NOT ENFORCED" in ddl
    assert 'OPTIONS(description="Fact \\"x\\" table")' in ddl  # description escaped


def test_create_table_ddl_replace():
    ddl = create_table_ddl("proj", _sample_table(), replace=True)
    assert ddl.startswith("CREATE OR REPLACE TABLE")


def test_insert_ddl_with_sample_data():
    t = _sample_table()
    t.sample_data = [
        {"id": 1, "amount": 12.50, "name": "a'b"},
        {"id": 2, "amount": None, "name": None},
    ]
    ins = insert_ddl("proj", t)
    assert ins.startswith("INSERT INTO `proj.gold.fact_x` (`id`, `amount`, `name`) VALUES")
    assert "(1, 12.5, 'a\\'b')" in ins
    assert "(2, NULL, NULL)" in ins


def test_insert_ddl_none_when_no_sample_data():
    assert insert_ddl("proj", _sample_table()) is None


# ── plan + full dry-run against real catalog ───────────────────────────────────

def test_build_plan_dedups_datasets():
    tables = load_catalog(_REAL_CATALOG)
    plan = build_plan(tables, "proj")
    # 13 silver + 1 bronze → 2 datasets, 14 tables.
    assert len(plan.schemas) == 2
    assert len(plan.tables) == 14
    assert all(s.startswith("CREATE SCHEMA IF NOT EXISTS") for s in plan.schemas)
    # schema DDL quotes the FQN as `proj.silver` — take the dataset part.
    ds_names = {s.split("`")[1].split(".")[-1] for s in plan.schemas}
    assert ds_names == {"silver", "bronze"}


def test_hydrate_dry_run_real_catalog():
    tables = load_catalog(_REAL_CATALOG)
    result = hydrate(tables, project="demo-proj", dry_run=True)
    assert result["dry_run"] is True
    assert result["table_count"] == 14
    assert result["dataset_count"] == 2
    sql = result["sql"]
    assert "CREATE SCHEMA IF NOT EXISTS `demo-proj.silver`" in sql
    assert "`demo-proj.silver.campaign_impressions_conformed`" in sql
    assert "`demo-proj.bronze.raw_ad_server_events`" in sql
    # No BigQuery types leaked as Databricks types
    assert "BIGINT" not in sql and "DOUBLE" not in sql
    # Types were translated
    assert "STRING" in sql and "TIMESTAMP" in sql


def test_hydrate_requires_project():
    try:
        hydrate([], project="", dry_run=True)
        assert False, "should require a project"
    except ValueError:
        pass


# ── plain-python runner ─────────────────────────────────────────────────────────

def _run_all():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, exc))
            print(f"  FAIL  {name}: {exc!r}")
    print(f"\n{passed}/{len(tests)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
