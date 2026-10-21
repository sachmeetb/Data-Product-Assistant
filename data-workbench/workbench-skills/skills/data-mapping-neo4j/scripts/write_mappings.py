#!/usr/bin/env python3
"""
Write :ColumnMapping nodes to Neo4j from a JSON mappings file,
with W3C PROV-O provenance.

Supports both:
  - Direct mappings (one source column -> one product column)
  - Derived mappings (multiple source columns + transform expression -> one product column)

Each entry can carry a structured transform contract:
  transform_kind:        direct | cast | format | concat | split | substring |
                         case | arithmetic | lookup | literal | expression |
                         bucket | mask | hash
  transform_expression:  SQL fragment, source of truth for view DDL generation
  transform_inputs:      ordered list of source column URIs referenced by the expression
  transform_params:      JSON-serialisable kind-specific parameters (separator, format, ...)
  transform_decorators:  JSON-serialisable decorators (standardization, default_if_null, ...)
  transform_author:      po_hint | ai_suggestion | engineer | steward_catalog
  transform_confidence:  optional float (only meaningful for ai_suggestion / steward_catalog)
  expression_dialect:    SQL dialect a raw transform_expression is authored in
                         (default 'postgres'); the capability validator parses
                         with this grammar. See docs/architecture/transform-portability.md.
  transform_schema_version: neutral-DSL revision stamp (default 'v1').

Skip semantics:
  A mapping is skipped if its target :DProdColumn already has a current mapping.
  Same source column may feed multiple derived mappings (no skip on source-side).

Input JSON format (one of two shapes per entry):

  Direct (legacy single-source):
    {
      "source_column_uri":   "column:hr.employees.id",
      "product_column_uri":  "dprod:my_product:column:employee_id",
      "similarity_score":    0.97,
      "rationale":           "PK match"
    }

  Derived (multi-source + transform):
    {
      "source_column_uris":  ["column:hr.employees.first_name", "column:hr.employees.last_name"],
      "product_column_uri":  "dprod:my_product:column:full_name",
      "similarity_score":    0.92,
      "rationale":           "Concatenate first + last for full_name",
      "transform_kind":      "concat",
      "transform_expression":"first_name || ' ' || last_name",
      "transform_inputs":    ["column:hr.employees.first_name", "column:hr.employees.last_name"],
      "transform_params":    {"separator": " "},
      "transform_decorators":{"standardization": ["trim"]},
      "transform_author":    "ai_suggestion",
      "transform_confidence":0.85
    }

Usage:
    python write_mappings.py <mappings_file> [--project-code <code>] [other neo4j options]
"""

import sys
import os
import argparse
import json
from datetime import datetime, timezone
from hashlib import md5

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

AI_AGENT_URI = "prov:agent:ai:data-mapping-neo4j-skill"

VALID_TRANSFORM_KINDS = {
    "direct", "cast", "format", "concat", "split", "substring",
    "case", "arithmetic", "lookup", "literal", "expression",
    # Phase 1 of implementingdatatransformations.md — privacy + discretization
    # column-level kinds. bucket = continuous-to-bands; mask = format-preserving
    # redaction; hash = irreversible digest. tokenize is intentionally deferred
    # (needs vault infrastructure).
    "bucket", "mask", "hash",
    # Phase 6 — window function. References a named window declared in
    # :DatasetTransform.window_specs via transformParams.window. Compiles
    # to `<FUNCTION>(<arg>) OVER (<window>)` at view-DDL time.
    "window",
    # Phase 7 of transform-portability.md — neutral date_difference op with an
    # explicit semantics discriminator (completed_units | boundary_count |
    # symbolic_interval). Supersedes hand-written AGE()/DATEDIFF() so the same
    # mapping renders correctly on every served platform. transformParams:
    # {unit: 'year', semantics: '...'}; inputs are [start, end] (single input
    # pairs with CURRENT_DATE — the age-from-dob case).
    "date_difference",
}
VALID_TRANSFORM_AUTHORS = {
    "po_hint", "ai_suggestion", "engineer", "steward_catalog",
}
# Phase 3 of implementingdatatransformations.md — column-level aggregation
# function. Lights up only when the product :DatasetTransform has grouping_keys
# set; ignored otherwise. COUNT_DISTINCT is a portable shorthand lowered to
# COUNT(DISTINCT x) by the renderer.
_VALID_AGGREGATE_FUNCTIONS = {
    "SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT", "FIRST", "LAST",
}


# ── URI helpers ────────────────────────────────────────────────────────────────

def _bare_source(uri: str) -> str:
    """'column:employees.employee.id' → 'employees.employee.id'"""
    return uri.replace("column:", "")


def _bare_product(uri: str) -> str:
    """'dprod:employee_product:column:employee_key' → 'employee_product.employee_key'"""
    return uri.replace("dprod:", "").replace(":column:", ".")


def _mapping_uri(project_code, source_uris, product_uri):
    """
    Build a stable, unique mapping URI.

    Single-source: mapping:{project_code}:{src_bare}:{tgt_bare}
    Multi-source:  mapping:{project_code}:{src0_bare}+{hash8}:{tgt_bare}
                   (hash disambiguates same-target mappings with different source sets)
    Literal (no sources): mapping:{project_code}:literal:{tgt_bare}
    """
    tgt = _bare_product(product_uri)
    if not source_uris:
        if project_code:
            return f"mapping:{project_code}:literal:{tgt}"
        return f"mapping:literal:{tgt}"
    primary_src = _bare_source(source_uris[0])
    if len(source_uris) > 1:
        digest = md5("|".join(sorted(source_uris)).encode()).hexdigest()[:8]
        primary_src = f"{primary_src}+{digest}"
    if project_code:
        return f"mapping:{project_code}:{primary_src}:{tgt}"
    return f"mapping:{primary_src}:{tgt}"


def _activity_uri(mapping_uri, timestamp, suffix="gen"):
    bare = mapping_uri.replace("mapping:", "")
    return f"prov:activity:mapping:{bare}:{suffix}:{timestamp}"


# ── Entry normalisation ────────────────────────────────────────────────────────

def _normalize_entry(entry):
    """
    Coerce single- and multi-source entries into a uniform shape.
    Returns None if required fields are missing.

    Literal mappings (transform_kind='literal') represent constant values
    and are allowed to have zero source columns.
    """
    declared_kind = (entry.get("transform_kind") or entry.get("mapping_type") or "").strip().lower()
    is_literal = declared_kind == "literal"

    if "source_column_uris" in entry and entry["source_column_uris"]:
        sources = list(entry["source_column_uris"])
    elif "source_column_uri" in entry and entry["source_column_uri"]:
        sources = [entry["source_column_uri"]]
    else:
        sources = []

    sources = [s.strip() for s in sources if s and s.strip()]
    if not sources and not is_literal:
        return None

    target = (entry.get("product_column_uri") or "").strip()
    if not target:
        return None

    score = entry.get("similarity_score")
    rationale = (entry.get("rationale") or "").strip()

    # Resolve transform_kind: explicit > legacy mapping_type > inferred from arity
    transform_kind = entry.get("transform_kind") or entry.get("mapping_type")
    if transform_kind:
        transform_kind = transform_kind.strip().lower()
    else:
        transform_kind = "direct" if len(sources) == 1 else "concat"
    # Map legacy 'derived' marker
    if transform_kind == "derived":
        transform_kind = "concat" if len(sources) > 1 else "expression"
    if transform_kind not in VALID_TRANSFORM_KINDS:
        transform_kind = "expression"

    transform_expression = entry.get("transform_expression") or ""
    transform_inputs = entry.get("transform_inputs") or sources
    transform_params = entry.get("transform_params") or {}
    transform_decorators = entry.get("transform_decorators") or {}

    transform_author = (entry.get("transform_author") or "ai_suggestion").strip().lower()
    if transform_author not in VALID_TRANSFORM_AUTHORS:
        transform_author = "ai_suggestion"

    transform_confidence = entry.get("transform_confidence")

    # Phase 3 of implementingdatatransformations.md — aggregation. Mappings
    # whose product dataset has a grouping_keys block must either reference
    # one of those keys (grouping_key=true) or carry an aggregate_function.
    # Both fields are optional and ignored at view-DDL time when the dataset
    # has no grouping_keys, so existing mappings continue to work unchanged.
    aggregate_function = (entry.get("aggregate_function") or "").strip().upper()
    if aggregate_function and aggregate_function not in _VALID_AGGREGATE_FUNCTIONS:
        aggregate_function = ""
    grouping_key = bool(entry.get("grouping_key", False))

    # Phase 4 of transform-portability.md: a raw transformExpression is authored
    # in a specific SQL dialect — legacy expressions are Postgres. Stamped so the
    # capability validator parses with the correct grammar instead of misreading
    # it as neutral. transformSchemaVersion marks the neutral-DSL revision.
    expression_dialect = (entry.get("expression_dialect") or "postgres").strip().lower()
    transform_schema_version = (entry.get("transform_schema_version") or "v1").strip().lower()

    return {
        "sources": sources,
        "target": target,
        "score": score,
        "rationale": rationale,
        "transform_kind": transform_kind,
        "transform_expression": transform_expression,
        "transform_inputs": transform_inputs,
        "transform_params": transform_params,
        "transform_decorators": transform_decorators,
        "transform_author": transform_author,
        "transform_confidence": transform_confidence,
        "aggregate_function": aggregate_function,
        "grouping_key": grouping_key,
        "expression_dialect": expression_dialect,
        "transform_schema_version": transform_schema_version,
    }


# ── Write logic ────────────────────────────────────────────────────────────────

def _source_label(uri: str) -> str:
    """Pick the Neo4j label to MATCH for a source URI.

    Consumer-aligned products mapping from another product's columns use
    DProdColumn URIs (prefix 'dprod:col:'); legacy / source-aligned flows
    map from raw :Column URIs (prefix 'column:'). literal-kind mappings
    have no source URIs at all so we never reach this resolver for them.
    """
    return "DProdColumn" if (uri or "").startswith("dprod:col:") else "Column"


def _strip_lookup_table(raw):
    """'schema.vw_order_header' -> ('schema', 'order_header').

    Returns (schema_or_None, bare_physical_name). A 'schema.' qualifier and a
    'vw_' author override are both stripped so the bare name matches
    :Dataset.name / :DProdOutputDataset.physicalName.
    """
    name = (raw or "").strip()
    schema = None
    if "." in name:
        schema, name = name.rsplit(".", 1)
    if name.startswith("vw_"):
        name = name[len("vw_"):]
    return (schema or None), name


# Resolve a lookup table/column to a graph node, mirroring the backend
# reconcile in workbench/backend/lookup_via.py — keep the two in sync.
# Case-insensitive via searchName (toLower of the name) so an authored lookup
# reference whose casing differs from the discovered identifier still resolves
# (Tier 7). Unquoted identifiers are the supported convention.
_RESOLVE_CATALOG_LOOKUP = """\
MATCH (cat:Catalog)-[:DCAT_DATASET]->(ds:Dataset)-[:HAS_COLUMN]->(col:Column)
WHERE cat.uri STARTS WITH $catalog_prefix
  AND coalesce(ds.searchName, toLower(ds.name)) = toLower($table)
  AND ($schema IS NULL OR toLower(ds.schema) = toLower($schema))
  AND coalesce(col.searchName, toLower(col.name)) = toLower($col_name)
RETURN collect(DISTINCT col.uri) AS uris
"""
_RESOLVE_DPROD_LOOKUP = """\
MATCH (dc:DataContract {id: $contract_id})-[:CONSUMES]->(:DProdDataProduct)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(c:DProdColumn)
WHERE toLower(ods.physicalName) = toLower($table)
  AND coalesce(c.searchName, toLower(c.name)) = toLower($col_name)
RETURN collect(DISTINCT c.uri) AS uris
"""
_MERGE_LOOKUP_VIA = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
MATCH (n {uri: $target_uri})
WHERE n:Column OR n:DProdColumn
MERGE (cm)-[r:LOOKUP_VIA]->(n)
SET r.role = $role, r.strategy = $strategy, r.createdBy = 'write_mappings'
"""


def _write_lookup_via_edges(session, project_code, mapping_uri, sources, params):
    """Materialise :LOOKUP_VIA edges for a lookup-kind mapping so lineage can
    surface the reference table the lookup reads from (often a different
    CONSUMES'd source product than the primary source column).

    Best-effort: an unresolvable bare name (undiscovered reference table, or an
    ambiguous match) is silently skipped — the transformParams JSON remains the
    source of truth for the view DDL regardless.
    """
    lookup_table = params.get("lookup_table")
    if not lookup_table:
        return
    strategy = (params.get("selection_strategy") or "equi").strip().lower()
    is_dprod = any((s or "").startswith("dprod:col:") for s in sources)
    schema, table = _strip_lookup_table(lookup_table)
    contract_id = f"{project_code}-contract" if project_code else None
    catalog_prefix = f"catalog:{project_code}:" if project_code else "catalog:"

    for role, col_name in (("value", params.get("value_column")), ("key", params.get("key_column"))):
        col_name = (col_name or "").strip()
        if not col_name:
            continue
        if is_dprod:
            if not contract_id:
                continue
            rec = session.run(
                _RESOLVE_DPROD_LOOKUP,
                contract_id=contract_id, table=table, col_name=col_name,
            ).single()
        else:
            rec = session.run(
                _RESOLVE_CATALOG_LOOKUP,
                catalog_prefix=catalog_prefix, table=table, schema=schema, col_name=col_name,
            ).single()
        uris = (rec or {}).get("uris") or []
        if len(uris) != 1:
            continue
        session.run(
            _MERGE_LOOKUP_VIA,
            mapping_uri=mapping_uri, target_uri=uris[0], role=role, strategy=strategy,
        )


def write_one_mapping(session, project_code, entry, timestamp, dry_run):
    """
    Returns one of: 'created', 'skipped', 'dry_run', 'error:<reason>'.
    """
    sources = entry["sources"]
    target = entry["target"]

    # All source columns must exist. Source can be :Column (catalog-mode
    # source-aligned mapping) or :DProdColumn (consumer-aligned mapping
    # from a CONSUMES'd source product). Pick the label by URI prefix.
    for src in sources:
        label = _source_label(src)
        r = session.run(
            f"MATCH (n:{label} {{uri: $uri}}) RETURN count(n) AS n",
            uri=src,
        )
        if r.single()["n"] == 0:
            return f"error:source_column_not_found:{src}"

    # Target product column must exist
    r = session.run("MATCH (c:DProdColumn {uri: $uri}) RETURN count(c) AS n", uri=target)
    if r.single()["n"] == 0:
        return "error:product_column_not_found"

    # Skip if target already has a current mapping
    r = session.run(
        """
        MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn {uri: $uri})
        WHERE cm.isCurrent = true
        RETURN count(cm) AS n
        """,
        uri=target,
    )
    if r.single()["n"] > 0:
        return "skipped"

    if dry_run:
        return "dry_run"

    mapping_uri = _mapping_uri(project_code, sources, target)
    activity_uri = _activity_uri(mapping_uri, timestamp, suffix="gen")
    legacy_mapping_type = "direct" if entry["transform_kind"] == "direct" else "derived"

    inputs_json = json.dumps(entry["transform_inputs"])
    params_json = json.dumps(entry["transform_params"]) if entry["transform_params"] else "{}"
    decorators_json = json.dumps(entry["transform_decorators"]) if entry["transform_decorators"] else "{}"

    similarity = float(entry["score"]) if entry["score"] is not None else None
    confidence = float(entry["transform_confidence"]) if entry["transform_confidence"] is not None else None

    try:
        # Step 1: create the mapping node + provenance + target edge
        session.run(
            """
            MATCH (pc:DProdColumn {uri: $product_col_uri})
            CREATE (cm:ColumnMapping {
                uri:                       $mapping_uri,
                status:                    'pending_review',
                isCurrent:                 true,
                similarityScore:           $similarity_score,
                rationale:                 $rationale,
                mappingType:               $legacy_mapping_type,
                transformKind:             $transform_kind,
                transformExpression:       $transform_expression,
                transformInputs:           $transform_inputs_json,
                transformParams:           $transform_params_json,
                transformDecorators:       $transform_decorators_json,
                transformAuthor:           $transform_author,
                transformConfidence:       $transform_confidence,
                transformEscalationReason: null,
                aggregateFunction:         $aggregate_function,
                groupingKey:               $grouping_key,
                expressionDialect:         $expression_dialect,
                transformSchemaVersion:    $transform_schema_version,
                createdAt:                 $timestamp
            })
            CREATE (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
            CREATE (act:ProvActivity {
                uri:          $activity_uri,
                activityType: 'mapping_generation',
                occurredAt:   $timestamp
            })
            MERGE (agent:ProvAgent {uri: $agent_uri})
            ON CREATE SET agent.agentType = 'ai',
                          agent.name      = 'data-mapping-neo4j-skill'
            CREATE (cm)-[:PROV_WAS_GENERATED_BY]->(act)
            CREATE (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent)
            """,
            mapping_uri=mapping_uri,
            product_col_uri=target,
            similarity_score=similarity,
            rationale=entry["rationale"],
            legacy_mapping_type=legacy_mapping_type,
            transform_kind=entry["transform_kind"],
            transform_expression=entry["transform_expression"],
            transform_inputs_json=inputs_json,
            transform_params_json=params_json,
            transform_decorators_json=decorators_json,
            transform_author=entry["transform_author"],
            transform_confidence=confidence,
            aggregate_function=entry.get("aggregate_function") or "",
            grouping_key=bool(entry.get("grouping_key", False)),
            expression_dialect=entry.get("expression_dialect") or "postgres",
            transform_schema_version=entry.get("transform_schema_version") or "v1",
            timestamp=timestamp,
            activity_uri=activity_uri,
            agent_uri=AI_AGENT_URI,
        )

        # Step 2: attach one [:MAPS_SOURCE_COLUMN] edge per source URI.
        # Label is dispatched per-URI so a single mapping with mixed sources
        # (rare, but possible if/when consumer products cite raw columns
        # alongside source-product columns) still works.
        for src_uri in sources:
            label = _source_label(src_uri)
            session.run(
                f"""
                MATCH (cm:ColumnMapping {{uri: $mapping_uri}})
                MATCH (n:{label} {{uri: $source_uri}})
                CREATE (cm)-[:MAPS_SOURCE_COLUMN]->(n)
                """,
                mapping_uri=mapping_uri,
                source_uri=src_uri,
            )

        # Step 3: for lookup-kind mappings, attach :LOOKUP_VIA edges to the
        # reference table's column(s) so lineage surfaces the (often
        # cross-product) lookup source. Best-effort — see _write_lookup_via_edges.
        if entry["transform_kind"] == "lookup":
            _write_lookup_via_edges(
                session, project_code, mapping_uri, sources, entry["transform_params"] or {}
            )
        return "created"
    except ClientError as e:
        return f"error:{e.message}"


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Write :ColumnMapping nodes to Neo4j from a JSON mappings file.",
    )
    parser.add_argument(
        "mappings_file",
        help="JSON file containing a list of mapping entries.",
    )
    parser.add_argument(
        "--project-code",
        default=None,
        help="Project code used to scope the :ColumnMapping URI (recommended).",
    )
    parser.add_argument("--host",      default=DEFAULT_HOST)
    parser.add_argument("--bolt-port", type=int, default=DEFAULT_BOLT_PORT)
    parser.add_argument("--username",  default=DEFAULT_USERNAME)
    parser.add_argument("--password",  default=DEFAULT_PASSWORD)
    parser.add_argument("--database",  default=DEFAULT_DATABASE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without making changes",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.mappings_file):
        print(f"ERROR: File not found: {args.mappings_file}", file=sys.stderr)
        sys.exit(1)

    with open(args.mappings_file) as f:
        mappings = json.load(f)

    if not isinstance(mappings, list):
        print("ERROR: JSON must be a list of mapping objects.", file=sys.stderr)
        sys.exit(1)

    bolt_uri = f"bolt://{args.host}:{args.bolt_port}"
    print(f"Connecting to {bolt_uri} as '{args.username}' (database: {args.database})...")
    if args.project_code:
        print(f"Project code: {args.project_code}")
    else:
        print("WARNING: --project-code not provided; mappings will not be project-scoped.", file=sys.stderr)

    try:
        driver = GraphDatabase.driver(bolt_uri, auth=(args.username, args.password))
        if not args.dry_run:
            driver.verify_connectivity()
    except ServiceUnavailable as e:
        print(f"ERROR: Cannot connect to Neo4j at {bolt_uri}\n  {e}", file=sys.stderr)
        sys.exit(1)
    except AuthError as e:
        print(f"ERROR: Authentication failed for user '{args.username}'\n  {e}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("[DRY-RUN MODE — no changes will be made]\n")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stats = {"created": 0, "skipped": 0, "errors": 0}

    with driver.session(database=args.database) as session:
        for entry in mappings:
            normalized = _normalize_entry(entry)
            if normalized is None:
                print(f"  SKIP (missing source/target): {entry}", file=sys.stderr)
                stats["errors"] += 1
                continue

            sources_label = ",".join(_bare_source(s) for s in normalized["sources"])
            target_label = _bare_product(normalized["target"])
            label = f"{sources_label} -> {target_label} [{normalized['transform_kind']}]"

            outcome = write_one_mapping(
                session, args.project_code, normalized, timestamp, args.dry_run
            )

            score = normalized["score"]
            score_str = f"score={score:.2f}" if score is not None else "score=n/a"

            if outcome == "created":
                print(f"  CREATED  {label}  ({score_str})")
                stats["created"] += 1
            elif outcome == "skipped":
                print(f"  SKIP     {label}  (target already has current mapping)")
                stats["skipped"] += 1
            elif outcome == "dry_run":
                print(f"  [DRY-RUN] Would create: {label}  ({score_str})")
                stats["created"] += 1
            else:
                print(f"  ERROR    {label}  ({outcome})", file=sys.stderr)
                stats["errors"] += 1

    driver.close()

    print(
        f"\nDone.  created={stats['created']}  "
        f"skipped={stats['skipped']}  errors={stats['errors']}"
    )

    if stats["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
