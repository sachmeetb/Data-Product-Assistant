#!/usr/bin/env python3
"""
Extract data product spec defaults from the Neo4j knowledge graph.

Queries :Dataset, :Column, :QualityMeasurement, :TopValue, and :PropertyShape
nodes and outputs a JSON document with all derivable defaults for the
data-product-spec-writer skill.

Only returns datasets that are NOT already linked to a :DataProduct node via
an [:INCLUDES_DATASET] relationship. Pass --include-covered to override.

Usage:
    python extract_spec_defaults.py [output_file] [options]

Options:
    --host HOST            Neo4j host (default: localhost)
    --bolt-port PORT       Bolt port (default: 7687)
    --username USER        Username (default: neo4j)
    --password PASS        Password (default: your_password)
    --database DB          Database (default: neo4j)
    --schema SCHEMA        Filter to a specific schema (optional)
    --include-covered      Include datasets already in a DataProduct (default: skip them)
"""
import sys
import json
import argparse
import re


def install_deps():
    import importlib
    try:
        importlib.import_module('neo4j')
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'neo4j', '-q'])


install_deps()
from neo4j import GraphDatabase


# ── Sensitivity heuristics ────────────────────────────────────────────────────

_RESTRICTED = re.compile(
    r'\b(ssn|social_security|passport|dob|birth_date|biometric|salary|wage|compensation|tax_id|credit_card|card_number|cvv)\b',
    re.I
)
_CONFIDENTIAL = re.compile(
    r'\b(email|phone|mobile|address|street|city|zip|postal|first_name|last_name|full_name|gender|race|ethnicity|religion|amount|income|account_number|iban)\b',
    re.I
)


def suggest_classification(col_name: str) -> str:
    if _RESTRICTED.search(col_name):
        return 'restricted'
    if _CONFIDENTIAL.search(col_name):
        return 'confidential'
    return 'internal'


# ── Quality dimension mapping ─────────────────────────────────────────────────

_DIM_MAP = {
    'mandatory':            'completeness',
    'range':                'validity',
    'unique':               'uniqueness',
    'allowedValues':        'validity',
    'referentialIntegrity': 'consistency',
}


# ── Grain suggestion ──────────────────────────────────────────────────────────

def suggest_grain(table_name: str, pk_cols: list) -> str:
    singular = table_name.rstrip('s')
    if not pk_cols:
        return f'1 row per {singular}'
    if len(pk_cols) == 1:
        col = pk_cols[0]
        if col.lower() in ('id', f'{singular}_id', f'{table_name}_id'):
            return f'1 row per {singular}'
        return f'1 row per {col}'
    return f'1 row per {" + ".join(pk_cols)} combination'


# ── Example value from metrics / top values ───────────────────────────────────

def derive_example(col_data: dict) -> str | None:
    if col_data.get('top_values'):
        return str(col_data['top_values'][0]['value'])
    m = col_data.get('metrics', {})
    if 'percentile_50' in m:
        return str(m['percentile_50'])
    if 'min' in m:
        return str(m['min'])
    return None


# ── Main extraction ───────────────────────────────────────────────────────────

def extract_defaults(
    driver,
    database: str,
    schema_filter: str | None = None,
    include_covered: bool = False,
) -> dict:
    with driver.session(database=database) as session:

        # 0 — Find datasets already covered by a :DataProduct node
        covered_rows = session.run(
            """
            MATCH (dp:DataProduct)-[:INCLUDES_DATASET]->(ds:Dataset)
            RETURN ds.uri AS dataset_uri,
                   dp.id AS product_id,
                   dp.name AS product_name,
                   dp.status AS product_status,
                   dp.version AS product_version
            ORDER BY ds.uri
            """
        )
        covered = {}  # dataset_uri -> product info
        for r in covered_rows:
            covered[r['dataset_uri']] = {
                'product_id': r['product_id'],
                'product_name': r['product_name'],
                'product_status': r['product_status'],
                'product_version': r['product_version'],
            }

        # 1 — Datasets
        tables_rows = session.run(
            """
            MATCH (cat:Catalog)-[:DCAT_DATASET]->(ds:Dataset)
            WHERE $schema IS NULL OR cat.name = $schema
            RETURN cat.name AS schema, ds.name AS table_name, ds.uri AS uri,
                   ds.row_count AS row_count, ds.sample_size AS sample_size,
                   ds.profiled_at AS profiled_at
            ORDER BY cat.name, ds.name
            """,
            schema=schema_filter,
        )
        tables = {}
        schemas = set()
        skipped = []
        for r in tables_rows:
            uri = r['uri']
            if uri in covered and not include_covered:
                skipped.append({
                    'uri': uri,
                    'schema': r['schema'],
                    'name': r['table_name'],
                    **covered[uri],
                })
                continue
            schemas.add(r['schema'])
            tables[uri] = {
                'schema': r['schema'],
                'name': r['table_name'],
                'uri': uri,
                'row_count': r['row_count'],
                'sample_size': r['sample_size'],
                'profiled_at': r['profiled_at'],
                'already_covered': covered.get(uri),  # populated if --include-covered
                'primary_key': [],
                'foreign_keys': [],
                'referenced_by': [],
                'columns': [],
                'dq_rules': [],
            }

        if not tables and not skipped:
            return {'schemas': [], 'tables': [], 'skipped': []}

        if not tables:
            return {'schemas': [], 'tables': [], 'skipped': skipped}

        uris = list(tables.keys())

        # 2 — Columns
        col_map = {}
        cols_rows = session.run(
            """
            MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
            WHERE ds.uri IN $uris
            RETURN ds.uri AS dataset_uri, col.uri AS col_uri,
                   col.name AS name, col.dataType AS data_type,
                   col.nullable AS nullable, col.primaryKey AS primary_key,
                   col.ordinal AS ordinal
            ORDER BY ds.uri, col.ordinal
            """,
            uris=uris,
        )
        for r in cols_rows:
            col = {
                'name': r['name'],
                'data_type': r['data_type'],
                'nullable': r['nullable'],
                'primary_key': r['primary_key'],
                'ordinal': r['ordinal'],
                'suggested_classification': suggest_classification(r['name']),
                'example_value': None,
                'metrics': {},
                'top_values': [],
                'dq_rules': [],
                'quality_dimensions': [],
            }
            col_map[r['col_uri']] = col
            ds = tables.get(r['dataset_uri'])
            if ds is not None:
                ds['columns'].append(col)
                if r['primary_key']:
                    ds['primary_key'].append(r['name'])

        # 3 — Quality measurements
        if col_map:
            met_rows = session.run(
                """
                MATCH (col:Column)-[:HAS_QUALITY_MEASUREMENT]->(qm:QualityMeasurement)-[:ON_METRIC]->(m:Metric)
                WHERE col.uri IN $col_uris
                RETURN col.uri AS col_uri, m.name AS metric, qm.value AS value
                """,
                col_uris=list(col_map.keys()),
            )
            for r in met_rows:
                if r['col_uri'] in col_map:
                    col_map[r['col_uri']]['metrics'][r['metric']] = r['value']

        # 4 — Top values
        if col_map:
            tv_rows = session.run(
                """
                MATCH (col:Column)-[:HAS_TOP_VALUE]->(tv:TopValue)
                WHERE col.uri IN $col_uris
                RETURN col.uri AS col_uri, tv.value AS value, tv.count AS count,
                       tv.frequency AS frequency
                ORDER BY col.uri, tv.frequency DESC
                """,
                col_uris=list(col_map.keys()),
            )
            for r in tv_rows:
                if r['col_uri'] in col_map:
                    col_map[r['col_uri']]['top_values'].append({
                        'value': r['value'],
                        'count': r['count'],
                        'frequency': round(r['frequency'], 4),
                    })

        # 5 — Derive example values now that metrics and top_values are populated
        for col in col_map.values():
            col['example_value'] = derive_example(col)

        # 6 — DQ rules
        rules_rows = session.run(
            """
            MATCH (ds:Dataset)-[:HAS_SHAPE]->(ns:NodeShape)-[:PROPERTY]->(ps:PropertyShape)
            WHERE ds.uri IN $uris
            OPTIONAL MATCH (ps)-[:ON_COLUMN]->(col:Column)
            OPTIONAL MATCH (ps)-[:REFERENCES_DATASET]->(ref:Dataset)
            OPTIONAL MATCH (ps)-[:ALLOWED_VALUE]->(tv:TopValue)
            WITH ds, ps, col, ref, collect(tv.value) AS allowed_values
            RETURN ds.uri AS dataset_uri,
                   ps.uri AS rule_uri,
                   ps.ruleType AS rule_type,
                   ps.path AS column_name,
                   ps.severity AS severity,
                   ps.description AS description,
                   ps.confidence AS confidence,
                   ps.minInclusive AS min_inclusive,
                   ps.maxInclusive AS max_inclusive,
                   ps.uniquenessRatio AS uniqueness_ratio,
                   ps.uniquenessThreshold AS uniqueness_threshold,
                   ps.coverage AS coverage,
                   ps.coverageThreshold AS coverage_threshold,
                   col.uri AS col_uri,
                   ref.uri AS ref_uri,
                   ref.name AS ref_table_name,
                   allowed_values
            ORDER BY ds.uri, ps.ruleType, ps.path
            """,
            uris=uris,
        )
        for r in rules_rows:
            rule = {
                'uri': r['rule_uri'],
                'rule_type': r['rule_type'],
                'column_name': r['column_name'],
                'severity': r['severity'],
                'description': r['description'],
                'confidence': r['confidence'],
                'quality_dimension': _DIM_MAP.get(r['rule_type'], 'validity'),
            }
            if r['min_inclusive'] is not None:
                rule['min_inclusive'] = r['min_inclusive']
            if r['max_inclusive'] is not None:
                rule['max_inclusive'] = r['max_inclusive']
            if r['uniqueness_ratio'] is not None:
                rule['uniqueness_ratio'] = round(r['uniqueness_ratio'], 4)
            if r['uniqueness_threshold'] is not None:
                rule['uniqueness_threshold'] = r['uniqueness_threshold']
            if r['coverage'] is not None:
                rule['coverage'] = round(r['coverage'], 4)
            if r['allowed_values']:
                rule['allowed_values'] = sorted(str(v) for v in r['allowed_values'])
            if r['ref_uri']:
                rule['references_dataset'] = r['ref_uri']
                rule['references_table'] = r['ref_table_name']

            ds = tables.get(r['dataset_uri'])
            if ds is not None:
                ds['dq_rules'].append(rule)

            col_uri = r['col_uri']
            if col_uri and col_uri in col_map:
                col_map[col_uri]['dq_rules'].append(rule)
                dim = _DIM_MAP.get(r['rule_type'])
                if dim and dim not in col_map[col_uri]['quality_dimensions']:
                    col_map[col_uri]['quality_dimensions'].append(dim)

        # 7 — FK / REFERENCES relationships
        fk_rows = session.run(
            """
            MATCH (ds1:Dataset)-[r:REFERENCES]->(ds2:Dataset)
            WHERE ds1.uri IN $uris OR ds2.uri IN $uris
            RETURN ds1.uri AS from_uri, ds1.name AS from_table,
                   ds2.uri AS to_uri, ds2.name AS to_table,
                   r.columns AS columns, r.referencedColumns AS referenced_columns,
                   r.constraintName AS constraint_name,
                   r.onDelete AS on_delete
            """,
            uris=uris,
        )
        for r in fk_rows:
            fk_entry = {
                'constraint_name': r['constraint_name'],
                'columns': list(r['columns']) if r['columns'] else [],
                'referenced_table': r['to_table'],
                'referenced_columns': list(r['referencedColumns']) if r.get('referencedColumns') else [],
                'on_delete': r['on_delete'],
            }
            ref_back = {
                'from_table': r['from_table'],
                'columns': list(r['columns']) if r['columns'] else [],
                'referenced_columns': list(r['referencedColumns']) if r.get('referencedColumns') else [],
            }
            if r['from_uri'] in tables:
                tables[r['from_uri']]['foreign_keys'].append(fk_entry)
            if r['to_uri'] in tables:
                tables[r['to_uri']]['referenced_by'].append(ref_back)

        # 8 — Add grain suggestions and quality dimension summaries
        for ds in tables.values():
            ds['suggested_grain'] = suggest_grain(ds['name'], ds['primary_key'])
            dim_summary: dict = {}
            for col in ds['columns']:
                for dim in col['quality_dimensions']:
                    dim_summary.setdefault(dim, []).append(col['name'])
            ds['quality_dimensions_summary'] = dim_summary

        return {
            'schemas': sorted(schemas),
            'tables': list(tables.values()),
            'skipped': skipped,
        }


def main():
    parser = argparse.ArgumentParser(
        description='Extract data product spec defaults from Neo4j'
    )
    parser.add_argument('output_file', nargs='?', default=None,
                        help='Output JSON file path (default: stdout)')
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--bolt-port', type=int, default=7687)
    parser.add_argument('--username', default='neo4j')
    parser.add_argument('--password', default='your_password')
    parser.add_argument('--database', default='neo4j')
    parser.add_argument('--schema', default=None,
                        help='Filter to a specific schema')
    parser.add_argument('--include-covered', action='store_true',
                        help='Include datasets already linked to a DataProduct node')
    args = parser.parse_args()

    bolt_uri = f'bolt://{args.host}:{args.bolt_port}'
    print(f'Connecting to {bolt_uri} as {args.username!r}...', file=sys.stderr)
    driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))

    try:
        defaults = extract_defaults(
            driver, args.database, args.schema, args.include_covered
        )
    finally:
        driver.close()

    skipped = defaults.get('skipped', [])
    if skipped:
        print(f'Skipped {len(skipped)} dataset(s) already in a DataProduct:', file=sys.stderr)
        for s in skipped:
            print(f'  {s["schema"]}.{s["name"]} → {s["product_id"]} ({s["product_name"]})', file=sys.stderr)

    n_tables = len(defaults['tables'])
    n_cols = sum(len(t['columns']) for t in defaults['tables'])
    n_rules = sum(len(t['dq_rules']) for t in defaults['tables'])
    print(
        f'Eligible: {len(defaults["schemas"])} schema(s), '
        f'{n_tables} table(s), {n_cols} column(s), {n_rules} rule(s)',
        file=sys.stderr,
    )

    output = json.dumps(defaults, indent=2, default=str)
    if args.output_file:
        with open(args.output_file, 'w') as fh:
            fh.write(output)
        print(f'Written to {args.output_file}', file=sys.stderr)
    else:
        print(output)


if __name__ == '__main__':
    main()
