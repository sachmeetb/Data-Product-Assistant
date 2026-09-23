"""
catalog_loader.py — read a utility_catalog*.json file into normalised, typed
table/column specs the hydrator can consume.

Handles the standard catalog shape used across backend/data/:

    { "data_catalog": { "layers": { "gold": [...], "silver": [...], "bronze": [...] } } }

Each table entry carries: full_name, catalog, schema_name, table_name, layer,
description, tags{}, owner, refresh_cadence, upstream_lineage[], and
columns[{name, data_type, nullable, description, is_pk, fk_ref, tags}].
Some entries may also carry a `sample_data` list of row dicts (optional).

Pure module — only the standard library.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ColumnSpec:
    name: str
    data_type: str                 # catalog (Databricks-style) type, e.g. "BIGINT"
    nullable: bool = True
    description: str = ""
    is_pk: bool = False
    fk_ref: str | None = None      # "table.column" or None
    tags: dict = field(default_factory=dict)


@dataclass
class TableSpec:
    full_name: str                 # design-time FQN, e.g. "acn_aggregated.marketing.campaign_clicks_conformed"
    catalog: str                   # design-time catalog, e.g. "acn_aggregated"
    schema_name: str               # domain schema, e.g. "marketing"
    table_name: str
    layer: str                     # "bronze" | "silver" | "gold"
    description: str = ""
    tags: dict = field(default_factory=dict)
    owner: str = ""
    refresh_cadence: str = ""
    upstream_lineage: list = field(default_factory=list)
    columns: list = field(default_factory=list)          # list[ColumnSpec]
    sample_data: list = field(default_factory=list)       # list[dict] (optional)

    @property
    def pk_columns(self) -> list:
        return [c.name for c in self.columns if c.is_pk]


_LAYER_ORDER = ("bronze", "silver", "gold")


def _column_from_dict(d: dict) -> ColumnSpec:
    return ColumnSpec(
        name=d["name"],
        data_type=d.get("data_type", "STRING"),
        nullable=bool(d.get("nullable", True)),
        description=d.get("description", "") or "",
        is_pk=bool(d.get("is_pk", False)),
        fk_ref=d.get("fk_ref"),
        tags=d.get("tags", {}) or {},
    )


def _split_fqn(full_name: str) -> tuple:
    """Split a 3-level 'catalog.schema.table' into its parts (missing → '')."""
    parts = (full_name or "").split(".")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    if len(parts) == 1 and parts[0]:
        return "", "", parts[0]
    return "", "", ""


def _table_from_dict(d: dict, layer: str) -> TableSpec:
    # Some catalog files carry stub entries that only have full_name — derive
    # the missing name/catalog/schema from it rather than KeyError-ing.
    fq_catalog, fq_schema, fq_table = _split_fqn(d.get("full_name", ""))
    table_name = d.get("table_name") or d.get("name") or fq_table
    if not table_name:
        raise KeyError("table entry has neither table_name nor a usable full_name")
    return TableSpec(
        full_name=d.get("full_name", ""),
        catalog=d.get("catalog") or fq_catalog,
        schema_name=d.get("schema_name") or fq_schema,
        table_name=table_name,
        layer=d.get("layer", layer),
        description=d.get("description", "") or "",
        tags=d.get("tags", {}) or {},
        owner=d.get("owner", "") or "",
        refresh_cadence=d.get("refresh_cadence", "") or "",
        upstream_lineage=list(d.get("upstream_lineage", []) or []),
        columns=[_column_from_dict(c) for c in d.get("columns", [])],
        sample_data=list(d.get("sample_data", []) or []),
    )


def load_catalog(path: str | Path, *, layers: tuple | None = None) -> list:
    """
    Load a catalog JSON file and return an ordered list[TableSpec].

    Args:
        path:   path to a utility_catalog*.json file.
        layers: restrict to these medallion layers (default: all present,
                bronze → silver → gold).

    Raises FileNotFoundError / json.JSONDecodeError on bad input, and KeyError
    if a table entry lacks a table_name.
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    catalog = data.get("data_catalog", data)   # tolerate either wrapped or bare
    layer_map = catalog.get("layers", {}) or {}

    wanted = layers or _LAYER_ORDER
    tables: list = []
    for layer in wanted:
        for entry in layer_map.get(layer, []) or []:
            tables.append(_table_from_dict(entry, layer))
    return tables


def load_all_tables(paths, *, layers: tuple | None = None) -> list:
    """
    Load and concatenate tables from several catalog files, de-duplicating by
    (layer, table_name) — later files win. Useful when the runtime-produced gold
    star schema lives in utility_catalog_pre_ddi.json separate from the base
    utility_catalog.json.
    """
    seen: dict = {}
    for p in paths:
        for t in load_catalog(p, layers=layers):
            seen[(t.layer, t.table_name)] = t
    return list(seen.values())
