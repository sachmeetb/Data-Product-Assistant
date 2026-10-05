#!/usr/bin/env python3
"""
Load GX validation results into the Neo4j knowledge graph.

Reads the JSON output from run_gx_validations.py and emits:
  (:TestRun)                   — per-execution summary
  (:TestResult)                — per-expectation pass/fail
  (:Project)-[:HAS_TEST_RUN]-> (:TestRun)
  (:TestRun)-[:VALIDATED]->    (:Dataset)
  (:TestRun)-[:PRODUCED]->     (:TestResult)
  (:TestResult)-[:VALIDATES_RULE]-> (:PropertyShape)
  (:TestResult)-[:ON_COLUMN]->      (:Column)          # catalog mode
  (:TestResult)-[:ON_DPROD_COLUMN]->(:DProdColumn)     # dprod mode

Per-expectation meta carries propertyShapeUri, ruleType, and columnUri
(embedded by generate_gx_tests.py), so results link back to graph nodes. In
``--source-mode dprod`` the columnUri is a :DProdColumn URI (the rules live on
the contract, not the source catalog), so the column anchor is matched + linked
against :DProdColumn via :ON_DPROD_COLUMN.

Usage:
    python load_test_results_to_graph.py [options]

Options:
    --results PATH      Single JSON results file. If omitted, uses the
                        most recent *.json under dq_tests_gx/results/.
    --results-dir DIR   Directory to scan for the latest *.json
                        (default: ./dq_tests_gx/results)
    --project-code CODE Scope TestRun/TestResult URIs to a project.
    --framework NAME    Framework tag ('gx' or 'pandera', default: gx)
    --host              Neo4j host (default: localhost)
    --bolt-port         Bolt port (default: 7687)
    --username          Neo4j username (default: neo4j)
    --password          Neo4j password (default: your_password)
    --database          Neo4j database (default: neo4j)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError
except ImportError:
    print("ERROR: neo4j driver required. Install: pip install neo4j", file=sys.stderr)
    sys.exit(1)


DEFAULT_RESULTS_DIR = "dq_tests_gx/results"


_MERGE_TEST_RUN = """\
MERGE (tr:TestRun {uri: $tr_uri})
SET tr.batchId = $batch_id,
    tr.framework = $framework,
    tr.executedAt = datetime($executed_at),
    tr.totalExpectations = $total,
    tr.successful = $successful,
    tr.unsuccessful = $unsuccessful,
    tr.resultsPath = $results_path
RETURN tr.uri AS uri
"""

_LINK_PROJECT_TO_RUN = """\
MATCH (prj:Project {projectCode: $project_code})
MATCH (tr:TestRun {uri: $tr_uri})
MERGE (prj)-[:HAS_TEST_RUN]->(tr)
"""

_LINK_RUN_TO_DATASET = """\
MATCH (tr:TestRun {uri: $tr_uri})
MATCH (ds:Dataset {uri: $dataset_uri})
MERGE (tr)-[:VALIDATED]->(ds)
"""

# Column anchor is mode-dependent: catalog rules live on :Column (linked
# :ON_COLUMN), dprod rules on :DProdColumn (linked :ON_DPROD_COLUMN). The rule
# link (:VALIDATES_RULE → shared :PropertyShape) works in both modes. The label +
# rel are interpolated (fixed, non-user strings) via _create_test_result_query().
_CREATE_TEST_RESULT_TMPL = """\
MATCH (tr:TestRun {{uri: $tr_uri}})
OPTIONAL MATCH (ps:PropertyShape {{uri: $shape_uri}})
OPTIONAL MATCH (col:{col_label} {{uri: $column_uri}})
CREATE (res:TestResult {{
  uri: $res_uri,
  batchId: $batch_id,
  ruleType: $rule_type,
  expectationType: $expectation_type,
  evaluated: $evaluated,
  successful: $successful,
  unsuccessful: $unsuccessful,
  passRate: $pass_rate,
  passed: $passed
}})
MERGE (tr)-[:PRODUCED]->(res)
FOREACH (_ IN CASE WHEN ps IS NULL THEN [] ELSE [1] END |
  MERGE (res)-[:VALIDATES_RULE]->(ps)
)
FOREACH (_ IN CASE WHEN col IS NULL THEN [] ELSE [1] END |
  MERGE (res)-[:{col_rel}]->(col)
)
"""


def _create_test_result_query(source_mode: str) -> str:
    if source_mode == "dprod":
        return _CREATE_TEST_RESULT_TMPL.format(
            col_label="DProdColumn", col_rel="ON_DPROD_COLUMN")
    return _CREATE_TEST_RESULT_TMPL.format(col_label="Column", col_rel="ON_COLUMN")


def _pick_results_file(args) -> Path:
    if args.results:
        p = Path(args.results)
        if not p.exists():
            print(f"ERROR: Results file not found: {p}", file=sys.stderr)
            sys.exit(1)
        return p
    rdir = Path(args.results_dir)
    if not rdir.exists():
        print(f"ERROR: Results dir not found: {rdir}", file=sys.stderr)
        sys.exit(1)
    # The runner writes THREE artifacts here: gx_results_*.json (the per-table
    # detail this loader needs), run_result.json (the RunRecorder v1 contract —
    # a different shape with no `tables` key), and report.md. Prefer the
    # gx_results_* files; never pick run_result.json (globbing *.json and taking
    # the newest would grab it and yield "no table entries").
    files = sorted(rdir.glob("gx_results_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        files = [
            f for f in sorted(rdir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            if f.name != "run_result.json"
        ]
    if not files:
        print(f"ERROR: No JSON results in {rdir}", file=sys.stderr)
        sys.exit(1)
    return files[-1]


def _dataset_uri_for(schema: str, table: str, project_code: str | None) -> str:
    if project_code:
        return f"dataset:{project_code}:{schema}.{table}"
    return f"dataset:{schema}.{table}"


def _parse_table_entries(data) -> list[dict]:
    """Normalize results JSON to a list of per-table entries.

    Supports shapes emitted by run_gx_validations.py:
      - {"run_at": ..., "tables": [entries]}
      - [entries]
      - a single entry dict
    """
    if isinstance(data, dict):
        if isinstance(data.get("tables"), list):
            return data["tables"]
        if "statistics" in data or "results" in data:
            return [data]
    if isinstance(data, list):
        return data
    return []


def main() -> None:
    ap = argparse.ArgumentParser(description="Load GX results into Neo4j knowledge graph.")
    ap.add_argument("--results")
    ap.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR)
    ap.add_argument("--project-code")
    ap.add_argument("--framework", default="gx")
    ap.add_argument(
        "--source-mode", choices=["catalog", "dprod"], default="catalog",
        help="Column anchor: 'catalog' (:Column via :ON_COLUMN) or 'dprod' "
             "(:DProdColumn via :ON_DPROD_COLUMN). Matches the generator's mode.",
    )
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--bolt-port", type=int, default=7687)
    ap.add_argument("--username", default="neo4j")
    ap.add_argument("--password", default="your_password")
    ap.add_argument("--database", default="neo4j")
    args = ap.parse_args()

    results_file = _pick_results_file(args)
    data = json.loads(results_file.read_text())
    entries = _parse_table_entries(data)
    if not entries:
        print("No table entries in results file; nothing to load.", file=sys.stderr)
        sys.exit(1)

    run_at = data.get("run_at") if isinstance(data, dict) else None
    if not run_at:
        run_at = datetime.fromtimestamp(results_file.stat().st_mtime).isoformat()

    batch_id = run_at
    tr_suffix = batch_id.replace(":", "-").replace(".", "-")
    prj = args.project_code
    tr_uri = f"testrun:{prj}:{tr_suffix}" if prj else f"testrun:{tr_suffix}"

    # Roll up totals across tables
    total = successful = unsuccessful = 0
    for e in entries:
        stats = e.get("statistics") or {}
        total += int(stats.get("evaluated", 0) or 0)
        successful += int(stats.get("successful", 0) or 0)
        unsuccessful += int(stats.get("unsuccessful", 0) or 0)

    print(f"Loading results for batch {batch_id} — {total} expectations, "
          f"{successful} passed, {unsuccessful} failed.")

    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        driver.verify_connectivity()
    except (ServiceUnavailable, AuthError) as e:
        print(f"ERROR: Cannot connect to Neo4j: {e}", file=sys.stderr)
        sys.exit(1)

    with driver.session(database=args.database) as session:
        session.run(
            _MERGE_TEST_RUN,
            tr_uri=tr_uri, batch_id=batch_id, framework=args.framework,
            executed_at=run_at, total=total, successful=successful,
            unsuccessful=unsuccessful,
            results_path=str(results_file),
        )
        if prj:
            session.run(_LINK_PROJECT_TO_RUN, project_code=prj, tr_uri=tr_uri)

        # Link run to datasets; create per-expectation TestResult nodes
        for entry in entries:
            table_id = entry.get("table") or ""  # e.g. "public.orders"
            if "." in table_id:
                schema, table = table_id.split(".", 1)
            else:
                schema, table = "", table_id
            if schema and table:
                ds_uri = _dataset_uri_for(schema, table, prj)
                session.run(_LINK_RUN_TO_DATASET, tr_uri=tr_uri, dataset_uri=ds_uri)

            for i, r in enumerate(entry.get("results", [])):
                meta = r.get("meta") or {}
                shape_uri = meta.get("propertyShapeUri") or ""
                column_uri = meta.get("columnUri") or ""
                rule_type = meta.get("ruleType") or ""
                expectation_type = r.get("expectation_type") or ""
                result_dict = r.get("result") or {}
                element_count = int(result_dict.get("element_count", 0) or 0)
                unexpected = int(result_dict.get("unexpected_count", 0) or 0)
                evaluated = element_count if element_count else 1
                succ = evaluated - unexpected if unexpected <= evaluated else 0
                pass_rate = (succ / evaluated) if evaluated else 0.0
                # Build a stable unique URI for this result
                res_suffix = shape_uri or f"{table_id}:{expectation_type}:{i}"
                res_uri = f"testresult:{prj or 'none'}:{batch_id}:{res_suffix}"

                session.run(
                    _create_test_result_query(args.source_mode),
                    tr_uri=tr_uri,
                    shape_uri=shape_uri,
                    column_uri=column_uri,
                    res_uri=res_uri,
                    batch_id=batch_id,
                    rule_type=rule_type,
                    expectation_type=expectation_type,
                    evaluated=evaluated,
                    successful=succ,
                    unsuccessful=unexpected,
                    pass_rate=pass_rate,
                    passed=bool(r.get("success")),
                )

    driver.close()
    print(f"Loaded :TestRun {tr_uri} with {sum(len(e.get('results', [])) for e in entries)} results.")


if __name__ == "__main__":
    main()
