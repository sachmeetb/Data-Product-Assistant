#!/usr/bin/env python3
"""
Generate domain-aware DQ rules by matching reference file patterns to columns.

Reads structured YAML reference files (common.yaml + {domain}.yaml) and matches
their patterns against columns in the Neo4j knowledge graph. Produces Cypher
that creates :PropertyShape nodes with ruleSource='domain' and status='pending_review'.

Usage:
    python generate_domain_rules.py --domain hr --reference-dir ../reference [options]

Options:
    --host           Neo4j host (default: localhost)
    --bolt-port      Bolt port (default: 7687)
    --username       Neo4j username (default: neo4j)
    --password       Neo4j password (default: your_password)
    --database       Neo4j database name (default: neo4j)
    --domain         Domain name (matches reference file, e.g., 'hr')
    --reference-dir  Path to the reference directory
    --extra-rules    JSON array of additional rules from guidance interpretation
    --append         Append to existing Cypher file instead of overwriting
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    import yaml
except ImportError:
    yaml = None

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError
except ImportError:
    print("ERROR: neo4j driver required. Install with: pip install neo4j", file=sys.stderr)
    sys.exit(1)

DEFAULT_HOST = "localhost"
DEFAULT_BOLT_PORT = 7687
DEFAULT_USERNAME = "neo4j"
DEFAULT_PASSWORD = "your_password"
DEFAULT_DATABASE = "neo4j"


# ── Graph query ──────────────────────────────────────────────────────────────

COLUMNS_WITH_DESCRIPTIONS = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true})
WHERE cd.status = 'approved' OR cd.status IS NULL
OPTIONAL MATCH (ds)-[:HAS_SHAPE]->(ns:NodeShape)
RETURN ds.uri AS dataset_uri, ds.schema AS schema, ds.name AS table_name,
       col.uri AS col_uri, col.name AS col_name, col.dataType AS data_type,
       col.ordinal AS ordinal,
       cd.text AS description,
       ns.uri AS shape_uri
ORDER BY ds.schema, ds.name, col.ordinal
"""

COLUMNS_WITH_DESCRIPTIONS_SCOPED = """\
MATCH (:Project {projectCode: $project_code})
      -[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset)
      -[:HAS_COLUMN]->(col:Column)
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true})
WHERE cd.status = 'approved' OR cd.status IS NULL
OPTIONAL MATCH (ds)-[:HAS_SHAPE]->(ns:NodeShape)
RETURN ds.uri AS dataset_uri, ds.schema AS schema, ds.name AS table_name,
       col.uri AS col_uri, col.name AS col_name, col.dataType AS data_type,
       col.ordinal AS ordinal,
       cd.text AS description,
       ns.uri AS shape_uri
ORDER BY ds.schema, ds.name, col.ordinal
"""


def esc(value) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def cypher_value(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f"'{esc(v)}'"
    if isinstance(v, float):
        return repr(v)
    return str(v)


# ── Reference file loading ───────────────────────────────────────────────────

def load_yaml_file(path: str) -> dict:
    """Load a YAML file, falling back to basic parsing if PyYAML is not installed."""
    if yaml:
        with open(path) as f:
            return yaml.safe_load(f)
    # Fallback: basic key-value parsing for simple cases
    # This won't handle the full YAML spec but covers our reference format
    with open(path) as f:
        content = f.read()
    try:
        import json as json_mod
        # Try loading as JSON (YAML is a superset)
        return json_mod.loads(content)
    except Exception:
        print(f"WARNING: PyYAML not installed and file is not JSON-compatible: {path}", file=sys.stderr)
        return {"rules": []}


def load_reference_rules(ref_dir: str, domain: str) -> list[dict]:
    """Load and merge common.yaml + {domain}.yaml rules."""
    rules = []
    for filename in ["common.yaml", f"{domain}.yaml"]:
        path = os.path.join(ref_dir, filename)
        if os.path.exists(path):
            data = load_yaml_file(path)
            file_rules = data.get("rules", [])
            # Tag each rule with its source file
            for i, rule in enumerate(file_rules):
                rule["_source_file"] = filename
                rule["_source_index"] = i
            rules.extend(file_rules)
            print(f"  Loaded {len(file_rules)} rule(s) from {filename}")
        else:
            print(f"  No reference file found: {filename}")
    return rules


def load_values_file(ref_dir: str, filename: str) -> list[str]:
    """Load a values list file (one value per line)."""
    path = os.path.join(ref_dir, filename)
    if not os.path.exists(path):
        print(f"  WARNING: Values file not found: {path}")
        return []
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


# ── Rule matching ────────────────────────────────────────────────────────────

def matches_rule(col: dict, match_criteria: dict) -> bool:
    """Check if a column matches a rule's match criteria."""
    description = (col.get("description") or "").lower()
    col_name = (col.get("col_name") or "").lower()
    data_type = (col.get("data_type") or "").lower()

    # description_contains: at least one keyword must appear
    if "description_contains" in match_criteria:
        keywords = match_criteria["description_contains"]
        if not any(kw.lower() in description for kw in keywords):
            return False

    # name_contains: at least one keyword must appear in column name
    if "name_contains" in match_criteria:
        keywords = match_criteria["name_contains"]
        if not any(kw.lower() in col_name for kw in keywords):
            return False

    # data_type: column's type must prefix-match one of the listed types
    if "data_type" in match_criteria:
        type_list = [t.lower() for t in match_criteria["data_type"]]
        dt_base = data_type.split("(")[0].strip()
        if not any(dt_base.startswith(t) or t.startswith(dt_base) for t in type_list):
            return False

    return True


# ── Cypher generation ────────────────────────────────────────────────────────

def generate_rule_cypher(col: dict, rule_def: dict, ref_dir: str, rule_index: int) -> list[str]:
    """Generate Cypher for a single domain rule on a column."""
    schema_table = f"{col['schema']}.{col['table_name']}"
    col_name = col["col_name"]
    col_uri = col["col_uri"]
    shape_uri = col.get("shape_uri") or f"shape:{schema_table}"
    rule_spec = rule_def.get("rule", {})
    rule_type = rule_spec.get("type", "range")
    severity = rule_spec.get("severity", "sh:Warning")
    rationale = rule_spec.get("rationale", "")
    source_file = rule_def.get("_source_file", "unknown")
    source_index = rule_def.get("_source_index", 0)

    uri = f"rule:{schema_table}.{col_name}.domain.{rule_type}.{rule_index}"

    # Handle "today" special value
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    props = [
        f"uri: '{esc(uri)}'",
        f"ruleType: '{rule_type}'",
        f"path: '{esc(col_name)}'",
        f"severity: '{severity}'",
        f"confidence: 1.0",
        f"ruleSource: 'domain'",
        f"status: 'pending_review'",
        f"domainRuleRef: '{esc(source_file)}#rule-{source_index}'",
    ]

    if rule_type == "range":
        min_val = rule_spec.get("min")
        max_val = rule_spec.get("max")
        if str(min_val) == "today":
            min_val = today
        if str(max_val) == "today":
            max_val = today

        desc = f"{col_name} should be between {min_val} and {max_val} ({rationale})"
        props.append(f"description: '{esc(desc)}'")
        props.append(f"minInclusive: {cypher_value(min_val)}")
        props.append(f"maxInclusive: {cypher_value(max_val)}")

    elif rule_type == "allowedValues":
        values = rule_spec.get("values", [])
        if rule_spec.get("values_file"):
            values = load_values_file(ref_dir, rule_spec["values_file"])
        desc = f"{col_name} should be one of {len(values)} allowed values ({rationale})"
        props.append(f"description: '{esc(desc)}'")
        props.append(f"evidenceValueCount: {len(values)}")

    elif rule_type == "pattern":
        regex = rule_spec.get("regex", "")
        desc = f"{col_name} should match pattern: {regex} ({rationale})"
        props.append(f"description: '{esc(desc)}'")
        props.append(f"pattern: '{esc(regex)}'")

    else:
        desc = f"{col_name}: {rationale}"
        props.append(f"description: '{esc(desc)}'")

    props_str = ", ".join(props)

    lines = [
        f"// Domain rule: {col_name} — {rule_type} (from {source_file})",
        f"MATCH (ns:NodeShape {{uri: '{esc(shape_uri)}'}})",
        f"MATCH (col:Column {{uri: '{esc(col_uri)}'}})",
        f"CREATE (ps:PropertyShape {{{props_str}}})",
        f"CREATE (ns)-[:PROPERTY]->(ps)",
        f"CREATE (ps)-[:ON_COLUMN]->(col);",
        "",
    ]
    return lines


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate domain-aware DQ rules from reference files.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--domain", required=True, help="Domain name (e.g., 'hr', 'customer')")
    parser.add_argument("--project-code", default=None, help="Project code to scope queries to")
    parser.add_argument("--reference-dir", required=True, help="Path to reference directory")
    parser.add_argument("--extra-rules", default=None, help="JSON array of additional rules from guidance")
    parser.add_argument("--append", action="store_true", help="Append to existing Cypher file")
    args = parser.parse_args()

    # Normalize domain name for file lookup
    domain = args.domain.lower().replace(" ", "_").replace("-", "_")
    # Map common domain names to reference file names
    domain_aliases = {
        "human_resources": "hr",
        "human resources": "hr",
    }
    domain_key = domain_aliases.get(domain, domain)

    print(f"Loading reference rules for domain '{domain_key}'...")
    ref_rules = load_reference_rules(args.reference_dir, domain_key)

    if not ref_rules and not args.extra_rules:
        print("No reference rules found and no extra rules provided.")
        print("Create reference files in the reference directory or provide --extra-rules.")
        sys.exit(0)

    # Connect to Neo4j and fetch columns
    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    print(f"\nConnecting to {bolt_uri}...")

    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        driver.verify_connectivity()
    except (ServiceUnavailable, AuthError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    pc = getattr(args, "project_code", None)
    with driver.session(database=args.database) as session:
        if pc:
            print(f"Scoping to project: {pc}")
            columns = [dict(r) for r in session.run(COLUMNS_WITH_DESCRIPTIONS_SCOPED, project_code=pc)]
        else:
            columns = [dict(r) for r in session.run(COLUMNS_WITH_DESCRIPTIONS)]
    driver.close()

    if not columns:
        print("No columns found in the graph. Run data-discovery first.")
        sys.exit(0)

    print(f"Found {len(columns)} column(s) across {len(set(c['dataset_uri'] for c in columns))} table(s)")

    # Match reference rules to columns
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cypher_lines = [
        "// Domain-Aware Data Quality Rules",
        "// Generated by the domain-rule-enhancement skill",
        f"// Generated at: {now.isoformat()}",
        f"// Domain: {domain_key}",
        f"// Reference files: common.yaml, {domain_key}.yaml",
        "",
    ]

    stats = {"matched": 0, "rules_generated": 0, "columns_without_match": 0, "extra_rules": 0}
    matched_columns = set()
    rule_counter = 0

    for col in columns:
        col_matched = False
        for rule_def in ref_rules:
            match_criteria = rule_def.get("match", {})
            if matches_rule(col, match_criteria):
                rule_lines = generate_rule_cypher(col, rule_def, args.reference_dir, rule_counter)
                cypher_lines.extend(rule_lines)
                rule_counter += 1
                stats["rules_generated"] += 1
                col_matched = True

        if col_matched:
            matched_columns.add(col["col_uri"])
            stats["matched"] += 1

    stats["columns_without_match"] = len(columns) - len(matched_columns)

    # Handle extra rules from guidance interpretation
    if args.extra_rules:
        try:
            extras = json.loads(args.extra_rules)
            cypher_lines.append("// === Extra rules from domain guidance ===")
            cypher_lines.append("")
            for extra in extras:
                col_uri = extra.get("col_uri")
                col = next((c for c in columns if c["col_uri"] == col_uri), None)
                if not col:
                    print(f"  WARNING: Column not found for extra rule: {col_uri}")
                    continue
                rule_def = {
                    "rule": {
                        "type": extra.get("rule_type", "range"),
                        "severity": extra.get("severity", "sh:Warning"),
                        "rationale": extra.get("rationale", "From domain guidance"),
                    },
                    "_source_file": "guidance",
                    "_source_index": stats["extra_rules"],
                }
                if "min" in extra:
                    rule_def["rule"]["min"] = extra["min"]
                if "max" in extra:
                    rule_def["rule"]["max"] = extra["max"]
                if "values" in extra:
                    rule_def["rule"]["values"] = extra["values"]
                    rule_def["rule"]["type"] = "allowedValues"

                rule_lines = generate_rule_cypher(col, rule_def, args.reference_dir, rule_counter)
                cypher_lines.extend(rule_lines)
                rule_counter += 1
                stats["extra_rules"] += 1
                stats["rules_generated"] += 1
        except (json.JSONDecodeError, TypeError) as e:
            print(f"  WARNING: Could not parse --extra-rules: {e}")

    # Write output
    cypher_dir = os.path.join(os.getcwd(), "cypher_scripts")
    os.makedirs(cypher_dir, exist_ok=True)
    output_file = os.path.join(cypher_dir, "domain_rules.cypher")

    mode = "a" if args.append else "w"
    with open(output_file, mode) as f:
        if args.append:
            f.write("\n")
        f.write("\n".join(cypher_lines))

    print(f"\n{'Appended to' if args.append else 'Generated'}: {output_file}")
    print(f"  Columns matched:          {stats['matched']}")
    print(f"  Rules from reference:      {stats['rules_generated'] - stats['extra_rules']}")
    print(f"  Rules from guidance:       {stats['extra_rules']}")
    print(f"  Total rules generated:     {stats['rules_generated']}")
    print(f"  Columns without match:     {stats['columns_without_match']}")


if __name__ == "__main__":
    main()
