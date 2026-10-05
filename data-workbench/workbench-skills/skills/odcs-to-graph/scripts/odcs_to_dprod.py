#!/usr/bin/env python3
"""Transform an ODCS v3.1 contract in Neo4j to a DPROD data product.

Produces one :DProdOutputDataset per :DataContractSchema, with :DProdColumn
nodes scoped to each dataset (so multi-schema contracts fan out instead of
collapsing into a single dataset).
"""

import argparse

from neo4j import GraphDatabase

# Cleanup first — removes any prior DPROD sub-structure for this contract so
# re-runs with renamed/removed schemas don't leave orphan datasets behind.
DPROD_WIPE = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})
OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(port:DProdOutputPort)
OPTIONAL MATCH (port)-[:DPROD_OUTPUT_DATASET]->(ds:DProdOutputDataset)
OPTIONAL MATCH (ds)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
DETACH DELETE port, ds, pc
"""

DPROD_UPSERT_PRODUCT = """\
MATCH (dc:DataContract {id: $contract_id})
MERGE (dp:DProdDataProduct {uri: 'dprod:' + dc.id})
SET dp.name = dc.name,
    dp.description = COALESCE(dc.description, dc.name, ''),
    dp.status = dc.status,
    dp.createdAt = datetime()
MERGE (dc)-[:MATERIALISES_AS]->(dp)
MERGE (dp)-[:DPROD_OUTPUT_PORT]->(port:DProdOutputPort {uri: 'dprod:port:' + dc.id})
RETURN dp.uri AS dprod_uri, dp.name AS dprod_name
"""

# Per-schema dataset. DProdColumn field names stay DPROD-spec (dataType,
# isPrimaryKey) but are populated from v3.1 ODCS physicalType / primaryKey.
DPROD_CREATE_DATASET = """\
MATCH (dp:DProdDataProduct {uri: 'dprod:' + $contract_id})-[:DPROD_OUTPUT_PORT]->(port:DProdOutputPort)
MATCH (dc:DataContract {id: $contract_id})-[:HAS_SCHEMA]->(s:DataContractSchema {physicalName: $schema_physical_name})
MERGE (port)-[:DPROD_OUTPUT_DATASET]->(ds:DProdOutputDataset {uri: 'dprod:ds:' + $contract_id + ':' + $schema_physical_name})
SET ds.name = s.name,
    ds.physicalName = s.physicalName,
    ds.description = s.description

WITH ds, s
OPTIONAL MATCH (s)-[:HAS_PROPERTY]->(p:DataContractProperty)
WITH ds, p
WHERE p IS NOT NULL
MERGE (ds)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn {
    uri: ds.uri + ':' + p.physicalName
})
SET pc.name = p.name,
    pc.searchName = toLower(p.name),
    pc.logicalName = p.logicalName,
    pc.dataType = p.physicalType,
    pc.logicalType = p.logicalType,
    pc.description = p.description,
    pc.isPrimaryKey = p.primaryKey,
    pc.datasetPhysicalName = ds.physicalName
RETURN ds.uri AS ds_uri, count(pc) AS column_count
"""

LIST_SCHEMAS = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_SCHEMA]->(s:DataContractSchema)
RETURN s.physicalName AS physical_name, s.name AS name ORDER BY s.name
"""

PUBLISH_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $dprod_uri})
SET dp.status = 'published',
    dp.publishedAt = datetime(),
    dp.publishedBy = $user
RETURN dp.uri AS uri, dp.name AS name
"""


def transform(driver, database, contract_id, publish=False, user="Data Product Owner"):
    """Transform ODCS contract to DPROD and optionally publish."""
    with driver.session(database=database) as session:
        session.run(DPROD_WIPE, contract_id=contract_id)

        row = session.run(DPROD_UPSERT_PRODUCT, contract_id=contract_id).single()
        if not row:
            return None
        dprod_uri = row["dprod_uri"]
        dprod_name = row["dprod_name"]

        datasets = []
        for sch in session.run(LIST_SCHEMAS, contract_id=contract_id):
            phys = sch["physical_name"]
            if not phys:
                continue
            dsrow = session.run(DPROD_CREATE_DATASET,
                                 contract_id=contract_id,
                                 schema_physical_name=phys).single()
            datasets.append({
                "physical_name": phys,
                "name": sch["name"],
                "uri": dsrow["ds_uri"] if dsrow else None,
                "columns": dsrow["column_count"] if dsrow else 0,
            })

        published = False
        if publish:
            pub = session.run(PUBLISH_QUERY, dprod_uri=dprod_uri, user=user).single()
            if pub:
                published = True

    return {
        "dprod_uri": dprod_uri,
        "dprod_name": dprod_name,
        "datasets": datasets,
        "published": published,
    }


def main():
    parser = argparse.ArgumentParser(description="Transform ODCS v3.1 contract to DPROD data product")
    parser.add_argument("--contract-id", required=True, help="ODCS contract ID to transform")
    parser.add_argument("--host", default="localhost", help="Neo4j host (default: localhost)")
    parser.add_argument("--bolt-port", type=int, default=7687, help="Neo4j Bolt port (default: 7687)")
    parser.add_argument("--username", default="neo4j", help="Neo4j username (default: neo4j)")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Neo4j database (default: neo4j)")
    parser.add_argument("--publish", action="store_true", help="Also publish the data product")
    parser.add_argument("--user", default="Data Product Owner", help="Publishing user name")
    parser.add_argument("--dry-run", action="store_true", help="Print queries without executing")
    args = parser.parse_args()

    if args.dry_run:
        print(f"Contract ID: {args.contract_id}")
        for name, q in [("DPROD_WIPE", DPROD_WIPE),
                        ("DPROD_UPSERT_PRODUCT", DPROD_UPSERT_PRODUCT),
                        ("LIST_SCHEMAS", LIST_SCHEMAS),
                        ("DPROD_CREATE_DATASET", DPROD_CREATE_DATASET)]:
            print(f"\n-- {name}\n{q.strip()}")
        if args.publish:
            print(f"\n-- PUBLISH_QUERY\n{PUBLISH_QUERY.strip()}")
        return

    uri = f"bolt://{args.host}:{args.bolt_port}"
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))
    try:
        result = transform(driver, args.database, args.contract_id, args.publish, args.user)
        if not result:
            print(f"No contract found with ID: {args.contract_id}", file=__import__("sys").stderr)
            raise SystemExit(1)
        print("DPROD generated:")
        print(f"  URI:  {result['dprod_uri']}")
        print(f"  Name: {result['dprod_name']}")
        print(f"  Datasets ({len(result['datasets'])}):")
        for d in result['datasets']:
            print(f"    - {d['name'] or d['physical_name']} ({d['columns']} columns)  [{d['uri']}]")
        if result['published']:
            print("  Status: published")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
