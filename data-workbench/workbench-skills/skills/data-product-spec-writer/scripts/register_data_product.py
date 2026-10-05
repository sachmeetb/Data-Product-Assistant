#!/usr/bin/env python3
"""
Register a completed data product in the Neo4j knowledge graph.

Creates a :DataProduct node (DPROD-inspired) and [:INCLUDES_DATASET] relationships
to the :Dataset nodes it covers. This marks those datasets as already belonging to
a data product so the spec writer skips them on future runs.

The :DataProduct node also carries [:HAS_SHAPE] links to the :NodeShape (DQ rule
containers) for each included dataset, making the full governance chain queryable.

Usage:
    python register_data_product.py \
      --product-id dp_employees_v1 \
      --product-name "Employees (Source-aligned)" \
      --domain "Human Resources" \
      --type source-aligned \
      --status draft \
      --version "1.0.0" \
      --owner "jane.doe@example.com" \
      --steward-team "data-platform" \
      --spec-file "/path/to/dp_employees_v1_spec.yaml" \
      --datasets employees.department employees.employee employees.salary \
      [--password your_password]

Exit codes:
    0  — success
    1  — connection or query failure
    2  — product ID already exists (use --force to overwrite)
"""
import sys
import argparse
from datetime import datetime, timezone


def install_deps():
    import importlib
    try:
        importlib.import_module('neo4j')
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'neo4j', '-q'])


install_deps()
from neo4j import GraphDatabase


def register(driver, database: str, product: dict, dataset_uris: list[str], force: bool) -> None:
    with driver.session(database=database) as session:

        # 1 — Check for existing DataProduct with same ID
        existing = session.run(
            "MATCH (dp:DataProduct {id: $id}) RETURN dp.uri AS uri LIMIT 1",
            id=product['id'],
        ).single()

        if existing:
            if not force:
                print(
                    f"ERROR: DataProduct '{product['id']}' already exists in the graph.\n"
                    f"       Use --force to overwrite it.",
                    file=sys.stderr,
                )
                sys.exit(2)
            # Delete the old node and its relationships
            session.run(
                "MATCH (dp:DataProduct {id: $id}) DETACH DELETE dp",
                id=product['id'],
            )
            print(f"  Replaced existing DataProduct '{product['id']}'.", file=sys.stderr)

        # 2 — Verify all dataset URIs exist
        check_rows = session.run(
            "MATCH (ds:Dataset) WHERE ds.uri IN $uris RETURN ds.uri AS uri",
            uris=dataset_uris,
        )
        found_uris = {r['uri'] for r in check_rows}
        missing = [u for u in dataset_uris if u not in found_uris]
        if missing:
            print("WARNING: The following dataset URIs were not found in the graph:", file=sys.stderr)
            for u in missing:
                print(f"  {u}", file=sys.stderr)
            print("  These datasets will be excluded from [:INCLUDES_DATASET] links.", file=sys.stderr)
            dataset_uris = [u for u in dataset_uris if u in found_uris]

        # 3 — Create the :DataProduct node
        session.run(
            """
            CREATE (dp:DataProduct {
                uri:          $uri,
                id:           $id,
                name:         $name,
                domain:       $domain,
                type:         $type,
                status:       $status,
                version:      $version,
                owner:        $owner,
                steward_team: $steward_team,
                spec_file:    $spec_file,
                created_at:   $created_at,
                dcatType:     'dprod:DataProduct'
            })
            """,
            uri=f"dataproduct:{product['id']}",
            **product,
            created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )
        print(f"  Created :DataProduct node  uri=dataproduct:{product['id']}", file=sys.stderr)

        # 4 — Link to :Dataset nodes
        if dataset_uris:
            session.run(
                """
                MATCH (dp:DataProduct {id: $product_id})
                MATCH (ds:Dataset)  WHERE ds.uri IN $uris
                CREATE (dp)-[:INCLUDES_DATASET]->(ds)
                """,
                product_id=product['id'],
                uris=dataset_uris,
            )
            print(
                f"  Created [:INCLUDES_DATASET] × {len(dataset_uris)} dataset(s)",
                file=sys.stderr,
            )

        # 5 — Link to :NodeShape nodes (DQ rule containers) if they exist
        shape_rows = session.run(
            """
            MATCH (ds:Dataset)-[:HAS_SHAPE]->(ns:NodeShape)
            WHERE ds.uri IN $uris
            RETURN ns.uri AS shape_uri
            """,
            uris=dataset_uris,
        )
        shape_uris = [r['shape_uri'] for r in shape_rows]
        if shape_uris:
            session.run(
                """
                MATCH (dp:DataProduct {id: $product_id})
                MATCH (ns:NodeShape) WHERE ns.uri IN $shape_uris
                CREATE (dp)-[:GOVERNED_BY]->(ns)
                """,
                product_id=product['id'],
                shape_uris=shape_uris,
            )
            print(
                f"  Created [:GOVERNED_BY] × {len(shape_uris)} NodeShape(s)",
                file=sys.stderr,
            )

        # 6 — Link :DataProduct to :Catalog (schema) nodes
        catalog_rows = session.run(
            """
            MATCH (cat:Catalog)-[:DCAT_DATASET]->(ds:Dataset)
            WHERE ds.uri IN $uris
            RETURN DISTINCT cat.uri AS cat_uri
            """,
            uris=dataset_uris,
        )
        cat_uris = [r['cat_uri'] for r in catalog_rows]
        if cat_uris:
            session.run(
                """
                MATCH (dp:DataProduct {id: $product_id})
                MATCH (cat:Catalog) WHERE cat.uri IN $cat_uris
                MERGE (dp)-[:FROM_CATALOG]->(cat)
                """,
                product_id=product['id'],
                cat_uris=cat_uris,
            )
            print(
                f"  Created [:FROM_CATALOG] × {len(cat_uris)} Catalog(s)",
                file=sys.stderr,
            )


def main():
    parser = argparse.ArgumentParser(
        description='Register a data product in the Neo4j knowledge graph'
    )
    parser.add_argument('--product-id', required=True,
                        help='Stable machine-friendly product ID (e.g. dp_employees_v1)')
    parser.add_argument('--product-name', required=True,
                        help='Human-readable product name')
    parser.add_argument('--domain', default=None, help='Business domain')
    parser.add_argument('--type', default='source-aligned', help='Product type')
    parser.add_argument('--status', default='draft',
                        choices=['draft', 'active', 'deprecated'])
    parser.add_argument('--version', default='1.0.0')
    parser.add_argument('--owner', default=None)
    parser.add_argument('--steward-team', default=None)
    parser.add_argument('--spec-file', default=None,
                        help='Path to the written YAML spec file')
    parser.add_argument('--datasets', nargs='+', required=True,
                        help='Dataset URIs or schema.table names to link '
                             '(e.g. employees.department  OR  dataset:employees.department)')
    parser.add_argument('--force', action='store_true',
                        help='Overwrite an existing DataProduct with the same ID')
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--bolt-port', type=int, default=7687)
    parser.add_argument('--username', default='neo4j')
    parser.add_argument('--password', default='your_password')
    parser.add_argument('--database', default='neo4j')
    args = parser.parse_args()

    # Normalise dataset identifiers to URI form
    def to_uri(s: str) -> str:
        return s if s.startswith('dataset:') else f'dataset:{s}'

    dataset_uris = [to_uri(d) for d in args.datasets]

    product = {
        'id': args.product_id,
        'name': args.product_name,
        'domain': args.domain,
        'type': args.type,
        'status': args.status,
        'version': args.version,
        'owner': args.owner,
        'steward_team': args.steward_team,
        'spec_file': args.spec_file,
    }

    bolt_uri = f'bolt://{args.host}:{args.bolt_port}'
    print(f'Connecting to {bolt_uri} as {args.username!r}...', file=sys.stderr)
    driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))

    try:
        register(driver, args.database, product, dataset_uris, args.force)
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
    finally:
        driver.close()

    print(f'Done. DataProduct registered: {args.product_id}')


if __name__ == '__main__':
    main()
