# PostgreSQL — legacy constructs to identify

What to look for when reverse-engineering PostgreSQL queries/jobs/reports.

## Dialect functions & idioms

| Construct | kind | Why it matters for migration |
|---|---|---|
| `string_agg(expr, ',')` | dialect_function | String aggregation; Spark `array_join(collect_list(...))`, Snowflake `LISTAGG`. |
| `COALESCE` / `NULLIF` | dialect_function | Portable; note if used for divide-by-zero guards. |
| `d + INTERVAL '1 day'` / `age(a, b)` | dialect_function | Date math; `age()` has no direct Spark/Snowflake analogue — decompose. |
| `to_char(d, 'YYYY-MM-DD')` / `to_date` | dialect_function | Format tokens differ across engines; must be re-expressed. |
| `::type` cast syntax | cast | Postgres shorthand; targets use `CAST(... AS ...)`. |
| `DISTINCT ON (col)` | dialect | Postgres-only "first row per group"; targets use `QUALIFY ROW_NUMBER()`/window dedup. |
| `generate_series(...)` | set_returning | Set-returning function; Spark/Snowflake need `sequence()`/a numbers table. |
| `ILIKE` | dialect | Case-insensitive match; Spark uses `lower() LIKE`, Snowflake supports `ILIKE`. |
| `array[...]` / `unnest(...)` | array | Native arrays; Spark arrays differ, Snowflake uses `FLATTEN`. |
| `jsonb ->> 'k'` / `->` | json | JSON operators are Postgres-specific; map to `get_json_object`/`:`/`PARSE_JSON`. |
| `RETURNING` clause | dml | Postgres `INSERT ... RETURNING`; no equivalent in Spark SQL. |
| `ON CONFLICT (...) DO UPDATE` | upsert | Upsert; becomes `MERGE` on targets. |
| Window frames `RANGE BETWEEN ...` | window | Portable but verify frame semantics differ subtly across engines. |
| `SELECT ... FOR UPDATE` | locking | Row locks — no analogue on analytical targets; usually dropped (flag it). |

## Connection / job patterns

- **`psycopg2` / SQLAlchemy `postgresql://`** DSNs in Python jobs → record the schemas.
- **Multi-schema in one database** (e.g. `edw`, `retail`, `analytics`) often simulate
  separate source platforms; there may be **no cross-schema FK** — joins ride a shared
  key (e.g. `customer_id`).
- **SCD-2 via `is_current` + effective-dating**; partial indexes `... WHERE is_current`
  hint at as-of query patterns to preserve.
- **Self-referencing hierarchies** (`parent_id`, `parent_department_id`) → recursive CTE
  candidates; note whether the legacy code uses `WITH RECURSIVE`.
- **Derived columns** (`line_total = quantity*unit_price*(1-discount_pct/100)`) — record
  the formula as a requirement so the target reproduces it exactly.
