#!/usr/bin/env python3
"""Read an ODCS v3.1 contract from Neo4j and reconstruct YAML.

Matches the graph shape written by save_odcs_to_graph.py and the workbench
backend's _save_odcs_to_graph — v3.1 canonical fields only.
"""

import argparse
import json
import sys
from typing import Any

import yaml
from neo4j import GraphDatabase

# ── Queries ────────────────────────────────────────────────────────────────

READ_CONTRACT = """\
MATCH (dc:DataContract {id: $contract_id})
OPTIONAL MATCH (dc)-[:HAS_TERMS]->(terms:DataContractTerms)
RETURN dc, terms
"""

READ_OWNERS = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_OWNER]->(o:DataContractOwner)
RETURN o ORDER BY o.role, o.email
"""

READ_STEWARDS = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_STEWARD]->(st:DataContractSteward)
RETURN st ORDER BY st.name
"""

READ_TEAM = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_TEAM_MEMBER]->(tm:DataContractTeamMember)
RETURN tm ORDER BY tm.username
"""

READ_ROLES = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_ROLE]->(r:DataContractRole)
RETURN r ORDER BY r.role
"""

READ_SERVERS = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_SERVER]->(srv:DataContractServer)
RETURN srv ORDER BY srv.environment, srv.name
"""

READ_SCHEMAS = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_SCHEMA]->(s:DataContractSchema)
RETURN s ORDER BY s.name
"""

READ_PROPERTIES = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_SCHEMA]->(s:DataContractSchema {physicalName: $schema_name})
       -[:HAS_PROPERTY]->(p:DataContractProperty)
RETURN p ORDER BY p.name
"""

READ_QUALITY = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_QUALITY_RULE]->(q:DataContractQuality)
RETURN q
"""

READ_SLA = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_SLA_PROPERTY]->(sla:DataContractSLAProperty)
RETURN sla
"""


def _from_json(s: Any) -> Any:
    if s in (None, ""):
        return None
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except Exception:
        return s


def read_from_graph(driver, database, contract_id):
    """Reconstruct an ODCS v3.1 spec dict from the graph."""
    with driver.session(database=database) as session:
        row = session.run(READ_CONTRACT, contract_id=contract_id).single()
        if not row:
            return None

        dc = dict(row["dc"])
        terms_node = dict(row["terms"]) if row["terms"] else {}

        spec: dict = {
            "apiVersion": dc.get("apiVersion", "v3.1.0"),
            "kind": dc.get("kind", "DataContract"),
            "id": dc.get("id", ""),
            "name": dc.get("name", ""),
            "version": dc.get("version", "1.0.0"),
            "status": dc.get("status", "draft"),
            "domain": dc.get("domain", ""),
            "dataProduct": dc.get("dataProduct", ""),
            "description": dc.get("description", ""),
            "purpose": dc.get("purpose", ""),
            "limitations": dc.get("limitations", ""),
        }

        tags = dc.get("tags") or []
        if tags:
            spec["tags"] = list(tags)

        support = _from_json(dc.get("support"))
        if support:
            spec["support"] = support

        custom = _from_json(dc.get("customProperties"))
        if custom:
            spec["customProperties"] = custom

        extras = _from_json(dc.get("extras"))
        if extras:
            spec["extras"] = extras

        owners = [dict(r["o"]) for r in session.run(READ_OWNERS, contract_id=contract_id)]
        spec["owners"] = [
            {k: o.get(k, "") for k in ("username", "name", "role", "email")}
            for o in owners
        ]

        stewards = [dict(r["st"]) for r in session.run(READ_STEWARDS, contract_id=contract_id)]
        if stewards:
            spec["stewards"] = [
                {k: st.get(k, "") for k in ("name", "email", "role", "username")}
                for st in stewards
            ]

        team = [dict(r["tm"]) for r in session.run(READ_TEAM, contract_id=contract_id)]
        if team:
            spec["team"] = [
                {k: tm.get(k, "") for k in ("username", "role", "name", "email")}
                for tm in team
            ]

        roles = [dict(r["r"]) for r in session.run(READ_ROLES, contract_id=contract_id)]
        if roles:
            spec["roles"] = [
                {
                    "role": r.get("role", ""),
                    "description": r.get("description", ""),
                    "access": r.get("access", ""),
                    "datasets": list(r.get("datasets") or []),
                }
                for r in roles
            ]

        servers = [dict(r["srv"]) for r in session.run(READ_SERVERS, contract_id=contract_id)]
        if servers:
            spec["servers"] = [
                {
                    "name": srv.get("name", ""),
                    "environment": srv.get("environment", ""),
                    "type": srv.get("type", ""),
                    "account": srv.get("account", ""),
                    "database": srv.get("database", ""),
                    "schema": srv.get("schema", ""),
                    "datasets": _from_json(srv.get("datasets")) or [],
                }
                for srv in servers
            ]

        schemas = [dict(r["s"]) for r in session.run(READ_SCHEMAS, contract_id=contract_id)]
        spec["schema"] = []
        for s in schemas:
            props = [dict(r["p"]) for r in session.run(READ_PROPERTIES,
                                                         contract_id=contract_id,
                                                         schema_name=s["physicalName"])]
            entry = {
                "name": s.get("name", ""),
                "physicalName": s.get("physicalName", ""),
                "description": s.get("description", ""),
                "physicalType": s.get("physicalType", "table"),
                "properties": [
                    {
                        "name": p.get("name", ""),
                        "physicalName": p.get("physicalName", ""),
                        "logicalName": p.get("logicalName", ""),
                        "logicalType": p.get("logicalType", ""),
                        "physicalType": p.get("physicalType", ""),
                        "description": p.get("description", ""),
                        "primaryKey": bool(p.get("primaryKey", False)),
                        "required": bool(p.get("required", False)),
                        "criticalDataElement": bool(p.get("criticalDataElement", False)),
                        "pii": bool(p.get("pii", False)),
                        "classification": p.get("classification", ""),
                        "examples": list(p.get("examples") or []),
                        "logicalTypeOptions": _from_json(p.get("logicalTypeOptions")) or {},
                        # PO derivation hint, only emit when present
                        **({"transform": _from_json(p.get("transformHint"))}
                           if p.get("transformHint")
                           else {}),
                    }
                    for p in props
                ],
            }
            fks = _from_json(s.get("foreignKeys"))
            if fks:
                entry["foreignKeys"] = fks
            spec["schema"].append(entry)

        quality_rows = [dict(r["q"]) for r in session.run(READ_QUALITY, contract_id=contract_id)]
        spec["quality"] = [
            {
                "rule": q.get("rule", ""),
                "name": q.get("name", ""),
                "description": q.get("description", ""),
                "severity": q.get("severity", "warning"),
                "dimension": q.get("dimension", ""),
                "businessImpact": q.get("businessImpact", ""),
            }
            for q in quality_rows
        ]

        sla_rows = [dict(r["sla"]) for r in session.run(READ_SLA, contract_id=contract_id)]
        spec["slaProperties"] = [
            {"property": s.get("property", ""), "value": s.get("value", ""), "unit": s.get("unit", "")}
            for s in sla_rows
        ]

        if terms_node:
            spec["terms"] = {
                "usage": terms_node.get("usage", ""),
                "limitations": terms_node.get("limitations", ""),
                "billing": terms_node.get("billing", ""),
                "noticePeriod": terms_node.get("noticePeriod", ""),
            }

    return spec


def main():
    parser = argparse.ArgumentParser(description="Read ODCS v3.1 contract from Neo4j and reconstruct YAML")
    parser.add_argument("--contract-id", required=True, help="Contract ID to read")
    parser.add_argument("--host", default="localhost", help="Neo4j host (default: localhost)")
    parser.add_argument("--bolt-port", type=int, default=7687, help="Neo4j Bolt port (default: 7687)")
    parser.add_argument("--username", default="neo4j", help="Neo4j username (default: neo4j)")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Neo4j database (default: neo4j)")
    parser.add_argument("--output", default=None, help="Output YAML file (default: stdout)")
    parser.add_argument("--format", choices=["yaml", "json"], default="yaml", help="Output format (default: yaml)")
    parser.add_argument("--dry-run", action="store_true", help="Print queries without executing")
    args = parser.parse_args()

    if args.dry_run:
        print(f"Contract ID: {args.contract_id}")
        for name, q in [
            ("READ_CONTRACT", READ_CONTRACT), ("READ_OWNERS", READ_OWNERS),
            ("READ_STEWARDS", READ_STEWARDS), ("READ_TEAM", READ_TEAM),
            ("READ_ROLES", READ_ROLES), ("READ_SERVERS", READ_SERVERS),
            ("READ_SCHEMAS", READ_SCHEMAS), ("READ_PROPERTIES", READ_PROPERTIES),
            ("READ_QUALITY", READ_QUALITY), ("READ_SLA", READ_SLA),
        ]:
            print(f"\n-- {name}\n{q.strip()}")
        return

    uri = f"bolt://{args.host}:{args.bolt_port}"
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))
    try:
        spec = read_from_graph(driver, args.database, args.contract_id)
        if not spec:
            print(f"No contract found with ID: {args.contract_id}", file=sys.stderr)
            raise SystemExit(1)
        if args.format == "json":
            output = json.dumps(spec, indent=2)
        else:
            output = yaml.dump(spec, default_flow_style=False, sort_keys=False, allow_unicode=True)
        if args.output:
            with open(args.output, "w") as f:
                f.write(output)
            print(f"Contract written to {args.output}")
        else:
            print(output)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
