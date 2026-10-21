"""Run a Cypher query against Neo4j, scoped to one project.

The caller must pass --project-code. The script refuses any query where
that code does not appear in the query text or in a parameter value,
which is the safety net that prevents cross-project answers.

Results are printed as JSON to stdout. Errors go to stderr with a
non-zero exit code.

Usage:
    python run_cypher.py \\
        --project-code dpe-04222026-01 \\
        --host localhost --port 7687 \\
        --user neo4j --password secret --database neo4j \\
        --query "MATCH (:Project {projectCode: 'dpe-04222026-01'})-[:HAS_CATALOG]->(c:Catalog) RETURN c.name"

With parameters:
    python run_cypher.py ... --query "MATCH (:Project {projectCode: $pc}) RETURN count(*)" \\
        --params '{"pc": "dpe-04222026-01"}'
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _value_contains(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_value_contains(v, needle) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_value_contains(v, needle) for v in value)
    return False


def _neo4j_value_to_python(value: Any) -> Any:
    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes, dict)):
        try:
            return [_neo4j_value_to_python(v) for v in value]
        except TypeError:
            pass
    if hasattr(value, "items"):
        try:
            return {k: _neo4j_value_to_python(v) for k, v in value.items()}
        except Exception:
            pass
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Project-scoped Cypher runner.")
    parser.add_argument("--project-code", required=True,
                        help="The project code; must appear in the query or params.")
    parser.add_argument("--query", required=True, help="Cypher query text.")
    parser.add_argument("--params", default=None,
                        help="JSON object of query parameters.")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7687)
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--limit", type=int, default=200,
                        help="Row cap applied to results returned to stdout.")
    args = parser.parse_args()

    params: dict = {}
    if args.params:
        try:
            params = json.loads(args.params)
            if not isinstance(params, dict):
                raise ValueError("--params must be a JSON object")
        except Exception as e:
            print(f"ERROR: could not parse --params: {e}", file=sys.stderr)
            return 2

    code = args.project_code.strip()
    if not code:
        print("ERROR: --project-code is required and must be non-empty.",
              file=sys.stderr)
        return 2

    query_has_code = code in args.query
    params_have_code = _value_contains(params, code)
    if not (query_has_code or params_have_code):
        print(
            f"ERROR: refusing to run query — project code '{code}' does not "
            "appear in the query text or in parameter values. Every query must "
            "be scoped to this project. Include `projectCode: '<code>'` in a "
            "MATCH, or pass the code via --params.",
            file=sys.stderr,
        )
        return 3

    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("ERROR: neo4j driver not installed in this environment.",
              file=sys.stderr)
        return 4

    uri = f"bolt://{args.host}:{args.port}"
    driver = GraphDatabase.driver(uri, auth=(args.user, args.password))
    rows: list[dict] = []
    try:
        with driver.session(database=args.database) as session:
            result = session.run(args.query, **params)
            for i, record in enumerate(result):
                if i >= args.limit:
                    break
                rows.append({k: _neo4j_value_to_python(v)
                             for k, v in record.items()})
    except Exception as e:
        print(f"ERROR: query failed: {e}", file=sys.stderr)
        return 5
    finally:
        driver.close()

    out = {
        "project_code": code,
        "row_count": len(rows),
        "truncated": len(rows) >= args.limit,
        "rows": rows,
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
