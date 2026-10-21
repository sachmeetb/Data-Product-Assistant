#!/usr/bin/env python3
"""
Generate Great Expectations (GX Core) validation code from Neo4j graph rules.

Reads :PropertyShape nodes from the knowledge graph and produces:
  output_dir/run_gx_validations.py  — self-contained GX validation script
  output_dir/requirements.txt

The generated script uses an ephemeral GX Data Context (no project files needed)
with Pandas DataFrames loaded from PostgreSQL via SQLAlchemy.

Usage:
    python generate_gx_tests.py [output_dir] [options]

    output_dir   destination directory (default: ./dq_tests_gx)

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

RULES_QUERY = """
MATCH (ds:Dataset)-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
// Only approved rules generate tests. Rejected and pending_review rules are
// invisible to downstream consumers (marketplace, scoring) by design — keep
// the test surface aligned with them. coalesce defaults legacy nodes that
// pre-date the status field to 'approved'.
WHERE coalesce(ps.status, 'approved') = 'approved'
OPTIONAL MATCH (ps)-[:ON_COLUMN]->(col:Column)
OPTIONAL MATCH (ps)-[:ALLOWED_VALUE]->(tv:TopValue)
WITH ds, ps, col, collect(tv.value) AS allowed_values
RETURN
  ds.uri                  AS dataset_uri,
  ps.uri                  AS shape_uri,
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
  ps.coverageThreshold    AS coverage_threshold,
  col.uri                 AS col_uri,
  col.dataType            AS col_type,
  allowed_values
ORDER BY ds.uri, ps.ruleType, ps.path
"""

# Project-scoped variant — only returns rules for datasets under the given
# :Project node's catalogs. Keeps tests from leaking across projects that
# share a single Neo4j instance.
RULES_QUERY_SCOPED = """
MATCH (:Project {projectCode: $project_code})-[:HAS_CATALOG]->(:Catalog)
      -[:DCAT_DATASET]->(ds:Dataset)
MATCH (ds)-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
WHERE coalesce(ps.status, 'approved') = 'approved'
OPTIONAL MATCH (ps)-[:ON_COLUMN]->(col:Column)
OPTIONAL MATCH (ps)-[:ALLOWED_VALUE]->(tv:TopValue)
WITH ds, ps, col, collect(tv.value) AS allowed_values
RETURN
  ds.uri                  AS dataset_uri,
  ps.uri                  AS shape_uri,
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
  ps.coverageThreshold    AS coverage_threshold,
  col.uri                 AS col_uri,
  col.dataType            AS col_type,
  allowed_values
ORDER BY ds.uri, ps.ruleType, ps.path
"""


# Product (dprod) variant — rules authored ON THE CONTRACT (spec/domain/user on
# :DProdColumn), keyed to the deployed product view. Traverses the product graph
# instead of the catalog and returns the SAME column aliases as RULES_QUERY so
# every downstream code-emission function consumes it unchanged. Edges mirror the
# proven marketplace quality read (routers/marketplace.py) + the writer
# (routers/odcs.py PERSIST_SPEC_RULE). Column name is pc.name (dprod columns carry
# no ps.path); the dataset key is built by the caller as
# "<view_schema>.vw_<safe_name(physicalName)>" to match generate_view_ddl.py's
# deployed view name exactly. NO dq_rule_generation for this mode — the rules are
# already on the contract.
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
  ps.uri                  AS shape_uri,
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
  ps.coverageThreshold    AS coverage_threshold,
  pc.uri                  AS col_uri,
  pc.dataType             AS col_type,
  allowed_values
ORDER BY physical_name, ps.ruleType, pc.name
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
    ``{view_schema}.vw_{safe}`` where ``view_schema`` may itself be multi-level
    (e.g. Databricks ``catalog.schema`` → ``catalog.schema.vw_x``).

    ``table`` is the trailing (leaf) segment; ``namespace`` is everything before
    it — which may be one or more dot-joined parts. Splitting on the LAST dot
    (rsplit) keeps the 2-level case identical while generalizing to any depth;
    ``_quote_table`` re-splits the namespace so each level is quoted.
    """
    # Trailing piece after any URI colons holds "{namespace}.{table}"; earlier
    # colons are the URI namespace ("dataset") and optional project code.
    qualified = uri.split(":")[-1]
    namespace, table = qualified.rsplit(".", 1)
    return namespace, table


def suite_name(schema: str, table: str) -> str:
    # Must be a valid Python identifier — it's used verbatim as a `def
    # setup_suite_<sname>(...)` function name. A multi-level namespace
    # (Databricks catalog.schema) carries dots, so sanitize the whole thing.
    return _safe_name(f"{schema}__{table}")


def py_repr(v) -> str:
    if v is None:
        return "None"
    if isinstance(v, (int, float)):
        return repr(v)
    return repr(str(v))


def _coerce_bool_set(raw_vals) -> list:
    # Profiler serializes booleans as lowercase strings in :TopValue.value,
    # but pandas reads PG boolean columns as native True/False. GX's
    # ExpectColumnValuesToBeInSet uses Python `in` — str vs bool never
    # matches, so every row fails. Accept the usual token variants.
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


# ── expectation code generation ───────────────────────────────────────────────

def rules_to_expectation_lines(rules: list[dict]) -> tuple[list[str], int]:
    """
    Convert a table's rule list into lines of Python code that call
    suite.add_expectation(...) and return (lines, expectation_count).
    RI rules become comments — GX has no native equivalent.

    Each expectation embeds a ``meta`` dict carrying ``propertyShapeUri``,
    ``ruleType``, and ``columnUri`` so the result loader can map back to
    graph nodes.
    """
    lines: list[str] = []
    count = 0

    # Group by column so each column's rules appear together
    col_rules: dict[str, list[dict]] = defaultdict(list)
    for r in rules:
        key = r.get("column_name") or "__table__"
        col_rules[key].append(r)

    for col_name in sorted(col_rules):
        for r in col_rules[col_name]:
            rt = r["rule_type"]
            col_repr = repr(col_name)
            meta_repr = repr({
                "propertyShapeUri": r.get("shape_uri") or "",
                "ruleType": rt,
                "columnUri": r.get("col_uri") or "",
            })

            if rt == "mandatory":
                lines.append(
                    f"    suite.add_expectation("
                    f"gx.expectations.ExpectColumnValuesToNotBeNull("
                    f"column={col_repr}, meta={meta_repr}))"
                )
                count += 1

            elif rt == "range":
                min_v = r["min_inclusive"] if r["min_inclusive"] is not None else r["min_date"]
                max_v = r["max_inclusive"] if r["max_inclusive"] is not None else r["max_date"]
                # GX's ExpectColumnValuesToBeBetween rejects a rule with BOTH
                # bounds None ("min_value and max_value cannot both be None") and
                # that error aborts suite construction for the whole run. A
                # bound-less range rule is meaningless — skip it with a note.
                if min_v is None and max_v is None:
                    lines.append(
                        f"    # NOTE: range rule on {col_name} skipped — no min/max bound recorded"
                    )
                else:
                    kwargs = f"column={col_repr}"
                    if min_v is not None:
                        kwargs += f", min_value={py_repr(min_v)}"
                    if max_v is not None:
                        kwargs += f", max_value={py_repr(max_v)}"
                    kwargs += f", meta={meta_repr}"
                    lines.append(
                        f"    suite.add_expectation("
                        f"gx.expectations.ExpectColumnValuesToBeBetween({kwargs}))"
                    )
                    count += 1

            elif rt == "unique":
                threshold = r.get("uniqueness_threshold") or 0.99
                lines.append(
                    f"    suite.add_expectation("
                    f"gx.expectations.ExpectColumnProportionOfUniqueValuesToBeBetween("
                    f"column={col_repr}, min_value={threshold}, max_value=1.0, "
                    f"meta={meta_repr}))"
                )
                count += 1

            elif rt == "allowedValues":
                raw_vals = [v for v in (r.get("allowed_values") or []) if v is not None]
                if r.get("col_type") == "boolean":
                    vals = _coerce_bool_set(raw_vals)
                else:
                    vals = sorted(str(v) for v in raw_vals)
                if vals:
                    lines.append(
                        f"    suite.add_expectation("
                        f"gx.expectations.ExpectColumnValuesToBeInSet("
                        f"column={col_repr}, value_set={repr(vals)}, "
                        f"meta={meta_repr}))"
                    )
                    count += 1

            elif rt == "referentialIntegrity":
                desc = r.get("description") or f"{col_name} referential integrity"
                lines.append(
                    f"    # NOTE: RI rule skipped — no native GX equivalent. Rule: {desc}"
                )

    return lines, count


# ── script generation ─────────────────────────────────────────────────────────

def generate_run_gx(rules_by_table: dict[str, list[dict]], generated_at: str) -> str:
    """Return source code for the self-contained run_gx_validations.py script."""

    # Gather per-table expectation lines and counts
    table_meta: list[tuple[str, str, str, list[str], int]] = []
    for dataset_uri in sorted(rules_by_table):
        schema, table = uri_to_parts(dataset_uri)
        sname = suite_name(schema, table)
        exp_lines, exp_count = rules_to_expectation_lines(rules_by_table[dataset_uri])
        table_meta.append((schema, table, sname, exp_lines, exp_count))

    lines: list[str] = [
        "#!/usr/bin/env python3",
        '"""',
        "Great Expectations (GX Core) data quality validations.",
        f"Generated from Neo4j graph on {generated_at}.",
        "",
        "Requires: great-expectations>=1.0, pandas, sqlalchemy",
        "          + platform driver: psycopg2-binary (postgres), pymysql (mysql),",
        "          + snowflake-sqlalchemy (snowflake), databricks-sql-connector (databricks)",
        "",
        "Usage:",
        "    python run_gx_validations.py --conn-string <connection-string> [options]",
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
        "import great_expectations as gx",
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
        "        # databricks-sqlalchemy 2.x registers the plain `databricks` dialect;",
        "        # 1.x used `databricks+connector`. Use whichever is installed so the",
        "        # URL scheme matches the resolved driver.",
        "        from sqlalchemy.dialects import registry",
        "        try:",
        "            registry.load('databricks')",
        "            return cs",
        "        except Exception:",
        "            return 'databricks+connector://' + cs[len('databricks://'):]",
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
        "    `catalog`.`schema`.`table`, not `catalog.schema`.`table`. The 2-level",
        '    case is unchanged.',
        '    """',
        "    parts = [p for p in schema.split('.') if p] + [table]",
        "    return '.'.join(_quote_identifier(p, platform) for p in parts)",
        "",
        "",
        "# ── Expectation suite setup — one function per table ─────────────────────────",
        "",
    ]

    # Generate one setup function per table
    for schema, table, sname, exp_lines, _ in table_meta:
        fn_name = f"setup_suite_{sname}"
        lines += [
            f"def {fn_name}(context) -> gx.ExpectationSuite:",
            f'    """Build expectation suite for {schema}.{table}."""',
            f"    suite = context.suites.add(gx.ExpectationSuite(name={repr(sname)}))",
        ]
        lines.extend(exp_lines)
        lines += [
            "    return suite",
            "",
        ]

    # TABLES registry
    lines += [
        "TABLES = [",
    ]
    for schema, table, sname, _, exp_count in table_meta:
        fn_name = f"setup_suite_{sname}"
        lines.append(
            f"    ({repr(schema)}, {repr(table)}, {repr(sname)}, {fn_name}),  # {exp_count} expectation(s)"
        )
    lines += [
        "]",
        "",
        "",
        "# ── Validation runner ────────────────────────────────────────────────────────",
        "",
        "# Cap the number of sample failing values captured per failed expectation.",
        "# Small enough to keep result JSON compact and safe for PII-sensitive graphs;",
        "# the downstream failure-analysis stage is what the user actually reads.",
        "SAMPLE_CAP = 10",
        "",
        "def _extract_samples(r, cap=SAMPLE_CAP):",
        '    """Return the top-N unexpected {value, count} pairs for a GX expectation.',
        "",
        "    GX populates ``partial_unexpected_counts`` on column-value expectations",
        "    (list of ``{value, count}``). Row/table-level expectations (row count,",
        "    etc.) have no unexpected list — for those we return an empty list and",
        "    callers can fall back to ``observed_value``.",
        '    """',
        "    res = r.result or {}",
        "    pu_counts = res.get('partial_unexpected_counts') or []",
        "    if pu_counts:",
        "        out = []",
        "        for entry in pu_counts[:cap]:",
        "            if isinstance(entry, dict):",
        "                out.append({'value': entry.get('value'), 'count': entry.get('count', 1)})",
        "            else:",
        "                out.append({'value': entry, 'count': 1})",
        "        return out",
        "    pu_list = res.get('partial_unexpected_list') or res.get('unexpected_list') or []",
        "    if pu_list:",
        "        counts = {}",
        "        for v in pu_list:",
        "            key = v if isinstance(v, (str, int, float, bool)) or v is None else str(v)",
        "            counts[key] = counts.get(key, 0) + 1",
        "        pairs = sorted(counts.items(), key=lambda x: -x[1])[:cap]",
        "        return [{'value': k if k is not None else None, 'count': c} for k, c in pairs]",
        "    return []",
        "",
        "def run_table(context, datasource, schema, table, sname, df) -> dict:",
        '    """Validate one table\'s DataFrame. Returns a result dict."""',
        "    asset = datasource.add_dataframe_asset(sname)  # sname is identifier-safe",
        "    batch_def = asset.add_batch_definition_whole_dataframe('batch')",
        "    batch = batch_def.get_batch(batch_parameters={'dataframe': df})",
        "    suite = context.suites.get(sname)",
        "    result = batch.validate(suite)",
        "    return {",
        "        'table': f'{schema}.{table}',",
        "        'suite': sname,",
        "        'success': result.success,",
        "        'statistics': {",
        "            'evaluated': result.statistics.get('evaluated_expectations', 0),",
        "            'successful': result.statistics.get('successful_expectations', 0),",
        "            'unsuccessful': result.statistics.get('unsuccessful_expectations', 0),",
        "        },",
        "        'results': [",
        "            {",
        "                'expectation_type': r.expectation_config.type,",
        "                'column': r.expectation_config.kwargs.get('column'),",
        "                'success': r.success,",
        "                'result': r.result,",
        "                'meta': getattr(r.expectation_config, 'meta', None) or {},",
        "                'samples': [] if r.success else _extract_samples(r),",
        "            }",
        "            for r in result.results",
        "        ],",
        "    }",
        "",
        "",
        "# ── Standardized, DWB-independent reporting ──────────────────────────────────",
        "# When this package is run OUTSIDE Data Workbench there is no Neo4j to load",
        "# results into, so the runner writes its own verdict: a machine-readable",
        "# run_result.json (the same v1 contract the serving packages emit) plus a",
        "# human-readable report.md — both alongside the framework-native results JSON.",
        "",
        "def _write_run_result(output_dir, platform, run_at, all_table_results, results_file):",
        "    total_eval = sum(t.get('statistics', {}).get('evaluated', 0) for t in all_table_results)",
        "    total_ok   = sum(t.get('statistics', {}).get('successful', 0) for t in all_table_results)",
        "    total_fail = sum(t.get('statistics', {}).get('unsuccessful', 0) for t in all_table_results)",
        "    tables_failed = [t['table'] for t in all_table_results if not t.get('success', False)]",
        "    errored = [t['table'] for t in all_table_results if t.get('error')]",
        "    steps = [",
        "        {'name': t['table'],",
        "         'status': ('success' if t.get('success') else 'failed'),",
        "         'detail': (t['error'] if t.get('error') else",
        "                    '%s pass / %s fail' % (t.get('statistics', {}).get('successful', 0),",
        "                                           t.get('statistics', {}).get('unsuccessful', 0)))}",
        "        for t in all_table_results",
        "    ]",
        "    if errored:",
        "        status, error = 'failed', {'class': 'execution_error',",
        "                                   'message': 'tables errored: ' + ', '.join(errored)}",
        "    elif total_fail:",
        "        status, error = 'failed', {'class': 'validation_failures',",
        "                                   'message': '%d expectation(s) failed across %d table(s)'",
        "                                              % (total_fail, len(tables_failed))}",
        "    else:",
        "        status, error = 'success', None",
        "    try:",
        "        finished = datetime.now()",
        "        duration_ms = int((finished - datetime.fromisoformat(run_at)).total_seconds() * 1000)",
        "    except Exception:",
        "        finished, duration_ms = datetime.now(), None",
        "    payload = {",
        "        'contract_version': '1', 'operation': 'dq_validation', 'mode': 'gx',",
        "        'status': status, 'started_at': run_at, 'finished_at': finished.isoformat(),",
        "        'duration_ms': duration_ms, 'target': {'platform': platform},",
        "        'metrics': {",
        "            'framework': 'gx', 'tables': len(all_table_results),",
        "            'expectations_evaluated': total_eval, 'expectations_passed': total_ok,",
        "            'expectations_failed': total_fail, 'tables_failed': tables_failed,",
        "            'results_file': str(results_file),",
        "        },",
        "        'steps': steps, 'error': error, 'log_file': None,",
        "    }",
        "    rr = Path(output_dir) / 'run_result.json'",
        "    with open(rr, 'w') as fh:",
        "        json.dump(payload, fh, indent=2, default=str)",
        "    return payload, rr",
        "",
        "",
        "def _write_report_md(output_dir, payload, all_table_results):",
        "    m = payload['metrics']",
        "    verdict = 'PASSED' if payload['status'] == 'success' else 'FAILED'",
        "    out = ['# DQ Validation Report — Great Expectations', '',",
        "           '- **Result:** ' + verdict,",
        "           '- **Run at:** ' + str(payload['started_at']),",
        "           '- **Platform:** ' + str(payload['target'].get('platform', '?')),",
        "           '- **Tables:** %s  ·  **Expectations:** %s passed / %s failed'",
        "           % (m['tables'], m['expectations_passed'], m['expectations_failed']),",
        "           '', '| Table | Result | Passed | Failed |', '|---|---|---|---|']",
        "    for t in all_table_results:",
        "        st = t.get('statistics', {})",
        "        res = 'error' if t.get('error') else ('pass' if t.get('success') else 'FAIL')",
        "        out.append('| %s | %s | %s | %s |' % (t['table'], res,",
        "                   st.get('successful', '-'), st.get('unsuccessful', '-')))",
        "    out.append('')",
        "    any_fail = False",
        "    for t in all_table_results:",
        "        fails = [r for r in t.get('results', []) if not r.get('success')]",
        "        if t.get('error') or fails:",
        "            if not any_fail:",
        "                out += ['## Failures', '']",
        "                any_fail = True",
        "            out += ['### ' + t['table'], '']",
        "            if t.get('error'):",
        "                out += ['- ERROR: ' + str(t['error']), '']",
        "                continue",
        "            for r in fails:",
        "                col = r.get('column') or '(table)'",
        "                out.append('- **%s** on `%s`' % (r.get('expectation_type'), col))",
        "                samples = r.get('samples') or []",
        "                if samples:",
        "                    vals = ', '.join('%s (x%s)' % (s.get('value'), s.get('count')) for s in samples[:5])",
        "                    out.append('  - unexpected: ' + vals)",
        "            out.append('')",
        "    if not any_fail:",
        "        out += ['All expectations passed. \\u2705', '']",
        "    rp = Path(output_dir) / 'report.md'",
        "    with open(rp, 'w') as fh:",
        "        fh.write('\\n'.join(out))",
        "    return rp",
        "",
        "",
        "def main():",
        "    parser = argparse.ArgumentParser(description='Run GX Core DQ validations against a relational database')",
        "    parser.add_argument('--conn-string', required=True, help='Database connection string (postgresql://, mysql://, snowflake://, databricks://)')",
        "    parser.add_argument('--sample', type=int, default=None, help='Max rows per table')",
        "    parser.add_argument('--output', default='results', help='Results output directory')",
        "    args = parser.parse_args()",
        "",
        "    platform = _get_platform(args.conn_string)",
        "    engine = sqlalchemy.create_engine(_get_sqlalchemy_url(args.conn_string))",
        "    run_at = datetime.now().isoformat()",
        "    print(f'GX validation run: {run_at}\\n')",
        "",
        "    # Create ephemeral GX context and build all suites",
        "    context = gx.get_context(mode='ephemeral')",
        "    datasource = context.data_sources.add_pandas('pg_data')",
        "    for schema, table, sname, setup_fn in TABLES:",
        "        setup_fn(context)",
        "",
        "    all_table_results = []",
        "    all_failed = []",
        "",
        "    for schema, table, sname, _ in TABLES:",
        "        tq = f'{schema}.{table}'",
        "        print(f'  {tq}...', end='', flush=True)",
        "        q = f'SELECT * FROM {_quote_table(schema, table, platform)}'",
        "        if args.sample:",
        "            q += f' LIMIT {args.sample}'",
        "        try:",
        "            df = pd.read_sql(q, engine)",
        "            # Snowflake returns UPPER physical column labels for objects made",
        "            # by unquoted DDL; the expectation suite keys on the logical",
        "            # (lower) names, so normalize to lower — matching what Postgres'",
        "            # unquoted view columns return — else every column expectation",
        "            # errors 'column not found'.",
        "            if platform == 'snowflake':",
        "                df.columns = [str(c).lower() for c in df.columns]",
        "            table_result = run_table(context, datasource, schema, table, sname, df)",
        "        except Exception as exc:",
        "            print(f' ERROR: {exc}')",
        "            table_result = {'table': tq, 'suite': sname, 'success': False, 'error': str(exc)}",
        "        all_table_results.append(table_result)",
        "        stats = table_result.get('statistics', {})",
        "        ok   = stats.get('successful', '?')",
        "        fail = stats.get('unsuccessful', '?')",
        "        print(f' {ok} pass  {fail} fail')",
        "        failed_exps = [r for r in table_result.get('results', []) if not r['success']]",
        "        for r in failed_exps:",
        "            col = r.get('column') or ''",
        "            print(f'    FAIL  {r[\"expectation_type\"]}  column={col}')",
        "            obs = r.get('result', {}).get('observed_value')",
        "            if obs is not None:",
        "                print(f'          observed: {obs}')",
        "        all_failed.extend(failed_exps)",
        "",
        "    # Save result log",
        "    Path(args.output).mkdir(parents=True, exist_ok=True)",
        "    ts = datetime.now().strftime('%Y%m%dT%H%M%S')",
        "    out_file = Path(args.output) / f'gx_results_{ts}.json'",
        "    with open(out_file, 'w') as fh:",
        "        json.dump({'run_at': run_at, 'tables': all_table_results}, fh, indent=2, default=str)",
        "    print(f'\\nResults → {out_file}')",
        "",
        "    total_ok   = sum(t.get('statistics', {}).get('successful', 0) for t in all_table_results)",
        "    total_fail = sum(t.get('statistics', {}).get('unsuccessful', 0) for t in all_table_results)",
        "    print(f'Summary: {total_ok} passed  {total_fail} failed')",
        "",
        "    # DWB-independent verdict: run_result.json (v1 contract) + report.md.",
        "    payload, rr = _write_run_result(args.output, platform, run_at, all_table_results, out_file)",
        "    rp = _write_report_md(args.output, payload, all_table_results)",
        "    print(f'Run result \\u2192 {rr}')",
        "    print(f'Report     \\u2192 {rp}')",
        "    if all_failed:",
        "        print('RESULT: FAILED — one or more expectations did not pass', file=sys.stderr)",
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
        description="Generate GX Core validation code from Neo4j graph rules."
    )
    parser.add_argument(
        "output_dir",
        nargs="?",
        default=os.path.join(os.getcwd(), "dq_tests_gx"),
        help="Output directory (default: ./dq_tests_gx)",
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
    # is byte-identical to the historic behaviour: observation rules on :Column,
    # tested against the source DB. 'dprod' reads contract rules on :DProdColumn
    # and targets the DEPLOYED product view (vw_<safe>) — no rule generation.
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
        if args.source_mode == "dprod":
            print(f"Fetching PRODUCT DQ rules for contract '{args.target_contract}' "
                  f"(dprod mode, views in schema '{args.view_schema}')...")
            result = session.run(RULES_QUERY_DPROD, contract_id=args.target_contract)
            for rec in result:
                rec = dict(rec)
                # Key each rule by the DEPLOYED view relation ("schema.vw_<safe>")
                # so uri_to_parts() splits it into (schema, table) verbatim.
                key = f"{args.view_schema}.vw_{_safe_name(rec['physical_name'])}"
                rules_by_table[key].append(rec)
        elif args.project_code:
            print(f"Fetching DQ rules scoped to project '{args.project_code}'...")
            result = session.run(RULES_QUERY_SCOPED, project_code=args.project_code)
            for rec in result:
                rules_by_table[rec["dataset_uri"]].append(dict(rec))
        else:
            print("Fetching DQ rules (all projects)...")
            result = session.run(RULES_QUERY)
            for rec in result:
                rules_by_table[rec["dataset_uri"]].append(dict(rec))

    driver.close()

    total_rules = sum(len(v) for v in rules_by_table.values())
    print(f"  {len(rules_by_table)} table(s)  {total_rules} rule(s)")

    if not rules_by_table:
        print(
            "No rules found in graph. Run data-quality-rule-generation first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Report expectation counts per table
    print()
    total_expectations = 0
    for dataset_uri in sorted(rules_by_table):
        schema, table = uri_to_parts(dataset_uri)
        _, exp_count = rules_to_expectation_lines(rules_by_table[dataset_uri])
        ri_count = sum(1 for r in rules_by_table[dataset_uri] if r["rule_type"] == "referentialIntegrity")
        total_expectations += exp_count
        ri_note = f"  ({ri_count} RI rule(s) skipped — no GX native equivalent)" if ri_count else ""
        print(f"  {schema}.{table:<25}  {exp_count} expectation(s){ri_note}")
    print(f"\n  Total expectations: {total_expectations}")

    # Create output directory
    output_dir = Path(args.output_dir)
    results_dir = output_dir / "results"
    for d in (output_dir, results_dir):
        d.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Write the validation script
    script_path = output_dir / "run_gx_validations.py"
    script_path.write_text(generate_run_gx(rules_by_table, generated_at))
    print(f"\n  Wrote {script_path}")

    # requirements.txt
    req_path = output_dir / "requirements.txt"
    req_path.write_text(
        "# Generated by data-quality-testing-gx skill\n"
        "great-expectations>=1.0,<2.0\n"
        "pandas\n"
        "sqlalchemy\n"
        "# Platform drivers — install the one matching your database\n"
        "psycopg2-binary        # PostgreSQL\n"
        "pymysql>=1.0           # MySQL\n"
        "snowflake-sqlalchemy>=1.5  # Snowflake\n"
        "databricks-sql-connector>=3.0.0  # Databricks\n"
    )
    print(f"  Wrote {req_path}")

    print(f"\nDone. To run the validations:")
    print(f"  pip install -r {output_dir}/requirements.txt")
    print(f"  python {output_dir}/run_gx_validations.py --conn-string <connection-string>")


if __name__ == "__main__":
    main()
