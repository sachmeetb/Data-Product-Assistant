#!/usr/bin/env python3
"""Query Neo4j for review outcomes (descriptions + mappings) within a domain,
and optionally for the existing playbook items.

Usage:
  # Review outcomes
  python scripts/query_review_outcomes.py --domain 'Human Resources' \
      --host localhost --bolt-port 7687 --username neo4j --password pw \
      --database neo4j --output playbook/review_outcomes.json

  # Existing playbook
  python scripts/query_review_outcomes.py --domain 'Human Resources' \
      --existing-playbook --host localhost --bolt-port 7687 --username neo4j \
      --password pw --database neo4j --output playbook/existing_playbook.json
"""

import argparse
import json
import sys

from neo4j import GraphDatabase


# ── Review outcome queries ──────────────────────────────────────────────────

DESCRIPTION_REVIEWS_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
MATCH (act:ProvActivity)-[:PROV_USED]->(cd)
MATCH (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent:ProvAgent)
WHERE act.activityType = 'review'
OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
OPTIONAL MATCH (corrected:ColumnDescription)-[:PROV_WAS_DERIVED_FROM]->(cd)
WHERE corrected.isCurrent = true
RETURN
    'description'        AS review_type,
    act.uri              AS activity_uri,
    act.outcome          AS outcome,
    act.quality          AS quality,
    act.occurredAt       AS occurred_at,
    agent.name           AS reviewer,
    ds.name              AS table_name,
    col.name             AS column_name,
    col.dataType         AS data_type,
    cd.text              AS original_text,
    cd.status            AS original_status,
    reason.category      AS rejection_category,
    reason.detail        AS rejection_detail,
    corrected.text       AS corrected_text
ORDER BY act.occurredAt
"""

MAPPING_REVIEWS_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
MATCH (cm:ColumnMapping)-[:MAPS_SOURCE_COLUMN]->(col)
MATCH (act:ProvActivity)-[:PROV_USED]->(cm)
MATCH (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent:ProvAgent)
WHERE act.activityType = 'mapping_review'
OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
OPTIONAL MATCH (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
RETURN
    'mapping'            AS review_type,
    act.uri              AS activity_uri,
    act.outcome          AS outcome,
    act.quality          AS quality,
    act.occurredAt       AS occurred_at,
    agent.name           AS reviewer,
    ds.name              AS table_name,
    col.name             AS column_name,
    col.dataType         AS data_type,
    cm.rationale         AS original_text,
    cm.status            AS original_status,
    reason.category      AS rejection_category,
    reason.detail        AS rejection_detail,
    pc.name              AS product_column_name,
    pc.description       AS product_column_description
ORDER BY act.occurredAt
"""

# ── Existing playbook query ─────────────────────────────────────────────────

EXISTING_PLAYBOOK_QUERY = """\
MATCH (d:Domain {name: $domain})-[:HAS_PLAYBOOK]->(pb:Playbook)-[:HAS_ITEM]->(pi:PlaybookItem)
WHERE pi.isCurrent = true
OPTIONAL MATCH (pi)-[:PROV_WAS_GENERATED_BY]->(act:ProvActivity)
OPTIONAL MATCH (act)-[:PROV_INFORMED_BY]->(evidence:ProvActivity)
RETURN
    pi.uri         AS item_uri,
    pb.phase       AS phase,
    pi.rule        AS rule,
    pi.version     AS version,
    act.rationale  AS rationale,
    act.operation  AS operation,
    act.occurredAt AS updated_at,
    collect(evidence.uri) AS evidence_activities
ORDER BY pb.phase, pi.version DESC
"""


def run_query(driver, database, query, **params):
    with driver.session(database=database) as session:
        result = session.run(query, **params)
        return [dict(r) for r in result]


def main():
    parser = argparse.ArgumentParser(description="Query review outcomes from Neo4j")
    parser.add_argument("--domain", required=True, help="Domain name")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--bolt-port", type=int, default=7687)
    parser.add_argument("--username", default="neo4j")
    parser.add_argument("--password", required=True)
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--output", required=True, help="Output JSON file path")
    parser.add_argument("--existing-playbook", action="store_true",
                        help="Query existing playbook items instead of review outcomes")
    args = parser.parse_args()

    uri = f"bolt://{args.host}:{args.bolt_port}"
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))

    try:
        if args.existing_playbook:
            items = run_query(driver, args.database, EXISTING_PLAYBOOK_QUERY, domain=args.domain)
            output = {"domain": args.domain, "playbook_items": items}
            print(f"Found {len(items)} existing playbook items for domain '{args.domain}'")
        else:
            desc_reviews = run_query(driver, args.database, DESCRIPTION_REVIEWS_QUERY, domain=args.domain)
            map_reviews = run_query(driver, args.database, MAPPING_REVIEWS_QUERY, domain=args.domain)
            output = {
                "domain": args.domain,
                "description_reviews": desc_reviews,
                "mapping_reviews": map_reviews,
                "summary": {
                    "total_description_reviews": len(desc_reviews),
                    "total_mapping_reviews": len(map_reviews),
                    "description_rejections": sum(1 for r in desc_reviews if r["outcome"] == "rejected"),
                    "description_approvals": sum(1 for r in desc_reviews if r["outcome"] == "approved"),
                    "mapping_rejections": sum(1 for r in map_reviews if r["outcome"] == "rejected"),
                    "mapping_approvals": sum(1 for r in map_reviews if r["outcome"] == "approved"),
                    "quality_3_descriptions": sum(1 for r in desc_reviews if r.get("quality") == 3),
                    "quality_1_descriptions": sum(1 for r in desc_reviews if r.get("quality") == 1),
                    "quality_3_mappings": sum(1 for r in map_reviews if r.get("quality") == 3),
                    "quality_1_mappings": sum(1 for r in map_reviews if r.get("quality") == 1),
                }
            }
            s = output["summary"]
            print(f"Domain: {args.domain}")
            print(f"Description reviews: {s['total_description_reviews']} "
                  f"({s['description_approvals']} approved, {s['description_rejections']} rejected)")
            print(f"Mapping reviews: {s['total_mapping_reviews']} "
                  f"({s['mapping_approvals']} approved, {s['mapping_rejections']} rejected)")
            print(f"Quality-3 exemplars: {s['quality_3_descriptions']} descriptions, {s['quality_3_mappings']} mappings")
            print(f"Quality-1 marginal: {s['quality_1_descriptions']} descriptions, {s['quality_1_mappings']} mappings")

        with open(args.output, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"Written to {args.output}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
