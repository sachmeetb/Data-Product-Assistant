# Databricks — target patterns, best practices, anti-patterns

Apply these when forward-engineering code to Databricks (Spark SQL / PySpark on a
Lakehouse with Unity Catalog). Prefer **Databricks SQL** for set-based logic;
reach for **PySpark** only when the artifact kind is a job/notebook needing
imperative control.

## Best practices

- **Three-level names.** Reference tables as `catalog.schema.table` (Unity Catalog).
  Use the `target_schema` / target catalog from `schema_mapping.json`; never hard-code.
- **`MERGE INTO` for upserts.** Convert `ON DUPLICATE KEY UPDATE` / `REPLACE INTO` /
  Oracle `MERGE` / `ON CONFLICT` into `MERGE INTO tgt USING src ON ... WHEN MATCHED ...`.
- **`QUALIFY` for top-N-per-group.** Convert `DISTINCT ON` / correlated subqueries /
  `ROWNUM` filters into `QUALIFY ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...) = 1`.
- **String aggregation:** `array_join(collect_list(x), ',')` (or `concat_ws`) for
  `GROUP_CONCAT` / `string_agg` / `LISTAGG`.
- **Date math:** `date_add`, `add_months`, `datediff`, `date_trunc`; format with
  `date_format(d, 'yyyy-MM-dd')` (Java-style tokens — NOT `%Y`).
- **Recursive hierarchies:** Databricks SQL supports `WITH RECURSIVE` (DBR 14.1+);
  convert `CONNECT BY` / self-joins to it. If runtime predates it, emit a bounded
  iterative PySpark loop and note the version requirement.
- **Explicit casts.** Databricks is stricter than MySQL/Oracle about implicit coercion —
  make every intended cast explicit with `CAST(... AS ...)` / `::` is not available.
- **SCD-2 as-of logic preserved.** Keep `WHERE is_current` / effective-date filters;
  do not collapse history silently.
- **PySpark jobs:** use the DataFrame API + `spark.table("catalog.schema.table")`, keep
  transformations lazy, write with `.saveAsTable(...)` / `MERGE` via Delta.

## Anti-patterns (do NOT emit)

- ❌ `SELECT *` into a persisted table — enumerate columns from `schema_mapping.json`.
- ❌ `collect()` / `toPandas()` on large frames in a job (driver OOM). Stay distributed.
- ❌ Row-at-a-time cursors / loops ported literally from PL/SQL — re-express set-based.
- ❌ Backtick-quoted identifiers copied from MySQL — Databricks uses backticks too, but
  only quote when needed; prefer unquoted lower_snake_case.
- ❌ Hard-coded credentials or JDBC URLs — the platform provides the connection.
- ❌ `%`-style date format tokens (`%Y-%m-%d`) — use Java/Spark tokens (`yyyy-MM-dd`).
- ❌ Treating a Teradata PRIMARY INDEX or a `NUMBER`/`TINYINT(1)` as a semantic
  constraint — carry the reviewer's note, don't invent uniqueness/booleans.
