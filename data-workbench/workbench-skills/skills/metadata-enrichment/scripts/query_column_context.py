#!/usr/bin/env python3
"""
Query Neo4j for columns that lack :ColumnDescription nodes, gathering rich context.

Modes:
  (default)              List tables that have at least one undescribed column.
  --tables <uri,...>     Return full context for undescribed columns in specified tables as JSON.

Output (default list mode):
  Numbered plain-text list: table name + undescribed column count + dataset URI.

Output (--tables mode):
  JSON array written to --output file (default: metadata/column_context_<timestamp>.json).
  Each element contains column metadata, table context, DQ rules, profiling metrics,
  top values, and FK relationships — everything needed to generate a good description.

Usage:
    python query_column_context.py [options]

Options:
    --tables      Comma-separated dataset URIs to fetch context for
                  (e.g. dataset:employees.employee,dataset:employees.department)
    --output      Output JSON file path (default: metadata/column_context_<timestamp>.json)
    --host        Neo4j host (default: localhost)
    --bolt-port   Bolt port (default: 7687)
    --username    Neo4j username (default: neo4j)
    --password    Neo4j password (default: your_password)
    --database    Neo4j database name (default: neo4j)
"""

import sys
import os
import argparse
import json
from datetime import datetime, timezone

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError
except ImportError:
    print("ERROR: neo4j driver is required. Install with: pip install neo4j", file=sys.stderr)
    sys.exit(1)

DEFAULT_HOST = "localhost"
DEFAULT_BOLT_PORT = 7687
DEFAULT_USERNAME = "neo4j"
DEFAULT_PASSWORD = "your_password"
DEFAULT_DATABASE = "neo4j"


# ── Graph queries ─────────────────────────────────────────────────────────────

def list_tables_needing_descriptions(session, project_code: str | None = None) -> list[dict]:
    """Find tables with at least one column lacking a :ColumnDescription node.

    When project_code is provided, scopes to datasets linked to that project's
    :Project node via [:HAS_CATALOG]->[:DCAT_DATASET].
    """
    if project_code:
        result = session.run("""
            MATCH (:Project {projectCode: $project_code})
                  -[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset)
                  -[:HAS_COLUMN]->(col:Column)
            WHERE NOT (col)-[:HAS_DESCRIPTION]->(:ColumnDescription)
            RETURN ds.schema        AS schema,
                   ds.name          AS name,
                   ds.uri           AS dataset_uri,
                   count(col)       AS undescribed_count
            ORDER BY ds.schema, ds.name
        """, project_code=project_code)
    else:
        result = session.run("""
            MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
            WHERE NOT (col)-[:HAS_DESCRIPTION]->(:ColumnDescription)
            RETURN ds.schema        AS schema,
                   ds.name          AS name,
                   ds.uri           AS dataset_uri,
                   count(col)       AS undescribed_count
            ORDER BY ds.schema, ds.name
        """)
    return [dict(r) for r in result]


def get_all_columns_for_tables(session, table_uris: list[str]) -> dict[str, list[dict]]:
    """Return all columns (including described ones) per table for context."""
    result = session.run("""
        MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
        WHERE ds.uri IN $uris
        RETURN ds.uri AS dataset_uri,
               collect({
                   name:       col.name,
                   dataType:   col.dataType,
                   primaryKey: col.primaryKey,
                   nullable:   col.nullable,
                   ordinal:    col.ordinal
               }) AS all_columns
    """, uris=table_uris)
    return {
        r["dataset_uri"]: sorted(r["all_columns"], key=lambda c: c.get("ordinal") or 0)
        for r in result
    }


def get_fk_context(session, table_uris: list[str]) -> dict[str, list[dict]]:
    """Return outbound FK relationships per table."""
    result = session.run("""
        MATCH (ds:Dataset)
        WHERE ds.uri IN $uris
        OPTIONAL MATCH (ds)-[r:REFERENCES]->(ref:Dataset)
        WITH ds, collect({
            columns:           r.columns,
            referencedColumns: r.referencedColumns,
            referencedSchema:  ref.schema,
            referencedTable:   ref.name
        }) AS foreign_keys
        RETURN ds.uri AS dataset_uri, foreign_keys
    """, uris=table_uris)
    return {
        r["dataset_uri"]: [fk for fk in r["foreign_keys"] if fk.get("columns")]
        for r in result
    }


def get_rules_per_column(session, table_uris: list[str]) -> dict[str, list[dict]]:
    """Return DQ rules (PropertyShape) grouped by column URI."""
    result = session.run("""
        MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
        WHERE ds.uri IN $uris
        OPTIONAL MATCH (ps:PropertyShape)-[:ON_COLUMN]->(col)
        WITH col, collect({
            ruleType:    ps.ruleType,
            description: ps.description,
            severity:    ps.severity,
            confidence:  ps.confidence
        }) AS rules
        RETURN col.uri AS column_uri,
               [r IN rules WHERE r.ruleType IS NOT NULL] AS rules
    """, uris=table_uris)
    return {r["column_uri"]: r["rules"] for r in result}


def get_undescribed_columns_with_profiling(session, table_uris: list[str]) -> list[dict]:
    """Fetch undescribed columns with profiling measurements and top values."""
    result = session.run("""
        MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
        WHERE ds.uri IN $uris
          AND NOT (col)-[:HAS_DESCRIPTION]->(:ColumnDescription)
        OPTIONAL MATCH (col)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)-[:ON_METRIC]->(m:Metric)
        WITH ds, col,
             [x IN collect({metric: m.name, value: qm.value})
              WHERE x.metric IS NOT NULL] AS measurements
        OPTIONAL MATCH (col)-[:HAS_TOP_VALUE]->(tv:TopValue)
        WITH ds, col, measurements,
             [x IN collect({value: tv.value, count: tv.count, frequency: tv.frequency})
              WHERE x.value IS NOT NULL] AS top_values
        RETURN ds.uri        AS dataset_uri,
               ds.schema     AS schema,
               ds.name       AS table_name,
               ds.row_count  AS row_count,
               col.uri       AS column_uri,
               col.name      AS column_name,
               col.dataType  AS data_type,
               col.nullable  AS nullable,
               col.primaryKey AS primary_key,
               col.ordinal   AS ordinal,
               measurements,
               top_values
        ORDER BY ds.schema, ds.name, col.ordinal
    """, uris=table_uris)
    return [dict(r) for r in result]


# ── Assembly ──────────────────────────────────────────────────────────────────

def build_column_context(session, table_uris: list[str]) -> list[dict]:
    """Assemble full context objects for each undescribed column in the specified tables."""
    columns = get_undescribed_columns_with_profiling(session, table_uris)

    if not columns:
        return []

    all_cols_by_ds = get_all_columns_for_tables(session, table_uris)
    fk_by_ds = get_fk_context(session, table_uris)
    rules_by_col = get_rules_per_column(session, table_uris)

    for col in columns:
        col_uri = col["column_uri"]
        ds_uri = col["dataset_uri"]
        col["rules"] = rules_by_col.get(col_uri, [])
        col["foreign_keys"] = fk_by_ds.get(ds_uri, [])
        col["all_table_columns"] = all_cols_by_ds.get(ds_uri, [])

    return columns


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query Neo4j for columns lacking :ColumnDescription nodes.",
    )
    parser.add_argument(
        "--tables",
        type=str,
        default=None,
        help="Comma-separated dataset URIs to fetch full context for",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON file path (default: metadata/column_context_<timestamp>.json)",
    )
    parser.add_argument("--project-code", default=None, help="Project code to scope queries to (filters by :Project node)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    args = parser.parse_args()

    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    print(f"Connecting to {bolt_uri} as '{args.username}' (database: {args.database})...")

    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        driver.verify_connectivity()
    except ServiceUnavailable as e:
        print(f"ERROR: Cannot connect to Neo4j at {bolt_uri}\n  {e}", file=sys.stderr)
        sys.exit(1)
    except AuthError as e:
        print(f"ERROR: Authentication failed\n  {e}", file=sys.stderr)
        sys.exit(1)

    with driver.session(database=args.database) as session:

        if args.tables:
            # ── Context mode: gather full column context for selected tables ──
            table_uris = [t.strip() for t in args.tables.split(",") if t.strip()]
            print(f"Gathering context for {len(table_uris)} table(s)...")

            columns = build_column_context(session, table_uris)

            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            metadata_dir = os.path.join(os.getcwd(), "metadata")
            os.makedirs(metadata_dir, exist_ok=True)

            output_path = (
                os.path.abspath(args.output)
                if args.output
                else os.path.join(metadata_dir, f"column_context_{ts}.json")
            )

            with open(output_path, "w") as f:
                json.dump(columns, f, indent=2, default=str)

            table_count = len(set(c["dataset_uri"] for c in columns))
            print(f"Written: {output_path}")
            print(f"  Tables:             {table_count}")
            print(f"  Undescribed columns: {len(columns)}")

        else:
            # ── List mode: show which tables have undescribed columns ──
            pc = getattr(args, "project_code", None)
            if pc:
                print(f"Scoping to project: {pc}")
            tables = list_tables_needing_descriptions(session, project_code=pc)

            if not tables:
                print("All columns already have descriptions — nothing to enrich.")
            else:
                print(f"\nTables with undescribed columns ({len(tables)} table(s)):\n")
                for i, t in enumerate(tables, 1):
                    print(
                        f"  {i:>2}. {t['schema']}.{t['name']}"
                        f"  ({t['undescribed_count']} column(s) without description)"
                        f"  uri={t['dataset_uri']}"
                    )

    driver.close()


if __name__ == "__main__":
    main()
