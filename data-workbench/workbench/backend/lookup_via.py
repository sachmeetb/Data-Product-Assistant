"""Materialise ``:LOOKUP_VIA`` edges from ``lookup``-kind column mappings.

A ``lookup`` transform reads a value from a *separate* reference table — for a
consumer-aligned product that reference table often belongs to a DIFFERENT
CONSUMES'd source product than the mapping's primary source column. The lookup
table / columns are stored only as **bare-name strings** inside
``:ColumnMapping.transformParams`` (``lookup_table`` / ``value_column`` /
``key_column``), never as graph edges. Every lineage query traverses only
``:MAPS_SOURCE_COLUMN``, so lookup sources are invisible in the marketplace /
engineer mapping graphs.

This module resolves those bare names back to the concrete ``:Column`` /
``:DProdColumn`` nodes and MERGEs a ``:LOOKUP_VIA`` edge per resolved column so
lineage can surface the real upstream products. The resolution mirrors the
``generate_view_ddl.py:_compile_lookup`` convention (strip a ``schema.`` prefix
and a ``vw_`` author override back to the bare physical name).

Idempotent: every reconcile first deletes the mapping's existing
``:LOOKUP_VIA`` edges, then re-creates them, so re-runs converge. Resolution
that finds no node (or an ambiguous match) is a no-op + a recorded warning —
never an error — so a lookup against an undiscovered reference table doesn't
break the pipeline.
"""

from __future__ import annotations

import json
from typing import Optional


# Every lookup mapping for a project, scoped by the project-prefixed mapping URI.
# (``mapping:{project_code}:...`` — see write_mappings.py:_mapping_uri.) We also
# grab the primary source column so we can tell catalog- from dprod-sourced
# mappings and resolve the lookup table in the matching namespace.
_LOOKUP_MAPPINGS_QUERY = """\
MATCH (cm:ColumnMapping)
WHERE cm.isCurrent = true
  AND cm.transformKind = 'lookup'
  AND cm.uri STARTS WITH $mapping_prefix
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(src)
WITH cm, collect(src.uri)[0] AS primary_src_uri
RETURN cm.uri AS mapping_uri,
       cm.transformParams AS params_json,
       primary_src_uri
"""

# Catalog-mode resolution: a raw :Column under the project's catalog prefix.
# Case-insensitive resolution via searchName (toLower of the name) so an authored
# lookup reference whose casing differs from the discovered identifier still
# resolves — unquoted identifiers are the supported convention (Tier 7).
_RESOLVE_CATALOG_COLUMN = """\
MATCH (cat:Catalog)-[:DCAT_DATASET]->(ds:Dataset)-[:HAS_COLUMN]->(col:Column)
WHERE cat.uri STARTS WITH $catalog_prefix
  AND coalesce(ds.searchName, toLower(ds.name)) = toLower($table)
  AND ($schema IS NULL OR toLower(ds.schema) = toLower($schema))
  AND coalesce(col.searchName, toLower(col.name)) = toLower($col_name)
RETURN collect(DISTINCT col.uri) AS uris
"""

# dprod-mode resolution: a :DProdColumn on a CONSUMES'd source product's output
# dataset, matched by the dataset's physicalName (the bare name before the
# view-DDL vw_<safe_name> rewrite).
_RESOLVE_DPROD_COLUMN = """\
MATCH (dc:DataContract {id: $contract_id})-[:CONSUMES]->(:DProdDataProduct)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(c:DProdColumn)
WHERE toLower(ods.physicalName) = toLower($table)
  AND coalesce(c.searchName, toLower(c.name)) = toLower($col_name)
RETURN collect(DISTINCT c.uri) AS uris
"""

_WIPE_LOOKUP_EDGES = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})-[r:LOOKUP_VIA]->()
DELETE r
"""

# Target is :Column or :DProdColumn — MATCH on uri alone (uris are globally
# unique by prefix) and guard the label so we never attach to a stray node type.
_MERGE_LOOKUP_EDGE = """\
MATCH (cm:ColumnMapping {uri: $mapping_uri})
MATCH (n {uri: $target_uri})
WHERE n:Column OR n:DProdColumn
MERGE (cm)-[r:LOOKUP_VIA]->(n)
SET r.role = $role,
    r.strategy = $strategy,
    r.createdBy = 'reconcile_lookup_via'
"""


def _strip_lookup_table(raw: str) -> tuple[Optional[str], str]:
    """``"schema.vw_order_header"`` → ``("schema", "order_header")``.

    Returns ``(schema_or_None, bare_physical_name)``. Mirrors the author-override
    handling in generate_view_ddl.py:_compile_lookup — a ``schema.`` qualifier and
    a ``vw_`` prefix are both stripped so the bare physical name matches
    :Dataset.name / :DProdOutputDataset.physicalName.
    """
    name = (raw or "").strip()
    schema: Optional[str] = None
    if "." in name:
        schema, name = name.rsplit(".", 1)
    if name.startswith("vw_"):
        name = name[len("vw_"):]
    return (schema or None), name


def merge_lookup_graph_rows(
    lookup_rows: list,
    source_tables: dict,
    seen_source_cols: set,
    mappings: list,
) -> None:
    """Fold lookup-edge rows into an existing mapping-graph payload in place.

    Shared by the marketplace and engineer mapping-graph handlers so both
    render lookup sources identically. ``lookup_rows`` come from a
    ``*_LOOKUP_GRAPH_QUERY`` (one row per ``:LOOKUP_VIA`` edge). Mutates:
      - ``source_tables`` — adds the lookup table/column, tagged ``is_lookup``;
      - ``seen_source_cols`` — so a column isn't duplicated across rows;
      - ``mappings`` — appends one dashed ``relation='lookup_via'`` edge per
        lookup column (synthetic, unique ``uri`` since one mapping can read
        several lookup columns).
    """
    for r in lookup_rows:
        col_uri = r.get("source_col_uri")
        if not col_uri:
            continue
        if not (r.get("source_table_uri") or r.get("source_schema") or r.get("source_table")):
            continue
        tbl_uri = r.get("source_table_uri") or f"{r.get('source_schema')}.{r.get('source_table')}"
        if tbl_uri not in source_tables:
            source_tables[tbl_uri] = {
                "uri": tbl_uri,
                "schema": r.get("source_schema"),
                "table": r.get("source_table"),
                "columns": [],
                "is_lookup": True,
            }
        if col_uri not in seen_source_cols:
            seen_source_cols.add(col_uri)
            source_tables[tbl_uri]["columns"].append({
                "uri": col_uri,
                "name": r.get("source_col_name"),
                "data_type": r.get("source_col_type"),
                "ordinal": r.get("source_col_ordinal"),
                "is_lookup": True,
                "role": r.get("lookup_role"),
            })
        role = r.get("lookup_role") or "value"
        mappings.append({
            "uri": f"{r.get('mapping_uri')}::lookup::{role}::{col_uri}",
            "mapping_uri": r.get("mapping_uri"),
            "status": r.get("status"),
            "source_uri": col_uri,
            "target_uri": r.get("product_col_uri"),
            "transform_kind": r.get("transform_kind"),
            "transform_author": r.get("transform_author"),
            # Carry the mapping's expression + params onto the lookup edge so
            # the canvas tooltip can show HOW the reference table is read
            # (strategy, key/value columns, aggregate function or the raw
            # aggregate_expression, filter clause) — previously the dashed edge
            # said only "lookup (key)" and the aggregation config was invisible.
            "transform_expression": r.get("transform_expression"),
            "transform_params_json": r.get("transform_params_json"),
            "transform_escalation_reason": None,
            "similarity_score": None,
            "literal_value": None,
            "relation": "lookup_via",
            "role": role,
            "strategy": r.get("lookup_strategy"),
        })


def reconcile_lookup_via(
    session,
    project_code: str,
    contract_id: Optional[str] = None,
) -> dict:
    """Rebuild ``:LOOKUP_VIA`` edges for every lookup mapping in a project.

    ``session`` is an open neo4j session (see ``neo4j_client.neo4j_session``).
    ``contract_id`` defaults to ``{project_code}-contract`` (used to scope the
    dprod-mode resolution to the consumer's CONSUMES'd source products).

    Returns ``{"resolved": int, "edges": int, "unresolved": [str, ...]}`` for
    logging. Never raises on an unresolvable name.
    """
    if not project_code:
        return {"resolved": 0, "edges": 0, "unresolved": []}
    contract_id = contract_id or f"{project_code}-contract"
    mapping_prefix = f"mapping:{project_code}:"
    catalog_prefix = f"catalog:{project_code}:"

    rows = list(session.run(_LOOKUP_MAPPINGS_QUERY, mapping_prefix=mapping_prefix))

    resolved = 0
    edges = 0
    unresolved: list[str] = []

    for row in rows:
        mapping_uri = row["mapping_uri"]
        primary_src_uri = row.get("primary_src_uri") or ""
        try:
            params = json.loads(row["params_json"]) if row.get("params_json") else {}
        except (ValueError, TypeError):
            params = {}
        if not isinstance(params, dict):
            params = {}

        lookup_table = params.get("lookup_table")
        if not lookup_table:
            continue
        strategy = (params.get("selection_strategy") or "equi").strip().lower()
        is_dprod = primary_src_uri.startswith("dprod:col:")
        schema, table = _strip_lookup_table(lookup_table)

        # Resolve each role independently. 'exists' has no value_column.
        role_columns = [
            ("value", params.get("value_column")),
            ("key", params.get("key_column")),
        ]

        # Always wipe-then-rebuild so re-runs (and edits that change the lookup
        # table) converge instead of accreting stale edges.
        session.run(_WIPE_LOOKUP_EDGES, mapping_uri=mapping_uri)

        any_resolved = False
        for role, col_name in role_columns:
            col_name = (col_name or "").strip()
            if not col_name:
                continue
            if is_dprod:
                res = session.run(
                    _RESOLVE_DPROD_COLUMN,
                    contract_id=contract_id, table=table, col_name=col_name,
                ).single()
            else:
                res = session.run(
                    _RESOLVE_CATALOG_COLUMN,
                    catalog_prefix=catalog_prefix, table=table,
                    schema=schema, col_name=col_name,
                ).single()
            uris = (res or {}).get("uris") or []
            if len(uris) != 1:
                # 0 = undiscovered reference table; >1 = ambiguous bare name.
                # Either way, record and skip rather than guess.
                unresolved.append(f"{mapping_uri} [{role}:{table}.{col_name}]")
                continue
            session.run(
                _MERGE_LOOKUP_EDGE,
                mapping_uri=mapping_uri, target_uri=uris[0],
                role=role, strategy=strategy,
            )
            edges += 1
            any_resolved = True

        if any_resolved:
            resolved += 1

    return {"resolved": resolved, "edges": edges, "unresolved": unresolved}
