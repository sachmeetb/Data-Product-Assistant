"""Retired-term guard: ``pg_connection`` must never return as an app-level
connection field/key.

Postgres is now a pure peer in the unified platform engine — every project binds
its source via ``SourceBinding`` → ``PlatformConnection`` (the single connection
contract). A Postgres DSN is only ever a transient value produced from a
structured ref (via ``build_connection_string``) at the moment a driver/skill
needs one; it is NEVER stored as a model field, a dict key, or a dataclass
attribute.

This test fails closed if the retired shape resurfaces:
  1. No SQLModel table carries a ``pg_connection`` field.
  2. The resolvers never return a ``pg_connection`` key in their connection_ref.
  3. The in-flight input dataclasses carry no ``pg_connection`` field.
  4. The quoted ``"pg_connection"`` app-level key appears in NO non-test backend
     source module (transient DSN *parameters* named ``pg_connection`` are the
     sanctioned migration affordance and use the bare identifier, not the key).
"""
from __future__ import annotations

import pathlib

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parents[1]


# 1. Model fields ───────────────────────────────────────────────────────────────

def test_project_model_has_no_pg_connection_field():
    from workbench.backend.models import Project
    assert "pg_connection" not in Project.model_fields


def test_materialization_target_has_no_pg_connection_field():
    from workbench.backend.models import MaterializationTarget
    assert "pg_connection" not in MaterializationTarget.model_fields


# 2. Resolvers never emit a pg_connection key ─────────────────────────────────

def test_resolve_source_connection_ref_never_returns_pg_connection_key():
    """An unbound project resolves to an empty STRUCTURED ref — no pg_connection
    key, no sentinel shape."""
    from sqlmodel import Session
    from workbench.backend.database import engine
    from workbench.backend.models import Project
    from workbench.backend.routers.connections import resolve_source_connection_ref

    with Session(engine) as session:
        proj = Project(project_code="guard-unbound", name="p")
        session.add(proj)
        session.commit()
        session.refresh(proj)
        platform, ref = resolve_source_connection_ref(proj, session)
    assert "pg_connection" not in ref


# 3. In-flight input dataclasses ──────────────────────────────────────────────

def test_chat_inputs_has_no_pg_connection_field():
    import dataclasses
    from workbench.backend.marketplace_chat import ChatInputs
    names = {f.name for f in dataclasses.fields(ChatInputs)}
    assert "pg_connection" not in names


def test_execute_inputs_has_no_pg_connection_field():
    import dataclasses
    from workbench.backend.qa_execute import ExecuteInputs
    names = {f.name for f in dataclasses.fields(ExecuteInputs)}
    assert "pg_connection" not in names
    assert "exec_pg_connection" not in names


# 4. The quoted app-level key appears nowhere in non-test backend source ──────────

def test_quoted_pg_connection_key_absent_from_backend_source():
    """No non-test backend .py may contain the quoted ``"pg_connection"`` — the
    app-level dict-key / string-key shape. Transient DSN *parameters* named
    ``pg_connection`` (bare identifier) are allowed; the quoted key is not."""
    offenders: list[str] = []
    for path in _BACKEND.rglob("*.py"):
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith("tests/"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if '"pg_connection"' in text or "'pg_connection'" in text:
            offenders.append(rel)
    assert not offenders, (
        "Retired app-level pg_connection key resurfaced in: "
        + ", ".join(sorted(offenders))
    )


# 5. The legacy resolver helpers are gone ──────────────────────────────────────

def test_legacy_resolver_helpers_removed():
    import workbench.backend.pg_resolver as r
    assert not hasattr(r, "resolve_pg_connection")
    assert not hasattr(r, "pg_dsn_from_ref")
    assert not hasattr(r, "_parse_pg_dsn_identity")
