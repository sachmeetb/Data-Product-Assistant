---
name: metadata-enrichment
description: Generates natural language column descriptions from Neo4j knowledge graph context and stores them as :ColumnDescription nodes linked to :Column nodes. Use this skill whenever the user wants to enrich metadata, generate column descriptions, document their data catalog, or describe what columns mean. Also trigger when the user says "generate descriptions", "enrich metadata", "describe my columns", "add column documentation", "what does this column mean", or "document the graph". Requires data-discovery-to-dcat-neo4j to have been run first. Works best when data-profiling-to-dqv-neo4j and data-quality-rule-generation have also been run, as richer graph context produces better descriptions.
---

## What this skill does

Queries the Neo4j knowledge graph for columns that do not yet have a `:ColumnDescription` node, presents the user with a list of tables to process, then uses all available graph context — column metadata, sibling columns, DQ rules, profiling statistics, top values, and FK relationships — to generate concise, factual column descriptions. The descriptions are written back to the graph as `:ColumnDescription` nodes with `status: pending_review`, linked to their `:Column` node via `[:HAS_DESCRIPTION]`.

A human reviewer then approves or rejects+corrects each description using `scripts/review_descriptions.py`. Full W3C PROV-O provenance is recorded for every generation and review event.

Descriptions are never overwritten. A column that already has a `:ColumnDescription` is always skipped.

## Expected graph state (before this skill runs)

When this skill runs, the Neo4j graph will already contain nodes from upstream pipeline stages. These are all expected and normal — do not flag them as unexpected structure.

### Required (from data-discovery-to-dcat-neo4j)

| Node | Purpose | Key relationships |
|------|---------|-------------------|
| `:Catalog` | Schema/source system | `[:DCAT_DATASET]` → `:Dataset` |
| `:Dataset` | Table (has `schema`, `name`, `uri`, `row_count`) | `[:HAS_COLUMN]` → `:Column`; `[:REFERENCES]` → `:Dataset` (FK) |
| `:Column` | Column (has `name`, `dataType`, `nullable`, `primaryKey`, `ordinal`, `uri`) | — |

### Optional (from data-profiling-to-dqv-neo4j) — enriches descriptions when present

| Node | Purpose | Key relationships |
|------|---------|-------------------|
| `:Metric` | Metric definitions (`null_rate`, `distinct_count`, `min`, `max`, etc.) | — |
| `:QualityMeasurement` | Measured value for one column × one metric | `(:Column)-[:HAS_QUALITY_MEASUREMENT]->(qm)-[:ON_METRIC]->(:Metric)` |
| `:TopValue` | Frequently occurring values for a column | `(:Column)-[:HAS_TOP_VALUE]->(:TopValue)` |

### Optional (from data-quality-rule-generation) — enriches descriptions when present

| Node | Purpose | Key relationships |
|------|---------|-------------------|
| `:NodeShape` | SHACL-inspired rule container for a table | `(:Dataset)-[:HAS_SHAPE]->(:NodeShape)` |
| `:PropertyShape` | Individual DQ rule (has `ruleType`, `severity`, `description`) | `(:NodeShape)-[:PROPERTY]->(ps)-[:ON_COLUMN]->(:Column)` |

### Optional (project scoping)

| Node | Purpose | Key relationships |
|------|---------|-------------------|
| `:Project` | Multi-project isolation node | `(:Project)-[:HAS_CATALOG]->(:Catalog)` |

All of these nodes are part of the standard pipeline. This skill reads from `:Dataset`, `:Column`, `:QualityMeasurement`, `:TopValue`, `:PropertyShape`, and `:Metric` nodes, and writes new `:ColumnDescription` and provenance nodes.

## Graph Model

### Nodes added

| Node | Description |
|------|-------------|
| `:ColumnDescription` | One per column per generation/correction — holds the natural language description and review status |
| `:TableDescription` | One per dataset per generation/correction — holds the table description, `relationshipKind` classification, and review status (parallels `:ColumnDescription`) |
| `:ProvActivity` | One per generation or review event |
| `:ProvAgent` | One per distinct actor (AI or human reviewer); shared via MERGE |
| `:ProvRejectionReason` | Created only when a review outcome is `rejected` |

### Properties on `:ColumnDescription`

| Property | Value |
|----------|-------|
| `uri` | `description:<schema>.<table>.<column>:<timestamp>` |
| `text` | The generated description |
| `status` | `pending_review` \| `approved` \| `rejected` |
| `isCurrent` | `true` on exactly one description per column at any time |

### Properties on `:ProvActivity`

| Property | Value |
|----------|-------|
| `uri` | `prov:activity:<schema>.<table>.<column>:<timestamp>` |
| `activityType` | `generation` \| `review` |
| `outcome` | null (generation) \| `approved` \| `rejected` (review) |
| `occurredAt` | ISO 8601 UTC timestamp |

### Properties on `:ProvAgent`

| Property | Value |
|----------|-------|
| `uri` | `prov:agent:ai:metadata-enrichment-skill` or `prov:agent:human:<name>` |
| `agentType` | `ai` \| `human` |
| `name` | `metadata-enrichment-skill` or reviewer name |

### Properties on `:ProvRejectionReason`

| Property | Value |
|----------|-------|
| `uri` | `prov:reason:<schema>.<table>.<column>:<timestamp>` |
| `category` | `incorrect_meaning` \| `too_vague` \| `too_specific` \| `incorrect_constraint` \| `incorrect_values` \| `incorrect_fk_reference` \| `other` |
| `detail` | Optional free-form text |

### Relationships added

| Relationship | From → To | When |
|---|---|---|
| `[:HAS_DESCRIPTION]` | `:Column` → `:ColumnDescription` | All descriptions (current and historical) |
| `[:PROV_WAS_GENERATED_BY]` | `:ColumnDescription` → `:ProvActivity` | On creation and on correction |
| `[:PROV_WAS_ASSOCIATED_WITH]` | `:ProvActivity` → `:ProvAgent` | Every activity |
| `[:PROV_WAS_DERIVED_FROM]` | `:ColumnDescription` → `:ColumnDescription` | Corrected description → the original it replaced |
| `[:PROV_USED]` | `:ProvActivity` → `:ColumnDescription` | Review activity → the description it reviewed |
| `[:HAS_REJECTION_REASON]` | `:ProvActivity` → `:ProvRejectionReason` | Rejected review activities only |

### Detecting columns without descriptions

```cypher
MATCH (col:Column)
WHERE NOT (col)-[:HAS_DESCRIPTION]->(:ColumnDescription)
RETURN col.uri, col.name
```

## Description style

Descriptions are written in noun-phrase style — pure semantic/business descriptions of what the column represents. 1-2 sentences max. No filler.

**Include:**
- What the column IS and what it represents in business terms
- FK references — what table/concept the column points to

**Exclude — do NOT include any of these:**
- Nullability statements ("always present", "may be null", "nullable", "NOT NULL")
- Value enumerations ("one of N values: ...", "allowed values include...")
- Range statements ("values range from X to Y", "between X and Y")
- Example values ("e.g., 'John', 'Jane'")
- Data type descriptions ("stored as bigint", "recorded in whole currency units", "numeric key")
- Statistical observations ("observed values span...", "distinct values...", "null rate...")
- Cardinality commentary ("drawn from a pool of...")
- Frequencies and percentages

The description should answer "What is this column?" — not "What does the data look like?" or "What are the technical properties?"

Examples:

- `id` (PK, bigint) → `"Unique identifier for each employee record."`
- `birth_date` (date) → `"Date of birth of the employee."`
- `hire_date` (date) → `"Date on which the employee was hired by the organisation."`
- `gender` (enum) → `"Gender of the employee."`
- `dept_no` (FK → department) → `"Department to which the employee is currently assigned."`
- `salary` (bigint) → `"Gross salary amount for the employee during the associated period."`
- `from_date` (date) → `"Start date of the associated record's effective period."`
- `to_date` (date) → `"End date of the associated record's effective period."`

Use this style consistently across all generated descriptions.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/query_column_context.py` | Query Neo4j for undescribed columns; list tables or export full context JSON |
| `scripts/summarize_context.py` | Condense a large context JSON into a compact per-column summary for description generation |
| `scripts/write_descriptions.py` | Write generated descriptions as `:ColumnDescription` nodes to Neo4j with `status: pending_review` and generation provenance |
| `scripts/write_table_descriptions.py` | Write per-`:Dataset` descriptions + `relationshipKind` classification as `:TableDescription` nodes |
| `scripts/write_relationship_descriptions.py` | Write per-FK-edge descriptions + `relationshipNature` classification as `:RelationshipDescription` nodes |
| `scripts/query_rejection_context.py` | Query Neo4j for rejected-then-corrected description pairs as few-shot learning examples |
| `scripts/query_playbook.py` | Query Neo4j for domain-scoped playbook rules to guide description generation |
| `dpe-tools/review/review_descriptions.py` | Interactive human review: approve, reject+correct, or skip pending descriptions |

All scripts support `--help`.

**Dependency:** all scripts require the `neo4j` Python driver — install with `pip install neo4j`.

`review_descriptions.py` lives in the **`dpe-tools` repo** (not in the skill or the project). It is a reusable governance tool shared across all DPE projects. Run it from the project directory so it picks up `.dpe.env` for connection config.

## Workflow

### Step 1 — Confirm prerequisites

The graph must already contain `:Column` nodes loaded by `data-discovery-to-dcat-neo4j`.

Check: `MATCH (col:Column) RETURN count(col)` — should be > 0.

If DQV profiling or DQ rules are also present, the descriptions will be richer, but they are not required.

### Step 2 — Get connection details

If not already provided, ask the user for their Neo4j connection details. Defaults:

| Setting | Default |
|---------|---------|
| Host | `localhost` |
| Bolt port | `7687` |
| Username | `neo4j` |
| Password | `your_password` |
| Database | `neo4j` |

### Step 3 — List tables with undescribed columns

**IMPORTANT:** When a project code is provided in the prompt, always pass `--project-code` to scope queries to the current project's datasets only. This prevents processing datasets from other projects.

```bash
python ${CLAUDE_SKILL_DIR}/scripts/query_column_context.py \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db> \
  --project-code <project_code>
```

This prints a numbered list such as:

```
Tables with undescribed columns (6 table(s)):

   1. employees.department         (2 column(s) without description)  uri=dataset:employees.department
   2. employees.department_employee (2 column(s) without description)  uri=dataset:employees.department_employee
   3. employees.employee           (6 column(s) without description)  uri=dataset:employees.employee
   ...
```

Present this list to the user exactly as printed. Then ask:

> "Which tables would you like to generate column descriptions for? Enter numbers, table names, or say 'all'."

If all columns already have descriptions, tell the user and stop.

### Step 4 — Fetch full column context

Resolve the user's selection to dataset URIs. Then run:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/query_column_context.py \
  --tables <uri1,uri2,...> \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

The script creates `metadata/column_context_<YYYYMMDD_HHMMSS>.json` in the current working directory and prints the full path.

### Step 5 — Summarise the context

The raw context JSON is large and must not be read directly. Always pass it through `summarize_context.py` first to get a compact, token-efficient summary:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/summarize_context.py \
  metadata/column_context_<YYYYMMDD_HHMMSS>.json
```

The script prints a compact per-table, per-column summary to stdout. Read that output — it contains everything needed for description generation (data type, PK/NOT NULL flags, rules, allowed values, FK references) without the verbose raw JSON.

**Do not read the raw context JSON file directly. Always use `summarize_context.py`.**

### Step 5b — Gather learning context (optional, improves quality)

If previous review cycles have generated rejection/approval data, gather it to improve description quality. These steps are optional — skip if this is the first enrichment run for the project.

**Rejection examples** — past corrections that teach what to avoid:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/query_rejection_context.py \
  --tables <uri1,uri2,...> \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

This produces `metadata/rejection_context_<timestamp>.json` containing:
- **Same-table rejections**: Descriptions rejected for columns in the same tables being enriched (most relevant)
- **Global rejections**: Descriptions rejected across all tables (broader patterns)
- **Exemplar descriptions**: Descriptions rated "Excellent" (quality=3) during review — positive examples

**Domain playbook** — curated rules from the reflector:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/query_playbook.py \
  --domain "<domain_name>" \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

This produces `metadata/playbook_guidance.json` containing domain-scoped rules that should guide description generation.

**How to use the learning context:**

When generating descriptions in Step 6, incorporate the learning context as follows:
1. If **rejection examples** exist for similar columns (same data type, same table, or same column naming pattern), study what was rejected and what the human correction looked like. Avoid making the same mistakes.
2. If **exemplar descriptions** exist for similar columns, use them as positive templates for style and content.
3. If **playbook rules** exist, follow them. These are domain-specific guidelines learned from prior review cycles.

The learning context supplements — not replaces — the evidence priority order in Step 6.

### Step 6 — Generate descriptions

For each column context object, synthesise a description using this priority order of evidence:

1. **Column name + table name** — the primary signal. Infer purpose from naming conventions (`id`, `_date`, `_no`, `dept_`, etc.).
2. **`all_table_columns`** — understanding the full column list places each column in context (e.g. knowing `id`, `birth_date`, `first_name`, `last_name`, `gender`, `hire_date` all exist together confirms this is an employee entity table).
3. **`primary_key` / `nullable`** — incorporate into the description where meaningful (PK = unique identifier; NOT NULL = always present).
4. **`rules`** — use rule descriptions to confirm mandatory/range/allowed-value behaviour. Prefer the rule's `description` field as a signal, but rewrite it in noun-phrase style.
5. **`measurements`** — use `min`/`max` for range context, `null_rate` to confirm non-null behaviour, `distinct_count` vs `row_count` for uniqueness signals.
6. **`top_values`** — use to enumerate allowed values for low-cardinality columns (e.g. gender M/F, boolean flags).
7. **`foreign_keys`** — if the column participates in a FK, describe it as a foreign key to the referenced table.

**Description rules:**
- Noun-phrase style. No "This column stores..." or "The X column contains...".
- One to two sentences maximum.
- Mention value constraints (enum values, ranges, not-null) only when they add meaningful context.
- Do not repeat the column name verbatim at the start — rephrase it as a concept.
- For PK columns: start with "Unique identifier for..."
- For FK columns: start with "Foreign key referencing..."
- For date columns: start with "Date [the/on which]..." or "Start/end date of..."
- For amount/numeric columns: include the observed range if available.

### Step 7 — Write descriptions JSON

Create `metadata/column_descriptions_<YYYYMMDD_HHMMSS>.json` in the current working directory using the same timestamp as the context file (or a new one if generating fresh). Format:

```json
[
  {
    "column_uri": "column:employees.employee.hire_date",
    "description": "Date the employee was hired by the organisation."
  },
  {
    "column_uri": "column:employees.employee.gender",
    "description": "Gender of the employee; one of two known values: M (male) or F (female)."
  }
]
```

Include every column from the context file — one entry per undescribed column.

### Step 8 — Load descriptions into Neo4j

```bash
python ${CLAUDE_SKILL_DIR}/scripts/write_descriptions.py metadata/column_descriptions_<timestamp>.json \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

Use `--dry-run` to preview without writing.

Descriptions are written with `status: pending_review` and `isCurrent: true`. A `:ProvActivity {activityType: 'generation'}` and shared `:ProvAgent` for the AI are created automatically.

Report:
- Full path of the descriptions file used
- Count of descriptions created, skipped, and errors
- Remind the user to run Step 8 to review

### Step 8b — Generate + load table descriptions

After the column descriptions are written, generate a short description per `:Dataset` plus a `relationship_kind` classification. This feeds two downstream surfaces: (a) PO review in `SourceProductValidationPanel` and marketplace/dataset cards, and (b) the view-DDL bridge-ranker — when auto-bridge BFS finds multiple equally-short paths through different junctions, the classification breaks the tie deterministically (general_membership outranks specialization).

For each dataset, produce one entry like:
```json
{
  "dataset_uri": "dataset:<project_code>:employees.department_employee",
  "description": "Records each employee's assignment to a department over time. One row per (employee, department, period).",
  "relationship_kind": "general_membership"
}
```

`relationship_kind` is one of:

| value | when |
|---|---|
| `fact` | Primary entity table (employee, customer, product, order). |
| `lookup_dimension` | Small reference table (country codes, status enum, types). |
| `general_membership` | Junction table for an M:N relationship — the typical "every X with every Y they're related to". Auto-bridge **prefers** these when picking junctions. |
| `specialization` | Role-specific subset of a relationship (department_manager — only employees who manage a department). Auto-bridge **demotes** these. |
| `audit_log` | History / event log without business-key uniqueness. |
| `configuration` | System / app settings tables. |
| `unknown` | Generator can't classify confidently. |

**Disambiguation guidance:** when faced with two FK-graph junctions linking the same entities (e.g. `department_employee` and `department_manager` both linking `employee↔department`), classify the one that covers ALL members as `general_membership` and the role-specific subset as `specialization`. The view-DDL ranker treats this as authoritative.

Use the same per-table context you already gathered for column descriptions (table name, column descriptions, FK relationships, profiling row_count). Write the file to `metadata/table_descriptions_<timestamp>.json`, then load:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/write_table_descriptions.py metadata/table_descriptions_<timestamp>.json \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

Datasets that already have a current `:TableDescription` are skipped (no overwrite). `status='pending_review'` by default; PO review approves via the existing review surface.

### Step 8c — Generate + load relationship descriptions

After the per-dataset table descriptions are written, generate a one-sentence description per `:Dataset-[:REFERENCES]->:Dataset` FK edge plus a `relationship_nature` classification. This feeds three downstream surfaces: (a) PO review in the new `Relationships` tab in `SourceProductValidationPanel`, (b) the data-mapping skill — `SOURCE_COLUMNS_QUERY` surfaces approved relationship descriptions per source column so the mapping skill can reason about FK semantics (not just FK structure), and (c) the view-DDL bridge ranker.

Walk every `(from:Dataset)-[:REFERENCES]->(to:Dataset)` edge scoped to the project (excluding `from = to` self-references). For each, gather both sides' table descriptions + their relevant column descriptions (the FK columns themselves) and produce one entry like:

```json
{
  "from_dataset_uri": "dataset:<project_code>:public.order_item",
  "to_dataset_uri":   "dataset:<project_code>:public.order_header",
  "text":             "Each order line belongs to exactly one order header; one header has many lines. Standard parent/child shape for per-order aggregations.",
  "relationship_nature": "belongs_to"
}
```

`relationship_nature` is a minimal v1 enum — keep it tight, downstream consumers depend on these specific values:

| value | when |
|---|---|
| `belongs_to` | Child → parent FK. Most common shape. The FROM dataset's rows each refer to one row in the TO dataset (order_item → order_header, address → customer). |
| `categorises` | Dimensional lookup. The TO dataset is a small reference table that categorises the FROM dataset's rows (product → category, customer → country_code lookup). |
| `audit_log_for` | History / event table. The FROM dataset records changes / events about the TO dataset's rows (price_history → product, status_history → order, audit_log → customer). |
| `references` | Generic / unclassifiable. Default when none of the more specific natures fits. |

**Style guidance for the description text** — one or two sentences, factual, name the cardinality (one-to-many, one-to-one) and the use case the relationship enables. Example: "Each price history row tracks one product's price at one point in time; the most recent row per product carries the current list price. Used for SCD-2 price reconstruction."

Write the file to `metadata/relationship_descriptions_<timestamp>.json`, then load:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/write_relationship_descriptions.py metadata/relationship_descriptions_<timestamp>.json \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

Relationships that already have a current `:RelationshipDescription` are skipped (no overwrite). Entries pointing at non-existent `:REFERENCES` edges are rejected by the script — describe real FK edges only. `status='pending_review'` by default; PO review approves via the new Relationships tab in the source-product validation surface.

### Step 9 — Human review

`review_descriptions.py` lives in the `dpe-tools` repo, not the project. Run it
from the project directory so it picks up `.dpe.env` for connection config.

```bash
# Preview pending descriptions without making changes
python /path/to/dpe-tools/review/review_descriptions.py --dry-run

# Run review session
python /path/to/dpe-tools/review/review_descriptions.py --reviewer <name>
```

Per-column prompt:
```
Table:  employees.employee
Column: gender (USER-DEFINED)
  "Gender of the employee; one of two values: M (male) or F (female)."
[A]pprove  [R]eject  [S]kip >
```

On **approve**: description status → `approved`; review `:ProvActivity` + `:ProvAgent` created.

On **reject**: prompted for a corrected description and a rejection category (1–7). Original description → `rejected`/`isCurrent: false`; corrected description created as `approved`/`isCurrent: true`; `:ProvRejectionReason` created.

To approve all pending in one command:
```bash
printf 'A\n%.0s' $(seq 1 <N>) | python scripts/review_descriptions.py \
  --neo4j-password <pass> --reviewer <name>
```

## Notes

- **No overwrite.** Columns with an existing `:ColumnDescription` are always skipped. To clear and regenerate from scratch (also removes all provenance):
  ```cypher
  MATCH (cd:ColumnDescription) DETACH DELETE cd;
  MATCH (act:ProvActivity) DETACH DELETE act;
  MATCH (agent:ProvAgent) DETACH DELETE agent;
  MATCH (r:ProvRejectionReason) DETACH DELETE r;
  ```
- **Verify current descriptions:**
  ```cypher
  MATCH (col:Column)-[:HAS_DESCRIPTION]->(cd:ColumnDescription {isCurrent: true})
  RETURN col.name, cd.text, cd.status
  ORDER BY col.name
  ```
- **Find remaining undescribed columns:**
  ```cypher
  MATCH (ds:Dataset)-[:HAS_COLUMN]->(col:Column)
  WHERE NOT (col)-[:HAS_DESCRIPTION]->(:ColumnDescription)
  RETURN ds.name AS table, col.name AS column
  ORDER BY ds.name, col.ordinal
  ```
- **Full provenance history for a column (one row per activity):**
  ```cypher
  MATCH (col:Column {uri: $uri})-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
  MATCH (cd)-[:PROV_WAS_GENERATED_BY]->(act:ProvActivity)-[:PROV_WAS_ASSOCIATED_WITH]->(agent:ProvAgent)
  OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
  RETURN act.activityType AS activity_type, act.outcome AS outcome, act.occurredAt AS occurred_at,
         agent.name AS agent, cd.text AS description, cd.status AS status, reason.category AS rejection_category
  ORDER BY act.occurredAt
  UNION
  MATCH (col:Column {uri: $uri})-[:HAS_DESCRIPTION]->(cd:ColumnDescription)
  MATCH (act:ProvActivity)-[:PROV_USED]->(cd)
  MATCH (act)-[:PROV_WAS_ASSOCIATED_WITH]->(agent:ProvAgent)
  OPTIONAL MATCH (act)-[:HAS_REJECTION_REASON]->(reason:ProvRejectionReason)
  RETURN act.activityType AS activity_type, act.outcome AS outcome, act.occurredAt AS occurred_at,
         agent.name AS agent, cd.text AS description, cd.status AS status, reason.category AS rejection_category
  ORDER BY act.occurredAt
  ```
- Descriptions are richer when DQV profiling and DQ rules are in the graph — if the `measurements`, `top_values`, and `rules` arrays in the context JSON are empty, descriptions will fall back to name + type inference only.
