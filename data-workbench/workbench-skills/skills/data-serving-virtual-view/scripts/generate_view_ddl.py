#!/usr/bin/env python3
"""Generate a CREATE VIEW DDL from approved column mappings in Neo4j.

Compiles structured transformations on :ColumnMapping into SQL:

  - direct       -> tN.col
  - cast         -> CAST(tN.col AS <target_type>)
  - format       -> UPPER(...) / LOWER(...) / TRIM(...) / TO_CHAR(...) per params
  - concat       -> tN.a || '<sep>' || tN.b   (rendered from transformExpression)
  - split        -> SPLIT_PART(tN.col, '<delim>', <index>)
  - substring    -> SUBSTRING(tN.col FROM <start> FOR <length>)
  - case         -> CASE WHEN ... END (rendered from transformExpression)
  - arithmetic   -> rendered from transformExpression
  - lookup       -> ref.value_col via implicit LEFT JOIN
                    selection_strategy: equi | latest | aggregate | exists | asof
                    (asof = point-in-time: pick the reference row whose
                     [effective_from, effective_to) window contains the anchor's
                     pivot date — the grain-correct strategy for an SCD-2 target.)
  - literal      -> SQL literal verbatim, no source / JOIN
  - expression   -> raw transformExpression (engineer escape hatch)
  - bucket       -> CASE WHEN x < b1 THEN l1 ... ELSE lN END
  - mask         -> format-preserving redaction (keep_last / keep_first / middle)
  - hash         -> md5(col) / digest(col, '<algo>'), optional salt

Substitution of source column names inside transformExpression uses
word-boundary regex driven by transformInputs (JSON array of source URIs),
so a column named `id` does not collide with `customer_id`.
"""

import argparse
import json
import re
import sys

from neo4j import GraphDatabase


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7: dialect abstraction
# ─────────────────────────────────────────────────────────────────────────────
# Most of the SQL view-DDL emits is dialect-portable: SUBSTRING ... FROM ...
# FOR ..., ROW_NUMBER() OVER (...), LEFT JOIN, GROUP BY, etc. The dialect
# escape hatch concentrates the bits that genuinely differ across PG / Snow-
# flake / Databricks / BigQuery so a v1 multi-platform shipment doesn't
# require rewriting the compiler.
#
# Three places need dialect dispatch today:
#   1. Type casts — PG has `value::type`; standard SQL uses `CAST(value AS type)`.
#      Most targets accept the standard form, so the default Dialect emits
#      that; PostgresDialect keeps the existing `::` form for back-compat
#      with persisted DDL.
#   2. SHA-1 / SHA-2 hashes — PG needs pgcrypto's `encode(digest(...), 'hex')`;
#      Snowflake / Databricks have built-in `SHA2(text, 256)`; BigQuery has
#      `TO_HEX(SHA256(text))`. MD5 is universal.
#   3. REGEXP_REPLACE — PG accepts a 4th-arg flags string ('g' for global),
#      Snowflake takes (input, pattern, repl, position, occurrence, params),
#      Databricks/BigQuery are global by default.
#
# Adding a new dialect: subclass `Dialect`, override the methods that diverge,
# add an entry to `_DIALECTS`. The view-DDL generator threads a single
# Dialect instance through `_compile_*` so future helpers can opt in by
# accepting the dialect param.

class Dialect:
    """Base dialect — methods default to the most portable standard-SQL
    form. Subclasses override only what genuinely differs.

    By default the base class targets ANSI-compatible SQL: `CAST(value AS
    type)` for casts, no pgcrypto for hashing (so SHA-1/SHA-2 fall back to
    md5 with an inline note), `REGEXP_REPLACE(haystack, pattern, repl)` with
    implicit-global semantics.
    """

    name = "ansi"
    string_type = "VARCHAR"   # used by _compile_hash for CAST-to-text
    nulls_last = " NULLS LAST"  # appended to ORDER BY in ROW_NUMBER/window dedup
    quote_char = '"'          # identifier quote char (backtick on MySQL/Databricks)

    def quote_qualified(self, view_schema: str, view_name: str) -> str:
        """Quote each part of ``<schema>.<name>`` (or ``<catalog>.<schema>.<name>``)
        independently so a dotted target namespace is a real multi-part identifier —
        not one identifier containing dots — and mixed-case names survive. Mirrors
        the runner's ``_NamespaceOps.qualified`` so the CREATE header and the
        smoke-test SELECT address the exact same object."""
        q = self.quote_char
        parts = [p for p in (view_schema or "").split(".") if p] + [view_name]
        return ".".join(f"{q}{p.replace(q, q + q)}{q}" for p in parts)

    def view_header(self, view_schema: str, view_name: str) -> str:
        """The ``CREATE OR REPLACE VIEW <quoted qualified name>`` prefix (no ``AS``).
        Subclasses that need per-platform view options (e.g. Snowflake COPY GRANTS)
        override this."""
        return f"CREATE OR REPLACE VIEW {self.quote_qualified(view_schema, view_name)}"

    def cast(self, value: str, target_type: str) -> str:
        return f"CAST({value} AS {target_type})"

    def hash_md5(self, text_expr: str) -> str:
        # md5() is portable across PG / Snowflake / Databricks; BigQuery
        # needs TO_HEX(MD5_BYTES(...)) but the default targets ANSI.
        return f"md5({text_expr})"

    def hash_sha(self, text_expr: str, algorithm: str) -> str:
        """Hash via a SHA-family algorithm. `algorithm` ∈ {sha1, sha256}.

        ANSI fallback: no widely-portable SHA function exists — degrade to
        md5 + a comment so the engineer sees the substitution. Dialect
        subclasses override with their native function."""
        return f"md5({text_expr}) /* {algorithm} unsupported in this dialect; degraded to md5 */"

    def regexp_replace_global(self, haystack: str, pattern: str, replacement: str) -> str:
        # ANSI / Databricks / BigQuery REGEXP_REPLACE is global by default.
        return f"REGEXP_REPLACE({haystack}, {pattern}, {replacement})"

    def str_concat(self, parts: list, sep: str = " ") -> str:
        """Dialect-safe multi-column concatenation with separator.
        ANSI/MySQL/Databricks/BigQuery use CONCAT(); Postgres/Snowflake use ||.
        """
        interleaved = []
        for i, p in enumerate(parts):
            if i > 0 and sep:
                interleaved.append(f"'{sep}'")
            interleaved.append(p)
        return f"CONCAT({', '.join(interleaved)})"

    # ── Phase 7 structured-kind emission (dialect-portable) ──────────────────
    def substring(self, expr: str, start, length=None) -> str:
        """Portable substring. ``SUBSTR(expr, start[, length])`` (1-based) is
        accepted by PG / MySQL / Snowflake / Databricks / BigQuery — unlike the
        ANSI ``SUBSTRING(x FROM s FOR l)`` form which BigQuery rejects."""
        if length is None:
            return f"SUBSTR({expr}, {start})"
        return f"SUBSTR({expr}, {start}, {length})"

    def split_part(self, expr: str, delim: str, index) -> str:
        """1-based split-part. Native on PG / Databricks / Snowflake; BigQuery
        and MySQL override with an emulated form (they have no SPLIT_PART)."""
        return f"SPLIT_PART({expr}, {delim}, {index})"

    def date_difference(self, start: str, end: str, unit: str = "year",
                        semantics: str = "completed_units") -> str:
        """Neutral date-difference op with an explicit ``semantics`` discriminator.

        v1 supports ``unit='year'`` (the age case). ``boundary_count`` (calendar
        boundaries crossed) has a portable EXTRACT form; ``completed_units`` and
        ``symbolic_interval`` have no portable ANSI form and are overridden per
        dialect — the base fails closed (no silent wrong-semantics)."""
        if (unit or "year").lower() != "year":
            raise ViewGenerationError(
                f"date_difference v1 supports unit='year' only (got {unit!r})."
            )
        if semantics == "boundary_count":
            return f"(EXTRACT(YEAR FROM {end}) - EXTRACT(YEAR FROM {start}))"
        raise ViewGenerationError(
            f"date_difference(semantics={semantics!r}) has no portable ANSI form; "
            f"the target dialect must implement it."
        )


class PostgresDialect(Dialect):
    name = "postgres"
    string_type = "TEXT"
    # nulls_last inherits " NULLS LAST" from base — Postgres supports it

    def cast(self, value: str, target_type: str) -> str:
        # PG-specific `::` shorthand. The standard CAST form would also
        # work; keeping `::` matches DDL persisted before Phase 7.
        return f"{value}::{target_type}"

    def hash_sha(self, text_expr: str, algorithm: str) -> str:
        algo = algorithm.lower()
        if algo not in ("sha1", "sha256"):
            return self.hash_md5(text_expr)
        return f"encode(digest({text_expr}, '{algo}'), 'hex')"

    def regexp_replace_global(self, haystack: str, pattern: str, replacement: str) -> str:
        # PG requires the explicit 'g' flag for global replacement.
        return f"REGEXP_REPLACE({haystack}, {pattern}, {replacement}, 'g')"

    def str_concat(self, parts: list, sep: str = " ") -> str:
        if sep:
            return f" || '{sep}' || ".join(parts)
        return " || ".join(parts)

    def date_difference(self, start, end, unit="year", semantics="completed_units"):
        if (unit or "year").lower() != "year":
            raise ViewGenerationError(f"date_difference v1 supports unit='year' only (got {unit!r}).")
        if semantics == "symbolic_interval":
            return f"AGE({end}, {start})"
        if semantics == "completed_units":
            # AGE yields a symbolic interval; EXTRACT(YEAR ...) reads completed years.
            return f"EXTRACT(YEAR FROM AGE({end}, {start}))"
        return f"(EXTRACT(YEAR FROM {end}) - EXTRACT(YEAR FROM {start}))"  # boundary_count


class DuckDBDialect(PostgresDialect):
    """DuckDB — Postgres-compatible for casts (`::`), `||` concat, NULLS LAST,
    and global REGEXP_REPLACE with the 'g' flag. Diverges only on SHA hashing:
    DuckDB has native `md5()` / `sha256()` but no pgcrypto `digest()`, and no
    native `sha1`.
    """
    name = "duckdb"
    string_type = "VARCHAR"

    def hash_sha(self, text_expr: str, algorithm: str) -> str:
        algo = algorithm.lower()
        if algo == "sha256":
            return f"sha256({text_expr})"
        # No native sha1 in DuckDB — degrade to md5 with a note.
        return f"md5({text_expr}) /* {algorithm} unsupported in duckdb; degraded to md5 */"


class SnowflakeDialect(Dialect):
    name = "snowflake"

    def quote_qualified(self, view_schema: str, view_name: str) -> str:
        """Create the view under its UPPER identifier. Snowflake folds unquoted
        DDL to UPPER, the view body's FROM/column refs are emitted unquoted (so
        they already fold to UPPER), and every read path folds references to
        UPPER (``sql_ident.quote_created_relation`` + the deploy runner's
        upper-folding smoke test). Emitting the CREATE header UPPER keeps the
        physical object, its body, the smoke test, and all reads on ONE case —
        quoted so a sanitized-but-reserved name (e.g. a table ``order``) is
        still safe."""
        q = self.quote_char
        parts = [p for p in (view_schema or "").split(".") if p] + [view_name]
        return ".".join(f"{q}{p.upper().replace(q, q + q)}{q}" for p in parts)

    def view_header(self, view_schema: str, view_name: str) -> str:
        # COPY GRANTS preserves grants across CREATE OR REPLACE (Snowflake replaces
        # the view object; without it, privileges granted on the prior view are
        # dropped). Snowflake syntax: CREATE OR REPLACE VIEW <name> COPY GRANTS AS …
        return f"{super().view_header(view_schema, view_name)} COPY GRANTS"

    def hash_sha(self, text_expr: str, algorithm: str) -> str:
        algo = algorithm.lower()
        if algo == "sha1":
            # SHA2(text, 224) doesn't exist for sha1 — Snowflake has SHA1().
            return f"SHA1({text_expr})"
        if algo == "sha256":
            return f"SHA2({text_expr}, 256)"
        return self.hash_md5(text_expr)

    def regexp_replace_global(self, haystack: str, pattern: str, replacement: str) -> str:
        # Snowflake's REGEXP_REPLACE replaces all occurrences when neither
        # position nor occurrence is given. Use the 3-arg form for parity
        # with the others.
        return f"REGEXP_REPLACE({haystack}, {pattern}, {replacement})"

    def str_concat(self, parts: list, sep: str = " ") -> str:
        # Snowflake supports || for string concat (NOT bitwise OR)
        if sep:
            return f" || '{sep}' || ".join(parts)
        return " || ".join(parts)

    def date_difference(self, start, end, unit="year", semantics="completed_units"):
        if (unit or "year").lower() != "year":
            raise ViewGenerationError(f"date_difference v1 supports unit='year' only (got {unit!r}).")
        if semantics == "boundary_count":
            return f"DATEDIFF('year', {start}, {end})"
        if semantics == "completed_units":
            return f"FLOOR(MONTHS_BETWEEN({end}, {start}) / 12)"
        raise ViewGenerationError(
            "date_difference(semantics='symbolic_interval') is unsupported on Snowflake — "
            "no native yrs+mos+days interval. Author completed_units/boundary_count per unit."
        )


class DatabricksDialect(Dialect):
    name = "databricks"
    string_type = "STRING"
    quote_char = "`"          # Databricks (Spark SQL) quotes identifiers with backticks
    # nulls_last inherits " NULLS LAST" — Databricks supports it

    def hash_sha(self, text_expr: str, algorithm: str) -> str:
        algo = algorithm.lower()
        if algo == "sha256":
            return f"sha2({text_expr}, 256)"
        if algo == "sha1":
            return f"sha1({text_expr})"
        return self.hash_md5(text_expr)

    # Databricks REGEXP_REPLACE is global by default; inherits ANSI base.

    def date_difference(self, start, end, unit="year", semantics="completed_units"):
        if (unit or "year").lower() != "year":
            raise ViewGenerationError(f"date_difference v1 supports unit='year' only (got {unit!r}).")
        if semantics == "boundary_count":
            return f"(YEAR({end}) - YEAR({start}))"
        if semantics == "completed_units":
            return f"FLOOR(MONTHS_BETWEEN({end}, {start}) / 12)"
        raise ViewGenerationError(
            "date_difference(semantics='symbolic_interval') is unsupported on Databricks — "
            "no native yrs+mos+days interval. Author completed_units/boundary_count per unit."
        )


class BigQueryDialect(Dialect):
    name = "bigquery"
    string_type = "STRING"
    # nulls_last inherits " NULLS LAST" — BigQuery supports it

    def hash_md5(self, text_expr: str) -> str:
        return f"TO_HEX(MD5({text_expr}))"

    def hash_sha(self, text_expr: str, algorithm: str) -> str:
        algo = algorithm.lower()
        if algo == "sha256":
            return f"TO_HEX(SHA256({text_expr}))"
        if algo == "sha1":
            return f"TO_HEX(SHA1({text_expr}))"
        return self.hash_md5(text_expr)

    # BigQuery REGEXP_REPLACE is global by default; inherits ANSI base.

    def substring(self, expr: str, start, length=None) -> str:
        # BigQuery uses SUBSTR (SUBSTRING/…FROM…FOR is not accepted).
        if length is None:
            return f"SUBSTR({expr}, {start})"
        return f"SUBSTR({expr}, {start}, {length})"

    def split_part(self, expr: str, delim: str, index) -> str:
        # No SPLIT_PART — SPLIT returns a 0-based array; convert the 1-based index.
        try:
            zero_based = int(index) - 1
        except (TypeError, ValueError):
            zero_based = f"({index} - 1)"
        return f"SPLIT({expr}, {delim})[SAFE_OFFSET({zero_based})]"

    def date_difference(self, start, end, unit="year", semantics="completed_units"):
        if (unit or "year").lower() != "year":
            raise ViewGenerationError(f"date_difference v1 supports unit='year' only (got {unit!r}).")
        if semantics == "boundary_count":
            return f"DATE_DIFF({end}, {start}, YEAR)"
        if semantics == "completed_units":
            # DATE_DIFF(YEAR) counts boundaries; subtract 1 when end's month/day
            # precedes start's to get completed elapsed years.
            return (
                f"DATE_DIFF({end}, {start}, YEAR) - "
                f"IF((EXTRACT(MONTH FROM {end}), EXTRACT(DAY FROM {end})) < "
                f"(EXTRACT(MONTH FROM {start}), EXTRACT(DAY FROM {start})), 1, 0)"
            )
        raise ViewGenerationError(
            "date_difference(semantics='symbolic_interval') is unsupported on BigQuery — "
            "no native yrs+mos+days interval. Author completed_units/boundary_count per unit."
        )


class MySQLDialect(Dialect):
    name = "mysql"
    string_type = "CHAR"   # MySQL CAST accepts CHAR not TEXT/VARCHAR
    quote_char = "`"        # MySQL quotes identifiers with backticks
    nulls_last = ""         # MySQL 8.0 does not support NULLS LAST syntax;
                            # DESC already puts NULLs last by default

    def hash_sha(self, text_expr: str, algorithm: str) -> str:
        algo = algorithm.lower()
        if algo == "sha256":
            return f"SHA2({text_expr}, 256)"
        if algo == "sha1":
            return f"SHA1({text_expr})"
        return self.hash_md5(text_expr)

    def split_part(self, expr: str, delim: str, index) -> str:
        # No SPLIT_PART — emulate 1-based via nested SUBSTRING_INDEX.
        return f"SUBSTRING_INDEX(SUBSTRING_INDEX({expr}, {delim}, {index}), {delim}, -1)"

    def date_difference(self, start, end, unit="year", semantics="completed_units"):
        if (unit or "year").lower() != "year":
            raise ViewGenerationError(f"date_difference v1 supports unit='year' only (got {unit!r}).")
        if semantics == "boundary_count":
            return f"(YEAR({end}) - YEAR({start}))"
        if semantics == "completed_units":
            return f"TIMESTAMPDIFF(YEAR, {start}, {end})"
        raise ViewGenerationError(
            "date_difference(semantics='symbolic_interval') is unsupported on MySQL — "
            "no native yrs+mos+days interval. Author completed_units/boundary_count per unit."
        )


_DIALECTS = {
    "postgres":   PostgresDialect(),
    "postgresql": PostgresDialect(),  # alias for back-compat with --platform
    "snowflake":  SnowflakeDialect(),
    "databricks": DatabricksDialect(),
    "bigquery":   BigQueryDialect(),
    "mysql":      MySQLDialect(),
    "duckdb":     DuckDBDialect(),
    "ansi":       Dialect(),
}


def get_dialect(name: str) -> Dialect:
    """Return a Dialect instance by name.

    None / empty string → PostgresDialect (pre-Phase-7 default).
    Non-empty unknown names raise ValueError (ADR-9 fail-closed).
    """
    key = (name or "").strip().lower()
    if not key:
        return _DIALECTS["postgres"]
    d = _DIALECTS.get(key)
    if d is None:
        known = ", ".join(sorted(_DIALECTS))
        raise ValueError(f"Unknown dialect '{key}'. Known dialects: {known}")
    return d

OUTPUT_DATASETS_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $product_uri})
      -[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
RETURN ods.uri AS uri,
       coalesce(ods.physicalName, ods.name) AS physical_name
ORDER BY physical_name
"""

# Phase 2 of implementingdatatransformations.md: read the optional dataset-
# level :DatasetTransform attached to the output dataset. Only filterPredicate
# and dedupeJson activate in Phase 2; the rest are reserved for later phases
# (joins → Phase 4, grouping_keys → Phase 3, window_specs → Phase 6, etc.).
# No row = no CTE wrapping, view collapses to today's flat shape.
DATASET_TRANSFORM_QUERY = """\
MATCH (ods:DProdOutputDataset {uri: $output_dataset_uri})-[:HAS_DATASET_TRANSFORM]->(dt:DatasetTransform)
RETURN dt LIMIT 1
"""

MAPPINGS_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $product_uri})
      -[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset {uri: $output_dataset_uri})
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (cm:ColumnMapping {isCurrent: true, status: 'approved'})
      -[:MAPS_TO_PRODUCT_COLUMN]->(pc)
// Source can be :Column (catalog: source-aligned / legacy dpe-cf) or
// :DProdColumn (consumer-aligned: source rows come from another product's
// already-published view). literal-kind mappings have no source edge so
// both OPTIONAL MATCHes return null and the row contributes nothing to
// the FROM clause.
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc_col:Column)<-[:HAS_COLUMN]-(ds:Dataset)
// IMPORTANT: bind the source-product output dataset to `src_ods`, NOT `ods`.
// `ods` is already bound at the top of this query to the CONSUMER's output
// dataset; reusing the name here makes Cypher treat it as an equality
// constraint (source ods == consumer ods), which can never match across
// products, so every dprod-sourced mapping falls through to source_kind =
// 'literal' and the script aborts with "every mapping is literal".
OPTIONAL MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc_dpc:DProdColumn)
               <-[:HAS_PRODUCT_COLUMN]-(src_ods:DProdOutputDataset)
               <-[:DPROD_OUTPUT_DATASET]-(:DProdOutputPort)
               <-[:DPROD_OUTPUT_PORT]-(srcDp:DProdDataProduct)
RETURN
    pc.name AS product_col,
    pc.dataType AS product_type,
    coalesce(sc_col.name, sc_dpc.name) AS source_col,
    coalesce(sc_col.dataType, sc_dpc.dataType) AS source_type,
    // Catalog-side raw schema/table — null when source is a DProdColumn
    ds.schema AS source_schema_raw,
    ds.name AS source_table_raw,
    ds.uri AS dataset_uri,
    // DProd-side context — null when source is a Column
    srcDp.uri AS source_dprod_product_uri,
    srcDp.name AS source_dprod_product_name,
    src_ods.uri AS source_dprod_dataset_uri,
    coalesce(src_ods.physicalName, src_ods.name) AS source_dprod_dataset_physical,
    // Discriminator: which path matched. 'literal' when neither.
    CASE
      WHEN sc_col IS NOT NULL THEN 'catalog'
      WHEN sc_dpc IS NOT NULL THEN 'dprod'
      ELSE 'literal'
    END AS source_kind,
    cm.uri AS mapping_uri,
    cm.mappingType AS mapping_type,
    cm.transformKind AS transform_kind,
    cm.transformExpression AS transform_expression,
    cm.transformInputs AS transform_inputs_json,
    cm.transformParams AS transform_params_json,
    cm.transformDecorators AS transform_decorators_json,
    // Phase 4 of transform-portability.md: a raw transformExpression is authored
    // in a specific SQL dialect (legacy rows are implicitly Postgres). Surfaced —
    // coalesced to 'postgres' — so the capability validator parses with the right
    // grammar instead of misreading it as neutral. transformSchemaVersion stamps
    // the neutral-DSL revision (v1 backfill for pre-portability rows).
    coalesce(cm.expressionDialect, 'postgres')  AS expression_dialect,
    coalesce(cm.transformSchemaVersion, 'v1')   AS transform_schema_version,
    // Phase 3 of implementingdatatransformations.md: aggregation. Both fields
    // are no-ops when the dataset has no grouping_keys; coalesced so older
    // mappings persisted before Phase 3 don't surface null.
    coalesce(cm.aggregateFunction, '') AS aggregate_function,
    coalesce(cm.groupingKey, false)    AS grouping_key,
    // Phase 5 of implementingdatatransformations.md: SCD-1 rendering.
    // Surfaced so view-DDL can synthesize a dedupe block from
    // :DatasetTransform.scdPolicy='latest_only' when no explicit dedupe
    // is declared — natural key is built from product columns flagged
    // isPrimaryKey here.
    coalesce(pc.isPrimaryKey, false)   AS is_primary_key
ORDER BY pc.name
"""

# Lowercased substring patterns indicating a column carries temporal /
# history semantics. Ordered most-specific first — `_pick_temporal_order_column`
# returns the first matching column, so "to_date" wins over "_at" if both are
# present. Used by:
#   - auto-temporal-bridge wrapping in `_build_from_fk_inferred`: when a
#     bridge table has any matching column, the bridge LEFT JOIN is wrapped
#     in ROW_NUMBER() OVER (PARTITION BY <fk> ORDER BY <temporal> DESC) = 1
#     so the bridge contributes the latest row per natural key (eliminates
#     row-multiplication when the bridge is a history table).
#   - multiplication-risk warnings: non-bridge mapped tables that hit one
#     of these patterns AND aren't covered by an explicit
#     :DatasetTransform.dedupe land in summary['multiplication_warnings']
#     so the engineer is prompted to declare dedupe.
#
# Detection is intentionally conservative — pure pattern match, no profiling.
# Engineers can override via explicit :DatasetTransform.dedupe / .joins[] when
# the heuristic misses or overreaches.
_TEMPORAL_PATTERNS = (
    # End-of-validity / explicit-history markers (highest priority)
    "to_date", "valid_to", "end_date", "expiration_date", "expiry",
    # Start-of-validity / effective-from markers
    "from_date", "valid_from", "effective_date", "effective_from", "start_date",
    # Audit timestamps (most-specific names first)
    "updated_at", "modified_at", "_updated", "_modified",
    # Last-resort generic suffix patterns
    "_at", "_ts", "created_at", "created_on",
)


def _pick_temporal_order_column(column_names):
    """Return the column to ORDER BY DESC for latest-row semantics, or None.

    Preference order matches `_TEMPORAL_PATTERNS` (most-specific first); among
    columns matching the same pattern, the first one in iteration order wins.
    Matching is substring on lowercased names.
    """
    if not column_names:
        return None
    lower_names = [(n.lower(), n) for n in column_names if n]
    for pattern in _TEMPORAL_PATTERNS:
        for low, orig in lower_names:
            if pattern in low:
                return orig
    return None


# Plain row-update audit timestamps — present on current-state tables (one row
# per key already), NOT SCD effective-dating. Their presence alone doesn't imply
# row multiplication, so a satellite whose ONLY temporal column is one of these
# is not auto-deduped. Module-level so both `_satellite_needs_dedup` (join-time
# auto-dedup) and the multiplication-warning detector share one definition.
_AUDIT_TS_COLS = {"created_at", "updated_at", "modified_at",
                  "last_modified", "last_modified_at", "last_updated"}

# Current-row flag column names — when an SCD satellite carries one, the latest
# row is the one with the flag set true, so we narrow to it inside the
# ROW_NUMBER dedup subquery (matches how PO-authored `latest` lookups filter,
# e.g. `filter_clause: "is_current = true"`).
_IS_CURRENT_PATTERNS = ("is_current", "iscurrent", "is_active", "current_flag",
                        "active_flag", "is_latest")

# Column-name tokens that signal the column is meant to hold a DISCRETE band /
# grade / tier rather than a raw value. Used by the semantic guard that flags a
# band-named column whose transform does no bucketing (would leak the raw
# source value). Substring match on the lowercased product-column name.
_BAND_NAME_TOKENS = ("band", "grade", "tier", "bracket", "bucket")


def _pick_is_current_column(column_names):
    """Return a boolean current-row flag column on the table, or None.

    Exact-match (case-insensitive) against `_IS_CURRENT_PATTERNS` so we don't
    misfire on substrings like `current_salary`.
    """
    if not column_names:
        return None
    lower = {n.lower(): n for n in column_names if n}
    for pat in _IS_CURRENT_PATTERNS:
        if pat in lower:
            return lower[pat]
    return None


def _satellite_needs_dedup(table_key, meta):
    """Decide whether a mapped, non-anchor (LEFT-joined) source table must be
    deduped to its current row before contributing columns.

    A satellite that carries genuine SCD effective-dating columns multiplies
    rows per natural key — so a passthrough column OR a lookup keyed off that
    satellite (e.g. job_title resolved via job_assignment_history.job_id) fans
    the view out to one row per historical version. Returns True for such
    tables. Guards mirror the multiplication-warning detector so the two stay
    consistent: surrogate-keyed change logs (`audit_log`) and fact-grain tables
    legitimately hold many rows per key, and a plain audit timestamp marks a
    current-state row rather than effective-dated history.
    """
    cols = (meta or {}).get("column_names") or []
    temporal_col = _pick_temporal_order_column(cols)
    if not temporal_col:
        return False
    # Strong SCD signals override a coarse relationshipKind label. A LEFT-joined
    # satellite that carries a current-row flag (`is_current`) OR an effective-
    # dating from/to pair is an effective-dated dimension: exactly ONE row per
    # natural key is wanted at the consumer's grain, so it MUST be deduped even
    # when the relationship-kind classifier tagged it `audit_log`. This is the
    # job_assignment_history case — labelled audit_log but shaped as an SCD-2
    # dimension (is_current + effective_from/effective_to), feeding the
    # job_title lookup; without dedup it fans the view out to one row per
    # historical assignment.
    lower = {c.lower() for c in cols if c}
    has_is_current = _pick_is_current_column(cols) is not None
    _FROM_MARKERS = ("effective_from", "valid_from", "from_date", "start_date")
    _TO_MARKERS = ("effective_to", "valid_to", "to_date", "end_date", "expiration")
    has_eff_pair = (
        any(any(p in c for p in _FROM_MARKERS) for c in lower)
        and any(any(p in c for p in _TO_MARKERS) for c in lower)
    )
    if has_is_current or has_eff_pair:
        return True
    # No explicit current-row semantics — be conservative and mirror the
    # multiplication-warning guards: surrogate-keyed change logs (`audit_log`)
    # and fact-grain tables legitimately hold many rows per key, and a plain
    # audit timestamp marks a current-state row rather than effective-dated
    # history.
    rkind = ((meta or {}).get("relationship_kind") or "").lower()
    if rkind in ("audit_log", "fact"):
        return False
    if temporal_col.lower() in _AUDIT_TS_COLS:
        return False
    return True


# Effective-dating start/end markers — shared by the satellite-dedup detector,
# the as-of bridge builder, and the cross-product bridge fallback. Module-level
# so the temporal-correctness logic stays consistent across all three.
_EFF_FROM_MARKERS = ("effective_from", "valid_from", "from_date", "start_date",
                     "effective_date")
_EFF_TO_MARKERS = ("effective_to", "valid_to", "to_date", "end_date", "expiration",
                   "expiry")

# Plausible join-key suffixes — mirrors join_preflight._looks_like_key so the
# generator and the planner bridge on the same notion of "key column".
_KEY_SUFFIXES = ("_id", "_code", "_key", "_no", "_number")


def _looks_like_key(col):
    c = (col or "").lower()
    return c == "id" or any(c.endswith(s) for s in _KEY_SUFFIXES)


def _pick_effective_from_column(column_names):
    """Return the effective-FROM (start-of-validity) column, or None.

    Distinct from `_pick_temporal_order_column` (which prefers end/audit markers
    for latest-row ORDER BY) — the as-of pivot needs the START of each validity
    span. Substring match, most-specific markers first."""
    if not column_names:
        return None
    lower = [(n.lower(), n) for n in column_names if n]
    for marker in _EFF_FROM_MARKERS:
        for low, orig in lower:
            if marker in low:
                return orig
    return None


def _pick_effective_to_column(column_names):
    """Return the effective-TO (end-of-validity) column, or None."""
    if not column_names:
        return None
    lower = [(n.lower(), n) for n in column_names if n]
    for marker in _EFF_TO_MARKERS:
        for low, orig in lower:
            if marker in low:
                return orig
    return None


def _is_effective_dated(cols):
    """True when a table carries an effective-dating from/to pair (an SCD-2
    history shape), i.e. it holds multiple validity-spanned rows per key."""
    return bool(_pick_effective_from_column(cols) and _pick_effective_to_column(cols))


def _shared_key_columns(cols_a, cols_b, preferred=None):
    """Key-looking columns present in BOTH column lists, case-insensitive,
    `preferred` (the grain's natural key) first when shared. The basis for a
    cross-product bridge: `:REFERENCES` never crosses source products, so two
    tables from different products are joined on a shared identity key instead."""
    a = {c.lower(): c for c in (cols_a or []) if c}
    b = {c.lower(): c for c in (cols_b or []) if c}
    shared = [a[k] for k in a if k in b and _looks_like_key(a[k])]
    shared.sort(key=lambda c: c.lower())
    if preferred:
        pl = preferred.lower()
        shared.sort(key=lambda c: 0 if c.lower() == pl else 1)
    return shared


_KEY_STEM_RE = re.compile(r"(_id|_code|_key|_no|_number)$")


def _score_chain_key(key, stuck_key, partner_key, preferred=None):
    """Rank a candidate (partner, key) pair for transitive chain bridging.

    Deterministic, documented as a *ranking*, not a guess: a table with no
    shared identity key on any partner still falls through to junction
    discovery / unbridged. Mirrored by join_preflight._score_chain_key —
    keep the two in sync so preflight recommendations match generated SQL.

      +2 the key's stem names either endpoint table (`customer_id` bridging
         into a table named `customer` / `vw_customer`) — the strongest
         signal that the key is a genuine identity link, not an incidental
         shared column;
      +1 the key is the declared grain natural key (dedupe/grouping-derived).
    """
    score = 0
    stem = _KEY_STEM_RE.sub("", (key or "").lower())
    if stem:
        for k in (stuck_key, partner_key):
            token = _bridge_name_token(k or "")
            if token and (stem in token or token in stem):
                score += 2
                break
    if preferred and key.lower() == preferred.lower():
        score += 1
    return score


def _rank_junction_candidates(candidates, joined_order, columns_by_key,
                              stuck_key, stuck_cols, preferred=None):
    """Rank consumed-but-unmapped datasets that could bridge `stuck_key` onto
    the already-joined set as a pure junction (Gap B).

    Valid only when the candidate shares DIFFERENT identity keys with the two
    sides (`transaction`: `account_id` with the joined side, `customer_id`
    with the stuck side) — same-key sharing is the shared-dimension case the
    transitive chain handles. Score mirrors `_rank_bridge_paths`:
    endpoint-name containment (0..2) × 10 + relationshipKind weight. Mirrored
    by join_preflight._rank_junction_candidates — keep in sync.

    Returns list of dicts sorted by score desc (ties broken by table name).
    """
    stuck_token = _bridge_name_token(stuck_key)
    ranked = []
    for cand in candidates or []:
        cand_cols = cand.get("cols") or []
        keys_to_stuck = _shared_key_columns(cand_cols, stuck_cols, preferred=preferred)
        if not keys_to_stuck:
            continue
        best_partner = None  # (rank, partner_key, kJ)
        for order_idx, j_key in enumerate(joined_order):
            j_cols = (columns_by_key.get(j_key) or {}).get("column_names") or []
            for k in _shared_key_columns(cand_cols, j_cols, preferred=preferred):
                sc = _score_chain_key(k, cand["key"], j_key, preferred)
                rank = (sc, -order_idx)
                if best_partner is None or rank > best_partner[0]:
                    best_partner = (rank, j_key, k)
        if best_partner is None:
            continue
        _, partner_key, k_joined = best_partner
        k_stuck = None
        for k in sorted(keys_to_stuck,
                        key=lambda c: -_score_chain_key(c, cand["key"], stuck_key, preferred)):
            if k.lower() != k_joined.lower():
                k_stuck = k
                break
        if k_stuck is None:
            continue
        partner_token = _bridge_name_token(partner_key)
        cand_token = _bridge_name_token(cand["key"])
        primary = int(bool(stuck_token) and stuck_token in cand_token) + \
                  int(bool(partner_token) and partner_token in cand_token)
        kind = (cand.get("relationship_kind") or "").lower()
        secondary = _RELATIONSHIP_KIND_WEIGHTS.get(kind, 0)
        ranked.append({
            "cand": cand, "partner_key": partner_key,
            "key_joined": k_joined, "key_stuck": k_stuck,
            "score": primary * 10 + secondary,
        })
    ranked.sort(key=lambda r: (-r["score"], r["cand"]["key"]))
    return ranked


# Prefix-scoped so we get the FULL FK graph for the project, not just edges
# touching mapped tables. _resolve_bridges does BFS over this graph to find
# unmapped junction tables that connect otherwise-unreachable mapped tables.
FK_QUERY = """\
MATCH (ds1:Dataset)-[r:REFERENCES]->(ds2:Dataset)
WHERE any(p IN $catalog_prefixes WHERE ds1.uri STARTS WITH p)
   OR any(p IN $catalog_prefixes WHERE ds2.uri STARTS WITH p)
RETURN ds1.schema AS from_schema, ds1.name AS from_table, ds1.uri AS from_uri,
       r.columns AS fk_columns,
       ds2.schema AS to_schema, ds2.name AS to_table, ds2.uri AS to_uri,
       r.referencedColumns AS pk_columns
"""

# Dprod-side FK lookup. odcs.py:_generate_dprod mirrors catalog :Dataset
# -[:REFERENCES]-> edges onto :DProdOutputDataset nodes during source-product
# materialization (DPROD_PROPAGATE_FK). This query reads them so consumer
# view-DDL can FK-join across the source product's per-dataset views. Scope
# is the source-product contract prefix (`dprod:ds:{contract_id}:`) so we
# get every FK edge within each source product the consumer reads from —
# including edges between datasets the consumer didn't directly map, which
# is what bridge-table BFS needs. The rows are post-processed in Python into
# the same `(view_schema, vw_<safe>)` tuple shape `_resolve_source_relation`
# returns for dprod sources — so `_build_from_fk_inferred` can match them
# against `tables[]` keys without further translation.
DPROD_FK_QUERY = """\
MATCH (ods1:DProdOutputDataset)-[r:REFERENCES]->(ods2:DProdOutputDataset)
WHERE any(p IN $dprod_contract_prefixes WHERE ods1.uri STARTS WITH p)
   OR any(p IN $dprod_contract_prefixes WHERE ods2.uri STARTS WITH p)
RETURN coalesce(ods1.physicalName, ods1.name) AS from_table_raw,
       ods1.uri AS from_uri,
       coalesce(ods2.physicalName, ods2.name) AS to_table_raw,
       ods2.uri AS to_uri,
       r.columns           AS fk_columns,
       r.referencedColumns AS pk_columns
"""

# Gap-B junction candidates: every OTHER output dataset of the source products
# this consumer :CONSUMES. A candidate sharing DIFFERENT identity keys with two
# disconnected mapped tables (transaction: account_id ↔ customer_id) can be
# synthesized as a PURE bridge — join-only, DISTINCT-projected, contributing no
# SELECT columns. CONSUMES-only by design: a dataset in a product the consumer
# never subscribed to must not silently become a dependency. Mirrors
# join_preflight._CONSUMED_DATASETS_QUERY — keep in sync.
CONSUMED_DATASETS_QUERY = """\
MATCH (dp:DProdDataProduct {uri:$product_uri})<-[:MATERIALISES_AS]-(dc:DataContract)
MATCH (dc)-[:CONSUMES]->(src:DProdDataProduct)
MATCH (src)-[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
WHERE NOT ods.uri IN $exclude_uris
MATCH (ods)-[:HAS_PRODUCT_COLUMN]->(c:DProdColumn)
OPTIONAL MATCH (src)-[:SERVED_BY]->(sd:ServingDefinition)
WITH ods, src, collect(DISTINCT c.name) AS cols,
     collect(DISTINCT sd.deploymentStatus) AS dep
RETURN ods.uri AS uri,
       coalesce(ods.physicalName, ods.name) AS phys,
       coalesce(ods.relationshipKind, '') AS relationship_kind,
       src.uri AS product_uri, src.name AS product_name,
       CASE WHEN 'deployed' IN dep THEN 'deployed'
            ELSE coalesce(dep[0], 'pending') END AS src_deployment_status,
       cols
"""

# Fetch column-name lists for every in-scope table — mapped tables + any
# auto-bridges. Used downstream to detect temporal columns (history-table
# patterns) and decide whether to wrap a bridge JOIN in ROW_NUMBER = 1
# latest-row semantics, or to surface a multiplication-risk warning for a
# non-bridge mapped table.
#
# Returns raw catalog (schema, name) and dprod physical_name. The Python
# caller composes the matching `{schema}.{table}` key via `_safe_name`,
# matching exactly the convention used to build `tables[]` keys.
TABLE_COLUMNS_QUERY = """\
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
WHERE ds.uri IN $catalog_uris
OPTIONAL MATCH (ds)-[:HAS_TABLE_DESCRIPTION]->(td:TableDescription)
WHERE td.isCurrent = true
WITH ds, td, collect(col.name) AS cols
RETURN ds.uri AS uri, 'catalog' AS kind,
       ds.schema AS schema, ds.name AS name, cols AS column_names,
       coalesce(td.text, '') AS description,
       coalesce(td.relationshipKind, '') AS relationship_kind
UNION
MATCH (ods:DProdOutputDataset)-[:HAS_PRODUCT_COLUMN]->(dpc:DProdColumn)
WHERE ods.uri IN $dprod_uris
WITH ods, collect(dpc.name) AS cols
RETURN ods.uri AS uri, 'dprod' AS kind,
       '' AS schema, coalesce(ods.physicalName, ods.name) AS name, cols AS column_names,
       coalesce(ods.description, '') AS description,
       coalesce(ods.relationshipKind, '') AS relationship_kind
"""


# Phase 4: resolve a dataset URI (catalog :Dataset or :DProdOutputDataset)
# referenced by an explicit join entry to its (schema, table) tuple. The
# caller passes a list of URIs from :DatasetTransform.joins[]; each row is
# (uri, schema, table) where :DProdOutputDataset entries use a synthetic
# schema = '' / table = 'vw_<safe_name(name)>' (mirroring
# _resolve_source_relation's logic for dprod sources).
EXPLICIT_JOIN_RESOLVER_QUERY = """\
OPTIONAL MATCH (ds:Dataset) WHERE ds.uri IN $uris
WITH collect({uri: ds.uri, schema: ds.schema, name: ds.name, kind: 'catalog'}) AS catalog_rows
OPTIONAL MATCH (ods:DProdOutputDataset) WHERE ods.uri IN $uris
// Aggregate the dprod rows on their OWN line — combining `catalog_rows + collect(...)`
// in one projection makes Cypher treat `catalog_rows` as an implicit grouping key
// inside the aggregating expression and rejects it. Carry catalog_rows as an
// explicit grouping key here, then concatenate in a plain (non-aggregating) WITH.
WITH catalog_rows, collect({uri: ods.uri, schema: '', name: coalesce(ods.physicalName, ods.name), kind: 'dprod'}) AS dprod_rows
WITH catalog_rows + dprod_rows AS rows
UNWIND rows AS r
WITH r WHERE r.uri IS NOT NULL
RETURN r.uri AS uri, r.schema AS schema, r.name AS name, r.kind AS kind
"""

PRODUCT_NAME_QUERY = """\
MATCH (dp:DProdDataProduct {uri: $product_uri})
RETURN dp.name AS name
"""


def _safe_name(name):
    return re.sub(r'[^a-zA-Z0-9_]', '_', name).lower()


# Consumer-over-materialized-source served map: {_safe_name(source_dataset_physical):
# "catalog.schema.relation"}. When a dpe-cf consumer :CONSUMES a source product
# that was materialized/transferred to a DISTINCT platform (e.g. a MySQL source
# loaded into Databricks), the FROM clause must reference the source's REAL
# materialized tables — not a co-located `vw_<name>` in the consumer's own schema.
# Set per-invocation by the public entry points (generate_ddl / generate_dbt_models /
# generate_lakehouse_models) from the --source-served-map arg; empty = the original
# co-located behavior, byte-for-byte.
_SOURCE_SERVED_MAP = {}


def _set_source_served_map(m):
    """Set the module-level served map for this invocation (reset each entry)."""
    global _SOURCE_SERVED_MAP
    _SOURCE_SERVED_MAP = dict(m or {})


def _served_relation(physical_name):
    """(schema, table) for a source dataset materialized elsewhere, or None.

    ``schema`` may be a dotted ``catalog.schema`` for 3-level platforms — the FROM
    builder emits ``schema.table`` raw (no quoting), so ``catalog.schema.relation``
    resolves correctly on Databricks/Snowflake."""
    served = _SOURCE_SERVED_MAP.get(_safe_name(physical_name or ""))
    if not served:
        return None
    schema_part, _sep, table_part = served.rpartition(".")
    if not schema_part:
        return None
    return schema_part, table_part


def _source_col_ref(m):
    """The physical column identifier to reference a source column in the
    consumer's SELECT.

    A `dprod` source is ANOTHER product's deployed view, whose columns are
    aliased `_safe_name(product_col)` (see the `AS {_safe_name(pc_name)}` emit).
    So a reference to a PO-authored consumer/aggregate column like `Mixed Case`
    must use its safe identifier `mixed_case`, or the FROM breaks with "column
    does not exist". This is a STRICT NO-OP for a source-aligned upstream (whose
    columns were already snake_cased by column-name standardization) and for
    catalog (raw-table) sources (whose real column names are kept verbatim).
    """
    col = m.get("source_col") or ""
    if (m.get("source_kind") or "") == "dprod":
        return _safe_name(col)
    return col


def _dprod_relation(physical_name, view_schema):
    """(schema, table) for a dprod source dataset: the served location when the
    source was materialized to a distinct target, else the co-located
    ``vw_<safe_name>`` in the consumer's own ``view_schema`` (v1 same-instance)."""
    served = _served_relation(physical_name)
    if served is not None:
        return served
    return view_schema, f"vw_{_safe_name(physical_name or 'data_product')}"


def _resolve_source_relation(row, view_schema):
    """For one mapping row, compute the (schema, table) of the underlying
    SQL relation the FROM clause should reference.

      catalog → (raw dataset.schema, raw dataset.name)
      dprod   → (view_schema, vw_<safe_name(source_dprod_dataset_physical)>)
                   — source products emit ONE view PER :DProdOutputDataset
                   (line 658: `view_name = f"vw_{_safe_name(dataset_physical_name)}"`),
                   so a consumer reading from N source-product datasets sees
                   N views. v1 assumes both products live in the same
                   Postgres instance + view_schema. Matches the dprod path
                   in EXPLICIT_JOIN_RESOLVER_QUERY.
      literal → (None, None) — the caller filters these out, no FROM entry.

    Source-aligned products run column_name_standardization which sets
    DProdColumn.name to a snake_case + domain-prefixed form. _safe_name on
    that is a no-op, so for SA-produced source columns the column name in
    the consumer's SELECT is identical to row['source_col'] from the mapping.
    A PO-authored consumer/aggregate upstream has no such guarantee (a
    `Mixed Case` column), so column REFERENCES go through `_source_col_ref`,
    which emits `_safe_name(source_col)` for a dprod source (matching the
    upstream view's `AS {_safe_name(pc_name)}` aliasing) while the expression
    substitutor keeps matching on the logical name.
    """
    kind = row.get("source_kind") or ""
    if kind == "dprod":
        ds_phys = row.get("source_dprod_dataset_physical") or "data_product"
        # Served-location-first: when the source product was materialized to a
        # distinct target (e.g. Databricks), FROM its real catalog.schema.table;
        # else the co-located vw_<name> (same-instance v1).
        return _dprod_relation(ds_phys, view_schema)
    if kind == "literal":
        return None, None
    return row.get("source_schema_raw"), row.get("source_table_raw")


def _parse_json(s, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return default


def _bare_col_name(uri):
    """'column:hr.employees.first_name' -> 'first_name'"""
    s = uri.replace("column:", "")
    return s.rsplit(".", 1)[-1] if "." in s else s


def _source_input_uri(m):
    """Build the source-column URI in the format that ColumnMapping.transformInputs
    was populated with by the data-mapping skill. Two shapes exist:

      catalog → ``column:<schema>.<table>.<col>``  (raw catalog source)
      dprod   → ``dprod:col:<contract_id>:<dataset_physical>:<col>``  (CONSUMES'd source product)

    Dispatched on ``source_kind`` because by the time this function runs,
    ``m['source_schema']`` / ``m['source_table']`` have been rewritten by
    ``_resolve_source_relation`` to ``(view_schema, vw_<safe>)`` for dprod
    sources — which doesn't match the dprod URI shape stored in
    transformInputs. Using ``source_schema_raw`` / ``source_table_raw`` on
    the catalog branch keeps the catalog match working regardless of
    whether resolution has already run on this row.
    """
    kind = (m.get("source_kind") or "").lower()
    col = m.get("source_col") or ""
    if kind == "dprod":
        product_uri = m.get("source_dprod_product_uri") or ""
        contract_id = product_uri[len("dprod:"):] if product_uri.startswith("dprod:") else product_uri
        dataset = m.get("source_dprod_dataset_physical") or ""
        return f"dprod:col:{contract_id}:{dataset}:{col}"
    schema = m.get("source_schema_raw") or m.get("source_schema") or ""
    table = m.get("source_table_raw") or m.get("source_table") or ""
    return f"column:{schema}.{table}.{col}"


def _substitute_inputs(expression, mappings_for_pc, alias_lookup):
    """
    Replace bare column names in `expression` with aliased references.
    Driven by mappings_for_pc, each row of which is one source column for
    this product column. Uses word-boundary regex so 'id' does not match
    inside 'customer_id'.

    If the mapping carries a transformInputs JSON array, prefer that as the
    authoritative input list; otherwise fall back to all source columns
    seen in mappings_for_pc.
    """
    expr = expression

    # Resolve each input to its join alias and the set of table qualifiers it
    # might appear under in the authored expression, so a source-qualified
    # identifier — e.g. CAST(job.grade AS varchar(20)) — gets the same alias
    # rewrite a plain column ref gets, instead of being emitted verbatim
    # against a table that isn't in the FROM scope.
    #
    # The qualifier in the authored SQL can be ANY of:
    #   - the LOGICAL source name (dprod dataset physical, e.g. "job") — what
    #     the mapping skill typically writes;
    #   - the RESOLVED relation name (e.g. "vw_job") that the FROM clause joins;
    #   - the raw catalog table name.
    # alias_lookup is keyed on the resolved "{schema}.{table}", so we look the
    # alias up there but rewrite every plausible qualifier form to it.
    resolved = []  # (col_name, [table qualifiers], [schema qualifiers], aliased_ref)

    def _add(row):
        alias = alias_lookup.get(f"{row.get('source_schema')}.{row.get('source_table')}")
        if not alias:
            return
        col = row.get("source_col")
        if not col:
            return
        tables = [t for t in (row.get("source_table"),
                              row.get("source_table_raw"),
                              row.get("source_dprod_dataset_physical")) if t]
        schemas = [s for s in (row.get("source_schema"),
                               row.get("source_schema_raw")) if s]
        # Match the authored expression on the LOGICAL column name (what the
        # mapper wrote), but emit the PHYSICAL identifier — a dprod source's
        # deployed view aliases columns _safe_name(product_col), so a CF
        # upstream's `Mixed Case` column must be referenced as `mixed_case`.
        resolved.append((col, list(dict.fromkeys(tables)),
                         list(dict.fromkeys(schemas)), f"{alias}.{_source_col_ref(row)}"))

    # Prefer explicit transformInputs (dispatched on source_kind via
    # _source_input_uri), then ALWAYS also fold in the mapping's own source
    # rows — robust when a transformInputs URI doesn't resolve (older/edited
    # mappings) so the expression still gets aliased.
    inputs_json = mappings_for_pc[0].get("transform_inputs_json")
    parsed_inputs = _parse_json(inputs_json, []) if inputs_json else []
    if parsed_inputs:
        uri_to_row = {_source_input_uri(m): m for m in mappings_for_pc}
        for input_uri in parsed_inputs:
            row = uri_to_row.get(input_uri)
            if row is not None:
                _add(row)
    for m in mappings_for_pc:
        _add(m)

    # Longest col first so 'customer_id' is handled before 'id'.
    resolved.sort(key=lambda x: len(x[0]), reverse=True)

    # Rewrite most-qualified first so each occurrence is rebound exactly once
    # and already-alias-qualified refs (t2.grade) are left alone. The negative
    # lookbehind `(?<![\w.])` stops a tier matching inside a longer qualified
    # identifier or re-touching a prior rewrite.
    def _rewrite(parts, aliased, s):
        pat = r'(?<![\w.])' + r'\.'.join(re.escape(p) for p in parts) + r'(?!\w)'
        return re.sub(pat, aliased, s)

    # tier 1: schema.table.col
    for col, tables, schemas, aliased in resolved:
        for sch in schemas:
            for tbl in tables:
                expr = _rewrite([sch, tbl, col], aliased, expr)
    # tier 2: table.col  (covers logical 'job', resolved 'vw_job', raw)
    for col, tables, schemas, aliased in resolved:
        for tbl in tables:
            expr = _rewrite([tbl, col], aliased, expr)
    # tier 3: bare col
    seen = set()
    for col, tables, schemas, aliased in resolved:
        if col in seen:
            continue
        seen.add(col)
        expr = _rewrite([col], aliased, expr)

    return expr


def _substitute_product_aliases(expression, dep_names):
    """Rewrite bare product-column references in a derived-on-derived
    expression to the safe_name aliases they carry in the prior CTE layer.

    Used for the "enriched" outer projection: a column like
    ``avg_order_value = lifetime_value / NULLIF(total_orders, 0)`` references
    OTHER product columns (lifetime_value, total_orders), not source columns.
    Those product columns are projected by an inner CTE under their safe_name
    alias, so the expression only needs the dependency names normalised to
    safe_name (usually a no-op since product names are already snake_case).
    Longest-first so 'order_total' is replaced before 'order'.
    """
    expr = expression
    for name in sorted({d for d in dep_names if d}, key=len, reverse=True):
        safe = _safe_name(name)
        if safe == name:
            continue
        pattern = r'(?<![\w.])' + re.escape(name) + r'(?!\w)'
        expr = re.sub(pattern, safe, expr)
    return expr


def _apply_decorators(expr, decorators_json, dialect=None):
    """Wrap expr in standardisation functions per decorator config."""
    if dialect is None:
        dialect = _DIALECTS["postgres"]
    decorators = _parse_json(decorators_json, {})
    if not decorators:
        return expr
    standardization = decorators.get("standardization", []) or []
    for op in standardization:
        op = op.lower()
        if op == "trim":
            expr = f"TRIM({expr})"
        elif op == "upper":
            expr = f"UPPER({expr})"
        elif op == "lower":
            expr = f"LOWER({expr})"
        elif op == "normalize_whitespace":
            expr = dialect.regexp_replace_global(expr, "'\\\\s+'", "' '")
    default_if_null = decorators.get("default_if_null")
    if default_if_null is not None:
        # Quote string defaults; numbers passed through
        if isinstance(default_if_null, str):
            quoted = "'" + default_if_null.replace("'", "''") + "'"
            expr = f"COALESCE({expr}, {quoted})"
        else:
            expr = f"COALESCE({expr}, {default_if_null})"
    return expr


_VALID_AGG_FUNCTIONS = {
    "SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT",
}


def _compile_lookup(pc_name, mappings_for_pc, alias_lookup, lookup_aliases, lookup_meta, view_schema="public"):
    """
    Compile a lookup kind: register a LEFT JOIN derived-table (or plain table)
    and return the value-column reference as the SELECT expression.

    Two-pass model:
      - Pass 1 (here): accumulate per-join metadata, including the full set of
        value_columns each shared scan needs to project. Return the SELECT
        expression referencing the not-yet-rendered alias.
      - Pass 2 (in `generate_ddl`): render each accumulated meta entry to its
        LEFT JOIN SQL fragment. The derived-table inner SELECT projects every
        accumulated value_column at once.

    transformParams expected shape (equi-join, default):
      {"lookup_table": "ref.iso_country", "key_column": "code",
       "value_column": "name"}

    selection_strategy extensions:
      - "latest"    — derived table with ROW_NUMBER() = 1.
                      Requires order_by_column; order_by_direction defaults DESC.
      - "aggregate" — derived table with GROUP BY key.
                      Requires aggregate_function from _VALID_AGG_FUNCTIONS.
      - "exists"    — same equi-join shape; the SELECT expression becomes
                      `(lk.key IS NOT NULL)` instead of `lk.value_column`.
      - "asof"      — point-in-time join against an effective-dated reference.
                      Requires effective_from_column + pivot_column (the anchor's
                      as-of date); effective_to_column optional. Emits an
                      interval-overlap predicate so each anchor row picks the
                      reference row valid at its pivot date. Use for SCD-2 grains.
      - "equi"      — today's behavior (omit selection_strategy = "equi").

    Optional filter_clause is applied inside the derived-table subquery
    (latest / aggregate) or as an AND on the equi-join ON clause (equi / exists).
    """
    m = mappings_for_pc[0]
    params = _parse_json(m.get("transform_params_json"), {})
    lookup_table = params.get("lookup_table")
    # The mapper-authored transformExpression (if any) references the lookup
    # table by its ORIGINAL logical name (e.g. `salary_history.amt`), not the
    # dprod-rewritten vw_ form — capture it before the rewrite below.
    orig_lookup_table = lookup_table
    key_column = params.get("key_column")
    value_column = params.get("value_column")
    strategy = (params.get("selection_strategy") or "equi").strip().lower()
    transform_expression = (m.get("transform_expression") or "").strip()
    # Raw aggregate EXPRESSION over the lookup table's columns — the composite
    # case a single aggregate_function can't express (e.g. the churn score:
    # GREATEST(0, LEAST(100, (CURRENT_DATE - MAX(txn_timestamp)::date) * 2
    # - COUNT(txn_id) FILTER (WHERE txn_timestamp >= CURRENT_DATE - 90) * 5))).
    # Emitted verbatim inside the GROUP BY derived table as `... AS agg_value`;
    # supersedes aggregate_function / value_column when present.
    aggregate_expression = (params.get("aggregate_expression") or "").strip()

    # ── dprod-mode lookup-table rewrite ─────────────────────────────────
    # When the mapping is sourced from a CONSUMES'd source product
    # (source_kind=='dprod'), the lookup_table named in transformParams is
    # a logical reference to one of that source product's
    # :DProdOutputDataset rows — but the actual SQL relation is the
    # generated `{view_schema}.vw_<safe_name>` view (same convention
    # _resolve_source_relation applies to the FROM-side table). Without
    # this rewrite the lookup join would `FROM order_header` instead of
    # `FROM public.vw_order_header` and the redeploy fails with
    # `relation "order_header" does not exist`.
    if lookup_table and (m.get("source_kind") or "").lower() == "dprod":
        # Resolve ANY logical reference to the generated view: bare names
        # (`transaction_ledger`), source-schema-qualified names
        # (`analytics.transaction_ledger`), and the UI picker's
        # product-name-qualified form (`Transaction Ledger.transaction_ledger`
        # — SOURCE_COLUMNS_QUERY labels dprod tables with the product name,
        # which is NOT a SQL schema and emitted verbatim breaks the deploy).
        # The ONLY override escape hatch is an explicit vw_-prefixed relation
        # (optionally schema-qualified) — that's the engineer deliberately
        # targeting a deployed view by its real name.
        bare = lookup_table.rsplit(".", 1)[-1].strip()
        if not bare.startswith("vw_"):
            # Served-location-first: a materialized source's lookup table is its
            # real catalog.schema.relation; else the co-located vw_<name>.
            _served = _served_relation(bare)
            if _served is not None:
                lookup_table = f"{_served[0]}.{_served[1]}"
            else:
                lookup_table = f"{view_schema}.vw_{_safe_name(bare)}"

    # exists doesn't need a value_column — the boolean output ignores it.
    # Neither does an aggregate with a raw aggregate_expression (the
    # expression IS the projected value).
    needs_value = strategy != "exists" and not (
        strategy == "aggregate" and aggregate_expression
    )
    if not (lookup_table and key_column) or (needs_value and not value_column):
        # Malformed/incomplete lookup params. If the mapper still authored a
        # derived expression, honor it (resolve main-source refs) rather than
        # silently collapsing to the bare source column — emitting wrong data
        # is worse than honoring intent.
        if transform_expression:
            expr = _substitute_inputs(transform_expression, mappings_for_pc, alias_lookup)
            return _apply_decorators(expr, m.get("transform_decorators_json"))
        tkey = f"{m['source_schema']}.{m['source_table']}"
        alias = alias_lookup.get(tkey, "?")
        return f"{alias}.{_source_col_ref(m)}"

    tkey = f"{m['source_schema']}.{m['source_table']}"
    src_alias = alias_lookup.get(tkey, "?")

    # ── Misauthoring guard: self-join detection ─────────────────────────
    # If transformInputs anchored the mapping on the lookup table itself
    # (i.e. m['source_table'] == params.lookup_table) AND we're in a
    # derived-subquery strategy (latest/aggregate/exists), the generator
    # would emit a self-join — and the FROM-clause FK BFS would later fail
    # with a generic "no FK path" error that masks the real cause. Raise
    # a targeted error pointing at the authoring fix instead. See the
    # `transformInputs anchor rule for lookups` section of the
    # data-mapping-neo4j SKILL.md.
    if strategy in ("latest", "aggregate", "exists"):
        anchor_table = (m.get("source_table") or "").strip()
        lookup_table_bare = lookup_table.rsplit(".", 1)[-1].strip()
        if anchor_table and anchor_table == lookup_table_bare:
            raise ViewGenerationError(
                f"Mapping {m.get('mapping_uri', '<unknown>')} uses a "
                f"'{strategy}' lookup with lookup_table='{lookup_table}', "
                f"but transformInputs anchors on the lookup table itself "
                f"(source_table='{anchor_table}'). This would cause a "
                f"self-join.\n\n"
                f"Fix: transformInputs[0] must point at the column on the "
                f"MAIN (FROM-side) table that joins to params.key_column "
                f"('{key_column}') on the lookup table. The lookup table "
                f"should appear ONLY inside the LEFT JOIN derived "
                f"subquery, never as the FROM anchor.\n\n"
                f"See the data-mapping-neo4j skill's "
                f"'transformInputs anchor rule for lookups' section for "
                f"the correct authoring pattern."
            )

    order_by = params.get("order_by_column") or ""
    order_dir = (params.get("order_by_direction") or "DESC").strip().upper()
    if order_dir not in ("ASC", "DESC"):
        order_dir = "DESC"
    agg_fn = (params.get("aggregate_function") or "").strip().upper()
    filter_clause = (params.get("filter_clause") or "").strip()
    # "asof": point-in-time lookup against an effective-dated reference table —
    # pick the row whose [effective_from, effective_to) validity window contains
    # the anchor's pivot date. The grain-correct strategy for an SCD-2 target
    # whose lookup reference is itself history (no bridge needed). Plain LEFT
    # JOIN shape (like equi) + an interval predicate; single row per anchor span.
    asof_from = (params.get("effective_from_column") or "").strip()
    asof_to = (params.get("effective_to_column") or "").strip()
    asof_pivot = (params.get("pivot_column") or "").strip()

    if strategy == "latest" and not order_by:
        strategy = "equi"  # degrade gracefully — engineer must specify order_by
    if strategy == "aggregate" and agg_fn not in _VALID_AGG_FUNCTIONS \
            and not aggregate_expression:
        strategy = "equi"
    if strategy == "asof" and not (asof_from and asof_pivot):
        strategy = "equi"  # degrade — need at least the from-column + pivot

    # Dedup join key — strategy-dependent so two product columns sharing
    # (table, key, strategy, order_by, agg_fn, filter) share one scan. For
    # `aggregate`, agg_fn (or the raw aggregate_expression) is in the key
    # (SUM and AVG can't share an inner SELECT). For `equi`, the join is the
    # raw table — value columns project off it directly, so they share
    # unconditionally.
    join_key = (
        lookup_table, src_alias, m["source_col"], strategy,
        order_by if strategy == "latest" else "",
        (aggregate_expression or agg_fn) if strategy == "aggregate" else "",
        filter_clause,
        (asof_from, asof_to, asof_pivot) if strategy == "asof" else "",
    )

    if join_key in lookup_aliases:
        lk_alias = lookup_aliases[join_key]
    else:
        lk_alias = f"lk{len(lookup_aliases) + 1}"
        lookup_aliases[join_key] = lk_alias
        lookup_meta[join_key] = {
            "lk_alias":      lk_alias,
            "strategy":      strategy,
            "lookup_table":  lookup_table,
            "key_column":    key_column,
            "value_columns": [],  # accumulated below; ordered, deduped
            "order_by":      order_by,
            "order_dir":     order_dir,
            "agg_fn":        agg_fn,
            "aggregate_expression": aggregate_expression,
            "filter_clause": filter_clause,
            "src_alias":     src_alias,
            "src_col":       m["source_col"],
            "asof_from":     asof_from,
            "asof_to":       asof_to,
            "asof_pivot":    asof_pivot,
        }

    # Always accumulate the value_column when present. Strategies with derived
    # tables (latest/aggregate) need every projected column in the inner SELECT.
    if value_column:
        existing = lookup_meta[join_key]["value_columns"]
        if value_column not in existing:
            existing.append(value_column)

    if strategy == "exists":
        expr = f"({lk_alias}.{key_column} IS NOT NULL)"
    elif strategy == "aggregate" and aggregate_expression:
        # The composite aggregate projects as the derived table's fixed
        # `agg_value` alias; any transform_expression is superseded (the
        # composite already IS the derived value).
        expr = f"{lk_alias}.agg_value"
    elif transform_expression:
        # The lookup feeds a derived expression (e.g. CASE bucketing over the
        # looked-up value: `CASE WHEN salary_history.amt < 60000 THEN 'A' ...`).
        # First resolve any MAIN-source (FROM-side) column refs to their table
        # aliases, then rewrite references to the looked-up value column to the
        # lookup join alias so `salary_history.amt` becomes `lk1.amt`.
        expr = _substitute_inputs(transform_expression, mappings_for_pc, alias_lookup)
        expr = _rewrite_lookup_value_refs(expr, orig_lookup_table, value_column, lk_alias)
    else:
        expr = f"{lk_alias}.{value_column}"

    return _apply_decorators(expr, m.get("transform_decorators_json"))


def _rewrite_lookup_value_refs(expr, lookup_table, value_column, lk_alias):
    """Rewrite references to a lookup table's value column inside a derived
    expression to the lookup join alias.

    `<lookup_table_bare>.<value_column>` and a bare `<value_column>` (not already
    alias/table-qualified) both become `<lk_alias>.<value_column>`. Used so a
    lookup transform that carries a CASE/derived `transformExpression` resolves
    its looked-up value to the actual join alias.
    """
    if not value_column:
        return expr
    out = expr
    bare_tbl = (lookup_table or "").rsplit(".", 1)[-1].strip()
    if bare_tbl:
        out = re.sub(
            rf"\b{re.escape(bare_tbl)}\.{re.escape(value_column)}\b",
            f"{lk_alias}.{value_column}",
            out,
        )
    # Bare value column not preceded by a word char or '.' (so already-qualified
    # refs like `lk1.amt` are left alone) and not itself followed by '.'.
    out = re.sub(
        rf"(?<![\w.]){re.escape(value_column)}\b(?!\s*\.)",
        f"{lk_alias}.{value_column}",
        out,
    )
    return out


def _render_lookup_join_from_meta(meta, dialect=None):
    """Render one accumulated lookup-meta entry to its LEFT JOIN SQL."""
    return _render_lookup_join(
        strategy=meta["strategy"],
        lookup_table=meta["lookup_table"],
        key_column=meta["key_column"],
        value_columns=meta["value_columns"],
        order_by=meta["order_by"],
        order_dir=meta["order_dir"],
        agg_fn=meta["agg_fn"],
        filter_clause=meta["filter_clause"],
        src_alias=meta["src_alias"],
        src_col=meta["src_col"],
        lk_alias=meta["lk_alias"],
        asof_from=meta.get("asof_from", ""),
        asof_to=meta.get("asof_to", ""),
        asof_pivot=meta.get("asof_pivot", ""),
        aggregate_expression=meta.get("aggregate_expression", ""),
        dialect=dialect,
    )


def _render_lookup_join(
    *, strategy, lookup_table, key_column, value_columns,
    order_by, order_dir, agg_fn, filter_clause,
    src_alias, src_col, lk_alias,
    asof_from="", asof_to="", asof_pivot="",
    aggregate_expression="",
    dialect=None,
):
    """Emit the LEFT JOIN fragment for the given lookup strategy.

    value_columns is the full accumulated set across product columns sharing
    this scan (see _compile_lookup's two-pass model). For derived-table
    strategies (latest, aggregate), every accumulated column must appear in
    the inner SELECT; for equi / exists, value_columns is informational only
    (the join is against the raw table, columns project off the alias directly).
    """
    _d = dialect or _DIALECTS["postgres"]
    where_inner = f"\n        WHERE {filter_clause}" if filter_clause else ""
    cols_csv = ", ".join(value_columns) if value_columns else "*"

    if strategy == "latest":
        # Universal ROW_NUMBER lowering — portable across PG / Snowflake /
        # Databricks / Azure SQL / BigQuery (engine-specific QUALIFY can ship
        # later as a renderer-subclass optimisation).
        return (
            f"    LEFT JOIN (\n"
            f"        SELECT {key_column}, {cols_csv} FROM (\n"
            f"            SELECT {key_column}, {cols_csv},\n"
            f"                   ROW_NUMBER() OVER (PARTITION BY {key_column}\n"
            f"                                      ORDER BY {order_by} {order_dir}{_d.nulls_last}) AS rn\n"
            f"            FROM {lookup_table}{where_inner}\n"
            f"        ) t WHERE rn = 1\n"
            f"    ) AS {lk_alias}\n"
            f"        ON {src_alias}.{src_col} = {lk_alias}.{key_column}"
        )

    if strategy == "aggregate":
        # Raw composite expression (aggregate_expression): the whole computed
        # value — possibly multiple aggregates + per-aggregate FILTER +
        # arithmetic/clamping — emitted verbatim under the fixed `agg_value`
        # alias. Part of the dedup key, so differing expressions get their own
        # scans and identical ones share.
        if aggregate_expression:
            return (
                f"    LEFT JOIN (\n"
                f"        SELECT {key_column}, {aggregate_expression} AS agg_value\n"
                f"        FROM {lookup_table}{where_inner}\n"
                f"        GROUP BY {key_column}\n"
                f"    ) AS {lk_alias}\n"
                f"        ON {src_alias}.{src_col} = {lk_alias}.{key_column}"
            )
        # COUNT_DISTINCT is a portable shorthand we expand to COUNT(DISTINCT x).
        # All product columns sharing this join run the same agg_fn (it's part
        # of the dedup key); they only differ in value_column.
        agg_parts = []
        for vc in value_columns or []:
            if agg_fn == "COUNT_DISTINCT":
                agg_parts.append(f"COUNT(DISTINCT {vc}) AS {vc}")
            else:
                agg_parts.append(f"{agg_fn}({vc}) AS {vc}")
        agg_csv = ", ".join(agg_parts) if agg_parts else f"COUNT(*) AS _count"
        return (
            f"    LEFT JOIN (\n"
            f"        SELECT {key_column}, {agg_csv}\n"
            f"        FROM {lookup_table}{where_inner}\n"
            f"        GROUP BY {key_column}\n"
            f"    ) AS {lk_alias}\n"
            f"        ON {src_alias}.{src_col} = {lk_alias}.{key_column}"
        )

    if strategy == "asof":
        # Point-in-time: join the effective-dated reference row whose validity
        # window contains the anchor's pivot date. Plain LEFT JOIN + interval
        # predicate — one reference row per anchor span, period-correct.
        asof = f"{lk_alias}.{asof_from} <= {src_alias}.{asof_pivot}"
        if asof_to:
            asof += (f" AND ({lk_alias}.{asof_to} IS NULL"
                     f" OR {lk_alias}.{asof_to} > {src_alias}.{asof_pivot})")
        extra = f" AND ({filter_clause})" if filter_clause else ""
        return (
            f"    LEFT JOIN {lookup_table} AS {lk_alias}\n"
            f"        ON {src_alias}.{src_col} = {lk_alias}.{key_column}\n"
            f"        AND {asof}{extra}"
        )

    # equi / exists share the same join shape; filter_clause becomes an AND.
    base_on = f"{src_alias}.{src_col} = {lk_alias}.{key_column}"
    extra_on = f" AND ({filter_clause})" if filter_clause else ""
    return (
        f"    LEFT JOIN {lookup_table} AS {lk_alias}\n"
        f"        ON {base_on}{extra_on}"
    )


def _quote_sql_string(s):
    """Single-quote a string literal for embedding in SQL."""
    return "'" + str(s).replace("'", "''") + "'"


def _compile_bucket(m, mappings_for_pc, alias_lookup, dialect):
    """
    Bucket / binning: continuous → ordered band labels.

    transformParams: {"boundaries": [25, 50, 100],
                      "labels":     ["low", "medium", "high", "very_high"]}
    N boundaries → N+1 labels. Emits a CASE WHEN chain.

    Composability (transform-portability.md): when ``transformParams.value`` is a
    nested neutral op (e.g. {"op":"date_difference","unit":"year",
    "semantics":"completed_units"}), the band is computed over that op lowered
    per-dialect instead of a raw source column — a self-contained, portable
    "band over a computed value" that needs no intermediate product column. The
    value expr references source columns via the join alias, so it resolves
    cleanly (unlike a bare product-column token in a derived-on-derived CASE).
    """
    params = _parse_json(m.get("transform_params_json"), {})
    boundaries = params.get("boundaries") or []
    labels = params.get("labels") or []

    value_spec = params.get("value")
    if isinstance(value_spec, dict):
        # Parenthesize: safe left operand of `< b` on every dialect (incl.
        # BigQuery's `DATE_DIFF(...) - IF(...)` completed_units form).
        col_ref = "(" + _compile_value_op(value_spec, m, mappings_for_pc, alias_lookup, dialect) + ")"
    else:
        tkey = f"{m['source_schema']}.{m['source_table']}"
        alias = alias_lookup.get(tkey, "?")
        col_ref = f"{alias}.{_source_col_ref(m)}"

    if not boundaries or len(labels) != len(boundaries) + 1:
        return col_ref  # malformed — degrade to the (computed or raw) value

    when_clauses = []
    for b, lab in zip(boundaries, labels[:-1]):
        when_clauses.append(f"WHEN {col_ref} < {b} THEN {_quote_sql_string(lab)}")
    else_clause = f"ELSE {_quote_sql_string(labels[-1])}"
    return "CASE " + " ".join(when_clauses) + " " + else_clause + " END"


def _compile_mask(m, alias_lookup, dialect=None):
    """
    Format-preserving redaction.

    transformParams:
      {"algorithm": "keep_last" | "keep_first" | "middle",
       "keep_n":    int (default 4),
       "mask_char": str (default 'X'),
       "keep_format": bool (default false)}

    keep_format keeps non-alphanumeric chars (e.g. dashes, dots) intact —
    useful for credit-card / SSN style strings. When false, all chars except
    the kept-region positions are replaced.

    Phase 7: format-preserving paths use REGEXP_REPLACE, whose 4th-argument
    flag syntax differs across PG / Snowflake / others. The dialect's
    `regexp_replace_global` emits the appropriate form.
    """
    if dialect is None:
        dialect = _DIALECTS["postgres"]
    params = _parse_json(m.get("transform_params_json"), {})
    algorithm = (params.get("algorithm") or "keep_last").strip().lower()
    keep_n = int(params.get("keep_n") or 4)
    mask_char = (params.get("mask_char") or "X")[:1] or "X"
    keep_format = bool(params.get("keep_format", False))

    tkey = f"{m['source_schema']}.{m['source_table']}"
    alias = alias_lookup.get(tkey, "?")
    col = f"{alias}.{_source_col_ref(m)}"
    mc = _quote_sql_string(mask_char)

    # Phase 7: emit dialect-portable substring (SUBSTR) + concat (dialect.str_concat)
    # instead of hardcoded ANSI `SUBSTRING(x FROM s FOR l)` + `||` — BigQuery rejects
    # the former and MySQL treats `||` as logical OR.
    L = f"LENGTH({col})"
    head = f"GREATEST({L} - {keep_n} + 1, 1)"        # start of the trailing kept region
    tail_len = f"GREATEST({L} - {keep_n}, 0)"        # length of the leading region
    mid_len = f"GREATEST({L} - 2 * {keep_n}, 0)"     # length of the middle region

    def _sub(start, length=None):
        return dialect.substring(col, start, length)

    if keep_format:
        # Replace alphanumerics outside the kept region with mask_char,
        # preserving every other character (delimiters, dots, spaces).
        if algorithm == "keep_last":
            rr = dialect.regexp_replace_global(_sub(1, tail_len), "'[A-Za-z0-9]'", mc)
            return dialect.str_concat([rr, _sub(head)], sep="")
        if algorithm == "keep_first":
            rr = dialect.regexp_replace_global(_sub(keep_n + 1), "'[A-Za-z0-9]'", mc)
            return dialect.str_concat([_sub(1, keep_n), rr], sep="")
        # middle
        rr = dialect.regexp_replace_global(_sub(keep_n + 1, mid_len), "'[A-Za-z0-9]'", mc)
        return dialect.str_concat([_sub(1, keep_n), rr, _sub(head)], sep="")

    # Non-format-preserving: pad with mask_char.
    if algorithm == "keep_last":
        return dialect.str_concat([f"REPEAT({mc}, {tail_len})", _sub(head)], sep="")
    if algorithm == "keep_first":
        return dialect.str_concat([_sub(1, keep_n), f"REPEAT({mc}, {tail_len})"], sep="")
    # middle
    return dialect.str_concat([_sub(1, keep_n), f"REPEAT({mc}, {mid_len})", _sub(head)], sep="")


def _compile_hash(m, alias_lookup, dialect=None):
    """
    Irreversible digest.

    transformParams:
      {"algorithm": "md5" | "sha1" | "sha256" (default md5),
       "salt":      "optional namespace prefix"}

    Phase 7: hash emission goes through the dialect. md5 is universal across
    PG / Snowflake / Databricks / BigQuery (BigQuery wraps in TO_HEX(MD5(...))
    via BigQueryDialect.hash_md5). SHA-1 / SHA-2 paths diverge:
      - Postgres:   encode(digest(text, '<algo>'), 'hex')  -- requires pgcrypto
      - Snowflake:  SHA1(text) / SHA2(text, 256)
      - Databricks: sha1(text) / sha2(text, 256)
      - BigQuery:   TO_HEX(SHA1(text)) / TO_HEX(SHA256(text))

    NOT a security primitive — salt here is a determinism / namespace tool,
    not a defense against rainbow-table attacks.
    """
    if dialect is None:
        dialect = _DIALECTS["postgres"]
    params = _parse_json(m.get("transform_params_json"), {})
    algorithm = (params.get("algorithm") or "md5").strip().lower()
    salt = params.get("salt") or ""

    tkey = f"{m['source_schema']}.{m['source_table']}"
    alias = alias_lookup.get(tkey, "?")
    col = f"{alias}.{_source_col_ref(m)}"

    # Coerce source to text for hashing (md5/digest/sha2 expect text or bytea).
    # The cast itself goes through the dialect so PG keeps `::` and others
    # get the standard CAST form.
    col_text = dialect.cast(col, dialect.string_type)
    # Phase 7: dialect-portable salt concat (MySQL treats `||` as logical OR).
    salted = f"({dialect.str_concat([_quote_sql_string(salt), col_text], sep='')})" if salt else col_text

    if algorithm == "md5":
        return dialect.hash_md5(salted)
    if algorithm in ("sha1", "sha256"):
        return dialect.hash_sha(salted, algorithm)
    # Unknown algorithm — degrade to md5 (already widely available).
    return dialect.hash_md5(salted)


_WINDOW_FUNCTIONS = {
    # Position / offset
    "LAG", "LEAD",
    # Ranking
    "ROW_NUMBER", "RANK", "DENSE_RANK", "NTILE", "PERCENT_RANK", "CUME_DIST",
    # Distribution / running aggregates (when the function appears with OVER)
    "SUM", "AVG", "MIN", "MAX", "COUNT", "COUNT_DISTINCT",
    "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE",
}


def _render_over_clause(spec):
    """Render a `(PARTITION BY ... ORDER BY ... <frame>)` clause from a
    normalised window spec. Empty partition / order pieces are dropped
    (a window with neither is degenerate but valid SQL — `OVER ()`). Frame
    is appended verbatim if non-empty; we don't try to parse it because
    different dialects accept different frame syntaxes."""
    parts = []
    if spec.get("partition_by"):
        parts.append("PARTITION BY " + ", ".join(spec["partition_by"]))
    if spec.get("order_by"):
        ordered = []
        for ob in spec["order_by"]:
            direction = (ob.get("direction") or "asc").upper()
            ordered.append(f"{ob['column']} {direction}")
        parts.append("ORDER BY " + ", ".join(ordered))
    if spec.get("frame"):
        parts.append(spec["frame"])
    return "OVER (" + " ".join(parts) + ")"


def _compile_window(m, alias_lookup, window_specs, undefined_refs):
    """Compile a `kind='window'` mapping to a SQL window-function expression.

    transformParams:
      function: window function name (LAG / LEAD / ROW_NUMBER / RANK /
                NTILE / SUM / AVG / FIRST_VALUE / etc.)
      window:   name of a :DatasetTransform.window_specs entry to reference
      offset:   for LAG / LEAD — the row offset (default 1)
      default:  for LAG / LEAD — fallback value when no row exists at offset
      ntile:    for NTILE — number of buckets

    transformInputs[0] = the source column the function operates on (for
    LAG/LEAD/aggregates — leave empty/null for ROW_NUMBER/RANK/etc. which
    don't take an argument).

    If the named window doesn't exist in window_specs, the reference is
    recorded in `undefined_refs` and the function falls through to a
    degenerate `OVER ()` so the SQL still parses (with a warning surfaced
    in summary).
    """
    params = _parse_json(m.get("transform_params_json"), {})
    fn = (params.get("function") or "").upper().strip()
    if not fn:
        fn = "ROW_NUMBER"
    win_name = (params.get("window") or "").strip()

    spec = window_specs.get(win_name) if win_name else None
    if win_name and spec is None:
        undefined_refs.add(win_name)
        spec = {"partition_by": [], "order_by": [], "frame": ""}

    # Compile the function argument from transformInputs[0] (if present) —
    # ROW_NUMBER / RANK / DENSE_RANK / etc. take no arguments; LAG / LEAD /
    # aggregates take one. We trust the engineer to know which is which;
    # bad SQL surfaces at materialization time.
    arg_expr = ""
    src_col = _source_col_ref(m)
    if src_col:
        tkey = f"{m['source_schema']}.{m['source_table']}"
        alias = alias_lookup.get(tkey, "?")
        arg_expr = f"{alias}.{src_col}"

    # COUNT_DISTINCT lowers to COUNT(DISTINCT ...) since SQL has no native
    # COUNT_DISTINCT keyword. Helps the apply-protocol stay symmetric with
    # lookup.aggregate_function's COUNT_DISTINCT.
    if fn == "COUNT_DISTINCT":
        call = f"COUNT(DISTINCT {arg_expr or '*'})"
    elif fn in ("LAG", "LEAD"):
        offset = params.get("offset")
        if not isinstance(offset, (int, float)) or offset < 1:
            offset = 1
        default = params.get("default")
        if default is None:
            call = f"{fn}({arg_expr}, {int(offset)})"
        else:
            # Quote string defaults; leave numeric / boolean as-is.
            default_sql = _quote_sql_string(str(default)) if isinstance(default, str) else str(default)
            call = f"{fn}({arg_expr}, {int(offset)}, {default_sql})"
    elif fn == "NTILE":
        ntile = params.get("ntile") or 4
        try:
            ntile = int(ntile)
        except (TypeError, ValueError):
            ntile = 4
        call = f"NTILE({ntile})"
    elif fn in ("ROW_NUMBER", "RANK", "DENSE_RANK", "PERCENT_RANK", "CUME_DIST"):
        call = f"{fn}()"
    else:
        # Aggregate / FIRST_VALUE / LAST_VALUE / unrecognised — pass the
        # argument through. Unknown function names land as-is; the engineer
        # sees them in the failing SQL if invalid.
        call = f"{fn}({arg_expr or '*'})"

    return f"{call} {_render_over_clause(spec)}"


def _single_col_ref(m, alias_lookup):
    """Table-aliased reference to a mapping's single source column."""
    tkey = f"{m['source_schema']}.{m['source_table']}"
    alias = alias_lookup.get(tkey, "?")
    return f"{alias}.{_source_col_ref(m)}"


def _ordered_mappings_by_inputs(mappings_for_pc):
    """Return ``mappings_for_pc`` ordered by the mapping's ``transformInputs``
    URI list — the authoritative parts order for multi-source kinds.

    The Neo4j query that populates ``mappings_for_pc`` gives NO intra-product-
    column ordering guarantee, so a positional kind (``concat`` emits
    left-to-right; ``date_difference`` needs ``[start, end]``) must not iterate
    the rows raw — that produced "Rodriguez Noah" from a ``full_name`` mapping
    whose ``transformInputs`` correctly said ``[first_name, last_name]``. This
    mirrors the URI-driven reordering ``_substitute_inputs`` already does for the
    verbatim-expression path (``_source_input_uri`` + ``transform_inputs_json``).

    Falls back to the original order when ``transform_inputs_json`` is absent or a
    row's URI is missing from it (older / hand-edited mappings stay stable); any
    rows not named in the URI list are appended in their original order."""
    if not mappings_for_pc:
        return mappings_for_pc
    parsed = _parse_json(mappings_for_pc[0].get("transform_inputs_json"), [])
    if not parsed:
        return mappings_for_pc
    uri_to_row = {_source_input_uri(m): m for m in mappings_for_pc}
    ordered = [uri_to_row[u] for u in parsed if u in uri_to_row]
    seen = {id(r) for r in ordered}
    ordered += [m for m in mappings_for_pc if id(m) not in seen]
    return ordered


def _compile_date_difference(m, mappings_for_pc, alias_lookup, dialect, params):
    """Compile the neutral ``date_difference`` op → dialect-specific SQL.

    transformParams: ``unit`` (default 'year'), ``semantics`` ∈
    {completed_units | boundary_count | symbolic_interval}. The two source
    columns are [start, end] in transformInputs order; a single source column
    pairs with ``CURRENT_DATE`` (the age-from-dob case). Raises
    ViewGenerationError when the (dialect, semantics) pair is unsupported —
    fail-closed, no silent wrong-semantics (the preflight surfaces the same
    finding at author time)."""
    unit = (params.get("unit") or "year").strip().lower()
    semantics = (params.get("semantics") or "completed_units").strip().lower()
    refs = [_single_col_ref(pm, alias_lookup)
            for pm in _ordered_mappings_by_inputs(mappings_for_pc)
            if pm.get("source_col")]
    start = refs[0] if len(refs) >= 1 else (params.get("start_column") or "NULL")
    end = refs[1] if len(refs) >= 2 else (params.get("end_column") or "CURRENT_DATE")
    return dialect.date_difference(start, end, unit=unit, semantics=semantics)


def _compile_value_op(value_spec, m, mappings_for_pc, alias_lookup, dialect):
    """Lower a nested neutral *value op* to a scalar SQL expression, so a
    structured kind (e.g. `bucket`) can band/operate over a COMPUTED value
    instead of a raw source column — the platform-independent "transform of a
    transform" composition point (transform-portability.md).

    v1 supports ``op='date_difference'`` (reuses _compile_date_difference; reads
    unit/semantics from value_spec, resolves start/end from mappings_for_pc).
    Extensible to cast/arithmetic later. Fail-closed on an unknown op."""
    op = (value_spec.get("op") or "").strip().lower()
    if op == "date_difference":
        return _compile_date_difference(m, mappings_for_pc, alias_lookup, dialect, value_spec)
    raise ViewGenerationError(
        f"bucket value op {op!r} is not supported (v1: date_difference only)."
    )


def _compile_structured_kind(kind, m, mappings_for_pc, alias_lookup, dialect):
    """Phase 7 dialect-portable emission for structured kinds. Returns the SQL
    expression, or None when the kind's params are insufficient (→ caller falls
    back to the verbatim transformExpression path). ``date_difference`` always
    compiles structurally and may raise ViewGenerationError (fail-closed)."""
    params = _parse_json(m.get("transform_params_json"), {})

    if kind == "date_difference":
        return _compile_date_difference(m, mappings_for_pc, alias_lookup, dialect, params)

    try:
        if kind == "cast":
            target = (params.get("target_type") or params.get("target_canonical_type") or "").strip()
            if not target:
                return None
            return dialect.cast(_single_col_ref(m, alias_lookup), target)

        if kind == "substring":
            start = params.get("start", params.get("from"))
            length = params.get("length", params.get("for"))
            if start is None:
                return None
            return dialect.substring(
                _single_col_ref(m, alias_lookup),
                int(start), int(length) if length is not None else None,
            )

        if kind == "split":
            delim = params.get("delimiter", params.get("separator"))
            index = params.get("index", params.get("part"))
            if delim is None or index is None:
                return None
            return dialect.split_part(
                _single_col_ref(m, alias_lookup), _quote_sql_string(delim), int(index)
            )

        if kind == "concat":
            sep = params.get("separator", "")
            # Emit parts in transformInputs order (left-to-right) — the mapping
            # rows carry no ordering guarantee on their own.
            parts = [_single_col_ref(pm, alias_lookup)
                     for pm in _ordered_mappings_by_inputs(mappings_for_pc)
                     if pm.get("source_col")]
            if not parts:
                return None
            return dialect.str_concat(parts, sep=sep)

        if kind == "format":
            # The case-fold op lives under transformParams.case (upper/lower/trim)
            # — the convention the catalogs + TransformEditor use (e.g.
            # common.yaml:email_normalize `case: lower`). `operation` kept as a
            # back-compat alias. transformParams.format (a date/number token) is a
            # documented follow-up (needs Dialect.date_format) — leave it None.
            case = (params.get("case") or params.get("operation") or "").strip().lower()
            col = _single_col_ref(m, alias_lookup)
            if case in ("upper", "uppercase"):
                return f"UPPER({col})"
            if case in ("lower", "lowercase"):
                return f"LOWER({col})"
            if case == "trim":
                return f"TRIM({col})"
            return None  # date-format token translation (params.format) is a follow-up
    except (ValueError, TypeError):
        return None  # malformed structured params → fall back to the expression

    return None


def _compile_select_expr(pc_name, mappings_for_pc, alias_lookup, lookup_aliases, lookup_meta, window_specs=None, window_undefined_refs=None, dialect=None, view_schema="public"):
    """
    Build the SELECT-clause expression for one product column.
    Returns the expression string (without 'AS <colname>').

    lookup_meta is the accumulating join-metadata dict from generate_ddl's
    two-pass compile model (see _compile_lookup).
    """
    if dialect is None:
        dialect = _DIALECTS["postgres"]
    m = mappings_for_pc[0]
    kind = (m.get("transform_kind") or "").lower() or None
    expression = m.get("transform_expression") or ""
    decorators_json = m.get("transform_decorators_json")

    # Literal: emit the SQL literal verbatim plus an optional cast. No source
    # column reference, no JOINs.
    if kind == "literal":
        params = _parse_json(m.get("transform_params_json"), {})
        value = (params.get("literal_value") or "").strip()
        target = (params.get("target_type") or "").strip()
        if not value:
            value = "NULL"
        return dialect.cast(value, target) if target else value

    # Lookup is special: it injects a JOIN side-effect
    if kind == "lookup":
        return _compile_lookup(pc_name, mappings_for_pc, alias_lookup, lookup_aliases, lookup_meta, view_schema=view_schema)

    # Phase 1 column-level kinds — single-source, no JOIN, decorators apply.
    if kind == "bucket":
        return _apply_decorators(
            _compile_bucket(m, mappings_for_pc, alias_lookup, dialect),
            decorators_json, dialect=dialect)
    if kind == "mask":
        return _apply_decorators(_compile_mask(m, alias_lookup, dialect=dialect), decorators_json)
    if kind == "hash":
        return _apply_decorators(_compile_hash(m, alias_lookup, dialect=dialect), decorators_json)

    # Phase 6 — window function. References a named entry in
    # :DatasetTransform.window_specs. Defensive: missing/empty window_specs
    # (caller passed None) → undefined-ref tracking is skipped but the call
    # still renders against an empty spec (`OVER ()`).
    if kind == "window":
        specs = window_specs or {}
        undefined = window_undefined_refs if window_undefined_refs is not None else set()
        return _apply_decorators(
            _compile_window(m, alias_lookup, specs, undefined),
            decorators_json,
        )

    # Phase 7 structured kinds — dialect-portable emission from transformParams
    # (routes cast/substring/split/concat/format through the Dialect, and the new
    # date_difference op). Fires when the params suffice; otherwise falls through
    # to the verbatim transformExpression path below for back-compat.
    if kind in ("cast", "substring", "split", "concat", "format", "date_difference"):
        structured = _compile_structured_kind(kind, m, mappings_for_pc, alias_lookup, dialect)
        if structured is not None:
            return _apply_decorators(structured, decorators_json, dialect=dialect)

    # If there's an explicit transformExpression, substitute inputs into it, then
    # render it to the target dialect (a raw expression is authored in ONE grammar
    # — usually the steward catalog's Postgres — but the product may be served on
    # Databricks/Snowflake/BigQuery; see _translate_raw_expression).
    if expression:
        expr = _substitute_inputs(expression, mappings_for_pc, alias_lookup)
        expr = _translate_raw_expression(expr, m, dialect)
        return _apply_decorators(expr, decorators_json, dialect=dialect)

    # No explicit expression — fall back by kind
    if kind == "direct" or len(mappings_for_pc) == 1:
        tkey = f"{m['source_schema']}.{m['source_table']}"
        alias = alias_lookup.get(tkey, "?")
        expr = f"{alias}.{_source_col_ref(m)}"
        return _apply_decorators(expr, decorators_json, dialect=dialect)

    # Multi-source mapping with no expression: fallback concat with single space
    parts = []
    for pm in mappings_for_pc:
        tkey = f"{pm['source_schema']}.{pm['source_table']}"
        alias = alias_lookup.get(tkey, "?")
        parts.append(f"{alias}.{_source_col_ref(pm)}")
    expr = dialect.str_concat(parts, sep=" ")
    return _apply_decorators(expr, decorators_json, dialect=dialect)


class ViewGenerationError(RuntimeError):
    """Raised when a view DDL cannot be safely generated.

    The serving stage prefers a hard failure over emitting a CROSS JOIN that
    would silently produce a Cartesian-product view — wrong data is worse
    than no data.
    """


# ── Build-time JOIN-predicate column validation (defense-in-depth) ───────────
# The PRIMARY fix for FK column-name drift lives at the catalog→product boundary
# (odcs.py DPROD_PROPAGATE_FK translates catalog names to the product's approved
# names). This is the renderer-side safety net: every FK join column must exist
# on the joined dataset, checked by STABLE dataset URI — not the rendered
# schema.table, which diverges from the column metadata for materialized
# cross-platform sources. Proven absence is a hard failure at BUILD instead of a
# runtime "column does not exist" at deploy.
_COL_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _assert_join_column_exists(uri, col, columns_by_uri, *, alias, diagnostics):
    """No-op unless we can PROVE the column is absent. A non-bare-identifier
    (function / expression / literal / quoted ident), an unknown dataset URI, or
    an empty column list yields a `validation_unavailable` diagnostic (visible,
    never a silent skip) rather than a false-positive failure."""
    if not _COL_IDENT_RE.match(col or ""):
        return
    real = columns_by_uri.get(uri) if uri else None
    if not real:
        if diagnostics is not None:
            diagnostics.append({
                "kind": "validation_unavailable", "alias": alias, "column": col,
                "uri": uri or "", "reason": "no column metadata for the joined dataset",
            })
        return
    if col.lower() in {c.lower() for c in real}:
        return
    raise ViewGenerationError(
        f"JOIN predicate references column `{alias}.{col}`, but dataset `{uri}` has no "
        f'such column — the view would fail at deploy with "column does not exist". '
        f"Columns that DO exist: {sorted(real)}. Fix by re-running odcs_to_dprod on the "
        f"SOURCE product (its approved column names are translated into the FK edge), or "
        f"declare an explicit join via :DatasetTransform.joins[]."
    )


def _assert_fk_join_columns(fk, fk_cols, pk_cols, columns_by_uri, *,
                            from_alias, to_alias, diagnostics):
    """Validate one matched FK edge's ON columns: composite sides must be
    non-empty and equal-length; each `fk_cols` must exist on the FROM dataset
    and each `pk_cols` on the TO dataset (both keyed by stable URI)."""
    if not fk_cols or not any(fk_cols) or len(fk_cols) != len(pk_cols):
        raise ViewGenerationError(
            f"FK edge {fk.get('from_uri')} → {fk.get('to_uri')} has empty or mismatched "
            f"join columns (columns={fk.get('fk_columns')!r}, "
            f"referencedColumns={fk.get('pk_columns')!r}); cannot build a valid ON clause."
        )
    for fc in fk_cols:
        _assert_join_column_exists(fk.get("from_uri"), fc, columns_by_uri,
                                   alias=from_alias, diagnostics=diagnostics)
    for pc in pk_cols:
        _assert_join_column_exists(fk.get("to_uri"), pc, columns_by_uri,
                                   alias=to_alias, diagnostics=diagnostics)


# Parse-only safety gate (kept self-contained — this script runs as a
# standalone subprocess and can't import the backend's sql_executor). Mirrors
# sql_executor.looks_like_prose: a real predicate always has at least one
# comparison / membership / null-test operator. Operator-less text like
# "employee status is active" is un-interpreted PO prose that bypassed the
# authoring/engineer gates and must NOT be emitted into a WHERE clause.
_PRED_OPERATOR_RE = re.compile(
    r"(?:[=<>]|!=|<>|\bIN\b|\bLIKE\b|\bILIKE\b|\bBETWEEN\b|"
    r"\bIS\s+(?:NOT\s+)?(?:NULL|TRUE|FALSE)\b|\bSIMILAR\s+TO\b|\b@@\b)",
    re.IGNORECASE,
)


def _filter_looks_like_prose(predicate: str) -> bool:
    frag = (predicate or "").strip().rstrip(";").strip()
    if not frag:
        return False
    return _PRED_OPERATOR_RE.search(frag) is None


def _resolve_explicit_join_aliases(session, joins, view_schema):
    """Resolve each joins[] entry's dataset_uri to a (schema.table → alias)
    override. Used so that when joins[] is set, the aliasing in the rendered
    SQL uses the engineer's declared alias instead of the auto-assigned
    t1/t2/... auto-aliases.

    Returns a dict keyed by 'schema.table' (matching the alias_lookup key
    format used by _compile_select_expr). DProdOutputDataset sources resolve
    to (view_schema, vw_<safe_name(name)>) — mirroring the resolution
    _resolve_source_relation does for dprod-mode mappings.
    """
    uris = [j.get("dataset_uri") for j in joins if j.get("dataset_uri")]
    if not uris:
        return {}, {}
    resolved = {}
    for r in session.run(EXPLICIT_JOIN_RESOLVER_QUERY, uris=uris):
        uri = r["uri"]
        if r["kind"] == "dprod":
            # Served-location-first (materialized source) → real catalog.schema.table;
            # else the co-located vw_<name> in the consumer's own view_schema.
            schema, table = _dprod_relation(r["name"] or "data_product", view_schema)
        else:
            schema = r["schema"]
            table = r["name"]
        resolved[uri] = (schema, table)

    # Map dataset_uri → engineer-declared alias, then re-key by schema.table.
    # `alias_relations` additionally carries alias → (schema, table) for EVERY
    # resolvable join entry — including entries no mapping reads from (pure
    # bridges / filter-only joins), which `tables[]` can't resolve.
    overrides = {}
    alias_relations = {}
    for j in joins:
        uri = j.get("dataset_uri")
        alias = j.get("alias")
        if uri and alias and uri in resolved:
            schema, table = resolved[uri]
            overrides[f"{schema}.{table}"] = alias
            alias_relations[alias] = (schema, table)
    return overrides, alias_relations


def _build_from_explicit(*, joins, tables, view_schema, output_dataset_uri,
                         columns_by_key=None, alias_relations=None, dialect=None,
                         join_diagnostics=None):
    """Phase 4: build FROM + JOIN clauses from :DatasetTransform.joins.

    The first entry in joins[] becomes the FROM table; subsequent entries
    become JOINs with their engineer-declared kind + predicate. Tables that
    mappings reference but joins[] does not cover trigger a ViewGenerationError
    (the engineer has under-declared the join graph).

    Engineer-declared joins are emitted as authored, with ONE safety overlay
    that mirrors the FK-inferred path: a LEFT/INNER-joined table carrying a
    genuine SCD effective-dating shape (`_satellite_needs_dedup`) is wrapped in
    a current-row ROW_NUMBER() = 1 derived table so it can't fan the view out
    to one row per historical version. This is what lets a consumer anchor on
    one source product's table (e.g. salary_history) and bridge to another
    product's history table (job_assignment_history) via an explicit join
    without re-introducing per-key multiplication. The FROM anchor (joins[0])
    is never wrapped — it defines the grain. To keep raw history, declare the
    table at the anchor or remove its SCD markers.
    """
    columns_by_key = columns_by_key or {}
    alias_relations = alias_relations or {}
    auto_deduped = []
    if not joins:
        raise ViewGenerationError(
            f"Output dataset {output_dataset_uri}: _build_from_explicit called "
            "with empty joins[]; this is a logic bug."
        )

    # Columns each alias is referenced with across ALL predicates — the
    # DISTINCT projection of a pure-bridge entry must cover its own ON keys
    # plus every key a LATER join reads off it.
    refs_by_alias = {}
    for j in joins:
        for a, c in re.findall(r"(\w+)\.(\w+)", j.get("predicate") or ""):
            refs_by_alias.setdefault(a, set()).add(c)

    # Build the per-table reverse index so we can confirm coverage.
    declared_tables = set()
    first = joins[0]
    declared_tables.add(first["alias"])

    # We need the resolved schema.table for the FROM entry. The caller has
    # already overridden tables[] with the explicit alias, so we walk it.
    alias_to_st = {info["alias"]: (info["schema"], info["table"]) for info in tables.values()}
    alias_to_key = {info["alias"]: f"{info['schema']}.{info['table']}" for info in tables.values()}

    # Defense-in-depth (warn-only — the predicate is engineer-authored and may
    # legitimately reference computed/aliased expressions): flag any bare-column
    # reference that doesn't exist on the joined table's known columns.
    if join_diagnostics is not None and columns_by_key:
        for a, cols in refs_by_alias.items():
            key = alias_to_key.get(a)
            real = (columns_by_key.get(key) or {}).get("column_names") or [] if key else []
            if not real:
                continue
            low = {c.lower() for c in real}
            for c in sorted(cols):
                if _COL_IDENT_RE.match(c) and c.lower() not in low:
                    join_diagnostics.append({
                        "kind": "join_predicate_warning", "alias": a, "column": c,
                        "table": key, "existing_columns": sorted(real),
                        "reason": "explicit join predicate references a column not found on the table",
                    })

    if first["alias"] not in alias_to_st:
        raise ViewGenerationError(
            f"Output dataset {output_dataset_uri}: joins[0].alias='{first['alias']}' "
            f"references no source table that any mapping reads from. Either remove "
            f"the join entry or add a mapping that pulls from this table."
        )
    s, t = alias_to_st[first["alias"]]
    from_clause = f"    {s}.{t} AS {first['alias']}"

    join_clauses = []
    for j in joins[1:]:
        alias = j["alias"]
        kind = (j.get("kind") or "left").upper()
        predicate = j.get("predicate") or ""
        unmapped = alias not in alias_to_st
        if unmapped:
            # A join entry no mapping reads from (a pure bridge / filter-only
            # join). Resolve its real relation from the dataset_uri resolver;
            # only when the URI itself is unresolvable fall back to the `?`
            # placeholder so the SQL still parses and the error surfaces
            # downstream during execution.
            s, t = alias_relations.get(alias, ("?", "?"))
        else:
            s, t = alias_to_st[alias]
        # Pure bridge (junction): contributes no SELECT columns, so emit it as
        # a DISTINCT projection over exactly the key columns the predicates
        # reference — a ledger-grain junction (N rows per entity) then can't
        # fan the view out. Applied to entries flagged bridge_only, and
        # defensively to any resolvable unmapped entry with an ON predicate.
        is_pure_bridge = (
            kind not in ("CROSS",) and predicate and s != "?"
            and (bool(j.get("bridge_only")) or unmapped)
        )
        if is_pure_bridge:
            proj = sorted(refs_by_alias.get(alias) or [], key=str.lower)
            if proj:
                join_kw = "INNER JOIN" if kind in ("INNER", "SEMI") else "LEFT JOIN"
                join_clauses.append(
                    f"    {join_kw} (\n"
                    f"        SELECT DISTINCT {', '.join(proj)}\n"
                    f"        FROM {s}.{t}\n"
                    f"    ) AS {alias}\n"
                    f"        ON {predicate}"
                )
                auto_deduped.append({
                    "table": f"{s}.{t}", "mode": "junction_distinct",
                    "keys": proj, "bridge_only": True,
                })
                declared_tables.add(alias)
                continue
        # Map kind to a SQL JOIN syntax. CROSS JOIN has no ON; others do.
        if kind == "CROSS":
            join_clauses.append(f"    CROSS JOIN {s}.{t} AS {alias}")
        else:
            join_kw = {
                "INNER":  "INNER JOIN",
                "LEFT":   "LEFT JOIN",
                "RIGHT":  "RIGHT JOIN",
                "FULL":   "FULL OUTER JOIN",
                "ANTI":   "LEFT JOIN",   # ANTI / SEMI synthesised via WHERE — for substrate, treat as LEFT
                "SEMI":   "INNER JOIN",
            }.get(kind, "LEFT JOIN")
            on_clause = f" ON {predicate}" if predicate else ""
            # SCD current-row wrap (see docstring). Only LEFT/INNER joins with a
            # predicate we can read the satellite FK column out of; never the
            # anchor.
            join_key = alias_to_key.get(alias)
            meta = columns_by_key.get(join_key) if join_key else None
            wrapped = False
            if (predicate and join_kw in ("LEFT JOIN", "INNER JOIN")
                    and meta and _satellite_needs_dedup(join_key, meta)):
                _d_expl = dialect or _DIALECTS["postgres"]
                cols = meta.get("column_names") or []
                temporal_col = _pick_temporal_order_column(cols)
                part_col = _extract_alias_column(predicate, alias) if temporal_col else None
                if temporal_col and part_col:
                    is_cur = _pick_is_current_column(cols)
                    where_cur = (
                        f"            WHERE {is_cur} = true\n" if is_cur else ""
                    )
                    join_clauses.append(
                        f"    {join_kw} (\n"
                        f"        SELECT * FROM (\n"
                        f"            SELECT *, ROW_NUMBER() OVER (\n"
                        f"                PARTITION BY {part_col}\n"
                        f"                ORDER BY {temporal_col} DESC{_d_expl.nulls_last}\n"
                        f"            ) AS _rn\n"
                        f"            FROM {s}.{t}\n"
                        f"{where_cur}"
                        f"        ) t WHERE _rn = 1\n"
                        f"    ) AS {alias}{on_clause}"
                    )
                    auto_deduped.append({
                        "table":            join_key,
                        "temporal_column":  temporal_col,
                        "partition_column": part_col,
                        "is_current_filter": is_cur,
                    })
                    wrapped = True
            if not wrapped:
                join_clauses.append(f"    {join_kw} {s}.{t} AS {alias}{on_clause}")
        declared_tables.add(alias)

    # Coverage check: every source table the mappings touch must be reachable
    # via an explicit alias.
    for info in tables.values():
        if info["alias"] not in declared_tables:
            raise ViewGenerationError(
                f"Output dataset {output_dataset_uri}: source table "
                f"{info['schema']}.{info['table']} (alias '{info['alias']}') is "
                f"referenced by mappings but missing from :DatasetTransform.joins[]. "
                f"Add it to joins[] or remove the unreachable mappings."
            )

    return from_clause, join_clauses, auto_deduped


class _AmbiguousBridges:
    """Sentinel returned by `_resolve_bridges` when multiple shortest FK paths
    between two required tables go through different junction-table sets
    AND the name-similarity ranker (`_rank_bridge_paths`) couldn't break the
    tie (multiple candidates at the top score). The caller renders a
    ViewGenerationError naming every candidate so the engineer can
    disambiguate via explicit :DatasetTransform.joins[] or by adding a
    mapping that touches one of the candidates."""
    def __init__(self, src_key, tgt_key, candidate_paths, top_score=0):
        self.src_key = src_key
        self.tgt_key = tgt_key
        self.candidate_paths = candidate_paths  # list[list[str]] of node keys
        self.top_score = top_score              # for richer error messaging


def _bridge_name_token(key):
    """Extract the discriminating segment from a `{schema}.{table}` key for
    name-similarity scoring.

      catalog `(schema, table)`           → `table` (raw catalog name)
      dprod  `(view_schema, vw_<name>)`   → `name` (the bit after `vw_`)

    The token is used to look for endpoint-name substring hits in candidate
    bridges' intermediate names. Returned lowercased.
    """
    if "." not in key:
        return key.lower()
    _, _, tail = key.partition(".")
    if tail.startswith("vw_"):
        tail = tail[3:]
    return tail.lower()


# `relationship_kind` classifications shipped by the metadata-enrichment
# skill via :TableDescription.relationshipKind (propagated onto
# :DProdOutputDataset.relationshipKind by _generate_dprod). The bridge
# ranker boosts/demotes candidates by these values when name-similarity
# alone produces a tie. Numeric weights mean higher = better bridge.
_RELATIONSHIP_KIND_WEIGHTS = {
    "general_membership": 3,   # the typical "every X with every Y" junction
    "lookup_dimension":   1,   # reference table; can bridge but rare
    "fact":               1,   # primary entity — usable but unusual as a bridge
    "audit_log":          0,
    "configuration":      0,
    "specialization":     -1,  # role-specific subset; should NOT be picked as the general bridge
    "unknown":            0,
}


def _rank_bridge_paths(paths, src_key, tgt_key, meta_by_key=None):
    """Rank candidate bridge paths by endpoint-name containment, with a
    secondary signal from :TableDescription.relationshipKind when available.

    Primary score: number of endpoint name tokens (from src_key, tgt_key)
    that appear as substrings in the concatenated intermediate node names.
    Maximum 2 (both endpoints' names appear in the bridge name — strong
    signal, e.g. `department_employee` for `employee↔department`).

    Secondary score (when meta_by_key is provided): the relationship_kind
    of each intermediate, summed via `_RELATIONSHIP_KIND_WEIGHTS`. Picks
    `general_membership` over `specialization` even when the names tie at
    the primary score (e.g. when both junctions contain both endpoint
    names, the kind classification — set at metadata-enrichment time, PO-
    approved — is the deciding factor).

    Total score = primary * 10 + secondary, so primary always dominates;
    secondary breaks ties at the same primary score.

    Returns list of (score, rationale, path) sorted by score descending.
    Empty input → empty list.
    """
    if not paths:
        return []
    src_name = _bridge_name_token(src_key)
    tgt_name = _bridge_name_token(tgt_key)
    meta_by_key = meta_by_key or {}
    ranked = []
    for path in paths:
        intermediates_text = " ".join(
            _bridge_name_token(n) for n in path[1:-1]
        )
        s_hit = bool(src_name) and src_name in intermediates_text
        t_hit = bool(tgt_name) and tgt_name in intermediates_text
        primary = int(s_hit) + int(t_hit)
        # Secondary signal: sum of relationship_kind weights across the
        # intermediate nodes. The PO-approved classification carries
        # authoritative semantic info that name patterns can't see.
        secondary_kinds = []
        secondary = 0
        for hop in path[1:-1]:
            meta = meta_by_key.get(hop) or {}
            kind = (meta.get("relationship_kind") or "").lower()
            if kind:
                secondary_kinds.append(kind)
                secondary += _RELATIONSHIP_KIND_WEIGHTS.get(kind, 0)
        score = primary * 10 + secondary
        # Rationale text combines both signals so the engineer auditing the
        # SQL knows which factors drove the pick.
        rationale_parts = []
        if primary == 2:
            rationale_parts.append(
                f"bridge contains both endpoint names "
                f"('{src_name}' and '{tgt_name}')"
            )
        elif primary == 1:
            rationale_parts.append(
                f"bridge contains one endpoint name "
                f"('{src_name if s_hit else tgt_name}')"
            )
        else:
            rationale_parts.append("bridge contains neither endpoint name")
        if secondary_kinds:
            rationale_parts.append(
                f"relationship_kind = {', '.join(secondary_kinds)}"
            )
        rationale = "; ".join(rationale_parts)
        ranked.append((score, rationale, path))
    ranked.sort(key=lambda r: -r[0])
    return ranked


def _resolve_bridges(tables, fk_rows, columns_by_key=None):
    """Walk the FK graph (fk_rows) to find unmapped junction tables that
    bridge otherwise-unreachable mapped tables.

    Returns (bridges_to_add, ambiguous_or_None, picks) where:
      - `bridges_to_add`: dict keyed by `{schema}.{table}` → {schema, table}
      - `ambiguous_or_None`: first `_AmbiguousBridges` encountered or None
      - `picks`: list of {src, tgt, path, rationale} for any path that the
        ranker auto-picked (strict-winner). Empty when no auto-pick was
        needed (all unambiguous) or when ambiguity short-circuits the loop.

    Algorithm:
      1. Build adjacency from fk_rows. Determine the direct-FK component
         containing the first mapped table.
      2. For each unreachable mapped table, BFS-find all shortest paths
         from the component to it.
      3. If multiple distinct intermediate-sets exist at the shortest
         length, run `_rank_bridge_paths`. If the top score is strictly
         greater than the second-best score, auto-pick that path; record
         a pick in `picks`. Otherwise emit `_AmbiguousBridges`.
      4. Add intermediates to `bridges_to_add`; mark them reachable for
         subsequent iterations.
    """
    from collections import defaultdict

    adj = defaultdict(set)
    # Track the underlying Neo4j URI per `{schema}.{table}` key. Caller uses
    # this to fetch the bridge table's column list (needed for temporal-bridge
    # detection) without making a second round-trip per bridge.
    key_to_uri = {}
    for fk in fk_rows:
        a = f"{fk['from_schema']}.{fk['from_table']}"
        b = f"{fk['to_schema']}.{fk['to_table']}"
        if a and b:
            adj[a].add(b)
            adj[b].add(a)
        if fk.get("from_uri"):
            key_to_uri.setdefault(a, fk["from_uri"])
        if fk.get("to_uri"):
            key_to_uri.setdefault(b, fk["to_uri"])

    mapped_keys = set(tables.keys())
    if len(mapped_keys) <= 1:
        return {}, None, []

    direct_edges = defaultdict(set)
    for fk in fk_rows:
        a = f"{fk['from_schema']}.{fk['from_table']}"
        b = f"{fk['to_schema']}.{fk['to_table']}"
        if a in mapped_keys and b in mapped_keys:
            direct_edges[a].add(b)
            direct_edges[b].add(a)

    def _component(start, edges):
        seen = {start}
        stack = [start]
        while stack:
            n = stack.pop()
            for m in edges.get(n, ()):
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        return seen

    first = next(iter(mapped_keys))
    direct_component = _component(first, direct_edges)
    if mapped_keys.issubset(direct_component):
        return {}, None, []

    bridges_to_add = {}
    picks = []
    unreached = mapped_keys - direct_component
    for tgt in unreached:
        all_paths = _shortest_paths_all(adj, direct_component, tgt)
        if not all_paths:
            continue
        intermediate_sets = {tuple(p[1:-1]) for p in all_paths}
        chosen_path = all_paths[0]
        chosen_rationale = None
        if len(intermediate_sets) > 1:
            # Ranker tie-break: prefer the path whose intermediate node
            # names contain the endpoint names (cf. `_rank_bridge_paths`).
            ranked = _rank_bridge_paths(
                all_paths, chosen_path[0], tgt, meta_by_key=columns_by_key
            )
            top_score = ranked[0][0]
            second_score = ranked[1][0] if len(ranked) > 1 else -1
            if top_score > second_score:
                # Strict winner: auto-pick.
                chosen_path = ranked[0][2]
                chosen_rationale = ranked[0][1]
                picks.append({
                    "src":       chosen_path[0],
                    "tgt":       tgt,
                    "path":      list(chosen_path),
                    "rationale": chosen_rationale,
                })
            else:
                # Tie at top tier — still ambiguous. Surface the candidates.
                return bridges_to_add, _AmbiguousBridges(
                    chosen_path[0], tgt, all_paths, top_score=top_score
                ), picks

        # Unambiguous or auto-picked: add intermediates as bridges.
        for hop in chosen_path[1:-1]:
            if hop in mapped_keys or hop in bridges_to_add:
                continue
            schema, _, table = hop.partition(".")
            bridges_to_add[hop] = {
                "schema": schema,
                "table": table,
                "uri":    key_to_uri.get(hop),  # may be None for unknown URIs
            }
        direct_component |= set(chosen_path)
    return bridges_to_add, None, picks


def _shortest_paths_all(adj, sources, target):
    """BFS from any node in `sources` (treated as virtual single source) to
    `target`. Returns every distinct shortest path as a list of node keys
    (including endpoints). Empty list if unreachable.
    """
    from collections import deque
    # Multi-source BFS: enqueue every source at depth 0 with its own path.
    queue = deque([(s, [s]) for s in sources])
    # visited[node] = depth at which the node was FIRST reached; later
    # visits at the same depth via different routes are allowed (to find
    # ALL shortest paths), deeper revisits are pruned.
    visited = {s: 0 for s in sources}
    paths = []
    target_depth = None
    while queue:
        node, path = queue.popleft()
        depth = len(path) - 1
        if target_depth is not None and depth >= target_depth:
            # We're past the shortest depth; safe to bail since BFS visits
            # in nondecreasing depth order.
            break
        for neighbor in adj.get(node, ()):
            new_depth = depth + 1
            if neighbor == target:
                if target_depth is None:
                    target_depth = new_depth
                if new_depth == target_depth:
                    paths.append(path + [neighbor])
                continue
            existing = visited.get(neighbor)
            if existing is None or existing == new_depth:
                visited[neighbor] = new_depth
                queue.append((neighbor, path + [neighbor]))
    return paths


def _extract_alias_column(join_condition, alias):
    """From a join_condition like `t4.employee_id = t3.id`, return the column
    name attached to `alias` (e.g. 'employee_id' when alias='t4'). Used to
    derive the PARTITION BY column for temporal-bridge ROW_NUMBER lowering.
    Returns None if the alias isn't found.
    """
    prefix = f"{alias}."
    # The condition format is `alias1.col1 = alias2.col2 [AND alias.col = ...]`
    for ref in re.findall(r"(\w+)\.(\w+)", join_condition):
        if ref[0] == alias:
            return ref[1]
    return None


def _extract_alias_columns(predicate, alias):
    """Every column referenced as `<alias>.<col>` in a predicate — plural
    sibling of `_extract_alias_column`. Used to build the DISTINCT projection
    of a pure-bridge (junction) derived table: the projection must cover every
    column any join predicate reads off the junction alias."""
    return {c for a, c in re.findall(r"(\w+)\.(\w+)", predicate or "") if a == alias}


def _build_from_fk_inferred(*, tables, alias_lookup, fk_rows, output_dataset_uri,
                            columns_by_key=None, view_schema="public",
                            scd_policy=None, grain_natural_key=None,
                            junction_candidates=None, dialect=None,
                            join_diagnostics=None):
    """FK-driven LEFT JOIN inference. When mapped tables don't all sit in a
    single connected component of the direct-FK graph, auto-bridge through
    unmapped junction tables found by BFS in `_resolve_bridges`. Fails loudly
    when the BFS finds either no path (no bridge can save us) or multiple
    equally-short paths through different junctions (ambiguous — engineer
    disambiguates via explicit :DatasetTransform.joins[] or by adding a
    mapping that touches one of the candidates).

    When `columns_by_key` is supplied (a `{schema.table → {column_names: [...]}}`
    map fetched upstream), bridges with detectable temporal columns are
    wrapped in a ROW_NUMBER() OVER (PARTITION BY <fk> ORDER BY <temporal>
    DESC NULLS LAST) = 1 derived table so the bridge contributes the latest
    row per natural key. Eliminates row-multiplication when the bridge is a
    history table (e.g. department_employee with from_date/to_date).
    """
    columns_by_key = columns_by_key or {}
    # Stable URI → real column names, for URI-keyed FK column validation (robust
    # across the served-map rewrite that diverges rendered schema.table).
    columns_by_uri = {
        e["uri"]: (e.get("column_names") or [])
        for e in columns_by_key.values() if e.get("uri")
    }
    _d_fk = dialect or _DIALECTS["postgres"]
    # Auto-bridge: extend `tables[]` and `alias_lookup` with any unmapped
    # junction tables BFS discovers in the FK graph. After this call, the
    # FK-matching loop below sees the bridges as in-scope and can resolve
    # joins through them as if the engineer had mapped them explicitly.
    # `picks` carries auto-pick rationales when the name-similarity ranker
    # broke a tie — surfaced upstream into summary['auto_bridge_choice'].
    bridges_to_add, ambig, picks = _resolve_bridges(tables, fk_rows, columns_by_key=columns_by_key)
    if ambig is not None:
        # Format the candidate paths as human-readable arrows. Each path is
        # a list of "{schema}.{table}" keys including the endpoints.
        formatted = "\n      - " + "\n      - ".join(
            " → ".join(p) for p in ambig.candidate_paths
        )
        # Note the ranker was attempted; tied at the top score.
        tier_note = (
            f"\n\nThe name-similarity ranker tried to disambiguate but tied "
            f"at score {ambig.top_score} (out of 2). A clear winner would "
            f"have one junction name containing both endpoint names "
            f"('{_bridge_name_token(ambig.src_key)}' and "
            f"'{_bridge_name_token(ambig.tgt_key)}') while the other "
            f"contains fewer — that signal isn't present here."
        ) if ambig.top_score is not None else ""
        raise ViewGenerationError(
            f"Output dataset {output_dataset_uri}: cannot auto-bridge "
            f"{ambig.src_key} → {ambig.tgt_key} — the FK graph has multiple "
            f"equally-short paths through different junction tables:"
            f"{formatted}{tier_note}\n\n"
            f"Disambiguate by:\n"
            f"  - adding ANY mapping that touches one of the candidate "
            f"junctions (its name then biases the ranker, or the junction "
            f"becomes in-scope and bridging is no longer needed), or\n"
            f"  - declaring an explicit join via "
            f":DatasetTransform.joins[] on this output dataset."
        )
    bridge_keys = set()
    for key, info in bridges_to_add.items():
        if key in tables:
            continue
        alias = f"t{len(tables) + 1}"
        tables[key] = {"schema": info["schema"], "table": info["table"], "alias": alias}
        alias_lookup[key] = alias
        bridge_keys.add(key)
    # Annotate the picks with the assigned alias + auto-detected temporal
    # column (if any) so the caller can render audit comments.
    for p in picks:
        for hop in p["path"][1:-1]:
            cols = (columns_by_key.get(hop) or {}).get("column_names") or []
            temporal_col = _pick_temporal_order_column(cols)
            if temporal_col:
                p["temporal_column"] = temporal_col
                p["bridge_key"] = hop
                break

    table_list = list(tables.values())
    from_clause = f"    {table_list[0]['schema']}.{table_list[0]['table']} AS {table_list[0]['alias']}"
    # Mapped satellites auto-deduped to their current row (reported upstream so
    # the summary can show "auto-deduped X" instead of a multiplication warning).
    auto_deduped = []

    # As-of context. For an SCD-2 (history) target, an effective-dated
    # satellite/bridge must be joined AS-OF the grain anchor's span start — NOT
    # current-row — so historical rows carry period-correct attributes (the
    # reported wrong-history bug). The anchor (table_list[0]) IS the grain; its
    # effective-from column is the pivot. Derived from declared metadata, never
    # hardcoded: scd_policy.type drives the mode, temporal columns are detected.
    _scd2 = (scd_policy or {}).get("type") == "scd2"
    _anchor_alias = table_list[0]["alias"]
    _anchor_key = f"{table_list[0]['schema']}.{table_list[0]['table']}"
    _anchor_cols = (columns_by_key.get(_anchor_key) or {}).get("column_names") or []
    _anchor_pivot = _pick_effective_from_column(_anchor_cols) if _scd2 else None
    # Cross-product natural-key bridges synthesized below (reported in summary).
    cross_product_bridges = []
    # Gap-B junctions synthesized below: join-only datasets pulled from a
    # CONSUMES'd product, emitted as a DISTINCT-projected derived table so a
    # ledger-grain junction (transaction ≈ N rows per entity) can't fan the
    # view out. The projection set isn't final until every chained table has
    # picked its key, so the clause is a placeholder patched after the loop.
    junction_info = {}  # join_key -> {alias, schema, table, on, projection, entry}

    # Worklist: emit each JOIN only after at least one of its FK endpoints is
    # already in the FROM/JOIN scope. Required once auto-bridges are in play
    # — a bridge may appear later in `table_list` than a mapped table that
    # depends on it, so iterating in order and accepting an FK to a
    # not-yet-joined alias would emit invalid SQL (alias referenced before
    # defined). Worklist loops until every table is placed; raises if no
    # progress is possible.
    joined_keys = {f"{table_list[0]['schema']}.{table_list[0]['table']}"}
    # Parallel ordered view of joined_keys: the cross-product fallback below
    # chains a stuck table to ANY already-joined table (anchor first, then in
    # join order), so it needs deterministic iteration order — a set can't
    # provide that.
    joined_order = [f"{table_list[0]['schema']}.{table_list[0]['table']}"]
    pending = list(table_list[1:])
    join_clauses = []
    while pending:
        progress = False
        next_pending = []
        for tinfo in pending:
            join_key = f"{tinfo['schema']}.{tinfo['table']}"
            join_condition = None
            for fk in fk_rows:
                fk_from = f"{fk['from_schema']}.{fk['from_table']}"
                fk_to = f"{fk['to_schema']}.{fk['to_table']}"
                fk_cols = [c.strip() for c in (fk["fk_columns"] or "").split(",")]
                pk_cols = [c.strip() for c in (fk["pk_columns"] or "").split(",")]

                if fk_from == join_key and fk_to in joined_keys:
                    other_alias = alias_lookup.get(fk_to)
                    if other_alias:
                        _assert_fk_join_columns(
                            fk, fk_cols, pk_cols, columns_by_uri,
                            from_alias=tinfo['alias'], to_alias=other_alias,
                            diagnostics=join_diagnostics)
                        conditions = [f"{tinfo['alias']}.{fc} = {other_alias}.{pc}"
                                      for fc, pc in zip(fk_cols, pk_cols)]
                        join_condition = " AND ".join(conditions)
                        break
                elif fk_to == join_key and fk_from in joined_keys:
                    other_alias = alias_lookup.get(fk_from)
                    if other_alias:
                        _assert_fk_join_columns(
                            fk, fk_cols, pk_cols, columns_by_uri,
                            from_alias=other_alias, to_alias=tinfo['alias'],
                            diagnostics=join_diagnostics)
                        conditions = [f"{other_alias}.{fc} = {tinfo['alias']}.{pc}"
                                      for fc, pc in zip(fk_cols, pk_cols)]
                        join_condition = " AND ".join(conditions)
                        break

            if join_condition:
                # Current-row wrap: render the JOIN against a
                # ROW_NUMBER() = 1 derived table partitioned by this table's FK
                # column on the "to-the-table" side of the join, so it
                # contributes only the latest row per natural key. Universal
                # lowering — portable across SQL dialects. Applied to:
                #   - auto-added BRIDGE tables with a temporal column (history
                #     junction tables, e.g. department_employee with from/to_date);
                #   - mapped SATELLITE tables that carry genuine SCD effective-
                #     dating (`_satellite_needs_dedup`). Without this a lookup
                #     keyed off the satellite (e.g. job_title via
                #     job_assignment_history.job_id) — or any passthrough column
                #     from it — fans the view out to one row per historical
                #     version. The anchor table (table_list[0]) is never in this
                #     worklist, so its grain is preserved.
                wrap_temporal_col = None
                wrap_partition_col = None
                wrap_is_current = None
                meta = columns_by_key.get(join_key) or {}
                is_bridge = join_key in bridge_keys
                cols = meta.get("column_names") or []
                needs_dedup = is_bridge or _satellite_needs_dedup(join_key, meta)

                # AS-OF (SCD-2 target): an effective-dated satellite/bridge is
                # joined to the anchor's span-start pivot, picking the one row
                # whose validity window contains it — period-correct history, no
                # row multiplication, no current-row collapse. Takes priority
                # over the current-row wrap when the target is SCD-2.
                emitted = False
                if needs_dedup and _scd2 and _anchor_pivot and _is_effective_dated(cols):
                    eff_from = _pick_effective_from_column(cols)
                    eff_to = _pick_effective_to_column(cols)
                    asof = f"{tinfo['alias']}.{eff_from} <= {_anchor_alias}.{_anchor_pivot}"
                    if eff_to:
                        asof += (f" AND ({tinfo['alias']}.{eff_to} IS NULL"
                                 f" OR {tinfo['alias']}.{eff_to} > {_anchor_alias}.{_anchor_pivot})")
                    join_clauses.append(
                        f"    LEFT JOIN {tinfo['schema']}.{tinfo['table']} AS {tinfo['alias']}\n"
                        f"        ON {join_condition}\n"
                        f"        AND {asof}"
                    )
                    auto_deduped.append({
                        "table": join_key, "mode": "as_of",
                        "pivot": f"{_anchor_alias}.{_anchor_pivot}",
                        "effective_from": eff_from, "effective_to": eff_to,
                    })
                    emitted = True

                if not emitted and needs_dedup:
                    wrap_temporal_col = _pick_temporal_order_column(cols)
                    if wrap_temporal_col:
                        # The partition key is this table's FK column on the
                        # side that connects to the already-joined table —
                        # i.e., the column appearing on `tinfo['alias'].*` in
                        # join_condition. Parse it back out.
                        wrap_partition_col = _extract_alias_column(
                            join_condition, tinfo["alias"]
                        )
                        # is_current narrowing is scoped to mapped satellites
                        # (the new path); bridge wraps keep their prior
                        # ROW_NUMBER-only shape to stay byte-identical for
                        # existing bridge-using products.
                        if not is_bridge:
                            wrap_is_current = _pick_is_current_column(cols)

                if emitted:
                    pass
                elif wrap_temporal_col and wrap_partition_col:
                    where_cur = (
                        f"            WHERE {wrap_is_current} = true\n"
                        if wrap_is_current else ""
                    )
                    join_clauses.append(
                        f"    LEFT JOIN (\n"
                        f"        SELECT * FROM (\n"
                        f"            SELECT *, ROW_NUMBER() OVER (\n"
                        f"                PARTITION BY {wrap_partition_col}\n"
                        f"                ORDER BY {wrap_temporal_col} DESC{_d_fk.nulls_last}\n"
                        f"            ) AS _rn\n"
                        f"            FROM {tinfo['schema']}.{tinfo['table']}\n"
                        f"{where_cur}"
                        f"        ) t WHERE _rn = 1\n"
                        f"    ) AS {tinfo['alias']}\n"
                        f"        ON {join_condition}"
                    )
                    if not is_bridge:
                        auto_deduped.append({
                            "table":            join_key,
                            "temporal_column":  wrap_temporal_col,
                            "partition_column": wrap_partition_col,
                            "is_current_filter": wrap_is_current,
                        })
                else:
                    join_clauses.append(
                        f"    LEFT JOIN {tinfo['schema']}.{tinfo['table']} AS {tinfo['alias']}\n"
                        f"        ON {join_condition}"
                    )
                joined_keys.add(join_key)
                joined_order.append(join_key)
                progress = True
            else:
                next_pending.append(tinfo)

        if not progress:
            # Cross-product / no-FK fallback. `:REFERENCES` never crosses source
            # products, so a consumer mixing tables from two source products has
            # NO FK edge between them — FK BFS can't connect them. Bridge each
            # remaining table to the already-joined grain anchor on a shared
            # identity key (the grain's natural key when known). For an SCD-2
            # target an effective-dated bridge is joined AS-OF the anchor's span
            # start (one bridge row per anchor span); otherwise current-row
            # (latest per key). This auto-constructs what an engineer would
            # otherwise hand-author via :DatasetTransform.joins[].
            bridged = []
            still = []
            for tinfo in next_pending:
                join_key = f"{tinfo['schema']}.{tinfo['table']}"
                cols = (columns_by_key.get(join_key) or {}).get("column_names") or []
                # Transitive chaining: the bridge partner is ANY already-joined
                # table, not just the anchor. Anchor keeps absolute priority —
                # when it shares a key the emission is identical to the
                # pre-chaining behavior — but when it doesn't, a stuck table
                # may chain onto an intermediate (account —account_id→
                # transaction —customer_id→ customer). Non-anchor partners are
                # ranked by _score_chain_key; ties break on join order.
                partner_key, partner_alias, shared = None, None, []
                anchor_shared = _shared_key_columns(_anchor_cols, cols, preferred=grain_natural_key)
                if anchor_shared:
                    partner_key, partner_alias, shared = _anchor_key, _anchor_alias, anchor_shared
                else:
                    best = None  # (score, -order_idx) -> (partner_key, alias, key)
                    for order_idx, cand_key in enumerate(joined_order[1:], start=1):
                        cand_cols = (columns_by_key.get(cand_key) or {}).get("column_names") or []
                        cand_shared = _shared_key_columns(cand_cols, cols, preferred=grain_natural_key)
                        cand_alias = alias_lookup.get(cand_key)
                        if not cand_shared or not cand_alias:
                            continue
                        for k in cand_shared:
                            score = _score_chain_key(k, join_key, cand_key, grain_natural_key)
                            rank = (score, -order_idx)
                            if best is None or rank > best[0]:
                                best = (rank, (cand_key, cand_alias, k))
                    if best:
                        partner_key, partner_alias, k = best[1]
                        shared = [k]
                if not shared or not partner_alias:
                    still.append(tinfo)
                    continue
                key = shared[0]
                base_on = f"{tinfo['alias']}.{key} = {partner_alias}.{key}"
                if partner_key in junction_info:
                    # Chaining through a DISTINCT-projected junction: the
                    # junction's derived table must project this key too.
                    junction_info[partner_key]["projection"].add(key)
                eff_dated = _is_effective_dated(cols)
                if _scd2 and _anchor_pivot and eff_dated:
                    eff_from = _pick_effective_from_column(cols)
                    eff_to = _pick_effective_to_column(cols)
                    asof = f"{tinfo['alias']}.{eff_from} <= {_anchor_alias}.{_anchor_pivot}"
                    if eff_to:
                        asof += (f" AND ({tinfo['alias']}.{eff_to} IS NULL"
                                 f" OR {tinfo['alias']}.{eff_to} > {_anchor_alias}.{_anchor_pivot})")
                    join_clauses.append(
                        f"    LEFT JOIN {tinfo['schema']}.{tinfo['table']} AS {tinfo['alias']}\n"
                        f"        ON {base_on}\n"
                        f"        AND {asof}"
                    )
                    mode = "as_of"
                elif eff_dated:
                    eff_order = _pick_temporal_order_column(cols)
                    is_cur = _pick_is_current_column(cols)
                    where_cur = f"            WHERE {is_cur} = true\n" if is_cur else ""
                    join_clauses.append(
                        f"    LEFT JOIN (\n"
                        f"        SELECT * FROM (\n"
                        f"            SELECT *, ROW_NUMBER() OVER (\n"
                        f"                PARTITION BY {key}\n"
                        f"                ORDER BY {eff_order} DESC{_d_fk.nulls_last}\n"
                        f"            ) AS _rn\n"
                        f"            FROM {tinfo['schema']}.{tinfo['table']}\n"
                        f"{where_cur}"
                        f"        ) t WHERE _rn = 1\n"
                        f"    ) AS {tinfo['alias']}\n"
                        f"        ON {base_on}"
                    )
                    mode = "current_row"
                else:
                    join_clauses.append(
                        f"    LEFT JOIN {tinfo['schema']}.{tinfo['table']} AS {tinfo['alias']}\n"
                        f"        ON {base_on}"
                    )
                    mode = "equi"
                cross_product_bridges.append({
                    "table": join_key, "key": key, "mode": mode,
                    "via": partner_key,
                })
                joined_keys.add(join_key)
                joined_order.append(join_key)
                bridged.append(tinfo)
            if bridged:
                pending = still
                continue

            # Gap B: no shared key chains the leftovers onto the join graph —
            # look for a JUNCTION dataset among the CONSUMES'd products' other
            # output datasets (a consumed-but-unmapped table sharing DIFFERENT
            # identity keys with the two sides, e.g. transaction carrying
            # account_id + customer_id). A strict winner is synthesized as a
            # pure bridge (DISTINCT-projected, no SELECT columns); a tie is
            # raised, never guessed.
            if junction_candidates:
                added_junction = False
                ambiguous = None  # (stuck_key, tied_candidates)
                for tinfo in next_pending:
                    stuck_key = f"{tinfo['schema']}.{tinfo['table']}"
                    stuck_cols = (columns_by_key.get(stuck_key) or {}).get("column_names") or []
                    remaining = [c for c in junction_candidates if c["key"] not in tables]
                    ranked = _rank_junction_candidates(
                        remaining, joined_order, columns_by_key,
                        stuck_key, stuck_cols, preferred=grain_natural_key)
                    if not ranked:
                        continue
                    top = ranked[0]
                    if len(ranked) > 1 and ranked[1]["score"] >= top["score"]:
                        if ambiguous is None:
                            ambiguous = (stuck_key,
                                         [r for r in ranked if r["score"] == top["score"]])
                        continue
                    cand = top["cand"]
                    jkey = cand["key"]
                    alias = f"t{len(tables) + 1}"
                    tables[jkey] = {"schema": cand["schema"], "table": cand["table"],
                                    "alias": alias}
                    alias_lookup[jkey] = alias
                    columns_by_key.setdefault(jkey, {
                        "column_names": list(cand.get("cols") or []),
                        "relationship_kind": cand.get("relationship_kind") or "",
                    })
                    kj = top["key_joined"]
                    on = f"{alias}.{kj} = {alias_lookup[top['partner_key']]}.{kj}"
                    projection = {kj}
                    jcols = cand.get("cols") or []
                    if _scd2 and _anchor_pivot and _is_effective_dated(jcols):
                        # Effective-dated junction + SCD-2 target: DISTINCT over
                        # (keys, validity span) + as-of vs the anchor pivot —
                        # period-correct linkage without version fan-out.
                        eff_from = _pick_effective_from_column(jcols)
                        eff_to = _pick_effective_to_column(jcols)
                        on += (f" AND {alias}.{eff_from} <= {_anchor_alias}.{_anchor_pivot}"
                               f" AND ({alias}.{eff_to} IS NULL"
                               f" OR {alias}.{eff_to} > {_anchor_alias}.{_anchor_pivot})")
                        projection.update({eff_from, eff_to})
                    entry = {"table": jkey, "key": kj, "keys": [kj],
                             "mode": "junction_distinct", "via": top["partner_key"],
                             "bridge_only": True}
                    cross_product_bridges.append(entry)
                    placeholder = f"__JUNCTION__{jkey}__"
                    join_clauses.append(placeholder)
                    junction_info[jkey] = {
                        "alias": alias, "schema": cand["schema"], "table": cand["table"],
                        "on": on, "projection": projection, "entry": entry,
                        "placeholder": placeholder,
                    }
                    joined_keys.add(jkey)
                    joined_order.append(jkey)
                    added_junction = True
                if added_junction:
                    # The stuck tables now chain onto the junction via the
                    # regular shared-key fallback on the next pass.
                    pending = next_pending
                    continue
                if ambiguous is not None:
                    amb_stuck, tied = ambiguous
                    formatted = "\n      - " + "\n      - ".join(
                        f"{r['cand']['key']} (score {r['score']}: links "
                        f"{r['partner_key']} on `{r['key_joined']}` ↔ {amb_stuck} "
                        f"on `{r['key_stuck']}`)"
                        for r in tied
                    )
                    raise ViewGenerationError(
                        f"Output dataset {output_dataset_uri}: cannot auto-bridge "
                        f"{amb_stuck} — multiple junction datasets from the "
                        f"CONSUMES'd products tie as candidates:{formatted}\n\n"
                        f"Disambiguate by:\n"
                        f"  - declaring an explicit join via "
                        f":DatasetTransform.joins[] on this output dataset "
                        f"(mark the junction entry bridge_only), or\n"
                        f"  - adding a mapping that reads from the intended "
                        f"junction so it becomes an in-scope base table."
                    )

            # Stuck: every remaining table needs at least one FK partner
            # already joined, but none do — and no shared identity key exists to
            # bridge on either. Raise with the first stuck table's key.
            stuck = next_pending[0]
            stuck_key = f"{stuck['schema']}.{stuck['table']}"
            raise ViewGenerationError(
                f"Output dataset {output_dataset_uri}: source table "
                f"{stuck_key} is referenced by mappings but no FK path "
                f"connects it to other tables in the FROM clause.\n\n"
                f"Likely causes (most common first):\n\n"
                f"  1. (consuming a SOURCE-ALIGNED product) the source "
                f"product's :DProdOutputDataset nodes are missing :REFERENCES "
                f"edges — the source contract was materialised before "
                f"DPROD_PROPAGATE_FK shipped. Fix: re-run odcs_to_dprod on the "
                f"SOURCE-ALIGNED project to repopulate the FK edges (idempotent, "
                f"no data loss). NOTE: this does NOT apply when the upstream is "
                f"itself an AGGREGATE or CONSUMER product — those have no "
                f"catalog and therefore no FK edges to propagate; re-running "
                f"never creates edges they never had. For a derived upstream "
                f"whose datasets don't share an identity key, you MUST declare "
                f"an explicit join (cause 2).\n\n"
                f"  2. The join graph is under-declared. If two datasets have no "
                f"direct FK and don't share an identity key to auto-bridge on, "
                f"declare an explicit join via :DatasetTransform.joins[] "
                f"(set_dataset_joins / the wizard Shape step). This is the "
                f"REQUIRED path for a multi-dataset aggregate/consumer upstream — "
                f"a consumer product exposes no :REFERENCES among its output "
                f"datasets, so name-based auto-bridging is a convenience, never "
                f"guaranteed. (A single-dataset upstream needs no join and never "
                f"hits this.)\n\n"
                f"  3. (dpe-sa / catalog sources) the catalog :REFERENCES "
                f"edge between the two datasets was never discovered. Add "
                f"the FK to the graph via a re-run of data_discovery, or "
                f"declare an explicit join.\n\n"
                f"  4. A junction table that would link the two sides (one "
                f"sharing a key with each) may live in a product this consumer "
                f"doesn't :CONSUMES — junction auto-bridging only searches "
                f"directly-consumed products. Add the missing product to the "
                f"consumer's sources (wizard step 9 / spec inputs[]) and "
                f"re-run."
            )

        pending = next_pending

    # Patch junction placeholders now that every chained table has picked its
    # key: the DISTINCT projection must cover every column later joins
    # reference on the junction alias. DISTINCT over just the link keys is
    # what makes a ledger-grain junction safe — N transaction rows per
    # account collapse to the distinct (account_id, customer_id) pairs.
    if junction_info:
        by_placeholder = {info["placeholder"]: info for info in junction_info.values()}
        for i, clause in enumerate(join_clauses):
            info = by_placeholder.get(clause)
            if not info:
                continue
            proj = sorted(info["projection"], key=str.lower)
            info["entry"]["keys"] = proj
            join_clauses[i] = (
                f"    LEFT JOIN (\n"
                f"        SELECT DISTINCT {', '.join(proj)}\n"
                f"        FROM {info['schema']}.{info['table']}\n"
                f"    ) AS {info['alias']}\n"
                f"        ON {info['on']}"
            )

    # Surface synthesized cross-product bridges alongside the satellite-dedup
    # report so the summary/UI can show how disconnected source products were
    # auto-joined (key + as-of/current/equi mode).
    if cross_product_bridges:
        auto_deduped = list(auto_deduped) + [
            {**b, "cross_product_bridge": True} for b in cross_product_bridges
        ]
    return from_clause, join_clauses, picks, auto_deduped


# ─────────────────────────────────────────────────────────────────────────────
# Transform capability diagnostics (Phase 3 of transform-portability.md)
# ─────────────────────────────────────────────────────────────────────────────
# This script runs BOTH as a standalone subprocess (the serving agent) and
# imported-by-path by the backend. Capability validation lives in the backend
# (`workbench.backend.dialect_sql.compile_expression`, authority =
# `platform/transform_capabilities.<ver>.json`). When the backend is importable
# we validate every raw/expression-bearing mapping against the target platform's
# capability artifact and fold the findings into the per-dataset
# `summary['transform_diagnostics']`; when it is NOT (a bare subprocess with the
# repo off sys.path) validation degrades to a no-op. This is not a bypass:
# enforcement is backend-side (`transform_preflight` re-runs this compiler over
# the mappings), so the subprocess's own validation is only an early warning.

_SERVED_VALIDATION_PLATFORMS = {"postgres", "databricks", "snowflake", "bigquery", "mysql"}
_VALIDATOR_SENTINEL = "unloaded"
_CAPABILITY_VALIDATOR = _VALIDATOR_SENTINEL
_CAPABILITIES = _VALIDATOR_SENTINEL


def _get_capability_validator():
    """Return the backend ``compile_expression`` callable, or None (cached).

    Defensive import: adds the repo root to ``sys.path`` so a standalone
    subprocess sharing the venv can still reach the backend. ANY failure →
    None → validation is skipped (never crashes view generation)."""
    global _CAPABILITY_VALIDATOR
    if _CAPABILITY_VALIDATOR is _VALIDATOR_SENTINEL:
        try:
            import sys as _sys
            from pathlib import Path as _Path
            root = str(_Path(__file__).resolve().parents[4])
            if root not in _sys.path:
                _sys.path.insert(0, root)
            from workbench.backend.dialect_sql import compile_expression as _ce
            _CAPABILITY_VALIDATOR = _ce
        except Exception:
            _CAPABILITY_VALIDATOR = None
    return _CAPABILITY_VALIDATOR


def _translate_raw_expression(expr, m, dialect):
    """Render a raw ``transformExpression`` from its authored dialect to the
    target dialect via the transform-portability compiler
    (``dialect_sql.compile_expression``), so a Postgres-authored steward-catalog
    expression (e.g. ``REGEXP_REPLACE(<phone>, '[^0-9]', '', 'g')``) emits native
    SQL on Databricks/Snowflake/BigQuery instead of leaking the Postgres ``'g'``
    flag. This closes the "verbatim raw-SQL emission" gap in
    docs/architecture/transform-portability.md — the emit path now consumes the
    ``CompileResult.sql`` the diagnostics pass already computes but discarded.

    Activates ONLY cross-dialect (authored != target) so same-dialect products are
    byte-unchanged. **Fail-open**: an unknown platform, an unsupported/unknown
    function (``result.errors``), or any exception leaves ``expr`` verbatim — never
    a regression vs. today; the capability diagnostics pass still surfaces the
    problem (enforcement remains backend-side)."""
    target = (getattr(dialect, "name", "") or "").lower()
    read = (m.get("expression_dialect") or "postgres").strip().lower()
    if not target or target == read:
        return expr  # same-dialect → zero change
    if target not in _SERVED_VALIDATION_PLATFORMS:
        return expr  # ansi / duckdb / unknown → don't risk a transpile
    validate = _get_capability_validator()  # dialect_sql.compile_expression
    if validate is None:
        return expr
    try:
        result = validate(expr, target, read=read)
    except Exception:
        return expr  # never let translation crash view generation
    if result.errors or not result.sql:
        return expr  # unsupported/unknown function → fail-open to verbatim
    return result.sql


def _get_capabilities():
    """Return the backend capability artifact object (for op-level checks), or
    None (cached). Same defensive-import contract as _get_capability_validator."""
    global _CAPABILITIES
    if _CAPABILITIES is _VALIDATOR_SENTINEL:
        try:
            from workbench.backend.platform.transform_capabilities import get_capabilities
            _CAPABILITIES = get_capabilities()
        except Exception:
            _CAPABILITIES = None
    return _CAPABILITIES


def _check_op_capability(diagnostics, pc_name, m, platform, op, semantics):
    """Check a neutral op (e.g. date_difference) against the artifact's op matrix
    and record an error diagnostic when the (op, semantics, platform) is
    unsupported — no silent semantic degradation."""
    caps = _get_capabilities()
    if caps is None:
        return
    support = caps.op_support(op, semantics, platform)
    if support in ("native", "emulated"):
        diagnostics["used_capabilities"].append(f"op:{op}/{semantics}@{platform}:{support}")
        return
    entry = caps.op_entry(op, semantics, platform) or {}
    diagnostics["errors"].append({
        "severity": "error",
        "code": "unsupported_op",
        "message": f"{op}(semantics='{semantics}') is {support or 'unknown'} on {platform}.",
        "remediation": entry.get("remediation") or "Author a platform-supported semantics instead.",
        "product_col": pc_name,
        "mapping_uri": m.get("mapping_uri"),
    })


def _empty_transform_diagnostics():
    return {
        "errors": [], "warnings": [], "used_capabilities": [],
        "validated": False, "catalog_version": None,
    }


def _accumulate_transform_diagnostics(diagnostics, product_cols, dialect):
    """Validate each product column's authored transform expression against the
    target platform's capability artifact; record structured-kind usage. Mutates
    ``diagnostics`` in place. No-op when the platform isn't a served native-view
    platform (ansi/duckdb) or the backend validator isn't importable."""
    platform = (getattr(dialect, "name", "") or "").lower()
    if platform not in _SERVED_VALIDATION_PLATFORMS:
        return
    validate = _get_capability_validator()
    if validate is None:
        return
    diagnostics["validated"] = True
    for pc_name, pc_mappings in product_cols.items():
        m = pc_mappings[0]
        kind = (m.get("transform_kind") or "").lower() or "direct"
        expr = (m.get("transform_expression") or "").strip()
        # Phase 7: the neutral date_difference op is validated at the OP level
        # (its semantics discriminator) against the capability matrix, not as raw
        # SQL — surfaces "symbolic_interval unsupported on databricks" at author
        # time instead of a generation-time ViewGenerationError.
        if kind == "date_difference":
            params = _parse_json(m.get("transform_params_json"), {})
            semantics = (params.get("semantics") or "completed_units").strip().lower()
            _check_op_capability(diagnostics, pc_name, m, platform, "date_difference", semantics)
            continue
        if kind == "bucket":
            # A bucket banding over a nested neutral value-op inherits that op's
            # capability — surface e.g. symbolic_interval-in-a-bucket at author time.
            params = _parse_json(m.get("transform_params_json"), {})
            vspec = params.get("value")
            if isinstance(vspec, dict) and (vspec.get("op") or "").strip().lower() == "date_difference":
                semantics = (vspec.get("semantics") or "completed_units").strip().lower()
                _check_op_capability(diagnostics, pc_name, m, platform, "date_difference", semantics)
            diagnostics["used_capabilities"].append(f"kind:bucket@{platform}")
            continue
        if not expr:
            # Structured kind (bucket/mask/hash/window/literal/direct/lookup): the
            # Dialect emits platform-appropriate SQL, so there is no raw SQL to
            # parse — record the op for the used-capabilities audit trail.
            diagnostics["used_capabilities"].append(f"kind:{kind}@{platform}")
            continue
        # The source grammar for parsing the authored expression. Default to the
        # TARGET platform (not always postgres) so a Snowflake-authored function
        # (IFF / DATEADD / LISTAGG / …) canonicalises to the sqlglot AST name the
        # capability artifact is keyed on, instead of a stray postgres-parse name
        # that reads as unknown_function. An explicit expression_dialect wins.
        read = (m.get("expression_dialect") or platform or "postgres")
        try:
            result = validate(expr, platform, read=read)
        except Exception as e:  # never let validation crash generation
            diagnostics["warnings"].append({
                "severity": "warning", "code": "validator_error",
                "message": f"capability validation raised: {type(e).__name__}: {e}",
                "product_col": pc_name, "mapping_uri": m.get("mapping_uri"),
            })
            continue
        if diagnostics["catalog_version"] is None:
            diagnostics["catalog_version"] = result.catalog_version
        for d in result.errors:
            rec = d.to_dict()
            rec["product_col"] = pc_name
            rec["mapping_uri"] = m.get("mapping_uri")
            diagnostics["errors"].append(rec)
        for d in result.warnings:
            rec = d.to_dict()
            rec["product_col"] = pc_name
            rec["mapping_uri"] = m.get("mapping_uri")
            diagnostics["warnings"].append(rec)
        diagnostics["used_capabilities"].extend(result.used_capabilities)


def _aggregate_transform_diagnostics(per_view_summaries):
    """Roll the per-dataset ``transform_diagnostics`` up to the emitter level so
    a backend caller reads one ``summary['transform_diagnostics']['errors']``."""
    agg = _empty_transform_diagnostics()
    for s in per_view_summaries:
        if not isinstance(s, dict):
            continue
        td = s.get("transform_diagnostics")
        if not isinstance(td, dict):
            continue
        agg["validated"] = agg["validated"] or td.get("validated", False)
        agg["catalog_version"] = agg["catalog_version"] or td.get("catalog_version")
        agg["errors"].extend(td.get("errors", []))
        agg["warnings"].extend(td.get("warnings", []))
        agg["used_capabilities"].extend(td.get("used_capabilities", []))
    agg["used_capabilities"] = sorted(set(agg["used_capabilities"]))
    return agg


def _generate_ddl_for_dataset(session, product_uri, output_dataset_uri,
                              dataset_physical_name, view_schema, dialect=None):
    """Compile one CREATE VIEW for one :DProdOutputDataset.

    Source-aligned products carry a 1:1 mapping from source table to output
    dataset (one dataset per discovered table), so this function typically
    emits a flat SELECT FROM single table with no joins. Consumer-aligned
    products usually carry one output dataset whose columns span multiple
    source tables. Two paths for the FROM/joins assembly:

      - Explicit joins[] on :DatasetTransform (Phase 4 active): build FROM +
        JOIN clauses verbatim from the declared list, use the engineer-
        supplied aliases. Skips FK discovery entirely.
      - No joins[] (today's behavior): FK-driven LEFT JOIN inference. Any
        required table that cannot be reached via a known FK raises
        ViewGenerationError.

    `dialect` (Phase 7): the Dialect instance for SQL emission. Defaults to
    PostgresDialect when omitted to preserve the pre-Phase-7 behavior; passed
    to `_compile_select_expr` so hash / mask / literal-cast paths get
    dialect-appropriate SQL.
    """
    if dialect is None:
        dialect = _DIALECTS["postgres"]
    view_name = f"vw_{_safe_name(dataset_physical_name)}"

    # Phase 2-4: read the :DatasetTransform up-front so the FROM/joins
    # assembly can branch on whether joins[] is set.
    dt_row = session.run(DATASET_TRANSFORM_QUERY, output_dataset_uri=output_dataset_uri).single()
    dt = _parse_dataset_transform(dt_row)

    mappings = [dict(r) for r in session.run(
        MAPPINGS_QUERY, product_uri=product_uri, output_dataset_uri=output_dataset_uri
    )]
    if not mappings:
        return None, view_name, "No approved mappings", None

    for m in mappings:
        schema_eff, table_eff = _resolve_source_relation(m, view_schema)
        m["source_schema"] = schema_eff
        m["source_table"] = table_eff

    tables = {}
    dataset_uris = set()        # catalog :Dataset URIs (for FK_QUERY)
    dprod_dataset_uris = set()  # source-product :DProdOutputDataset URIs (for DPROD_FK_QUERY)
    for m in mappings:
        if not m.get("source_schema") or not m.get("source_table"):
            continue
        key = f"{m['source_schema']}.{m['source_table']}"
        if key not in tables:
            alias = f"t{len(tables) + 1}"
            tables[key] = {"schema": m["source_schema"], "table": m["source_table"], "alias": alias}
        if m.get("source_kind") == "catalog" and m.get("dataset_uri"):
            dataset_uris.add(m["dataset_uri"])
        elif m.get("source_kind") == "dprod" and m.get("source_dprod_dataset_uri"):
            dprod_dataset_uris.add(m["source_dprod_dataset_uri"])

    # Phase 4: if joins[] is set on the DatasetTransform, the explicit aliases
    # supersede the mapping-derived auto-aliases (t1, t2...) so the rendered
    # SELECT references the engineer-declared table.alias instead.
    explicit_alias_overrides = {}
    explicit_alias_relations = {}
    if dt["joins"]:
        explicit_alias_overrides, explicit_alias_relations = \
            _resolve_explicit_join_aliases(session, dt["joins"], view_schema)
        # Replace the auto-alias with the explicit one for any table covered
        # by joins[]. Tables NOT covered (e.g. lookup-table references) keep
        # their auto-alias.
        for key, info in tables.items():
            if key in explicit_alias_overrides:
                info["alias"] = explicit_alias_overrides[key]

    # Build prefix-scoped FK queries: we need the FULL FK graph for the
    # projects/contracts in scope, not just edges touching mapped tables,
    # so _resolve_bridges can BFS through unmapped junction tables.
    #
    # Catalog URI: `dataset:{project_code}:{schema}.{table}` → prefix is the
    # project_code segment (e.g. `dataset:dpe-sa-05112026-02:`). Splitting on
    # `:` and rejoining the first two segments gives the prefix.
    #
    # Dprod URI: `dprod:ds:{contract_id}:{schema_physical_name}` → prefix is
    # the contract segment (e.g. `dprod:ds:dpe-sa-05112026-02-contract:`).
    catalog_prefixes = sorted({
        ":".join(u.split(":", 2)[:2]) + ":"
        for u in dataset_uris
        if u and u.startswith("dataset:")
    })
    dprod_contract_prefixes = sorted({
        ":".join(u.split(":", 3)[:3]) + ":"
        for u in dprod_dataset_uris
        if u and u.startswith("dprod:ds:")
    })

    fk_rows = []
    if catalog_prefixes:
        fk_rows.extend(dict(r) for r in session.run(FK_QUERY, catalog_prefixes=catalog_prefixes))
    # Dprod-side FK rows: walk :DProdOutputDataset-[:REFERENCES]->:DProdOutputDataset
    # edges propagated from catalog FKs during _generate_dprod. Each row is
    # translated into the (view_schema, vw_<safe>, ...) tuple format so it
    # matches `tables[]` keys without further work in _build_from_fk_inferred.
    if dprod_contract_prefixes:
        for r in session.run(DPROD_FK_QUERY, dprod_contract_prefixes=dprod_contract_prefixes):
            # Honor the served map so FK nodes key-match tables[] entries: a
            # materialized source's edges resolve to catalog.schema.relation, the
            # co-located case stays view_schema.vw_<safe>.
            _fs, _ft = _dprod_relation(r["from_table_raw"], view_schema)
            _ts, _tt = _dprod_relation(r["to_table_raw"], view_schema)
            fk_rows.append({
                "from_schema": _fs,
                "from_table":  _ft,
                "from_uri":    r.get("from_uri"),
                "to_schema":   _ts,
                "to_table":    _tt,
                "to_uri":      r.get("to_uri"),
                "fk_columns":  r["fk_columns"],
                "pk_columns":  r["pk_columns"],
            })

    alias_lookup = {f"{v['schema']}.{v['table']}": v["alias"] for v in tables.values()}

    by_mapping = {}
    for m in mappings:
        by_mapping.setdefault(m["mapping_uri"], []).append(m)

    product_cols = {}
    for mapping_uri, rows in by_mapping.items():
        pc = rows[0]["product_col"]
        product_cols.setdefault(pc, []).extend(rows)

    # Phase 5: suppressedColumns activation. Product columns listed here exist
    # in the contract / graph (lineage + mapping intact) but are NOT projected
    # by the materialized view — primary use case is PII / sensitive data the
    # consumer shouldn't see.
    #
    # Defensive cleanup:
    #   - PKs flagged isPrimaryKey are NEVER suppressed even if declared:
    #     dropping them would break the contract's stated key and SCD-1
    #     dedupe synthesis. Surfaced as a warning.
    #   - dedupe.keys / grouping_keys / scd_policy referencing a suppressed
    #     column are quietly filtered (an explicit dedupe key on a column
    #     that the consumer never sees is degenerate but not destructive —
    #     leaving it in would just emit a SQL error). Cleanup is reported in
    #     the summary so the engineer can fix the underlying authoring.
    #   - If ALL non-PK product columns end up suppressed the view collapses
    #     to a PK-only SELECT; we don't raise — the engineer wanted a
    #     minimal-projection view and that's fine.
    suppressed_declared = list(dt.get("suppressed_columns") or [])
    suppressed_set = set(suppressed_declared)
    suppressed_unknown = [
        s for s in suppressed_declared if s not in product_cols
    ]
    suppressed_pks_skipped = []
    suppressed_applied = []
    if suppressed_set:
        pk_names = {
            pc for pc, rows in product_cols.items()
            if rows and rows[0].get("is_primary_key")
        }
        for pc in list(product_cols.keys()):
            if pc not in suppressed_set:
                continue
            if pc in pk_names:
                suppressed_pks_skipped.append(pc)
                continue
            del product_cols[pc]
            suppressed_applied.append(pc)
        # Re-derive the effective suppression set for downstream cleanup so
        # PKs aren't accidentally stripped from dedupe/grouping/SCD.
        effective_suppressed = set(suppressed_applied)
        if dt.get("dedupe"):
            cleaned = [k for k in dt["dedupe"]["keys"] if k not in effective_suppressed]
            if cleaned != dt["dedupe"]["keys"]:
                if not cleaned:
                    dt["dedupe"] = None
                else:
                    dt["dedupe"] = {**dt["dedupe"], "keys": cleaned}
        if dt.get("grouping_keys"):
            dt["grouping_keys"] = [
                k for k in dt["grouping_keys"] if k not in effective_suppressed
            ]

    # Phase 3 transform-portability: validate authored transform expressions
    # against the target platform's capability artifact. Findings ride in the
    # per-dataset summary; enforcement is applied by the backend caller.
    transform_diagnostics = _empty_transform_diagnostics()
    _accumulate_transform_diagnostics(transform_diagnostics, product_cols, dialect)

    lookup_aliases = {}
    lookup_meta = {}
    # Phase 6 window-function bookkeeping. `window_specs_used` collects the
    # names of windows actually referenced by some mapping (so the audit
    # comment + summary can list them); `window_undefined_refs` collects
    # references that didn't match any declared spec (typo / stale name —
    # surfaced as a warning).
    window_specs = dt.get("window_specs") or {}
    window_undefined_refs: set[str] = set()
    window_specs_used: set[str] = set()

    # Derived-on-derived columns: a mapping whose transformExpression references
    # OTHER product columns of this dataset (declared via the mapper's
    # transform_params.depends_on_product_columns hint). Postgres forbids
    # referencing a sibling SELECT output alias within the same SELECT level, so
    # such a column cannot live in the base SELECT next to the columns it
    # depends on. We defer it to an outer "enriched" CTE layer (built below)
    # where the dependency columns exist as real columns of the prior CTE.
    deferred_derived = {}
    for pc_name, pc_mappings in product_cols.items():
        m0 = pc_mappings[0]
        params = _parse_json(m0.get("transform_params_json"), {})
        deps = [
            d for d in (params.get("depends_on_product_columns") or [])
            if d in product_cols and d != pc_name
        ]
        expr = (m0.get("transform_expression") or "").strip()
        if deps and expr:
            deferred_derived[pc_name] = {
                "expr": expr,
                "deps": deps,
                "decorators": m0.get("transform_decorators_json"),
            }

    select_parts = []
    for pc_name, pc_mappings in product_cols.items():
        if pc_name in deferred_derived:
            continue  # emitted in an outer enriched layer (see below)
        # Track window references BEFORE compiling so we can preserve the
        # mapping → window relationship even if compilation falls through
        # to a degenerate OVER ().
        for pm in pc_mappings:
            if (pm.get("transform_kind") or "").lower() == "window":
                params = _parse_json(pm.get("transform_params_json"), {})
                win = (params.get("window") or "").strip()
                if win:
                    window_specs_used.add(win)
        expr = _compile_select_expr(
            pc_name, pc_mappings, alias_lookup, lookup_aliases, lookup_meta,
            window_specs=window_specs,
            window_undefined_refs=window_undefined_refs,
            dialect=dialect,
            view_schema=view_schema,
        )
        select_parts.append(f"    {expr} AS {_safe_name(pc_name)}")

    # Build the enriched-layer stack for deferred derived-on-derived columns.
    # Greedy topological layering: each layer computes the columns whose
    # dependencies are all satisfied by the base columns + prior layers, so a
    # derived column that depends on another derived column lands one layer
    # later. Each layer becomes an outer CTE in _assemble_view_ddl.
    base_pc_names = [pc for pc in product_cols if pc not in deferred_derived]
    derived_layers = []
    derived_unresolved = []
    if deferred_derived:
        available = set(base_pc_names)
        remaining = dict(deferred_derived)
        while remaining:
            layer = []
            progressed = []
            for pc, info in remaining.items():
                if all(d in available for d in info["deps"]):
                    e = _substitute_product_aliases(info["expr"], info["deps"])
                    e = _apply_decorators(e, info["decorators"])
                    layer.append(f"{e} AS {_safe_name(pc)}")
                    progressed.append(pc)
            if not progressed:
                break  # dependency cycle / unresolved — fall back below
            for pc in progressed:
                available.add(pc)
                del remaining[pc]
            derived_layers.append(layer)
        # Cyclic or otherwise-unsatisfiable: don't silently drop the column —
        # emit it in the base SELECT (pre-fix behavior; the DDL may error, but
        # that reflects a genuinely broken spec). Done before lookup_joins is
        # materialised so a fallback lookup compile still injects its join.
        for pc in remaining:
            derived_unresolved.append(pc)
            expr = _compile_select_expr(
                pc, product_cols[pc], alias_lookup, lookup_aliases, lookup_meta,
                window_specs=window_specs,
                window_undefined_refs=window_undefined_refs,
                dialect=dialect,
                view_schema=view_schema,
            )
            select_parts.append(f"    {expr} AS {_safe_name(pc)}")

    lookup_joins = [_render_lookup_join_from_meta(meta, dialect=dialect) for meta in lookup_meta.values()]

    if not tables:
        # All mappings are literal — emit a SELECT with no FROM clause is
        # not portable. This is rare enough to fail rather than guess.
        raise ViewGenerationError(
            f"Output dataset {output_dataset_uri}: every mapping is literal — "
            "no source table to SELECT FROM."
        )

    # Fetch column lists for every in-scope table: mapped catalog/dprod
    # tables (we know their URIs from the mappings) plus any bridges that
    # `_resolve_bridges` might add inside `_build_from_fk_inferred`. We
    # don't yet know which bridges will be picked, so we fetch columns for
    # ALL FK-graph neighbours that could possibly become bridges — cheap
    # because FK edges are sparse. The result map is keyed by the same
    # `{schema}.{table}` convention used by `tables[]`.
    columns_by_key = {}
    if dataset_uris or dprod_dataset_uris:
        # Extend the URI lists with every possible bridge candidate's URI
        # from the fk_rows we already fetched. fk_rows carries from_uri /
        # to_uri (added when we widened the FK queries).
        all_catalog_uris = set(dataset_uris)
        all_dprod_uris = set(dprod_dataset_uris)
        for fk in fk_rows:
            for u in (fk.get("from_uri"), fk.get("to_uri")):
                if not u:
                    continue
                if u.startswith("dprod:ds:"):
                    all_dprod_uris.add(u)
                elif u.startswith("dataset:"):
                    all_catalog_uris.add(u)
        for r in session.run(
            TABLE_COLUMNS_QUERY,
            catalog_uris=list(all_catalog_uris),
            dprod_uris=list(all_dprod_uris),
        ):
            kind = r["kind"]
            name = r["name"]
            schema = r["schema"]
            if kind == "dprod":
                # Key by the SAME served-aware relation tables[] uses
                # (_resolve_source_relation → _dprod_relation): a --source-served-map
                # entry rewrites a dprod source to its REAL materialized relation
                # (e.g. `workspace.default.job_assignment_history`). Keying this map
                # by the co-located `vw_` fallback instead diverges from tables[] and
                # silently DROPS the SCD/temporal current-row dedup wrap in
                # _build_from_fk_inferred / _build_from_explicit (their `meta =
                # columns_by_key.get(join_key)` misses), fanning out history joins to
                # one row per version. Empty served-map ⇒ byte-identical `vw_` key.
                _cs, _ct = _dprod_relation(name, view_schema)
                key = f"{_cs}.{_ct}"
            else:
                key = f"{schema}.{name}"
            columns_by_key[key] = {
                "uri":               r["uri"],
                "column_names":      list(r["column_names"] or []),
                "description":       r.get("description") or "",
                "relationship_kind": r.get("relationship_kind") or "",
            }

    # Gap-B junction candidates: the CONSUMES'd products' OTHER output datasets
    # (not read by any mapping). Fetched only for the FK-inferred path with
    # dprod-kind sources — the explicit-joins path is engineer-authoritative
    # and catalog-only products bridge through the catalog FK graph instead.
    junction_candidates = []
    if not dt["joins"] and dprod_dataset_uris:
        for r in session.run(CONSUMED_DATASETS_QUERY, product_uri=product_uri,
                             exclude_uris=list(dprod_dataset_uris | dataset_uris)):
            phys = r["phys"]
            junction_candidates.append({
                "key":    f"{view_schema}.vw_{_safe_name(phys)}",
                "schema": view_schema,
                "table":  f"vw_{_safe_name(phys)}",
                "uri":    r["uri"],
                "phys":   phys,
                "relationship_kind":     r.get("relationship_kind") or "",
                "product_name":          r.get("product_name") or "",
                "src_deployment_status": r.get("src_deployment_status") or "pending",
                "cols":   list(r["cols"] or []),
            })

    # Build-time JOIN-column diagnostics (validation_unavailable / warnings),
    # folded into the summary + surfaced by ServingWarningsCallout.
    join_diagnostics: list = []
    if dt["joins"]:
        # Phase 4: explicit joins[] always wins. The engineer-declared list
        # is authoritative — FK discovery is bypassed entirely. Any source
        # table referenced by a mapping but missing from joins[] fails.
        from_clause, join_clauses, auto_satellite_dedup = _build_from_explicit(
            joins=dt["joins"], tables=tables, view_schema=view_schema,
            output_dataset_uri=output_dataset_uri, columns_by_key=columns_by_key,
            alias_relations=explicit_alias_relations, dialect=dialect,
            join_diagnostics=join_diagnostics,
        )
        auto_bridge_picks = []
    else:
        # Grain natural-key hint for the cross-product bridge: the column the
        # grain is "per". Best-effort from declared metadata (dedupe / grouping
        # keys); when it doesn't match a shared source column the bridge falls
        # back to any shared identity key. scd_policy drives current-vs-as-of.
        _nk = None
        if dt.get("dedupe") and dt["dedupe"].get("keys"):
            _nk = dt["dedupe"]["keys"][0]
        elif dt.get("grouping_keys"):
            _nk = dt["grouping_keys"][0]
        from_clause, join_clauses, auto_bridge_picks, auto_satellite_dedup = _build_from_fk_inferred(
            tables=tables, alias_lookup=alias_lookup, fk_rows=fk_rows,
            output_dataset_uri=output_dataset_uri,
            columns_by_key=columns_by_key, view_schema=view_schema,
            scd_policy=dt.get("scd_policy"), grain_natural_key=_nk,
            junction_candidates=junction_candidates, dialect=dialect,
            join_diagnostics=join_diagnostics,
        )

    # Build per-column aggregation info from the mappings. For a grouped
    # dataset, the renderer needs to know which product columns are grouping
    # keys (passthrough) vs aggregates (wrap in agg_fn()).
    agg_info = {}
    for pc_name, rows in product_cols.items():
        m = rows[0]
        agg_info[pc_name] = {
            "agg_fn":          (m.get("aggregate_function") or "").upper(),
            "is_grouping_key": bool(m.get("grouping_key")),
        }

    # Phase 5: SCD-1 ("latest_only") lowering. When the PO has declared
    # scd_policy={type: 'latest_only'} on the output dataset's schema-level
    # transform and has NOT also authored an explicit dedupe, synthesize a
    # dedupe block from the product's natural key + a temporal-column pick
    # on the product column names. The existing `deduped` CTE machinery in
    # `_assemble_view_ddl` then renders the ROW_NUMBER() OVER (PARTITION BY
    # <pk> ORDER BY <temporal> DESC) = 1 wrap, so latest_only and explicit
    # dedupe share one code path.
    #
    # Skipped silently when:
    #   - scd_policy is None or non-'latest_only' (e.g. 'snapshot', 'scd2'
    #     — out of scope today, surfaced raw on dt for later phases);
    #   - dedupe is already declared (engineer-authored wins);
    #   - the product has no isPrimaryKey columns (we can't derive a
    #     partition key — surfaced as a summary warning so the engineer
    #     knows to either set PKs or declare dedupe explicitly);
    #   - the product columns hold no detectable temporal column (no
    #     order_by to pick from — same warning surface).
    scd_synthesized = False
    scd_warning = None
    # latest_only satisfied structurally (every effective-dated satellite was
    # auto-deduped to its current row + current-state base) rather than via a
    # synthesized outer dedupe — so the "cannot be lowered" warning is a false
    # alarm and is suppressed.
    scd_satisfied_via_dedup = False
    # SCD-2 specific state: validated effective/expiration column names + the
    # add_is_current toggle. Populated only when scd_policy.type='scd2' AND
    # the named columns exist on the product. Empty / False otherwise.
    scd_effective_column = ""
    scd_expiration_column = ""
    scd_is_current_derived = False
    scd_policy = dt.get("scd_policy") or {}
    if (scd_policy.get("type") == "latest_only"
            and not dt.get("dedupe")):
        pk_cols = [
            pc for pc, rows in product_cols.items()
            if rows and rows[0].get("is_primary_key")
        ]
        product_col_names = list(product_cols.keys())
        order_col = _pick_temporal_order_column(product_col_names)
        if pk_cols and order_col:
            dt["dedupe"] = {
                "keys":      pk_cols,
                "order_by":  order_col,
                "direction": "desc",
            }
            scd_synthesized = True
        else:
            # Before warning, check whether latest_only is already satisfied
            # structurally: an outer dedupe can't be synthesized without a
            # product temporal column, but it isn't NEEDED when every
            # effective-dated satellite was auto-deduped to its current row
            # (Fix: `_satellite_needs_dedup`) AND the base/anchor table is
            # current-state (not itself effective-dated history). In that case
            # the view already yields one row per natural key, so the
            # "cannot be lowered" warning would be a false alarm.
            anchor_key = next(iter(tables), None)
            auto_deduped_keys = {d["table"] for d in (auto_satellite_dedup or [])}
            residual_history_satellites = [
                k for k in tables
                if k != anchor_key
                and k not in auto_deduped_keys
                and _satellite_needs_dedup(k, columns_by_key.get(k))
            ]
            anchor_is_history = _satellite_needs_dedup(
                anchor_key, columns_by_key.get(anchor_key)
            )
            if (pk_cols and not order_col
                    and not residual_history_satellites
                    and not anchor_is_history):
                scd_satisfied_via_dedup = True
            else:
                missing = []
                if not pk_cols:
                    missing.append("no product column flagged isPrimaryKey")
                if not order_col:
                    missing.append("no product column matches a temporal pattern")
                scd_warning = (
                    f"scd_policy.type='latest_only' declared but cannot be "
                    f"lowered: {', '.join(missing)}. Either flag a primary key "
                    f"and add a temporal column to the product schema, or "
                    f"declare :DatasetTransform.dedupe explicitly."
                )
    elif scd_policy.get("type") == "scd2":
        # SCD-2: preserve history. The PO declares which product columns
        # carry the validity period; view-DDL does NOT dedupe — the source's
        # multiple-rows-per-key shape passes through verbatim. We do three
        # things here:
        #   1. Validate effective_column / expiration_column exist on the
        #      product (warn + skip rendering work if missing).
        #   2. Warn if dedupe is also declared (semantic conflict — SCD-2
        #      preserves history; dedupe would discard it). The engineer's
        #      explicit dedupe wins (their override takes precedence over a
        #      contract-level declaration), but we surface the conflict.
        #   3. When add_is_current=true AND expiration_column exists, append
        #      a derived `is_current` column to SELECT — a portable boolean
        #      computed from the expiration column, useful for consumers
        #      filtering to current rows without parsing the contract.
        eff_col = (scd_policy.get("effective_column") or "").strip()
        exp_col = (scd_policy.get("expiration_column") or "").strip()
        add_is_current = bool(scd_policy.get("add_is_current"))
        missing = []
        if eff_col and eff_col not in product_cols:
            missing.append(f"effective_column={eff_col!r} not in product columns")
        if exp_col and exp_col not in product_cols:
            missing.append(f"expiration_column={exp_col!r} not in product columns")
        if not eff_col and not exp_col:
            missing.append("at least one of effective_column / expiration_column required")
        conflict = bool(dt.get("dedupe"))
        if missing or conflict:
            parts = []
            if missing:
                parts.append("; ".join(missing))
            if conflict:
                parts.append(
                    "explicit dedupe is also declared — view will dedupe "
                    "(engineer override wins), but this discards the history "
                    "SCD-2 is supposed to preserve"
                )
            scd_warning = "scd_policy.type='scd2': " + ". ".join(parts)
        # Record the validated columns even when partially missing — UI
        # surfaces them so the engineer sees what was declared vs not.
        if eff_col in product_cols:
            scd_effective_column = eff_col
        if exp_col in product_cols:
            scd_expiration_column = exp_col
        # Skip the derived append when the product already emits an
        # `is_current` column (the SCD-2 source table commonly carries its own
        # current-row flag, mapped through 1:1). Appending a second one yields
        # `column "is_current" specified more than once` in Postgres and breaks
        # the dbt snapshot compile. The existing mapped column already serves
        # the consumer-facing current-row predicate.
        _existing_aliases = {_safe_name(pc) for pc in product_cols}
        if "is_current" in _existing_aliases:
            add_is_current = False
        if add_is_current and scd_expiration_column:
            # Append a derived column AFTER the user's mapped SELECT exprs,
            # wrapped in `(... ) AS is_current` so it slots into the existing
            # comma-separated select_parts list. Reference the expiration
            # column's *source* expression (table-qualified), NOT the bare
            # output alias: Postgres can't reference a SELECT alias within the
            # same SELECT list, and when the FROM joins more than one
            # effective-dated table (the SCD-2 as-of bridge path adds a second
            # `effective_to`) a bare column name is ambiguous. Compiling the
            # source expr resolves it to the specific aliased column the
            # product's expiration column is mapped from. Portable across
            # PG / Snowflake / Databricks / BigQuery (CURRENT_TIMESTAMP is a
            # standard SQL construct).
            exp_mappings = product_cols.get(scd_expiration_column)
            if exp_mappings:
                exp_ref = _compile_select_expr(
                    scd_expiration_column, exp_mappings, alias_lookup,
                    lookup_aliases, lookup_meta,
                    window_specs=window_specs,
                    window_undefined_refs=window_undefined_refs,
                    dialect=dialect,
                    view_schema=view_schema,
                )
            else:
                exp_ref = _safe_name(scd_expiration_column)
            is_current_expr = (
                f"    (CASE WHEN {exp_ref} IS NULL OR {exp_ref} > CURRENT_TIMESTAMP "
                f"THEN true ELSE false END) AS is_current"
            )
            select_parts.append(is_current_expr)
            scd_is_current_derived = True

    # Phase 5 snapshot SCD lowering. When the contract declares
    # scd_policy={type: 'snapshot', snapshot_column, as_of_date}, synthesize a
    # base-CTE filter pinning the view to one point-in-time. Unlike SCD-1
    # which dedupes, snapshot keeps every row that matches the as-of-date —
    # the assumption is the source carries full snapshots tagged by
    # snapshot_column (e.g. daily full reloads), and the engineer wants one
    # frozen-in-time slice.
    #
    # Implementation: the filter goes in the existing dt['filter'] slot so
    # _assemble_view_ddl's WHERE clause emits it without new machinery.
    # When the engineer already has a filter declared, we AND the two
    # together so both narrowings compose.
    #
    # The filter references the SOURCE column (NOT the product AS alias),
    # because WHERE evaluates before SELECT aliases are computed in standard
    # SQL. We look up the mapping's source_schema/table/col for the named
    # product column and emit `<alias>.<source_col> = '<as_of_date>'`.
    #
    # Validation surfaces in scd_warning:
    #   - missing snapshot_column declaration
    #   - missing as_of_date declaration
    #   - snapshot_column doesn't name a real product column
    #   - product column is literal / has no source mapping (degenerate)
    scd_snapshot_column = ""
    scd_snapshot_as_of = ""
    scd_synthesized_filter = False
    if scd_policy.get("type") == "snapshot":
        snap_col = (scd_policy.get("snapshot_column") or "").strip()
        as_of = scd_policy.get("as_of_date")
        as_of_str = "" if as_of is None else str(as_of).strip()
        missing = []
        if not snap_col:
            missing.append("snapshot_column required")
        if not as_of_str:
            missing.append("as_of_date required")
        if snap_col and snap_col not in product_cols:
            missing.append(f"snapshot_column={snap_col!r} not in product columns")
        # If everything resolved, look up the source for the product column.
        snapshot_filter = ""
        if not missing and snap_col in product_cols:
            row0 = (product_cols[snap_col] or [{}])[0]
            schema_ = row0.get("source_schema")
            table_ = row0.get("source_table")
            src_col = _source_col_ref(row0)
            if not (schema_ and table_ and src_col):
                missing.append(
                    f"snapshot_column={snap_col!r} has no source mapping "
                    f"(literal-kind columns can't anchor a snapshot)"
                )
            else:
                tkey = f"{schema_}.{table_}"
                alias = alias_lookup.get(tkey, "?")
                snapshot_filter = f"{alias}.{src_col} = {_quote_sql_string(as_of_str)}"
        if missing:
            scd_warning = "scd_policy.type='snapshot': " + "; ".join(missing)
        elif snapshot_filter:
            # AND together with any engineer-declared filter so narrowings
            # compose rather than the engineer's filter silently winning.
            existing = (dt.get("filter") or "").strip()
            dt["filter"] = f"({existing}) AND ({snapshot_filter})" if existing else snapshot_filter
            scd_snapshot_column = snap_col
            scd_snapshot_as_of = as_of_str
            scd_synthesized_filter = True

    ddl, used_filter, used_dedupe, used_grouping, underspecified, select_body = _assemble_view_ddl(
        view_schema=view_schema,
        view_name=view_name,
        select_parts=select_parts,
        from_clause=from_clause,
        join_clauses=join_clauses,
        lookup_joins=lookup_joins,
        # Inner CTE carry-through (dedupe / grouping) operates on base columns
        # only — deferred derived columns aren't in the base SELECT.
        product_col_names=base_pc_names,
        filter_predicate=dt["filter"],
        filter_intent=dt.get("filter_intent", ""),
        dedupe=dt["dedupe"],
        grouping_keys=dt["grouping_keys"],
        agg_info=agg_info,
        derived_layers=derived_layers,
        dialect=dialect,
    )

    # Multiplication-risk warnings: walk the originally-mapped tables (NOT
    # bridges, which we either wrap as temporal or leave plain) and flag
    # any with detectable temporal columns. Bridges that auto-wrapped
    # already handle their own row-multiplication via ROW_NUMBER = 1; the
    # warning targets the FROM root and other mapped joined tables which
    # the system can't safely auto-dedupe. The engineer should declare
    # :DatasetTransform.dedupe to pick one row per natural key.
    multiplication_warnings = []
    bridge_keys_in_tables = {
        f"{p.get('bridge_key')}" for p in auto_bridge_picks if p.get("bridge_key")
    }
    # Mapped satellites we already wrapped in a current-row dedup at join time
    # (Fix: `_satellite_needs_dedup`) — no fan-out risk remains, so don't also
    # warn about them; they're reported informationally via auto_satellite_dedup.
    auto_deduped_keys = {d["table"] for d in (auto_satellite_dedup or [])}
    dedupe_active = bool(dt.get("dedupe"))
    for key, info in tables.items():
        if key in bridge_keys_in_tables:
            continue  # bridge already handled
        if key in auto_deduped_keys:
            continue  # satellite already deduped to current row at join time
        meta = columns_by_key.get(key) or {}
        cols = meta.get("column_names") or []
        temporal_col = _pick_temporal_order_column(cols)
        if not temporal_col:
            continue
        if dedupe_active:
            # Engineer already declared dedupe; assume they're aware.
            # (Approximate: any dedupe declared → suppress; refine if it
            # causes false-suppression in practice.)
            continue
        # Don't cry wolf on source mirrors that are one-row-per-key by
        # construction. The PO-approved relationshipKind tells us when a
        # temporal column is benign: `audit_log` datasets are surrogate-keyed
        # change logs (many rows per natural key IS the intent), and `fact`
        # datasets sit at their own grain. Likewise a plain audit timestamp
        # (updated_at/created_at) marks a current-state row, not history.
        # Keep the warning for dimension-style tables (lookup_dimension /
        # general_membership / specialization) with genuine effective-dating
        # and no dedupe — the real fan-out risk.
        rkind = (meta.get("relationship_kind") or "").lower()
        if rkind in ("audit_log", "fact"):
            continue
        if temporal_col.lower() in _AUDIT_TS_COLS:
            continue
        multiplication_warnings.append({
            "table":           key,
            "temporal_column": temporal_col,
            "suggestion": (
                f"Source `{key}` has a temporal column (`{temporal_col}`) "
                f"suggesting history-style data. Without "
                f":DatasetTransform.dedupe on this output dataset's schema, "
                f"the view may yield multiple rows per natural key. "
                f"Declare dedupe to pick one row per key, e.g.: "
                f"dedupe: {{keys: [<natural_key>], "
                f"order_by: {temporal_col}, direction: desc}}."
            ),
        })

    # Chained-bridge grain advisory: the fact/audit_log exemption above was
    # written for tables at their OWN grain. When a cross-product chain routes
    # ANOTHER table THROUGH an intermediate, the chained rows arrive at the
    # intermediate's grain. Two flavours, both informational:
    #   - via a raw mapped ledger-grain table (fact / audit_log): many rows
    #     per entity unless dedupe / grouping is declared;
    #   - via a DISTINCT-projected junction: fan-out-safe for 1:1 links, but
    #     a genuinely M:N junction (joint accounts) still multiplies.
    # The junction entry itself and anchor-adjacent hops are exempt (the
    # junction is deduped by construction; the anchor defines the grain).
    if not dedupe_active and not dt.get("grouping_keys"):
        _junction_tbls = {
            b["table"] for b in (auto_satellite_dedup or [])
            if b.get("mode") == "junction_distinct"
        }
        _anchor_tbl_key = next(iter(tables), None)
        _ledger_keys = {
            key for key, meta_ in (
                (k, columns_by_key.get(k) or {}) for k in tables
            ) if (meta_.get("relationship_kind") or "").lower() in ("fact", "audit_log")
            and key not in _junction_tbls
        }
        for b in (auto_satellite_dedup or []):
            via = b.get("via")
            if (not b.get("cross_product_bridge")
                    or b.get("mode") == "junction_distinct"
                    or not via or via == _anchor_tbl_key):
                continue
            if via in _junction_tbls:
                multiplication_warnings.append({
                    "table":           b["table"],
                    "temporal_column": None,
                    "suggestion": (
                        f"`{b['table']}` joins through the DISTINCT-projected "
                        f"junction `{via}` on `{b.get('key')}` — safe for 1:1 "
                        f"links, but if the junction relates one anchor row to "
                        f"MANY `{b['table']}` rows (M:N), rows multiply. If the "
                        f"product grain is one row per key, declare "
                        f":DatasetTransform.dedupe or grouping_keys."
                    ),
                })
            elif via in _ledger_keys:
                multiplication_warnings.append({
                    "table":           b["table"],
                    "temporal_column": None,
                    "suggestion": (
                        f"`{b['table']}` is chained into the view through "
                        f"ledger-grain table `{via}` on `{b.get('key')}` — the "
                        f"joined rows sit at `{via}`'s grain (many rows per "
                        f"entity). If the product grain is one row per key, "
                        f"declare :DatasetTransform.dedupe or grouping_keys."
                    ),
                })

    # Semantic guard: a column NAMED like a band / grade / tier / bracket but
    # whose transform carries NO bucketing logic (no CASE, no `bucket` kind)
    # passes its source value through verbatim. When that source is a raw
    # numeric — e.g. a salary amount looked up for a "compensation_band"
    # column — the view EXPOSES the raw value under a column that claims to be
    # a discrete band, leaking sensitive data and contradicting the declared
    # (often short-string) type. Surfaced as a warning so the mapping is
    # corrected to bucket the value; the generator can't invent band
    # boundaries (domain knowledge) so it cannot auto-fix.
    semantic_warnings = []
    for pc_name, rows in product_cols.items():
        low = pc_name.lower()
        if not any(tok in low for tok in _BAND_NAME_TOKENS):
            continue
        m0 = rows[0]
        kind = (m0.get("transform_kind") or "").lower()
        expr = (m0.get("transform_expression") or "")
        params = _parse_json(m0.get("transform_params_json"), {})
        expr_low = expr.lower()
        has_banding = (
            kind == "bucket"
            or ("case" in expr_low and "when" in expr_low)
            or bool(params.get("buckets") or params.get("boundaries")
                    or params.get("bins") or params.get("thresholds"))
        )
        if has_banding:
            continue
        semantic_warnings.append({
            "column":         pc_name,
            "issue":          "band_name_without_bucketing",
            "transform_kind": kind or "direct",
            "suggestion": (
                f"Product column `{pc_name}` is named like a band/grade/tier "
                f"but its transform contains no bucketing logic (no CASE / no "
                f"`bucket` kind) — it passes the source value through verbatim. "
                f"If the source is a raw numeric (e.g. a salary amount), the "
                f"view will EXPOSE that raw value under a column that claims to "
                f"be a band, leaking sensitive data and contradicting the "
                f"declared type. Author a bucketing CASE in transformExpression "
                f"(or use the `bucket` transform kind) so the column emits the "
                f"discrete grade, and confirm the column's sensitivity "
                f"classification reflects the underlying data."
            ),
        })

    summary = {
        "view_name": f"{view_schema}.{view_name}",
        "output_dataset_uri": output_dataset_uri,
        "product_columns": len(product_cols),
        # Phase 3 transform-portability: per-dataset capability findings
        # (errors/warnings/used_capabilities). `validated=False` means validation
        # was skipped (non-served dialect or backend not importable), NOT "clean".
        "transform_diagnostics": transform_diagnostics,
        # Build-time JOIN-column validation: warn-level `join_predicate_warning`
        # (explicit joins) + `validation_unavailable` (metadata missing) entries.
        # A proven-absent FK column hard-fails before reaching here.
        "join_diagnostics": join_diagnostics,
        # PK product columns as their physical SELECT aliases — the dbt snapshot
        # emitter (scd2 materialization) uses these as `unique_key`.
        "primary_key_columns": [
            _safe_name(pc) for pc, rows in product_cols.items()
            if rows and rows[0].get("is_primary_key")
        ],
        "source_tables": len(tables),
        "joins": len(join_clauses),
        "lookup_joins": len(lookup_joins),
        "tables": [f"{v['schema']}.{v['table']}" for v in tables.values()],
        "filter": used_filter,
        "dedupe": used_dedupe,
        "grouping": used_grouping,
        "explicit_joins": bool(dt["joins"]),
        "underspecified_aggregates": underspecified,
        "auto_bridge_choice": auto_bridge_picks,
        # Mapped SCD satellite tables auto-wrapped in a current-row dedup at
        # join time so a lookup/passthrough off them can't fan the view out.
        "auto_satellite_dedup": auto_satellite_dedup,
        "multiplication_warnings": multiplication_warnings,
        # Band/grade/tier columns whose transform does no bucketing — likely
        # leaking a raw (sensitive) source value under a band-named column.
        "semantic_warnings": semantic_warnings,
        # Phase 5 SCD-1: scd_policy_type carries whatever was declared on the
        # transform ('latest_only', 'snapshot', 'scd2', ...) for downstream
        # surfaces; scd_synthesized_dedupe records whether the
        # 'latest_only' lowering actually fired (False when scd_policy isn't
        # set, when explicit dedupe pre-empted, or when synthesis was blocked
        # — see scd_warning). scd_warning carries the human-readable reason
        # when latest_only was declared but couldn't be lowered.
        "scd_policy_type":         (dt.get("scd_policy") or {}).get("type") or "",
        "scd_synthesized_dedupe":  scd_synthesized,
        "scd_satisfied_via_satellite_dedup": scd_satisfied_via_dedup,
        "scd_warning":             scd_warning,
        # SCD-2 only: which product columns the policy named for validity,
        # whether a derived is_current column was appended. Empty / False
        # for non-scd2 policies.
        "scd_effective_column":    scd_effective_column,
        "scd_expiration_column":   scd_expiration_column,
        "scd_is_current_derived":  scd_is_current_derived,
        # Snapshot SCD: which column pins the snapshot, what date was
        # selected, whether the filter was successfully synthesized. Empty /
        # False for non-snapshot policies.
        "scd_snapshot_column":     scd_snapshot_column,
        "scd_snapshot_as_of":      scd_snapshot_as_of,
        "scd_synthesized_filter":  scd_synthesized_filter,
        # Phase 5 suppressedColumns: surfaced verbatim so the UI can render an
        # info chip + the engineer can spot typos via `suppressed_unknown`.
        # `suppressed_pks_skipped` lists declared PK suppressions that we
        # refused (PK removal would break the contract — see filter logic).
        "suppressed_columns":      suppressed_applied,
        "suppressed_unknown":      suppressed_unknown,
        "suppressed_pks_skipped":  suppressed_pks_skipped,
        # Phase 6 window functions: which named windows the mappings ended
        # up referencing, and which references didn't resolve to a declared
        # spec (typo / stale name — view-DDL fell back to degenerate
        # `OVER ()` so the SQL parses, but the result is almost certainly
        # not what the engineer wanted).
        "window_specs_used":       sorted(window_specs_used),
        "window_specs_undefined":  sorted(window_undefined_refs),
        # Derived-on-derived columns deferred to outer enriched CTE layer(s)
        # (each references other product columns, not source columns).
        # `derived_enriched_columns` lists them; `derived_enriched_layers` is
        # the layer count (>1 only for dependency chains);
        # `derived_unresolved` lists any with cyclic/unsatisfiable deps that
        # fell back to the base SELECT.
        "derived_enriched_columns": sorted(deferred_derived.keys()),
        "derived_enriched_layers":  len(derived_layers),
        "derived_unresolved":       sorted(derived_unresolved),
    }

    # Prepend audit comments naming each auto-picked bridge + temporal-bridge
    # behavior + non-bridge multiplication warnings, so the engineer sees
    # every non-obvious choice in the SQL artifact itself. The
    # `store_serving_definition.py` view-name extractor anchors to start-of-
    # line, so these `--` comments don't get scooped as fake CREATE VIEW.
    header_lines = []
    for p in auto_bridge_picks:
        header_lines.append(
            f"-- auto-bridge: {' → '.join(p['path'])}  ({p['rationale']})"
        )
        if p.get("temporal_column"):
            header_lines.append(
                f"-- temporal bridge: {p['bridge_key']} uses latest-row "
                f"semantics ordered by {p['temporal_column']} DESC"
            )
    for w in multiplication_warnings:
        if w.get("temporal_column"):
            header_lines.append(
                f"-- warning: {w['table']} has temporal column ({w['temporal_column']}) "
                f"and no :DatasetTransform.dedupe — rows may multiply per natural key"
            )
        else:
            # Chained-bridge grain advisory (no temporal column involved) —
            # the suggestion text carries the flavour (ledger-grain vs M:N).
            header_lines.append(f"-- warning: {w['suggestion']}")
    for d in auto_satellite_dedup:
        mode = d.get("mode")
        if mode == "as_of":
            # SCD-2 AS-OF join: no current-row collapse — the satellite row is
            # picked by validity window against the anchor pivot.
            _eff_to = d.get("effective_to") or "(open-ended)"
            header_lines.append(
                f"-- scd-2 as-of: {d['table']} joined to {d.get('pivot')} "
                f"(effective_from={d.get('effective_from')}, "
                f"effective_to={_eff_to}) — period-correct history, no fan-out"
            )
            continue
        if mode == "junction_distinct":
            header_lines.append(
                f"-- junction bridge: {d['table']} "
                f"(SELECT DISTINCT {', '.join(d.get('keys') or [])}) linking "
                f"via {d.get('via')} — pure join-only bridge, fan-out-safe"
            )
            continue
        if mode in ("equi", "current_row"):
            # Cross-product natural-key bridge (chained). These entries carry
            # key/via, not the satellite partition/temporal fields.
            _via = f" via {d['via']}" if d.get("via") else ""
            _latest = " (latest row per key)" if mode == "current_row" else ""
            header_lines.append(
                f"-- cross-product bridge: {d['table']} joined on "
                f"{d.get('key')}{_via}{_latest}"
            )
            continue
        _icf = f", WHERE {d['is_current_filter']} = true" if d.get("is_current_filter") else ""
        header_lines.append(
            f"-- scd: {d['table']} auto-deduped to current row "
            f"(PARTITION BY {d['partition_column']} ORDER BY "
            f"{d['temporal_column']} DESC{_icf}) — prevents per-key fan-out"
        )
    for w in semantic_warnings:
        header_lines.append(
            f"-- warning: column '{w['column']}' is named like a band/grade but "
            f"its transform does no bucketing — may expose a raw (sensitive) "
            f"value; author a bucketing CASE / `bucket` kind"
        )
    if scd_synthesized:
        synthesized = dt["dedupe"]
        header_lines.append(
            f"-- scd-1: latest-row semantics synthesized from "
            f"scd_policy.type='latest_only' — PARTITION BY "
            f"({', '.join(synthesized['keys'])}) ORDER BY "
            f"{synthesized['order_by']} {synthesized['direction'].upper()}"
        )
    if scd_effective_column or scd_expiration_column:
        parts = []
        if scd_effective_column:
            parts.append(f"effective_column={scd_effective_column}")
        if scd_expiration_column:
            parts.append(f"expiration_column={scd_expiration_column}")
        if scd_is_current_derived:
            parts.append("is_current=<derived>")
        header_lines.append(
            f"-- scd-2: history preserved; " + ", ".join(parts)
        )
    if scd_synthesized_filter:
        header_lines.append(
            f"-- scd-snapshot: view pinned to "
            f"{scd_snapshot_column} = '{scd_snapshot_as_of}' "
            f"(synthesized from scd_policy.type='snapshot')"
        )
    if scd_warning:
        header_lines.append(f"-- warning: {scd_warning}")
    if suppressed_applied:
        header_lines.append(
            f"-- suppressed: {', '.join(suppressed_applied)} "
            f"(removed from SELECT per :DatasetTransform.suppressedColumns)"
        )
    if suppressed_pks_skipped:
        header_lines.append(
            f"-- warning: refused to suppress primary-key column(s) "
            f"{', '.join(suppressed_pks_skipped)} — would break the contract"
        )
    if suppressed_unknown:
        header_lines.append(
            f"-- warning: :DatasetTransform.suppressedColumns listed "
            f"unknown product column(s): {', '.join(suppressed_unknown)} "
            f"(likely typo)"
        )
    for win_name in sorted(window_specs_used):
        spec = window_specs.get(win_name) or {}
        parts = []
        if spec.get("partition_by"):
            parts.append(f"PARTITION BY ({', '.join(spec['partition_by'])})")
        if spec.get("order_by"):
            order_strs = [f"{ob['column']} {ob['direction'].upper()}" for ob in spec["order_by"]]
            parts.append(f"ORDER BY ({', '.join(order_strs)})")
        if spec.get("frame"):
            parts.append(f"FRAME ({spec['frame']})")
        header_lines.append(
            f"-- window: {win_name} = " + (" ".join(parts) if parts else "OVER ()")
        )
    for win_name in sorted(window_undefined_refs):
        header_lines.append(
            f"-- warning: window '{win_name}' referenced by a mapping but "
            f"not declared in :DatasetTransform.window_specs — emitted as "
            f"degenerate OVER () (likely typo or missing declaration)"
        )
    if deferred_derived:
        for pc, info in sorted(deferred_derived.items()):
            header_lines.append(
                f"-- enriched: {pc} = {info['expr']} computed in an outer CTE "
                f"layer (depends on product column(s) "
                f"{', '.join(info['deps'])}, not source columns)"
            )
    for pc in sorted(derived_unresolved):
        header_lines.append(
            f"-- warning: derived column {pc} has cyclic/unresolved product-"
            f"column dependencies — emitted in base SELECT (may not deploy)"
        )
    if header_lines:
        ddl = "\n".join(header_lines) + "\n" + ddl

    return ddl, f"{view_schema}.{view_name}", summary, select_body


def _parse_dataset_transform(dt_row):
    """Decode the optional :DatasetTransform Neo4j node into a dict.

    Returns a dict with the active-phase fields populated:
      filter             (Phase 2): str raw SQL WHERE fragment or ''
      dedupe             (Phase 2): {keys, order_by, direction} or None
      grouping_keys      (Phase 3): list of product-column names or []
      joins              (Phase 4): list of {alias, dataset_uri, kind, predicate}
                                    or [] (empty → FK-driven fallback)
      scd_policy         (Phase 5): {type: 'latest_only'|'snapshot'|'scd2', ...}
                                    or None. Only 'latest_only' is rendered
                                    today (lowered to a synthetic dedupe in
                                    `_generate_ddl_for_dataset`); other types
                                    are surfaced raw for future phases.
      suppressed_columns (Phase 5): list of product-column names to drop from
                                    the final SELECT. The column still exists
                                    in the contract / graph (lineage,
                                    documentation, mapping) but the
                                    materialized view doesn't project it —
                                    primary use case is PII / sensitive
                                    data the consumer shouldn't see in the
                                    served view. Empty list = no
                                    suppression.
      window_specs       (Phase 6): dict of name → spec. Each spec carries
                                    `partition_by` (list of column refs),
                                    `order_by` (list of {column, direction}),
                                    and optional `frame` (raw SQL frame
                                    clause). Column-level transforms with
                                    `kind='window'` reference these by name
                                    to emit window-function SELECT
                                    expressions. Empty dict = no windows
                                    declared.
    """
    empty = {
        "filter": "", "filter_intent": "", "dedupe": None, "grouping_keys": [],
        "joins": [], "scd_policy": None, "suppressed_columns": [],
        "window_specs": {},
    }
    if not dt_row:
        return empty
    dt = dict(dt_row["dt"])

    filter_predicate = (dt.get("filterPredicate") or "").strip()
    # The PO's plain-language row filter. The generator NEVER emits it — it's
    # only used to fail loud when an intent was declared but never compiled to
    # a real predicate (see the gate in _assemble_view_ddl).
    filter_intent = (dt.get("filterIntent") or "").strip()

    dedupe_raw = _parse_json(dt.get("dedupeJson"), {})
    dedupe = None
    if isinstance(dedupe_raw, dict) and dedupe_raw.get("keys"):
        direction = (dedupe_raw.get("direction") or "desc").lower()
        if direction not in ("asc", "desc"):
            direction = "desc"
        dedupe = {
            "keys":      list(dedupe_raw.get("keys") or []),
            "order_by":  dedupe_raw.get("order_by") or "",
            "direction": direction,
        }

    grouping_keys = _parse_json(dt.get("groupingKeysJson"), [])
    if not isinstance(grouping_keys, list):
        grouping_keys = []

    joins = _parse_json(dt.get("joinsJson"), [])
    if not isinstance(joins, list):
        joins = []

    scd_policy_raw = _parse_json(dt.get("scdPolicyJson"), {})
    scd_policy = None
    if isinstance(scd_policy_raw, dict) and scd_policy_raw.get("type"):
        scd_policy = dict(scd_policy_raw)

    suppressed_columns = _parse_json(dt.get("suppressedColumnsJson"), [])
    if not isinstance(suppressed_columns, list):
        suppressed_columns = []
    # Keep names case-preserved; filter to non-empty strings to avoid passing
    # garbage shapes into the suppression set.
    suppressed_columns = [s for s in suppressed_columns if isinstance(s, str) and s]

    # Phase 6: window_specs is a dict name → spec. Each spec normalised to
    # {partition_by: [...], order_by: [{column, direction}], frame: str}.
    # Garbage shapes get pruned silently so view-DDL only sees valid specs.
    window_specs_raw = _parse_json(dt.get("windowSpecsJson"), {})
    window_specs = {}
    if isinstance(window_specs_raw, dict):
        for nm, spec in window_specs_raw.items():
            if not isinstance(nm, str) or not nm or not isinstance(spec, dict):
                continue
            partition = spec.get("partition_by") or []
            if not isinstance(partition, list):
                partition = []
            partition = [str(p) for p in partition if isinstance(p, str) and p]
            order = spec.get("order_by") or []
            if not isinstance(order, list):
                order = []
            cleaned_order = []
            for ob in order:
                if isinstance(ob, dict) and ob.get("column"):
                    direction = (ob.get("direction") or "asc").lower()
                    if direction not in ("asc", "desc"):
                        direction = "asc"
                    cleaned_order.append({"column": str(ob["column"]), "direction": direction})
            frame = spec.get("frame")
            if not isinstance(frame, str):
                frame = ""
            window_specs[nm] = {
                "partition_by": partition,
                "order_by":     cleaned_order,
                "frame":        frame.strip(),
            }

    return {
        "filter":             filter_predicate,
        "filter_intent":      filter_intent,
        "dedupe":             dedupe,
        "grouping_keys":      grouping_keys,
        "joins":              joins,
        "scd_policy":         scd_policy,
        "suppressed_columns": suppressed_columns,
        "window_specs":       window_specs,
    }


_AS_ALIAS_RE = re.compile(r"\bAS\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
_REL_SCHEMA_RE = re.compile(r"(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)\.", re.IGNORECASE)
_QUALIFIER_RE = re.compile(r"""(?<![\w."'])([a-z_][a-z0-9_]*)\.""", re.IGNORECASE)


def _validate_alias_resolution(select_parts, from_clause, join_clauses,
                               lookup_joins, view_schema):
    """Generation-time gate: every table-qualified identifier in the projected
    expressions must resolve to an alias / CTE / schema that's actually in the
    query scope. Catches a transform expression that kept a raw source-table
    name (e.g. ``CAST(job.grade AS varchar(20))`` where ``job`` is joined
    ``AS t2``) — statically invalid SQL that would otherwise be marked complete
    and only blow up at deploy with ``sql_error``. Raises ViewGenerationError so
    the generator refuses to emit (and persist) a broken view.
    """
    clause_text = " ".join(
        [from_clause or ""] + list(join_clauses or []) + list(lookup_joins or [])
    )
    valid = {a.lower() for a in _AS_ALIAS_RE.findall(clause_text)}        # t1, t2, lk1 …
    valid.update(s.lower() for s in _REL_SCHEMA_RE.findall(clause_text))  # source/view schemas
    valid.update({"base", "deduped", "grouped"})                         # CTE layers
    if view_schema:
        valid.add(view_schema.lower())

    offending = set()
    for part in select_parts:
        scrubbed = re.sub(r"'[^']*'", "''", part)  # ignore string literals
        for q in _QUALIFIER_RE.findall(scrubbed):
            if q.lower() not in valid:
                offending.add(q)
    if offending:
        raise ViewGenerationError(
            f"view generation produced unresolved table reference(s) "
            f"{sorted(offending)} in the SELECT — none of the query's "
            f"aliases/CTEs/schemas {sorted(valid)}. This usually means a "
            f"transform expression kept a raw source-table name instead of the "
            f"join alias; the view would fail to deploy. Fix the mapping's "
            f"transform to use the join alias (or re-run Mapping & Transformation)."
        )


_SQL_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def _output_alias_expr_map(select_parts):
    """Map each projected AS-alias → its underlying SQL expression, parsed from
    the rendered ``<expr> AS <alias>`` select parts. The projection alias is the
    LAST top-level ` AS `, so an inner ` AS ` (e.g. ``CAST(x AS INT) AS foo``)
    doesn't confuse the split."""
    out = {}
    for p in select_parts or []:
        s = (p or "").strip()
        m = re.search(r"\s+AS\s+([A-Za-z_][\w$]*)\s*$", s, re.IGNORECASE)
        if not m:
            continue
        alias = m.group(1)
        expr = s[:m.start()].strip()
        if alias and expr:
            out[alias] = expr
    return out


def _rewrite_predicate_output_cols(predicate, alias_expr):
    """Rewrite a base-CTE WHERE predicate authored against product OUTPUT column
    names so it references the underlying SOURCE expressions instead.

    A base-CTE ``WHERE`` evaluates BEFORE the SELECT's own aliases exist, so a
    filter like ``employment_status = 'active'`` (an AS-alias of
    ``t1.hr_employment_status``) is unresolvable on Spark/Databricks and only
    resolves on Postgres by COINCIDENCE when a same-named source column happens
    to exist. This inlines each output-column reference with its source
    expression — the same discipline the snapshot-SCD path already follows
    (see the comment near the snapshot filter synthesis). Substitution is
    whole-word, string-literal-safe, and skips already-qualified refs (``a.b``)
    so a hand-written source predicate is left untouched. Empty map / no matches
    ⇒ the predicate is returned byte-for-byte."""
    if not predicate or not alias_expr:
        return predicate
    # Split into code vs single-quoted string-literal segments; only rewrite code.
    segments, last = [], 0
    for m in _SQL_STRING_LITERAL_RE.finditer(predicate):
        segments.append(("code", predicate[last:m.start()]))
        segments.append(("str", m.group(0)))
        last = m.end()
    segments.append(("code", predicate[last:]))
    # Longest alias first so a shorter name can't pre-empt a longer overlap.
    aliases = sorted(alias_expr, key=len, reverse=True)

    def _sub(seg):
        for a in aliases:
            # Whole-word: not preceded/followed by a word char or '.', so a
            # qualified `t1.<a>` or a longer identifier (`<a>_x`) is left alone.
            seg = re.sub(rf"(?<![\w.]){re.escape(a)}(?![\w.])",
                         lambda _m, _e=alias_expr[a]: f"({_e})", seg)
        return seg

    return "".join(_sub(t) if kind == "code" else t for kind, t in segments)


def _assemble_view_ddl(*, view_schema, view_name, select_parts, from_clause,
                       join_clauses, lookup_joins, product_col_names,
                       filter_predicate, dedupe, grouping_keys, agg_info,
                       derived_layers=None, filter_intent="", dialect=None):
    """Assemble the final CREATE VIEW DDL.

    When no dataset-level shape is set, emits today's flat form:
        CREATE VIEW ... AS SELECT <select_parts> FROM <from_clause> [joins] [lookup_joins];

    CTE layers activate independently based on which fields :DatasetTransform
    carries (Phase 2-4 design §3.3 + Phase 3 GROUP BY extension):

        CREATE VIEW ... AS
        WITH base AS (                                      -- always present when any CTE is used
          SELECT <select_parts> FROM <from> [joins] [lookup_joins] [WHERE <filter>]
        )
        [, deduped AS (                                     -- Phase 2: dedupe set
          SELECT <cols> FROM (SELECT <cols>, ROW_NUMBER() OVER (PARTITION BY <keys>
                                                               ORDER BY <order_by> <dir>) AS _rn
                              FROM base) t WHERE _rn = 1
        )]
        [, grouped AS (                                     -- Phase 3: grouping_keys set
          SELECT <gk_cols>, <agg_fn>(<col>) AS <col>, ... FROM <base|deduped> GROUP BY <gk_cols>
        )]
        SELECT * FROM <base|deduped|grouped>;

    grouping_keys = list of product-column names declared as the GROUP BY
    keys. agg_info = {product_col_name: {agg_fn, is_grouping_key}}. A product
    column referenced in grouping_keys is passed through; other columns are
    wrapped in `agg_fn(<col>)` — defaulting to MAX() when agg_fn is empty
    (lets the view compile but flags an under-specified mapping).

    Returns (ddl, used_filter_bool, used_dedupe_bool, used_grouping_bool,
    underspecified, select_body). `select_body` is the pure SELECT/CTE text
    with no `CREATE VIEW … AS` wrapper and no trailing `;` — the dbt model
    emitter (`generate_dbt_models`) reuses it verbatim as a model body so the
    transform DSL is compiled in exactly one place.
    """
    # Per-dialect CREATE header (quoted qualified name + platform view options
    # e.g. Snowflake COPY GRANTS). Defaults to the portable ANSI form so a
    # dialect-less call (legacy / tests) still emits a valid double-quoted header.
    dialect = dialect or Dialect()

    # Self-check the projected expressions resolve to in-scope aliases before
    # we emit anything (defense-in-depth backstop to the alias rewrite).
    _validate_alias_resolution(select_parts, from_clause, join_clauses,
                               lookup_joins, view_schema)

    # Fail loud if the PO declared a row filter that was never compiled to SQL.
    # Normally the serving stage auto-compiles it (the filterIntent → predicate
    # pre-run hook); this backstop catches any path that bypassed that (e.g. a
    # direct generator run) so a declared filter can never silently ship as
    # "all rows".
    if filter_intent and not filter_predicate:
        raise ViewGenerationError(
            f"This product declares a row filter ({filter_intent!r}) that hasn't been "
            "compiled to a SQL condition yet. It is normally auto-compiled when you run the "
            "serving stage; if you're seeing this, finalize the filter in the Filter Review "
            "panel (or via set_dataset_filter) before serving."
        )

    if filter_predicate and _filter_looks_like_prose(filter_predicate):
        raise ViewGenerationError(
            f"The dataset filter still reads like plain language and isn't valid SQL: "
            f"{filter_predicate!r}. It was never interpreted into a real condition — open "
            "the filter review (or re-run “Check filter” in the wizard) to ground it to a "
            "column/value before serving."
        )

    has_filter = bool(filter_predicate)
    has_dedupe = bool(dedupe)
    has_grouping = bool(grouping_keys)
    has_derived = bool(derived_layers)

    if not has_filter and not has_dedupe and not has_grouping and not has_derived:
        body_parts = [
            "SELECT",
            ",\n".join(select_parts),
            "FROM",
            from_clause,
        ]
        if join_clauses:
            body_parts.extend(join_clauses)
        if lookup_joins:
            body_parts.extend(lookup_joins)
        select_body = "\n".join(body_parts)
        ddl = f"{dialect.view_header(view_schema, view_name)} AS\n{select_body};"
        return ddl, False, False, False, [], select_body

    # CTE form. base CTE carries the full SELECT + joins + filter.
    base_parts = ["    SELECT", ",\n".join(f"    {p.lstrip()}" for p in select_parts), "    FROM", f"    {from_clause.lstrip()}"]
    for j in join_clauses:
        base_parts.append(f"    {j.lstrip()}")
    for lj in lookup_joins:
        base_parts.append(f"    {lj.lstrip()}")
    if has_filter:
        # The PO's row filter is authored against product OUTPUT column names,
        # but a base-CTE WHERE can't see the SELECT's own AS-aliases — inline
        # them to their source expressions (a Postgres-only coincidence made
        # the raw form appear to work; it fails on Databricks/Snowflake).
        where_sql = _rewrite_predicate_output_cols(
            filter_predicate, _output_alias_expr_map(select_parts)
        )
        base_parts.append(f"    WHERE {where_sql}")
    base_sql = "\n".join(base_parts)

    ctes = [f"base AS (\n{base_sql}\n)"]
    final_source = "base"

    if has_dedupe:
        # Use safe_name on product columns so the dedupe keys reference the
        # AS aliases emitted in base's SELECT. The PO/engineer authors the
        # dedupe block using product column names; we lower to their physical
        # SQL identifiers here.
        safe_keys = [_safe_name(k) for k in dedupe["keys"]]
        safe_order_by = _safe_name(dedupe["order_by"]) if dedupe["order_by"] else ""
        partition_csv = ", ".join(safe_keys)
        order_clause = f"{safe_order_by} {dedupe['direction'].upper()}" if safe_order_by else partition_csv
        safe_cols = [_safe_name(c) for c in product_col_names]
        cols_csv = ", ".join(safe_cols)
        deduped_sql = (
            f"deduped AS (\n"
            f"    SELECT {cols_csv} FROM (\n"
            f"        SELECT {cols_csv},\n"
            f"               ROW_NUMBER() OVER (PARTITION BY {partition_csv}\n"
            f"                                  ORDER BY {order_clause}) AS _rn\n"
            f"        FROM base\n"
            f"    ) t WHERE _rn = 1\n"
            f")"
        )
        ctes.append(deduped_sql)
        final_source = "deduped"

    underspecified = []
    if has_grouping:
        # Phase 3: GROUP BY assembly. Grouping keys are PO-authored as product
        # column names; lower to their AS aliases (already emitted in base /
        # deduped). Non-grouping product columns are wrapped in their
        # aggregate_function; empty agg_fn falls back to MAX() so the view
        # still compiles — the engineer should set an explicit function via
        # TransformEditor or the chat assistant. Underspecified columns are
        # surfaced in the summary rather than inlined as -- comments (an
        # inline -- comment in a comma-separated SELECT eats the next column).
        gk_safe = [_safe_name(k) for k in grouping_keys]
        gk_set = set(gk_safe)
        select_cols = []
        for pc_name in product_col_names:
            alias = _safe_name(pc_name)
            info = agg_info.get(pc_name) or {}
            if alias in gk_set or info.get("is_grouping_key"):
                select_cols.append(f"    {alias}")
                continue
            agg_fn = (info.get("agg_fn") or "").upper()
            if agg_fn == "COUNT_DISTINCT":
                select_cols.append(f"    COUNT(DISTINCT {alias}) AS {alias}")
            elif agg_fn:
                select_cols.append(f"    {agg_fn}({alias}) AS {alias}")
            else:
                # Default to MAX so the view still parses; record so callers
                # can warn the engineer to set an explicit function.
                select_cols.append(f"    MAX({alias}) AS {alias}")
                underspecified.append(pc_name)
        grouped_sql = (
            f"grouped AS (\n"
            f"    SELECT\n" + ",\n".join(select_cols) + "\n"
            f"    FROM {final_source}\n"
            f"    GROUP BY " + ", ".join(gk_safe) + "\n"
            f")"
        )
        ctes.append(grouped_sql)
        final_source = "grouped"

    # Phase: derived-on-derived enrichment. Each layer projects everything from
    # the prior CTE plus the derived columns whose dependencies are now
    # available as real columns — sidestepping Postgres' ban on referencing a
    # sibling SELECT alias. Stacked so a chain (derived depending on derived)
    # resolves one layer at a time.
    for i, layer in enumerate(derived_layers or []):
        layer_name = f"enriched_{i + 1}"
        layer_exprs = ",\n".join(f"    {e}" for e in layer)
        ctes.append(
            f"{layer_name} AS (\n"
            f"    SELECT *,\n{layer_exprs}\n"
            f"    FROM {final_source}\n"
            f")"
        )
        final_source = layer_name

    select_body = (
        "WITH " + ",\n".join(ctes) + "\n"
        f"SELECT * FROM {final_source}"
    )
    ddl = f"{dialect.view_header(view_schema, view_name)} AS\n{select_body};"
    return ddl, has_filter, has_dedupe, has_grouping, underspecified, select_body


def generate_ddl(driver, database, product_uri, view_schema="public", dialect_name="postgres",
                 source_served_map=None):
    """Generate one CREATE VIEW DDL per :DProdOutputDataset, concatenated.

    Returns (ddl_text, primary_view_name, summary). `primary_view_name` is the
    first emitted view (for back-compat with `store_serving_definition.py`);
    `summary['views']` is the full list of per-dataset summaries.

    Invariant: exactly one CREATE VIEW per output dataset linked to the
    product. Source-aligned products mirror the source 1:1 (N source tables
    → N output datasets → N views). Consumer-aligned products usually have
    one output dataset and emit a single multi-table joined view.

    `dialect_name` (Phase 7): SQL dialect for emission. Defaults to
    'postgres' to preserve the pre-Phase-7 DDL shape. Unknown names fall
    back to PostgresDialect — see `get_dialect`.

    `source_served_map` (dpe-cf over a materialized source): maps each CONSUMES'd
    source dataset to its real `catalog.schema.relation` served location. Empty/None
    preserves the co-located `vw_<name>` behavior byte-for-byte.
    """
    _set_source_served_map(source_served_map)
    dialect = get_dialect(dialect_name)

    with driver.session(database=database) as session:
        datasets = [dict(r) for r in session.run(OUTPUT_DATASETS_QUERY, product_uri=product_uri)]
        if not datasets:
            return None, None, "No output datasets found for product"

        view_blocks = []
        per_view_summaries = []
        primary_view_name = None
        empty_datasets = []

        for ds in datasets:
            ds_uri = ds["uri"]
            physical = ds["physical_name"]
            ddl, view_name, summary, _select_body = _generate_ddl_for_dataset(
                session, product_uri, ds_uri, physical, view_schema, dialect=dialect
            )
            if ddl is None:
                empty_datasets.append(f"{view_name} ({summary})")
                continue
            view_blocks.append(f"-- Output dataset: {physical}\n{ddl}")
            per_view_summaries.append(summary)
            if primary_view_name is None:
                primary_view_name = view_name

        if not view_blocks:
            return None, None, (
                "No views generated — every output dataset has zero approved mappings. "
                f"Datasets checked: {len(datasets)}."
            )

    ddl_text = "\n\n".join(view_blocks)
    summary = {
        "views": per_view_summaries,
        "view_count": len(per_view_summaries),
        "output_dataset_count": len(datasets),
        "skipped_empty_datasets": empty_datasets,
        # Phase 7: which dialect emitted this DDL. Persisted on
        # :ServingDefinition.targetPlatform via store_serving_definition.py
        # so regeneration can pick the right dialect without re-asking.
        "dialect": dialect.name,
        # Phase 3 transform-portability: product-wide roll-up of capability
        # findings across every view. A backend caller checks
        # summary['transform_diagnostics']['errors'] and refuses to deploy when
        # non-empty (staged enforcement).
        "transform_diagnostics": _aggregate_transform_diagnostics(per_view_summaries),
    }
    return ddl_text, primary_view_name, summary


def generate_dbt_models(driver, database, product_uri, view_schema="public",
                        dialect_name="postgres", source_served_map=None):
    """Compile one dbt model body per :DProdOutputDataset.

    Reuses the exact same per-dataset compiler as `generate_ddl` (the virtual-
    view path) — `_generate_ddl_for_dataset` — so the transform DSL
    (:ColumnMapping + :DatasetTransform CTE layers, FK-inferred joins, dialect
    casts, SCD lowering) is compiled in ONE place. The only difference from the
    view path is the emitter: instead of wrapping the SELECT in
    `CREATE OR REPLACE VIEW … AS … ;`, we hand back the bare `select_body` for
    a dbt model file ({{ config(...) }} + body, no trailing `;`).

    Returns (models, summary) where `models` is a list of
        {model_name, physical_name, qualified_view_name, select_body, summary}
    one per non-empty output dataset, and `summary` mirrors `generate_ddl`'s
    aggregate summary shape (views[], view_count, dialect, …).

    `source_served_map`: see `generate_ddl`. Empty/None = co-located behavior.
    """
    _set_source_served_map(source_served_map)
    dialect = get_dialect(dialect_name)

    with driver.session(database=database) as session:
        datasets = [dict(r) for r in session.run(OUTPUT_DATASETS_QUERY, product_uri=product_uri)]
        if not datasets:
            return [], "No output datasets found for product"

        models = []
        per_view_summaries = []
        empty_datasets = []

        for ds in datasets:
            ds_uri = ds["uri"]
            physical = ds["physical_name"]
            ddl, qualified_name, summary, select_body = _generate_ddl_for_dataset(
                session, product_uri, ds_uri, physical, view_schema, dialect=dialect
            )
            if ddl is None or not select_body:
                empty_datasets.append(f"{qualified_name} ({summary})")
                continue
            models.append({
                "model_name": _safe_name(physical),
                "physical_name": physical,
                "qualified_view_name": qualified_name,
                "select_body": select_body,
                "summary": summary,
            })
            per_view_summaries.append(summary)

        if not models:
            return [], (
                "No models generated — every output dataset has zero approved mappings. "
                f"Datasets checked: {len(datasets)}."
            )

    summary = {
        "views": per_view_summaries,
        "view_count": len(per_view_summaries),
        "output_dataset_count": len(datasets),
        "skipped_empty_datasets": empty_datasets,
        "dialect": dialect.name,
        # Phase 3 transform-portability: product-wide capability roll-up (see
        # generate_ddl). Shared by the dbt + lakehouse emitters.
        "transform_diagnostics": _aggregate_transform_diagnostics(per_view_summaries),
    }
    return models, summary


def generate_lakehouse_models(driver, database, product_uri, view_schema="public",
                             dialect_name="duckdb", source_served_map=None):
    """Compile one DuckDB SELECT body per :DProdOutputDataset for the lakehouse
    (Parquet + DuckDB) serving path — the THIRD emitter on the shared SQL core.

    Reuses the exact same per-dataset compiler as the virtual-view and dbt paths
    (`_generate_ddl_for_dataset`), so the transform DSL, FK-inferred joins, SCD
    lowering, and dialect casts resolve in ONE place. The only difference from the
    dbt emitter is the default dialect (DuckDB) and the caller's wrapping: the
    export worker wraps each body in `COPY (<body>) TO '<uri>' (FORMAT PARQUET)`
    and registers `CREATE OR REPLACE VIEW <product> AS SELECT * FROM read_parquet(...)`.

    Base relations in the compiled body reference bare `"schema"."table"`; the
    export worker ATTACHes the live source database and `USE`s it so those
    references resolve (postgres_scan under the hood). For a Parquet *source*,
    the same body is emitted and the worker supplies `read_parquet(...)` refs.

    Returns (models, summary) with the same shape as `generate_dbt_models`:
        {model_name, physical_name, qualified_view_name, select_body, summary}

    `source_served_map`: see `generate_ddl`. NOTE the cross-platform TRANSFER
    path compiles the SOURCE product (catalog-kind mappings, raw relations) with
    no served map — this only affects a dpe-cf consumer over a materialized source.
    """
    _set_source_served_map(source_served_map)
    dialect = get_dialect(dialect_name)

    with driver.session(database=database) as session:
        datasets = [dict(r) for r in session.run(OUTPUT_DATASETS_QUERY, product_uri=product_uri)]
        if not datasets:
            return [], "No output datasets found for product"

        models = []
        per_view_summaries = []
        empty_datasets = []

        for ds in datasets:
            ds_uri = ds["uri"]
            physical = ds["physical_name"]
            ddl, qualified_name, summary, select_body = _generate_ddl_for_dataset(
                session, product_uri, ds_uri, physical, view_schema, dialect=dialect
            )
            if ddl is None or not select_body:
                empty_datasets.append(f"{qualified_name} ({summary})")
                continue
            models.append({
                "model_name": _safe_name(physical),
                "physical_name": physical,
                "qualified_view_name": qualified_name,
                "select_body": select_body,
                "summary": summary,
            })
            per_view_summaries.append(summary)

        if not models:
            return [], (
                "No models generated — every output dataset has zero approved mappings. "
                f"Datasets checked: {len(datasets)}."
            )

    summary = {
        "views": per_view_summaries,
        "view_count": len(per_view_summaries),
        "output_dataset_count": len(datasets),
        "skipped_empty_datasets": empty_datasets,
        "dialect": dialect.name,
        # Phase 3 transform-portability: product-wide capability roll-up (see
        # generate_ddl). Shared by the dbt + lakehouse emitters.
        "transform_diagnostics": _aggregate_transform_diagnostics(per_view_summaries),
    }
    return models, summary


def main():
    parser = argparse.ArgumentParser(description="Generate virtual view DDL from approved column mappings")
    parser.add_argument("--product-uri", required=True, help="Data product URI (e.g., dprod:my-contract)")
    parser.add_argument("--view-schema", default="public", help="Schema for the view (default: public)")
    parser.add_argument("--host", default="localhost", help="Neo4j host")
    parser.add_argument("--bolt-port", type=int, default=7687, help="Neo4j Bolt port")
    parser.add_argument("--username", default="neo4j", help="Neo4j username")
    parser.add_argument("--password", required=True, help="Neo4j password")
    parser.add_argument("--database", default="neo4j", help="Neo4j database")
    parser.add_argument("--output", default=None, help="Output SQL file (default: stdout)")
    parser.add_argument(
        "--dialect",
        default="postgres",
        choices=sorted(_DIALECTS.keys()),
        help=(
            "SQL dialect to emit (default: postgres). "
            "Affects hash function bodies, type-cast syntax, and REGEXP_REPLACE flags. "
            "Pass to match the target database engine; the chosen name lands on "
            ":ServingDefinition.targetPlatform via store_serving_definition.py."
        ),
    )
    parser.add_argument(
        "--source-served-map",
        default="{}",
        help=(
            "JSON object mapping each CONSUMES'd source dataset (physical name) to "
            "the fully-qualified 'catalog.schema.relation' where that source product "
            "was materialized (e.g. a MySQL source loaded into Databricks). Default "
            "'{}' preserves co-located behavior (FROM the source's vw_<name> in the "
            "consumer's own --view-schema). Keys are matched case-insensitively via "
            "the same _safe_name normalization used for view names."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print queries without executing")
    args = parser.parse_args()

    try:
        _served_map_arg = json.loads(args.source_served_map or "{}")
        if not isinstance(_served_map_arg, dict):
            _served_map_arg = {}
    except (TypeError, ValueError):
        print("Warning: --source-served-map is not valid JSON; ignoring.", file=sys.stderr)
        _served_map_arg = {}

    if args.dry_run:
        print(f"Product URI: {args.product_uri}")
        print(f"\n-- MAPPINGS_QUERY\n{MAPPINGS_QUERY.strip()}")
        print(f"\n-- FK_QUERY\n{FK_QUERY.strip()}")
        return

    uri = f"bolt://{args.host}:{args.bolt_port}"
    driver = GraphDatabase.driver(uri, auth=(args.username, args.password))

    try:
        try:
            ddl, view_name, summary = generate_ddl(
                driver, args.database, args.product_uri, args.view_schema,
                dialect_name=args.dialect, source_served_map=_served_map_arg,
            )
        except ViewGenerationError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)

        if ddl is None:
            print(f"Error: {summary}", file=sys.stderr)
            sys.exit(1)

        if args.output:
            import os
            os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
            with open(args.output, "w") as f:
                f.write(f"-- Virtual view(s) for data product: {args.product_uri}\n")
                f.write(f"-- Generated by data-serving-virtual-view skill\n")
                f.write(f"-- One CREATE VIEW per :DProdOutputDataset ({summary['view_count']} total)\n\n")
                f.write(ddl)
                f.write("\n")
            print(f"DDL written to {args.output}")
            # Sidecar summary JSON — same path with `.summary.json` suffix.
            # store_serving_definition.py auto-loads this if present so the
            # structured warnings (multiplication_warnings, auto_bridge_choice,
            # scd_warning, underspecified_aggregates) land on the
            # :ServingDefinition node and surface in the UI.
            import json as _json
            summary_path = args.output + ".summary.json"
            with open(summary_path, "w") as f:
                _json.dump(summary, f, indent=2, default=str)
            print(f"Summary written to {summary_path}")
        else:
            print(ddl)

        print(f"\nPrimary view: {view_name}")
        print(f"Output datasets: {summary['output_dataset_count']}, views emitted: {summary['view_count']}")
        for v in summary.get("views", []):
            tables = ", ".join(v.get("tables", []))
            extras = []
            if v.get("joins"):
                extras.append(f"joins={v['joins']}")
            if v.get("lookup_joins"):
                extras.append(f"lookups={v['lookup_joins']}")
            extra = (" [" + ", ".join(extras) + "]") if extras else ""
            print(f"  - {v['view_name']}: {v['product_columns']} cols from {tables}{extra}")
        if summary.get("skipped_empty_datasets"):
            print(f"Skipped (no approved mappings): {summary['skipped_empty_datasets']}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
