#!/usr/bin/env python3
"""Summarise failing GX expectations into a markdown report.

Reads ``{project_dir}/dq_tests_gx/results/gx_results_*.json``, uses the most
recent run, and for every failed expectation surfaces the top-N unexpected
values (already captured by the GX runner) plus column/rule context from the
project's Neo4j graph. Writes:

  {project_dir}/dq_tests_gx/failure_analysis.md
  {project_dir}/dq_tests_gx/failure_analysis.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    from neo4j import GraphDatabase
except Exception:  # pragma: no cover — skill runs under the workbench env
    GraphDatabase = None  # type: ignore


DEFAULT_MAX_SAMPLES = 10


# ── Neo4j queries ────────────────────────────────────────────────────────────

# Pull PII flag, description text, and rule metadata for a given (schema, table,
# column). All three contributions are best-effort — any that is missing just
# leaves the corresponding context empty.
COLUMN_CONTEXT_QUERY = """\
MATCH (prj:Project {projectCode: $project_code})-[:HAS_CATALOG]->(cat:Catalog)
      -[:DCAT_DATASET]->(ds:Dataset {schema: $schema, name: $table})
OPTIONAL MATCH (ds)-[:HAS_COLUMN]->(col:Column {name: $column})
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true})
WITH col, cd
OPTIONAL MATCH (col)-[:HAS_SHAPE]->(ps:PropertyShape)
RETURN
  col.dataType AS data_type,
  col.pii      AS pii,
  cd.text      AS description,
  collect(DISTINCT {
    ruleType:   ps.ruleType,
    ruleSource: ps.ruleSource,
    status:     ps.status,
    operator:   ps.operator,
    value:      ps.value,
    pattern:    ps.pattern,
    values:     ps.allowedValues
  }) AS rules
LIMIT 1
"""

# Fallback lookup when there is no :Project (e.g. legacy graphs). Less precise —
# may pull a column from another project that happens to share schema.table.col,
# but we never leak its pii flag silently: _column_context() only uses the first
# row when project-scoped query yields nothing.
COLUMN_CONTEXT_FALLBACK = """\
MATCH (ds:Dataset {schema: $schema, name: $table})-[:HAS_COLUMN]->(col:Column {name: $column})
OPTIONAL MATCH (col)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true})
OPTIONAL MATCH (col)-[:HAS_SHAPE]->(ps:PropertyShape)
RETURN
  col.dataType AS data_type,
  col.pii      AS pii,
  cd.text      AS description,
  collect(DISTINCT {
    ruleType:   ps.ruleType,
    ruleSource: ps.ruleSource,
    status:     ps.status,
    operator:   ps.operator,
    value:      ps.value,
    pattern:    ps.pattern,
    values:     ps.allowedValues
  }) AS rules
LIMIT 1
"""


# Product (dprod) context — for a product-DQ suite the failing "table" is a
# deployed vw_<name> view (a lossy transform of the dataset physicalName), so we
# match the :DProdColumn by name within the product's contract rather than by
# schema.table. Best-effort like the catalog lookups.
COLUMN_CONTEXT_DPROD = """\
MATCH (dc:DataContract)-[:MATERIALISES_AS]->(dp:DProdDataProduct)
WHERE dc.id = $contract_id
MATCH (dp)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn {name: $column})
OPTIONAL MATCH (pc)<-[:ON_DPROD_COLUMN]-(ps:PropertyShape)
RETURN
  pc.dataType         AS data_type,
  coalesce(pc.pii, false) AS pii,
  pc.description      AS description,
  collect(DISTINCT {
    ruleType:   ps.ruleType,
    ruleSource: ps.ruleSource,
    status:     ps.status,
    operator:   ps.operator,
    value:      ps.value,
    pattern:    ps.pattern,
    values:     ps.allowedValues
  }) AS rules
LIMIT 1
"""


def _connect(args) -> Any:
    if GraphDatabase is None:
        return None
    try:
        driver = GraphDatabase.driver(
            f"bolt://{args.host}:{args.bolt_port}",
            auth=(args.username, args.password),
        )
        driver.verify_connectivity()
        return driver
    except Exception as exc:
        print(f"[warn] Neo4j connection failed: {exc}. Report will omit column context.",
              file=sys.stderr)
        return None


def _column_context(driver, database: str, project_code: str | None,
                    schema: str, table: str, column: str,
                    source_mode: str = "catalog",
                    target_contract: str | None = None) -> dict:
    """Return ``{data_type, pii, description, rules}`` for a column, best effort.

    ``source_mode='dprod'`` pulls the context from the product's :DProdColumn
    (contract rules) instead of the source :Column — for a Product-DQ suite."""
    if driver is None or not column:
        return {"data_type": None, "pii": False, "description": None, "rules": []}
    try:
        with driver.session(database=database) as s:
            row = None
            if source_mode == "dprod":
                contract_id = target_contract or (
                    (project_code + "-contract") if project_code else None)
                if contract_id:
                    row = s.run(COLUMN_CONTEXT_DPROD,
                                contract_id=contract_id, column=column).single()
                # No catalog fallback in dprod mode — a dprod product has no :Column.
                if row is None:
                    return {"data_type": None, "pii": False, "description": None, "rules": []}
            if row is None and project_code:
                row = s.run(COLUMN_CONTEXT_QUERY,
                            project_code=project_code,
                            schema=schema, table=table, column=column).single()
            if row is None and source_mode != "dprod":
                row = s.run(COLUMN_CONTEXT_FALLBACK,
                            schema=schema, table=table, column=column).single()
            if row is None:
                return {"data_type": None, "pii": False, "description": None, "rules": []}
            rules = [r for r in (row["rules"] or []) if r.get("ruleType")]
            return {
                "data_type": row["data_type"],
                "pii": bool(row["pii"]),
                "description": row["description"],
                "rules": rules,
            }
    except Exception as exc:
        print(f"[warn] graph lookup failed for {schema}.{table}.{column}: {exc}",
              file=sys.stderr)
        return {"data_type": None, "pii": False, "description": None, "rules": []}


# ── Result-file discovery ────────────────────────────────────────────────────

def _most_recent_result(results_dir: Path) -> Path | None:
    candidates = sorted(results_dir.glob("gx_results_*.json"))
    return candidates[-1] if candidates else None


def _parse_table_name(full: str) -> tuple[str, str]:
    if "." in full:
        schema, table = full.split(".", 1)
        return schema, table
    return "", full


# ── Rendering ────────────────────────────────────────────────────────────────

def _format_sample_value(v: Any) -> str:
    if v is None:
        return "`null`"
    if isinstance(v, str):
        # Preserve whitespace so the reader can spot trailing spaces etc.
        return f"`{v}`"
    return f"`{v}`"


def _render_rule_context(rules: Iterable[dict]) -> str:
    bits = []
    for r in rules:
        rt = r.get("ruleType") or "rule"
        op = r.get("operator") or ""
        val = r.get("value")
        pat = r.get("pattern")
        vals = r.get("values")
        source = r.get("ruleSource") or ""
        chunk = rt
        if op and val is not None:
            chunk += f" {op} {val}"
        elif pat:
            chunk += f" matches `{pat}`"
        elif vals:
            sample = ", ".join(str(v) for v in list(vals)[:5])
            more = f" (+{len(vals) - 5} more)" if len(vals) > 5 else ""
            chunk += f" ∈ {{{sample}}}{more}"
        if source:
            chunk += f" _(source: {source})_"
        bits.append(chunk)
    return "; ".join(bits)


def _render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# DQ Failure Analysis — {report['project_code'] or '(no project)'}")
    lines.append("")
    lines.append(f"_Generated {report['generated_at']}_  ")
    lines.append(f"_Source: `{report['source_file']}`_")
    lines.append("")

    summary = report["summary"]
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Tables evaluated: **{summary['tables']}**")
    lines.append(f"- Expectations: **{summary['evaluated']}** evaluated "
                 f"({summary['successful']} passed, **{summary['unsuccessful']} failed**)")
    if summary["unsuccessful"] == 0:
        lines.append("")
        lines.append("No failures in the latest run.")
        return "\n".join(lines)

    lines.append(f"- Failed expectations by column: **{summary['distinct_failed_columns']}** distinct columns affected")
    lines.append("")

    for tbl in report["tables"]:
        if not tbl["failed"]:
            continue
        lines.append(f"## `{tbl['table']}`")
        lines.append("")
        stats = tbl["statistics"]
        lines.append(f"{stats.get('successful', 0)} passed · "
                     f"**{stats.get('unsuccessful', 0)} failed** · "
                     f"{stats.get('evaluated', 0)} evaluated")
        lines.append("")

        for fail in tbl["failed"]:
            col = fail.get("column") or "(table-level)"
            header = f"### `{col}` — {fail['expectation_type']}"
            lines.append(header)
            lines.append("")

            ctx = fail.get("context") or {}
            meta_bits = []
            if ctx.get("data_type"):
                meta_bits.append(f"type `{ctx['data_type']}`")
            if ctx.get("pii"):
                meta_bits.append("**PII**")
            if meta_bits:
                lines.append(" · ".join(meta_bits))
                lines.append("")
            if ctx.get("description"):
                lines.append(f"> {ctx['description']}")
                lines.append("")
            if ctx.get("rules"):
                rendered = _render_rule_context(ctx["rules"])
                if rendered:
                    lines.append(f"**Rule:** {rendered}")
                    lines.append("")

            res = fail.get("result") or {}
            ec = res.get("element_count")
            uc = res.get("unexpected_count")
            if ec is not None or uc is not None:
                pct = ""
                if ec and uc is not None and ec > 0:
                    pct = f" ({(uc / ec) * 100:.2f}%)"
                lines.append(f"**{uc or 0}** unexpected of **{ec or 0}** rows{pct}")
                lines.append("")

            samples = fail.get("samples") or []
            if ctx.get("pii") and samples:
                lines.append(f"_Top {len(samples)} unexpected values redacted — column marked PII._")
                total = sum((s.get("count") or 0) for s in samples)
                lines.append(f"_(samples cover {total} rows)_")
                lines.append("")
            elif samples:
                lines.append(f"Top unexpected values (count):")
                lines.append("")
                lines.append("| Value | Occurrences |")
                lines.append("| --- | ---: |")
                for s in samples:
                    v = _format_sample_value(s.get("value"))
                    lines.append(f"| {v} | {s.get('count', 1)} |")
                lines.append("")
            elif res.get("observed_value") is not None:
                lines.append(f"Observed value: `{res['observed_value']}`")
                lines.append("")
            else:
                lines.append("_No sample failing values were captured for this expectation._")
                lines.append("")

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def analyze(project_dir: Path, driver, database: str,
            project_code: str | None, max_samples: int,
            results_dir: Path | None = None,
            source_mode: str = "catalog",
            target_contract: str | None = None) -> dict:
    if results_dir is None:
        results_dir = project_dir / "dq_tests_gx" / "results"
    src = _most_recent_result(results_dir)
    if src is None:
        raise FileNotFoundError(f"No result file found under {results_dir}")

    with open(src) as fh:
        raw = json.load(fh)

    report_tables: list[dict] = []
    tot_eval = tot_ok = tot_fail = 0
    failed_cols: set[str] = set()

    for t in raw.get("tables") or []:
        schema, table = _parse_table_name(t.get("table") or "")
        stats = t.get("statistics") or {}
        tot_eval += stats.get("evaluated", 0)
        tot_ok += stats.get("successful", 0)
        tot_fail += stats.get("unsuccessful", 0)

        failed = []
        for r in t.get("results") or []:
            if r.get("success"):
                continue
            col = r.get("column") or ""
            ctx = _column_context(driver, database, project_code, schema, table, col,
                                  source_mode=source_mode, target_contract=target_contract) if col else {
                "data_type": None, "pii": False, "description": None, "rules": []
            }
            samples = (r.get("samples") or [])[:max_samples]
            failed.append({
                "expectation_type": r.get("expectation_type") or "",
                "column": col,
                "result": r.get("result") or {},
                "samples": samples,
                "meta": r.get("meta") or {},
                "context": ctx,
            })
            if col:
                failed_cols.add(f"{schema}.{table}.{col}")

        report_tables.append({
            "table": t.get("table"),
            "statistics": stats,
            "failed": failed,
        })

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source_file": str(src.relative_to(project_dir)) if project_dir in src.parents else str(src),
        "project_code": project_code,
        "summary": {
            "tables": len(raw.get("tables") or []),
            "evaluated": tot_eval,
            "successful": tot_ok,
            "unsuccessful": tot_fail,
            "distinct_failed_columns": len(failed_cols),
        },
        "tables": report_tables,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Summarise failing DQ test results into a markdown report.")
    p.add_argument("--project-dir", required=True, help="Project directory (cwd for the stage)")
    p.add_argument("--project-code", default=None, help="Project code for Neo4j scoping")
    p.add_argument("--results-dir", default=None,
                   help="Directory containing result JSON files (default: <project-dir>/dq_tests_gx/results)")
    p.add_argument("--output-dir", default=None,
                   help="Directory to write failure_analysis.md/.json (default: <project-dir>/dq_tests_gx)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--bolt-port", type=int, default=7687)
    p.add_argument("--username", default="neo4j")
    p.add_argument("--password", default="")
    p.add_argument("--database", default="neo4j")
    p.add_argument("--max-samples", type=int, default=DEFAULT_MAX_SAMPLES,
                   help="Max unexpected-value samples to render per expectation (default: 10)")
    p.add_argument("--no-neo4j", action="store_true",
                   help="Skip Neo4j lookups (report will have no column/rule context)")
    p.add_argument("--source-mode", choices=["catalog", "dprod"], default="catalog",
                   help="Column context home: 'catalog' (:Column) or 'dprod' (:DProdColumn / product contract).")
    p.add_argument("--target-contract", default=None,
                   help="Contract id ({project_code}-contract) for --source-mode dprod (defaults from --project-code).")
    args = p.parse_args()

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(f"error: project dir not found: {project_dir}", file=sys.stderr)
        return 2

    results_dir = Path(args.results_dir).resolve() if args.results_dir else None
    out_dir = Path(args.output_dir).resolve() if args.output_dir else (project_dir / "dq_tests_gx")

    driver = None if args.no_neo4j else _connect(args)
    try:
        report = analyze(project_dir, driver, args.database, args.project_code,
                         args.max_samples, results_dir=results_dir,
                         source_mode=args.source_mode, target_contract=args.target_contract)
    finally:
        if driver is not None:
            driver.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "failure_analysis.json").write_text(json.dumps(report, indent=2, default=str))

    md = _render_markdown(report)
    (out_dir / "failure_analysis.md").write_text(md)

    print(f"Wrote {out_dir / 'failure_analysis.md'}")
    print(f"  tables: {report['summary']['tables']}")
    print(f"  expectations: {report['summary']['evaluated']} "
          f"({report['summary']['successful']} passed, "
          f"{report['summary']['unsuccessful']} failed)")
    print(f"  distinct failed columns: {report['summary']['distinct_failed_columns']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
