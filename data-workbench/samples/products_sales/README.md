# Products & Sales — sample PostgreSQL dataset

A self-contained demo dataset for the Data Workbench. Load it into any PostgreSQL 13+ instance, point a Workbench project at it, and discover/profile/map/serve real data across an 8-table star-shaped schema.

The schema is deliberately shaped so a downstream consumer-aligned data product can exercise **every** column-level transformation kind and **every** dataset-level Shape feature the Workbench supports today (column-level: direct, cast, format, concat, split, substring, case, arithmetic, lookup with all four strategies, literal, expression, bucket, mask, hash, window; dataset-level: filter, dedupe, grouping_keys, joins[], suppressed_columns, window_specs, all three SCD policies, plus auto-bridge and temporal-bridge wrapping).

## Contents

| File | Purpose |
|---|---|
| `01_schema.sql` | `CREATE SCHEMA products_sales` + 8 tables + FKs + indexes + table/column comments |
| `02_seed.sql` | Deterministic seed data (~1,400 rows across all tables) + integrity checks |
| `03_consumer_schema.sql` | `CREATE SCHEMA wb_views` — target for served views (currently loaded manually; see below) |

## Loading the data

### Option A — Existing local Postgres

```bash
createdb products_sales_demo
psql products_sales_demo -f samples/products_sales/01_schema.sql
psql products_sales_demo -f samples/products_sales/02_seed.sql
psql products_sales_demo -f samples/products_sales/03_consumer_schema.sql
```

Use this connection string in the Workbench when creating the project:
```
postgresql://<user>:<pass>@localhost:5432/products_sales_demo
```

### Option B — Throwaway Postgres in Docker

All commands below assume CWD is the repo root. Adjust the SQL paths if you `cd samples/products_sales` first.

```bash
# Idempotent — remove a prior pgdemo container if it exists
docker rm -f pgdemo 2>/dev/null

docker run -d --name pgdemo -p 5433:5432 \
  -e POSTGRES_DB=products_sales_demo \
  -e POSTGRES_PASSWORD=demo postgres:16
sleep 5
```

Then load the SQL one of two ways:

**B1 — `psql` on the host** (requires the `postgresql-client` package, **not** `postgresql-client-common` which is just the wrapper and ships no `psql` binary):

```bash
sudo apt install -y postgresql-client     # Debian/Ubuntu; skip if you already have psql
PGURL=postgresql://postgres:demo@localhost:5433/products_sales_demo
psql "$PGURL" -f samples/products_sales/01_schema.sql
psql "$PGURL" -f samples/products_sales/02_seed.sql
psql "$PGURL" -f samples/products_sales/03_consumer_schema.sql
```

**B2 — `psql` inside the container** (no host install needed):

```bash
docker exec -i pgdemo psql -U postgres -d products_sales_demo < samples/products_sales/01_schema.sql
docker exec -i pgdemo psql -U postgres -d products_sales_demo < samples/products_sales/02_seed.sql
docker exec -i pgdemo psql -U postgres -d products_sales_demo < samples/products_sales/03_consumer_schema.sql
```

Workbench connection string: `postgresql://postgres:demo@localhost:5433/products_sales_demo`

### Verifying the load

```sql
\dn                                     -- should show products_sales and wb_views schemas
\dt products_sales.*                    -- 8 tables
SELECT 'category'                   AS t, COUNT(*) FROM products_sales.category
UNION ALL SELECT 'product',                COUNT(*) FROM products_sales.product
UNION ALL SELECT 'product_price_history',  COUNT(*) FROM products_sales.product_price_history
UNION ALL SELECT 'customer',               COUNT(*) FROM products_sales.customer
UNION ALL SELECT 'customer_segment_assignment', COUNT(*) FROM products_sales.customer_segment_assignment
UNION ALL SELECT 'order_header',           COUNT(*) FROM products_sales.order_header
UNION ALL SELECT 'order_item',             COUNT(*) FROM products_sales.order_item
UNION ALL SELECT 'order_status_history',   COUNT(*) FROM products_sales.order_status_history;
```

Expected row counts (deterministic — reproducible across loads):

| Table | Rows |
|---|---:|
| `category` | 20 |
| `product` | 50 |
| `product_price_history` | 135 |
| `customer` | 30 |
| `customer_segment_assignment` | 61 |
| `order_header` | 150 |
| `order_item` | 375 |
| `order_status_history` | 552 |

## Schema overview

```
                          category (hierarchical, self-FK on parent_id)
                            │
                            ▼
   customer ──┐         product ──┐
              │            │      │
              │            │      └──> product_price_history (SCD-2)
              ▼            ▼
   customer_segment    order_header (fact)
   _assignment              │
   (SCD-2)                  ├──> order_item (junction; bridges customer ↔ product)
                            │
                            └──> order_status_history (temporal bridge)
```

| Table | Role | Demo coverage |
|---|---|---|
| `category` | Hierarchical dimension | self-join · hierarchical FK · multi-level rollup |
| `product` | Dimension | direct · cast · case · arithmetic (margin) · lookup-equi (→ category) |
| `product_price_history` | SCD-2 history | SCD-2 lowering · lookup `latest` (current price) · snapshot SCD (price as-of date) |
| `customer` | Dimension (PII) | concat (full name) · split / substring (email username) · format (postal) · mask (email) · hash (id) · bucket (LTV band) |
| `customer_segment_assignment` | SCD-2 history | `latest_only` synthesised dedupe · lookup `latest` (current segment) |
| `order_header` | Fact | grouping_keys · windowing (rank by spend, row_number per customer ordered by date) · filter (delivered only) |
| `order_item` | Junction | auto-bridge (customer ↔ product through header → item) · lookup `aggregate` (qty per customer) · arithmetic (line_total) |
| `order_status_history` | Temporal bridge | temporal-bridge auto-wrap · lookup `exists` (`has_returned_order`) · multiplication warning if unwrapped |

## Suggested consumer-aligned data products to author

You don't have to build all of these — the dataset is shaped so each one exercises a distinct slice of the transformation surface. Pick one or two to start; the appendix `playbook/odcs_templates/products_sales_*.yaml` files are starting points for two of them.

### 1. `customer_360`

A single row per customer with current segment + lifetime stats.

> **PO ask:** "Marketing and CX keep pulling the same five things together by hand — who the customer is, what segment they're in right now, how much they've spent over their lifetime, how many orders they've placed, and whether they've ever returned anything. I want one place they can pull from instead of stitching the customer table, the SCD-2 segment assignments, and the orders table every time. One row per customer, freshest segment, lifetime rollups."

- **Concat** `first_name + " " + last_name` → `full_name`
- **Mask** `email` (keep_last 4 + keep_format) → `email_masked`
- **Bucket** `lifetime_value_usd` → `clv_band` (low / mid / high / vip)
- **Lookup `latest`** `current_segment` from `customer_segment_assignment` (SCD-2 latest_only)
- **Lookup `aggregate`** `total_orders` (COUNT) and `total_spend` (SUM) from delivered orders
- **Lookup `exists`** `has_returned_order` from `order_status_history` where status='returned'
- **Window** `signup_rank_in_country` = `RANK() OVER (PARTITION BY country_code ORDER BY signup_date)`

### 2. `product_performance`

Product roster with current price and sales rollups.

> **PO ask:** "Category managers want a weekly view of how the catalogue is performing without anyone re-deriving 'current price' from the SCD-2 price history each time. Per product: its category, what it's priced at today, what the margin is, how many units have sold, what the revenue is, and how it ranks against its siblings in the same category. The merchandising deck pulls from this — so it needs to be one clean roster, not a join puzzle."

- **Lookup-equi** `category_name` from `category`
- **Lookup `latest`** `current_price` from `product_price_history` where `is_current=true`
- **Arithmetic** `margin = (list_price - cost) / list_price * 100`
- **Lookup `aggregate`** `units_sold` (SUM(quantity)) and `revenue` (SUM(line_total)) from `order_item`
- **Window** `top_seller_rank` = `RANK() OVER (PARTITION BY category_id ORDER BY revenue DESC)`

### 3. `order_snapshot_2024_q4`

Orders as of 2024-12-31 with their then-current status.

> **PO ask:** "Finance is closing Q4 and needs an auditable freeze of the order book as of December 31. Not today's state — the state at that moment. They want the order, the customer it belonged to, where it was shipping, and what status it was in *on that date* (not its current status, which may have moved). It's a one-and-done snapshot — they'll cite it in the quarterly close and shouldn't see it drift afterwards."

- **Snapshot SCD** filter on `as_of_date='2024-12-31'`
- **Lookup `latest`** order's status from `order_status_history` where `status_changed_at <= as_of_date` (temporal bridge demo)
- **Lookup-equi** `customer_name`, `customer_country`
- **Grouping** by `customer_id` and `shipping_country_code` for rollup variants

### 4. `customer_analytics_safe`

Privacy-preserving customer view for analytics.

> **PO ask:** "Data science wants to model churn and lifetime value on the customer base, but Legal won't approve raw PII landing in the analytics warehouse. Build them a safe version: drop names, birth date, postal code; hash the customer id so they can still join across other safe products; mask the email so they can see domain patterns (`gmail.com` vs corporate) without seeing the address itself; bucket age into bands instead of exposing date of birth. Same analytical signal, no PII liability."

- **Hash** `customer_id` → `hashed_id` (sha256)
- **Mask** `email` → `email_masked`
- **Suppressed columns**: `first_name`, `last_name`, `date_of_birth`, `postal_code` dropped from the served view (still in the contract / lineage).
- **Bucket** `date_of_birth` → `age_band` (under-25 / 25-34 / 35-49 / 50+) via arithmetic on `EXTRACT(YEAR FROM AGE(date_of_birth))`

### 5. `historical_price_book`

Full SCD-2 price history exposed as-is.

> **PO ask:** "Pricing strategy doesn't want the 'current price' rollup — they want the *whole* price history so they can run their own as-of-date analyses, elasticity curves, and competitive benchmarks. Give them the SCD-2 table preserved (effective / expiration / is_current intact) with the product SKU, product name, and category joined in so they don't have to look those up themselves. The shape is the value here — don't collapse it."

- **SCD-2 policy** on the dataset: `effective_column=effective_date`, `expiration_column=expiration_date`, `add_is_current=true`.
- **Lookup-equi** `product_sku`, `product_name`, `category_name`.

### 6. `daily_sales_summary`

One row per calendar day with revenue, order count, and customer count.

> **PO ask:** "Ops and finance both want the same daily heartbeat: how many orders did we book yesterday, how much revenue, how many distinct customers. Right now it's a Slack message someone runs a query for every morning. Make it a product — daily grain, weekday vs weekend flagged, year-month already broken out so the BI tool can pivot without re-parsing dates, and a 7-day moving average column so a single bad day doesn't look like a trend. Tag every row with the source system so when we add the second OMS next quarter the union still makes sense."

- **Grouping keys** `order_day` (day-truncated `order_date`) — activates the `grouped` CTE
- **Format** `order_date` → `order_day` as `YYYY-MM-DD` ISO string
- **Substring** `order_year_month` = `SUBSTRING(order_day, 1, 7)` (`'2024-12'`) for BI pivot
- **Case** `day_kind` = `WHEN EXTRACT(DOW FROM order_date) IN (0,6) THEN 'weekend' ELSE 'weekday'`
- **Literal** `source_system = 'products_sales'` (provenance constant, no source column)
- **Aggregate** `orders_count` (COUNT), `revenue` (SUM(line_total) from `order_item`), `distinct_customers` (COUNT DISTINCT)
- **Window** `revenue_7d_moving_avg` = `AVG(revenue) OVER (ORDER BY order_day ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)`
- **Filter** delivered orders only

### 7. `fulfillment_sla`

Per-order cycle-time view — placed → shipped → delivered with SLA banding.

> **PO ask:** "Operations is getting beaten up on delivery times and we have no clean view of it. For each order I want when it was placed, when it shipped, when it was delivered, and the day-count between each leg. Bucket the total cycle time into SLA bands (under-3-day / 3-7 / 7-14 / over-14) so the ops weekly can show 'we missed SLA on 11% of orders this week' without re-deriving it every time. The status history table has all the timestamps — we just need them pivoted onto the order row."

- **Lookup `latest`** `placed_at` from `order_status_history` where `status='placed'` (temporal bridge auto-wrap)
- **Lookup `latest`** `shipped_at` from `order_status_history` where `status='shipped'`
- **Lookup `latest`** `delivered_at` from `order_status_history` where `status='delivered'`
- **Arithmetic** `time_to_ship_days = shipped_at - placed_at`, `time_to_deliver_days = delivered_at - shipped_at`, `cycle_time_days = delivered_at - placed_at`
- **Bucket** `cycle_time_days` → `sla_band` (`under_3d` / `3_to_7d` / `7_to_14d` / `over_14d`)
- **Case** `met_sla` = `WHEN cycle_time_days <= 7 THEN true ELSE false`
- **Window** `slowest_rank_per_country` = `RANK() OVER (PARTITION BY shipping_country_code ORDER BY cycle_time_days DESC)`
- **Filter** delivered orders only (exclude in-flight)

## Manual workaround for the deploy step

The Workbench's `serving_virtual_view` stage **generates** the view DDL but does not currently execute it on Postgres. After the stage runs, the DDL lands at `projects/<project_code>/serving/virtual_view.sql`. To finish the demo end-to-end, deploy it manually:

```bash
# 1. Inspect the generated DDL
cat projects/<project_code>/serving/virtual_view.sql

# 2. If the DDL doesn't already qualify the view name with wb_views,
#    prepend a search_path directive before running it:
(echo "SET search_path TO wb_views, products_sales;"; \
 cat projects/<project_code>/serving/virtual_view.sql) \
  | psql products_sales_demo

# 3. Verify
psql products_sales_demo -c '\dv wb_views.*'
psql products_sales_demo -c 'SELECT * FROM wb_views.<view_name> LIMIT 5;'
```

Authoring tip: the engineer can edit the column mapping `transformExpression` to fully-qualify table names (`products_sales.product`, `products_sales.customer`) so the view works regardless of `search_path`.

## What's deterministic / reproducible

- All transactional data is generated from id-arithmetic (`((id * 7 + 3) % 30)` etc.), not random functions. Two fresh loads produce byte-identical state.
- Customer lifetime values are recomputed at the end of `02_seed.sql` from delivered order totals — they match the order data exactly.
- Integrity DO-block at the end of `02_seed.sql` raises an exception if any FK is dangling or if any product has zero or more than one current price row.

## Re-loading

Re-running the SQL is destructive — `01_schema.sql` starts with `DROP SCHEMA IF EXISTS products_sales CASCADE`. The `wb_views` schema is preserved unless you also drop it manually. Any views previously deployed there will become invalid once the source tables are recreated; either drop and recreate them, or rely on `CREATE OR REPLACE VIEW`.

```bash
# Full reset
psql products_sales_demo -c 'DROP SCHEMA IF EXISTS wb_views CASCADE;'
psql products_sales_demo -f samples/products_sales/01_schema.sql
psql products_sales_demo -f samples/products_sales/02_seed.sql
psql products_sales_demo -f samples/products_sales/03_consumer_schema.sql
```

## Companion playbook files

Authored alongside this dataset:

- `playbook/domain_catalogs/products_sales.yaml` — column definitions + recommended DQ rules; surfaced in the wizard when the engineer picks a Products & Sales-themed domain.
- `playbook/transformation_catalogs/products_sales.yaml` — pre-canned transformation templates the mapping skill auto-matches (e.g., `email_masked`, `current_product_price`, `lifetime_value_band`, `has_returned_order`).
- `playbook/domain_catalogs/guidance/products_sales.md` — free-form domain prose: typical analytics patterns, schema notes, naming conventions.
- `playbook/odcs_templates/products_sales_customer_360.yaml` and `playbook/odcs_templates/products_sales_product_performance.yaml` — example consumer-aligned ODCS contracts referencing this schema.
