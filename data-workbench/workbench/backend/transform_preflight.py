"""Backend exposure of transform CompileResults (Phase 3/5 of transform-portability.md).

The per-dataset compiler (`generate_view_ddl.py`) validates every authored
transform expression against the capability artifact and folds the findings into
`summary['transform_diagnostics']`. This module lifts those findings into the
single `dialect_sql.CompileResult` shape that REST (`routers/serving.py`,
materialization, transfer), MCP, and the generated `run.py` all surface — and
that the Phase-6 gate reads to refuse deployable SQL when `errors` is non-empty.

Enforcement lives HERE (the shared result), not in any one router and not in the
bypassable generator subprocess: `preflight_product` re-runs the compiler over
the mappings for a target platform, so the virtual-view path (whose DDL is
authored in the agent's subprocess) is validated backend-side regardless.
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Optional

from . import config
from .config import SKILLS_DIR
from .dialect_sql import CompileResult, TransformDiagnostic

logger = logging.getLogger(__name__)

_GV_PATH = SKILLS_DIR / "data-serving-virtual-view" / "scripts" / "generate_view_ddl.py"


def _load_gv():
    """Import generate_view_ddl.py by path (it lives in the skills plugin, not a
    backend package). Mirrors lakehouse_export / transfer_execution."""
    spec = importlib.util.spec_from_file_location("generate_view_ddl", _GV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def compile_result_from_diagnostics(
    diagnostics: Optional[dict], *, sql: Optional[str] = None,
) -> CompileResult:
    """Convert a `transform_diagnostics` dict (from a generator summary) into a
    `CompileResult`. `sql` is attached ONLY when there are no errors — the
    load-bearing "no deployable SQL while errors present" contract."""
    diagnostics = diagnostics or {}
    errors = [TransformDiagnostic.from_dict(e) for e in diagnostics.get("errors", [])]
    warnings = [TransformDiagnostic.from_dict(w) for w in diagnostics.get("warnings", [])]
    return CompileResult(
        sql=sql if not errors else None,
        catalog_version=diagnostics.get("catalog_version"),
        warnings=warnings,
        errors=errors,
        used_capabilities=list(diagnostics.get("used_capabilities", [])),
        validated=bool(diagnostics.get("validated", False)),
    )


def compile_result_from_summary(summary, *, sql: Optional[str] = None) -> CompileResult:
    """Lift the `transform_diagnostics` roll-up out of a generator summary.

    A generator summary is EITHER a dict (success) OR an error string (e.g. "No
    approved mappings") — handle both; a string summary yields an empty result."""
    if not isinstance(summary, dict):
        return CompileResult(sql=sql, catalog_version=None, validated=False)
    return compile_result_from_diagnostics(summary.get("transform_diagnostics"), sql=sql)


def preflight_product(
    driver,
    database: str,
    product_uri: str,
    *,
    view_schema: str = "public",
    platform: str = "postgres",
    source_served_map: Optional[dict] = None,
) -> CompileResult:
    """Compile a product's views for `platform` and return the aggregated
    CompileResult. Read-only: runs the generator (which does not deploy), reads
    the diagnostics, and attaches the DDL only when the compile is clean.

    `platform` is a served platform id (postgres/databricks/snowflake/bigquery/
    mysql). Anything else (ansi/duckdb) yields `validated=False` diagnostics
    from the generator and an empty (non-blocking) result — validation is scoped
    to native-view served platforms.
    """
    gv = _load_gv()
    ddl_text, _primary, summary = gv.generate_ddl(
        driver, database, product_uri,
        view_schema=view_schema, dialect_name=platform,
        source_served_map=source_served_map,
    )
    return compile_result_from_summary(summary, sql=ddl_text)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6 — staged enforcement gate
# ─────────────────────────────────────────────────────────────────────────────
# Enforcement lives in the shared result, not per-router: any build/deploy path
# that has a CompileResult (or a generator summary) calls one of these helpers.
# Default mode is 'warn' — nothing is blocked until an operator sets
# WB_TRANSFORM_ENFORCEMENT=block (after the portfolio audit is clean).


def enforcement_mode() -> str:
    return config.transform_enforcement()


def enforcement_enabled() -> bool:
    """True when the gate should run its preflight at all (warn or block). In
    'off' mode callers skip the extra compile entirely."""
    return enforcement_mode() != "off"


def is_blocked(result: Optional[CompileResult]) -> bool:
    """True only in 'block' mode with a result carrying errors."""
    return enforcement_mode() == "block" and bool(result and result.errors)


def raise_if_blocked(result: Optional[CompileResult], *, context: str = "deploy") -> None:
    """The gate. In 'block' mode with errors → raise HTTPException(422) with the
    full diagnostics (mirrors the materialization join-preflight 422). In 'warn'
    mode with errors → log and allow. In 'off' mode or when clean → no-op.

    Router-friendly (raises HTTPException). Non-router callers use ``is_blocked``.
    """
    if result is None or not result.errors:
        return
    mode = enforcement_mode()
    if mode == "off":
        return
    if mode == "warn":
        logger.warning(
            "transform-portability[%s]: %d capability error(s) (warn mode, allowing deploy): %s",
            context, len(result.errors), [e.code for e in result.errors],
        )
        return
    # block
    from fastapi import HTTPException
    payload = result.to_dict()
    payload.update({
        "error": "transform_capability_block",
        "context": context,
        "message": (
            f"{len(result.errors)} transform expression(s) are not supported on the "
            f"target platform. Re-author the mapping(s) (see errors[].remediation) or set "
            f"WB_TRANSFORM_ENFORCEMENT=warn to override. Failing mappings: "
            + ", ".join(sorted({e.product_col or e.function or e.code for e in result.errors}))
        ),
    })
    raise HTTPException(status_code=422, detail=payload)


def raise_if_summary_blocked(summary, *, context: str = "build") -> None:
    """Gate directly off a generator summary (dict or error string) — used by the
    emitter paths (dbt/lakehouse/transfer) that already hold a summary carrying
    ``transform_diagnostics``, avoiding a second compile."""
    raise_if_blocked(compile_result_from_summary(summary), context=context)
