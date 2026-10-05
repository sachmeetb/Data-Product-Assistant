"""Serving-build filter auto-compile — the plain-language dataset filter
(filterIntent → filterPredicate) must be compiled before EVERY serving-generation
path runs generate_view_ddl, not just the `serving_virtual_view` SDK stage.

Two layers, both SQLite-safe (no Neo4j):
  * the sync `ensure_serving_filters_compiled` wrapper drives the async hook, and
  * a source-level guard that every dbt/lakehouse build entry point calls it, so a
    future build path can't silently drop the compile (the drift-stopper pattern).
"""

import inspect

import pytest

from workbench.backend import stage_execution as se


# ── Sync wrapper drives the async hook ──────────────────────────────────────


def test_ensure_serving_filters_compiled_invokes_async_hook(monkeypatch):
    seen = []

    async def _stub(project, session):
        seen.append((project, session))

    monkeypatch.setattr(se, "_autocompile_filters_for_serving", _stub)
    se.ensure_serving_filters_compiled("proj", "sess")
    assert seen == [("proj", "sess")]


def test_ensure_serving_filters_compiled_propagates_valueerror(monkeypatch):
    async def _raiser(project, session):
        raise ValueError("Could not compile the row filter 'active employees only'")

    monkeypatch.setattr(se, "_autocompile_filters_for_serving", _raiser)
    with pytest.raises(ValueError, match="Could not compile the row filter"):
        se.ensure_serving_filters_compiled("proj", "sess")


# ── Anti-regression guard: every serving-generation entry compiles filters ───


def test_every_serving_build_path_compiles_filters():
    from workbench.backend.routers import materialization as mat
    from workbench.backend import lakehouse_export as lh

    entries = [
        mat._scaffold_dbt_project,      # dbt build + materialize (shared choke)
        lh.run_lakehouse_export,        # lakehouse deploy
        lh.build_lakehouse_package,     # lakehouse build
    ]
    for fn in entries:
        src = inspect.getsource(fn)
        assert "ensure_serving_filters_compiled" in src, (
            f"{fn.__module__}.{fn.__name__} no longer compiles the plain-language "
            "filter before generate_view_ddl — a serving build could trip the "
            "intent-without-predicate guard"
        )
