"""Shared SQL identifier quoting.

Single source of truth for how a ``(schema, relation)`` pair is quoted per
platform. MySQL / Databricks (Spark SQL) use backticks; everything else
(Postgres, Snowflake, BigQuery, DuckDB, ANSI) uses double-quotes.

Kept as a tiny standalone module (no heavy imports) so every sampler path —
serving preview, marketplace preview, deployment reflection, QA execute, the
marketplace NL→SQL sampler — can route through one helper instead of
hardcoding ``f'SELECT * FROM "{schema}"."{name}"'`` (which silently produces
invalid SQL against a MySQL source). The quoting convention here matches
``platform/sql_renderer.py``'s ``_DoubleQuoteMixin`` / ``_BacktickMixin``.
"""
from __future__ import annotations

# Platforms whose identifier quote character is the backtick. Everything else
# (the default) uses the ANSI double-quote.
_BACKTICK_PLATFORMS = frozenset({"mysql", "databricks"})

# Platforms that fold UNQUOTED identifiers to UPPERCASE at object-creation time.
# Snowflake is the only supported serving target that does this (Oracle/Redshift/
# DB2 also upper-fold and can be added here if they ever become serving targets).
# dlt/dbt emit unquoted DDL (like dbt with `quoting: false`), so the physical
# objects they create on these platforms are UPPERCASE — a read that
# double-quotes a lowercase name (`"party"`) is case-sensitive-exact and misses
# the UPPER object (`PARTY`). ``quote_created_relation`` folds to this natural
# case before quoting so reads of unquoted-created objects resolve.
_UPPER_FOLD_PLATFORMS = frozenset({"snowflake"})


def quote_style(platform: str | None = "postgres") -> str:
    """Return the identifier quote style for ``platform``: 'backtick' | 'double'.

    THE single source of the quote character. `quote_ident` uses it, and the
    platform NamespaceModel serialises it into the deploy package descriptor so
    the stdlib-only runner quotes identically without importing this module.
    """
    return "backtick" if (platform or "postgres").lower() in _BACKTICK_PLATFORMS else "double"


def quote_ident(name: str, platform: str | None = "postgres") -> str:
    """Return a single identifier quoted for ``platform``."""
    return f"`{name}`" if quote_style(platform) == "backtick" else f'"{name}"'


def quote_relation(schema: str | None, name: str, platform: str | None = "postgres") -> str:
    """Return a fully-qualified quoted ``schema.relation`` for ``platform``.

    When ``schema`` is falsy the relation name is quoted on its own (some
    engines — DuckDB catalog views, e.g. — address relations bare).

    ``schema`` may itself be a multi-part namespace (e.g. a Databricks Unity
    Catalog ``catalog.schema`` like ``workspace.default``). Each dotted segment
    is quoted independently so a 3-level target renders as
    ```catalog`.`schema`.`relation``` rather than a single mis-quoted identifier
    ```catalog.schema`.`relation``` (which the engine reads as one literal name).
    """
    if schema:
        prefix = ".".join(
            quote_ident(part, platform) for part in str(schema).split(".") if part
        )
        if prefix:
            return f"{prefix}.{quote_ident(name, platform)}"
    return quote_ident(name, platform)


def upper_folds(platform: str | None = "postgres") -> bool:
    """True when ``platform`` folds UNQUOTED identifiers to UPPERCASE at object
    creation (Snowflake). Lets non-quoting callers (the deploy-package namespace
    descriptor) carry the fold decision without importing the private set."""
    return (platform or "").lower() in _UPPER_FOLD_PLATFORMS


def fold_created_ident(name: str, platform: str | None = "postgres") -> str:
    """Fold a single identifier to the platform's natural UNQUOTED case.

    On an upper-folding platform (Snowflake) this uppercases ``name`` so a quoted
    reference matches the object dlt/dbt created via unquoted DDL. A no-op
    (identity) on every non-upper-folding platform — so callers can route through
    it unconditionally without changing byte-for-byte output for Postgres/MySQL/
    Databricks/etc.
    """
    return name.upper() if (platform or "").lower() in _UPPER_FOLD_PLATFORMS else name


def quote_created_relation(schema: str | None, name: str, platform: str | None = "postgres") -> str:
    """``quote_relation`` for objects CREATEd by unquoted DDL (dlt/dbt loads,
    dbt-materialized tables, and — going forward — virtual views).

    Folds EACH identifier segment (schema parts + relation name) to the
    platform's natural unquoted case via ``fold_created_ident``, THEN quotes,
    so the result is the case-sensitive-exact quoted form of the physical object
    the engine actually made. Quoting-in-UPPER (rather than emitting bare SQL)
    keeps a sanitized-but-reserved name (e.g. a table ``order``) safe.

    Byte-identical to ``quote_relation`` for non-upper-folding platforms — the
    fold is an identity there.
    """
    if schema:
        prefix = ".".join(
            quote_ident(fold_created_ident(part, platform), platform)
            for part in str(schema).split(".") if part
        )
        if prefix:
            return f"{prefix}.{quote_ident(fold_created_ident(name, platform), platform)}"
    return quote_ident(fold_created_ident(name, platform), platform)
