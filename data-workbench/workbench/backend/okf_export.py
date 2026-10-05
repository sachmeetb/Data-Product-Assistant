"""Open Knowledge Format (OKF v0.1) export.

Projects published data products into an OKF bundle — a directory of markdown
files with YAML frontmatter, cross-linked by plain markdown links (spec:
github.com/GoogleCloudPlatform/knowledge-catalog/okf). Two modes share one
assembly core:

  - single product    → collect_okf_files(settings, uri=<dp uri>)
  - whole marketplace → collect_okf_files(settings)   (products cross-linked by :CONSUMES)

This is a deterministic, lossy-by-design projection of the Neo4j graph the
Workbench already maintains: the graph IS the cross-link structure, and the
prose artifacts (descriptions, grain prose, OSI/QA narratives, deployment
reflection) ARE the doc bodies. Untyped OKF links can't carry our typed edges,
so every cross-link states its relationship in prose AND stashes the edge type
in a custom `relationship:` frontmatter key (OKF consumers must tolerate unknown
keys). Reserved filenames (index.md / log.md) are deliberately avoided — we use
catalog.md / product.md so the bundle is trivially v0.1-conformant.

Read-only; nothing is persisted. Reuses marketplace.PRODUCT_DETAIL +
_shape_detail so OKF tracks the exact shape the marketplace already renders.
"""

import re
from typing import Any, Optional

import yaml


# A latest-deployment-reflection narrative, keyed by contract id. Optional —
# folded into the product doc only when a reflection has been run.
_REFLECTION_QUERY = """\
MATCH (dc:DataContract {id: $contract_id})-[:HAS_DEPLOY_REFL]->(dr:DeploymentReflection)
WITH dr ORDER BY dr.evaluatedAt DESC LIMIT 1
RETURN dr.verdict AS verdict, dr.narrative AS narrative
"""


def _slug(s: str) -> str:
    """Filesystem-safe lowercase path component."""
    return re.sub(r"[^a-z0-9_]+", "-", (s or "").lower()).strip("-") or "unnamed"


def _dir_code(detail: dict) -> str:
    """Per-product bundle subdir name. Contract ids are ``{project_code}-contract``;
    fall back to a slug of the product name when absent."""
    cid = detail.get("contract_id") or ""
    if cid.endswith("-contract"):
        return _slug(cid[: -len("-contract")])
    return _slug(detail.get("name") or detail.get("title") or "product")


def _first_sentence(text: str) -> str:
    """OKF recommends `description` be a single sentence; bodies keep the full text."""
    text = (text or "").strip().replace("\n", " ")
    if not text:
        return ""
    m = re.search(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text)[:240]


def _fm(meta: dict) -> str:
    """Render a YAML frontmatter block. Drops empty values but always keeps the
    spec-required `type` field. Unknown keys are fine — consumers tolerate them."""
    clean = {"type": meta.get("type") or "Concept"}
    for k, v in meta.items():
        if k == "type":
            continue
        if v in (None, "", [], {}):
            continue
        clean[k] = v
    body = yaml.safe_dump(clean, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{body}---\n"


def _doc(meta: dict, *body_parts: str) -> str:
    body = "\n".join(p for p in body_parts if p).strip()
    return _fm(meta) + "\n" + body + "\n"


def _columns_table(columns: list[dict]) -> str:
    if not columns:
        return "_No columns._"
    rows = ["| Column | Type | PK | Sensitivity | Description |",
            "|---|---|---|---|---|"]
    for c in columns:
        name = c.get("logicalName") or c.get("name") or ""
        typ = c.get("logicalType") or c.get("physicalType") or ""
        pk = "✓" if c.get("primaryKey") else ""
        sens = c.get("sensitivity") or ""
        desc = (c.get("description") or "").replace("\n", " ").replace("|", "\\|")
        rows.append(f"| {name} | {typ} | {pk} | {sens} | {desc} |")
    return "\n".join(rows)


def _shape_lines(transform: Optional[dict]) -> str:
    """Render the :DatasetTransform shape (grain, filter, dedupe, joins, scd…)."""
    if not transform:
        return ""
    out: list[str] = []
    if transform.get("grain_prose"):
        out.append(f"- **Grain:** {transform['grain_prose']}")
    if transform.get("filter"):
        out.append(f"- **Filter:** `{transform['filter']}`")
    if transform.get("scd_policy"):
        out.append(f"- **SCD policy:** `{transform['scd_policy']}`")
    if transform.get("grouping_keys"):
        out.append(f"- **Grouped by:** {', '.join(transform['grouping_keys'])}")
    if transform.get("suppressed_columns"):
        out.append(f"- **Suppressed:** {', '.join(transform['suppressed_columns'])}")
    if transform.get("joins"):
        out.append(f"- **Explicit joins:** {len(transform['joins'])} defined")
    if not out:
        return ""
    return "## Shape\n\n" + "\n".join(out)


def _link(name: str, uri: str, uri_to_path: dict[str, str]) -> str:
    """Bundle-relative markdown link to another product when it's in the bundle,
    else a prose reference to its URI (broken links are permitted by OKF)."""
    target = uri_to_path.get(uri)
    if target:
        return f"[{name or uri}](/{target})"
    return f"{name or 'external product'} (`{uri}`)"


def _product_docs(
    ns,
    detail: dict,
    prefix: str,
    uri_to_path: dict[str, str],
) -> dict[str, str]:
    """Build every markdown doc for one product under ``prefix`` (e.g. "" for a
    single-product bundle, "products/<code>/" for the marketplace bundle)."""
    files: dict[str, str] = {}
    name = detail.get("title") or detail.get("name") or "Data Product"
    uri = detail.get("uri") or ""

    # ── Cross-product lineage (the part OKF's links are actually good for) ──
    consumes = detail.get("consumes") or []
    consumed_by = detail.get("consumed_by") or []
    lineage_parts: list[str] = []
    if consumes:
        items = "\n".join(
            f"- Consumes {_link(c.get("name"), c.get("uri"), uri_to_path)}"
            for c in consumes if c.get("uri")
        )
        lineage_parts.append("### Built on\n\n" + items)
    if consumed_by:
        items = "\n".join(
            f"- Consumed by {_link(c.get("name"), c.get("uri"), uri_to_path)}"
            for c in consumed_by if c.get("uri")
        )
        lineage_parts.append("### Feeds\n\n" + items)
    lineage = ("## Lineage\n\n" + "\n\n".join(lineage_parts)) if lineage_parts else ""

    serving = (detail.get("serving") or [{}])[0] if detail.get("serving") else {}
    qa = detail.get("qa_evaluation") or {}

    # ── product.md (overview) ──
    meta = {
        "type": "Data Product",
        "title": name,
        "description": _first_sentence(detail.get("description") or ""),
        "resource": uri,
        "domain": detail.get("domain") or "",
        "product_kind": detail.get("product_kind") or "",
        "tags": list(detail.get("tags") or []),
        "owners": [o.get("email") for o in (detail.get("owners") or []) if o.get("email")],
        "osi_band": detail.get("osi_band") or "",
        "osi_completeness": detail.get("osi_completeness"),
        "serving_mode": serving.get("servingMode") or "",
        "deployment_status": serving.get("deploymentStatus") or "",
        # custom keys (OKF tolerates unknowns) — recover the typed edges a
        # consumer would otherwise lose to OKF's untyped links.
        "consumes": [c.get("uri") for c in consumes if c.get("uri")],
        "consumed_by": [c.get("uri") for c in consumed_by if c.get("uri")],
    }
    overview = [f"# {name}\n"]
    if detail.get("description"):
        overview.append(detail["description"].strip())
    if detail.get("purpose"):
        overview.append(f"\n**Purpose:** {detail['purpose'].strip()}")
    if detail.get("limitations"):
        overview.append(f"\n**Limitations:** {detail['limitations'].strip()}")
    ds_index = [
        f"- [{d.get('name') or d.get('physicalName')}](./datasets/{_slug(d.get('physicalName') or d.get('name'))}.md)"
        for d in (detail.get("datasets") or [])
    ]
    if ds_index:
        overview.append("## Datasets\n\n" + "\n".join(ds_index))
    if (detail.get("quality_rules") or []):
        overview.append(f"## Quality\n\nSee [quality rules](./quality.md) "
                        f"({len(detail['quality_rules'])} rules).")
    if qa.get("narrative"):
        overview.append("## Answerable questions\n\n" + qa["narrative"].strip())
        questions = [q.get("text") for q in (qa.get("questions") or []) if q.get("text")]
        if questions:
            overview.append("\n".join(f"- {q}" for q in questions[:25]))
    if lineage:
        overview.append(lineage)
    files[f"{prefix}product.md"] = _doc(meta, *overview)

    # ── datasets/<phys>.md ──
    for d in (detail.get("datasets") or []):
        phys = d.get("physicalName") or d.get("name") or "dataset"
        d_meta = {
            "type": "Dataset",
            "title": d.get("name") or phys,
            "resource": d.get("uri") or "",
            "physical_name": phys,
            "relationship_kind": d.get("relationshipKind") or "",
            "primary_keys": [c.get("name") for c in (d.get("columns") or []) if c.get("primaryKey")],
        }
        parts = [f"# {d.get('name') or phys}\n"]
        if d.get("description"):
            parts.append(d["description"].strip())
        shape = _shape_lines(d.get("transform"))
        if shape:
            parts.append(shape)
        parts.append("## Columns\n\n" + _columns_table(d.get("columns") or []))
        parts.append(f"\nPart of [{name}](../product.md).")
        files[f"{prefix}datasets/{_slug(phys)}.md"] = _doc(d_meta, *parts)

    # ── quality.md ──
    rules = detail.get("quality_rules") or []
    if rules:
        by_table: dict[str, list[dict]] = {}
        for r in rules:
            by_table.setdefault(r.get("dataset_name") or "(product)", []).append(r)
        parts = [f"# Quality rules — {name}\n"]
        for table, trules in sorted(by_table.items()):
            parts.append(f"## {table}\n")
            for r in trules:
                sev = r.get("severity") or ""
                col = r.get("column_name") or ""
                label = r.get("name") or r.get("rule") or "rule"
                desc = (r.get("description") or "").strip()
                head = f"- **{label}**" + (f" (`{col}`)" if col else "") + (f" — _{sev}_" if sev else "")
                parts.append(head + (f": {desc}" if desc else ""))
        files[f"{prefix}quality.md"] = _doc(
            {"type": "Quality Rules", "title": f"Quality — {name}", "resource": uri}, *parts
        )

    # ── reflection.md (optional) ──
    if detail.get("contract_id"):
        refl = ns.run(_REFLECTION_QUERY, contract_id=detail["contract_id"]).single()
        if refl and (refl.get("narrative") or "").strip():
            r_meta = {"type": "Deployment Reflection", "title": f"Reflection — {name}",
                      "resource": uri, "verdict": refl.get("verdict") or ""}
            files[f"{prefix}reflection.md"] = _doc(
                r_meta, f"# Deployment reflection — {name}\n",
                f"**Verdict:** {refl.get('verdict') or 'n/a'}\n", refl["narrative"].strip(),
                f"\nReflects [{name}](./product.md)."
            )

    return files


def collect_okf_files(settings, uri: Optional[str] = None) -> dict[str, str]:
    """Assemble an OKF bundle as {relative_path: markdown}.

    ``uri`` given  → single-product bundle rooted at product.md.
    ``uri`` None   → whole-marketplace bundle: catalog.md + products/<code>/…,
                     with :CONSUMES rendered as bundle-relative cross-links.
    """
    from .routers import marketplace as mkt

    files: dict[str, str] = {}
    with mkt._neo4j_from_settings(settings) as ns:
        if uri is not None:
            row = ns.run(mkt.PRODUCT_DETAIL, uri=uri).single()
            if not row:
                return files
            detail = mkt._shape_detail(dict(row))
            # Single-product bundle: neighbors aren't in the bundle, so links
            # fall back to prose URIs (empty uri_to_path).
            files.update(_product_docs(ns, detail, "", {}))
            return files

        # ── whole-marketplace bundle ──
        # Mirror the marketplace listing exactly: it applies NO status filter,
        # so the consumer view shows BOTH source-aligned AND consumer-aligned
        # products (incl. approved / in-engineering ones whose dp.status is still
        # 'draft'). ALL_PRODUCTS already version-pins to the latest published/
        # superseded contract version, so no in-flight draft schema leaks. We
        # only require a resolvable uri.
        listed = [dict(r) for r in ns.run(mkt.ALL_PRODUCTS, owned_by=None)]
        to_export = [r for r in listed if r.get("uri")]
        # Map every product uri → its product.md path so cross-links resolve.
        uri_to_path: dict[str, str] = {}
        details: list[dict] = []
        for r in to_export:
            row = ns.run(mkt.PRODUCT_DETAIL, uri=r["uri"]).single()
            if not row:
                continue
            detail = mkt._shape_detail(dict(row))
            code = _dir_code(detail)
            uri_to_path[detail.get("uri")] = f"products/{code}/product.md"
            details.append((code, detail))

        for code, detail in details:
            files.update(_product_docs(ns, detail, f"products/{code}/", uri_to_path))

        # ── catalog.md — the marketplace index, grouped by domain ──
        by_domain: dict[str, list[tuple[str, dict]]] = {}
        for code, detail in details:
            by_domain.setdefault(detail.get("domain") or "(uncategorized)", []).append((code, detail))
        n_consumer = sum(1 for _, d in details if (d.get("product_kind") or "") == "consumer")
        n_source = len(details) - n_consumer
        parts = ["# Data Product Catalog\n",
                 f"{len(details)} data products ({n_source} source-aligned, "
                 f"{n_consumer} consumer-aligned) across {len(by_domain)} domains.\n"]
        for domain in sorted(by_domain):
            parts.append(f"## {domain}\n")
            for code, detail in sorted(by_domain[domain], key=lambda x: x[1].get("name") or ""):
                nm = detail.get("title") or detail.get("name") or code
                kind = detail.get("product_kind") or ""
                state = detail.get("lifecycle_state") or ""
                desc = _first_sentence(detail.get("description") or "")
                line = f"- [{nm}](/products/{code}/product.md)"
                tags = " ".join(t for t in (kind, state if state and state != "published" else "") if t)
                if tags:
                    line += f" — _{tags}_"
                if desc:
                    line += f": {desc}"
                parts.append(line)
        files["catalog.md"] = _doc(
            {"type": "Data Product Catalog", "title": "Data Product Catalog",
             "product_count": len(details)}, *parts
        )
    return files
