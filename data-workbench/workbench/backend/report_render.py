"""Mermaid + Markdown rendering helpers for the data product report.

Pure-Python, no I/O — the marketplace router gathers graph data and hands it
here for stringification. Kept separate from ``routers/marketplace.py`` so
that file doesn't grow further; the queries live there, the render lives
here.
"""

from __future__ import annotations

import re
from typing import Iterable


# Length cap for inline expression strings in Mermaid edge labels. Mermaid
# breaks rendering on very long labels, and the per-mapping table below the
# diagram carries the full expression anyway.
_EXPR_MAX = 40


def _safe_id(s: str | None) -> str:
    """Mermaid node ids must be alphanumerics + underscore. Strip everything
    else and prefix with ``n_`` to guarantee a leading letter so ids like
    ``42`` (which Mermaid sometimes rejects as a numeric literal) round-trip
    cleanly."""
    if not s:
        return "n_empty"
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_")
    if not cleaned:
        return "n_empty"
    return "n_" + cleaned


def _escape_label(s: str | None) -> str:
    """Mermaid labels in quoted form ("..." ) handle most punctuation, but
    quotes and backslashes still need escaping. Newlines collapse to spaces
    so they don't break the line-oriented parser."""
    if s is None:
        return ""
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _truncate(s: str | None, limit: int = _EXPR_MAX) -> str:
    """Truncate a label to ``limit`` chars with an ellipsis. Empty stays empty."""
    if not s:
        return ""
    s = str(s)
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _as_col_list(v) -> list[str]:
    """Coerce an FK edge's ``columns`` / ``referencedColumns`` payload to a
    list of column names.

    Catalog discovery stores single-column FKs as a bare string (most common
    case) and multi-column FKs as a comma-separated string or a list. Anything
    else is normalised away. Returning a list lets the caller ``zip`` it with
    the matching ``referencedColumns`` list without accidentally iterating
    chars of a single string.
    """
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        # Comma-separated payload (the form Postgres FK introspection emits).
        return [p.strip() for p in s.split(",") if p.strip()]
    return [str(v)]


def _mermaid_type(logical: str | None, physical: str | None) -> str:
    """Choose a single type token for the Mermaid erDiagram column row.

    erDiagram is strict about the column-type token (must be a single
    word), so we slug whatever the contract carries down to a safe form.
    Logical type wins because it's the contract-stable one; physical is
    the storage representation and may carry length/precision noise.
    """
    raw = (logical or physical or "string").strip()
    if not raw:
        return "string"
    # erDiagram tolerates dotted names like ``decimal(10,2)`` if we strip
    # the parens/commas; safer to keep alnum only.
    return re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_") or "string"


def build_erd_for_dataset(
    dataset: dict,
    refs: Iterable[dict] = (),
) -> str:
    """Emit a Mermaid ``erDiagram`` block for one dataset.

    Renders one entity with its columns + types + PK markers. ``refs`` are
    rows from the :DProdOutputDataset-[:REFERENCES]->:DProdOutputDataset
    query — each contributes a relationship line to a sibling dataset.
    Pass ``refs=()`` to render the entity in isolation.

    Args:
        dataset: a dict shaped like the ODCS schema entry — ``physicalName``,
            ``properties`` list of ``{name, logicalType, physicalType,
            primaryKey, required}``.
        refs: iterable of dicts ``{from_physical, to_physical, columns,
            referenced_columns}`` describing FK edges sourced from this
            dataset.
    """
    physical = dataset.get("physicalName") or dataset.get("name") or "dataset"
    entity = _safe_id(physical)
    lines = ["```mermaid", "erDiagram"]

    # Relationship lines first — they have to reference entity names defined
    # *somewhere* in the block, but Mermaid hoists entity definitions, so
    # listing relationships before the entity block is fine and keeps the
    # joins visible before the column wall.
    for ref in refs:
        target_phys = ref.get("to_physical") or ref.get("to") or ""
        if not target_phys or target_phys == physical:
            continue
        target = _safe_id(target_phys)
        # ``}o--||`` = many-to-one (mandatory on the right). We don't know
        # cardinality from the FK edge so this is a defensible default —
        # the same shape catalog discovery uses for parent→child references.
        # Catalog FK edges may store columns/referencedColumns as either a
        # list (multi-column FK) or a single string (single-column FK, the
        # common case). Normalise both into a list so the zip below doesn't
        # iterate chars-of-string by accident.
        cols = _as_col_list(ref.get("columns"))
        ref_cols = _as_col_list(ref.get("referenced_columns"))
        label_pairs = []
        for i, c in enumerate(cols):
            rc = ref_cols[i] if i < len(ref_cols) else c
            label_pairs.append(f"{c}={rc}")
        label = _escape_label(",".join(label_pairs)) or "references"
        lines.append(f'    {entity} }}o--|| {target} : "{label}"')

    # Entity block: ``EntityName { TYPE name PK "description" }``
    lines.append(f"    {entity} {{")
    for col in dataset.get("properties") or []:
        name = col.get("name") or "col"
        # Entity field names must be alphanumeric; mirror erDiagram syntax.
        col_id = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "col"
        type_tok = _mermaid_type(col.get("logicalType"), col.get("physicalType"))
        markers: list[str] = []
        if col.get("primaryKey"):
            markers.append("PK")
        # erDiagram doesn't have a NOT NULL marker per se; description goes
        # in a quoted field. We pack required-ness into the description so
        # readers can see it without leaving the diagram.
        comment_parts = []
        if col.get("required") and not col.get("primaryKey"):
            comment_parts.append("required")
        desc = col.get("description") or ""
        if desc:
            comment_parts.append(_truncate(desc, 60))
        marker_str = " ".join(markers)
        comment_str = '"' + _escape_label(", ".join(comment_parts)) + '"' if comment_parts else ""
        # ``    TYPE name PK "comment"`` — empty marker/comment fields are
        # tolerated by Mermaid but cleaner to omit.
        row = f"        {type_tok} {col_id}"
        if marker_str:
            row += f" {marker_str}"
        if comment_str:
            row += f" {comment_str}"
        lines.append(row)
    lines.append("    }")
    lines.append("```")
    return "\n".join(lines)


def build_lineage_graph(mappings: list[dict]) -> str:
    """Emit a Mermaid ``graph LR`` block for the product's mapping edges.

    ``mappings`` is the deduped list from the ``/mapping-graph`` endpoint
    (or the raw rows from ``MARKETPLACE_MAPPING_GRAPH_QUERY``). Each entry
    needs at minimum:
      - ``source_col_uri`` (None for literals)
      - ``source_schema``, ``source_table``, ``source_col_name``
      - ``product_col_uri``, ``product_col_name``
      - ``product_dataset_name``
      - ``transform_kind`` (used as edge label)
      - ``transform_expression`` (truncated, appended to edge label for
        non-direct kinds)
      - ``literal_value`` (rendered as a synthetic source node when
        transform_kind == 'literal')

    Sources are grouped into ``subgraph`` blocks per source table.
    Products are grouped per output dataset.
    """
    if not mappings:
        return ""

    lines = ["```mermaid", "graph LR"]

    # Group source columns by (schema, table). Each table becomes a subgraph.
    # Literals don't have a source — they get a synthetic ``lit_<n>`` node
    # rendered inline (not inside any source subgraph) so they don't pollute
    # the source table layout.
    sources: dict[tuple[str, str], list[dict]] = {}
    products: dict[str, list[dict]] = {}
    literals: list[tuple[str, dict]] = []  # (literal_id, mapping_dict)
    seen_source_cols: set[str] = set()
    seen_product_cols: set[str] = set()

    for i, m in enumerate(mappings):
        kind = (m.get("transform_kind") or "").lower()
        pc_uri = m.get("product_col_uri") or m.get("target_uri")
        pc_name = m.get("product_col_name") or "?"
        prod_ds = m.get("product_dataset_name") or "product"

        if pc_uri and pc_uri not in seen_product_cols:
            seen_product_cols.add(pc_uri)
            products.setdefault(prod_ds, []).append({
                "uri": pc_uri,
                "name": pc_name,
                "id": _safe_id(pc_uri),
            })

        if kind == "literal":
            # Render the literal value as a synthetic source node so the
            # mapping edge still has something to terminate.
            lit_id = f"lit_{i}"
            literals.append((lit_id, m))
        else:
            sc_uri = m.get("source_col_uri") or m.get("source_uri")
            sc_name = m.get("source_col_name") or "?"
            schema = m.get("source_schema") or ""
            table = m.get("source_table") or ""
            if sc_uri and sc_uri not in seen_source_cols:
                seen_source_cols.add(sc_uri)
                sources.setdefault((schema, table), []).append({
                    "uri": sc_uri,
                    "name": sc_name,
                    "id": _safe_id(sc_uri),
                })

    # Emit source-side subgraphs.
    for (schema, table), cols in sorted(sources.items()):
        sg_id = _safe_id(f"src_{schema}_{table}")
        title = _escape_label(f"{schema}.{table}".strip(".") or "source")
        lines.append(f'  subgraph {sg_id}["{title}"]')
        for c in cols:
            lines.append(f'    {c["id"]}["{_escape_label(c["name"])}"]')
        lines.append("  end")

    # Literal nodes — rendered as a single subgraph at the bottom of the
    # source side. Constants ARE sources for purposes of the diagram.
    if literals:
        lines.append('  subgraph n_literals["literals"]')
        for lit_id, m in literals:
            val = m.get("literal_value")
            label = _escape_label(f"= {val}") if val is not None else "(literal)"
            lines.append(f'    {lit_id}["{label}"]')
        lines.append("  end")

    # Emit product-side subgraphs.
    for prod_ds, cols in sorted(products.items()):
        sg_id = _safe_id(f"prod_{prod_ds}")
        title = _escape_label(prod_ds or "product")
        lines.append(f'  subgraph {sg_id}["{title}"]')
        for c in cols:
            lines.append(f'    {c["id"]}["{_escape_label(c["name"])}"]')
        lines.append("  end")

    # Emit edges.
    for i, m in enumerate(mappings):
        kind = (m.get("transform_kind") or "direct").lower()
        pc_uri = m.get("product_col_uri") or m.get("target_uri")
        if not pc_uri:
            continue
        pc_id = _safe_id(pc_uri)
        if kind == "literal":
            src_id = f"lit_{i}"
        else:
            sc_uri = m.get("source_col_uri") or m.get("source_uri")
            if not sc_uri:
                continue
            src_id = _safe_id(sc_uri)

        # Edge label = kind (+ optional truncated expression). ``direct`` is
        # the boring case — omit the label entirely so the diagram doesn't
        # get visually noisy on a typical SA mapping where every edge is
        # direct.
        label_parts: list[str] = []
        if kind and kind != "direct":
            label_parts.append(kind)
        expr = m.get("transform_expression")
        if expr and kind not in ("direct", "literal"):
            label_parts.append(_truncate(expr))
        if label_parts:
            label = _escape_label(" — ".join(label_parts))
            lines.append(f'  {src_id} -->|"{label}"| {pc_id}')
        else:
            lines.append(f"  {src_id} --> {pc_id}")

    lines.append("```")
    return "\n".join(lines)
