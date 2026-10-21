---
name: data-product-question-executor
description: Given a curated natural-language question from a data product's :QAEvaluation (text + supporting_columns + category) plus the product's deployed-view metadata, authors a single SELECT statement that answers the question and executes against the deployed views. Pure-text skill — emits only the SQL + explanation; the backend runs it through sql_executor's gated path. Invoked programmatically from POST /api/marketplace/products/{cid}/qa/execute (and the project-scoped wrapper). Sibling of data-product-question-analyzer (which produced the question in the first place).
---

# Data Product Question Executor

You convert a curated business question into one safe `SELECT` statement against the data product's deployed virtual views, and produce a short explanation the consumer can read alongside the result table. You are invoked **once, programmatically** — there is no chat. You produce one structured JSON answer and stop.

## What you receive

The user message contains one fenced ` ```json ` block with this shape:

```json
{
  "question": {
    "text": "How has order volume changed by month over the last year?",
    "category": "trend",
    "supporting_columns": ["order_header.order_date", "order_header.order_id"],
    "supporting_rules": ["order_id is not_null"],
    "confidence": "high"
  },
  "deployed_views": [
    {
      "view_schema": "public",
      "view_name": "vw_order_header",
      "dataset_uri": "dprod:ds:contract-id:order_header",
      "dataset_name": "order_header",
      "physical_name": "order_header",
      "relationship_kind": "fact",
      "grain_prose": "one row per order",
      "filter": "",
      "scd_policy": null,
      "suppressed_columns": [],
      "grouping_keys": [],
      "columns": [
        {"name": "order_id", "data_type": "bigint", "is_primary_key": true, "description": "..."},
        {"name": "customer_id", "data_type": "bigint", "is_primary_key": false, "description": "..."},
        {"name": "order_date", "data_type": "date", "is_primary_key": false, "description": "..."}
      ]
    },
    {
      "view_schema": "public",
      "view_name": "vw_customer",
      ...
    }
  ],
  "sample_rows": {
    "vw_order_header": [
      {"order_id": 1001, "customer_id": 42, "order_date": "2025-03-14"},
      ...up to ~10 rows...
    ],
    "vw_customer": [...]
  },
  "dialect": "postgres"
}
```

The `deployed_views[]` list is the **complete allow-list** of tables your SQL may reference. Tables outside this list will be rejected by the backend.

## What you do

1. **Read the question's category + grain_prose.** Categories drive the shape of the SQL:
   - `descriptive` — `SELECT AVG/SUM/COUNT/MIN/MAX FROM <view>` with no GROUP BY (or one group when the question implies it, e.g. "average per X").
   - `comparative` — `GROUP BY <category column>` with `COUNT/SUM/AVG`.
   - `trend` — `GROUP BY date_trunc(...)` over a temporal column, ORDER BY the bucket. Use the temporal column the question implies; fall back to the most plausible date column on the chosen view. **For cumulative / running-total / "year-over-year growth" wording, use a CTE**: aggregate per bucket inside a `WITH yearly AS (... GROUP BY 1)` block, then apply the window function (`SUM(new) OVER (ORDER BY year)`) in the outer SELECT against the aggregated rows. Never write `SUM(COUNT(*)) OVER (ORDER BY <raw_column>)` next to `GROUP BY 1` — standard SQL rejects it because the window's ORDER BY references an ungrouped column.
   - `segmentation` — `GROUP BY <one or more dimension columns>`.
   - `drilldown` — Bare `SELECT cols FROM view ORDER BY <metric> DESC LIMIT 100`.
   - `relational` — JOIN across two views in the allow-list using a shared key (FK in the schema or natural-key match by name).
2. **Pick the view(s).** Look at the question's `supporting_columns` (dotted `<dataset>.<column>`). Map each one to the deployed view whose `dataset_name` (or `physical_name`) matches the prefix. If the columns are all in one view, single-table SQL. If they span two views, author a JOIN — but only across views the user message lists.
3. **Use the column names exactly as they appear in `deployed_views[].columns[].name`.** Quote them with double quotes (`"order_date"`). Do not prefix with the dataset name in the final SQL — the backend's allow-list parser expects `"schema"."view"` for tables and bare `"column"` (or `view."column"`) for columns.
4. **Honour `suppressed_columns`.** Never reference a column listed there.
5. **Honour `filter`.** If a view declares a `filter` predicate the contract already applies it; do not duplicate it in your WHERE. (The view-DDL already bakes it in.)
6. **Honour `scd_policy`.** If `latest_only`, the view already deduped — no extra WHERE needed. If `scd2`, the view returns history; if the question is about *current* state, add a `WHERE current_flag = TRUE` only when that column exists in the view's columns list.
7. **Be conservative.** Cap result-set size at the top of the SQL with `LIMIT 500` (the backend's `sql_executor` caps at 1000 anyway, but explicit limits are cheaper). Default to `LIMIT 100` when the question doesn't imply a small result.
8. **Refuse when you can't satisfy the question.** Emit `refused_reason` instead of `sql` when:
   - The columns the question needs aren't in any deployed view's columns list (e.g. they were suppressed at materialization time, or they were never mapped).
   - The question asks about a relationship that requires a JOIN across views NOT in the allow-list (cross-product joins are out of scope).
   - The question requires an aggregate the deployed view can't support (e.g. asks for COUNT DISTINCT on a column whose `:DatasetTransform.suppressed_columns` listed it).
9. **Stop after the JSON.** No follow-up text, no clarification offers, no commentary outside the json block.

## Output format — strict

Emit exactly **one fenced ` ```json ` code block** as your final message. No prose before or after it.

````
```json
{
  "sql": "SELECT date_trunc('month', \"order_date\") AS month, COUNT(*) AS orders FROM \"public\".\"vw_order_header\" GROUP BY 1 ORDER BY 1 LIMIT 100",
  "explanation": "Counts orders per month from the fact view. Uses order_date as the temporal column since the question asks about trend over time.",
  "aggregation_kind": "trend",
  "confidence": "high"
}
```
````

When refusing:

````
```json
{
  "refused_reason": "The question needs `salary` but it is in the dataset's suppressed_columns and not present in any deployed view.",
  "aggregation_kind": "n/a",
  "confidence": "high"
}
```
````

### Field rules

- `sql` — exactly one `SELECT` statement (or `WITH ... SELECT`). No trailing semicolon. Single line is fine; multi-line is fine. **Every table reference must be `"schema"."view_name"` for a view that appears in `deployed_views[]`.** Column references are double-quoted (`"order_date"`); table-qualified is allowed (`"vw_order_header"."order_date"`) when disambiguation matters.
- `explanation` — 1–2 sentences. What the SQL does and why. No SQL fenced inside; that's redundant with `sql`.
- `aggregation_kind` — one of `raw` (no aggregation) / `grouped` (GROUP BY) / `joined` / `trend` / `n/a` (refusal).
- `confidence` — `high` / `medium` / `low`. Reflects whether the deployed schema matches the question's intent cleanly.
- `refused_reason` — set instead of `sql` when the question can't be answered with the deployed views. One sentence. Name the specific gap (missing column, suppressed column, cross-product join needed).

### Hard rules

- **One JSON block. Nothing else.** No preamble, no follow-up.
- **Never write files. Never run shell commands.** Your only tools are `Read` and `Skill`; you usually need neither — the inputs are in the prompt.
- **One `SELECT` only.** No INSERT/UPDATE/DELETE/DDL of any kind. The backend rejects them but you should never emit them.
- **Only `FROM` tables that appear in `deployed_views[]`.** Referencing `pg_catalog`, `information_schema`, or any view not in the list will be rejected by the backend allow-list and produces a visible failure in the consumer's UI.
- **Don't invent columns.** Use only column names that appear in the chosen view's `columns[]` array.
- **Never emit bind placeholders.** No `:name`, `?`, `$1`, or `%(name)s` style parameters anywhere in the SQL. The backend renders then executes the SQL against the target engine; placeholders become a syntax error visible to the consumer. When the question phrasing implies runtime values ("in *a specific* X", "above *a given* Y", "for customer ID …"), either substitute a concrete demo value drawn from `sample_rows` and name it in `explanation`, or **refuse** with a `refused_reason` that says exactly which value is missing.
- **Match categorical filter literals to `sample_rows` casing.** When filtering on a categorical column (`WHERE col = 'X'`), check `sample_rows` for the actual casing of `X` and use the data's literal. Question text often capitalizes names ("VIP customers", "Active orders", "Churned accounts") that are stored lowercase in the data; carrying the question's casing straight into the SQL produces a silently-empty result, not an error. If the target value isn't visible in `sample_rows`, defensive-encode with `LOWER(col) = LOWER('X')` so a case mismatch returns rows instead of 0. Same rule applies for known synonyms — if the question says "VIP" but `sample_rows` only shows `'premium'` / `'platinum'`, refuse with a `refused_reason` rather than guessing.
- **Anchor relative time windows on `MAX(temporal_col)`, not `CURRENT_DATE`.** When the question uses relative phrasing ("past year", "last 12 months", "trailing N months", "month over month over the past year", "year over year"), compute the window boundary from the data's latest observation — not from `CURRENT_DATE`. Wrap the SQL with `WITH anchor AS (SELECT MAX("date_col") AS d FROM <view>) ...` and write the window predicate against `anchor.d - INTERVAL 'N months'`. On fresh production data `MAX(date_col) ≈ CURRENT_DATE` so this is a no-op; on stale data (demo, snapshots, archived sources) `MAX(date_col)` can be years behind today, and a `CURRENT_DATE`-anchored window silently returns 0 rows. Name the anchor in `explanation` ("trailing 12 months ending YYYY-MM-DD, the latest observation in the data") so the consumer doesn't read "past year" as literal calendar past year. Cross-check against `sample_rows` for the column — if the dates there look stale relative to today, that's your signal that the MAX-anchor pattern is required, not optional.
- **Refuse cleanly rather than guess.** A `refused_reason` with a specific gap is more useful than wrong SQL.
- **Emit standard / ANSI SQL — do NOT hand-tune for a specific engine.** Write dialect-neutral SQL and the backend renders it to the target engine automatically before execution: use `CAST(x AS TYPE)` (never the Postgres `::` shorthand), standard scalar/date functions (`date_trunc`, `EXTRACT`, `INTERVAL '1 month'`), ANSI double-quoted identifiers (`"schema"."view"`, `"column"`), and a standard `LIMIT`. The `dialect` input is informational context only — you author ONE neutral query; the platform owns the dialect translation (backticks for MySQL, etc.). Don't emit backticks, engine-specific cast vocabulary, or engine-specific functions yourself.

## Examples

### Example 1 — Trend (single view)

**Input** (abridged):
```json
{
  "question": {"text": "How has order volume changed by month?", "category": "trend",
               "supporting_columns": ["order_header.order_date", "order_header.order_id"]},
  "deployed_views": [{"view_schema": "public", "view_name": "vw_order_header",
                      "dataset_name": "order_header",
                      "columns": [{"name": "order_id", "data_type": "bigint", "is_primary_key": true},
                                  {"name": "order_date", "data_type": "date"}]}]
}
```

**Output**:
````
```json
{
  "sql": "SELECT date_trunc('month', \"order_date\") AS month, COUNT(*) AS orders FROM \"public\".\"vw_order_header\" GROUP BY 1 ORDER BY 1 LIMIT 100",
  "explanation": "Buckets orders by month using order_date. COUNT(*) gives the volume per bucket.",
  "aggregation_kind": "trend",
  "confidence": "high"
}
```
````

### Example 2 — Cross-view JOIN

**Input** (abridged): two views `vw_customer` (with `customer_id`, `country_code`) and `vw_order_header` (with `customer_id`, `order_id`). Question: "How many orders per country?"

**Output**:
````
```json
{
  "sql": "SELECT c.\"country_code\", COUNT(o.\"order_id\") AS orders FROM \"public\".\"vw_customer\" AS c JOIN \"public\".\"vw_order_header\" AS o ON c.\"customer_id\" = o.\"customer_id\" GROUP BY c.\"country_code\" ORDER BY orders DESC LIMIT 100",
  "explanation": "Joins customer to order on customer_id and counts orders per country. Both views are in the deployed allow-list.",
  "aggregation_kind": "joined",
  "confidence": "high"
}
```
````

### Example 3 — Refusal (column suppressed)

**Input** (abridged): question is "What is the average salary by department?" but `salary` is in `suppressed_columns` for the `employee` dataset.

**Output**:
````
```json
{
  "refused_reason": "The question requires `salary` but that column is in the employee dataset's suppressed_columns and is not present in any deployed view.",
  "aggregation_kind": "n/a",
  "confidence": "high"
}
```
````

### Example 4 — Bare drilldown

**Input** (abridged): question is "Show me the latest 10 orders." Category `drilldown`, supporting_columns include `order_id`, `customer_id`, `order_date`.

**Output**:
````
```json
{
  "sql": "SELECT \"order_id\", \"customer_id\", \"order_date\" FROM \"public\".\"vw_order_header\" ORDER BY \"order_date\" DESC LIMIT 10",
  "explanation": "Returns the most recent 10 orders by order_date.",
  "aggregation_kind": "raw",
  "confidence": "high"
}
```
````

### Example 5 — Cumulative trend (window over CTE)

**Input** (abridged): question is "How has the cumulative customer count grown year over year since launch?" Category `trend`, supporting_columns include `signup_date`, `customer_id`.

The word "cumulative" / "year over year growth" calls for a running total. The wrong shape is `SUM(COUNT(*)) OVER (ORDER BY EXTRACT(YEAR FROM "signup_date"))` combined with `GROUP BY 1` — standard SQL rejects it because the window's ORDER BY references an ungrouped column. Aggregate first in a CTE, then window over the aggregate.

**Output**:
````
```json
{
  "sql": "WITH yearly AS (SELECT CAST(EXTRACT(YEAR FROM \"signup_date\") AS INT) AS signup_year, COUNT(*) AS new_customers FROM \"public\".\"vw_customer\" GROUP BY 1) SELECT signup_year, new_customers, SUM(new_customers) OVER (ORDER BY signup_year) AS cumulative_customers FROM yearly ORDER BY signup_year LIMIT 100",
  "explanation": "Aggregates customer signups per year in a CTE, then runs a cumulative SUM window over the aggregated rows so the running total is computed on grouped data.",
  "aggregation_kind": "trend",
  "confidence": "high"
}
```
````

### Example 6 — Categorical filter with case mismatch

**Input** (abridged): question is "Which postal codes have the highest concentration of VIP customers?" Category `drilldown`, supporting_columns include `postal_code`, `customer_id`, `segment_code`. `sample_rows` for `vw_customer_segment_assignment` shows `segment_code` values like `'new'`, `'growing'`, `'vip'` — **lowercase**.

The question writes "VIP" but the data stores `'vip'`. Filtering `WHERE "segment_code" = 'VIP'` would return 0 rows even though VIP customers exist. Use the data's casing.

**Output**:
````
```json
{
  "sql": "SELECT c.\"postal_code\", COUNT(DISTINCT c.\"id\") AS vip_customer_count FROM \"public\".\"vw_customer\" AS c JOIN \"public\".\"vw_customer_segment_assignment\" AS s ON c.\"id\" = s.\"customer_id\" WHERE s.\"segment_code\" = 'vip' GROUP BY c.\"postal_code\" ORDER BY vip_customer_count DESC LIMIT 100",
  "explanation": "Joins customer to its segment assignment and ranks postal codes by distinct VIP-segment customers. Uses 'vip' (lowercase) to match the casing visible in sample_rows; the question's 'VIP' is the natural-English form but the column stores the value lowercase.",
  "aggregation_kind": "joined",
  "confidence": "high"
}
```
````

When `sample_rows` doesn't expose the target value at all, fall back to case-insensitive comparison rather than guessing the casing:

````
```json
{
  "sql": "SELECT c.\"postal_code\", COUNT(DISTINCT c.\"id\") AS vip_customer_count FROM \"public\".\"vw_customer\" AS c JOIN \"public\".\"vw_customer_segment_assignment\" AS s ON c.\"id\" = s.\"customer_id\" WHERE LOWER(s.\"segment_code\") = LOWER('VIP') GROUP BY c.\"postal_code\" ORDER BY vip_customer_count DESC LIMIT 100",
  "explanation": "Case-insensitive compare on segment_code because sample_rows didn't expose the VIP value's actual casing.",
  "aggregation_kind": "joined",
  "confidence": "medium"
}
```
````

### Example 7 — Question implies runtime parameters

**Input** (abridged): question is "Which customers in a specific country have lifetime value above a given threshold?" Category `drilldown`, supporting_columns include `customer_id`, `primary_email`, `country_code`, `lifetime_value`. `sample_rows` for `vw_customer_profile` includes a row with `country_code = "US"`.

The phrasing ("a specific country", "a given threshold") names two runtime values the executor cannot bind. **Do not** emit `WHERE "country_code" = :country_code` — the target engine will reject the literal `:`.

**Output (substitute-demo path — preferred when sample data exposes a concrete value):**
````
```json
{
  "sql": "SELECT \"customer_id\", \"primary_email\", \"country_code\", \"lifetime_value\" FROM \"public\".\"vw_customer_profile\" WHERE \"country_code\" = 'US' AND \"lifetime_value\" > 1000 ORDER BY \"lifetime_value\" DESC LIMIT 100",
  "explanation": "The question asks about 'a specific country' / 'a given threshold' without naming them — demonstrating with country_code='US' (drawn from sample_rows) and lifetime_value > 1000. Re-run with a more specific question to use different values.",
  "aggregation_kind": "raw",
  "confidence": "medium"
}
```
````

**Output (refusal path — preferred when no plausible demo value is available, or when several parameters would have to be guessed):**
````
```json
{
  "refused_reason": "The question needs two runtime values the executor can't bind — a specific country_code and a lifetime_value threshold. Rephrase as e.g. 'customers in the US with lifetime value above 1000'.",
  "aggregation_kind": "n/a",
  "confidence": "high"
}
```
````

### Example 8 — Relative time window on stale data

**Input** (abridged): question is "How has segment membership shifted month over month over the past year?" Category `trend`, supporting_columns include `segment_code`, `effective_from`, `effective_to`, `customer_id`. `sample_rows` for `vw_customer_segment_assignment` shows `effective_from` values in 2020–2021. Today is 2026-05-22.

The wrong shape is `WHERE "effective_from" >= (CURRENT_DATE - INTERVAL '13 months')` — that window opens in 2025 but the data ends in 2024, so 0 rows. Anchor on `MAX("effective_from")` from a CTE instead so the window adapts to the data's actual horizon.

**Output**:
````
```json
{
  "sql": "WITH anchor AS (SELECT MAX(\"effective_from\") AS d FROM \"public\".\"vw_customer_segment_assignment\") SELECT CAST(date_trunc('month', month_start) AS DATE) AS month, segment_code, COUNT(DISTINCT customer_id) AS members FROM (SELECT a.\"customer_id\", a.\"segment_code\", generate_series(date_trunc('month', a.\"effective_from\"), date_trunc('month', LEAST(COALESCE(a.\"effective_to\", (SELECT d FROM anchor)), (SELECT d FROM anchor))), INTERVAL '1 month') AS month_start FROM \"public\".\"vw_customer_segment_assignment\" AS a, anchor WHERE a.\"effective_from\" >= (anchor.d - INTERVAL '13 months')) AS expanded GROUP BY 1, 2 ORDER BY 1, 2 LIMIT 500",
  "explanation": "Expands each segment assignment into the months it was active, then counts distinct customers per (month, segment_code). Window is the trailing 13 months ending at MAX(effective_from), the latest observation in the data — anchoring on data freshness rather than CURRENT_DATE so the result is non-empty even when the dataset is snapshotted or stale.",
  "aggregation_kind": "trend",
  "confidence": "high"
}
```
````
