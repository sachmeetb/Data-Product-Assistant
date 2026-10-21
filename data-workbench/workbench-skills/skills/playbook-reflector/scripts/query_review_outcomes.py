#!/usr/bin/env python3
"""
Query Neo4j for review outcomes (descriptions and mappings) to feed the
playbook reflector. Gathers rejected items with corrections, approved items
with quality scores, and aggregate statistics.

Modes:
  (default)              Query review outcomes for a domain.
  --existing-playbook    Query existing playbook items for a domain.

Output:
  JSON file written to --output path.

Usage:
    python query_review_outcomes.py --domain "Human Resources" [options]

Options:
    --domain              Domain name to scope the query (required)
    --existing-playbook   Query existing playbook instead of review outcomes
    --output              Output JSON file path (default: playbook/review_outcomes.json)
    --host                Neo4j host (default: localhost)
    --bolt-port           Bolt port (default: 7687)
    --username            Neo4j username (default: neo4j)
    --password            Neo4j password (default: your_password)
    --database            Neo4j database name (default: neo4j)
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


# ── Queries: Review Outcomes ─────────────────────────────────────────────────

REJECTED_DESCRIPTIONS_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(orig:ColumnDescription)
MATCH (act:ProvActivity {activityType: 'review', outcome: 'rejected'})-[:PROV_USED]->(orig)
MATCH (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent:ProvAgent)
OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
OPTIONAL MATCH (corrected:ColumnDescription)-[:PROV_WAS_DERIVED_FROM]->(orig)
WHERE corrected.isCurrent = true
RETURN
    ds.schema         AS schema,
    ds.name           AS table_name,
    col.name          AS column_name,
    col.dataType      AS data_type,
    col.primaryKey     AS primary_key,
    col.nullable      AS nullable,
    orig.text         AS original_text,
    corrected.text    AS corrected_text,
    reason.category   AS rejection_category,
    reason.detail     AS rejection_detail,
    act.uri           AS activity_uri,
    act.occurredAt    AS occurred_at,
    agent.name        AS reviewer
ORDER BY ds.name, col.name
"""

APPROVED_DESCRIPTIONS_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
WHERE cd.status = 'approved' AND cd.isCurrent = true
MATCH (act:ProvActivity {activityType: 'review', outcome: 'approved'})-[:PROV_USED]->(cd)
RETURN
    ds.schema         AS schema,
    ds.name           AS table_name,
    col.name          AS column_name,
    col.dataType      AS data_type,
    col.primaryKey     AS primary_key,
    col.nullable      AS nullable,
    cd.text           AS description_text,
    act.quality       AS quality,
    act.uri           AS activity_uri,
    act.occurredAt    AS occurred_at
ORDER BY act.quality DESC, ds.name, col.name
"""

REJECTED_MAPPINGS_QUERY = """\
MATCH (orig:ColumnMapping)-[:MAPS_SOURCE_COLUMN]->(col:Column)
MATCH (orig)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col)
MATCH (act:ProvActivity {activityType: 'mapping_review', outcome: 'rejected'})-[:PROV_USED]->(orig)
OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
OPTIONAL MATCH (remapped:ColumnMapping)-[:PROV_WAS_DERIVED_FROM]->(orig)
OPTIONAL MATCH (remapped)-[:MAPS_TO_PRODUCT_COLUMN]->(rpc:DProdColumn)
RETURN
    ds.schema           AS source_schema,
    ds.name             AS source_table,
    col.name            AS source_column,
    col.dataType        AS source_type,
    pc.name             AS original_target,
    rpc.name            AS remapped_target,
    orig.similarityScore AS original_score,
    reason.category     AS rejection_category,
    reason.detail       AS rejection_detail,
    act.uri             AS activity_uri,
    act.occurredAt      AS occurred_at
ORDER BY ds.name, col.name
"""

APPROVED_MAPPINGS_QUERY = """\
MATCH (cm:ColumnMapping)-[:MAPS_SOURCE_COLUMN]->(col:Column)
MATCH (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col)
WHERE cm.status = 'approved' AND cm.isCurrent = true
MATCH (act:ProvActivity {activityType: 'mapping_review', outcome: 'approved'})-[:PROV_USED]->(cm)
RETURN
    ds.schema             AS source_schema,
    ds.name               AS source_table,
    col.name              AS source_column,
    col.dataType          AS source_type,
    pc.name               AS target_column,
    cm.similarityScore    AS similarity_score,
    cm.rationale          AS rationale,
    act.quality           AS quality,
    act.uri               AS activity_uri,
    act.occurredAt        AS occurred_at
ORDER BY act.quality DESC, ds.name, col.name
"""

# ── Queries: Existing Playbook ───────────────────────────────────────────────

EXISTING_PLAYBOOK_QUERY = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)-[:HAS_ITEM]->(pi:PlaybookItem)
WHERE pi.isCurrent = true
OPTIONAL MATCH (pi)-[:PROV_WAS_GENERATED_BY]->(act:ProvActivity)
RETURN
    pb.phase       AS phase,
    pi.uri         AS item_uri,
    pi.rule        AS rule,
    pi.version     AS version,
    act.operation  AS operation,
    act.rationale  AS rationale,
    act.occurredAt AS updated_at
ORDER BY pb.phase, pi.version DESC
"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query Neo4j for review outcomes to feed the playbook reflector.",
    )
    parser.add_argument("--domain", required=True, help="Domain name")
    parser.add_argument("--existing-playbook", action="store_true",
                        help="Query existing playbook items instead of review outcomes")
    parser.add_argument("--output", type=str, default=None)
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

    playbook_dir = os.path.join(os.getcwd(), "playbook")
    os.makedirs(playbook_dir, exist_ok=True)

    with driver.session(database=args.database) as session:

        if args.existing_playbook:
            # ── Existing playbook mode ──
            result = session.run(EXISTING_PLAYBOOK_QUERY, domain=args.domain)
            items = [dict(r) for r in result]

            output_path = args.output or os.path.join(playbook_dir, "existing_playbook.json")
            with open(output_path, "w") as f:
                json.dump({"domain": args.domain, "items": items}, f, indent=2, default=str)

            print(f"Written: {output_path}")
            print(f"  Existing playbook items: {len(items)}")

        else:
            # ── Review outcomes mode ──
            rejected_descs = [dict(r) for r in session.run(REJECTED_DESCRIPTIONS_QUERY)]
            approved_descs = [dict(r) for r in session.run(APPROVED_DESCRIPTIONS_QUERY)]
            rejected_maps = [dict(r) for r in session.run(REJECTED_MAPPINGS_QUERY)]
            approved_maps = [dict(r) for r in session.run(APPROVED_MAPPINGS_QUERY)]

            # Compute aggregate stats
            total_descs = len(approved_descs) + len(rejected_descs)
            total_maps = len(approved_maps) + len(rejected_maps)

            desc_rejection_rate = len(rejected_descs) / total_descs if total_descs > 0 else 0
            map_rejection_rate = len(rejected_maps) / total_maps if total_maps > 0 else 0

            # Rejection category counts
            desc_categories = {}
            for r in rejected_descs:
                cat = r.get("rejection_category") or "unknown"
                desc_categories[cat] = desc_categories.get(cat, 0) + 1

            map_categories = {}
            for r in rejected_maps:
                cat = r.get("rejection_category") or "unknown"
                map_categories[cat] = map_categories.get(cat, 0) + 1

            # Quality score distribution
            desc_quality = {}
            for r in approved_descs:
                q = r.get("quality") or 2
                desc_quality[q] = desc_quality.get(q, 0) + 1

            map_quality = {}
            for r in approved_maps:
                q = r.get("quality") or 2
                map_quality[q] = map_quality.get(q, 0) + 1

            outcomes = {
                "domain": args.domain,
                "queried_at": datetime.now(timezone.utc).isoformat(),
                "statistics": {
                    "descriptions": {
                        "total_reviewed": total_descs,
                        "approved": len(approved_descs),
                        "rejected": len(rejected_descs),
                        "rejection_rate": round(desc_rejection_rate, 3),
                        "rejection_categories": desc_categories,
                        "quality_distribution": {str(k): v for k, v in sorted(desc_quality.items())},
                    },
                    "mappings": {
                        "total_reviewed": total_maps,
                        "approved": len(approved_maps),
                        "rejected": len(rejected_maps),
                        "rejection_rate": round(map_rejection_rate, 3),
                        "rejection_categories": map_categories,
                        "quality_distribution": {str(k): v for k, v in sorted(map_quality.items())},
                    },
                },
                "rejected_descriptions": rejected_descs,
                "approved_descriptions": approved_descs,
                "rejected_mappings": rejected_maps,
                "approved_mappings": approved_maps,
            }

            output_path = args.output or os.path.join(playbook_dir, "review_outcomes.json")
            with open(output_path, "w") as f:
                json.dump(outcomes, f, indent=2, default=str)

            print(f"Written: {output_path}")
            print(f"  Descriptions — approved: {len(approved_descs)}, rejected: {len(rejected_descs)}")
            print(f"  Mappings     — approved: {len(approved_maps)}, rejected: {len(rejected_maps)}")
            if desc_categories:
                print(f"  Top description rejection categories: {desc_categories}")
            if map_categories:
                print(f"  Top mapping rejection categories: {map_categories}")

    driver.close()


if __name__ == "__main__":
    main()
