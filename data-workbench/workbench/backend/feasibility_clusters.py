"""Deterministic source-table clustering ("seams") for Connected-Estate assembly.

Given a set of estate datasets (tables) — optionally with their captured FK
``:REFERENCES`` edges — group them into natural **clusters**, each a candidate
**source-aligned data product**. This is the deterministic first pass; the
``data-product-boundary-advisor`` skill refines/names it, and the PO reorganizes it
in the Assembly Sources tab.

Two signals, union-find combined:
1. **FK edges** (high precision) — a real foreign key joins two tables' cluster.
2. **Shared identity keys** (recall) — two tables sharing a key-looking column
   (``customer_id``) probably belong together — BUT guarded by **selectivity**: a
   ubiquitous key (a bare ``id`` present in every table, a ``tenant_id`` / audit key)
   must NOT collapse the whole schema into one blob. With no profiling available
   (the metadata scan is PII-safe), selectivity is a **frequency proxy**: a key that
   appears in a MINORITY of tables is joinable; one in the majority is too generic.

Pure + dependency-light (reuses ``join_preflight``'s ``_UnionFind`` / ``_looks_like_key``);
estate-wide-capable (feed it the whole scan, or one spec's in-scope datasets).
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

from .join_preflight import _looks_like_key, _UnionFind

# Keys that are NEVER a merge signal regardless of frequency — a table's own bare PK
# and common tenancy/audit keys. The frequency guard catches most; this is belt-and-
# suspenders for a small estate where a generic key sits in only 2–3 tables.
_NEVER_MERGE_KEYS = {
    "id", "tenant_id", "org_id", "organization_id", "account_id_audit",
    "created_by", "updated_by", "modified_by", "deleted_by", "row_id",
    "load_id", "batch_id", "source_id", "etl_id", "ingest_id",
}

# Key suffixes recognised for clustering — the join_preflight set plus dimensional
# surrogate/foreign keys (`_sk`, `_fk`) common in star schemas (TPC-DS etc.).
_KEY_STEM_RE = re.compile(r"(_id|_code|_key|_no|_number|_sk|_fk)$")
_CLUSTER_KEY_SUFFIXES = ("_sk", "_fk")


def _is_cluster_key(col: str) -> bool:
    c = (col or "").lower()
    return _looks_like_key(col) or any(c.endswith(x) for x in _CLUSTER_KEY_SUFFIXES)


def _split_words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def _singular(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _clean_words(name: str) -> list[str]:
    """Split a table/key name into words, dropping a leading 1–2 char table prefix
    (dimensional models prefix every column: ``c_customer`` / ``cc_call_center``) when
    a real word follows — so the derived name reads "Customer" / "Call Center"."""
    words = _split_words(name)
    if len(words) > 1 and len(words[0]) <= 2:
        words = words[1:]
    return words


def _pretty(words: list[str]) -> str:
    if not words:
        return ""
    out = list(words)
    out[-1] = _singular(out[-1])  # singularize only the trailing noun
    return " ".join(out).title()


def _dominant_token(tables: list[str]) -> str:
    """A deterministic cluster name from its tables (one table → its cleaned name;
    many → the most common cleaned leading token)."""
    if len(tables) == 1:
        return _pretty(_clean_words(tables[0])) or tables[0].title()
    firsts: dict[str, int] = {}
    for t in tables:
        w = _clean_words(t)
        if w:
            firsts[w[0]] = firsts.get(w[0], 0) + 1
    if firsts:
        best = sorted(firsts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return _pretty([best])
    return (tables[0] if tables else "cluster").title()


def _cluster_name(tables: list[str], keys_here: list[str]) -> str:
    """Name a cluster by the joining-entity of its shared key (``customer_id`` →
    "Customer", ``cc_call_center_sk`` → "Call Center") when there is one — the seam's
    subject — else fall back to the tables' dominant token."""
    stems: dict[str, int] = {}
    for k in keys_here:
        words = _clean_words(_KEY_STEM_RE.sub("", k.lower()))
        if words:
            stems[" ".join(words)] = stems.get(" ".join(words), 0) + 1
    if stems:
        best = sorted(stems.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return _pretty(best.split())
    return _dominant_token(tables)


def cluster_datasets(
    datasets: list[dict[str, Any]],
    fk_edges: Optional[list[dict[str, Any]]] = None,
    *,
    ubiquity_ratio: float = 0.9,
) -> list[dict[str, Any]]:
    """Group ``datasets`` (each ``{uri, table, schema, columns:[{name}]}``) into
    clusters. Returns ``[{cluster_id, name, table_refs, uris, rationale, edited_by_user}]``
    ordered largest-first. FK edges take precedence; a shared key merges tables only
    when it's **selective** — present in some but NOT ~all tables (a key in nearly
    every table doesn't discriminate a seam). ``ubiquity_ratio`` is the upper cut
    (min 4 tables), so 2–3-table estates always trust a shared key."""
    uris = [d.get("uri", "") for d in datasets if d.get("uri")]
    by_uri = {d["uri"]: d for d in datasets if d.get("uri")}
    if not uris:
        return []
    uf = _UnionFind(uris)

    # 1) FK edges (both endpoints must be in scope).
    fk_pairs = 0
    for e in fk_edges or []:
        su, tu = e.get("src_uri"), e.get("tgt_uri")
        if su in by_uri and tu in by_uri and su != tu:
            uf.union(su, tu)
            fk_pairs += 1

    # 2) shared identity keys → the set of uris carrying each key-looking column.
    key_tables: dict[str, set[str]] = {}
    for d in datasets:
        for c in d.get("columns", []) or []:
            name = (c.get("name") or "")
            if _is_cluster_key(name):
                key_tables.setdefault(name.lower(), set()).add(d["uri"])
    n = len(uris)
    # A key in ~all tables doesn't discriminate a seam; exclude it. Min-4 floor means
    # a 2–3-table estate always trusts a shared key.
    cap = max(4, math.ceil(ubiquity_ratio * n))
    selective_keys = {
        k for k, us in key_tables.items()
        if 2 <= len(us) < cap and k not in _NEVER_MERGE_KEYS
    }
    for k in sorted(selective_keys):
        us = sorted(key_tables[k])
        for u in us[1:]:
            uf.union(us[0], u)

    comps: dict[str, list[str]] = {}
    for u in uris:
        comps.setdefault(uf.find(u), []).append(u)

    clusters: list[dict[str, Any]] = []
    for members in comps.values():
        member_set = set(members)
        tables = sorted({(by_uri[u].get("table") or "") for u in members})
        keys_here = sorted(k for k in selective_keys
                           if len(key_tables[k] & member_set) >= 2)
        n_fk = sum(1 for e in (fk_edges or [])
                   if e.get("src_uri") in member_set and e.get("tgt_uri") in member_set)
        bits = []
        if n_fk:
            bits.append(f"{n_fk} FK edge(s)")
        if keys_here:
            bits.append("shared key(s) " + ", ".join(keys_here[:4]))
        rationale = ("grouped on " + "; ".join(bits)) if bits else "a standalone table"
        clusters.append({
            "cluster_id": "", "name": _cluster_name(tables, keys_here),
            "table_refs": tables, "uris": sorted(members),
            "rationale": rationale, "edited_by_user": False,
        })
    # Largest first, then name; assign stable ids after ordering.
    clusters.sort(key=lambda c: (-len(c["table_refs"]), c["name"]))
    for i, c in enumerate(clusters):
        c["cluster_id"] = f"c{i}"
    return clusters


# ── boundary-advisor (agentic refinement) input + validation ────────────────────

def boundary_advisor_input(
    clusters: list[dict[str, Any]],
    datasets: list[dict[str, Any]],
    fk_edges: Optional[list[dict[str, Any]]] = None,
    *,
    max_tables: int = 60,
    max_cols: int = 12,
) -> dict[str, Any]:
    """Shape the bounded input for ``data-product-boundary-advisor``: each table with its
    generated description + a column sample, the FK signals among them, and the
    deterministic seed grouping. Descriptions are the reasoning signal the name-based
    clusterer lacks."""
    tables: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in datasets[:max_tables]:
        t = (d.get("table") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        cols = [(c.get("name") or "") for c in (d.get("columns") or [])][:max_cols]
        tables.append({
            "table_ref": t, "description": (d.get("description") or "")[:300],
            "sample_columns": [c for c in cols if c],
        })
    signals: list[dict[str, Any]] = []
    for e in (fk_edges or []):
        sf, st = (e.get("src_table") or "").strip(), (e.get("tgt_table") or "").strip()
        if sf and st and sf in seen and st in seen and sf != st:
            signals.append({"from": sf, "to": st, "kind": "fk", "on": e.get("columns") or []})
    seed = [{"name": c.get("name", ""), "table_refs": c.get("table_refs", [])}
            for c in clusters]
    return {"tables": tables, "signals": signals, "seed_clusters": seed}


def validate_attribute_groups(raw: Any, all_names: list[str]) -> Optional[list[dict[str, Any]]]:
    """Fail-closed validation of an LLM attribute-theme partition. Returns the
    normalized groups ONLY when every supplied attribute name appears in exactly one
    group (none omitted / duplicated / invented); else ``None`` → an ungrouped list."""
    if not isinstance(raw, list) or not raw:
        return None
    want = sorted(set(n for n in all_names if n))
    seen: list[str] = []
    out: list[dict[str, Any]] = []
    for i, g in enumerate(raw):
        if not isinstance(g, dict):
            return None
        names = g.get("attribute_names")
        if not isinstance(names, list) or not names:
            return None
        clean = [str(x) for x in names]
        for x in clean:
            if x not in want:
                return None
            seen.append(x)
        out.append({"name": str(g.get("name") or f"Group {i + 1}")[:48],
                    "attribute_names": clean,
                    "rationale": str(g.get("rationale") or "")[:160]})
    if sorted(seen) != want or len(seen) != len(set(seen)):
        return None
    return out


def validate_clusters(raw: Any, all_tables: list[str]) -> Optional[list[dict[str, Any]]]:
    """Fail-closed validation of an LLM cluster partition against the supplied tables.

    Returns the normalized clusters ONLY when they form a true **partition** — every
    supplied table in exactly one cluster, no table omitted / duplicated / invented.
    Any violation returns ``None`` (→ the deterministic clusters stand)."""
    if not isinstance(raw, list) or not raw:
        return None
    want = sorted(set(t for t in all_tables if t))
    seen: list[str] = []
    out: list[dict[str, Any]] = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            return None
        refs = c.get("table_refs")
        if not isinstance(refs, list) or not refs:
            return None
        clean = [str(r) for r in refs]
        for r in clean:
            if r not in want:
                return None  # unknown / invented table
            seen.append(r)
        out.append({
            "cluster_id": f"c{i}", "name": str(c.get("name") or f"Cluster {i + 1}")[:60],
            "table_refs": sorted(set(clean)),
            "rationale": str(c.get("rationale") or "")[:200],
            "edited_by_user": False, "source": "advisor",
        })
    if sorted(seen) != want or len(seen) != len(set(seen)):
        return None  # not a partition (missing / duplicate)
    out.sort(key=lambda c: (-len(c["table_refs"]), c["name"]))
    for i, c in enumerate(out):
        c["cluster_id"] = f"c{i}"
    return out
