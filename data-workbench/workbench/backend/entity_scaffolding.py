"""Deterministic entity scaffolding for the business-concept layer.

Derives a 3-tier Entity → Attribute → Value model + entity-to-entity
relationships directly from the graph's structure — tables (`:DProdOutputDataset`
+ `relationshipKind`), primary keys, and FK `:REFERENCES` edges — with no LLM.
This is the backbone of the entity model; the `business-concept-advisor` skill
stays additive (nicer names/definitions/value enums).

Rules (validated against the customer + products_sales graphs):
  - entity candidates = tables whose relationshipKind ∈ {fact, lookup_dimension,
    specialization, general_membership} OR that have a PK (unknown kind). Tables
    that derive the same entity name are merged (e.g. customer + customer_core).
  - audit_log / configuration tables are NOT entities (their measures are
    historized attributes of the table they reference) — skipped here.
  - attributes = non-PK, non-FK columns, bound to their `:DProdColumn`.
  - values = the column's profiled top-values when it looks categorical.
  - relationships = each `:REFERENCES` FK whose both endpoints are entities;
    kind inferred from the endpoints' relationshipKind (self → parent_of,
    →lookup_dimension → classified_by, else references).

Exposes ``scaffold(settings, domain, dry_run)``: dry_run returns the proposed
model without writing; otherwise it start-fresh deprecates the domain's concepts
and persists entities/attributes/values (active) + relationships (pending).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import business_concepts as bc
from .models import AppSettings
from .neo4j_client import neo4j_session

# Tables with their relationshipKind + columns (PK flag, dtype, top-values).
_TABLES_QUERY = """
MATCH (p:Project)-[:HAS_CONTRACT]->(dc:DataContract)
WHERE coalesce(dc.isCurrent, true) = true AND p.domain = $domain
MATCH (dc)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
      -[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
OPTIONAL MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
OPTIONAL MATCH (cm:ColumnMapping)-[:MAPS_TO_PRODUCT_COLUMN]->(pc)
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(src:Column)-[:HAS_TOP_VALUE]->(tv:TopValue)
WITH ods, pc, collect(DISTINCT tv.value)[..12] AS top_values
ORDER BY pc.ordinal
RETURN ods.uri AS dataset_uri,
       ods.physicalName AS physical_name,
       coalesce(ods.relationshipKind, 'unknown') AS relationship_kind,
       collect(CASE WHEN pc IS NULL THEN NULL ELSE {
         column_uri: pc.uri, name: pc.name,
         is_pk: coalesce(pc.isPrimaryKey, false),
         data_type: coalesce(pc.dataType, ''),
         top_values: top_values
       } END) AS cols
ORDER BY physical_name
"""

_FK_QUERY = """
MATCH (p:Project)-[:HAS_CONTRACT]->(dc:DataContract)
WHERE coalesce(dc.isCurrent, true) = true AND p.domain = $domain
MATCH (dc)-[:MATERIALISES_AS]->(:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)
      -[:DPROD_OUTPUT_DATASET]->(a:DProdOutputDataset)
MATCH (a)-[r:REFERENCES]->(b:DProdOutputDataset)
RETURN a.physicalName AS from_table, b.physicalName AS to_table,
       coalesce(r.columns, []) AS fk_cols, coalesce(r.referencedColumns, []) AS ref_cols
"""

# relationshipKinds that are NOT standalone entities (their columns are
# historized attributes / config of the table they reference).
_NON_ENTITY_KINDS = {"audit_log", "configuration"}

_DOMAIN_PREFIX_RE = None  # set per-call


def _titleize(token: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[_\s]+", token) if w)


def _singularize(token: str) -> str:
    if token.endswith("ies") and len(token) > 3:
        return token[:-3] + "y"
    if token.endswith("ses") and len(token) > 3:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 1:
        return token[:-1]
    return token


_TABLE_SUFFIXES = ("_header", "_core", "_dim", "_fact", "_master", "_table", "_tbl")


def _entity_name(physical_name: str, domain: str) -> str:
    # NB: table physical names are NOT domain-prefixed (only columns are, via the
    # column-name-standardizer), so we only strip table-shape suffixes. This also
    # merges e.g. `customer` + `customer_core` into one Customer entity.
    n = (physical_name or "").lower()
    for suf in _TABLE_SUFFIXES:
        if n.endswith(suf):
            n = n[: -len(suf)]
            break
    n = _singularize(n)
    return _titleize(n) or _titleize(physical_name)


def _attr_label(col_name: str, domain: str) -> str:
    n = (col_name or "").lower()
    if domain and n.startswith(domain.lower() + "_"):
        n = n[len(domain) + 1:]
    return _titleize(n)


def _looks_categorical(col: dict[str, Any]) -> bool:
    tv = col.get("top_values") or []
    if not tv or len(tv) > 12:
        return False
    name = (col.get("name") or "").lower()
    dt = (col.get("data_type") or "").lower()
    if any(k in name for k in ("status", "code", "method", "channel", "type", "kind", "segment", "category", "gender", "flag")):
        return True
    # short text columns with a small bounded set of values
    return ("char" in dt or "text" in dt or dt == "") and len(tv) <= 8


def build_model(settings: AppSettings, domain: str) -> dict[str, Any]:
    """Derive the entity/attribute/value/relationship model for a domain."""
    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        tables = [dict(r) for r in ns.run(_TABLES_QUERY, domain=domain)]
        fks = [dict(r) for r in ns.run(_FK_QUERY, domain=domain)]

    # FK columns per table (so we can exclude them from attributes).
    fk_cols_by_table: dict[str, set[str]] = {}
    for f in fks:
        cols = f.get("fk_cols") or []
        cols = cols if isinstance(cols, list) else [cols]
        fk_cols_by_table.setdefault(f["from_table"], set()).update(str(c).lower() for c in cols)

    # table physical_name → derived entity name (audit/config tables excluded).
    table_to_entity: dict[str, str] = {}
    entities: dict[str, dict[str, Any]] = {}  # name → entity dict

    for t in tables:
        phys = t["physical_name"]
        kind = t["relationship_kind"]
        cols = [c for c in (t["cols"] or []) if c]
        has_pk = any(c.get("is_pk") for c in cols)
        if kind in _NON_ENTITY_KINDS:
            continue
        if not (has_pk or kind in ("fact", "lookup_dimension", "specialization", "general_membership")):
            continue
        ename = _entity_name(phys, domain)
        table_to_entity[phys] = ename
        ent = entities.setdefault(ename, {
            "name": ename, "physical_names": [], "table_bindings": [],
            "identity": [], "attr_candidates": [], "country_refs": [], "relationship_kinds": set(),
        })
        ent["physical_names"].append(phys)
        ent["table_bindings"].append(t["dataset_uri"])
        ent["relationship_kinds"].add(kind)
        fk_cols = fk_cols_by_table.get(phys, set())
        for c in cols:
            cname = c.get("name") or ""
            if not cname:
                continue
            if c.get("is_pk"):
                if cname not in ent["identity"]:
                    ent["identity"].append(cname)
                continue
            if cname.lower() in fk_cols:
                continue  # FK column → relationship, not attribute
            values = []
            if _looks_categorical(c):
                for v in (c.get("top_values") or []):
                    vs = str(v).strip()
                    if vs:
                        values.append({"name": _titleize(vs) or vs, "value_token": vs})
            label = _attr_label(cname, domain)
            # Shared-concept reconciliation (v1: Country). A country-code column
            # is NOT a standalone attribute — it's a reference to the shared
            # Country entity, captured as a relationship carrying the role +
            # the implementing column.
            if "country" in label.lower():
                ent["country_refs"].append({
                    "kind": "ships_to" if "ship" in label.lower() else "located_in",
                    "via_column": cname,
                    "value_tokens": [v["value_token"] for v in values],
                })
                continue
            # One candidate per column; _merge_attributes collapses synonyms.
            ent["attr_candidates"].append({
                "label": label,
                "column_uri": c.get("column_uri"),
                "data_type": c.get("data_type"), "values": values,
            })

    # Relationships from FK edges where both endpoints are entities.
    relationships: list[dict[str, Any]] = []
    seen_rel: set[tuple[str, str, str]] = set()
    kind_by_table = {t["physical_name"]: t["relationship_kind"] for t in tables}
    for f in fks:
        ft, tt = f["from_table"], f["to_table"]
        e_from = table_to_entity.get(ft)
        e_to = table_to_entity.get(tt)
        if not e_from or not e_to:
            continue  # one side is an audit/config table — skip
        if e_from == e_to:
            kind = "parent_of"
        elif kind_by_table.get(tt) == "lookup_dimension":
            kind = "classified_by"
        elif kind_by_table.get(ft) == "general_membership":
            kind = "belongs_to"
        else:
            kind = "references"
        key = (e_from, e_to, kind)
        if key in seen_rel:
            continue
        seen_rel.add(key)
        relationships.append({"from_name": e_from, "to_name": e_to, "kind": kind})

    # Shape for output; merge synonymous attribute candidates per entity.
    out_entities = []
    references: list[dict[str, Any]] = []          # entity -> shared Country
    country_tokens: list[str] = []                 # union of country value tokens
    for ename, ent in sorted(entities.items()):
        out_entities.append({
            "name": ename,
            "physical_names": ent["physical_names"],
            "table_bindings": ent["table_bindings"],
            "identity": ent["identity"],
            "attributes": _merge_attributes(ent["attr_candidates"]),
        })
        for ref in ent["country_refs"]:
            references.append({
                "from_name": ename, "to_shared": "Country",
                "kind": ref["kind"], "via_column": ref["via_column"],
            })
            for tok in ref["value_tokens"]:
                if tok not in country_tokens:
                    country_tokens.append(tok)
    return {
        "domain": domain, "entities": out_entities, "relationships": relationships,
        "references": references, "country_tokens": sorted(country_tokens),
    }


# Trailing qualifier tokens stripped for the normalized-label merge key. Kept
# deliberately tiny — stripping meaningful tokens (code/status/type) would
# wrongly merge distinct attributes (e.g. Country Code into Country).
_QUALIFIER_TOKENS = {"usd", "amt", "amount", "value"}


def _norm_label(label: str) -> str:
    toks = [t for t in re.split(r"\s+", (label or "").lower()) if t]
    while len(toks) > 1 and toks[-1] in _QUALIFIER_TOKENS:
        toks.pop()
    return _singularize(" ".join(toks))


def _merge_two(gi: dict[str, Any], gj: dict[str, Any]) -> None:
    """Fold group gj into gi (labels, column_uris, values)."""
    gi["labels"].extend(gj["labels"])
    for u in gj["column_uris"]:
        if u not in gi["column_uris"]:
            gi["column_uris"].append(u)
    for v in gj["values"]:
        if not any(ev["value_token"] == v["value_token"] for ev in gi["values"]):
            gi["values"].append(v)


def _merge_attributes(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse synonymous attribute candidates into one attribute carrying
    multiple column bindings.

    Pass 1 — normalized-label key (lowercase + singularize + strip a tiny set
    of trailing unit qualifiers) merges 'Lifetime Value' / 'Lifetime Value Usd'
    and identical columns appearing in more than one of the entity's tables.

    Pass 2 — token-subset merge: when one group's word-set is a strict subset
    of exactly one other group's, fold it in ('Email' ⊂ 'Primary Email',
    'Status' ⊂ 'Account Status'). Deterministic and safe — it never merges
    siblings like 'First Name' / 'Last Name' (neither is a subset of the
    other), which an embedding-similarity pass wrongly collapsed."""
    if not candidates:
        return []

    # Pass 1: normalized-label groups.
    groups: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for c in candidates:
        key = _norm_label(c["label"])
        g = by_key.get(key)
        if g is None:
            g = {"labels": [], "column_uris": [], "values": [], "data_type": c.get("data_type")}
            by_key[key] = g
            groups.append(g)
        g["labels"].append(c["label"])
        if c.get("column_uri") and c["column_uri"] not in g["column_uris"]:
            g["column_uris"].append(c["column_uri"])
        for v in c.get("values") or []:
            if not any(ev["value_token"] == v["value_token"] for ev in g["values"]):
                g["values"].append(v)

    # Pass 2: strict token-subset merge (fold the smaller into its unique superset).
    tokens = [set(re.split(r"\s+", min(g["labels"], key=len).lower())) for g in groups]
    merged_into: dict[int, int] = {}
    for i in range(len(groups)):
        if i in merged_into:
            continue
        supersets = [
            j for j in range(len(groups))
            if j != i and j not in merged_into and tokens[i] < tokens[j]
        ]
        if len(supersets) == 1:
            _merge_two(groups[supersets[0]], groups[i])
            merged_into[i] = supersets[0]
    groups = [g for k, g in enumerate(groups) if k not in merged_into]

    # Canonical name = shortest label (favours the un-qualified form).
    out = []
    for g in groups:
        name = min(g["labels"], key=len)
        out.append({
            "name": name, "column_uris": g["column_uris"],
            "data_type": g["data_type"], "values": g["values"],
        })
    return sorted(out, key=lambda a: a["name"])


def scaffold(settings: AppSettings, domain: str, dry_run: bool = True) -> dict[str, Any]:
    """Build the model and (unless dry_run) persist it start-fresh."""
    model = build_model(settings, domain)
    counts = {
        "entities": len(model["entities"]),
        "attributes": sum(len(e["attributes"]) for e in model["entities"]),
        "values": sum(len(a["values"]) for e in model["entities"] for a in e["attributes"]),
        "relationships": len(model["relationships"]),
    }
    if dry_run:
        return {"dry_run": True, "domain": domain, "counts": counts, **model}

    # Start-fresh: deprecate every existing concept in the domain first.
    deprecated = bc.deprecate_domain_concepts(settings, domain)

    name_to_uri: dict[str, str] = {}
    for e in model["entities"]:
        res = bc.create_concept(
            settings,
            name=e["name"],
            definition=(
                f"{e['name']} — business entity"
                + (f" (source: {', '.join(e['physical_names'])})" if e["physical_names"] else "")
                + (f"; identified by {', '.join(e['identity'])}" if e["identity"] else "") + "."
            ),
            domain=domain, level="entity",
            represented_by_dataset_uris=e["table_bindings"],
            created_by="entity-scaffolder", if_exists="deprecate_existing",
        )
        name_to_uri[e["name"]] = res["uri"]
        for a in e["attributes"]:
            attr = bc.create_concept(
                settings,
                name=a["name"],
                definition=f"{a['name']} — attribute of {e['name']}.",
                domain=domain, level="attribute",
                parent_uri=res["uri"],
                represented_by_uris=a.get("column_uris") or [],
                created_by="entity-scaffolder", if_exists="deprecate_existing",
            )
            # If this attribute name coreferenced an existing higher-tier concept
            # (an entity of the same name — e.g. an FK-shaped attribute), don't
            # graft values onto that entity.
            if attr.get("coreferenced"):
                continue
            for v in a["values"]:
                try:
                    bc.create_concept(
                        settings,
                        name=v["name"],
                        definition=f"{v['name']} — value of {a['name']}.",
                        domain=domain, level="value",
                        value_token=v["value_token"], parent_uri=attr["uri"],
                        created_by="entity-scaffolder", if_exists="deprecate_existing",
                    )
                except Exception:
                    continue

    rels_created = 0
    for r in model["relationships"]:
        fu, tu = name_to_uri.get(r["from_name"]), name_to_uri.get(r["to_name"])
        if not fu or not tu:
            continue
        bc.set_relationship_status(settings, fu, tu, r["kind"], "pending")
        rels_created += 1

    # Shared-concept reconciliation (auto): country references → one shared
    # Country reference entity that both products point at. Deterministic URIs
    # so re-scaffolding any domain upserts (not duplicates) the shared nodes;
    # the 'shared' domain is never touched by deprecate_domain_concepts(domain).
    refs_created = _apply_shared_country(settings, model, name_to_uri)

    # Cross-product entity coreference (Phase A): when a consumer product's
    # dataset maps (via its authored :ColumnMappings) predominantly to a source
    # dataset that IS an entity, bind that consumer dataset to the SAME in-domain
    # entity. Lineage-anchored (never name/value heuristics) so it can't
    # over-merge; a no-op until the consumer's mapping stage has run.
    xproduct_bound = _apply_cross_product_bindings(settings, model, name_to_uri)

    return {
        "dry_run": False, "domain": domain, "counts": counts,
        "deprecated_existing": deprecated, "relationships_created": rels_created,
        "shared_references_created": refs_created,
        "cross_product_bindings": xproduct_bound,
    }


# ── Cross-product entity coreference (Phase A) ────────────────────────────────
#
# Anchor: a consumer product's :ColumnMapping links its output :DProdColumn
# (MAPS_TO_PRODUCT_COLUMN) to a CONSUMED source :DProdColumn (MAPS_SOURCE_COLUMN).
# That is provable dataset↔dataset lineage — no names, no value probing — so it
# cannot over-merge distinct entities. The source product's own 1:1 auto-mappings
# point MAPS_SOURCE_COLUMN at a raw :Column, so requiring a :DProdColumn source
# naturally excludes them (only true cross-product mappings match).
_CROSS_PRODUCT_LINEAGE_QUERY = """
UNWIND $src_ds_uris AS src_uri
MATCH (src_ds:DProdOutputDataset {uri: src_uri})-[:HAS_PRODUCT_COLUMN]->(src_col:DProdColumn)
MATCH (cm:ColumnMapping)-[:MAPS_SOURCE_COLUMN]->(src_col)
MATCH (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(cons_col:DProdColumn)<-[:HAS_PRODUCT_COLUMN]-(cons_ds:DProdOutputDataset)
WHERE cons_ds.uri <> src_ds.uri
WITH src_ds, cons_ds, count(DISTINCT cons_col) AS mapped_cols
MATCH (cons_ds)-[:HAS_PRODUCT_COLUMN]->(ac:DProdColumn)
RETURN src_ds.uri AS source_ds_uri, cons_ds.uri AS consumer_ds_uri,
       mapped_cols AS mapped_cols, count(DISTINCT ac) AS total_cols
"""

_BIND_CONSUMER_DATASET = """
MATCH (e:BusinessConcept {uri: $entity_uri}), (ds:DProdOutputDataset {uri: $consumer_ds_uri})
MERGE (e)-[:REPRESENTED_BY]->(ds)
RETURN e.uri AS uri
"""


def _pick_cross_product_bindings(rows: list[dict], min_ratio: float = 0.5) -> dict[str, str]:
    """Pure: decide which consumer dataset coreferences which source dataset.

    ``rows`` = ``[{source_ds_uri, consumer_ds_uri, mapped_cols, total_cols}]``.
    For each consumer dataset, bind it to the ONE source dataset it maps to most,
    but only when (a) that source dataset covers ``>= min_ratio`` of the consumer
    dataset's columns and (b) the winner is a STRICT maximum — a consumer dataset
    that mixes columns from two source datasets equally (a junction/blend) is left
    unbound rather than guessed. Returns ``{consumer_ds_uri: source_ds_uri}``.
    """
    by_consumer: dict[str, list[dict]] = {}
    for r in rows:
        by_consumer.setdefault(r["consumer_ds_uri"], []).append(r)
    out: dict[str, str] = {}
    for cons_uri, cand in by_consumer.items():
        cand = sorted(cand, key=lambda c: (c.get("mapped_cols") or 0), reverse=True)
        top = cand[0]
        total = top.get("total_cols") or 0
        mapped = top.get("mapped_cols") or 0
        if total <= 0 or (mapped / total) < min_ratio:
            continue
        # Strict max: reject a tie for the top mapped_cols across source datasets.
        if len(cand) > 1 and (cand[1].get("mapped_cols") or 0) == mapped:
            continue
        out[cons_uri] = top["source_ds_uri"]
    return out


def _apply_cross_product_bindings(
    settings: AppSettings, model: dict, name_to_uri: dict[str, str]
) -> int:
    """Bind consumer datasets to the in-domain entity of the source dataset they
    map to (lineage-anchored). Returns the number of bindings MERGEd."""
    # Map each entity's source dataset URI → the entity's concept URI.
    ds_to_entity: dict[str, str] = {}
    for e in model["entities"]:
        euri = name_to_uri.get(e["name"])
        if not euri:
            continue
        for ds_uri in e.get("table_bindings") or []:
            ds_to_entity[ds_uri] = euri
    if not ds_to_entity:
        return 0

    with neo4j_session(
        settings.neo4j_host, settings.neo4j_port,
        settings.neo4j_user, settings.neo4j_password, settings.neo4j_database,
    ) as ns:
        rows = [dict(r) for r in ns.run(
            _CROSS_PRODUCT_LINEAGE_QUERY, src_ds_uris=list(ds_to_entity.keys()))]
        picks = _pick_cross_product_bindings(rows)
        bound = 0
        for consumer_ds_uri, source_ds_uri in picks.items():
            entity_uri = ds_to_entity.get(source_ds_uri)
            if not entity_uri:
                continue
            ns.run(_BIND_CONSUMER_DATASET,
                   entity_uri=entity_uri, consumer_ds_uri=consumer_ds_uri).consume()
            bound += 1
    return bound


_SHARED_DOMAIN = "shared"
_COUNTRY_URI = "concept:shared:country"
_COUNTRY_CODE_ATTR_URI = "concept:shared:country:code"


def _apply_shared_country(settings: AppSettings, model: dict, name_to_uri: dict[str, str]) -> int:
    """Ensure the shared Country reference entity (+ its Country Code attribute
    and value vocabulary) exists, and MERGE a pending reference relationship
    from each country-referencing entity → Country carrying via_column + role.
    Idempotent across per-domain scaffold runs via fixed URIs."""
    refs = model.get("references") or []
    if not refs:
        return 0
    # Upsert shared Country entity + Country Code attribute (fixed URIs).
    bc.create_concept(
        settings, name="Country",
        definition="Country — shared reference entity; the country a record is associated with.",
        domain=_SHARED_DOMAIN, level="entity", uri=_COUNTRY_URI,
        created_by="entity-scaffolder", if_exists="fail",
    )
    bc.create_concept(
        settings, name="Country Code",
        definition="ISO country code identifying a country (e.g. AU = Australia).",
        domain=_SHARED_DOMAIN, level="attribute", uri=_COUNTRY_CODE_ATTR_URI,
        parent_uri=_COUNTRY_URI, created_by="entity-scaffolder", if_exists="fail",
    )
    # Merge value tokens (idempotent per token).
    for tok in model.get("country_tokens") or []:
        try:
            bc.create_concept(
                settings, name=_titleize(tok) or tok,
                definition=f"{tok} — a country value.",
                domain=_SHARED_DOMAIN, level="value", value_token=tok,
                parent_uri=_COUNTRY_CODE_ATTR_URI, uri=f"concept:shared:country:val:{tok}",
                created_by="entity-scaffolder", if_exists="fail",
            )
        except Exception:
            continue
    # Reference relationships (pending) from each owner entity → Country.
    created = 0
    for ref in refs:
        fu = name_to_uri.get(ref["from_name"])
        if not fu:
            continue
        bc.set_relationship_status(
            settings, fu, _COUNTRY_URI, ref["kind"], "pending", via_column=ref["via_column"],
        )
        created += 1
    return created


# ── LLM enrichment (Phase C) ─────────────────────────────────────────────────

_ENRICH_SYSTEM = (
    "You enrich a data product's business glossary. For each concept write a concise "
    "1-sentence business definition and 2-5 synonyms a user might say.\n"
    "- ENTITY or ATTRIBUTE: when the given name is a raw column-ish label (e.g. "
    "'Segment Code', 'Lifetime Value Usd') you MAY suggest a cleaner business "
    "`display_name` (e.g. 'Customer Segment', 'Lifetime Value'); otherwise omit it.\n"
    "- VALUE: the item is a CODED value (its `value_token` is the literal stored in the "
    "data, e.g. 'AU'). Set `display_name` to the human-readable name (e.g. 'Australia') "
    "and put alternate forms in `synonyms` (e.g. ['AU','AUS','Commonwealth of Australia']) "
    "so a question phrased with the name resolves to the token. NEVER change the token.\n"
    "Return ONLY one fenced json block: {\"concepts\": [{\"uri\": \"...\", "
    "\"definition\": \"...\", \"synonyms\": [\"...\"], \"display_name\": \"...\"}]}. "
    "Echo each uri exactly. No prose outside the json block."
)


async def enrich(settings: AppSettings, domain: str) -> dict[str, Any]:
    """LLM pass that writes business-friendly definitions + synonyms (and cleaner
    display names) onto the domain's entities, attributes, and coded VALUES — plus
    the shared reference entities (Country) the domain points at, so coded values
    like AU gain a human label (Australia). Re-embeds. Best-effort: no-op if the
    SDK or a parseable response isn't available."""
    from . import marketplace_chat as mc  # reuse the inline-SDK helper

    concepts_in: list[dict[str, Any]] = []

    def _gather(tree: list[dict[str, Any]]) -> None:
        for e in tree:
            if e.get("level") != "entity":
                continue
            concepts_in.append({
                "uri": e["uri"], "name": e["name"], "level": "entity",
                "tables": [t["physical_name"] for t in (e.get("represented_by_datasets") or [])],
            })
            for a in e.get("children") or []:
                concepts_in.append({
                    "uri": a["uri"], "name": a["name"], "level": "attribute",
                    "entity": e["name"],
                    "columns": [b["column_name"] for b in (a.get("represented_by") or [])],
                })
                for v in a.get("children") or []:
                    concepts_in.append({
                        "uri": v["uri"], "name": v["name"], "level": "value",
                        "value_token": v.get("value_token"), "attribute": a["name"],
                    })

    _gather(bc.get_tree(settings, domain))
    _gather(bc.get_tree(settings, _SHARED_DOMAIN))  # Country + its coded values
    if not concepts_in:
        return {"enriched": 0, "domain": domain}

    payload = {"domain": domain, "concepts": concepts_in}
    user_prompt = (
        "Enrich these concepts.\n\n"
        f"```json\n{json.dumps(payload, default=str)}\n```\n\n"
        "Emit one fenced ```json block: {\"concepts\": [{uri, definition, synonyms, display_name?}]}."
    )
    text = await mc._run_inline_json(_ENRICH_SYSTEM, user_prompt, max_turns=2)
    if not text:
        return {"enriched": 0, "domain": domain, "note": "enrichment unavailable"}
    matches = mc._JSON_BLOCK_RE.findall(text)
    if not matches:
        return {"enriched": 0, "domain": domain, "note": "no parseable output"}
    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return {"enriched": 0, "domain": domain, "note": "unparseable json"}

    valid_uris = {c["uri"] for c in concepts_in}
    enriched = 0
    for item in parsed.get("concepts") or []:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri")
        if uri not in valid_uris:
            continue  # never trust a hallucinated uri
        syn = [str(s) for s in (item.get("synonyms") or []) if str(s).strip()]
        ok = bc.update_concept_enrichment(
            settings, uri,
            name=item.get("display_name") or None,
            definition=item.get("definition") or None,
            synonyms=syn or None,
        )
        if ok:
            enriched += 1
    return {"enriched": enriched, "domain": domain, "considered": len(concepts_in)}
