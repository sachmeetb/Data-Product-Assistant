# postgres-products — legacy Postgres sales-rollup ETL

`legacy_sales_rollup.py` — a monthly per-product revenue rollup ETL (psycopg2)
written against the **`products_sales`** sample.

## What it computes

Per product per month (delivered orders only): total revenue, units, the mix of
customers' **current** segments (SCD-2 as-of), and the product's current list price.
Written as an idempotent upsert so it is re-runnable.

## Legacy constructs it exercises (what the reverse stage should identify)

- `string_agg(DISTINCT ... ORDER BY ...)` — Postgres string aggregation (→ Databricks `array_join(collect_list(...))`).
- `date_trunc('month', ...)::date` + `::type` casts — Postgres shorthand casts.
- `to_char(d, 'YYYY-MM')` — Postgres format model (re-express for the target).
- `DISTINCT ON`-style "current row" via correlated `ORDER BY ... LIMIT 1` (→ `QUALIFY ROW_NUMBER()`).
- `ILIKE` — case-insensitive match.
- `ON CONFLICT (...) DO UPDATE` — Postgres upsert (→ target `MERGE`).
- **SCD-2 as-of** join on `effective_from`/`effective_to` that the target must preserve.
- **Derived-column arithmetic** (`quantity * unit_price * (1 - discount_pct/100)`) — reproduce exactly.
- Hard-coded DSN fallback — must NOT survive into the converted code (platform provides the connection).

Pair with a `dmig` migration of `products_sales` → Databricks; convert this to a
Databricks **PySpark job**.
