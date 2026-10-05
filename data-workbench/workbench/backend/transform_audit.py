"""Read-only portfolio audit (Phase 4 of transform-portability.md).

Dry-run compiles every data product's mappings across ALL projects, for each
served platform, and emits a diagnostics report of which mappings fail / need
review. This runs BEFORE any enforcement (Phase 6) so the portfolio can be
cleaned first — turning the compiler gate on against a dirty portfolio would
block existing deployed products.

Nothing is written and nothing is deployed: it invokes the generator (which only
reads Neo4j) via `transform_preflight.preflight_product` and aggregates the
`CompileResult`s. The pure aggregation (`build_audit_report`) + markdown render
are unit-tested without Neo4j; `run_portfolio_audit` needs a live driver.
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable, Optional

from .dialect_sql import CompileResult
from .transform_preflight import preflight_product

# The served native-view platforms the audit compiles against.
SERVED_PLATFORMS = ["postgres", "databricks", "snowflake", "bigquery", "mysql"]

# Every data product + its owning project + the view schema to compile under.
_PRODUCTS_QUERY = """\
MATCH (p:Project)-[:HAS_CONTRACT]->(dc:DataContract)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
RETURN p.projectCode        AS project_code,
       dp.uri               AS product_uri,
       coalesce(dc.productKind, '') AS product_kind
ORDER BY project_code, product_uri
"""


def build_audit_report(entries: Iterable[tuple]) -> dict:
    """Aggregate audit entries into a report.

    ``entries``: iterable of ``(project_code, product_uri, platform,
    CompileResult)``. Pure — no I/O — so it is directly unit-testable.
    """
    products: dict[str, dict] = {}
    by_platform_totals: dict[str, dict] = {p: {"compilations": 0, "with_errors": 0, "errors": 0} for p in SERVED_PLATFORMS}
    catalog_version: Optional[str] = None
    total_errors = 0

    for project_code, product_uri, platform, result in entries:
        if catalog_version is None and result.catalog_version:
            catalog_version = result.catalog_version
        key = product_uri
        prod = products.setdefault(key, {
            "project_code": project_code,
            "product_uri": product_uri,
            "by_platform": {},
            "error_count": 0,
        })
        err_dicts = [e.to_dict() for e in result.errors]
        warn_dicts = [w.to_dict() for w in result.warnings]
        prod["by_platform"][platform] = {
            "ok": result.ok,
            "error_count": len(err_dicts),
            "warning_count": len(warn_dicts),
            "errors": err_dicts,
            "warnings": warn_dicts,
        }
        prod["error_count"] += len(err_dicts)
        total_errors += len(err_dicts)
        pt = by_platform_totals.setdefault(platform, {"compilations": 0, "with_errors": 0, "errors": 0})
        pt["compilations"] += 1
        pt["errors"] += len(err_dicts)
        if err_dicts:
            pt["with_errors"] += 1

    product_list = sorted(products.values(), key=lambda d: (-d["error_count"], d["product_uri"]))
    products_with_errors = sum(1 for p in product_list if p["error_count"] > 0)

    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "catalog_version": catalog_version,
        "clean": total_errors == 0,
        "summary": {
            "products": len(product_list),
            "products_with_errors": products_with_errors,
            "total_errors": total_errors,
            "by_platform": by_platform_totals,
        },
        "products": product_list,
    }


def render_markdown(report: dict) -> str:
    """Human-readable portfolio audit report."""
    s = report["summary"]
    lines = [
        "# Transform portability — portfolio audit",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Capability catalog: {report.get('catalog_version') or 'n/a'}",
        f"- Products: {s['products']}  ·  with errors: {s['products_with_errors']}  ·  total errors: {s['total_errors']}",
        f"- Result: {'✅ CLEAN — safe to enable enforcement' if report['clean'] else '⚠️ ERRORS — remediate before enforcing'}",
        "",
        "## Per-platform",
        "",
        "| platform | compilations | products w/ errors | errors |",
        "|---|---|---|---|",
    ]
    for platform in SERVED_PLATFORMS:
        pt = s["by_platform"].get(platform, {"compilations": 0, "with_errors": 0, "errors": 0})
        lines.append(f"| {platform} | {pt['compilations']} | {pt['with_errors']} | {pt['errors']} |")
    lines.append("")

    failing = [p for p in report["products"] if p["error_count"] > 0]
    if not failing:
        lines.append("No failing products. 🎉")
        return "\n".join(lines) + "\n"

    lines.append("## Failing products")
    lines.append("")
    for p in failing:
        lines.append(f"### {p['product_uri']}  ({p['project_code']})")
        for platform, info in sorted(p["by_platform"].items()):
            if not info["errors"]:
                continue
            lines.append(f"- **{platform}** — {info['error_count']} error(s):")
            for e in info["errors"]:
                loc = e.get("product_col") or e.get("mapping_uri") or ""
                fix = f" → {e['remediation']}" if e.get("remediation") else ""
                lines.append(f"    - `{loc}` [{e['code']}] {e['message']}{fix}")
        lines.append("")
    return "\n".join(lines) + "\n"


def run_portfolio_audit(
    driver,
    database: str,
    *,
    platforms: Optional[list[str]] = None,
    view_schema: str = "public",
) -> dict:
    """Compile every data product across all projects for each served platform.
    Read-only. Requires a live Neo4j driver."""
    platforms = platforms or SERVED_PLATFORMS
    with driver.session(database=database) as session:
        product_rows = [dict(r) for r in session.run(_PRODUCTS_QUERY)]

    entries: list[tuple] = []
    for row in product_rows:
        for platform in platforms:
            try:
                result = preflight_product(
                    driver, database, row["product_uri"],
                    view_schema=view_schema, platform=platform,
                )
            except Exception as e:  # a broken product must not abort the audit
                result = CompileResult(
                    sql=None, catalog_version=None,
                    errors=[_audit_error(f"preflight raised: {type(e).__name__}: {e}")],
                )
            entries.append((row.get("project_code"), row["product_uri"], platform, result))
    return build_audit_report(entries)


def _audit_error(message: str):
    from .dialect_sql import TransformDiagnostic
    return TransformDiagnostic("error", "audit_error", message)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json
    from neo4j import GraphDatabase

    ap = argparse.ArgumentParser(description="Read-only transform-portability portfolio audit.")
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="your_password")
    ap.add_argument("--database", default="neo4j")
    ap.add_argument("--platforms", default=",".join(SERVED_PLATFORMS),
                    help="comma-separated served platforms")
    ap.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = ap.parse_args(argv)

    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    try:
        report = run_portfolio_audit(
            driver, args.database,
            platforms=[p.strip() for p in args.platforms.split(",") if p.strip()],
        )
    finally:
        driver.close()

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(report))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
