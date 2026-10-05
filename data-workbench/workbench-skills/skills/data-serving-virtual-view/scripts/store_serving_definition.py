#!/usr/bin/env python3
"""Store a serving definition (DDL, mode, platform) in Neo4j linked to the data product.

A data product may have N output datasets and therefore emit N CREATE VIEW
statements concatenated into the DDL file. All N views are stored under one
:ServingDefinition node: `viewName` is the first (back-compat for readers
that project a single name) and `viewNames` is the JSON-encoded list.
"""

import argparse
import json
import re
import sys

from neo4j import GraphDatabase

STORE_SERVING = """\
MATCH (dp:DProdDataProduct {uri: $product_uri})
MERGE (dp)-[:SERVED_BY]->(sd:ServingDefinition {productUri: $product_uri, servingMode: $serving_mode})
SET sd.targetPlatform = $target_platform,
    sd.viewName = $view_name,
    sd.viewNames = $view_names_json,
    sd.viewCount = $view_count,
    sd.viewSchema = $view_schema,
    sd.ddl = $ddl,
    sd.summaryJson = $summary_json,
    sd.createdAt = datetime()
RETURN sd.viewName AS view_name, sd.viewCount AS view_count, sd.servingMode AS serving_mode
"""


# Anchor to start-of-line so `CREATE VIEW` appearing inside `--` comments
# (e.g. the header comment generate_view_ddl.py writes when emitting multiple
# views) doesn't get scooped up as a real CREATE statement. The DDL emitter
# always puts the actual statement at column 0; comment lines start with `--`.
#
# The captured identifier list is PER-PLATFORM QUOTED: double-quotes for
# Postgres/Snowflake (`"db"."schema"."vw"`), backticks for Databricks/MySQL
# (`` `cat`.`schema`.`vw` ``), or bare (`schema.vw`). The char class therefore
# includes both quote chars + `.` + word chars; `_split_qualified` then strips
# the quotes properly (a naive `.strip('"').rsplit('.')` mangles a multi-part
# quoted name into e.g. `"vw`, which then gets re-quoted downstream).
_CREATE_VIEW_RE = re.compile(
    r'^\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.`"]+)',
    re.IGNORECASE | re.MULTILINE,
)


def _split_qualified(ref):
    """Split a possibly-quoted dotted identifier into clean (unquoted) parts.

    Handles `"a"."b"."c"` (double-quote), `` `a`.`b`.`c` `` (backtick) and `a.b.c`
    (bare). A `.` inside a quoted segment is treated as data, not a separator.
    """
    parts, buf, quote = [], [], None
    for ch in ref:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in ('"', '`'):
            quote = ch
        elif ch == '.':
            parts.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p for p in parts if p != ""]


def extract_view_names(ddl_text):
    """Return [(qualified, bare), ...] for every CREATE [OR REPLACE] VIEW in ddl_text.
    `qualified` is the unquoted dotted name; `bare` is the relation (last part)."""
    out = []
    for match in _CREATE_VIEW_RE.finditer(ddl_text):
        parts = _split_qualified(match.group(1).strip())
        if not parts:
            continue
        out.append((".".join(parts), parts[-1]))
    return out


def store_definition(driver, database, product_uri, ddl, view_name, view_names,
                     view_schema, platform, summary_json=""):
    """Store serving definition in Neo4j.

    ``summary_json`` is the JSON string of the structured summary written by
    generate_view_ddl.py — contains per-view multiplication_warnings,
    auto_bridge_choice, scd_warning, underspecified_aggregates so the UI can
    surface them on the serving cards. Persisted verbatim on
    :ServingDefinition.summaryJson; pass "" when not available (older
    invocations / dry-run paths)."""
    with driver.session(database=database) as session:
        result = session.run(
            STORE_SERVING,
            product_uri=product_uri,
            serving_mode="virtual_view",
            target_platform=platform,
            view_name=view_name,
            view_names_json=json.dumps(view_names),
            view_count=len(view_names),
            view_schema=view_schema,
            ddl=ddl,
            summary_json=summary_json,
        ).single()
        return dict(result) if result else None


def main():
    parser = argparse.ArgumentParser(description="Store serving definition in Neo4j")
    parser.add_argument("--product-uri", required=True, help="Data product URI")
    parser.add_argument("--ddl-file", required=True, help="Path to SQL DDL file")
    parser.add_argument("--summary-file", default=None,
                        help="Path to summary JSON sidecar (default: <ddl-file>.summary.json if present).")
    parser.add_argument("--view-name", default=None,
                        help="Primary view name (default: auto-extracted from DDL).")
    parser.add_argument("--view-schema", default="public", help="View schema (default: public)")
    parser.add_argument("--platform", default="postgresql", help="Target platform (default: postgresql)")
    parser.add_argument("--host", default="localhost", help="Neo4j host")
    parser.add_argument("--bolt-port", type=int, default=7687, help="Neo4j Bolt port")
    parser.add_argument("--username", default="neo4j", help="Neo4j username")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Neo4j database")
    parser.add_argument("--dry-run", action="store_true", help="Print query without executing")
    args = parser.parse_args()

    with open(args.ddl_file) as f:
        ddl = f.read()

    extracted = extract_view_names(ddl)
    if not extracted:
        print("Error: no CREATE VIEW statements found in DDL file", file=sys.stderr)
        sys.exit(1)

    bare_names = [bare for _, bare in extracted]
    primary_view_name = args.view_name or extracted[0][1]

    # Auto-locate the sidecar summary if not explicitly passed. generate_view_ddl.py
    # writes `<ddl-file>.summary.json` next to the DDL — pick it up so the
    # structured warnings land on :ServingDefinition without a second wiring step.
    import os
    summary_path = args.summary_file or (args.ddl_file + ".summary.json")
    summary_json = ""
    if os.path.exists(summary_path):
        with open(summary_path) as f:
            summary_json = f.read().strip()
        print(f"Loaded summary sidecar: {summary_path} ({len(summary_json)} chars)")
    else:
        print(f"No summary sidecar at {summary_path} — proceeding without it")

    # Phase 7: if the sidecar carries a dialect name, prefer it over the
    # --platform arg. generate_view_ddl.py emits dialect-specific SQL based
    # on --dialect; persisting the same name on :ServingDefinition.targetPlatform
    # keeps the read side honest about what flavor of SQL the DDL is.
    effective_platform = args.platform
    if summary_json:
        try:
            from_summary = json.loads(summary_json).get("dialect")
            if isinstance(from_summary, str) and from_summary:
                effective_platform = from_summary
                print(f"Dialect from sidecar: {effective_platform} (overrides --platform)")
        except (json.JSONDecodeError, AttributeError):
            pass

    if args.dry_run:
        print(f"Product URI: {args.product_uri}")
        print(f"Primary view: {args.view_schema}.{primary_view_name}")
        print(f"All views ({len(bare_names)}): {bare_names}")
        print(f"DDL length: {len(ddl)} chars")
        print(f"Platform: {effective_platform}")
        print(f"\n-- STORE_SERVING\n{STORE_SERVING.strip()}")
        return

    uri = f"bolt://{args.host}:{args.bolt_port}"
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))

    try:
        result = store_definition(driver, args.database, args.product_uri, ddl,
                                   primary_view_name, bare_names,
                                   args.view_schema, effective_platform,
                                   summary_json=summary_json)
        if result:
            print(f"Stored serving definition: {result['view_name']} "
                  f"({result['view_count']} view(s), {result['serving_mode']})")
        else:
            print("Error: data product not found", file=sys.stderr)
            sys.exit(1)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
