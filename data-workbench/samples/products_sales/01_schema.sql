-- =============================================================================
-- Data Workbench sample dataset — Products & Sales
-- File 1 of 3: schema definition
--
-- Run order: 01_schema.sql -> 02_seed.sql -> 03_consumer_schema.sql
-- See README.md in this directory for load instructions and demo coverage.
-- =============================================================================

DROP SCHEMA IF EXISTS products_sales CASCADE;
CREATE SCHEMA products_sales;
SET search_path TO products_sales, public;

-- ---------------------------------------------------------------------------
-- category
-- Hierarchical product taxonomy. 3-level tree rooted at depth=0.
-- Self-FK on parent_id enables self-join demos and hierarchical roll-ups.
-- ---------------------------------------------------------------------------
CREATE TABLE category (
    id              INTEGER     PRIMARY KEY,
    parent_id       INTEGER     REFERENCES category(id),
    name            TEXT        NOT NULL,
    depth           SMALLINT    NOT NULL CHECK (depth BETWEEN 0 AND 5),
    is_active       BOOLEAN     NOT NULL DEFAULT true
);

COMMENT ON TABLE  category IS 'Hierarchical product taxonomy. Demo: self-FK joins, hierarchical roll-up.';
COMMENT ON COLUMN category.parent_id IS 'Null for top-level categories. Self-FK to category.id.';
COMMENT ON COLUMN category.depth     IS 'Depth in the hierarchy (0 = top-level).';

-- ---------------------------------------------------------------------------
-- product
-- Dimension table. One row per SKU.
-- Demo: direct, cast, case, arithmetic, lookup-equi (-> category).
-- ---------------------------------------------------------------------------
CREATE TABLE product (
    id              INTEGER         PRIMARY KEY,
    sku             VARCHAR(32)     NOT NULL UNIQUE,
    name            TEXT            NOT NULL,
    category_id     INTEGER         NOT NULL REFERENCES category(id),
    brand           TEXT,
    list_price      NUMERIC(10,2)   NOT NULL CHECK (list_price >= 0),
    cost            NUMERIC(10,2)   CHECK (cost >= 0),
    weight_kg       NUMERIC(8,3)    CHECK (weight_kg >= 0),
    color           VARCHAR(30),
    size_code       VARCHAR(10),
    active_flag     BOOLEAN         NOT NULL DEFAULT true,
    launched_at     DATE            NOT NULL
);

CREATE INDEX product_category_idx ON product(category_id);

COMMENT ON TABLE  product IS 'Product master dimension. Demo: lookup-equi to category, arithmetic margin = list_price-cost.';
COMMENT ON COLUMN product.sku IS 'Stock-keeping unit. Format: <3-letter category code>-<3-digit sequence>.';

-- ---------------------------------------------------------------------------
-- product_price_history
-- SCD-2 history table. One row per (product, effective_date).
-- Demo: SCD-2 lowering, lookup-latest, snapshot SCD (filter on as_of_date).
-- ---------------------------------------------------------------------------
CREATE TABLE product_price_history (
    id                  BIGSERIAL       PRIMARY KEY,
    product_id          INTEGER         NOT NULL REFERENCES product(id),
    price               NUMERIC(10,2)   NOT NULL CHECK (price >= 0),
    effective_date      DATE            NOT NULL,
    expiration_date     DATE,
    is_current          BOOLEAN         NOT NULL,
    UNIQUE (product_id, effective_date),
    CHECK (expiration_date IS NULL OR expiration_date > effective_date)
);

CREATE INDEX product_price_current_idx ON product_price_history(product_id, is_current);
CREATE INDEX product_price_effective_idx ON product_price_history(product_id, effective_date);

COMMENT ON TABLE  product_price_history IS 'SCD-2 price history. is_current=true marks the active row per product.';
COMMENT ON COLUMN product_price_history.effective_date  IS 'Date this price became active.';
COMMENT ON COLUMN product_price_history.expiration_date IS 'Date this price was superseded. Null for the current row.';
COMMENT ON COLUMN product_price_history.is_current      IS 'Convenience flag. Equivalent to expiration_date IS NULL.';

-- ---------------------------------------------------------------------------
-- customer
-- Dimension table. One row per customer.
-- Demo: concat (first+last), split/substring (email), mask, hash, bucket (LTV).
-- ---------------------------------------------------------------------------
CREATE TABLE customer (
    id                  INTEGER         PRIMARY KEY,
    email               VARCHAR(320)    NOT NULL UNIQUE,
    first_name          VARCHAR(100)    NOT NULL,
    last_name           VARCHAR(100)    NOT NULL,
    signup_date         DATE            NOT NULL,
    status              VARCHAR(20)     NOT NULL CHECK (status IN ('active', 'inactive', 'banned')),
    country_code        CHAR(2)         NOT NULL,
    postal_code         VARCHAR(20),
    lifetime_value_usd  NUMERIC(12,2)   NOT NULL DEFAULT 0 CHECK (lifetime_value_usd >= 0),
    date_of_birth       DATE,
    gender              VARCHAR(10)
);

COMMENT ON TABLE  customer IS 'Customer dimension. PII: email, first_name, last_name. Demo: mask, hash, bucket.';
COMMENT ON COLUMN customer.country_code IS 'ISO 3166-1 alpha-2.';
COMMENT ON COLUMN customer.lifetime_value_usd IS 'Materialised aggregate of order_header.total_amount over time. Demo: bucket bands.';

-- ---------------------------------------------------------------------------
-- customer_segment_assignment
-- SCD-2 history of segment membership.
-- Demo: latest_only SCD synthesised dedupe, lookup-latest (current segment).
-- ---------------------------------------------------------------------------
CREATE TABLE customer_segment_assignment (
    id                  BIGSERIAL       PRIMARY KEY,
    customer_id         INTEGER         NOT NULL REFERENCES customer(id),
    segment_code        VARCHAR(20)     NOT NULL CHECK (segment_code IN ('new', 'growing', 'vip', 'dormant', 'churned')),
    effective_from      DATE            NOT NULL,
    effective_to        DATE,
    UNIQUE (customer_id, effective_from),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE INDEX cust_segment_customer_idx ON customer_segment_assignment(customer_id, effective_from);

COMMENT ON TABLE  customer_segment_assignment IS 'SCD-2 segment history. effective_to=NULL marks the current segment per customer.';

-- ---------------------------------------------------------------------------
-- order_header
-- Fact table. One row per order.
-- Demo: grouping_keys, windowing (rank by spend, first-order flag), filter.
-- ---------------------------------------------------------------------------
CREATE TABLE order_header (
    id                      INTEGER         PRIMARY KEY,
    customer_id             INTEGER         NOT NULL REFERENCES customer(id),
    order_date              DATE            NOT NULL,
    status                  VARCHAR(20)     NOT NULL CHECK (status IN ('pending', 'processing', 'shipped', 'delivered', 'cancelled', 'returned')),
    total_amount            NUMERIC(12,2)   NOT NULL CHECK (total_amount >= 0),
    payment_method          VARCHAR(20)     CHECK (payment_method IN ('card', 'paypal', 'wire', 'cash_on_delivery', 'gift_card')),
    shipping_country_code   CHAR(2),
    channel                 VARCHAR(20)     NOT NULL CHECK (channel IN ('web', 'mobile', 'store', 'partner'))
);

CREATE INDEX order_customer_idx ON order_header(customer_id);
CREATE INDEX order_date_idx     ON order_header(order_date);

COMMENT ON TABLE  order_header IS 'Order fact. status is the latest status (see order_status_history for full timeline).';

-- ---------------------------------------------------------------------------
-- order_item
-- Junction / line-item table. One row per (order, product line).
-- Demo: auto-bridge (customer <-> product via order_header -> order_item),
--       lookup-aggregate (qty per customer), arithmetic (line_total).
-- ---------------------------------------------------------------------------
CREATE TABLE order_item (
    id              BIGSERIAL       PRIMARY KEY,
    order_id        INTEGER         NOT NULL REFERENCES order_header(id),
    product_id      INTEGER         NOT NULL REFERENCES product(id),
    quantity        INTEGER         NOT NULL CHECK (quantity > 0),
    unit_price      NUMERIC(10,2)   NOT NULL CHECK (unit_price >= 0),
    discount_pct    NUMERIC(5,2)    NOT NULL DEFAULT 0 CHECK (discount_pct BETWEEN 0 AND 100),
    line_total      NUMERIC(12,2)   NOT NULL CHECK (line_total >= 0)
);

CREATE INDEX order_item_order_idx   ON order_item(order_id);
CREATE INDEX order_item_product_idx ON order_item(product_id);

COMMENT ON TABLE  order_item IS 'Order line. line_total = quantity * unit_price * (1 - discount_pct/100).';

-- ---------------------------------------------------------------------------
-- order_status_history
-- Temporal bridge. One row per status change.
-- Demo: temporal-bridge auto-wrap (ROW_NUMBER OVER ... = 1 by status_changed_at),
--       lookup-exists (has_returned_order where status='returned'),
--       multiplication-warning when included as a non-bridge mapped source.
-- ---------------------------------------------------------------------------
CREATE TABLE order_status_history (
    id                  BIGSERIAL       PRIMARY KEY,
    order_id            INTEGER         NOT NULL REFERENCES order_header(id),
    status              VARCHAR(20)     NOT NULL CHECK (status IN ('pending', 'processing', 'shipped', 'delivered', 'cancelled', 'returned')),
    status_changed_at   TIMESTAMP       NOT NULL,
    actor               VARCHAR(50),
    note                TEXT,
    UNIQUE (order_id, status_changed_at)
);

CREATE INDEX order_status_history_order_idx ON order_status_history(order_id, status_changed_at);

COMMENT ON TABLE  order_status_history IS 'Status timeline per order. Bridges order_header to itself; latest row per order_id is the current status.';
