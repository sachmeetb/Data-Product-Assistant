-- =============================================================================
-- Data Workbench sample dataset — Products & Sales
-- File 2 of 3: seed data
--
-- Strategy: dimension rows hand-authored; transactional rows generated
-- deterministically so two loads produce identical state.
-- =============================================================================

SET search_path TO products_sales, public;

-- ---------------------------------------------------------------------------
-- category — 20 rows, 3-level hierarchy
-- ---------------------------------------------------------------------------
INSERT INTO category (id, parent_id, name, depth, is_active) VALUES
    ( 1, NULL, 'Electronics',  0, true),
    ( 2,    1, 'Computers',    1, true),
    ( 3,    2, 'Laptops',      2, true),
    ( 4,    2, 'Desktops',     2, true),
    ( 5,    2, 'Tablets',      2, true),
    ( 6,    1, 'Audio',        1, true),
    ( 7,    6, 'Headphones',   2, true),
    ( 8,    6, 'Speakers',     2, true),
    ( 9,    1, 'Wearables',    1, true),
    (10, NULL, 'Apparel',      0, true),
    (11,   10, 'Mens',         1, true),
    (12,   11, 'Shirts',       2, true),
    (13,   11, 'Pants',        2, true),
    (14,   10, 'Womens',       1, true),
    (15,   14, 'Tops',         2, true),
    (16,   14, 'Dresses',      2, true),
    (17, NULL, 'Home',         0, true),
    (18,   17, 'Kitchen',      1, true),
    (19,   17, 'Furniture',    1, true),
    (20, NULL, 'Books',        0, true);

-- ---------------------------------------------------------------------------
-- product — 50 rows distributed across categories
-- ---------------------------------------------------------------------------
INSERT INTO product (id, sku, name, category_id, brand, list_price, cost, weight_kg, color, size_code, active_flag, launched_at) VALUES
    -- Laptops (category 3)
    ( 1, 'LPT-001', 'Aero 13 Ultrabook',          3, 'Aero',    1299.00,  780.00, 1.250, 'Silver',  '13in',   true,  '2022-03-15'),
    ( 2, 'LPT-002', 'Aero 15 Pro',                3, 'Aero',    1899.00, 1150.00, 1.780, 'Space Gray','15in', true,  '2022-09-01'),
    ( 3, 'LPT-003', 'Forge Gaming Rig 17',        3, 'Forge',   2499.00, 1620.00, 2.650, 'Black',   '17in',   true,  '2023-05-10'),
    ( 4, 'LPT-004', 'Budget Laptop 14',           3, 'Acme',     499.00,  310.00, 1.520, 'Gray',    '14in',   true,  '2021-11-20'),
    -- Desktops (category 4)
    ( 5, 'DSK-001', 'Tower Workstation X1',       4, 'Forge',   1799.00, 1080.00, 9.500, 'Black',   NULL,     true,  '2023-01-15'),
    ( 6, 'DSK-002', 'Mini PC Cube',               4, 'Aero',     649.00,  410.00, 1.100, 'Silver',  NULL,     true,  '2023-06-20'),
    -- Tablets (category 5)
    ( 7, 'TAB-001', 'Slate 11 Tablet',            5, 'Aero',     799.00,  470.00, 0.520, 'Silver',  '11in',   true,  '2022-06-01'),
    ( 8, 'TAB-002', 'Slate 13 Pro',               5, 'Aero',    1099.00,  660.00, 0.640, 'Space Gray','13in', true,  '2023-04-15'),
    -- Headphones (category 7)
    ( 9, 'AUD-001', 'Echo Wireless Headphones',   7, 'Echo',     249.00,  120.00, 0.280, 'Black',   NULL,     true,  '2022-01-10'),
    (10, 'AUD-002', 'Echo Studio Over-Ear',       7, 'Echo',     399.00,  220.00, 0.350, 'White',   NULL,     true,  '2022-08-05'),
    (11, 'AUD-003', 'Sport Earbuds Pro',          7, 'Echo',     179.00,   85.00, 0.045, 'Blue',    NULL,     true,  '2023-02-28'),
    (12, 'AUD-004', 'Budget Headphones',          7, 'Acme',      59.00,   28.00, 0.220, 'Black',   NULL,     true,  '2021-05-01'),
    -- Speakers (category 8)
    (13, 'SPK-001', 'Echo Soundbar 5.1',          8, 'Echo',     699.00,  410.00, 4.200, 'Black',   NULL,     true,  '2023-03-12'),
    (14, 'SPK-002', 'Portable Bluetooth Speaker', 8, 'Echo',     129.00,   65.00, 0.580, 'Red',     NULL,     true,  '2022-04-20'),
    (15, 'SPK-003', 'Bookshelf Speaker Pair',     8, 'Vibe',     449.00,  260.00, 6.500, 'Walnut',  NULL,     true,  '2023-07-08'),
    -- Wearables (category 9)
    (16, 'WRB-001', 'Pulse Smartwatch 4',         9, 'Pulse',    349.00,  185.00, 0.045, 'Black',   '42mm',   true,  '2023-09-15'),
    (17, 'WRB-002', 'Pulse Fitness Band',         9, 'Pulse',     99.00,   42.00, 0.030, 'Black',   NULL,     true,  '2022-11-01'),
    (18, 'WRB-003', 'Pulse Smartwatch 3',         9, 'Pulse',    279.00,  150.00, 0.045, 'Silver',  '42mm',   false, '2021-09-20'),
    -- Mens Shirts (category 12)
    (19, 'MSH-001', 'Classic Oxford Shirt',      12, 'Tailor',    79.00,   28.00, 0.300, 'White',   'M',      true,  '2020-04-10'),
    (20, 'MSH-002', 'Linen Casual Shirt',        12, 'Tailor',    89.00,   34.00, 0.280, 'Navy',    'L',      true,  '2021-06-05'),
    (21, 'MSH-003', 'Performance Polo',          12, 'Sportix',   59.00,   22.00, 0.250, 'Black',   'M',      true,  '2022-03-22'),
    (22, 'MSH-004', 'Flannel Plaid',             12, 'Tailor',    69.00,   28.00, 0.420, 'Red',     'L',      true,  '2020-10-15'),
    -- Mens Pants (category 13)
    (23, 'MPT-001', 'Stretch Chinos',            13, 'Tailor',    99.00,   40.00, 0.520, 'Khaki',   '32',     true,  '2020-08-12'),
    (24, 'MPT-002', 'Slim Fit Jeans',            13, 'Denim Co', 119.00,   48.00, 0.620, 'Indigo',  '32',     true,  '2021-09-01'),
    (25, 'MPT-003', 'Performance Joggers',       13, 'Sportix',   89.00,   34.00, 0.450, 'Gray',    'M',      true,  '2022-05-18'),
    -- Womens Tops (category 15)
    (26, 'WTP-001', 'Silk Blouse',               15, 'Atelier',  149.00,   55.00, 0.180, 'Cream',   'S',      true,  '2021-03-20'),
    (27, 'WTP-002', 'Cotton Tee',                15, 'Atelier',   39.00,   14.00, 0.180, 'White',   'M',      true,  '2020-06-10'),
    (28, 'WTP-003', 'Knit Sweater',              15, 'Atelier',  119.00,   46.00, 0.520, 'Beige',   'M',      true,  '2022-10-22'),
    -- Womens Dresses (category 16)
    (29, 'WDR-001', 'Wrap Midi Dress',           16, 'Atelier',  179.00,   72.00, 0.380, 'Navy',    'M',      true,  '2022-04-08'),
    (30, 'WDR-002', 'A-Line Summer Dress',       16, 'Atelier',  129.00,   50.00, 0.320, 'Floral',  'S',      true,  '2023-05-15'),
    (31, 'WDR-003', 'Evening Gown',              16, 'Atelier',  349.00,  140.00, 0.580, 'Black',   'M',      true,  '2021-11-30'),
    -- Kitchen (category 18)
    (32, 'KTC-001', 'Stand Mixer 5L',            18, 'Hearth',   449.00,  220.00, 7.800, 'Red',     NULL,     true,  '2022-02-14'),
    (33, 'KTC-002', 'Espresso Machine',          18, 'Hearth',   799.00,  410.00, 9.200, 'Stainless', NULL,   true,  '2022-09-30'),
    (34, 'KTC-003', 'Chef Knife 8in',            18, 'Hearth',    89.00,   34.00, 0.220, NULL,      NULL,     true,  '2021-04-22'),
    (35, 'KTC-004', 'Cast Iron Skillet 12in',    18, 'Hearth',    69.00,   24.00, 3.500, 'Black',   NULL,     true,  '2020-08-10'),
    (36, 'KTC-005', 'Cookware Set 10pc',         18, 'Hearth',   349.00,  170.00, 6.800, 'Stainless', NULL,   true,  '2023-01-08'),
    -- Furniture (category 19)
    (37, 'FRN-001', 'Solid Oak Dining Table',    19, 'Heritage', 1499.00,  720.00, 42.000, 'Oak',    NULL,    true,  '2022-07-15'),
    (38, 'FRN-002', 'Linen Sofa 3-Seater',       19, 'Heritage', 1899.00,  920.00, 58.000, 'Beige',  NULL,    true,  '2023-03-22'),
    (39, 'FRN-003', 'Ergonomic Office Chair',    19, 'Posture',   599.00,  280.00, 18.000, 'Black',  NULL,    true,  '2022-11-08'),
    (40, 'FRN-004', 'Bookshelf 5-Tier',          19, 'Heritage',  349.00,  165.00, 24.000, 'Walnut', NULL,    true,  '2021-08-30'),
    -- Books (category 20)
    (41, 'BOK-001', 'Data Engineering Handbook', 20, 'Press',     49.00,   18.00, 0.580, NULL,      NULL,     true,  '2023-02-15'),
    (42, 'BOK-002', 'Modern SQL Cookbook',       20, 'Press',     39.00,   15.00, 0.520, NULL,      NULL,     true,  '2022-09-10'),
    (43, 'BOK-003', 'Statistics for Engineers',  20, 'Press',     59.00,   22.00, 0.680, NULL,      NULL,     true,  '2021-05-08'),
    (44, 'BOK-004', 'The Pragmatic Engineer',    20, 'Press',     34.00,   12.00, 0.420, NULL,      NULL,     true,  '2023-08-20'),
    (45, 'BOK-005', 'Cooking Through the Year',  20, 'Press',     29.00,   11.00, 1.200, NULL,      NULL,     true,  '2020-11-15'),
    -- Discontinued / inactive products
    (46, 'LPT-099', 'Legacy Laptop 12',           3, 'Acme',     399.00,  240.00, 1.420, 'Black',   '12in',   false, '2019-03-10'),
    (47, 'AUD-099', 'Wired Headphones (legacy)',  7, 'Acme',      29.00,   12.00, 0.180, 'Black',   NULL,     false, '2018-11-15'),
    (48, 'MSH-099', 'Discontinued Polo',         12, 'Tailor',    49.00,   18.00, 0.250, 'Yellow',  'M',      false, '2019-06-22'),
    -- Additional varied items
    (49, 'KTC-006', 'Bread Maker',               18, 'Hearth',   179.00,   80.00, 5.200, NULL,      NULL,     true,  '2023-04-18'),
    (50, 'WRB-004', 'Premium Smartwatch Gold',    9, 'Pulse',    899.00,  420.00, 0.060, 'Gold',    '42mm',   true,  '2024-01-10');

-- ---------------------------------------------------------------------------
-- product_price_history — multiple price points per product
-- Strategy: for products launched before 2023-01-01, emit 2 historical rows
-- + 1 current row (3 total). For products launched after, emit 1 + 1 (2 total).
-- Prices drift by deterministic +/- 5-12% steps so demos see real ranges.
-- ---------------------------------------------------------------------------
INSERT INTO product_price_history (product_id, price, effective_date, expiration_date, is_current)
SELECT
    p.id,
    ROUND(p.list_price * 0.88, 2),
    p.launched_at,
    p.launched_at + INTERVAL '8 months',
    false
FROM product p
WHERE p.launched_at < DATE '2023-01-01';

INSERT INTO product_price_history (product_id, price, effective_date, expiration_date, is_current)
SELECT
    p.id,
    ROUND(p.list_price * 0.94, 2),
    p.launched_at + INTERVAL '8 months',
    p.launched_at + INTERVAL '18 months',
    false
FROM product p
WHERE p.launched_at < DATE '2023-01-01';

INSERT INTO product_price_history (product_id, price, effective_date, expiration_date, is_current)
SELECT
    p.id,
    p.list_price,
    GREATEST(p.launched_at, p.launched_at + INTERVAL '18 months'),
    NULL,
    true
FROM product p
WHERE p.launched_at < DATE '2023-01-01';

-- Products launched 2023-01-01 or later: shorter history (intro price -> current)
INSERT INTO product_price_history (product_id, price, effective_date, expiration_date, is_current)
SELECT
    p.id,
    ROUND(p.list_price * 0.92, 2),
    p.launched_at,
    p.launched_at + INTERVAL '6 months',
    false
FROM product p
WHERE p.launched_at >= DATE '2023-01-01';

INSERT INTO product_price_history (product_id, price, effective_date, expiration_date, is_current)
SELECT
    p.id,
    p.list_price,
    p.launched_at + INTERVAL '6 months',
    NULL,
    true
FROM product p
WHERE p.launched_at >= DATE '2023-01-01';

-- ---------------------------------------------------------------------------
-- customer — 30 hand-authored customers across geographies and statuses
-- ---------------------------------------------------------------------------
INSERT INTO customer (id, email, first_name, last_name, signup_date, status, country_code, postal_code, lifetime_value_usd, date_of_birth, gender) VALUES
    ( 1, 'alice.bennett@example.com',     'Alice',     'Bennett',    '2020-02-14', 'active',   'US', '94107',    0.00, '1988-04-22', 'female'),
    ( 2, 'bob.carter@example.com',        'Bob',       'Carter',     '2020-05-09', 'active',   'US', '10011',    0.00, '1976-11-30', 'male'),
    ( 3, 'caroline.davis@example.com',    'Caroline',  'Davis',      '2020-07-22', 'active',   'GB', 'SW1A 1AA', 0.00, '1992-01-12', 'female'),
    ( 4, 'david.evans@example.com',       'David',     'Evans',      '2020-11-03', 'active',   'GB', 'EC1A 1BB', 0.00, '1985-07-18', 'male'),
    ( 5, 'elena.fischer@example.com',     'Elena',     'Fischer',    '2021-01-28', 'active',   'DE', '10115',    0.00, '1990-09-05', 'female'),
    ( 6, 'felix.gruber@example.com',      'Felix',     'Gruber',     '2021-03-15', 'active',   'DE', '80331',    0.00, '1982-03-19', 'male'),
    ( 7, 'gabriel.henri@example.com',     'Gabriel',   'Henri',      '2021-04-20', 'active',   'FR', '75001',    0.00, '1995-12-08', 'male'),
    ( 8, 'helene.icard@example.com',      'Helene',    'Icard',      '2021-06-18', 'active',   'FR', '69001',    0.00, '1987-05-22', 'female'),
    ( 9, 'isamu.jin@example.com',         'Isamu',     'Jin',        '2021-08-12', 'active',   'JP', '100-0001', 0.00, '1979-10-14', 'male'),
    (10, 'junko.kato@example.com',        'Junko',     'Kato',       '2021-09-05', 'active',   'JP', '530-0001', 0.00, '1991-02-28', 'female'),
    (11, 'kevin.lawson@example.com',      'Kevin',     'Lawson',     '2021-10-30', 'active',   'CA', 'M5V 3A8',  0.00, '1984-08-11', 'male'),
    (12, 'laura.morris@example.com',      'Laura',     'Morris',     '2021-12-18', 'active',   'CA', 'V6B 4N9',  0.00, '1993-06-25', 'female'),
    (13, 'mark.nguyen@example.com',       'Mark',      'Nguyen',     '2022-01-22', 'active',   'AU', '2000',     0.00, '1989-11-09', 'male'),
    (14, 'nina.ortiz@example.com',        'Nina',      'Ortiz',      '2022-03-10', 'active',   'ES', '28013',    0.00, '1986-04-17', 'female'),
    (15, 'oscar.perez@example.com',       'Oscar',     'Perez',      '2022-04-28', 'active',   'MX', '06600',    0.00, '1978-09-30', 'male'),
    (16, 'priya.raman@example.com',       'Priya',     'Raman',      '2022-06-12', 'active',   'IN', '110001',   0.00, '1994-03-04', 'female'),
    (17, 'rafael.silva@example.com',      'Rafael',    'Silva',      '2022-07-30', 'active',   'BR', '01310-100',0.00, '1983-12-21', 'male'),
    (18, 'sarah.taylor@example.com',      'Sarah',     'Taylor',     '2022-09-15', 'active',   'US', '90210',    0.00, '1990-07-08', 'female'),
    (19, 'tom.underwood@example.com',     'Tom',       'Underwood',  '2022-11-04', 'active',   'US', '02108',    0.00, '1975-05-15', 'male'),
    (20, 'uma.vasquez@example.com',       'Uma',       'Vasquez',    '2023-01-18', 'active',   'US', '60601',    0.00, '1996-10-02', 'female'),
    (21, 'victor.wong@example.com',       'Victor',    'Wong',       '2023-02-26', 'active',   'GB', 'M1 1AA',   0.00, '1981-08-29', 'male'),
    (22, 'wendy.xu@example.com',          'Wendy',     'Xu',         '2023-04-10', 'active',   'GB', 'B1 1AA',   0.00, '1988-01-16', 'female'),
    (23, 'xavier.young@example.com',      'Xavier',    'Young',      '2023-05-22', 'active',   'FR', '13001',    0.00, '1980-04-03', 'male'),
    (24, 'yasmin.zola@example.com',       'Yasmin',    'Zola',       '2023-08-08', 'active',   'US', '78701',    0.00, '1997-02-19', 'female'),
    (25, 'zach.albright@example.com',     'Zach',      'Albright',   '2023-11-19', 'active',   'US', '98101',    0.00, '1992-06-27', 'male'),
    (26, 'amy.briggs@example.com',        'Amy',       'Briggs',     '2024-02-04', 'active',   'CA', 'H2Y 1C6',  0.00, '1995-09-14', 'female'),
    (27, 'ben.collins@example.com',       'Ben',       'Collins',    '2024-04-22', 'active',   'AU', '3000',     0.00, '1991-12-31', 'male'),
    -- Inactive / banned tail
    (28, 'old.account@example.com',       'Old',       'Account',    '2020-01-05', 'inactive', 'US', '12345',    0.00, '1970-01-01', 'male'),
    (29, 'inactive.user@example.com',     'Inactive',  'User',       '2020-08-18', 'inactive', 'US', '67890',    0.00, '1985-05-05', 'female'),
    (30, 'spam.account@example.com',      'Spam',      'Account',    '2023-03-10', 'banned',   'US', '00000',    0.00, NULL,         NULL);

-- ---------------------------------------------------------------------------
-- customer_segment_assignment — SCD-2 history of segment membership
-- Every customer starts in 'new'; long-tenured customers progress.
-- Customer 30 (banned) has just one 'churned' assignment.
-- ---------------------------------------------------------------------------
-- First segment: 'new' from signup
INSERT INTO customer_segment_assignment (customer_id, segment_code, effective_from, effective_to)
SELECT id, 'new', signup_date,
       CASE
         WHEN status = 'banned' THEN signup_date + INTERVAL '6 months'
         WHEN id <= 20 THEN signup_date + INTERVAL '6 months'
         ELSE NULL  -- recent customers still 'new'
       END
FROM customer;

-- Second segment: 'growing' for customers who graduated from 'new'
INSERT INTO customer_segment_assignment (customer_id, segment_code, effective_from, effective_to)
SELECT id, 'growing', signup_date + INTERVAL '6 months',
       CASE
         WHEN id <= 10 THEN signup_date + INTERVAL '18 months'
         ELSE NULL
       END
FROM customer
WHERE id <= 20 AND status != 'banned';

-- Third segment: 'vip' for top-tier long-tenured
INSERT INTO customer_segment_assignment (customer_id, segment_code, effective_from, effective_to)
SELECT id, 'vip', signup_date + INTERVAL '18 months', NULL
FROM customer
WHERE id IN (1, 2, 3, 5, 7, 9);

-- 'dormant' for some inactive
INSERT INTO customer_segment_assignment (customer_id, segment_code, effective_from, effective_to)
SELECT id, 'dormant', signup_date + INTERVAL '18 months', NULL
FROM customer
WHERE id IN (4, 6, 8, 10);

-- 'churned' for the banned account
INSERT INTO customer_segment_assignment (customer_id, segment_code, effective_from, effective_to)
SELECT id, 'churned', signup_date + INTERVAL '6 months', NULL
FROM customer
WHERE status = 'banned';

-- ---------------------------------------------------------------------------
-- order_header — 150 orders generated deterministically
-- ---------------------------------------------------------------------------
INSERT INTO order_header (id, customer_id, order_date, status, total_amount, payment_method, shipping_country_code, channel)
SELECT
    n AS id,
    ((n * 7 + 3) % 30) + 1 AS customer_id,
    DATE '2022-06-01' + ((n * 5) % 800) AS order_date,
    CASE
        WHEN n % 20 = 0 THEN 'returned'
        WHEN n % 17 = 0 THEN 'cancelled'
        WHEN n % 13 = 0 THEN 'shipped'
        WHEN n % 11 = 0 THEN 'processing'
        WHEN n % 31 = 0 THEN 'pending'
        ELSE 'delivered'
    END AS status,
    0.00 AS total_amount,  -- recomputed below from order_item.line_total
    CASE n % 10
        WHEN 0 THEN 'paypal'
        WHEN 1 THEN 'wire'
        WHEN 2 THEN 'gift_card'
        WHEN 3 THEN 'cash_on_delivery'
        ELSE 'card'
    END AS payment_method,
    NULL AS shipping_country_code,  -- recopied below from customer.country_code
    CASE n % 10
        WHEN 0 THEN 'partner'
        WHEN 1 THEN 'store'
        WHEN 2 THEN 'store'
        WHEN 3 THEN 'mobile'
        WHEN 4 THEN 'mobile'
        WHEN 5 THEN 'mobile'
        ELSE 'web'
    END AS channel
FROM generate_series(1, 150) AS n;

UPDATE order_header oh
SET shipping_country_code = c.country_code
FROM customer c
WHERE oh.customer_id = c.id;

-- ---------------------------------------------------------------------------
-- order_item — 1-4 line items per order
-- Strategy: cross-join orders with a per-order item index, where the index
-- count varies by order id. product_id picked deterministically from id math.
-- ---------------------------------------------------------------------------
INSERT INTO order_item (order_id, product_id, quantity, unit_price, discount_pct, line_total)
SELECT
    oh.id AS order_id,
    ((oh.id * 11 + idx * 13) % 50) + 1 AS product_id,
    ((oh.id + idx) % 4) + 1 AS quantity,
    p.list_price AS unit_price,
    CASE (oh.id + idx) % 8
        WHEN 0 THEN 10.00
        WHEN 1 THEN 5.00
        WHEN 2 THEN 15.00
        WHEN 3 THEN 0.00
        WHEN 4 THEN 0.00
        WHEN 5 THEN 0.00
        WHEN 6 THEN 20.00
        ELSE 0.00
    END AS discount_pct,
    0.00 AS line_total  -- filled by UPDATE below
FROM order_header oh
CROSS JOIN LATERAL generate_series(1, ((oh.id % 4) + 1)) AS g(idx)
JOIN product p ON p.id = ((oh.id * 11 + idx * 13) % 50) + 1;

UPDATE order_item
SET line_total = ROUND(quantity * unit_price * (1 - discount_pct / 100.0), 2);

UPDATE order_header oh
SET total_amount = sub.s
FROM (
    SELECT order_id, SUM(line_total) AS s
    FROM order_item
    GROUP BY order_id
) sub
WHERE oh.id = sub.order_id;

-- ---------------------------------------------------------------------------
-- order_status_history — 1-5 status events per order, depending on final status
-- ---------------------------------------------------------------------------
-- Every order starts 'pending'
INSERT INTO order_status_history (order_id, status, status_changed_at, actor, note)
SELECT id, 'pending', order_date::timestamp, 'system', 'Order placed'
FROM order_header;

-- Orders that progressed past pending: add 'processing' 1 day later
INSERT INTO order_status_history (order_id, status, status_changed_at, actor, note)
SELECT id, 'processing', order_date::timestamp + INTERVAL '1 day', 'fulfillment', 'Payment cleared, picking'
FROM order_header
WHERE status IN ('processing', 'shipped', 'delivered', 'cancelled', 'returned');

-- Orders that shipped: add 'shipped' 3 days after order_date (skip cancelled)
INSERT INTO order_status_history (order_id, status, status_changed_at, actor, note)
SELECT id, 'shipped', order_date::timestamp + INTERVAL '3 days', 'fulfillment', 'Handed to carrier'
FROM order_header
WHERE status IN ('shipped', 'delivered', 'returned');

-- Orders delivered or returned: add 'delivered' 6 days after order_date
INSERT INTO order_status_history (order_id, status, status_changed_at, actor, note)
SELECT id, 'delivered', order_date::timestamp + INTERVAL '6 days', 'carrier', 'Signed for at destination'
FROM order_header
WHERE status IN ('delivered', 'returned');

-- Cancelled: add 'cancelled' 2 days after processing
INSERT INTO order_status_history (order_id, status, status_changed_at, actor, note)
SELECT id, 'cancelled', order_date::timestamp + INTERVAL '2 days', 'support', 'Customer requested cancellation'
FROM order_header
WHERE status = 'cancelled';

-- Returned: add 'returned' 14 days after delivered
INSERT INTO order_status_history (order_id, status, status_changed_at, actor, note)
SELECT id, 'returned', order_date::timestamp + INTERVAL '20 days', 'support', 'Return RMA processed'
FROM order_header
WHERE status = 'returned';

-- ---------------------------------------------------------------------------
-- Recompute customer.lifetime_value_usd from delivered + returned order totals
-- (returned orders contribute net zero, but kept simple: only count delivered)
-- ---------------------------------------------------------------------------
UPDATE customer c
SET lifetime_value_usd = COALESCE(sub.s, 0.00)
FROM (
    SELECT customer_id, SUM(total_amount) AS s
    FROM order_header
    WHERE status = 'delivered'
    GROUP BY customer_id
) sub
WHERE c.id = sub.customer_id;

-- ---------------------------------------------------------------------------
-- Integrity checks — raise NOTICE if anything looks wrong
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    cnt INTEGER;
BEGIN
    SELECT COUNT(*) INTO cnt FROM product p WHERE NOT EXISTS (SELECT 1 FROM category c WHERE c.id = p.category_id);
    IF cnt > 0 THEN RAISE EXCEPTION 'product has % rows with dangling category_id', cnt; END IF;

    SELECT COUNT(*) INTO cnt FROM order_header oh WHERE NOT EXISTS (SELECT 1 FROM customer c WHERE c.id = oh.customer_id);
    IF cnt > 0 THEN RAISE EXCEPTION 'order_header has % rows with dangling customer_id', cnt; END IF;

    SELECT COUNT(*) INTO cnt FROM order_item oi WHERE NOT EXISTS (SELECT 1 FROM order_header oh WHERE oh.id = oi.order_id);
    IF cnt > 0 THEN RAISE EXCEPTION 'order_item has % rows with dangling order_id', cnt; END IF;

    SELECT COUNT(*) INTO cnt FROM order_item oi WHERE NOT EXISTS (SELECT 1 FROM product p WHERE p.id = oi.product_id);
    IF cnt > 0 THEN RAISE EXCEPTION 'order_item has % rows with dangling product_id', cnt; END IF;

    -- Each product must have exactly one is_current=true row
    SELECT COUNT(*) INTO cnt FROM (
        SELECT product_id, COUNT(*) AS c FROM product_price_history WHERE is_current GROUP BY product_id HAVING COUNT(*) <> 1
    ) sub;
    IF cnt > 0 THEN RAISE EXCEPTION 'product_price_history has % products without exactly one current row', cnt; END IF;

    RAISE NOTICE 'Integrity checks passed.';
END $$;

-- ---------------------------------------------------------------------------
-- Volume summary
-- ---------------------------------------------------------------------------
SELECT 'category'                     AS table_name, COUNT(*) AS row_count FROM category
UNION ALL SELECT 'product',                          COUNT(*) FROM product
UNION ALL SELECT 'product_price_history',            COUNT(*) FROM product_price_history
UNION ALL SELECT 'customer',                         COUNT(*) FROM customer
UNION ALL SELECT 'customer_segment_assignment',      COUNT(*) FROM customer_segment_assignment
UNION ALL SELECT 'order_header',                     COUNT(*) FROM order_header
UNION ALL SELECT 'order_item',                       COUNT(*) FROM order_item
UNION ALL SELECT 'order_status_history',             COUNT(*) FROM order_status_history
ORDER BY table_name;
