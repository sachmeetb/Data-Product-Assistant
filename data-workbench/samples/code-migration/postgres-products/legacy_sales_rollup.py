#!/usr/bin/env python3
"""Legacy monthly product-sales rollup ETL  (PostgreSQL 16, psycopg2).

Source: products_sales sample (schema: products_sales).

Rolls up delivered order lines into a per-product monthly revenue table, tagged
with the customer's CURRENT segment (SCD-2 as-of) and the product's current list
price. Written in idiomatic legacy Postgres/psycopg2 so a code migration has real
constructs to convert (target: a Databricks PySpark job).

This is UNTRUSTED sample code — it is *read* by the migration, never executed here.
"""
import os
import psycopg2

# Legacy pattern: DSN assembled from env with a hard-coded fallback.
DSN = os.environ.get("PG_DSN", "postgresql://app:app@localhost:5432/products_sales")

# The whole job is one set-based statement — the kind of thing that should become a
# single Databricks SQL MERGE / INSERT, not a row-by-row loop.
ROLLUP_SQL = """
INSERT INTO products_sales.monthly_product_revenue
    (product_id, month, revenue, units, current_segment_mix, list_price)
SELECT
    oi.product_id,
    -- Postgres date_trunc + ::date cast + to_char formatting.
    to_char(date_trunc('month', oh.order_date)::date, 'YYYY-MM')      AS month,
    -- Derived-column arithmetic must be reproduced exactly.
    SUM(oi.quantity * oi.unit_price * (1 - oi.discount_pct / 100.0))  AS revenue,
    SUM(oi.quantity)                                                 AS units,
    -- Postgres string aggregation of the customers' CURRENT segment.
    string_agg(DISTINCT seg.segment_code, ',' ORDER BY seg.segment_code) AS current_segment_mix,
    -- DISTINCT ON: the product's current price row (Postgres-only idiom).
    (SELECT pph.price
       FROM products_sales.product_price_history pph
      WHERE pph.product_id = oi.product_id
        AND pph.is_current
      ORDER BY pph.effective_date DESC
      LIMIT 1)                                                       AS list_price
FROM products_sales.order_item oi
JOIN products_sales.order_header oh
    ON oh.id = oi.order_id
JOIN products_sales.customer c
    ON c.id = oh.customer_id
-- SCD-2 as-of: only the customer's currently-effective segment assignment.
LEFT JOIN products_sales.customer_segment_assignment seg
    ON seg.customer_id = c.id
   AND seg.effective_from <= oh.order_date
   AND (seg.effective_to IS NULL OR seg.effective_to > oh.order_date)
WHERE oh.status = 'delivered'
  -- ILIKE is a Postgres case-insensitive match idiom.
  AND c.status ILIKE 'active'
GROUP BY oi.product_id, date_trunc('month', oh.order_date)
-- Upsert so the job is re-runnable (Postgres ON CONFLICT → target MERGE).
ON CONFLICT (product_id, month) DO UPDATE
    SET revenue = EXCLUDED.revenue,
        units = EXCLUDED.units,
        current_segment_mix = EXCLUDED.current_segment_mix,
        list_price = EXCLUDED.list_price;
"""


def main() -> None:
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(ROLLUP_SQL)
        conn.commit()
        print("monthly_product_revenue rollup complete")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
