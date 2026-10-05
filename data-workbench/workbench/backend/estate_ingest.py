"""Estate ingestion: CSV rows → per-project estate YAML + gap report.

The upstream source of truth is a metadata DATABASE we don't control; for now
its export arrives as one flat CSV (one row per estate object, code artifacts
embedded as text/base64 columns). This module is deliberately split so the CSV
is just a transport:

    parse_csv(bytes)        → list[dict]      (header-driven, alias-tolerant)
    normalize_rows(rows, …) → (estate_doc, gap_report)   ← transport-agnostic
    write_estate(...)       → projects/{code}/modernization/{estate.yaml, gap_report.json}

A future direct-DB extractor calls normalize_rows() with cursor rows — no
format change. The persisted estate_doc is EXACTLY the shape of the demo
fixture (playbook/discovery/estate.yaml) so every downstream
consumer (inventory endpoint, migration planning, spawn, code conversion)
works unchanged once loaders resolve the per-project file first.

Import is LENIENT: rows are never rejected for missing fields — safe defaults
are synthesized and every degradation is recorded in the gap report.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import BASE_PROJECT_DIR

ESTATE_SUBDIR = "modernization"

# ── column aliases (matched on lowercased, non-alphanumeric-stripped header) ──
_COLUMN_ALIASES: dict[str, str] = {
    "id": "id", "objectid": "id", "objid": "id",
    "name": "name", "displayname": "name",
    "objectname": "object_name",
    "type": "type", "nodetype": "type",
    "objecttype": "object_type",
    "domain": "domain", "application": "application", "applicationname": "application",
    "instance": "instance", "instancename": "instance",
    "database": "database", "databasename": "database",
    "schema": "schema", "schemaname": "schema",
    "team": "team", "owner": "team",
    "platform": "platform", "system": "platform",
    "legacyplatform": "legacy_platform", "sourceplatform": "legacy_platform",
    "sizegb": "size_gb", "size": "size_gb",
    "rowcount": "row_count", "rows": "row_count",
    "querycount90d": "query_count_90d", "queries90d": "query_count_90d",
    "consumercount": "consumer_count", "consumers": "consumer_count",
    "active": "active", "activeinactive": "active",
    "phipii": "phi_pii", "pii": "phi_pii",
    "adhoc": "adhoc", "adhocflag": "adhoc",
    "compatibility": "compatibility", "compatibilityflag": "compatibility",
    "recommendation": "recommendation",
    "recommendationreason": "recommendation_reason",
    "target": "target", "targetplatform": "target",
    "complexity": "complexity",
    "migrationapproach": "migration_approach",
    "disposition": "disposition",
    "upstream": "upstream", "reads": "upstream", "lineage": "upstream",
    "columns": "columns", "samplecolumns": "columns",
    "code": "code", "ddl": "code", "definition": "code", "sourcecode": "code",
    "codeencoding": "code_encoding",
    "codelanguage": "code_language", "language": "code_language",
    "lastrun": "last_run",
    "status": "status", "freshness": "status",
}

# Canonical row fields the normalizer consumes, with one-line descriptions.
# Served by the import-preview endpoint (mapping UI dropdowns) and fed to the
# estate-intake-mapper skill so it can map arbitrary export headers onto them.
CANONICAL_FIELDS: list[dict[str, str]] = [
    {"field": "id", "description": "Unique object identifier (synthesized from name when absent)"},
    {"field": "name", "description": "Display name of the object"},
    {"field": "object_name", "description": "Physical/technical object name"},
    {"field": "type", "description": "Object kind: table|view|query|snapshot|report (job/dashboard etc. aliased)"},
    {"field": "object_type", "description": "Raw platform-specific object type label"},
    {"field": "domain", "description": "Business domain"},
    {"field": "application", "description": "Owning application"},
    {"field": "instance", "description": "Platform instance / server name"},
    {"field": "database", "description": "Database name"},
    {"field": "schema", "description": "Schema name"},
    {"field": "team", "description": "Owning team / owner"},
    {"field": "platform", "description": "Platform / system (Teradata, Hive, SAS, ...)"},
    {"field": "legacy_platform", "description": "Source platform for code objects being migrated"},
    {"field": "size_gb", "description": "Storage size in GB"},
    {"field": "row_count", "description": "Row count"},
    {"field": "query_count_90d", "description": "Query count over the last 90 days"},
    {"field": "consumer_count", "description": "Number of distinct consumers/users"},
    {"field": "active", "description": "Active flag (y/n, true/false)"},
    {"field": "phi_pii", "description": "Contains PHI/PII (y/n)"},
    {"field": "adhoc", "description": "Ad-hoc object flag (y/n)"},
    {"field": "compatibility", "description": "Target-compatibility assessment"},
    {"field": "recommendation", "description": "Disposition recommendation label"},
    {"field": "recommendation_reason", "description": "Why the recommendation was made"},
    {"field": "target", "description": "Target platform for migration/modernization"},
    {"field": "complexity", "description": "Migration complexity (Low/Medium/High)"},
    {"field": "migration_approach", "description": "Planned migration approach/tooling"},
    {"field": "disposition", "description": "Disposition: modernize|migrate|retire|remain"},
    {"field": "upstream", "description": "Upstream object ids this object reads from (';'-delimited)"},
    {"field": "columns", "description": "Column metadata: JSON array or 'name:type;name:type'"},
    {"field": "code", "description": "DDL/query source code, plain text or base64"},
    {"field": "code_encoding", "description": "Encoding of the code cell (plain|base64)"},
    {"field": "code_language", "description": "Language of the code cell (sql|hql|py|scala)"},
    {"field": "last_run", "description": "Last run/refresh timestamp"},
    {"field": "status", "description": "Data freshness: fresh|warn|stale"},
]
_CANONICAL_FIELD_SET = {f["field"] for f in CANONICAL_FIELDS}

_VALID_TYPES = {"table", "query", "view", "snapshot", "report", "product", "database"}
_TYPE_ALIASES = {
    "job": "query", "procedure": "query", "script": "query", "workflow": "query",
    "macro": "query", "sql": "query", "etl": "query",
    "dashboard": "report", "reportpage": "report",
    "materializedview": "view", "mview": "view",
    "file": "table", "dataset": "table",
}
_VALID_DISPOSITIONS = {"modernize", "migrate", "retire", "remain"}
_VALID_STATUS = {"fresh", "warn", "stale"}

_LANG_EXT = {
    "sql": ".sql", "bteq": ".sql", "teradata": ".sql", "hql": ".hql", "hive": ".hql",
    "py": ".py", "python": ".py", "pyspark": ".py", "scala": ".scala",
}

_TRUE = {"y", "yes", "true", "1", "active", "t"}
_FALSE = {"n", "no", "false", "0", "inactive", "f", ""}


def _canon_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", (s or "").strip().lower()).strip("-") or "obj"


def parse_csv(
    data: bytes,
    header_mapping: dict[str, str] | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Header-driven CSV → list of canonical-keyed row dicts.

    Returns (rows, unmapped_headers). Headers are matched case/space/
    punctuation-insensitively against _COLUMN_ALIASES; unrecognized headers
    are kept out of the rows but reported so we notice when the real DB
    export carries more than we consume.

    `header_mapping` (raw header → canonical field, from the import-preview
    confirm step) takes precedence over the alias table; an empty-string /
    unknown-field value means "ignore this header" (not reported as unmapped).
    """
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    explicit = header_mapping or {}
    mapping: dict[str, str] = {}
    unmapped: list[str] = []
    for h in headers:
        if h in explicit:
            canon = explicit[h]
            if canon in _CANONICAL_FIELD_SET:
                mapping[h] = canon
            continue  # explicitly ignored — not a gap
        canon = _COLUMN_ALIASES.get(_canon_header(h))
        if canon:
            mapping[h] = canon
        elif h and h.strip():
            unmapped.append(h)
    rows: list[dict[str, str]] = []
    for raw in reader:
        row: dict[str, str] = {}
        for h, key in mapping.items():
            v = (raw.get(h) or "").strip()
            if v != "":
                row[key] = v
        if row:
            rows.append(row)
    return rows, unmapped


# ── field parsers ──────────────────────────────────────────────

def _parse_bool(v: Any, default: bool) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return default


def _parse_num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _split_list(v: str) -> list[str]:
    return [p.strip() for p in re.split(r"[;|,]", v or "") if p.strip()]


def _parse_columns(v: Any) -> list[dict[str, Any]] | None:
    """`columns` cell: JSON array of {name,type,...} OR delimited name:type pairs."""
    if not v:
        return None
    if isinstance(v, list):
        return [c for c in v if isinstance(c, dict) and c.get("name")] or None
    s = str(v).strip()
    if s.startswith("["):
        try:
            arr = json.loads(s)
            return [c for c in arr if isinstance(c, dict) and c.get("name")] or None
        except json.JSONDecodeError:
            return None
    cols = []
    for part in _split_list(s):
        name, _, typ = part.partition(":")
        if name.strip():
            cols.append({"name": name.strip(), "type": typ.strip() or "unknown"})
    return cols or None


def _decode_code(raw: str, encoding_hint: str | None) -> str:
    """Code cell: plain text or base64 (explicit hint, or auto-detected when the
    cell has no SQL-ish characters and decodes cleanly to text)."""
    s = (raw or "").strip()
    hint = (encoding_hint or "").strip().lower()
    if hint in ("base64", "b64", "bytes"):
        try:
            return base64.b64decode(s, validate=True).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            return s
    # auto-detect: long single-token base64-looking blob with no whitespace
    if len(s) > 40 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", s) and " " not in s.strip():
        try:
            decoded = base64.b64decode(s, validate=True).decode("utf-8")
            if decoded.isprintable() or "\n" in decoded:
                return decoded
        except (binascii.Error, ValueError, UnicodeDecodeError):
            pass
    return s


# ── normalization core (transport-agnostic) ────────────────────

def normalize_rows(
    rows: list[dict[str, Any]],
    project_code: str,
    sources_dir: Path,
    *,
    unmapped_headers: list[str] | None = None,
    scenario: str | None = None,
    description: str | None = None,
    domain: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rows (from CSV today, a DB cursor tomorrow) → (estate_doc, gap_report).

    estate_doc matches the demo fixture shape: {scenario, description, objects[]}
    with lineage as per-object `reads[]`. Never raises on bad data — degrades
    with defaults + gap entries instead.
    """
    gaps_by_obj: dict[str, list[dict[str, str]]] = {}
    global_gaps: list[dict[str, str]] = []

    def gap(oid: str, code: str, message: str, severity: str = "warn") -> None:
        gaps_by_obj.setdefault(oid, []).append({"code": code, "severity": severity, "message": message})

    if unmapped_headers:
        global_gaps.append({
            "code": "unmapped_columns",
            "message": f"CSV columns not consumed by the importer: {', '.join(unmapped_headers)}",
        })

    objects: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    edges_dropped = 0

    for i, row in enumerate(rows):
        # ── identity ──
        oid = str(row.get("id") or "").strip()
        if not oid:
            oid = f"obj-{_slug(str(row.get('object_name') or row.get('name') or ''))}-{i + 1}"
            gap(oid, "no_id", "Row had no id — synthesized from object_name.")
        if oid in seen_ids:
            gap(oid, "duplicate_id", f"Duplicate id '{oid}' — later row skipped.", "error")
            continue
        seen_ids.add(oid)

        name = str(row.get("name") or row.get("object_name") or oid)
        object_name = str(row.get("object_name") or row.get("name") or oid)

        # ── type ──
        raw_type = str(row.get("type") or "").strip().lower()
        typ = raw_type if raw_type in _VALID_TYPES else _TYPE_ALIASES.get(_canon_header(raw_type), "")
        if not typ:
            typ = "table"
            if raw_type:
                gap(oid, "unknown_type", f"Unrecognized type '{raw_type}' — defaulted to 'table'.")
            else:
                gap(oid, "no_type", "No type — defaulted to 'table'.")
        object_type = str(row.get("object_type") or typ.upper().replace(" ", "_"))

        # ── disposition / status ──
        raw_disp = str(row.get("disposition") or "").strip().lower()
        disposition = raw_disp if raw_disp in _VALID_DISPOSITIONS else "remain"
        if raw_disp and disposition != raw_disp:
            gap(oid, "unknown_disposition", f"Unrecognized disposition '{raw_disp}' — defaulted to 'remain'.")
        elif not raw_disp:
            gap(oid, "no_disposition", "No disposition — defaulted to 'remain' (Out of Scope).")
        raw_status = str(row.get("status") or "").strip().lower()
        status = raw_status if raw_status in _VALID_STATUS else "fresh"

        # ── numbers / booleans ──
        size_gb = _parse_num(row.get("size_gb")) or 0.0
        row_count = _parse_num(row.get("row_count"))
        queries = _parse_num(row.get("query_count_90d"))
        consumers = _parse_num(row.get("consumer_count"))
        active = _parse_bool(row.get("active"), True)
        phi_pii = _parse_bool(row.get("phi_pii"), False)
        adhoc = _parse_bool(row.get("adhoc"), False)

        # ── metric (display) from whatever stats exist ──
        parts = []
        if row_count is not None:
            parts.append(f"{_human(row_count)} rows")
        if queries is not None:
            parts.append(f"{_human(queries)} q/90d")
        if consumers is not None:
            parts.append(f"{int(consumers)} users")
        if size_gb:
            parts.append(f"{size_gb:g} GB")
        metric = " · ".join(parts) or "—"

        # ── lineage (validated after all ids are known) ──
        reads = _split_list(str(row.get("upstream") or ""))

        # ── sample columns ──
        sample_columns = _parse_columns(row.get("columns"))
        if sample_columns is None and typ in ("table", "view", "report", "snapshot"):
            gap(oid, "no_sample_columns",
                "No column metadata — product comparison / reference-model scoring degraded.")

        # ── code artifact ──
        sample_source: str | None = None
        code_raw = row.get("code")
        language = str(row.get("code_language") or "sql").strip().lower()
        if code_raw:
            text = _decode_code(str(code_raw), str(row.get("code_encoding") or "") or None)
            ext = _LANG_EXT.get(language, ".sql")
            sources_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{_slug(oid)}{ext}"
            (sources_dir / fname).write_text(text, encoding="utf-8")
            # project-relative path — resolved by _resolve_sample_source()
            sample_source = f"{ESTATE_SUBDIR}/sources/{fname}"
        elif typ in ("query", "view"):
            gap(oid, "no_code", "No legacy code captured — code conversion unavailable for this object.")

        recommendation = str(row.get("recommendation") or "") or _default_recommendation(disposition)

        obj: dict[str, Any] = {
            "id": oid,
            "type": typ,
            "name": name,
            "object_name": object_name,
            "object_type": object_type,
            "domain": str(row.get("domain") or "Unknown"),
            "application": str(row.get("application") or "Unknown"),
            "instance": str(row.get("instance") or "Unknown"),
            "database": str(row.get("database") or "Unknown"),
            "schema": str(row.get("schema") or "Unknown"),
            "team": str(row.get("team") or "Unknown"),
            "size_gb": size_gb,
            "active": active,
            "adhoc": adhoc,
            "phi_pii": phi_pii,
            "compatibility": str(row.get("compatibility") or "Compatible"),
            "recommendation": recommendation,
            "recommendation_reason": str(row.get("recommendation_reason") or ""),
            "target": str(row.get("target") or ""),
            "complexity": str(row.get("complexity") or "Medium"),
            "disposition": disposition,
            "reads": reads,
            "status": status,
            "metric": metric,
            # legacy layout coords required by the EstateObject type — the graph
            # computes its own layout, so a deterministic grid is fine.
            "x": 120 + (i % 8) * 160,
            "y": 120 + (i // 8) * 120,
            "scope": "core",
        }
        if row.get("platform"):
            obj["platform"] = str(row["platform"])
        if row.get("legacy_platform"):
            obj["legacy_platform"] = str(row["legacy_platform"])
        elif row.get("platform") and typ in ("query", "view"):
            obj["legacy_platform"] = str(row["platform"])
        if row.get("migration_approach"):
            obj["migration_approach"] = str(row["migration_approach"])
        if row.get("last_run"):
            obj["last_run"] = str(row["last_run"])
        if sample_columns:
            obj["sample_columns"] = sample_columns
        if sample_source:
            obj["sample_source"] = sample_source

        obj["panel_rows"] = [pr for pr in [
            ["Type", object_type],
            ["System", obj.get("platform") or obj["instance"]],
            ["Schema", f"{obj['database']}.{obj['schema']}" if "Unknown" not in (obj["database"], obj["schema"]) else ""],
            ["Size", f"{size_gb:g} GB" if size_gb else ""],
            ["Owner", obj["team"] if obj["team"] != "Unknown" else ""],
            ["Last run", obj.get("last_run") or ""],
        ] if pr[1] and pr[1] != "Unknown"]

        objects.append(obj)

    # ── lineage validation: drop reads that reference unknown ids ──
    known = {o["id"] for o in objects}
    edges_kept = 0
    for o in objects:
        valid = []
        for src in o["reads"]:
            if src in known:
                valid.append(src)
                edges_kept += 1
            else:
                edges_dropped += 1
                gap(o["id"], "unknown_edge_ref",
                    f"Upstream reference '{src}' not found in this import — edge dropped.")
        o["reads"] = valid

    estate_doc = {
        "scenario": scenario or f"Imported estate — {project_code}",
        "description": description or
            f"Estate imported from upstream metadata export ({len(objects)} objects). "
            "Review dispositions, drill lineage in the Estate Graph, and plan work per object.",
        "objects": objects,
    }
    if domain:
        estate_doc["domain"] = domain

    gap_report = {
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "project_code": project_code,
        "counts": {
            "objects": len(objects),
            "edges_kept": edges_kept,
            "edges_dropped": edges_dropped,
            "objects_with_gaps": len(gaps_by_obj),
        },
        "global": global_gaps,
        "objects": gaps_by_obj,
    }
    return estate_doc, gap_report


def _human(n: float) -> str:
    n = float(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def _default_recommendation(disposition: str) -> str:
    return {
        "modernize": "Modernize",
        "migrate": "Migrate",
        "retire": "Review & Retire",
        "remain": "Retain",
    }.get(disposition, "Retain")


# ── persistence ────────────────────────────────────────────────

def estate_dir(project_code: str) -> Path:
    return BASE_PROJECT_DIR / project_code / ESTATE_SUBDIR


def estate_path(project_code: str) -> Path:
    return estate_dir(project_code) / "estate.yaml"


def gap_report_path(project_code: str) -> Path:
    return estate_dir(project_code) / "gap_report.json"


def sources_dir(project_code: str) -> Path:
    return estate_dir(project_code) / "sources"


def write_estate(project_code: str, estate_doc: dict[str, Any], gap_report: dict[str, Any]) -> None:
    """Atomic write of estate.yaml + gap_report.json (tmp + rename).
    Re-importing simply overwrites — update semantics = re-export upstream."""
    d = estate_dir(project_code)
    d.mkdir(parents=True, exist_ok=True)
    for fname, payload, dump in (
        ("estate.yaml", estate_doc, lambda p: yaml.safe_dump(p, sort_keys=False, allow_unicode=True)),
        ("gap_report.json", gap_report, lambda p: json.dumps(p, indent=2)),
    ):
        tmp = d / (fname + ".tmp")
        tmp.write_text(dump(payload), encoding="utf-8")
        tmp.replace(d / fname)


# ══════════════════════════════════════════════════════════════════════════════
# Offline estate manifest import (Connected Estate — no live connection)
# ══════════════════════════════════════════════════════════════════════════════
#
# Distinct from the CSV modernization import above: a client runs the vetted
# offline extractor in THEIR environment, produces a reviewable YAML manifest,
# and uploads it. Import is a deterministic REPLAY into the SAME graph writer the
# live scan uses (``estate.write_scan_snapshot``), so an imported scan is
# indistinguishable from a live one — enrichment, feasibility, act-on-green all
# work unchanged. Two entry points mirror the ODCS ``parse`` (no-write) →
# ``from-odcs`` (commit) split.

from datetime import datetime as _dt   # noqa: E402

from sqlmodel import Session, func, select   # noqa: E402

from . import estate as estate_mod   # noqa: E402
from . import estate_manifest as manifest_mod   # noqa: E402
from .models import Estate, EstateScan, EstateScanNamespace, EstateSource   # noqa: E402


def preview_estate_manifest(raw_yaml: str) -> dict[str, Any]:
    """Parse + validate + summarize an uploaded manifest with **no graph write**.

    Raises :class:`estate_manifest.EstateManifestValidationError` on a malformed
    document (the router maps it to a 422). The returned digest — counts, per-schema
    breakdown, redaction summary, PII-flagged columns, warnings — drives the upload
    preview so the PO confirms before anything touches the graph."""
    manifest = manifest_mod.load_manifest(raw_yaml)
    return manifest_mod.summarize_manifest(manifest)


def import_estate_manifest(
    session: Session,
    *,
    estate: Estate,
    source: EstateSource,
    manifest: "manifest_mod.EstateManifest",
    initiated_by: str = "",
) -> dict[str, Any]:
    """The atomic replay: materialize a validated manifest as a real
    ``:EstateScan`` (identical shape to a live scan). Returns ``{scan_id, stats}``.

    Steps mirror ``estate_scan.run_scan`` §4–5, minus the live provider calls:
      1. allocate a new ``EstateScan`` row (monotone ``scan_version``).
      2. ``write_scan_snapshot`` with ``to_relation_dicts(manifest)`` — the exact
         live-scan writer (tombstone/diff/schemaHash/versioning all identical).
      3. ``apply_manifest_enrichment`` (guarded profiles + source-comment seeds).
      4. FK edges + optional code assets.
      5. finalize state=completed + ``update_scan_graph_state`` with an
         ``origin: offline_import`` marker.
    """
    estate_id = estate.id
    source_id = source.id
    database = manifest.db_segment

    # 1. Allocate a new scan version for this source.
    max_v = session.exec(
        select(func.max(EstateScan.scan_version)).where(
            EstateScan.estate_id == estate_id, EstateScan.source_id == source_id
        )
    ).one()
    version = (max_v or 0) + 1
    relations = manifest_mod.to_relation_dicts(manifest)
    scanned_schemas = sorted({r["schema"] for r in relations if r.get("schema")})
    summary = manifest_mod.summarize_manifest(manifest)
    depth = summary["depth"]

    scan = EstateScan(
        estate_id=estate_id, source_id=source_id, scan_version=version,
        depth=depth, state="running", initiated_by=initiated_by,
        started_at=_dt.utcnow(),
    )
    session.add(scan)
    session.commit()
    session.refresh(scan)
    scan_id = scan.id

    # Per-namespace outcome rows (so get_scan drill-down works + tombstoning scope
    # is recorded, exactly like the live scan's _record_namespace).
    schema_relcols: dict[str, tuple[int, int]] = {}
    for r in relations:
        sch = r.get("schema", "") or ""
        rc, cc = schema_relcols.get(sch, (0, 0))
        schema_relcols[sch] = (rc + 1, cc + len(r.get("columns", []) or []))
    for sch, (rc, cc) in schema_relcols.items():
        session.add(EstateScanNamespace(
            scan_id=scan_id, estate_id=estate_id, namespace=sch,
            outcome="scanned", relation_count=rc, column_count=cc, detail="offline_import",
        ))
    session.commit()

    # 2. Base graph — the exact live writer.
    graph_stats = estate_mod.write_scan_snapshot(
        session,
        estate_id=estate_id, estate_name=estate.name, estate_domain=estate.domain,
        estate_status=estate.status, scan_id=scan_id, source_id=source_id,
        version=version, depth=depth, state="completed",
        relations=relations, scanned_schemas=scanned_schemas,
    )

    # 3. Offline extras — guarded profiles + source-comment descriptions.
    try:
        enrich_stats = estate_mod.apply_manifest_enrichment(
            session, estate_id=estate_id, source_id=source_id, database=database,
            relations=manifest_mod.to_enrichment_relations(manifest),
        )
        graph_stats.update({f"offline_{k}": v for k, v in enrich_stats.items()})
    except Exception:  # noqa: BLE001 — extras are additive, never fail the import
        pass

    # 4. FK edges (best-effort).
    try:
        fk_pairs = manifest_mod.to_fk_tuples(manifest, estate_id=estate_id, source_id=source_id)
        if fk_pairs:
            graph_stats["fk_edges_written"] = estate_mod.write_fk_edges(
                session, estate_id=estate_id, source_id=source_id, fk_pairs=fk_pairs)
    except Exception:  # noqa: BLE001
        pass

    # 4b. Code assets (best-effort).
    if manifest.code_assets:
        try:
            graph_stats["code_assets_written"] = estate_mod.write_code_asset_snapshot(
                session, estate_id=estate_id, source_id=source_id, scan_id=scan_id,
                version=version, assets=manifest_mod.to_code_asset_summaries(manifest),
                namespaces=scanned_schemas,
            )
        except Exception:  # noqa: BLE001
            pass

    # 5. Finalize.
    graph_stats.update({
        "origin": "offline_import",
        "manifest_version": manifest.manifest_version,
        "platform": manifest.platform,
        "tool_version": manifest.tool_version,
        "generated_at": manifest.generated_at,
        "namespaces_scanned": len(scanned_schemas),
        "namespaces_selected": len(scanned_schemas),
        "redaction": dict(manifest.extraction.redaction or {}),
    })
    scan = session.get(EstateScan, scan_id)
    scan.state = "completed"
    scan.finished_at = _dt.utcnow()
    scan.stats_json = json.dumps(graph_stats, default=str)
    scan.scan_progress_json = json.dumps(
        {"stage": "completed", "origin": "offline_import",
         "updated_at": _dt.utcnow().isoformat()}, default=str)
    scan.updated_at = _dt.utcnow()
    session.add(scan)
    session.commit()
    estate_mod.update_scan_graph_state(session, scan_id, "completed", graph_stats)

    return {"scan_id": scan_id, "scan_version": version, "depth": depth,
            "stats": graph_stats}
