"""
bigquery_hydrator.py — turn catalog TableSpecs into BigQuery datasets + tables.

Design → BigQuery conventions (mirrors the app's own publisher_agent /
schema_inspector so hydrated tables line up with what the pipeline expects):

  * The BigQuery *dataset* is the medallion layer: bronze | silver | gold.
    (Design-time catalogs acn_source / acn_aggregated / acn_consumption map to
    bronze / silver / gold respectively — see DESIGN_CATALOG_TO_LAYER.)
  * The BigQuery table FQN is `{project}.{layer}.{table_name}`.
  * Column types are translated via type_mapping.to_bigquery_type.

"As and when necessary": generation is idempotent — CREATE SCHEMA/TABLE
IF NOT EXISTS by default, so re-running only fills what's missing. Pass
replace=True to CREATE OR REPLACE (destructive; drops existing data).

This module does NOT run at import time and is not wired into the app. Use the
`hydrate()` function or the `hydrate.py` CLI explicitly.

The DDL builders are pure (no google deps). Only `execute_statements()` needs
`google-cloud-bigquery`, imported lazily so dry-run works with zero extra deps.
"""

from dataclasses import dataclass

from type_mapping import to_bigquery_type

# Design-time catalog prefix → BigQuery dataset (medallion layer). Kept for
# reference / cross-checking; hydration keys off the table's own `layer` field.
DESIGN_CATALOG_TO_LAYER = {
    "acn_source": "bronze",
    "acn_aggregated": "silver",
    "acn_consumption": "gold",
}

_VALID_LAYERS = {"bronze", "silver", "gold"}


@dataclass
class HydrationPlan:
    """The full set of statements to bring BigQuery up to the catalog."""
    schemas: list        # list[str] CREATE SCHEMA statements (deduped)
    tables: list         # list[str] CREATE TABLE statements
    inserts: list        # list[str] INSERT statements (only when sample_data present)
    skipped: list = None  # list[str] table names skipped (no columns to create)

    def __post_init__(self):
        if self.skipped is None:
            self.skipped = []

    @property
    def all_statements(self) -> list:
        return [*self.schemas, *self.tables, *self.inserts]

    def to_sql(self) -> str:
        return "\n\n".join(self.all_statements) + ("\n" if self.all_statements else "")


def dataset_for(table, dataset_override: str | None = None) -> str:
    """
    Resolve the BigQuery dataset for a table. Prefers an explicit override, then
    the table's medallion `layer`, then the design-catalog mapping.
    """
    if dataset_override:
        return dataset_override
    layer = (table.layer or "").lower()
    if layer in _VALID_LAYERS:
        return layer
    mapped = DESIGN_CATALOG_TO_LAYER.get((table.catalog or "").lower())
    if mapped:
        return mapped
    raise ValueError(
        f"cannot resolve a BigQuery dataset for table {table.table_name!r} "
        f"(layer={table.layer!r}, catalog={table.catalog!r})"
    )


def _bq(identifier: str) -> str:
    """Backtick-quote a BigQuery identifier component."""
    return f"`{identifier}`"


def table_fqn(project: str, table, dataset_override: str | None = None) -> str:
    ds = dataset_for(table, dataset_override)
    return f"{project}.{ds}.{table.table_name}"


def _escape_str_option(text: str) -> str:
    """Escape a string for use inside an OPTIONS(description="...") clause."""
    return (text or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()


def column_ddl(col) -> str:
    """Build one column definition line for a CREATE TABLE."""
    bq_type = to_bigquery_type(col.data_type)
    # A primary-key column must be NOT NULL in BigQuery.
    not_null = " NOT NULL" if (not col.nullable or col.is_pk) else ""
    desc = _escape_str_option(col.description)
    opts = f' OPTIONS(description="{desc}")' if desc else ""
    return f"  {_bq(col.name)} {bq_type}{not_null}{opts}"


def create_schema_ddl(project: str, dataset: str, location: str | None = None) -> str:
    loc = f' OPTIONS(location="{location}")' if location else ""
    return f"CREATE SCHEMA IF NOT EXISTS {_bq(f'{project}.{dataset}')}{loc};"


def create_table_ddl(project: str, table, *, replace: bool = False,
                     dataset_override: str | None = None) -> str:
    fqn = table_fqn(project, table, dataset_override)
    verb = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE IF NOT EXISTS"

    lines = [column_ddl(c) for c in table.columns]
    pk = table.pk_columns
    if pk:
        # BigQuery supports unenforced PRIMARY KEY constraints.
        pk_cols = ", ".join(_bq(c) for c in pk)
        lines.append(f"  PRIMARY KEY ({pk_cols}) NOT ENFORCED")

    body = ",\n".join(lines)
    tbl_desc = _escape_str_option(table.description)
    tbl_opts = f'\nOPTIONS(description="{tbl_desc}")' if tbl_desc else ""
    return f"{verb} {_bq(fqn)} (\n{body}\n){tbl_opts};"


def _sql_literal(value, bq_type: str) -> str:
    if value is None:
        return "NULL"
    t = bq_type.upper()
    if t == "BOOL":
        return "TRUE" if (value is True or str(value).lower() in ("true", "1", "yes")) else "FALSE"
    if t in ("INT64", "FLOAT64") or t.startswith(("NUMERIC", "BIGNUMERIC")):
        return str(value)
    if t in ("DATE", "TIMESTAMP", "DATETIME"):
        esc = str(value).replace("'", "''")
        return f"{t} '{esc}'" if t != "TIMESTAMP" else f"TIMESTAMP '{esc}'"
    # STRING / BYTES / JSON / fallback
    esc = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{esc}'"


def insert_ddl(project: str, table, *, dataset_override: str | None = None) -> str | None:
    """Build an INSERT for a table's sample_data rows, or None if there are none."""
    if not table.sample_data:
        return None
    fqn = table_fqn(project, table, dataset_override)
    col_names = [c.name for c in table.columns]
    col_types = {c.name: to_bigquery_type(c.data_type) for c in table.columns}
    cols_sql = ", ".join(_bq(c) for c in col_names)

    rows_sql = []
    for row in table.sample_data:
        vals = [_sql_literal(row.get(c), col_types[c]) for c in col_names]
        rows_sql.append("  (" + ", ".join(vals) + ")")
    values_sql = ",\n".join(rows_sql)
    return f"INSERT INTO {_bq(fqn)} ({cols_sql}) VALUES\n{values_sql};"


def build_plan(tables, project: str, *, replace: bool = False,
               location: str | None = None, load_sample_data: bool = False,
               dataset_override: str | None = None) -> HydrationPlan:
    """
    Build the ordered CREATE SCHEMA / CREATE TABLE / (optional) INSERT plan.
    """
    if not project:
        raise ValueError("a BigQuery project id is required")

    schema_stmts, seen_ds = [], set()
    table_stmts, insert_stmts, skipped = [], [], []
    for t in tables:
        # A table with no columns can't be created — skip it (some catalog files
        # carry stub entries that only reference a table by name). Record it so
        # the skip is visible rather than silent.
        if not t.columns:
            skipped.append(t.full_name or t.table_name)
            continue
        ds = dataset_for(t, dataset_override)
        if ds not in seen_ds:
            seen_ds.add(ds)
            schema_stmts.append(create_schema_ddl(project, ds, location))
        table_stmts.append(create_table_ddl(project, t, replace=replace,
                                             dataset_override=dataset_override))
        if load_sample_data:
            ins = insert_ddl(project, t, dataset_override=dataset_override)
            if ins:
                insert_stmts.append(ins)
    return HydrationPlan(schema_stmts, table_stmts, insert_stmts, skipped)


def execute_statements(statements, *, project: str, location: str | None = None) -> list:
    """
    Execute a list of DDL/DML statements against BigQuery, one at a time.

    Imports google-cloud-bigquery lazily so dry-run needs no extra deps. Returns
    a list of {"statement": ..., "status": "ok"|"error", "error"?: str}.
    Continues past individual failures so one bad statement doesn't abort the run.
    """
    from google.cloud import bigquery  # lazy — only needed for live runs

    client = bigquery.Client(project=project, location=location)
    results = []
    for stmt in statements:
        try:
            client.query(stmt).result()
            results.append({"statement": stmt, "status": "ok"})
        except Exception as exc:  # noqa: BLE001
            results.append({"statement": stmt, "status": "error", "error": repr(exc)})
    return results


def hydrate(tables, *, project: str, dry_run: bool = True, replace: bool = False,
            location: str | None = None, load_sample_data: bool = False,
            dataset_override: str | None = None) -> dict:
    """
    Top-level entry: build the plan and (optionally) execute it.

    dry_run=True (default) does NOT touch BigQuery — it returns the SQL so you
    can review or run it by hand. dry_run=False executes it live.
    """
    plan = build_plan(
        tables, project, replace=replace, location=location,
        load_sample_data=load_sample_data, dataset_override=dataset_override,
    )
    out = {
        "project": project,
        "dry_run": dry_run,
        "dataset_count": len(plan.schemas),
        "table_count": len(plan.tables),
        "insert_count": len(plan.inserts),
        "skipped": list(plan.skipped),
        "sql": plan.to_sql(),
    }
    if not dry_run:
        out["execution"] = execute_statements(
            plan.all_statements, project=project, location=location,
        )
    return out
