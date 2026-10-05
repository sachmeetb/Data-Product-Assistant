---
name: data-mapping-neo4j
description: Maps source dataset columns to data product columns using semantic similarity of column descriptions, stores mappings as :ColumnMapping nodes in Neo4j with PROV-O provenance, and requires human approval via review_mappings.py. Use this skill when the user wants to map discovered columns to a data product, create column lineage between a source dataset and a DProdOutputDataset, or link DCAT columns to DProd columns. Trigger when the user says "map columns", "column mapping", "map source to product", "data mapping", or "link source columns to the data product".
---

## What this skill does

Compares the descriptions of source `:Column` nodes (from `metadata-enrichment`) with the
`description` properties of `:DProdColumn` nodes (from `load_employee_dprod.cypher` or
`data-product-spec-to-dprod-neo4j`). For each source column, identifies the best-matching
product column by semantic similarity of names and descriptions, assigns a confidence score,
and writes a `:ColumnMapping` node at `status: pending_review`.

A human reviewer then approves or rejects+remaps each mapping using `tools/review_mappings.py`.
Full W3C PROV-O provenance is recorded for every generation and review event.

Mappings are never overwritten. A source column that already has a current `:ColumnMapping`
is skipped.

## Prerequisites

- `data-discovery-to-dcat-neo4j` has been run (`:Dataset` and `:Column` nodes exist)
- `metadata-enrichment` has been run and at least some `:ColumnDescription` nodes are
  `approved` (source descriptions drive the semantic match quality)
- The target data product has been loaded into Neo4j with `:DProdColumn` nodes and
  `description` properties (e.g. via `cypher_scripts/load_employee_dprod.cypher`)

## Graph model

### Nodes added

| Node | Description |
|------|-------------|
| `:ColumnMapping` | One per source column per generation/correction — holds the proposed match and review status |
| `:ProvActivity` | One per mapping generation or review event |
| `:ProvAgent` | One per distinct actor (AI skill or human reviewer); shared via MERGE |
| `:ProvRejectionReason` | Created only when a review outcome is `rejected` |

### Properties on `:ColumnMapping`

| Property | Value |
|----------|-------|
| `uri` | `mapping:{project_code}:<src_bare>:<tgt_bare>` (multi-source: src_bare gets `+<hash8>` suffix) |
| `status` | `pending_review` \| `approved` \| `rejected` \| `steward_review` |
| `isCurrent` | `true` on exactly one mapping per target product column at any time |
| `similarityScore` | Float 0.0–1.0 |
| `rationale` | One-sentence explanation of the match |
| `mappingType` | Legacy: `direct` or `derived`. New code should read `transformKind` instead. |
| `transformKind` | `direct` \| `cast` \| `format` \| `concat` \| `split` \| `substring` \| `case` \| `arithmetic` \| `lookup` \| `literal` \| `expression` \| `bucket` \| `mask` \| `hash` \| `window` \| `date_difference` |
| `transformExpression` | SQL fragment, source of truth for view DDL generation |
| `transformInputs` | JSON-stringified ordered list of source `:Column` URIs referenced by the expression |
| `transformParams` | JSON-stringified kind-specific params (e.g. `{"separator": " "}`) |
| `transformDecorators` | JSON-stringified decorators (e.g. `{"standardization": ["trim"]}`) |
| `transformAuthor` | `po_hint` \| `ai_suggestion` \| `engineer` \| `steward_catalog` |
| `transformConfidence` | Optional float (only meaningful for `ai_suggestion` / `steward_catalog`) |
| `transformEscalationReason` | Free text, populated only when `status='steward_review'` |
| `createdAt` | ISO 8601 UTC timestamp |

For derived mappings (any `transformKind != 'direct'`), the `:ColumnMapping` has multiple `[:MAPS_SOURCE_COLUMN]` edges — one for each URI in `transformInputs`.

**Skip semantics:** A new mapping is skipped if its target `:DProdColumn` already has a current mapping. The same source column may feed multiple derived mappings (e.g. `first_name` can feed both `full_name` and `display_name`).

### Relationships added

| Relationship | From → To | When |
|---|---|---|
| `[:MAPS_SOURCE_COLUMN]` | `:ColumnMapping` → `:Column` | All mappings (current and historical) |
| `[:MAPS_TO_PRODUCT_COLUMN]` | `:ColumnMapping` → `:DProdColumn` | All mappings |
| `[:PROV_WAS_GENERATED_BY]` | `:ColumnMapping` → `:ProvActivity` | On creation and on human remap |
| `[:PROV_WAS_ASSOCIATED_WITH]` | `:ProvActivity` → `:ProvAgent` | Every activity |
| `[:PROV_WAS_DERIVED_FROM]` | `:ColumnMapping` → `:ColumnMapping` | Remapped mapping → the original it replaced |
| `[:PROV_USED]` | `:ProvActivity` → `:ColumnMapping` | Review activity → the mapping it reviewed |
| `[:HAS_REJECTION_REASON]` | `:ProvActivity` → `:ProvRejectionReason` | Rejected reviews only |

### Querying mappings

```cypher
// All approved mappings for a source table
MATCH (cm:ColumnMapping {status: 'approved', isCurrent: true})
MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(col:Column)
MATCH (cm)-[:MAPS_TO_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (ds:Dataset)-[:HAS_COLUMN]->(col)
WHERE ds.uri = 'dataset:employees.employee'
RETURN col.name AS source_column, pc.name AS product_column,
       cm.similarityScore AS score, cm.rationale AS rationale
ORDER BY col.ordinal;

// Pending mappings awaiting review
MATCH (cm:ColumnMapping {status: 'pending_review', isCurrent: true})
RETURN cm.uri, cm.similarityScore, cm.createdAt;
```

## Step-by-step workflow

### Step 1 — Discover available datasets and products

Run `query_mapping_candidates.py` in list mode to show what is available:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/query_mapping_candidates.py
```

Present the numbered lists to the user and ask:
- Which source dataset do you want to map from?
- Which data product do you want to map to?

### Step 2 — Fetch mapping candidates

Run `query_mapping_candidates.py` in context mode with the user's selections:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/query_mapping_candidates.py \
    --source-dataset <dataset_uri> \
    --target-product <product_uri>
```

This writes `metadata/mapping_candidates_<timestamp>.json`. Note the output file path.

### Step 3 — Review the candidates file

Read the JSON file. It contains:
- `source_columns`: list of source column objects with `column_uri`, `column_name`,
  `data_type`, `description` (from ColumnDescription), `already_mapped`
- `product_columns`: list of DProdColumn objects with `column_uri`, `column_name`,
  `data_type`, `description`

Columns where `already_mapped: true` should be skipped.

### Step 3.5 — Query FK relationships + PO-approved relationship descriptions (optional but recommended)

Pull every FK edge between the source tables AND any `:RelationshipDescription` the PO has approved on it:

```cypher
MATCH (ds1:Dataset)-[r:REFERENCES]->(ds2:Dataset)
WHERE ds1.uri IN $source_dataset_uris OR ds2.uri IN $source_dataset_uris
OPTIONAL MATCH (ds1)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd:RelationshipDescription
                                                     {isCurrent: true, status: 'approved'})
              -[:DESCRIBES_REFERENCE_TO]->(ds2)
RETURN ds1.name AS from_table, r.columns AS fk_columns,
       ds2.name AS to_table, r.referencedColumns AS pk_columns,
       coalesce(rd.relationshipNature, '') AS relationship_nature,
       coalesce(rd.text, '')               AS relationship_text
```

Use the result for **two** decisions:

**(a) Which table "owns" a column** — same as before. If `employee_id` appears in both `employees` (as PK) and `employee_departments` (as FK), prefer the `employees` table as the source. FK columns in referencing tables are duplicative.

**(b) Whether a join is semantically justified for this product column.** The `relationship_nature` chip names HOW the two tables relate, and `relationship_text` carries the PO-approved description. Use it to disambiguate cases the column names alone don't settle:

| Nature | Direction | What it means for mapping |
|---|---|---|
| `belongs_to`  | `from → to` (child → parent) | Each FROM row references one TO row. Safe to join FROM to TO to pull TO's attributes. The TO side is the source of truth for those attributes. |
| `categorises` | `from → to` (entity → dim) | TO is a dimensional lookup. Join FROM to TO when the consumer wants a TO-side attribute (category name, country name) for each FROM row. |
| `audit_log_for` | `from → to` (history → entity) | FROM is a history table about TO. Join only when the consumer asks for historical state or change-over-time; for current state, map to TO's columns directly. |
| `references` | generic | No specific direction; use FK column matching + name overlap to decide. |

**When you cite a relationship in your mapping decision, name the nature + the FROM/TO pair in your `rationale`** so the reviewer can audit the choice. Example: `"Mapped category_name from product.name via the categorises FK (product → category); the PO-approved relationship description confirms category is the dimensional lookup."`

When `relationship_nature` is empty (no approved description yet), fall back to FK-only reasoning. Encourage the PO to fill in the description if you find yourself frequently picking the wrong side of an ambiguous FK — that's the lever for higher-quality mappings going forward.

### Step 3.6 — Validate JOIN CONNECTIVITY of your chosen base tables (CRITICAL)

Step 3.5 reasons about FKs **one pair at a time**. That is not enough: the view-DDL must assemble ALL the base tables your mappings read from into a **single `FROM` clause**, which only works if those tables form **one FK-connected component**. If they split into ≥2 components, serving fails late with `ViewGenerationError: … source table X … no FK path …`. Don't hand that to the engineer — catch it here and recommend the fix.

**This bites hardest for consumer-aligned (`dpe-cf`) products that consume MORE THAN ONE source product.** `:REFERENCES` (FK) edges never cross source products (the only cross-product edge is `:CONSUMES`), so two tables from two different source products are **never** FK-connected even when they describe the same entity (e.g. `salary_history` from a Compensation product and `employee` from an Employee product, both keyed by `employee_id`).

Do this before writing mappings:

1. **List your base tables** — the FROM-side source datasets your mappings anchor on (`transformInputs[0]`'s table for each mapping). NOTE: a `lookup`'s reference table (`transformParams.lookup_table`) is NOT a base table — it's a keyed LEFT JOIN and needs no FK path. So a two-hop attribute (e.g. `job_title` = `employee_id → job_id → job.title`) makes `job_assignment_history` a base table, not `job`.
2. **Check connectivity** — union the base tables over the `:REFERENCES` edges from Step 3.5. If they don't collapse to ONE component, you have a gap. (The backend runs exactly this check at `GET /api/projects/{id}/serving/join-preflight` and returns a ready-to-apply recommendation — defer to it when unsure.)
3. **Recommend the bridge, don't emit a dead-end mapping.** Pick the **grain/anchor** table (usually the one supplying the most product columns / matching the dataset's grain), find a **shared key** present on a table in each disconnected component (commonly the entity id), and propose an explicit join graph on `:DatasetTransform.joins[]`:

```json
{ "joins": [
  {"alias": "sh",  "dataset_uri": "<anchor ods uri>",      "kind": "cross", "predicate": ""},
  {"alias": "emp", "dataset_uri": "<other-product ods uri>", "kind": "left", "predicate": "emp.employee_id = sh.employee_id"},
  {"alias": "jah", "dataset_uri": "<other ods uri>",        "kind": "left", "predicate": "jah.employee_id = sh.employee_id"}
] }
```

`joins[0]` is the FROM anchor (use `kind:"cross"` so it needs no predicate; the builder reads only its alias). Every base table must appear. The view-DDL still auto-dedups any SCD/effective-dated table you bridge in (it won't fan out). Surface this proposed join graph in your Step 5 mapping table and name it in your `rationale` (e.g. *"`salary_history` and `employee` are in different source products with no FK; bridged on the shared `employee_id`"*). The engineer applies it via the Joins override (or `PUT /dataset-transform/joins`).

**Auto-bridge (you usually don't need explicit joins[] anymore).** When base tables span source products with no FK but DO share an identity key, the serving layer now auto-constructs the bridge on that shared key — and, when the target grain is **SCD-2**, joins the effective-dated bridge **as-of** the anchor's span start (period-correct, no fan-out). Bridging **chains transitively**: a table that shares no key with the anchor can hop through an intermediate base table (`account —account_id→ transaction —customer_id→ customer`). When even chaining leaves a gap, the serving layer searches the CONSUMES'd products' **other (unmapped) datasets** for a **junction** — a dataset sharing *different* identity keys with the two sides — and synthesizes it as a pure bridge (`bridge_only`, emitted as a `SELECT DISTINCT` of the link keys so a ledger-grain junction can't fan the view out). So a clean cross-product product serves without hand-authored `joins[]`. `get_join_preflight` reports such products `connected: true, bridged_via: "auto_natural_key"` with the chained/junction joins as *informational* (`via` names each hop; `junction_bridges` lists synthesized junctions — check their `src_deployment_status`: the junction's source view must be deployed). Explicit `joins[]` is now only required when **no chainable key and no unambiguous junction exists** (a genuine gap — preflight returns `connected: false` with `unbridged_tables`), or to override the auto-bridge.

**If no shared key exists** between the components, check the preflight's `junction_candidates` first: a tie between plausible junction datasets is surfaced there (auto-pick refuses to guess) — resolve it by declaring explicit `joins[]` with the intended junction marked `bridge_only: true`, or by mapping a column from it so it becomes a base table. If there are no candidates at all, a linking table may exist in a source product this consumer **doesn't CONSUME** — recommend the PO add that product to the consumer's sources (wizard step 9 / `source-candidates-needed`) rather than papering over it with a mapping that can't compile. Only when no linking entity exists anywhere is it a genuine product-scope gap for the PO (wrong source picked, or a missing key).

### Step 4 — Generate semantic mappings

**IMPORTANT: Mapping is PRODUCT-COLUMN-DRIVEN.** For each product column, find the
best-matching source column — not the other way around.

**Transformation priority (consult in this order, then stop):**

1. **PO transform_hint.** Each product column in the candidates payload may carry a
   `transform_hint` object (decoded from the contract's `transform` block). If present,
   it is the PO's authoritative declaration of derivation intent. Emit the mapping with:
   - `transform_kind` = the hint's `kind`
   - `transform_inputs` = the source column URIs that resolve the hint's `inputs`
   - `transform_expression` = compose from the hint (or leave blank — engineer will fill)
   - `transform_author = "po_hint"`

2. **Steward transformation catalog.** For product columns without a hint, run the
   matcher against the steward-curated catalog:
   ```bash
   python ${CLAUDE_SKILL_DIR}/scripts/match_transformation_catalog.py \
       --product-columns metadata/product_cols_<table>_<ts>.json \
       --source-columns  metadata/source_cols_<table>_<ts>.json \
       --domain          <project_domain> \
       --output          metadata/catalog_matches_<table>_<ts>.json
   ```
   For any matches returned, emit with `transform_author = "steward_catalog"` and the
   template's `confidence_floor` as `transform_confidence`. Use the template's
   `transform.kind`, `transform.inputs`, `transform.params`, and `transform.decorators`
   verbatim.

3. **LLM inference (fallback).** For product columns left unresolved by steps 1+2,
   propose a mapping by comparing column names, descriptions, and types as before:
   - Names (exact or near-exact name overlap is strong evidence)
   - Description semantics (core concept, data type alignment, business context)
   - The `source_column_hint` property on DProdColumn (high-confidence evidence)
   - FK preference: prefer PK/owning table over FK referencing tables
   Default to `transform_kind = "direct"` for 1:1 matches; only use `concat` / `cast` /
   `expression` / `lookup` when name or type semantics demand it. Mark with
   `transform_author = "ai_suggestion"`.

**Always populate `transform_inputs`** with the source column URIs your
`transform_expression` references. The view-DDL generator uses `transform_inputs` for
safe word-boundary substitution; without them, the SQL won't compile correctly.

**Transform portability (READ THIS — the target serving platform is NOT known at
mapping time).** A product mapped here may be served on Postgres, Databricks,
Snowflake, BigQuery, or MySQL — the dialect is chosen LATER (at `configure_serving`),
so you must author **platform-neutral** transforms and let the compiler render the
right SQL per engine. Concretely:

- **Prefer a structured `transform_kind` over a raw `expression`.** A structured
  kind (`cast`, `concat`, `split`, `substring`, `date_difference`, `bucket`, `mask`,
  `hash`, `window`, …) is rendered per-dialect by the compiler. A raw `expression`
  is now **rendered to the target dialect at emit time too** (parsed as its
  `expression_dialect`, transpiled via the transform-portability compiler), but that
  render is **best-effort / fail-open**: a function the target can't support is left
  verbatim and will break on that engine. Structured kinds are still the robust path
  — they never depend on the transpiler succeeding.
- **NEVER hand-write engine-specific SQL functions** in a `transform_expression`:
  `AGE()`, `DATEDIFF()`, `DATE_PART()`, `EXTRACT(... FROM AGE(...))`, `TO_CHAR()`,
  `SPLIT_PART()`, `MONTHS_BETWEEN()`, `TIMESTAMPDIFF()`, `IFF()`, `NVL()`, `||`
  string-concat. These run on some engines and silently break on others.
  - age / years-of-service / elapsed-time → **`date_difference`** (pick `semantics`).
  - split a delimited string → **`split`**; substring → **`substring`**;
    join columns → **`concat`**; type change → **`cast`**; upper/lower/trim →
    **`format`**. See the Transform-params reference below.
- If a bespoke computation genuinely needs raw SQL (`case` / `arithmetic` /
  `expression`), keep it **ANSI-portable** (plain `CASE WHEN … END`, `+ - * /`,
  `COALESCE`, `CAST`) and reference columns via `transform_inputs`. A raw expression
  is parsed as its `expression_dialect` (default `postgres`) and validated against
  the *target* platform downstream — non-portable SQL will be flagged.

Assign a `similarity_score` (0.0–1.0):
- 0.95–1.0: name matches or descriptions are near-identical in concept
- 0.80–0.94: strong semantic overlap, same business concept
- 0.60–0.79: plausible match, some shared meaning
- Below 0.60: do not create a mapping (note as unmatched instead)

Write a one-sentence `rationale` explaining the match, including why this source
table was preferred if the column exists in multiple tables.

For product columns with no plausible match above 0.60 and no hint or catalog template,
note them as unmatched and inform the user. Do not create a `:ColumnMapping` for them —
the engineer can flag them for steward escalation in the review UI.

### Step 5 — Show the proposed mappings (do NOT ask for confirmation)

Print the proposed mapping table to the log so the engineer can see what was mapped:

```
Product column    ←  Source column                  Type     Score  Rationale
───────────────────────────────────────────────────────────────────────────────
employee_id       ←  employees.employee.emp_no      direct   0.97   PK match...
full_name         ←  employees.employee.first_name  derived  0.90   Concat first+last
                     employees.employee.last_name
department_name   ←  employees.department.dept_name  direct   0.95   ...
[unmatched]       ←  ???                             —        —      tenure_years: no source
```

Then proceed **directly** to Step 6 and write the mappings — do **NOT** ask the user to
confirm, and do **NOT** call `agent_ask.py`. This table is an informational display only,
not an approval gate. The engineer reviews and approves (or replaces / escalates) these
mappings in the **Mappings review panel** *after* they are written; every mapping lands at
`status: pending_review`, so nothing is applied without human review downstream. Pausing to
ask here is wrong — it blocks the pipeline and duplicates the review gate.

### Step 6 — Write mappings JSON

Write the proposed mappings to `metadata/mappings_<timestamp>.json`. Each entry is one of two shapes — direct (single source) or derived (multi-source + transform):

```json
[
  {
    "source_column_uri":   "column:employees.employee.emp_no",
    "product_column_uri":  "dprod:employee_product:column:employee_id",
    "similarity_score":    0.97,
    "rationale":           "Both represent the unique identifier for an employee record.",
    "transform_kind":      "direct",
    "transform_author":    "ai_suggestion"
  },
  {
    "source_column_uris":  ["column:employees.employee.first_name", "column:employees.employee.last_name"],
    "product_column_uri":  "dprod:employee_product:column:full_name",
    "similarity_score":    0.90,
    "rationale":           "Full name is composed from first and last name columns.",
    "transform_kind":      "concat",
    "transform_expression":"first_name || ' ' || last_name",
    "transform_inputs":    ["column:employees.employee.first_name", "column:employees.employee.last_name"],
    "transform_params":    {"separator": " "},
    "transform_decorators":{"standardization": ["trim"]},
    "transform_author":    "ai_suggestion",
    "transform_confidence":0.85
  }
]
```

Notes on the transform contract:
- `transform_kind` values: `direct`, `cast`, `format`, `concat`, `split`, `substring`, `case`, `arithmetic`, `lookup`, `literal`, `expression`, `bucket`, `mask`, `hash`, `window`, `date_difference`. If omitted, defaults to `direct` for single-source and `concat` for multi-source.
- `transform_expression` is the SQL fragment the view-DDL generator will compile. It must reference column names listed in `transform_inputs`.
- `transform_inputs` defaults to the source URIs if omitted.
- **⚠️ `transform_inputs` ORDER IS LOAD-BEARING for positional kinds.** For `concat` the view DDL emits the parts left-to-right in `transform_inputs` order; for `date_difference` the order is `[start, end]`. The Neo4j rows carry no ordering guarantee of their own, so the emitter trusts `transform_inputs` verbatim. **Derive the order from the product column's DESCRIPTION** — e.g. a `full_name` described as "first name + last name" MUST list `[..first_name, ..last_name]`, not the reverse, or the served value comes out "Last First". When you match a steward-catalog template, copy its `inputs` list **verbatim** — never reorder it. Double-check this ordering against the description before writing every multi-source `concat`.
- `transform_author`: `po_hint` (PO hint from ODCS contract), `ai_suggestion` (LLM proposal), `engineer` (hand-edited), `steward_catalog` (matched from a curated transformation template).
- The legacy `mapping_type` ("direct" / "derived") is still accepted for backward compat and is reflected onto a `mappingType` property; new code should use `transform_kind`.

### Transform-params reference

Phase 1 column-level kinds beyond the original 11. All params shipped as JSON in `transform_params`; the view-DDL generator compiles them per kind. Choose the simplest kind that expresses the intent.

**`date_difference`** — **PORTABLE** age / elapsed-time between two dates. **Use this instead of hand-writing `AGE()`, `DATEDIFF()`, `DATE_PART()`, `EXTRACT(... FROM AGE(...))`, `MONTHS_BETWEEN()`, or `TIMESTAMPDIFF()`** — those are engine-specific and break when the product is served on a non-Postgres platform (the exact bug that shipped a broken Databricks view). The neutral op renders the correct per-platform SQL at serving time. Source: one or two date columns via `transform_inputs`, order `[start, end]`; a single input pairs with `CURRENT_DATE` (the age-from-date-of-birth case).
```json
{
  "unit": "year",
  "semantics": "completed_units"
}
```
`semantics` is **load-bearing** — a wrong choice silently changes the number:
- `completed_units` — completed elapsed whole years (age: someone born 2000-12-31 is 24 on 2025-08-01, not 25). **This is what "age" / "years of service" almost always means.**
- `boundary_count` — calendar-year boundaries crossed (`YEAR(end) − YEAR(start)`; the same person counts as 25).
- `symbolic_interval` — the composite yrs+mos+days interval; **Postgres-only** (the preflight flags it unsupported on other engines).

`unit` is `year` in v1. Worked example — an `age` product column from a `date_of_birth` source column:
```json
{
  "source_column_uris":  ["column:hr.employees.date_of_birth"],
  "product_column_uri":  "dprod:workforce:column:age",
  "transform_kind":      "date_difference",
  "transform_inputs":    ["column:hr.employees.date_of_birth"],
  "transform_params":    {"unit": "year", "semantics": "completed_units"},
  "transform_author":    "ai_suggestion"
}
```
To **band** a computed tenure/age, author `date_difference` for the numeric column (e.g. `tenure_years`) and a separate `case`/`bucket` over it via `depends_on_product_columns: ["tenure_years"]` — never put `AGE(...)` inside a `CASE`.

**`bucket`** — discretize a continuous value into ordered bands. Source: one column.
```json
{
  "boundaries": [25, 50, 100],
  "labels":     ["low", "medium", "high", "very_high"]
}
```
N boundaries → N+1 labels. Renders as a CASE WHEN chain (`x < 25 THEN 'low' ... ELSE 'very_high'`).

**`mask`** — format-preserving redaction. Source: one column.
```json
{
  "algorithm":   "keep_last",
  "keep_n":      4,
  "mask_char":   "X"
}
```
Algorithms: `keep_last` (mask all but last N chars), `keep_first` (mask all but first N), `middle` (mask middle, keep first and last keep_n chars). For credit-card style with preserved separators, pair with the `keep_format` boolean to leave non-alphanumeric chars unmasked.

**`hash`** — irreversible digest. Source: one column.
```json
{
  "algorithm":   "md5",
  "salt":        "optional-namespace-prefix"
}
```
Algorithms: `md5` (built-in PG), `sha1` / `sha256` (require `pgcrypto`). Salt is optional and prepended to the source value before hashing. **Not a security primitive** — use for determinism / namespace separation, not for password-grade obfuscation.

**`lookup.selection_strategy`** — extends `lookup` from equi-join only to five strategies:
- `equi` (default, today's behavior) — `LEFT JOIN lookup_table ON src.fk = lk.key_column`, return `lk.value_column`
- `latest` — `LEFT JOIN (ROW_NUMBER() OVER (PARTITION BY key ORDER BY order_by DESC) = 1)` derived table. Use for "current_X" patterns (latest department from a history table) on a **current-state** target.
- `aggregate` — `LEFT JOIN (SELECT key, agg_fn(value) GROUP BY key)` derived table. Use for `total_X`, `count_of_X`, `last_seen_X`.
- `exists` — `LEFT JOIN ... IS NOT NULL` returning boolean. Use for `has_X`, `is_X` flags.
- `asof` — point-in-time `LEFT JOIN lookup_table ON src.fk = lk.key AND lk.effective_from <= src.<pivot> AND (lk.effective_to IS NULL OR lk.effective_to > src.<pivot>)`. Picks the reference row valid at the anchor's pivot date. **Use this — not `latest` — when the target dataset's grain is SCD-2 / effective-dated history**, so each historical row carries period-correct attributes instead of present-day ones. Requires `effective_from_column` + `pivot_column` (the anchor's effective-from); `effective_to_column` optional.

**Grain-awareness (CRITICAL for history products).** Match the lookup's temporality to the TARGET dataset's `scd_policy`: a **current-state** grain wants `latest`/`is_current`; an **SCD-2** grain wants `asof` (or anchor the lookup key on an effective-dated bridge table — see Step 3.6 — which the serving layer then auto-joins as-of). A `latest`/`is_current` lookup feeding an SCD-2 target stamps present-day values on history — wrong data. The serving layer auto-applies as-of for effective-dated **bridge** tables on an SCD-2 grain, but for a **direct** lookup on an effective-dated reference you must author `selection_strategy: asof` explicitly.

`latest` requires `order_by_column` (and optional `order_by_direction`, default `DESC`). `aggregate` requires `aggregate_function` (`SUM`, `COUNT`, `AVG`, `MIN`, `MAX`, `COUNT_DISTINCT`) — OR a raw `aggregate_expression` (below). All strategies accept an optional `filter_clause` (raw SQL fragment applied inside the derived table).

**`aggregate_expression` — composite aggregates (one mapping, one scan).** When the target column is a FORMULA over two or more aggregates of the same reference table (a recency/frequency score, a ratio of two sums, a windowed count minus an all-time count), a single `aggregate_function` can't express it. Set `aggregate_expression` instead: raw SQL over the lookup table's columns, any number of aggregates, each with its own `FILTER (WHERE …)` when the windows differ. It supersedes `aggregate_function`/`value_column` and is emitted verbatim as `SELECT <key_column>, <aggregate_expression> AS agg_value FROM <lookup_table> [WHERE <filter_clause>] GROUP BY <key_column>`. Do NOT use `filter_clause` for a per-aggregate window — it constrains EVERY aggregate in the scan; use per-aggregate `FILTER` inside the expression. Worked example — a churn risk score (0 = active … 100 = churned) per customer over a transaction ledger:

```json
{
  "transformKind": "lookup",
  "transformInputs": ["<anchor_table>.customer_id"],
  "transformParams": {
    "lookup_table": "transaction_ledger", "key_column": "customer_id",
    "selection_strategy": "aggregate",
    "aggregate_expression": "GREATEST(0, LEAST(100, (CURRENT_DATE - MAX(txn_timestamp)::date) * 2 - COUNT(txn_id) FILTER (WHERE txn_timestamp >= CURRENT_DATE - 90) * 5))"
  }
}
```

**Derived-on-derived (expression over OTHER product columns).** When a target column is computed from sibling PRODUCT columns of the same dataset (e.g. a score combining two already-mapped aggregate columns), author a plain `expression`-kind mapping whose `transformExpression` references the sibling product columns by name and set `transformParams.depends_on_product_columns` to the list of those names. The generator defers such columns to an outer "enriched" CTE layer where the referenced columns exist (topologically layered, so chains work). Prefer a single `aggregate_expression` lookup when all inputs come from ONE reference table; prefer derived-on-derived when composing across different sources or when the intermediate columns are product columns in their own right.

#### Banding / grading a value (CRITICAL — never leak a raw value under a band column)

A target column described as a **band / grade / tier / bracket / bucket** (e.g. `compensation_band` = "Salary banded into ordinal grades A=entry … E=executive") MUST emit the discrete band, **never** the underlying raw value. Passing the raw number through under a band-named column leaks the sensitive value (e.g. exact salary) and contradicts the column's declared (usually short-string) type. The view-DDL generator now flags this as a `semantic_warning` ("band-named column with no bucketing logic"), but author it correctly up front:

- **The value is already a plain column on the main FROM-side table** → use the `bucket` kind with `boundaries` + `labels` (see above).
- **The value must first be looked up** (the common case — e.g. *current* salary lives in a `salary_history` SCD table, latest *score* in a history table) → use a **`lookup`** with the right `selection_strategy` (`latest` for current-state) AND author the bucketing **CASE in `transformExpression`** over the looked-up value column. The generator resolves the looked-up value to the join alias, so reference it by `<lookup_table>.<value_column>`:

```json
{
  "transformKind": "lookup",
  "transformInputs": ["<main_table>.employee_id"],
  "transformExpression": "CASE WHEN salary_history.amt IS NULL THEN NULL WHEN salary_history.amt < 60000 THEN 'A' WHEN salary_history.amt < 85000 THEN 'B' WHEN salary_history.amt < 110000 THEN 'C' WHEN salary_history.amt < 135000 THEN 'D' ELSE 'E' END",
  "transformParams": {
    "lookup_table": "salary_history", "key_column": "employee_id", "value_column": "amt",
    "selection_strategy": "latest", "order_by_column": "effective_from", "filter_clause": "is_current = true"
  }
}
```

Always include an `IS NULL → NULL` arm (otherwise NULL falls through to the `ELSE` band). Ground the boundaries in the source column's profiling (min / percentiles / max) when available, and confirm the band column's **sensitivity classification** reflects the underlying data — a salary-derived band is still confidential.

> #### `transformInputs` anchor rule for lookups (CRITICAL)
>
> For **every** `lookup` mapping — including `equi`, `latest`, `aggregate`, and `exists` — `transformInputs` MUST anchor on **the main (FROM-side) table's join column**, NOT on the lookup table's value column. The view generator uses `transformInputs[0]`'s table to determine which table sits in the main `FROM` clause; the lookup table itself goes inside a `LEFT JOIN` (derived subquery for `latest` / `aggregate` / `exists`) and must never be the FROM-anchor.
>
> Anchor rule:
> ```
> transformInputs[0]  -->  the column on the MAIN table that joins to params.key_column on params.lookup_table
> ```
>
> Worked example — `total_orders` on a `customer_core` product, counting `order_header` rows per customer:
>
> ✅ **Correct**
> ```json
> {
>   "transformKind": "lookup",
>   "transformExpression": "COUNT(order_header.id)",
>   "transformInputs": ["dprod:col:<customer-source>:customer:id"],
>   "transformParams": {
>     "selection_strategy": "aggregate",
>     "lookup_table":       "order_header",
>     "key_column":         "customer_id",
>     "value_column":       "id",
>     "aggregate_function": "COUNT",
>     "filter_clause":      "status = 'delivered'"
>   }
> }
> ```
> Result: `customer` stays in the FROM; the generator emits `LEFT JOIN (SELECT customer_id, COUNT(id) ... GROUP BY customer_id) lk1 ON customer.id = lk1.customer_id`.
>
> ❌ **Wrong** (causes a self-join + an FK-path-not-found failure during view DDL generation)
> ```json
> {
>   "transformInputs": ["dprod:col:<orders-source>:order_header:id"],
>   "transformParams": { "...": "...", "lookup_table": "order_header" }
> }
> ```
> Result: the generator treats `order_header` as the FROM anchor, self-joins it (`ON order_header.id = lk1.customer_id`), and then refuses because there's no FK path from `order_header` to the rest of the mapped tables.
>
> Rule of thumb: `transformInputs[0]`'s table should be a table that is **directly mapped** (i.e., another mapping has `transformKind='direct'` against it) or otherwise FK-reachable from the directly mapped tables. The lookup table itself usually isn't — it lives only inside the lookup.
>
> This rule matters most for **cross source-product lookups** in consumer-aligned products (the lookup table lives in a different `dprod:` source than the main table). In that case the two tables can never have a `:REFERENCES` edge in the dprod graph — getting `transformInputs` wrong is the difference between "view compiles cleanly" and "view DDL generation fails with no recoverable FK path".

> #### Lineage: `:LOOKUP_VIA` edges (automatic)
>
> `write_mappings.py` resolves each lookup mapping's `lookup_table` + `value_column` / `key_column` (bare names in `transformParams`) back to the concrete `:Column` / `:DProdColumn` node and MERGEs a `(:ColumnMapping)-[:LOOKUP_VIA {role, strategy}]->(...)` edge (`role ∈ {value, key}`). This is what makes the lookup's reference table — often a **different** CONSUMES'd source product — appear in the marketplace / engineer lineage as a distinct (dashed, "lookup"-badged) source rather than being hidden inside the transform JSON. Resolution is best-effort: an undiscovered reference table is silently skipped (the DDL still works), and the backend re-reconciles these edges on mapping approve / replace and via `scripts/backfill_lookup_via.py`.

### Step 7 — Load mappings into Neo4j

```bash
python ${CLAUDE_SKILL_DIR}/scripts/write_mappings.py \
    metadata/mappings_<timestamp>.json \
    --project-code <project_code>
```

`--project-code` is recommended; it scopes the `:ColumnMapping` URI so two projects mapping the same source/target pair don't collide. Without it the script warns and uses an unscoped URI.

### Step 7.5 — Validate transform PORTABILITY (recommended)

Mirror of the Step-3.6 JOIN preflight, for transforms. Once mappings are loaded,
ask the backend to compile every transform against the product's resolved serving
platform and report anything unsupported:

```
GET /api/projects/{project_id}/serving/transform-preflight
```

The response is a `CompileResult`: `{ok, validated, platform, errors[], warnings[]}`.
Each error carries `product_col`, `function`, and a `remediation` string (e.g.
"Databricks has no AGE. Decompose to a date_difference op …"). For every error,
**re-author that mapping** (usually: swap a hand-written engine-specific expression
for the neutral kind named in the remediation — `date_difference`, `split`, etc.)
and re-run Step 7.

Notes:
- Most effective for **consumer-aligned (dpe-cf)** products, whose serving platform
  is already resolved from the CONSUMES'd (already-deployed) source. For a product
  whose serving target isn't configured yet, the preflight resolves to `postgres`
  and won't catch a non-Postgres break — which is exactly why the Step-4 portability
  rule (author neutral by default) is the primary safeguard, not this check.
- `validated: false` means the check was **skipped** (unresolved / non-served
  dialect), not "clean".

### Step 8 — Hand off to reviewer

Tell the user:

```
Mappings loaded as 'pending_review'. To validate them, run:

    python tools/review_mappings.py --reviewer "Your Name"

Add --source-dataset or --target-product to filter to specific mappings.
Use --dry-run to preview without making changes.
```

## Human review workflow (`tools/review_mappings.py`)

For each pending mapping the reviewer sees:

```
── Mapping 1/6 ────────────────────────────────────────────────────────
  Source:  employees.employee.id  (bigint)
  Desc:    Unique identifier for each employee record, assigned as a numeric key.

  Product: Employee Data Product.employee_key
  Desc:    Unique surrogate identifier for an employee record within the enterprise HR system.

  Score:    0.97
  Rationale: Both represent the unique identifier for an employee record.

[A]pprove  [R]eject  [S]kip >
```

**Approve** → `status: approved`, review ProvActivity created.

**Reject** → prompts for rejection category + optional detail, then asks:
  `Remap to a different product column? [y/N]`
  - If yes: numbered list of product columns presented; selection creates a new
    `approved` ColumnMapping with `[:PROV_WAS_DERIVED_FROM]` → original.
  - If no: original marked `rejected`, no replacement created.

**Skip** → deferred (mapping remains `pending_review`).

### Rejection categories

1. `incorrect_mapping` — columns are semantically unrelated
2. `wrong_target_column` — correct concept but wrong product column chosen
3. `too_low_confidence` — rationale does not justify the match
4. `no_match_exists` — source column has no valid product equivalent
5. `duplicate_mapping` — another source column is a better match for this product column
6. `other` — free-form reason only
