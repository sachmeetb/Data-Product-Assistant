#!/usr/bin/env python3
"""
Query Neo4j for column mapping candidates between a source dataset and a
target DProdDataProduct.

Source modes:
  catalog   (default) — sources are :Dataset / :Column from the project's
            own discovery graph (source-aligned and legacy dpe-cf flows).
  dprod     — sources are :DProdOutputDataset / :DProdColumn from
            *consumed* source-aligned products, reached via
            (:DataContract {id})-[:CONSUMES]->(:DProdDataProduct)->...->(:DProdColumn).
            Used for consumer-aligned dpe-cf products.

Modes:
  (default)                     List available source datasets and data products
                                in the chosen --source-mode.
  --source-dataset <uri>        Return mapping context for the specified
  --target-product <uri>        source dataset + target data product as JSON.

Output (default list mode):
  Plain-text numbered lists.

Output (context mode):
  JSON written to --output (default: metadata/mapping_candidates_<timestamp>.json).
  Output payload shape is identical for both source modes — only the
  resolution path differs. The agent's downstream prompt and write_mappings.py
  don't need to know which mode produced the candidates.

Usage:
    python query_mapping_candidates.py [options]

Options:
    --source-mode     'catalog' (default) or 'dprod' — picks the source-column
                      vocabulary (raw :Column vs source-product :DProdColumn)
    --source-dataset  Dataset URI (catalog mode) or DProdOutputDataset URI (dprod mode)
    --target-product  DProdDataProduct URI to fetch product columns for
                      (e.g. dprod:employee_product)
    --output          Output JSON file path
    --host            Neo4j host (default: localhost)
    --bolt-port       Bolt port (default: 7687)
    --username        Neo4j username (default: neo4j)
    --password        Neo4j password (default: your_password)
    --database        Neo4j database name (default: neo4j)
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


# ── Graph queries ──────────────────────────────────────────────────────────────

def list_source_datasets(session) -> list[dict]:
    """List all Datasets, showing how many columns have approved/current descriptions."""
    # ORDER BY uses RETURN aliases — when RETURN aggregates (count(...)), the
    # only variables in post-aggregation scope are the names introduced by
    # RETURN. Referencing `ds.schema` post-aggregation is rejected by Neo4j 5+.
    result = session.run("""
        MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
        OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
        WHERE cd.isCurrent = true AND cd.status = 'approved'
        RETURN ds.schema        AS schema,
               ds.name          AS name,
               ds.uri           AS dataset_uri,
               count(col)       AS total_columns,
               count(cd)        AS described_columns
        ORDER BY schema, name
    """)
    return [dict(r) for r in result]


def list_data_products(session) -> list[dict]:
    """List all DProdDataProduct nodes."""
    # ORDER BY name (the RETURN alias) — post-aggregation, `dp` is out of scope.
    result = session.run("""
        MATCH (dp:DProdDataProduct)
        OPTIONAL MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
                          -[:DPROD_OUTPUT_DATASET]->(od:DProdOutputDataset)
                          -[:HAS_PRODUCT_COLUMN]->(c:DProdColumn)
        RETURN dp.uri    AS product_uri,
               dp.name   AS name,
               dp.domain AS domain,
               dp.status AS status,
               count(c)  AS column_count
        ORDER BY name
    """)
    return [dict(r) for r in result]


def get_source_columns(session, dataset_uri: str) -> list[dict]:
    """Fetch source columns with their current approved ColumnDescription."""
    result = session.run("""
        MATCH (ds:Dataset {uri: $dataset_uri})-[:HAS_COLUMN]->(col:Column)
        OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
        WHERE cd.isCurrent = true
        RETURN col.uri       AS column_uri,
               col.name      AS column_name,
               col.dataType  AS data_type,
               col.nullable  AS nullable,
               col.primaryKey AS primary_key,
               col.ordinal   AS ordinal,
               cd.text       AS description,
               cd.status     AS description_status
        ORDER BY col.ordinal
    """, dataset_uri=dataset_uri)
    return [dict(r) for r in result]


# ── DPROD source mode (consumer-aligned: sources are SA product columns) ────

def list_source_dprod_datasets(session, target_contract_id: str | None = None) -> list[dict]:
    """List :DProdOutputDataset nodes that the consumer contract consumes.

    When ``target_contract_id`` is supplied we scope to outputs from products
    reachable via ``(:DataContract {id})-[:CONSUMES]->(:DProdDataProduct)``,
    filtering the temporal CONSUMES edge to the contract's current version.
    Without it we list all DProdOutputDatasets in the graph (useful for
    debugging; the agent should always pass the contract id).
    """
    if target_contract_id:
        q = """
            MATCH (dc:DataContract {id: $contract_id})-[r_c:CONSUMES]->(dp:DProdDataProduct)
            WHERE r_c.fromVersion <= dc.currentVersion
              AND (r_c.toVersion IS NULL OR r_c.toVersion >= dc.currentVersion)
            MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
                  -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
                  -[:HAS_PRODUCT_COLUMN]->(c:DProdColumn)
            RETURN dp.uri              AS source_product_uri,
                   coalesce(dp.name, '') AS source_product_name,
                   ods.uri             AS dataset_uri,
                   coalesce(ods.physicalName, ods.name, '') AS name,
                   coalesce(ods.physicalName, '') AS physical_name,
                   count(c)            AS total_columns
            ORDER BY source_product_name, physical_name
        """
        result = session.run(q, contract_id=target_contract_id)
    else:
        q = """
            MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
                  -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
                  -[:HAS_PRODUCT_COLUMN]->(c:DProdColumn)
            RETURN dp.uri              AS source_product_uri,
                   coalesce(dp.name, '') AS source_product_name,
                   ods.uri             AS dataset_uri,
                   coalesce(ods.physicalName, ods.name, '') AS name,
                   coalesce(ods.physicalName, '') AS physical_name,
                   count(c)            AS total_columns
            ORDER BY source_product_name, physical_name
        """
        result = session.run(q)
    return [dict(r) for r in result]


def get_source_columns_dprod(session, dataset_uri: str) -> list[dict]:
    """Fetch :DProdColumn nodes belonging to a :DProdOutputDataset.

    Output shape mirrors ``get_source_columns`` so the downstream agent and
    write_mappings.py are mode-agnostic. ``description`` comes from the
    DProdColumn's own .description (descriptions on source-aligned products
    were already approved by the source PO, so there's no separate
    :ColumnDescription review chain on the consumer side).
    """
    result = session.run("""
        MATCH (ods:DProdOutputDataset {uri: $dataset_uri})-[:HAS_PRODUCT_COLUMN]->(c:DProdColumn)
        RETURN c.uri                 AS column_uri,
               c.name                AS column_name,
               c.dataType            AS data_type,
               coalesce(c.nullable, true) AS nullable,
               coalesce(c.isPrimaryKey, false) AS primary_key,
               coalesce(c.ordinal, 0) AS ordinal,
               coalesce(c.description, '') AS description,
               'approved'            AS description_status,
               coalesce(c.datasetPhysicalName, '') AS source_dataset_physical_name
        ORDER BY c.ordinal, c.name
    """, dataset_uri=dataset_uri)
    return [dict(r) for r in result]


def get_product_columns(session, product_uri: str) -> list[dict]:
    """Fetch DProdColumn nodes for the given product, including dataset name.

    Includes the PO's `transformHint` if present — this is JSON-encoded
    derivation intent (kind/inputs/separator/...) that the agent should
    treat as authoritative when proposing the mapping.
    """
    result = session.run("""
        MATCH (dp:DProdDataProduct {uri: $product_uri})
        MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
                  -[:DPROD_OUTPUT_DATASET]->(od:DProdOutputDataset)
                  -[:HAS_PRODUCT_COLUMN]->(c:DProdColumn)
        RETURN dp.uri           AS product_uri,
               dp.name          AS product_name,
               od.uri           AS output_dataset_uri,
               od.name          AS output_dataset_name,
               c.uri            AS column_uri,
               c.name           AS column_name,
               c.dataType       AS data_type,
               c.nullable       AS nullable,
               c.ordinal        AS ordinal,
               c.description    AS description,
               c.sourceColumn   AS source_column_hint,
               c.transformHint  AS transform_hint
        ORDER BY c.ordinal
    """, product_uri=product_uri)
    rows = []
    import json as _json
    for r in result:
        d = dict(r)
        # Decode transformHint JSON so the agent sees it as a structured object
        # rather than an opaque string. Empty/null stays out of the payload.
        th = d.pop("transform_hint", None)
        if th:
            try:
                d["transform_hint"] = _json.loads(th)
            except (ValueError, TypeError):
                # Surface the raw string if it isn't valid JSON; better than dropping it.
                d["transform_hint"] = th
        rows.append(d)
    return rows


def get_existing_mappings(session, dataset_uri: str, product_uri: str) -> set[tuple]:
    """Return set of (source_column_uri, product_column_uri) that already have a
    current ColumnMapping (any status), so we can skip re-generating them.

    Source side accepts either :Column (catalog mode) or :DProdColumn (dprod
    mode) — the dataset filter is applied on whichever container the source
    column lives in.
    """
    result = session.run("""
        MATCH (cm:ColumnMapping)
        WHERE cm.isCurrent = true
        MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(src)
        WHERE src:Column OR src:DProdColumn
        MATCH (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
        OPTIONAL MATCH (ds:Dataset)-[:HAS_COLUMN]->(src)
        OPTIONAL MATCH (ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(src)
        MATCH (dp:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
                  -[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
                  -[:HAS_PRODUCT_COLUMN]->(pc)
        WHERE coalesce(ds.uri, ods.uri) = $dataset_uri AND dp.uri = $product_uri
        RETURN src.uri AS source_col_uri, pc.uri AS product_col_uri
    """, dataset_uri=dataset_uri, product_uri=product_uri)
    return {(r["source_col_uri"], r["product_col_uri"]) for r in result}


# ── Assembly ───────────────────────────────────────────────────────────────────

def build_mapping_candidates(
    session,
    dataset_uri: str,
    product_uri: str,
    source_mode: str = "catalog",
) -> dict:
    """Assemble the full mapping candidate payload.

    ``source_mode`` picks the source-column resolution path:
      - 'catalog' walks :Dataset->:Column (raw discovery graph)
      - 'dprod' walks :DProdOutputDataset->:DProdColumn (consumed source product)

    The product side is identical in both modes since the consumer's own
    output is always a :DProdDataProduct.
    """
    if source_mode == "dprod":
        source_cols = get_source_columns_dprod(session, dataset_uri)
    else:
        source_cols = get_source_columns(session, dataset_uri)
    product_cols = get_product_columns(session, product_uri)
    existing = get_existing_mappings(session, dataset_uri, product_uri)

    if not source_cols:
        print(f"WARNING: No columns found for dataset {dataset_uri}", file=sys.stderr)
    if not product_cols:
        print(f"WARNING: No DProdColumn nodes found for product {product_uri}", file=sys.stderr)

    # Tag source columns that already have a current mapping
    for col in source_cols:
        col["already_mapped"] = any(
            col["column_uri"] == src for src, _ in existing
        )

    return {
        "source_dataset_uri": dataset_uri,
        "target_product_uri": product_uri,
        "source_mode": source_mode,
        "existing_mapping_count": len(existing),
        "source_columns": source_cols,
        "product_columns": product_cols,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query Neo4j for column mapping candidates.",
    )
    parser.add_argument(
        "--source-mode",
        choices=("catalog", "dprod"),
        default="catalog",
        help=(
            "'catalog' (default) — sources are :Dataset / :Column from the project's "
            "discovery graph (source-aligned and legacy dpe-cf flows). "
            "'dprod' — sources are :DProdOutputDataset / :DProdColumn from consumed "
            "source-aligned products (consumer-aligned dpe-cf)."
        ),
    )
    parser.add_argument(
        "--source-dataset",
        metavar="URI",
        default=None,
        help=(
            "Source dataset URI. catalog mode: a :Dataset uri "
            "(e.g. dataset:employees.employee). dprod mode: a "
            ":DProdOutputDataset uri (e.g. dprod:ds:cust-sa-051125-01-contract:customers)."
        ),
    )
    parser.add_argument(
        "--target-product",
        metavar="URI",
        default=None,
        help="Target data product URI (e.g. dprod:employee_product)",
    )
    parser.add_argument(
        "--target-contract",
        metavar="ID",
        default=None,
        help=(
            "Consumer contract id, used in --source-mode dprod list mode to "
            "scope the displayed source datasets to those reached via :CONSUMES "
            "from this contract. Required for sane list output in dprod mode."
        ),
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON file path (default: metadata/mapping_candidates_<timestamp>.json)",
    )
    parser.add_argument("--host",      default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username",  default=DEFAULT_USERNAME)
    parser.add_argument("--password",  default=DEFAULT_PASSWORD)
    parser.add_argument("--database",  default=DEFAULT_DATABASE)
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

        if args.source_dataset and args.target_product:
            # ── Context mode ──────────────────────────────────────────────────
            print(
                f"Fetching mapping candidates ({args.source_mode}):\n"
                f"  Source:  {args.source_dataset}\n"
                f"  Product: {args.target_product}"
            )
            payload = build_mapping_candidates(
                session, args.source_dataset, args.target_product, source_mode=args.source_mode
            )

            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            metadata_dir = os.path.join(os.getcwd(), "metadata")
            os.makedirs(metadata_dir, exist_ok=True)
            output_path = (
                os.path.abspath(args.output)
                if args.output
                else os.path.join(metadata_dir, f"mapping_candidates_{ts}.json")
            )

            with open(output_path, "w") as f:
                json.dump(payload, f, indent=2, default=str)

            src_count = len(payload["source_columns"])
            prd_count = len(payload["product_columns"])
            already = payload["existing_mapping_count"]
            unmapped = sum(1 for c in payload["source_columns"] if not c["already_mapped"])
            print(f"Written: {output_path}")
            print(f"  Source columns:          {src_count}")
            print(f"  Product columns:         {prd_count}")
            print(f"  Already mapped (current): {already}")
            print(f"  Awaiting mapping:         {unmapped}")

        else:
            # ── List mode ─────────────────────────────────────────────────────
            if args.source_mode == "dprod":
                datasets = list_source_dprod_datasets(session, args.target_contract)
                print(f"\nAvailable source datasets (dprod mode, contract={args.target_contract or '<all>'}):\n")
                if not datasets:
                    print(
                        "  (none found — confirm this consumer contract has :CONSUMES edges to "
                        "published source-aligned products)"
                    )
                for i, d in enumerate(datasets, 1):
                    print(
                        f"  {i:>2}. {d['source_product_name']} / {d['name']}"
                        f"  ({d['total_columns']} columns)  uri={d['dataset_uri']}"
                    )
            else:
                datasets = list_source_datasets(session)
                print("\nAvailable source datasets:\n")
                if not datasets:
                    print("  (none found — run data-discovery and data-discovery-to-dcat-neo4j first)")
                for i, d in enumerate(datasets, 1):
                    desc_note = (
                        f"{d['described_columns']}/{d['total_columns']} columns described"
                    )
                    print(f"  {i:>2}. {d['schema']}.{d['name']}  ({desc_note})  uri={d['dataset_uri']}")

            products = list_data_products(session)
            print("\nAvailable data products:\n")
            if not products:
                print("  (none found — run load_employee_dprod.cypher or data-product-spec-to-dprod-neo4j first)")
            for i, p in enumerate(products, 1):
                print(
                    f"  {i:>2}. {p['name']}"
                    f"  domain={p['domain'] or 'n/a'}"
                    f"  columns={p['column_count']}"
                    f"  uri={p['product_uri']}"
                )

            if args.source_dataset or args.target_product:
                print(
                    "\nNOTE: Provide both --source-dataset and --target-product to fetch candidates.",
                    file=sys.stderr,
                )

    driver.close()


if __name__ == "__main__":
    main()
