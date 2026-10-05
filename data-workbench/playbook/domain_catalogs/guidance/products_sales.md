# Products & Sales — domain guidance

Free-form guidance for the **Products & Sales** domain. Read this before authoring a consumer-aligned data product over the `samples/products_sales/` source schema. Covers terminology, schema notes, typical analytics shapes, and naming conventions.

## What's in scope

The Products & Sales domain models the artefacts a direct-to-consumer or retail business produces in the regular course of selling physical or digital goods:

- **Catalog** — the products being sold (`product`, `category`).
- **Pricing** — list price today plus the full history of past prices (`product_price_history`).
- **Customers** — accounts that can buy (`customer`).
- **Segmentation** — lifecycle classifications attached to customers over time (`customer_segment_assignment`).
- **Orders** — the commercial transactions (`order_header`, `order_item`).
- **Order lifecycle** — the timeline of status changes per order (`order_status_history`).

Out of scope (not in this dataset, by design):
- Inventory / warehouse state
- Returns / refunds as first-class artefacts (a return is implied by `order_status='returned'` and a row in `order_status_history`)
- Promotions / coupon codes
- Reviews / ratings
- Cart abandonment events
- Shipping carrier integration
- Tax calculation and remittance

## Schema notes

### Hierarchical category tree (`category`)

20 nodes in three depths. Top-level categories (Electronics, Apparel, Home, Books) have `parent_id IS NULL` and `depth=0`. Walk the tree with a recursive CTE:

```sql
WITH RECURSIVE tree AS (
  SELECT id, name, parent_id, depth, name AS path FROM category WHERE parent_id IS NULL
  UNION ALL
  SELECT c.id, c.name, c.parent_id, c.depth, t.path || ' > ' || c.name FROM category c
    JOIN tree t ON c.parent_id = t.id
)
SELECT * FROM tree ORDER BY path;
```

Most analytics queries flatten the hierarchy by joining to the depth-2 leaf categories and rolling up via `parent_id` chains.

### SCD-2 tables: `product_price_history` and `customer_segment_assignment`

Both follow the same shape:

- A natural key (`product_id` / `customer_id`) plus a versioning window.
- `effective_from` (or `effective_date`) is inclusive: the row was active starting that date.
- `effective_to` (or `expiration_date`) is exclusive: the row stopped being active on (or, more precisely, was superseded by another row on) that date. `NULL` marks the currently-active row.
- A convenience boolean (`is_current` on prices) marks the active row redundantly with `expiration_date IS NULL`. Demos can use either.

When authoring a consumer product that needs the current price or current segment:
- **The lookup `latest` strategy** in the transformation DSL is the right primitive — it emits a derived-table join with `ROW_NUMBER() OVER (PARTITION BY key ORDER BY <order_by_column> DESC) = 1`.
- Alternatively use the `is_current = true` filter directly for prices (`current_product_price` template in `transformation_catalogs/products_sales.yaml`).

For SCD-2 dataset-level shape, declare `scd_policy.type='scd2'` with `effective_column` and `expiration_column`. The view will preserve the history; optional `add_is_current=true` derives a boolean.

For point-in-time reporting (e.g. "what was the price on 2024-06-30?"), use `scd_policy.type='snapshot'` with `snapshot_column='effective_date'` and `as_of_date='2024-06-30'`.

### Temporal bridge: `order_status_history`

One row per status change per order. Use cases:

- **Current status of an order** — already denormalised onto `order_header.status` for convenience. Engineers building a consumer product can either use that directly, or compute it from `order_status_history` via lookup-latest as a demo.
- **"Has ever been X" flags** — `lookup` `exists` strategy. The `has_returned_order` and `has_cancelled_order` templates show the pattern.
- **Time-in-status analytics** — joining the table to itself (`status_changed_at` to `LEAD(status_changed_at)`) yields the duration of each status. Out of scope for the default templates but a natural extension.

When `order_status_history` is mapped as a source AND the consumer's mapped tables span the FK graph, the Workbench's auto-bridge BFS may pick it as an intermediate. Because the table has `status_changed_at` and matches the temporal pattern, the view-DDL generator auto-wraps it in `ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY status_changed_at DESC) = 1` so the bridge contributes the **latest** row per order. This avoids row-multiplication.

### PII handling on `customer`

The PII columns are `email`, `first_name`, `last_name`, and `date_of_birth`. Best practices for a consumer product:

- **Operational views** (used by support, fulfilment): keep PII as-is.
- **Analytics views** (used by BI, data science): mask or hash. Use the `email_masked`, `customer_id_hash` templates. Suppress `first_name`, `last_name`, `date_of_birth`, `postal_code` via the dataset-level `suppressed_columns` field.
- **Marketing views**: keep email (needed for outbound) but mask other PII; verify `customer_status != 'banned'` filter is in place.

## Naming conventions

- **Boolean fields**: prefix with `is_`, `has_`, or `flag` suffix. E.g. `is_current`, `has_returned_order`, `active_flag`.
- **Date fields**: suffix with `_date` for plain dates, `_at` for timestamps. E.g. `signup_date`, `status_changed_at`.
- **Aggregate-derived fields**: prefix with `total_` (SUM/COUNT), `avg_` (AVG), `max_`/`min_`. E.g. `total_orders`, `avg_order_value`.
- **Currency fields**: suffix with `_usd` (or other ISO 4217 code) when the unit is fixed; suffix with `_amount` when the currency is implicit from a sibling column. E.g. `lifetime_value_usd`, `total_amount`.
- **Foreign-key fields**: suffix with `_id`. The referenced table is the prefix without `_id`. E.g. `customer_id` → `customer`, `category_id` → `category`.

## Typical consumer-product shapes

### Customer 360
**Grain**: one row per customer (PK = `customer_id`).
**Sources**: `customer`, `customer_segment_assignment`, `order_header`, `order_status_history`.
**Transforms**: concat name, mask email, bucket LTV, lookup-latest segment, aggregate order count + spend, exists for returned/cancelled.
**Shape**: filter to `customer_status != 'banned'`. Optional grouping_keys for country-level rollup variants.

### Product Performance
**Grain**: one row per product (PK = `product_id`).
**Sources**: `product`, `category`, `product_price_history`, `order_item`, `order_header`.
**Transforms**: lookup-equi category, lookup-latest current_price, arithmetic margin, aggregate units_sold + revenue.
**Shape**: filter to `active_flag = true`. Window for top-N rank within category.

### Order Snapshot
**Grain**: one row per order at a fixed point in time (PK = `order_id`).
**Sources**: `order_header`, `order_status_history`, `customer`, `order_item`, `product`.
**Transforms**: lookup-latest status from history with `status_changed_at <= as_of_date`, joins to customer + product.
**Shape**: `scd_policy.type='snapshot'` with `as_of_date='YYYY-MM-DD'`. Auto-bridge BFS will use `order_status_history` between header and itself for the status at-time-of-snapshot, with temporal wrap.

### Customer Analytics Safe
**Grain**: one row per customer.
**Sources**: `customer`, `customer_segment_assignment`, `order_header`.
**Transforms**: hash customer_id, mask email, bucket LTV + age, lookup-latest segment, aggregate spend.
**Shape**: `suppressed_columns: [first_name, last_name, date_of_birth, postal_code]`. Filter `customer_status != 'banned'`.

### Historical Price Book
**Grain**: one row per (product, effective_date). All history preserved.
**Sources**: `product_price_history`, `product`, `category`.
**Transforms**: lookup-equi product_name, sku, category_name.
**Shape**: `scd_policy.type='scd2'` with `effective_column=effective_date`, `expiration_column=expiration_date`, `add_is_current=true`.

## Sample-data caveats

The seed in `samples/products_sales/02_seed.sql` is **deterministic** and **small** (~3,000 rows total). Some demos that need realistic distributions or skew (e.g. "show the long-tail of customer spend") will look thin. The dataset is sized for in-class demos and quick reloads, not for production-style stress testing. Increase the loop bounds in `02_seed.sql` if needed; the generation logic scales linearly.
