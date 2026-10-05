"""Confirm Physical Schema (D3) — a reviewed, structured physical schema for a
schema-only migration project.

The blueprint is too lossy to seed a graph from directly (its ``note`` has no
grammar, ``domain`` ≠ a DB schema, 2-level name parsing misses Snowflake/
Databricks 3-level, and a missing type validates fine). So instead of treating
the blueprint as a machine contract, we pre-fill a **best-effort draft** from it
and require an engineer to **explicitly confirm** structured fields —
catalog/namespace/table (3-level aware), a required physical ``data_type``,
``nullable``, ``primary_key``, and a structured FK reference.

The confirmed artifact (persisted as ``ProjectPhysicalSchema.physical_schema_json``,
blueprint untouched) is what the deterministic seeder (Phase E) consumes. Names
become filenames there, so confirmation hard-blocks path-unsafe / colliding
names and (for included tables) missing types.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlmodel import Session

from .models import IntakeSubmission, Project, ProjectPhysicalSchema

PHYSICAL_SCHEMA_VERSION = "1.0"


class PhysicalColumn(BaseModel):
    name: str = ""
    data_type: str = ""          # physical type; REQUIRED for an included table
    nullable: Optional[bool] = None
    primary_key: bool = False
    # Structured FK reference (no free-form prose): target table (namespace-
    # qualified allowed) + column. Both empty = no FK.
    fk_table: str = ""
    fk_column: str = ""


class PhysicalTable(BaseModel):
    catalog: str = ""            # 3-level container (Databricks/Snowflake); "" for 2-level
    namespace: str = ""          # the DB schema / namespace
    table: str = ""              # table name, separate from the namespace
    columns: list[PhysicalColumn] = Field(default_factory=list)
    excluded: bool = False       # skip on seed (kept for context)


class PhysicalSchema(BaseModel):
    version: str = PHYSICAL_SCHEMA_VERSION
    tables: list[PhysicalTable] = Field(default_factory=list)


class PhysicalSchemaValidationError(ValueError):
    """Confirmation blocked — carries the blocking ``errors`` list."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# ── name safety (names become filenames in the seeder) ───────────────────────

def _name_unsafe(name: str) -> bool:
    """True when a name can't be trusted as a path component / identifier."""
    if not name or name.strip() != name:
        return True
    return "/" in name or "\\" in name or ".." in name or "\x00" in name


# ── prefill from the blueprint (best-effort, then confirmed) ─────────────────

def _split_qualified(raw: str) -> tuple[str, str, str]:
    """Split a possibly-qualified dataset name into (catalog, namespace, table).
    1 part → table only; 2 → namespace.table; 3+ → catalog.namespace.table
    (extra leading parts folded into catalog)."""
    parts = [p for p in (raw or "").split(".") if p != ""]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return "", "", parts[0]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return ".".join(parts[:-2]), parts[-2], parts[-1]


def prefill_from_blueprint(blueprint: dict[str, Any]) -> PhysicalSchema:
    """Best-effort draft from a migration blueprint's top-level datasets. Types
    are copied where present (confirmed/edited later); nothing is fabricated."""
    tables: list[PhysicalTable] = []
    for ds in (blueprint or {}).get("datasets") or []:
        raw_name = ((ds.get("name") or {}).get("value")) or ""
        catalog, namespace, table = _split_qualified(raw_name)
        cols: list[PhysicalColumn] = []
        for c in ds.get("columns") or []:
            dt = ((c.get("data_type") or {}).get("value")) or ""
            cols.append(
                PhysicalColumn(
                    name=((c.get("name") or {}).get("value")) or "",
                    data_type="" if str(dt).lower() in ("", "missing") else str(dt),
                )
            )
        tables.append(PhysicalTable(catalog=catalog, namespace=namespace, table=table, columns=cols))
    return PhysicalSchema(tables=tables)


# ── validation (confirm gate) ────────────────────────────────────────────────

def validate_for_confirm(schema: PhysicalSchema) -> None:
    """Hard-block confirmation on path-unsafe/colliding names or (for an included
    table) a missing type. Raises PhysicalSchemaValidationError with every reason."""
    errors: list[str] = []
    included = [t for t in schema.tables if not t.excluded]
    if not included:
        errors.append("no tables to seed (all excluded or empty)")

    seen_tables: set[tuple[str, str, str]] = set()
    for t in included:
        label = ".".join([p for p in (t.catalog, t.namespace, t.table) if p]) or "(unnamed)"
        for field, val in (("catalog", t.catalog), ("namespace", t.namespace), ("table", t.table)):
            # catalog may legitimately be empty (2-level); namespace/table may not.
            if field != "catalog" and not val:
                errors.append(f"{label}: {field} is required")
            elif val and _name_unsafe(val):
                errors.append(f"{label}: unsafe {field} name '{val}'")
        key = (t.catalog, t.namespace, t.table)
        if key in seen_tables:
            errors.append(f"{label}: duplicate table")
        seen_tables.add(key)

        seen_cols: set[str] = set()
        for c in t.columns:
            if not c.name:
                errors.append(f"{label}: a column is missing a name")
                continue
            if _name_unsafe(c.name):
                errors.append(f"{label}.{c.name}: unsafe column name")
            if not (c.data_type or "").strip():
                errors.append(f"{label}.{c.name}: physical data_type is required")
            if c.name in seen_cols:
                errors.append(f"{label}.{c.name}: duplicate column")
            seen_cols.add(c.name)

    if errors:
        raise PhysicalSchemaValidationError(errors)


# ── persistence ──────────────────────────────────────────────────────────────

def get_or_prefill(session: Session, project: Project) -> tuple[ProjectPhysicalSchema, bool]:
    """Return the saved draft, or synthesize one from the originating intake
    blueprint on first read (not persisted until the engineer saves). The second
    element is ``prefilled`` (True when synthesized, not yet stored)."""
    row = session.get(ProjectPhysicalSchema, project.project_code)
    if row is not None:
        return row, False

    schema = PhysicalSchema()
    sub_id = getattr(project, "parent_intake_submission_id", None)
    if sub_id is not None:
        sub = session.get(IntakeSubmission, sub_id)
        if sub is not None and sub.blueprint_json:
            import json

            try:
                schema = prefill_from_blueprint(json.loads(sub.blueprint_json))
            except (ValueError, TypeError):
                schema = PhysicalSchema()
    row = ProjectPhysicalSchema(
        project_code=project.project_code,
        physical_schema_json=schema.model_dump_json(),
        status="draft",
        source_intake_submission_id=sub_id,
    )
    return row, True


def save_draft(session: Session, project: Project, schema: PhysicalSchema) -> ProjectPhysicalSchema:
    from datetime import datetime

    row = session.get(ProjectPhysicalSchema, project.project_code)
    if row is None:
        row = ProjectPhysicalSchema(
            project_code=project.project_code,
            source_intake_submission_id=getattr(project, "parent_intake_submission_id", None),
        )
    row.physical_schema_json = schema.model_dump_json()
    row.status = "draft"          # editing re-opens a previously-confirmed schema
    row.confirmed_at = None
    row.confirmed_by = None
    row.updated_at = datetime.utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def confirm(session: Session, project: Project, schema: PhysicalSchema, by: str) -> ProjectPhysicalSchema:
    """Validate + persist as ``confirmed``. Raises PhysicalSchemaValidationError
    (→ 422 in the router) with the blocking reasons if not confirmable."""
    from datetime import datetime

    validate_for_confirm(schema)
    row = session.get(ProjectPhysicalSchema, project.project_code) or ProjectPhysicalSchema(
        project_code=project.project_code,
        source_intake_submission_id=getattr(project, "parent_intake_submission_id", None),
    )
    row.physical_schema_json = schema.model_dump_json()
    row.status = "confirmed"
    row.confirmed_at = datetime.utcnow()
    row.confirmed_by = by
    row.updated_at = datetime.utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row
