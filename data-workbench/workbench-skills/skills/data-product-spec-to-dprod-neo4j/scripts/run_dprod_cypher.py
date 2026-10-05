#!/usr/bin/env python3
"""
Load a DPROD-aligned Cypher script into Neo4j.

Pre-flight checks per product:
  1. No :DProdDataProduct with the same URI already exists.
  2. The :DataProduct registry node exists (needed to resolve dataset links).
  3. At least one :Dataset is linked via the registry (warns if none found).

Usage:
    python run_dprod_cypher.py <cypher_file> [options]

Options:
    --host HOST          Neo4j host (default: localhost)
    --bolt-port PORT     Bolt port (default: 7687)
    --username USER      Username (default: neo4j)
    --password PASS      Password (default: your_password)
    --database DB        Database (default: neo4j)
    --dry-run            Print statements without executing
    --force              Skip the duplicate-check pre-flight and load anyway
"""
import sys
import re
import argparse


def install_deps():
    import importlib
    try:
        importlib.import_module('neo4j')
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'neo4j', '-q'])


install_deps()
from neo4j import GraphDatabase  # noqa: E402


def parse_cypher(path: str) -> tuple[str | None, list[str]]:
    """
    Parse a DPROD Cypher file.
    Returns (product_id, [statements]).
    Product ID is extracted from the '// Product: <id>' header comment.
    """
    with open(path) as fh:
        content = fh.read()

    product_id = None
    m = re.search(r'^//\s*Product:\s*(\S+)', content, re.MULTILINE)
    if m:
        product_id = m.group(1)

    raw = re.split(r';\s*\n', content)
    statements = []
    for s in raw:
        s = s.strip()
        # Strip comment-only blocks
        lines = [l for l in s.splitlines() if not l.strip().startswith('//') and l.strip()]
        if lines:
            statements.append(s)

    return product_id, statements


def preflight(session, product_id: str, dry_run: bool, force: bool) -> bool:
    """
    Returns True if it is safe to proceed, False if the load should be skipped.
    """
    if dry_run:
        return True

    # 1 — Check for existing DProdDataProduct
    if not force:
        dprod_uri = f'dprod:{product_id}'
        existing = session.run(
            'MATCH (dp:DProdDataProduct {uri: $uri}) RETURN dp.id AS id LIMIT 1',
            uri=dprod_uri,
        ).single()
        if existing:
            print(
                f'  SKIP: :DProdDataProduct already exists for product {product_id!r}.\n'
                f'        Use --force to overwrite (first delete existing nodes manually).',
                file=sys.stderr,
            )
            return False

    # 2 — Check registry :DataProduct node exists
    reg = session.run(
        'MATCH (dp:DataProduct {id: $id}) RETURN dp.id AS id LIMIT 1',
        id=product_id,
    ).single()
    if not reg:
        print(
            f'  WARNING: No :DataProduct registry node found for {product_id!r}.\n'
            f'           Dataset and quality shape links will fail at runtime.\n'
            f'           Run the data-product-spec-writer skill first.',
            file=sys.stderr,
        )
        # Allow proceeding — the MATCH statements will simply find no rows

    # 3 — Check linked datasets
    else:
        ds_count = session.run(
            'MATCH (:DataProduct {id: $id})-[:INCLUDES_DATASET]->(ds:Dataset) RETURN count(ds) AS n',
            id=product_id,
        ).single()['n']
        if ds_count == 0:
            print(
                f'  WARNING: Registry node found but no :Dataset nodes linked. '
                f'Dataset links will not be created.',
                file=sys.stderr,
            )

    return True


def run(cypher_path: str, neo4j_uri: str, auth: tuple, database: str,
        dry_run: bool, force: bool) -> tuple[int, int]:
    """Returns (succeeded, failed)."""
    product_id, statements = parse_cypher(cypher_path)
    print(f'Parsed {len(statements)} statement(s) from {cypher_path!r}', file=sys.stderr)
    if product_id:
        print(f'Product ID: {product_id}', file=sys.stderr)

    if dry_run:
        print('[dry-run] Statements that would be executed:', file=sys.stderr)
        for i, s in enumerate(statements, 1):
            preview = s[:100].replace('\n', ' ')
            print(f'  [{i}/{len(statements)}] {preview}...', file=sys.stderr)
        return len(statements), 0

    driver = GraphDatabase.driver(neo4j_uri, auth=auth)
    succeeded = 0
    failed = 0

    try:
        print(f'Connecting to {neo4j_uri} as {auth[0]!r} (database: {database})...',
              file=sys.stderr)

        with driver.session(database=database) as session:
            if not preflight(session, product_id, dry_run, force):
                return 0, 0

            for i, stmt in enumerate(statements, 1):
                preview = stmt[:90].replace('\n', ' ')
                try:
                    result = session.run(stmt)
                    summary = result.consume()
                    cntrs = summary.counters
                    nodes = cntrs.nodes_created
                    rels = cntrs.relationships_created
                    props = cntrs.properties_set
                    print(
                        f'  [{i}/{len(statements)}] OK '
                        f' nodes+={nodes}  rels+={rels}  props+={props}'
                        f'  | {preview}...',
                        file=sys.stderr,
                    )
                    succeeded += 1
                except Exception as exc:
                    print(
                        f'  [{i}/{len(statements)}] FAIL: {exc}\n'
                        f'    Statement: {preview}',
                        file=sys.stderr,
                    )
                    failed += 1
    finally:
        driver.close()

    return succeeded, failed


def main():
    parser = argparse.ArgumentParser(
        description='Load a DPROD Cypher script into Neo4j'
    )
    parser.add_argument('cypher_file', help='Path to the generated DPROD Cypher file')
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--bolt-port', type=int, default=7687)
    parser.add_argument('--username', default='neo4j')
    parser.add_argument('--password', default='your_password')
    parser.add_argument('--database', default='neo4j')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview statements without executing')
    parser.add_argument('--force', action='store_true',
                        help='Skip duplicate-check pre-flight')
    args = parser.parse_args()

    neo4j_uri = f'bolt://{args.host}:{args.bolt_port}'
    succeeded, failed = run(
        args.cypher_file,
        neo4j_uri,
        auth=(args.username, args.password),
        database=args.database,
        dry_run=args.dry_run,
        force=args.force,
    )

    print(f'\nDone. {succeeded} succeeded, {failed} failed.', file=sys.stderr)
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
