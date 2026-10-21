#!/usr/bin/env python3
"""Recommend standardized snake_case + domain-prefixed physical names for
every :Column node in a project's Neo4j graph.

Writes:
    col.recommendedName       = <standardized name>
    col.recommendedNameStatus = 'pending_review'

Idempotent. Skips columns whose existing recommendedName already matches.

See SKILL.md for the naming algorithm and abbreviation dictionary.
"""

import argparse
import re
import sys

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import ServiceUnavailable, AuthError, ClientError
except ImportError:
    print("ERROR: neo4j driver is required. Install with: pip install neo4j", file=sys.stderr)
    sys.exit(1)


DEFAULT_HOST = "localhost"
DEFAULT_BOLT_PORT = 7687
DEFAULT_USERNAME = "neo4j"
DEFAULT_PASSWORD = "your_password"
DEFAULT_DATABASE = "neo4j"


# Common abbreviation expansions. Lowercase keys; expansions also lowercase
# (snake_case is applied after).
ABBREVIATIONS: dict[str, str] = {
    "id": "id",
    "cust": "customer",
    "qty": "quantity",
    "addr": "address",
    "pmt": "payment",
    "pmnt": "payment",
    "ord": "order",
    "prod": "product",
    "inv": "invoice",
    "acct": "account",
    "amt": "amount",
    "dt": "date",
    "dttm": "date",
    "desc": "description",
    "nbr": "number",
    "num": "number",
    "tx": "transaction",
    "txn": "transaction",
    "ref": "reference",
    "src": "source",
    "tgt": "target",
    "dst": "destination",
    "yr": "year",
    "mth": "month",
    "ts": "timestamp",
    "code": "code",
    "type": "type",
}


def tokenise(raw: str) -> list[str]:
    """Split a column name into lowercase tokens.

    Handles snake_case, kebab-case, camelCase, PascalCase, and ALLCAPS.
    Example: ``CustomerID`` → ``[customer, id]``;
             ``cust_addr_1`` → ``[cust, addr, 1]``.
    """
    if not raw:
        return []
    # Insert a delimiter at every case boundary, then split on the union
    # of delimiters. The intermediate replacements yield a stream of
    # space-separated tokens; .split() collapses runs.
    s = raw
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)        # camel → camel humps
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)     # ALLCAPSWord → ALLCAPS Word
    s = re.sub(r"[_\-\s/.]+", " ", s)                     # underscores/dashes/etc.
    return [t.lower() for t in s.split() if t]


def standardize(raw: str, domain: str | None) -> str:
    """Apply the algorithm: tokenise → expand abbreviations → lowercase
    → optionally prepend the domain prefix → snake_case join."""
    tokens = tokenise(raw)
    if not tokens:
        return raw or ""

    expanded: list[str] = []
    for tok in tokens:
        expanded.append(ABBREVIATIONS.get(tok, tok))

    # Domain prefix: tokenise so a multi-word domain ("retail banking")
    # snake_cases cleanly (retail_banking_...) instead of leaking its internal
    # space into the joined name. Idempotent — skip when the column already
    # leads with the domain tokens.
    if domain:
        dtoks = tokenise(domain)
        if dtoks and expanded[: len(dtoks)] != dtoks:
            expanded = dtoks + expanded

    return "_".join(expanded)


READ_COLUMNS = """\
MATCH (:Project {projectCode: $project_code})
      -[:HAS_CATALOG]->(:Catalog)
      -[:DCAT_DATASET]->(ds:Dataset)
      -[:HAS_COLUMN]->(col:Column)
RETURN
    col.uri               AS uri,
    col.name              AS name,
    col.recommendedName   AS existing_recommendation
ORDER BY ds.schema, ds.name, col.ordinal
"""

WRITE_RECOMMENDATION = """\
MATCH (col:Column {uri: $uri})
SET col.recommendedName       = $recommended,
    col.recommendedNameStatus = coalesce(col.recommendedNameStatus, 'pending_review')
RETURN col.recommendedName AS final
"""


def standardize_project(driver, database: str, project_code: str, domain: str | None) -> dict:
    written = 0
    skipped_uptodate = 0
    errors = 0

    with driver.session(database=database) as session:
        rows = list(session.run(READ_COLUMNS, project_code=project_code))
        if not rows:
            return {"written": 0, "skipped": 0, "errors": 0, "total": 0}

        for r in rows:
            uri = r["uri"]
            source_name = r["name"] or ""
            existing = r["existing_recommendation"] or ""
            recommended = standardize(source_name, domain)
            if existing == recommended and existing != "":
                skipped_uptodate += 1
                continue
            try:
                session.run(WRITE_RECOMMENDATION, uri=uri, recommended=recommended).consume()
                written += 1
            except Exception as exc:
                errors += 1
                print(f"ERROR writing {uri}: {exc}", file=sys.stderr)

    return {
        "written": written,
        "skipped": skipped_uptodate,
        "errors": errors,
        "total": len(rows),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Recommend snake_case + domain-prefixed physical names for :Column nodes."
    )
    parser.add_argument("--project-code", required=True,
                        help="Project code that scopes which :Column nodes to standardize.")
    parser.add_argument("--domain", default=None,
                        help="Optional domain prefix (e.g. 'customer'). Lowercased before use.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    args = parser.parse_args()

    uri = f"bolt://{args.host}:{args.bolt_port}"
    try:
        driver = GraphDatabase.driver(uri, auth=(args.username, args.password))
    except (ServiceUnavailable, AuthError) as exc:
        print(f"ERROR: cannot connect to Neo4j: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        result = standardize_project(driver, args.database, args.project_code, args.domain)
    except ClientError as exc:
        print(f"ERROR: Cypher error: {exc}", file=sys.stderr)
        sys.exit(3)
    finally:
        driver.close()

    print(
        f"Wrote {result['written']} recommendations "
        f"({result['skipped']} already up-to-date, {result['errors']} errors) "
        f"across {result['total']} columns."
    )
    if result["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
