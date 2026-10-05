"""Shared, pure assembler for the layered value-flow ("Sankey") view.

Emits one normalized ``FlowPayload`` — **always** the same 5 columns in a fixed
left→right, data-flow order::

    source_system → source_aligned → aggregate → consumer → use_case

rendered by a **context-agnostic** frontend component. The marketplace context
(v1) fills the middle three columns from the ``:CONSUMES`` DAG and the leftmost
column from source-mapping catalogs; the Estate/Feasibility context
(fast-follow) reuses the exact same shape + component, differing only in the
leftmost column's human label and the ``status`` semantics.

Pure: no I/O, no DB, no Neo4j. Unit-testable in isolation (mirrors the
pure-logic style of ``tests/test_feasibility.py``).

Payload shape::

    {
      "columns": [                       # ALWAYS 5, fixed order (COLUMN_ORDER)
        {"key": "aggregate", "label": "...",
         "nodes": [{"id","label","column","kind?","status?","count?","meta?","placeholder?"}]}
      ],
      "links": [{"source","target","kind","status?","weight?"}]
    }

An empty column carries ``nodes: []`` — the frontend synthesizes one ghost
node so the column still occupies horizontal space.
"""

from __future__ import annotations

from typing import Any, Optional


# The 5 columns in fixed order + default human labels. Single authority for the
# frame; both contexts import COLUMN_ORDER so they can never disagree about how
# many columns exist or their order.
#
# The ``source_system`` column's LABEL is context-overridable ("Source Schemas"
# in the marketplace — a synthesized per-catalog grouping — vs "Source Systems"
# in the Estate view, which has real named systems). The KEY stays
# ``source_system`` so the shared component is context-agnostic; only the human
# label differs by context (the backend passes it in).
COLUMN_ORDER: list[str] = [
    "source_system",
    "source_aligned",
    "aggregate",
    "consumer",
    "use_case",
]

COLUMN_LABELS: dict[str, str] = {
    "source_system": "Source Systems",
    "source_aligned": "Source-Aligned Products",
    "aggregate": "Aggregate / Derived Products",
    "consumer": "Consumer-Aligned Products",
    "use_case": "Use Cases",
}

# Link kinds the payload can carry. ``consumes`` + ``source_binding`` are emitted
# by the marketplace context; ``supports`` + ``feasibility_match`` are reserved
# for the estate fast-follow (kept here so the vocabulary is one authority).
LINK_KINDS: frozenset[str] = frozenset(
    {"consumes", "source_binding", "supports", "feasibility_match"}
)

# productKind → column key. Blank / unknown → source_aligned, matching
# PRODUCT_LINEAGE_NODES's ``coalesce(dc.productKind, '')`` convention (an
# unclassified product is treated as source-aligned).
_KIND_TO_COLUMN: dict[str, str] = {
    "source": "source_aligned",
    "aggregate": "aggregate",
    "consumer": "consumer",
}


def bucket_product_kind(kind: Optional[str]) -> str:
    """Map a ``productKind`` to its flow column key.

    ``source`` → ``source_aligned``, ``aggregate`` → ``aggregate``,
    ``consumer`` → ``consumer``. Blank / ``None`` / unknown → ``source_aligned``.
    """
    return _KIND_TO_COLUMN.get((kind or "").strip().lower(), "source_aligned")


class FlowBuilder:
    """Accumulates nodes + links, then emits the always-5-columns dict.

    - ``add_node`` is idempotent per id (a re-add with the SAME column is a
      no-op returning the existing node); reusing an id across DIFFERENT columns
      raises ``ValueError`` (a defensive guard against, e.g., a catalog URI
      colliding with a product URI).
    - ``add_link`` dedups on ``(source, target, kind)``.
    - ``build`` drops any link whose endpoints aren't BOTH present as nodes —
      the same defensive pattern as ``get_product_lineage`` (a link into a node
      that was filtered out, e.g. by domain scope, silently disappears rather
      than dangling).
    """

    def __init__(self, column_labels: Optional[dict[str, str]] = None) -> None:
        self._labels: dict[str, str] = dict(COLUMN_LABELS)
        if column_labels:
            # Only known column keys can be relabelled; unknown keys are ignored
            # so a typo can't silently add a phantom column.
            for k, v in column_labels.items():
                if k in self._labels:
                    self._labels[k] = v
        self._nodes: dict[str, dict[str, Any]] = {}
        self._node_column: dict[str, str] = {}
        self._column_nodes: dict[str, list[str]] = {k: [] for k in COLUMN_ORDER}
        self._links: list[dict[str, Any]] = []
        self._link_keys: set[tuple[str, str, str]] = set()

    def add_node(
        self,
        column: str,
        node_id: str,
        label: str,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        count: Optional[int] = None,
        meta: Optional[dict[str, Any]] = None,
        placeholder: bool = False,
    ) -> dict[str, Any]:
        if column not in self._column_nodes:
            raise ValueError(f"unknown column key: {column!r}")
        existing_col = self._node_column.get(node_id)
        if existing_col is not None:
            if existing_col != column:
                raise ValueError(
                    f"node id {node_id!r} reused across columns "
                    f"({existing_col!r} vs {column!r})"
                )
            # Idempotent: same id + same column → return the existing node
            # unchanged (first write wins).
            return self._nodes[node_id]
        node: dict[str, Any] = {"id": node_id, "label": label, "column": column}
        if kind is not None:
            node["kind"] = kind
        if status is not None:
            node["status"] = status
        if count is not None:
            node["count"] = count
        if meta is not None:
            node["meta"] = meta
        if placeholder:
            node["placeholder"] = True
        self._nodes[node_id] = node
        self._node_column[node_id] = column
        self._column_nodes[column].append(node_id)
        return node

    def add_link(
        self,
        source: str,
        target: str,
        kind: str,
        *,
        status: Optional[str] = None,
        weight: Optional[int] = None,
    ) -> None:
        key = (source, target, kind)
        if key in self._link_keys:
            return
        self._link_keys.add(key)
        link: dict[str, Any] = {"source": source, "target": target, "kind": kind}
        if status is not None:
            link["status"] = status
        if weight is not None:
            link["weight"] = weight
        self._links.append(link)

    def build(self) -> dict[str, Any]:
        present = set(self._nodes.keys())
        links = [
            lk
            for lk in self._links
            if lk["source"] in present and lk["target"] in present
        ]
        columns = [
            {
                "key": key,
                "label": self._labels[key],
                "nodes": [self._nodes[nid] for nid in self._column_nodes[key]],
            }
            for key in COLUMN_ORDER
        ]
        return {"columns": columns, "links": links}
