# Snowflake — target patterns, best practices, anti-patterns

Apply these when forward-engineering code to Snowflake.

## Best practices

- **Three-level names** `database.schema.table` from `schema_mapping.json`; never
  hard-code. Respect Snowflake's UPPER-case default identifier folding — quote only when
  the source truly used mixed case.
- **`MERGE INTO`** for all upserts (`ON DUPLICATE KEY UPDATE`, `REPLACE INTO`,
  `ON CONFLICT`, Oracle `MERGE`).
- **`QUALIFY ROW_NUMBER() OVER (...)`** for top-N-per-group / `DISTINCT ON` / `ROWNUM`.
- **`LISTAGG(expr, ',') WITHIN GROUP (ORDER BY ...)`** for `GROUP_CONCAT`/`string_agg`.
- **Date math:** `DATEADD`, `DATEDIFF`, `DATE_TRUNC`, `TO_CHAR(d, 'YYYY-MM-DD')`
  (Snowflake format model — not `%Y`).
- **Semi-structured:** `PARSE_JSON`, `:` path access, `FLATTEN` for `jsonb`/arrays.
- **Recursive hierarchies:** `WITH RECURSIVE` for `CONNECT BY`.
- **Explicit `CAST(... AS ...)`**; Snowflake `::` shorthand is acceptable and idiomatic.
- **Preserve SCD-2 as-of filters** (`WHERE is_current`, effective dating).
- Prefer **set-based SQL**; use Snowflake Scripting (`BEGIN...END`) only when the artifact
  is genuinely procedural, and keep it minimal.

## Anti-patterns (do NOT emit)

- ❌ `SELECT *` into a table — enumerate mapped columns.
- ❌ Row-by-row cursors ported from PL/SQL — re-express set-based.
- ❌ MySQL backticks / Postgres `::text` idioms left unconverted.
- ❌ `%`-style date tokens — use Snowflake's format model.
- ❌ Hard-coded warehouse/role/credentials in the converted code.
- ❌ Relying on implicit empty-string = NULL (Oracle) — Snowflake distinguishes them;
  make the intent explicit and flag it if the original depended on it.
