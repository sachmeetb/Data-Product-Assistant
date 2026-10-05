#!/usr/bin/env python3
"""
Generate Python/Pandera data quality test code from Neo4j graph rules.

Reads :PropertyShape nodes from the knowledge graph and produces:
  output_dir/validators/<schema>_<table>.py  — Pandera schema module per table
  output_dir/run_all.py                       — master test runner
  output_dir/requirements.txt

Usage:
    python generate_python_tests.py [output_dir] [options]

    output_dir   destination directory (default: ./dq_tests_python)

Options:
    --host        Neo4j host (default: localhost)
    --bolt-port   Bolt port (default: 7687)
    --username    Neo4j username (default: neo4j)
    --password    Neo4j password (default: your_password)
    --database    Neo4j database name (default: neo4j)
"""

import sys
import os
import re
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError
except ImportError:
    print("ERROR: neo4j driver required. Install: pip install neo4j", file=sys.stderr)
    sys.exit(1)

DEFAULT_HOST = "localhost"
DEFAULT_BOLT_PORT = 7687
DEFAULT_USERNAME = "neo4j"
DEFAULT_PASSWORD = "your_password"
DEFAULT_DATABASE = "neo4j"

# Fetch all DQ rules from the graph
_RULES_RETURN = """
RETURN
  ds.uri                  AS dataset_uri,
  ps.ruleType             AS rule_type,
  ps.path                 AS column_name,
  ps.severity             AS severity,
  ps.description          AS description,
  ps.confidence           AS confidence,
  ps.minInclusive         AS min_inclusive,
  ps.maxInclusive         AS max_inclusive,
  ps.minDate              AS min_date,
  ps.maxDate              AS max_date,
  ps.uniquenessThreshold  AS uniqueness_threshold,
  ps.uniquenessRatio      AS uniqueness_ratio,
  ps.coverageThreshold    AS coverage_threshold,
  ps.coverage             AS coverage,
  ps.evidenceNullRate     AS evidence_null_rate,
  col.dataType            AS col_type,
  ref.uri                 AS ref_dataset_uri,
  allowed_values
ORDER BY ds.uri, ps.ruleType, ps.path
"""

RULES_QUERY = """
MATCH (ds:Dataset)-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
// Only approved rules generate tests. Rejected and pending_review rules are
// invisible to downstream consumers (marketplace, scoring) by design — keep
// the test surface aligned with them. coalesce defaults legacy nodes that
// pre-date the status field to 'approved'.
WHERE coalesce(ps.status, 'approved') = 'approved'
OPTIONAL MATCH (ps)-[:ON_COLUMN]->(col:Column)
OPTIONAL MATCH (ps)-[:REFERENCES_DATASET]->(ref:Dataset)
OPTIONAL MATCH (ps)-[:ALLOWED_VALUE]->(tv:TopValue)
WITH ds, ps, col, ref, collect(tv.value) AS allowed_values
""" + _RULES_RETURN

# Project-scoped variant — only returns rules for datasets under the given
# :Project node's catalogs. Keeps tests from leaking across projects that
# share a single Neo4j instance.
RULES_QUERY_SCOPED = """
MATCH (:Project {projectCode: $project_code})-[:HAS_CATALOG]->(:Catalog)
      -[:DCAT_DATASET]->(ds:Dataset)
MATCH (ds)-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
WHERE coalesce(ps.status, 'approved') = 'approved'
OPTIONAL MATCH (ps)-[:ON_COLUMN]->(col:Column)
OPTIONAL MATCH (ps)-[:REFERENCES_DATASET]->(ref:Dataset)
OPTIONAL MATCH (ps)-[:ALLOWED_VALUE]->(tv:TopValue)
WITH ds, ps, col, ref, collect(tv.value) AS allowed_values
""" + _RULES_RETURN

# Fetch FK constraint details (which columns reference which target columns)
FK_QUERY = """
MATCH (ds:Dataset)-[fk:REFERENCES]->(ref:Dataset)
RETURN
  ds.uri               AS dataset_uri,
  fk.columns           AS fk_columns,
  fk.referencedColumns AS ref_columns,
  ref.uri              AS ref_uri
"""

FK_QUERY_SCOPED = """
MATCH (:Project {projectCode: $project_code})-[:HAS_CATALOG]->(:Catalog)
      -[:DCAT_DATASET]->(ds:Dataset)-[fk:REFERENCES]->(ref:Dataset)
RETURN
  ds.uri               AS dataset_uri,
  fk.columns           AS fk_columns,
  fk.referencedColumns AS ref_columns,
  ref.uri              AS ref_uri
"""

# Product (dprod) variants — rules authored ON THE CONTRACT, keyed to the deployed
# product view. Mirror the catalog aliases so downstream code-emission is unchanged
# (column from pc.name; the caller builds the "<view_schema>.vw_<safe>" key). Edges
# match routers/marketplace.py + routers/odcs.py. No dq_rule_generation for dprod.
RULES_QUERY_DPROD = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)<-[:ON_DPROD_COLUMN]-(ps:PropertyShape)
WHERE ps.ruleSource IN ['spec', 'domain', 'user']
  AND coalesce(ps.status, 'approved') = 'approved'
OPTIONAL MATCH (ps)-[:ALLOWED_VALUE]->(tv:TopValue)
WITH ods, pc, ps, collect(tv.value) AS allowed_values
RETURN
  coalesce(ods.physicalName, ods.name) AS physical_name,
  ps.ruleType             AS rule_type,
  pc.name                 AS column_name,
  ps.severity             AS severity,
  ps.description          AS description,
  ps.confidence           AS confidence,
  ps.minInclusive         AS min_inclusive,
  ps.maxInclusive         AS max_inclusive,
  ps.minDate              AS min_date,
  ps.maxDate              AS max_date,
  ps.uniquenessThreshold  AS uniqueness_threshold,
  ps.uniquenessRatio      AS uniqueness_ratio,
  ps.coverageThreshold    AS coverage_threshold,
  ps.coverage             AS coverage,
  ps.evidenceNullRate     AS evidence_null_rate,
  pc.dataType             AS col_type,
  null                    AS ref_dataset_uri,
  allowed_values
ORDER BY physical_name, ps.ruleType, pc.name
"""

# FK constraints between the product's OWN output datasets (DPROD_PROPAGATE_FK
# mirrors catalog FK edges onto the :DProdOutputDataset chain — the same edges
# generate_view_ddl.py:DPROD_FK_QUERY reads). Referenced relation is the deployed
# ref view; the caller builds ref_uri as "<view_schema>.vw_<safe(ref_physical)>".
FK_QUERY_DPROD = """
MATCH (dc:DataContract {id: $contract_id})-[:MATERIALISES_AS]->(dp:DProdDataProduct)
MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(ds:DProdOutputDataset)
MATCH (ds)-[fk:REFERENCES]->(ref:DProdOutputDataset)
RETURN
  coalesce(ds.physicalName, ds.name)   AS physical_name,
  fk.columns                           AS fk_columns,
  fk.referencedColumns                 AS ref_columns,
  coalesce(ref.physicalName, ref.name) AS ref_physical_name
"""


# ── helpers ───────────────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    """Sanitize a physical name into the deployed view suffix. MUST stay
    byte-identical to generate_view_ddl.py:_safe_name — the dprod DQ suite must
    reference the same ``vw_<safe>`` relation the serving view path deploys."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).lower()


def uri_to_parts(uri: str) -> tuple[str, str]:
    """Split a Dataset URI into (namespace, table).

    Accepts ``dataset:{schema}.{table}``, project-scoped
    ``dataset:{project_code}:{schema}.{table}``, and the dprod key form
    ``{view_schema}.vw_{safe}`` where ``view_schema`` may be multi-level
    (e.g. Databricks ``catalog.schema`` → ``catalog.schema.vw_x``).

    ``table`` is the trailing (leaf) segment; ``namespace`` is everything before
    it (one or more dot-joined parts). Splitting on the LAST dot keeps the
    2-level case identical while generalizing to any depth; ``_quote_table``
    re-splits the namespace so each level is quoted.
    """
    qualified = uri.split(":")[-1]
    namespace, table = qualified.rsplit(".", 1)
    return namespace, table


def module_name(schema: str, table: str) -> str:
    # Used verbatim as a Python module filename + import name, so it must be a
    # valid identifier — a multi-level namespace (Databricks catalog.schema)
    # carries dots. _safe_name sanitizes every non-identifier char (incl. dots
    # and hyphens).
    return _safe_name(f"{schema}_{table}")


DATE_TYPES = {
    "date", "timestamp", "timestamp without time zone",
    "timestamp with time zone", "timestamptz",
}


def is_date_type(col_type: str | None) -> bool:
    return col_type is not None and col_type.lower().split("(")[0].strip() in DATE_TYPES


def py_repr(v) -> str:
    """Safe Python literal for a graph value."""
    if v is None:
        return "None"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return repr(v)
    return repr(str(v))


def py_date_repr(v) -> str:
    """Emit datetime.date(...) for a date bound (PostgreSQL date columns load as datetime.date)."""
    if v is None:
        return "None"
    from datetime import date as _date
    s = str(v)[:10]  # take YYYY-MM-DD portion
    y, m, d = s.split("-")
    return f"datetime.date({int(y)}, {int(m)}, {int(d)})"


def _coerce_bool_set(raw_vals) -> list:
    # Profiler serializes booleans as lowercase strings in :TopValue.value,
    # but pandas reads PG boolean columns as native True/False. Pandera's
    # Check.isin does a Python `in` — str vs bool never matches, so every
    # row fails. Accept the usual token variants.
    TRUE_TOKENS = {"true", "t", "1", "yes", "y"}
    FALSE_TOKENS = {"false", "f", "0", "no", "n"}
    out: set[bool] = set()
    for v in raw_vals:
        if isinstance(v, bool):
            out.add(v)
            continue
        s = str(v).strip().lower()
        if s in TRUE_TOKENS:
            out.add(True)
        elif s in FALSE_TOKENS:
            out.add(False)
    return sorted(out)


# ── code generators ───────────────────────────────────────────────────────────

def generate_validator_module(
    schema: str,
    table: str,
    rules: list[dict],
    fk_rows: list[dict],
    generated_at: str,
) -> str:
    """Return the source code for a Pandera validator module."""

    table_qualified = f"{schema}.{table}"
    ri_rules = [r for r in rules if r["rule_type"] == "referentialIntegrity"]

    # Per-column rules (excluding RI which is handled separately)
    col_rules: dict[str, list[dict]] = defaultdict(list)
    for r in rules:
        if r.get("column_name") and r["rule_type"] != "referentialIntegrity":
            col_rules[r["column_name"]].append(r)

    lines: list[str] = []

    # Module header
    lines += [
        f'"""',
        f'Data quality validators for {table_qualified}.',
        f'Generated from Neo4j graph on {generated_at}.',
        f'Regenerate with: python generate_python_tests.py',
        f'"""',
        "import datetime",
        "import pandas as pd",
        "import pandera as pa",
        "from pandera import Column, DataFrameSchema, Check",
        "",
        "",
        "def _detect_platform(engine) -> str:",
        '    """Detect the database platform from the SQLAlchemy engine URL."""',
        "    url_str = str(engine.url)",
        "    if url_str.startswith(('postgresql', 'postgres')):",
        "        return 'postgres'",
        "    if url_str.startswith('mysql'):",
        "        return 'mysql'",
        "    if url_str.startswith('snowflake'):",
        "        return 'snowflake'",
        "    if url_str.startswith('databricks'):",
        "        return 'databricks'",
        "    return 'unknown'",
        "",
        "",
        "def _quote_identifier(name: str, platform: str) -> str:",
        '    """Quote a SQL identifier for the target platform.',
        "",
        "    Snowflake folds unquoted DDL (dlt/dbt loads, deployed views) to UPPER,",
        "    so a quoted read must upper-fold the name to address the real physical",
        "    object; a lowercase-quoted name is a case-sensitive miss.",
        '    """',
        "    if platform == 'snowflake':",
        "        name = name.upper()",
        "    if platform in ('postgres', 'snowflake', 'unknown'):",
        '        return f\'"{name}"\'',
        "    else:  # mysql, databricks",
        "        return f'`{name}`'",
        "",
        "",
        f"TABLE_SCHEMA = {repr(schema)}",
        f"TABLE_NAME = {repr(table)}",
        f"TABLE_QUALIFIED = {repr(table_qualified)}",
        "",
    ]

    # ── Pandera DataFrameSchema ───────────────────────────────────────────────
    lines += [
        "def build_schema() -> DataFrameSchema:",
        f'    """Pandera schema for {table_qualified}, derived from graph DQ rules."""',
        "    return DataFrameSchema(",
        f"        name=TABLE_QUALIFIED,",
        "        columns={",
    ]

    for col_name in sorted(col_rules):
        checks: list[str] = []
        nullable = True

        for r in col_rules[col_name]:
            rt = r["rule_type"]
            if rt == "mandatory":
                nullable = False
            elif rt == "range":
                min_v = r["min_inclusive"] if r["min_inclusive"] is not None else r.get("min_date")
                max_v = r["max_inclusive"] if r["max_inclusive"] is not None else r.get("max_date")
                repr_fn = py_date_repr if is_date_type(r.get("col_type")) else py_repr
                if min_v is not None and max_v is not None:
                    checks.append(
                        f"Check.in_range({repr_fn(min_v)}, {repr_fn(max_v)}, "
                        f"include_min=True, include_max=True)"
                    )
                elif min_v is not None:
                    checks.append(f"Check.greater_than_or_equal_to({repr_fn(min_v)})")
                elif max_v is not None:
                    checks.append(f"Check.less_than_or_equal_to({repr_fn(max_v)})")
            elif rt == "unique":
                threshold = r.get("uniqueness_threshold") or 0.99
                checks.append(
                    f"Check(lambda s: s.nunique() / max(len(s), 1) >= {threshold}, "
                    f'name="uniqueness_ratio", error="uniqueness ratio below {threshold}")'
                )
            elif rt == "allowedValues":
                raw_vals = [v for v in (r.get("allowed_values") or []) if v is not None]
                if r.get("col_type") == "boolean":
                    vals = _coerce_bool_set(raw_vals)
                else:
                    vals = sorted(str(v) for v in raw_vals)
                if vals:
                    checks.append(f"Check.isin({repr(vals)})")

        lines.append(f"            {repr(col_name)}: Column(")
        lines.append(f"                nullable={nullable},")
        if checks:
            lines.append("                checks=[")
            for chk in checks:
                lines.append(f"                    {chk},")
            lines.append("                ],")
        lines.append("            ),")

    lines += [
        "        },",
        "        coerce=False,",
        "        strict=False,  # ignore columns not covered by rules",
        "    )",
        "",
    ]

    # ── Referential integrity ─────────────────────────────────────────────────
    if ri_rules:
        lines += [
            "def check_referential_integrity(df: pd.DataFrame, engine) -> list[dict]:",
            '    """Check FK references against target tables. Requires a live DB engine."""',
            "    _platform = _detect_platform(engine)",
            "    results = []",
        ]

        for r in ri_rules:
            col = r["column_name"]
            ref_uri = r.get("ref_dataset_uri") or ""
            if not ref_uri:
                continue
            ref_schema, ref_tbl = uri_to_parts(ref_uri)

            # Find the matching target column from the REFERENCES relationship
            target_col: str | None = None
            for fk in fk_rows:
                fk_cols = list(fk.get("fk_columns") or [])
                ref_cols = list(fk.get("ref_columns") or [])
                if col in fk_cols and fk.get("ref_uri") == ref_uri:
                    idx = fk_cols.index(col)
                    if idx < len(ref_cols):
                        target_col = ref_cols[idx]
                    break

            lines += [
                f"    # {col} → {ref_schema}.{ref_tbl}",
                f"    try:",
                f"        ref_col = {repr(target_col)}",
            ]
            if target_col:
                lines += [
                    f"        _q_tbl = '.'.join(_quote_identifier(_p, _platform) for _p in {repr([*ref_schema.split('.'), ref_tbl])})",
                    f"        _q_col = _quote_identifier({repr(target_col)}, _platform)",
                    "        ref_q = f'SELECT DISTINCT {_q_col} FROM {_q_tbl}'",
                    f"        ref_df = pd.read_sql(ref_q, engine)",
                    "        if _platform == 'snowflake':",
                    "            ref_df.columns = [str(_c).lower() for _c in ref_df.columns]",
                    f"        ref_vals = set(ref_df[{repr(target_col)}].dropna().astype(str))",
                ]
            else:
                lines += [
                    f"        _q_tbl_fb = '.'.join(_quote_identifier(_p, _platform) for _p in {repr([*ref_schema.split('.'), ref_tbl])})",
                    "        ref_df = pd.read_sql(f'SELECT * FROM {_q_tbl_fb}', engine)",
                    f"        ref_vals = set(ref_df.iloc[:, 0].dropna().astype(str))",
                ]
            lines += [
                f"        fk_vals = set(df[{repr(col)}].dropna().astype(str)) if {repr(col)} in df.columns else set()",
                f"        orphans = fk_vals - ref_vals",
                f"        results.append({{",
                f"            'table': TABLE_QUALIFIED, 'column': {repr(col)},",
                f"            'rule_type': 'referentialIntegrity', 'severity': 'sh:Violation',",
                f"            'ref_table': {repr(f'{ref_schema}.{ref_tbl}')},",
                f"            'status': 'FAIL' if orphans else 'PASS',",
                f"            'detail': f'{{len(orphans)}} orphan value(s) not found in {ref_schema}.{ref_tbl}' if orphans else 'OK',",
                f"        }})",
                f"    except Exception as exc:",
                f"        results.append({{",
                f"            'table': TABLE_QUALIFIED, 'column': {repr(col)},",
                f"            'rule_type': 'referentialIntegrity', 'status': 'ERROR', 'detail': str(exc),",
                f"        }})",
            ]

        lines += ["    return results", ""]

    # ── validate() entry point ────────────────────────────────────────────────
    lines += [
        "def validate(df: pd.DataFrame, engine=None) -> list[dict]:",
        '    """Validate DataFrame against all DQ rules. Returns list of result dicts."""',
        "    results = []",
        "    schema = build_schema()",
        "    try:",
        "        schema.validate(df, lazy=True)",
        "        results.append({",
        "            'table': TABLE_QUALIFIED, 'rule_type': 'pandera_schema',",
        "            'status': 'PASS', 'detail': 'All Pandera checks passed',",
        "        })",
        "    except pa.errors.SchemaErrors as exc:",
        "        for _, row in exc.failure_cases.iterrows():",
        "            results.append({",
        "                'table': TABLE_QUALIFIED,",
        "                'column': row.get('column'),",
        "                'rule_type': str(row.get('check')),",
        "                'severity': 'sh:Violation',",
        "                'status': 'FAIL',",
        "                'detail': str(row.get('failure_case')),",
        "            })",
    ]
    if ri_rules:
        lines += [
            "    if engine is not None:",
            "        results.extend(check_referential_integrity(df, engine))",
        ]
    lines.append("    return results")

    return "\n".join(lines)


def generate_run_all(table_pairs: list[tuple[str, str]], generated_at: str) -> str:
    """Return source code for the master run_all.py runner."""
    mods = [(s, t, module_name(s, t)) for s, t in table_pairs]

    lines: list[str] = [
        "#!/usr/bin/env python3",
        '"""',
        "Run all generated DQ validators against a relational database.",
        f"Generated from Neo4j graph on {generated_at}.",
        "",
        "Usage:",
        "    python run_all.py --conn-string <connection-string> [options]",
        "",
        "Options:",
        "    --conn-string  Database connection string — postgresql://, mysql://,",
        "                   snowflake://, or databricks:// (required)",
        "    --sample N     Max rows to load per table (default: all rows)",
        "    --output DIR   Results output directory (default: results)",
        '"""',
        "import sys",
        "import json",
        "import argparse",
        "import pandas as pd",
        "import sqlalchemy",
        "from datetime import datetime",
        "from pathlib import Path",
        "",
        "",
        "# ── Platform helpers ─────────────────────────────────────────────────────────",
        "",
        "def _get_platform(conn_string: str) -> str:",
        "    cs = conn_string.strip()",
        "    if cs.startswith(('postgresql://', 'postgres://')):",
        "        return 'postgres'",
        "    if cs.startswith('mysql://'):",
        "        return 'mysql'",
        "    if cs.startswith('snowflake://'):",
        "        return 'snowflake'",
        "    if cs.startswith('databricks://'):",
        "        return 'databricks'",
        "    return 'unknown'",
        "",
        "",
        "def _get_sqlalchemy_url(conn_string: str) -> str:",
        '    """Map raw connection string to the right SQLAlchemy driver URL."""',
        "    cs = conn_string.strip()",
        "    if cs.startswith('postgres://'):",
        "        return 'postgresql+psycopg2://' + cs[len('postgres://'):]",
        "    if cs.startswith('postgresql://') and '+' not in cs.split('://')[0]:",
        "        return 'postgresql+psycopg2://' + cs[len('postgresql://'):]",
        "    if cs.startswith('mysql://'):",
        "        try:",
        "            import pymysql  # noqa: F401",
        "        except ImportError:",
        "            raise ImportError('pymysql is required for MySQL connections. pip install pymysql')",
        "        return 'mysql+pymysql://' + cs[len('mysql://'):]",
        "    if cs.startswith('databricks://'):",
        "        return 'databricks+connector://' + cs[len('databricks://'):]",
        "    # snowflake:// is accepted by snowflake-sqlalchemy as-is",
        "    return cs",
        "",
        "",
        "def _quote_identifier(name: str, platform: str) -> str:",
        '    """Quote a SQL identifier for the target platform.',
        "",
        "    Snowflake folds unquoted DDL (dlt/dbt loads, deployed views) to UPPER,",
        "    so a quoted read must upper-fold the name to address the real physical",
        "    object; a lowercase-quoted name is a case-sensitive miss.",
        '    """',
        "    if platform == 'snowflake':",
        "        name = name.upper()",
        "    if platform in ('postgres', 'snowflake', 'unknown'):",
        '        return f\'"{name}"\'',
        "    else:  # mysql, databricks",
        "        return f'`{name}`'",
        "",
        "",
        "def _quote_table(schema: str, table: str, platform: str) -> str:",
        '    """Return a fully-qualified, platform-quoted table reference.',
        "",
        "    ``schema`` may be a multi-level namespace (e.g. Databricks",
        "    ``catalog.schema``); quote EACH dot-segment so a 3-level target renders",
        "    `catalog`.`schema`.`table`. The 2-level case is unchanged.",
        '    """',
        "    parts = [p for p in schema.split('.') if p] + [table]",
        "    return '.'.join(_quote_identifier(p, platform) for p in parts)",
        "",
        "",
    ]

    for s, t, mod in mods:
        lines.append(f"from validators.{mod} import validate as validate_{mod}")

    lines += [
        "",
        "VALIDATORS = [",
    ]
    for s, t, mod in mods:
        lines.append(f"    ({repr(s)}, {repr(t)}, validate_{mod}),")
    lines.append("]")

    lines += [
        "",
        "",
        "# ── Standardized, DWB-independent reporting ──────────────────────────────────",
        "# Run outside Data Workbench there is no Neo4j to load into, so the runner",
        "# writes its own verdict: run_result.json (the v1 contract the serving",
        "# packages emit) + a human-readable report.md, next to the framework results.",
        "",
        "def _write_run_result(output_dir, platform, run_at, all_results, results_file):",
        "    tables = sorted({r.get('table') for r in all_results if r.get('table')})",
        "    fails  = [r for r in all_results if r.get('status') == 'FAIL']",
        "    errs   = [r for r in all_results if r.get('status') == 'ERROR']",
        "    passes = [r for r in all_results if r.get('status') == 'PASS']",
        "    violations = [r for r in fails if r.get('severity') == 'sh:Violation']",
        "    warnings   = [r for r in fails if r.get('severity') != 'sh:Violation']",
        "    tables_failed = sorted({r.get('table') for r in (fails + errs) if r.get('table')})",
        "    steps = []",
        "    for tb in tables:",
        "        trs = [r for r in all_results if r.get('table') == tb]",
        "        p = len([r for r in trs if r.get('status') == 'PASS'])",
        "        f = len([r for r in trs if r.get('status') == 'FAIL'])",
        "        e = len([r for r in trs if r.get('status') == 'ERROR'])",
        "        steps.append({'name': tb, 'status': ('failed' if (f or e) else 'success'),",
        "                      'detail': '%d pass / %d fail / %d error' % (p, f, e)})",
        "    if errs:",
        "        status, error = 'failed', {'class': 'execution_error',",
        "                                   'message': '%d check(s) errored' % len(errs)}",
        "    elif violations:",
        "        status, error = 'failed', {'class': 'validation_failures',",
        "                                   'message': '%d violation check(s) failed' % len(violations)}",
        "    else:",
        "        status, error = 'success', None",
        "    try:",
        "        finished = datetime.now()",
        "        duration_ms = int((finished - datetime.fromisoformat(run_at)).total_seconds() * 1000)",
        "    except Exception:",
        "        finished, duration_ms = datetime.now(), None",
        "    payload = {",
        "        'contract_version': '1', 'operation': 'dq_validation', 'mode': 'pandera',",
        "        'status': status, 'started_at': run_at, 'finished_at': finished.isoformat(),",
        "        'duration_ms': duration_ms, 'target': {'platform': platform},",
        "        'metrics': {",
        "            'framework': 'pandera', 'tables': len(tables),",
        "            'checks_total': len(all_results), 'checks_passed': len(passes),",
        "            'checks_failed': len(fails), 'checks_errored': len(errs),",
        "            'violations_failed': len(violations), 'warnings_failed': len(warnings),",
        "            'tables_failed': tables_failed, 'results_file': str(results_file),",
        "        },",
        "        'steps': steps, 'error': error, 'log_file': None,",
        "    }",
        "    rr = Path(output_dir) / 'run_result.json'",
        "    with open(rr, 'w') as fh:",
        "        json.dump(payload, fh, indent=2, default=str)",
        "    return payload, rr",
        "",
        "",
        "def _write_report_md(output_dir, payload, all_results):",
        "    m = payload['metrics']",
        "    verdict = 'PASSED' if payload['status'] == 'success' else 'FAILED'",
        "    out = ['# DQ Validation Report — Pandera', '',",
        "           '- **Result:** ' + verdict,",
        "           '- **Run at:** ' + str(payload['started_at']),",
        "           '- **Platform:** ' + str(payload['target'].get('platform', '?')),",
        "           '- **Checks:** %s passed / %s failed / %s errored (%s violations, %s warnings)'",
        "           % (m['checks_passed'], m['checks_failed'], m['checks_errored'],",
        "              m['violations_failed'], m['warnings_failed']),",
        "           '', '| Table | Result | Pass | Fail | Error |', '|---|---|---|---|---|']",
        "    tables = sorted({r.get('table') for r in all_results if r.get('table')})",
        "    for tb in tables:",
        "        trs = [r for r in all_results if r.get('table') == tb]",
        "        p = len([r for r in trs if r.get('status') == 'PASS'])",
        "        f = len([r for r in trs if r.get('status') == 'FAIL'])",
        "        e = len([r for r in trs if r.get('status') == 'ERROR'])",
        "        res = 'error' if e else ('FAIL' if f else 'pass')",
        "        out.append('| %s | %s | %s | %s | %s |' % (tb, res, p, f, e))",
        "    out.append('')",
        "    bad = [r for r in all_results if r.get('status') in ('FAIL', 'ERROR')]",
        "    if bad:",
        "        out += ['## Failures', '']",
        "        for r in bad:",
        "            col = r.get('column') or '(table)'",
        "            sev = r.get('severity') or ('error' if r.get('status') == 'ERROR' else 'sh:Warning')",
        "            out.append('- **%s** on `%s` [%s/%s]: %s'",
        "                       % (r.get('rule_type'), col, r.get('status'), sev, r.get('detail')))",
        "        out.append('')",
        "    else:",
        "        out += ['All checks passed. \\u2705', '']",
        "    rp = Path(output_dir) / 'report.md'",
        "    with open(rp, 'w') as fh:",
        "        fh.write('\\n'.join(out))",
        "    return rp",
        "",
        "",
        "def main():",
        "    parser = argparse.ArgumentParser(description='Run DQ validations against a relational database')",
        "    parser.add_argument('--conn-string', required=True, help='Database connection string (postgresql://, mysql://, snowflake://, databricks://)')",
        "    parser.add_argument('--sample', type=int, default=None, help='Max rows per table')",
        "    parser.add_argument('--output', default='data_quality_results', help='Results output directory')",
        "    args = parser.parse_args()",
        "",
        "    platform = _get_platform(args.conn_string)",
        "    engine = sqlalchemy.create_engine(_get_sqlalchemy_url(args.conn_string))",
        "    all_results = []",
        "    run_at = datetime.now().isoformat()",
        "    print(f'DQ validation run: {run_at}\\n')",
        "",
        "    for schema, table, validate_fn in VALIDATORS:",
        "        tq = f'{schema}.{table}'",
        "        print(f'  {tq}...', end='', flush=True)",
        "        q = f'SELECT * FROM {_quote_table(schema, table, platform)}'",
        "        if args.sample:",
        "            q += f' LIMIT {args.sample}'",
        "        try:",
        "            df = pd.read_sql(q, engine)",
        "            # Snowflake returns UPPER physical column labels for objects made",
        "            # by unquoted DDL; the Pandera schema keys on the logical (lower)",
        "            # names, so normalize to lower — matching what Postgres' unquoted",
        "            # view columns return — else every column check errors 'column",
        "            # not found'.",
        "            if platform == 'snowflake':",
        "                df.columns = [str(c).lower() for c in df.columns]",
        "            results = validate_fn(df, engine)",
        "        except Exception as exc:",
        "            print(' ERROR')",
        "            results = [{'table': tq, 'status': 'ERROR', 'detail': str(exc)}]",
        "        all_results.extend(results)",
        "        fails = [r for r in results if r['status'] == 'FAIL']",
        "        errs  = [r for r in results if r['status'] == 'ERROR']",
        "        ok    = len(results) - len(fails) - len(errs)",
        "        print(f' {ok} pass  {len(fails)} fail  {len(errs)} error')",
        "",
        "    # Save timestamped result log",
        "    Path(args.output).mkdir(parents=True, exist_ok=True)",
        "    ts = datetime.now().strftime('%Y%m%dT%H%M%S')",
        "    out_file = Path(args.output) / f'dq_results_{ts}.json'",
        "    with open(out_file, 'w') as fh:",
        "        json.dump({'run_at': run_at, 'results': all_results}, fh, indent=2, default=str)",
        "    print(f'\\nResults → {out_file}')",
        "",
        "    # Exit non-zero if any sh:Violation checks failed",
        "    violations = [r for r in all_results if r.get('severity') == 'sh:Violation' and r['status'] == 'FAIL']",
        "    fails      = [r for r in all_results if r['status'] == 'FAIL']",
        "    passes     = [r for r in all_results if r['status'] == 'PASS']",
        "    print(f'Summary: {len(passes)} passed  {len(fails)} failed  ({len(violations)} violations)')",
        "",
        "    # DWB-independent verdict: run_result.json (v1 contract) + report.md.",
        "    payload, rr = _write_run_result(args.output, platform, run_at, all_results, out_file)",
        "    rp = _write_report_md(args.output, payload, all_results)",
        "    print(f'Run result \\u2192 {rr}')",
        "    print(f'Report     \\u2192 {rp}')",
        "    if violations:",
        "        print('RESULT: FAILED — violation-severity checks did not pass', file=sys.stderr)",
        "        sys.exit(1)",
        "    print('RESULT: PASSED')",
        "",
        "",
        "if __name__ == '__main__':",
        "    main()",
    ]

    return "\n".join(lines)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate Python/Pandera DQ test code from Neo4j graph rules."
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=os.path.join(os.getcwd(), "data_quality_tests"),
        help="Output directory (default: ./data_quality_tests)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument(
        "--project-code",
        default=None,
        help="Scope rules to a single project (reads :Project{projectCode} → :Catalog → :Dataset).",
    )
    # Dual-mode (mirrors data-mapping-neo4j's --source-mode). 'catalog' (default)
    # is byte-identical to the historic behaviour; 'dprod' reads contract rules on
    # :DProdColumn and targets the DEPLOYED product view — no rule generation.
    parser.add_argument(
        "--source-mode", choices=["catalog", "dprod"], default="catalog",
        help="Rule home + test target: 'catalog' (source :Column) or 'dprod' (product contract).",
    )
    parser.add_argument(
        "--target-contract", default=None,
        help="Contract id ({project_code}-contract) — required for --source-mode dprod.",
    )
    parser.add_argument(
        "--view-schema", default="public",
        help="Schema the deployed product views live in (dprod mode). Default: public.",
    )
    args = parser.parse_args()

    if args.source_mode == "dprod" and not args.target_contract:
        print("ERROR: --source-mode dprod requires --target-contract.", file=sys.stderr)
        sys.exit(1)

    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    print(f"Connecting to {bolt_uri} as '{args.username}' (database: {args.database})...")

    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        driver.verify_connectivity()
    except ServiceUnavailable as e:
        print(f"ERROR: Cannot connect to Neo4j: {e}", file=sys.stderr)
        sys.exit(1)
    except AuthError as e:
        print(f"ERROR: Authentication failed: {e}", file=sys.stderr)
        sys.exit(1)

    with driver.session(database=args.database) as session:
        rules_by_table: dict[str, list[dict]] = defaultdict(list)
        fks_by_table: dict[str, list[dict]] = defaultdict(list)
        if args.source_mode == "dprod":
            print(f"Fetching PRODUCT DQ rules for contract '{args.target_contract}' "
                  f"(dprod mode, views in schema '{args.view_schema}')...")
            for rec in session.run(RULES_QUERY_DPROD, contract_id=args.target_contract):
                rec = dict(rec)
                key = f"{args.view_schema}.vw_{_safe_name(rec['physical_name'])}"
                rules_by_table[key].append(rec)
            print("Fetching product FK constraints...")
            for rec in session.run(FK_QUERY_DPROD, contract_id=args.target_contract):
                rec = dict(rec)
                key = f"{args.view_schema}.vw_{_safe_name(rec['physical_name'])}"
                # Point RI at the deployed ref view so uri_to_parts() resolves it.
                rec["ref_uri"] = f"{args.view_schema}.vw_{_safe_name(rec['ref_physical_name'])}"
                fks_by_table[key].append(rec)
        elif args.project_code:
            print(f"Fetching DQ rules scoped to project '{args.project_code}'...")
            for rec in session.run(RULES_QUERY_SCOPED, project_code=args.project_code):
                rules_by_table[rec["dataset_uri"]].append(dict(rec))
            print("Fetching FK constraints...")
            for rec in session.run(FK_QUERY_SCOPED, project_code=args.project_code):
                fks_by_table[rec["dataset_uri"]].append(dict(rec))
        else:
            print("Fetching DQ rules (all projects)...")
            for rec in session.run(RULES_QUERY):
                rules_by_table[rec["dataset_uri"]].append(dict(rec))
            print("Fetching FK constraints...")
            for rec in session.run(FK_QUERY):
                fks_by_table[rec["dataset_uri"]].append(dict(rec))

    driver.close()

    total_rules = sum(len(v) for v in rules_by_table.values())
    print(f"  {len(rules_by_table)} table(s)  {total_rules} rule(s)")

    if not rules_by_table:
        print(
            "No rules found in graph. Run data-quality-rule-generation first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Create output directories
    output_dir = Path(args.output_dir)
    validators_dir = output_dir / "validators"
    results_dir = output_dir / "results"
    for d in (validators_dir, results_dir):
        d.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    table_pairs: list[tuple[str, str]] = []

    print(f"\nGenerating validators → {output_dir}/")
    for dataset_uri in sorted(rules_by_table):
        schema, table = uri_to_parts(dataset_uri)
        table_pairs.append((schema, table))
        rules = rules_by_table[dataset_uri]
        fk_rows = fks_by_table.get(dataset_uri, [])

        mod = module_name(schema, table)
        content = generate_validator_module(schema, table, rules, fk_rows, generated_at)
        out_path = validators_dir / f"{mod}.py"
        out_path.write_text(content)

        rule_counts = defaultdict(int)
        for r in rules:
            rule_counts[r["rule_type"]] += 1
        summary = "  ".join(f"{k}:{v}" for k, v in sorted(rule_counts.items()))
        print(f"  {schema}.{table:<25}  {len(rules)} rules  ({summary})")

    # validators/__init__.py
    (validators_dir / "__init__.py").write_text("")

    # run_all.py
    run_all_path = output_dir / "run_all.py"
    run_all_path.write_text(generate_run_all(table_pairs, generated_at))
    print(f"\n  Wrote {run_all_path}")

    # requirements.txt
    req_path = output_dir / "requirements.txt"
    req_path.write_text(
        "# Generated by data-quality-testing-python skill\n"
        "pandas\n"
        "pandera\n"
        "sqlalchemy\n"
        "# Platform drivers — install the one matching your database\n"
        "psycopg2-binary        # PostgreSQL\n"
        "pymysql>=1.0           # MySQL\n"
        "snowflake-sqlalchemy>=1.5  # Snowflake\n"
        "databricks-sql-connector>=3.0.0  # Databricks\n"
    )
    print(f"  Wrote {req_path}")

    print(f"\nDone. To run the test suite:")
    print(f"  pip install -r {output_dir}/requirements.txt")
    print(f"  python {output_dir}/run_all.py --conn-string <connection-string>")


if __name__ == "__main__":
    main()
