# Reasoning guide — worked adjudications

Worked examples of the entity/kind reasoning the matcher applies. These are
illustrative; always decide from the descriptions actually supplied in the input.

## 1. Entity disambiguation (`name` → customer, not nation)

Attribute `name` (concept "customer name"). Candidates:

| table | column | column_description | score | reason |
|---|---|---|---|---|
| nation | n_name | "The name of the nation." | 96 | chosen |
| customer | c_name | "Customer full name for display." | 92 | wrong_entity |

The deterministic pick (`nation.n_name`) wins only on a surface-string quirk. The
attribute is about the **customer** entity, and `c_name`'s description literally says
"Customer full name". → **match `customer.c_name`**.

## 2. Kind disambiguation (category ≠ identifier)

Attribute `customer_type` (concept "customer segment/category"). Candidates:

| table | column | column_description | score | reason |
|---|---|---|---|---|
| customer | c_customer_id | "Surrogate key for the customer." | 71 | identifier_mismatch |
| customer | c_mktsegment | "Market segment of the customer." | 66 | below_threshold |

An identifier can never stand in for a category, so `c_customer_id` is wrong despite
its higher score. `c_mktsegment` is a few points below threshold but its description
IS the segment. → **match `customer.c_mktsegment`** (a rescue).

If the ONLY candidate were `c_customer_id`, the answer is **gap** — a surrogate key
is not a customer type.

## 3. Genuine gap

Attribute `loyalty_tier` (concept "customer loyalty tier"). Candidates:

| table | column | column_description | score | reason |
|---|---|---|---|---|
| customer | c_acctbal | "Account balance." | 64 | below_threshold |

No candidate means "loyalty tier". Account balance is a different concept. → **gap**.

## 4. Confirm the deterministic pick

Attribute `email` (concept "email address"), current match `customer.c_email`
("Customer contact email"), runner-up far behind. Nothing to improve → **omit the
attribute** (leave the deterministic pick untouched) or restate it as a `match` on
the same column. Either is fine; omitting is cheaper.
