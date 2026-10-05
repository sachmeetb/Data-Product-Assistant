#!/usr/bin/env python3
"""Parse an ODCS v3.1 YAML file and persist to Neo4j as a graph substructure.

Matches the shape written by workbench/backend/routers/odcs.py so UI-driven and
agent-driven saves produce an interchangeable graph. Key labels / relationships:

  (:DataContract)-[:HAS_OWNER]->(:DataContractOwner)
  (:DataContract)-[:HAS_STEWARD]->(:DataContractSteward)
  (:DataContract)-[:HAS_TEAM_MEMBER]->(:DataContractTeamMember)
  (:DataContract)-[:HAS_ROLE]->(:DataContractRole)
  (:DataContract)-[:HAS_SERVER]->(:DataContractServer)
  (:DataContract)-[:HAS_SCHEMA]->(:DataContractSchema)
                                     -[:HAS_PROPERTY]->(:DataContractProperty)
  (:DataContract)-[:HAS_QUALITY_RULE]->(:DataContractQuality)
  (:DataContract)-[:HAS_SLA_PROPERTY]->(:DataContractSLAProperty)
  (:DataContract)-[:HAS_TERMS]->(:DataContractTerms)
"""

import argparse
import json
import sys
from typing import Any

import yaml
from neo4j import GraphDatabase

# ── Helpers ───────────────────────────────────────────────────────────────

def _to_json_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, default=str)
    except Exception:
        return str(v)


def _coerce_string(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    try:
        return yaml.dump(v, default_flow_style=False, sort_keys=False).strip()
    except Exception:
        return str(v)


_KNOWN_TOP_LEVEL = {
    "apiVersion", "kind", "id", "name", "version", "status",
    "domain", "dataProduct",
    "description", "purpose", "limitations",
    "owners", "stewards", "team", "roles", "servers",
    "schema", "quality", "slaProperties", "terms",
    "tags", "support", "customProperties", "extras",
}


def canonicalize(raw: dict) -> dict:
    """Normalize common ODCS v3.1 author variations to the canonical shape."""
    spec = dict(raw)

    schemas_out = []
    for s in (spec.get("schema") or []):
        if not isinstance(s, dict):
            continue
        props_in = s.get("properties") or s.get("fields") or s.get("columns") or []
        pk_names = set()
        for pk in (s.get("primaryKeys") or []):
            if isinstance(pk, dict) and pk.get("name"):
                pk_names.add(pk["name"])
            elif isinstance(pk, str):
                pk_names.add(pk)

        props_out = []
        for p in props_in:
            if not isinstance(p, dict):
                continue
            if "primaryKey" in p:
                is_pk = bool(p.get("primaryKey"))
            elif "isPrimaryKey" in p:
                is_pk = bool(p.get("isPrimaryKey"))
            else:
                is_pk = p.get("name") in pk_names
            if "required" in p:
                is_required = bool(p.get("required"))
            elif "nullable" in p:
                is_required = not bool(p.get("nullable"))
            elif "isNullable" in p:
                is_required = not bool(p.get("isNullable"))
            else:
                is_required = False
            examples = p.get("examples")
            if examples is not None and not isinstance(examples, list):
                examples = [examples]
            props_out.append({
                "name": p.get("name", ""),
                "physicalName": p.get("physicalName") or p.get("name", ""),
                "logicalName": p.get("logicalName", ""),
                "logicalType": p.get("logicalType", ""),
                "physicalType": p.get("physicalType") or p.get("dataType", ""),
                "description": p.get("description", ""),
                "primaryKey": is_pk,
                "required": is_required,
                "criticalDataElement": bool(p.get("criticalDataElement", False)),
                "pii": bool(p.get("pii", False)),
                "classification": p.get("classification", ""),
                "examples": examples or [],
                "logicalTypeOptions": p.get("logicalTypeOptions") or {},
            })
        schemas_out.append({
            "name": s.get("name", ""),
            "physicalName": s.get("physicalName") or s.get("name", ""),
            "physicalType": s.get("physicalType") or s.get("type") or "table",
            "description": s.get("description", ""),
            "properties": props_out,
            "foreignKeys": s.get("foreignKeys") or [],
        })
    if "schema" in spec:
        spec["schema"] = schemas_out

    sla_in = spec.get("slaProperties") or spec.get("sla") or []
    sla_out = []
    for s in sla_in:
        if not isinstance(s, dict):
            continue
        prop = s.get("property") or s.get("metric") or s.get("name") or ""
        val = s.get("value")
        if val is None:
            val = s.get("objective")
        if val is None and isinstance(s.get("threshold"), dict):
            val = s["threshold"].get("value")
        if val is None:
            val = ""
        unit = s.get("unit") or s.get("window") or ""
        sla_out.append({"property": str(prop), "value": str(val), "unit": str(unit)})
    if "slaProperties" in spec or "sla" in spec:
        spec["slaProperties"] = sla_out
    spec.pop("sla", None)

    quality_out = []
    for q in (spec.get("quality") or []):
        if not isinstance(q, dict):
            continue
        rule = q.get("rule") or q.get("type") or ""
        desc = q.get("description", "")
        hint_bits = []
        if q.get("dataset"):
            hint_bits.append(f"dataset={q['dataset']}")
        col = q.get("field") or q.get("column")
        if col:
            hint_bits.append(f"column={col}")
        if hint_bits:
            hint = "[" + ", ".join(hint_bits) + "]"
            desc = f"{desc} {hint}".strip() if desc else hint
        quality_out.append({
            "rule": str(rule),
            "name": q.get("name", ""),
            "description": desc,
            "severity": q.get("severity", "warning"),
            "dimension": q.get("dimension", ""),
            "businessImpact": q.get("businessImpact", ""),
        })
    if "quality" in spec:
        spec["quality"] = quality_out

    cp = spec.get("customProperties")
    if not spec.get("terms") and isinstance(cp, dict) and isinstance(cp.get("terms"), dict):
        spec["terms"] = cp["terms"]
    terms = spec.get("terms")
    if isinstance(terms, dict):
        spec["terms"] = {
            "usage": _coerce_string(terms.get("usage")),
            "limitations": _coerce_string(terms.get("limitations")),
            "billing": _coerce_string(terms.get("billing")),
            "noticePeriod": _coerce_string(terms.get("noticePeriod")),
        }

    tags = spec.get("tags")
    if isinstance(tags, list):
        spec["tags"] = [str(t) for t in tags if t is not None]
    elif isinstance(tags, str):
        spec["tags"] = [tags]
    else:
        spec.pop("tags", None)

    stewards = []
    for st in (spec.get("stewards") or []):
        if isinstance(st, dict):
            stewards.append({
                "name": st.get("name", ""),
                "email": st.get("email", ""),
                "role": st.get("role", ""),
                "username": st.get("username", ""),
            })
    if "stewards" in spec:
        spec["stewards"] = stewards

    team = []
    for t in (spec.get("team") or []):
        if isinstance(t, dict):
            team.append({
                "username": t.get("username") or t.get("name", ""),
                "role": t.get("role", ""),
                "name": t.get("name", ""),
                "email": t.get("email", ""),
            })
    if "team" in spec:
        spec["team"] = team

    roles = []
    for r in (spec.get("roles") or []):
        if not isinstance(r, dict):
            continue
        perms = r.get("permissions") if isinstance(r.get("permissions"), dict) else {}
        access = r.get("access") or perms.get("access") or ""
        datasets = r.get("datasets") or perms.get("datasets") or []
        if not isinstance(datasets, list):
            datasets = [datasets]
        roles.append({
            "role": r.get("role", ""),
            "description": r.get("description", ""),
            "access": str(access),
            "datasets": [str(d) for d in datasets],
        })
    if "roles" in spec:
        spec["roles"] = roles

    servers = []
    for sv in (spec.get("servers") or []):
        if not isinstance(sv, dict):
            continue
        ds = sv.get("datasets") or sv.get("tables") or []
        servers.append({
            "name": sv.get("name") or sv.get("server", ""),
            "environment": sv.get("environment", ""),
            "type": sv.get("type", ""),
            "account": sv.get("account", ""),
            "database": sv.get("database", ""),
            "schema": sv.get("schema", ""),
            "datasets": ds,
        })
    if "servers" in spec:
        spec["servers"] = servers

    support = spec.get("support")
    if isinstance(support, dict):
        contacts = list(support.get("contacts") or [])
        docs = list(support.get("documentation") or [])
        if not contacts:
            for key, ctype in (("contactEmail", "email"), ("contactSlack", "slack"),
                               ("contactPhone", "phone")):
                if support.get(key):
                    contacts.append({"type": ctype, "value": str(support[key])})
        if not docs:
            for key, dtype in (("documentationUrl", "docs"),
                               ("runbookUrl", "runbook")):
                if support.get(key):
                    docs.append({"type": dtype, "url": str(support[key])})
        normalised = {"contacts": contacts, "documentation": docs}
        escalation = support.get("escalationPolicy") or support.get("escalation")
        if escalation:
            normalised["escalationPolicy"] = _coerce_string(escalation)
        spec["support"] = normalised

    if isinstance(cp, dict):
        remaining = {k: v for k, v in cp.items() if k != "terms"}
        if remaining:
            spec["customProperties"] = remaining
        else:
            spec.pop("customProperties", None)

    extras = {k: v for k, v in spec.items() if k not in _KNOWN_TOP_LEVEL}
    for k in list(extras):
        spec.pop(k, None)
    if extras:
        spec["extras"] = extras

    return spec


# ── Cypher ────────────────────────────────────────────────────────────────

MERGE_CONTRACT = """\
MERGE (dc:DataContract {id: $id})
SET dc.name = $name,
    dc.version = $version,
    dc.status = $status,
    dc.apiVersion = $apiVersion,
    dc.kind = $kind,
    dc.domain = $domain,
    dc.dataProduct = $dataProduct,
    dc.description = $description,
    dc.purpose = $purpose,
    dc.limitations = $limitations,
    dc.tags = $tags,
    dc.support = $support,
    dc.customProperties = $customProperties,
    dc.extras = $extras,
    dc.updatedAt = datetime()
RETURN dc
"""

WIPE_SUBSTRUCTURE = """\
MATCH (dc:DataContract {id: $contract_id})
OPTIONAL MATCH (dc)-[:HAS_OWNER]->(o:DataContractOwner)
OPTIONAL MATCH (dc)-[:HAS_STEWARD]->(st:DataContractSteward)
OPTIONAL MATCH (dc)-[:HAS_TEAM_MEMBER]->(tm:DataContractTeamMember)
OPTIONAL MATCH (dc)-[:HAS_ROLE]->(r:DataContractRole)
OPTIONAL MATCH (dc)-[:HAS_SERVER]->(srv:DataContractServer)
OPTIONAL MATCH (dc)-[:HAS_QUALITY_RULE]->(q:DataContractQuality)
OPTIONAL MATCH (dc)-[:HAS_SLA_PROPERTY]->(sla:DataContractSLAProperty)
OPTIONAL MATCH (dc)-[:HAS_TERMS]->(t:DataContractTerms)
OPTIONAL MATCH (dc)-[:HAS_SCHEMA]->(s:DataContractSchema)
OPTIONAL MATCH (s)-[:HAS_PROPERTY]->(p:DataContractProperty)
DETACH DELETE o, st, tm, r, srv, q, sla, t, s, p
"""

CREATE_OWNER = """\
MATCH (dc:DataContract {id: $contract_id})
CREATE (dc)-[:HAS_OWNER]->(:DataContractOwner {contractId: $contract_id, username: $username, name: $name, role: $role, email: $email})
"""

CREATE_STEWARD = """\
MATCH (dc:DataContract {id: $contract_id})
CREATE (dc)-[:HAS_STEWARD]->(:DataContractSteward {contractId: $contract_id, name: $name, email: $email, role: $role, username: $username})
"""

CREATE_TEAM_MEMBER = """\
MATCH (dc:DataContract {id: $contract_id})
CREATE (dc)-[:HAS_TEAM_MEMBER]->(:DataContractTeamMember {contractId: $contract_id, username: $username, role: $role, name: $name, email: $email})
"""

CREATE_ROLE = """\
MATCH (dc:DataContract {id: $contract_id})
CREATE (dc)-[:HAS_ROLE]->(:DataContractRole {contractId: $contract_id, role: $role, description: $description, access: $access, datasets: $datasets})
"""

CREATE_SERVER = """\
MATCH (dc:DataContract {id: $contract_id})
CREATE (dc)-[:HAS_SERVER]->(:DataContractServer {contractId: $contract_id, name: $name, environment: $environment, type: $type, account: $account, database: $database, schema: $schema, datasets: $datasets})
"""

CREATE_SCHEMA = """\
MATCH (dc:DataContract {id: $contract_id})
CREATE (dc)-[:HAS_SCHEMA]->(:DataContractSchema {contractId: $contract_id, physicalName: $physicalName, name: $name, description: $description, physicalType: $physicalType, foreignKeys: $foreignKeys})
"""

CREATE_PROPERTY = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_SCHEMA]->(s:DataContractSchema {physicalName: $schema_name, contractId: $contract_id})
CREATE (s)-[:HAS_PROPERTY]->(:DataContractProperty {
    contractId: $contract_id, schemaName: $schema_name,
    physicalName: $physicalName, name: $name, logicalName: $logicalName,
    logicalType: $logicalType, physicalType: $physicalType, description: $description,
    primaryKey: $primaryKey, required: $required, criticalDataElement: $criticalDataElement,
    pii: $pii, classification: $classification, examples: $examples, logicalTypeOptions: $logicalTypeOptions,
    transformHint: $transformHint
})
"""

CREATE_QUALITY = """\
MATCH (dc:DataContract {id: $contract_id})
CREATE (dc)-[:HAS_QUALITY_RULE]->(:DataContractQuality {contractId: $contract_id, rule: $rule, name: $name, description: $description, severity: $severity, dimension: $dimension, businessImpact: $businessImpact})
"""

CREATE_SLA_PROPERTY = """\
MATCH (dc:DataContract {id: $contract_id})
CREATE (dc)-[:HAS_SLA_PROPERTY]->(:DataContractSLAProperty {contractId: $contract_id, property: $property, value: $value, unit: $unit})
"""

CREATE_TERMS = """\
MATCH (dc:DataContract {id: $contract_id})
CREATE (dc)-[:HAS_TERMS]->(:DataContractTerms {contractId: $contract_id, usage: $usage, limitations: $limitations, billing: $billing, noticePeriod: $noticePeriod})
"""


def save_to_graph(driver, database, spec, contract_id=None):
    """Persist a full ODCS v3.1 spec to Neo4j. Returns (contract_id, stats)."""
    spec = canonicalize(spec)
    cid = contract_id or spec.get("id", "")
    if not cid:
        raise ValueError("Contract ID is required (provide --contract-id or set 'id' in YAML)")

    stats = {
        "schemas": 0, "properties": 0,
        "owners": 0, "stewards": 0, "team": 0, "roles": 0, "servers": 0,
        "quality_rules": 0, "slaProperties": 0,
    }

    with driver.session(database=database) as session:
        session.run(MERGE_CONTRACT,
                    id=cid, name=spec.get("name", ""),
                    version=spec.get("version", "1.0.0"),
                    status=spec.get("status", "draft"),
                    apiVersion=spec.get("apiVersion", "v3.1.0"),
                    kind=spec.get("kind", "DataContract"),
                    domain=spec.get("domain", ""),
                    dataProduct=spec.get("dataProduct", ""),
                    description=spec.get("description", ""),
                    purpose=spec.get("purpose", ""),
                    limitations=spec.get("limitations", ""),
                    tags=[str(t) for t in (spec.get("tags") or [])],
                    support=_to_json_str(spec.get("support") or {}),
                    customProperties=_to_json_str(spec.get("customProperties") or {}),
                    extras=_to_json_str(spec.get("extras") or {}))

        session.run(WIPE_SUBSTRUCTURE, contract_id=cid)

        for owner in (spec.get("owners") or []):
            session.run(CREATE_OWNER, contract_id=cid,
                        username=owner.get("username", ""), name=owner.get("name", ""),
                        role=owner.get("role", ""), email=owner.get("email", ""))
            stats["owners"] += 1

        for st in (spec.get("stewards") or []):
            session.run(CREATE_STEWARD, contract_id=cid,
                        name=st.get("name", ""), email=st.get("email", ""),
                        role=st.get("role", ""), username=st.get("username", ""))
            stats["stewards"] += 1

        for tm in (spec.get("team") or []):
            session.run(CREATE_TEAM_MEMBER, contract_id=cid,
                        username=tm.get("username", ""), role=tm.get("role", ""),
                        name=tm.get("name", ""), email=tm.get("email", ""))
            stats["team"] += 1

        for r in (spec.get("roles") or []):
            session.run(CREATE_ROLE, contract_id=cid,
                        role=r.get("role", ""), description=r.get("description", ""),
                        access=r.get("access", ""),
                        datasets=[str(d) for d in (r.get("datasets") or [])])
            stats["roles"] += 1

        for sv in (spec.get("servers") or []):
            session.run(CREATE_SERVER, contract_id=cid,
                        name=sv.get("name", ""), environment=sv.get("environment", ""),
                        type=sv.get("type", ""), account=sv.get("account", ""),
                        database=sv.get("database", ""), schema=sv.get("schema", ""),
                        datasets=_to_json_str(sv.get("datasets") or []))
            stats["servers"] += 1

        for schema in (spec.get("schema") or []):
            phys = schema.get("physicalName") or schema.get("name", "")
            session.run(CREATE_SCHEMA, contract_id=cid,
                        physicalName=phys, name=schema.get("name", ""),
                        description=schema.get("description", ""),
                        physicalType=schema.get("physicalType", "table"),
                        foreignKeys=_to_json_str(schema.get("foreignKeys") or []))
            stats["schemas"] += 1

            for prop in (schema.get("properties") or []):
                examples = prop.get("examples") or []
                if not isinstance(examples, list):
                    examples = [examples]
                transform_hint = prop.get("transform")
                transform_hint_json = _to_json_str(transform_hint) if transform_hint else ""
                session.run(CREATE_PROPERTY, contract_id=cid, schema_name=phys,
                            physicalName=prop.get("physicalName") or prop.get("name", ""),
                            name=prop.get("name", ""), logicalName=prop.get("logicalName", ""),
                            logicalType=prop.get("logicalType", ""),
                            physicalType=prop.get("physicalType", ""),
                            description=prop.get("description", ""),
                            primaryKey=bool(prop.get("primaryKey", False)),
                            required=bool(prop.get("required", False)),
                            criticalDataElement=bool(prop.get("criticalDataElement", False)),
                            pii=bool(prop.get("pii", False)),
                            classification=prop.get("classification", ""),
                            examples=[str(x) for x in examples],
                            logicalTypeOptions=_to_json_str(prop.get("logicalTypeOptions") or {}),
                            transformHint=transform_hint_json)
                stats["properties"] += 1

        for q in (spec.get("quality") or []):
            session.run(CREATE_QUALITY, contract_id=cid,
                        rule=q.get("rule", ""), name=q.get("name", ""),
                        description=q.get("description", ""),
                        severity=q.get("severity", "warning"),
                        dimension=q.get("dimension", ""),
                        businessImpact=q.get("businessImpact", ""))
            stats["quality_rules"] += 1

        for sla in (spec.get("slaProperties") or []):
            session.run(CREATE_SLA_PROPERTY, contract_id=cid,
                        property=sla.get("property", ""),
                        value=str(sla.get("value", "")),
                        unit=sla.get("unit", ""))
            stats["slaProperties"] += 1

        terms = spec.get("terms") or {}
        if isinstance(terms, dict) and any(terms.values()):
            session.run(CREATE_TERMS, contract_id=cid,
                        usage=terms.get("usage", ""),
                        limitations=terms.get("limitations", ""),
                        billing=terms.get("billing", ""),
                        noticePeriod=terms.get("noticePeriod", ""))

    return cid, stats


def main():
    parser = argparse.ArgumentParser(description="Save ODCS v3.1 YAML spec to Neo4j graph")
    parser.add_argument("yaml_file", help="Path to ODCS YAML file")
    parser.add_argument("--contract-id", default=None, help="Override contract ID (default: from YAML)")
    parser.add_argument("--host", default="localhost", help="Neo4j host (default: localhost)")
    parser.add_argument("--bolt-port", type=int, default=7687, help="Neo4j Bolt port (default: 7687)")
    parser.add_argument("--username", default="neo4j", help="Neo4j username (default: neo4j)")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Neo4j database (default: neo4j)")
    parser.add_argument("--dry-run", action="store_true", help="Canonicalize and print summary without writing")
    args = parser.parse_args()

    with open(args.yaml_file) as f:
        spec = yaml.safe_load(f)

    spec = canonicalize(spec)

    if args.dry_run:
        cid = args.contract_id or spec.get("id", "<unset>")
        print(f"Contract ID: {cid}")
        print(f"Name: {spec.get('name', '')}")
        print(f"Schemas: {len(spec.get('schema', []))}")
        props = sum(len(s.get('properties', [])) for s in spec.get('schema', []))
        print(f"Properties: {props}")
        print(f"Owners: {len(spec.get('owners', []))}, Stewards: {len(spec.get('stewards', []))}, "
              f"Team: {len(spec.get('team', []))}, Roles: {len(spec.get('roles', []))}, "
              f"Servers: {len(spec.get('servers', []))}")
        print(f"Quality rules: {len(spec.get('quality', []))}")
        print(f"SLA properties: {len(spec.get('slaProperties', []))}")
        print(f"Tags: {spec.get('tags', [])}")
        print("(dry-run — no data written)")
        return

    uri = f"bolt://{args.host}:{args.bolt_port}"
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))
    try:
        cid, stats = save_to_graph(driver, args.database, spec, args.contract_id)
        print(f"Saved contract '{cid}' to Neo4j:")
        for k, v in stats.items():
            print(f"  {k}: {v}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
