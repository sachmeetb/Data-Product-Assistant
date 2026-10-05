---
name: data-serving-virtual-view
description: Generates a virtual view DDL (CREATE VIEW) for a data product by reading approved column mappings and source table relationships from a Neo4j knowledge graph. The view composes the data product logically over the source system with no data copying. Use this skill when the user wants to materialize a data product as a view, generate SQL for a virtual data product, create a view over source tables, or serve a data product via a logical view. Supports PostgreSQL initially, with extensibility for other platforms.
---

# Data Serving — Virtual View

Generates one `CREATE OR REPLACE VIEW` DDL statement **per `:DProdOutputDataset`** linked to the data product. The views are derived from approved column mappings in the Neo4j knowledge graph, using foreign key relationships to determine join conditions where needed.

**Serving mode: virtual view over source** — no data is copied; each output dataset is exposed as a SQL view that joins/filters source tables on read.

## The deploy is a package, not a one-off

The DDL you emit here is not deployed by a bespoke internal path. The backend assembles a **self-contained, downloadable deploy package** (`serving_package.assemble_view_package`) — `view.sql` (your DDL) + a stdlib `run.py` runner + `.env.example` + `requirements.txt` + README — and Data Workbench deploys by running that package's `run.py`, which applies the DDL to the target (with the `CREATE OR REPLACE VIEW` recovery for column-type drift) and writes a structured `run_result.json`. An engineer downloads the identical package and runs it with their own `.env`. So your only job stays: emit correct DDL + the `.summary.json` sidecar. Keep the DDL platform-neutral where possible (the runner handles per-platform quoting/apply); the deploy script and docs are owned by the backend + `data-serving-dbt-documenter`/fallbacks, not authored here.

## Output invariant — load-bearing

**Exactly one `CREATE OR REPLACE VIEW` is emitted per `:DProdOutputDataset` linked to the product.** Source-aligned (`dpe-sa`) products are 1:1 with discovered source tables, so emit N views for N source tables (typically `SELECT col AS alias FROM <source_table>` with no joins). Consumer-aligned (`dpe-cf`) products usually carry a single output dataset and emit one multi-table joined view.

**Joins are required, never speculative.** Only join source tables that contribute a mapped column to the dataset under generation. If a required source table cannot be reached via FK from the FROM-clause root, `generate_view_ddl.py` exits non-zero with a clear error — it never falls back to `CROSS JOIN`, which would silently emit Cartesian-product data.

**Post-generation self-check.** After generating `serving/virtual_view.sql`, count the `CREATE OR REPLACE VIEW` lines in the file. That count MUST equal the number of `:DProdOutputDataset` nodes linked to the product. If they don't match, stop and report the mismatch — do not proceed to storage.

## Prerequisites

- Data mapping stage completed with approved mappings in Neo4j
- Source table FK relationships loaded (from data discovery)
- `neo4j` and `psycopg2-binary` Python packages installed

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/generate_view_ddl.py` | Query graph for mappings and FKs, generate CREATE VIEW DDL |
| `scripts/store_serving_definition.py` | Store the serving definition (DDL, mode, platform) in Neo4j |

All scripts support `--help` and `--dry-run`.

## Graph Model

### Nodes Created

```
(:DProdDataProduct)
  -[:SERVED_BY]-> (:ServingDefinition {
      servingMode:    "virtual_view",
      targetPlatform: "postgresql",
      viewName:       "vw_<first_dataset_physical_name>",  // first view, back-compat
      viewNames:      "[\"vw_a\", \"vw_b\", ...]",          // JSON array of every view emitted
      viewCount:      <int>,
      viewSchema:     "<schema>",
      ddl:            "CREATE OR REPLACE VIEW ... ;\n\nCREATE OR REPLACE VIEW ... ;",
      summaryJson:    "{\"views\": [{\"view_name\": ..., \"multiplication_warnings\": [...], \"auto_bridge_choice\": [...], \"scd_warning\": null, ...}], ...}",
      createdAt:      <datetime>
  })
```

A product has at most one `:ServingDefinition`; its `ddl` field carries every `CREATE VIEW` concatenated and `summaryJson` carries the structured per-view summary (warnings, auto-bridge picks, SCD lowering state) that the UI renders as a callout above the DDL.

### Query for output datasets (drives the loop)

```cypher
MATCH (dp:DProdDataProduct {uri: $product_uri})
      -[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset)
RETURN ods.uri AS uri,
       coalesce(ods.physicalName, ods.name) AS physical_name
ORDER BY physical_name
```

### Query for Approved Mappings (per output dataset)

```cypher
MATCH (dp:DProdDataProduct {uri: $product_uri})
      -[:DPROD_OUTPUT_PORT]->()-[:DPROD_OUTPUT_DATASET]->(ods:DProdOutputDataset {uri: $output_dataset_uri})
      -[:HAS_PRODUCT_COLUMN]->(pc:DProdColumn)
MATCH (cm:ColumnMapping {isCurrent: true, status: 'approved'})
      -[:MAPS_TO_PRODUCT_COLUMN]->(pc)
MATCH (cm)-[:MAPS_SOURCE_COLUMN]->(sc:Column)<-[:HAS_COLUMN]-(ds:Dataset)
RETURN
    pc.name AS product_col, pc.dataType AS product_type,
    sc.name AS source_col, sc.dataType AS source_type,
    ds.schema AS source_schema, ds.name AS source_table,
    cm.uri AS mapping_uri,
    cm.mappingType AS mapping_type,
    cm.transformKind AS transform_kind,
    cm.transformExpression AS transform_expression,
    cm.transformInputs AS transform_inputs_json,
    cm.transformParams AS transform_params_json,
    cm.transformDecorators AS transform_decorators_json,
    ds.uri AS dataset_uri
ORDER BY pc.name
```

### Transformation compilation

For each product column, the generator compiles a SELECT-clause expression based on the
mapping's `transformKind`:

| Kind | SQL emitted |
|------|-------------|
| `direct` | `tN.col` |
| `cast` | `CAST(tN.col AS <target_type>)` (driven by `transformExpression` if explicit) |
| `format` | `UPPER(...)` / `LOWER(...)` / `TRIM(...)` / `TO_CHAR(...)` per `transformParams` |
| `concat` | `transformExpression` with bare column names substituted to `tN.col` refs |
| `split` | `SPLIT_PART(tN.col, '<d>', <i>)` (driven by `transformExpression`) |
| `substring` | `SUBSTRING(tN.col FROM <s> FOR <l>)` (driven by `transformExpression`) |
| `case` | `CASE WHEN ... END` (driven by `transformExpression`) |
| `arithmetic` | `transformExpression` with bare column names substituted |
| `lookup` | `lkN.<value_column>` plus an implicit `LEFT JOIN <lookup_table> AS lkN ON tN.<src> = lkN.<key_column>` |
| `expression` | `transformExpression` verbatim with bare column names substituted (engineer escape hatch) |

Substitution of bare column names inside `transformExpression` is **word-boundary** —
a column called `id` will not match inside `customer_id`. The substitution is driven by
the JSON array stored in `transformInputs`.

Decorators (`standardization`, `default_if_null`) wrap the compiled expression:

| Decorator | Effect |
|-----------|--------|
| `standardization: ["trim"]` | wraps in `TRIM(...)` |
| `standardization: ["upper"]` | wraps in `UPPER(...)` |
| `standardization: ["lower"]` | wraps in `LOWER(...)` |
| `standardization: ["normalize_whitespace"]` | wraps in `REGEXP_REPLACE(..., '\s+', ' ', 'g')` |
| `default_if_null: "X"` | wraps in `COALESCE(..., 'X')` |

### Query for FK Join Conditions

```cypher
MATCH (ds1:Dataset)-[r:REFERENCES]->(ds2:Dataset)
WHERE ds1.uri IN $dataset_uris OR ds2.uri IN $dataset_uris
OPTIONAL MATCH (ds1)-[:HAS_RELATIONSHIP_DESCRIPTION]->(rd:RelationshipDescription
                                                     {isCurrent: true, status: 'approved'})
              -[:DESCRIBES_REFERENCE_TO]->(ds2)
RETURN ds1.schema AS from_schema, ds1.name AS from_table,
       r.columns AS fk_columns,
       ds2.schema AS to_schema, ds2.name AS to_table,
       r.referencedColumns AS pk_columns,
       coalesce(rd.relationshipNature, '') AS relationship_nature,
       coalesce(rd.text, '')               AS relationship_text
```

The optional `relationship_nature` field carries the PO-approved classification of the FK edge — `belongs_to` / `categorises` / `audit_log_for` / `references`. It informs **bridge ranking** when multiple FK paths connect the same pair of mapped tables.

### Bridge ranking — when multiple FK paths exist

When the FK graph offers two or more equally-short paths between mapped tables (e.g. `customer → department_employee → department` vs `customer → department_manager → department`), `_resolve_bridges` ranks candidates using two signals, in order:

1. **`:TableDescription.relationshipKind`** on the *bridge table itself* — `general_membership` outranks `specialization` (the manager-only junction loses to the all-employees junction because the manager subset would silently drop most rows).

2. **`:RelationshipDescription.relationshipNature`** on the *FK edges from the bridge* — `belongs_to` and `categorises` are preferred over `audit_log_for` (history/event tables shouldn't be chosen as join paths for current-state queries; they multiply rows by event count).

If both signals are tied or absent, the ranker raises `_AmbiguousBridges` and the engineer must either author an explicit `:DatasetTransform.joins[]` override or ask the PO to fill in the missing classifications. Don't pick blindly — wrong data is worse than no data.

When you emit a bridge into the view DDL, name the rationale in the `auto-bridge` comment (e.g. `-- auto-bridge: customer → department_employee → department (general_membership, belongs_to)`) so reviewers can audit the choice without re-running the ranker.

## Workflow

### Step 1 — Get connection details

| Setting | Default |
|---------|---------|
| Host | `localhost` |
| Bolt port | `7687` |
| Username | `neo4j` |
| Password | `your_password` |
| Database | `neo4j` |

### Step 2 — Generate the view DDL(s)

```bash
python ${CLAUDE_SKILL_DIR}/scripts/generate_view_ddl.py \
  --product-uri "dprod:<contract_id>" \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db> \
  --output serving/virtual_view.sql \
  --dialect postgres
```

The `--dialect` flag (default `postgres`) selects the SQL dialect for emission. Accepted values: `postgres` (alias `postgresql`), `snowflake`, `databricks`, `bigquery`, `ansi`. The dialect affects hash function bodies (PG uses `encode(digest(...), 'hex')` via pgcrypto; Snowflake/Databricks use `SHA2(...)`; BigQuery uses `TO_HEX(SHA256(...))`), type-cast syntax (PG `::type` vs ANSI `CAST(... AS type)`), and REGEXP_REPLACE flags (PG's explicit `'g'` vs implicit-global elsewhere). The chosen dialect name lands in the sidecar summary's `dialect` field; `store_serving_definition.py` reads it and sets `:ServingDefinition.targetPlatform` accordingly.

The script:
1. Queries every `:DProdOutputDataset` linked to the product.
2. For each output dataset, queries the approved mappings whose product column belongs to that dataset.
3. Determines the source tables strictly required by those mappings.
4. Queries FK relationships only between those tables, and emits `LEFT JOIN`s. When mapped tables span source products with no FK edge, it auto-bridges on shared identity keys — **chaining transitively** through intermediates, and when no shared key exists at all, synthesizing a **junction dataset from a CONSUMES'd product** as a pure bridge (`SELECT DISTINCT` of the link keys, contributing no SELECT columns).
5. **Refuses to emit `CROSS JOIN` fallbacks** — if a required source table has no FK path, no chainable identity key, and no unambiguous junction, the script exits non-zero with a clear error (tied junction candidates are listed, never guessed).
6. Names each view `vw_<output_dataset_physical_name>` (snake-cased, non-alphanumerics → `_`).
7. Concatenates every `CREATE OR REPLACE VIEW` statement into the output file.

### Step 3 — Self-check the view count

Count the `CREATE OR REPLACE VIEW` statements in `serving/virtual_view.sql` and compare to the number of `:DProdOutputDataset` nodes linked to the product. They MUST be equal. Example:

```bash
grep -c "^CREATE OR REPLACE VIEW" serving/virtual_view.sql
```

If they don't match, stop and surface the mismatch — do NOT proceed to storage.

### Step 4 — Store serving definition in Neo4j

```bash
python ${CLAUDE_SKILL_DIR}/scripts/store_serving_definition.py \
  --product-uri "dprod:<contract_id>" \
  --ddl-file serving/virtual_view.sql \
  --platform postgresql \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

The script auto-extracts every view name from the DDL file; `--view-name` is optional and only overrides the "primary" name shown to single-view readers.

It also auto-loads the sidecar summary at `<ddl-file>.summary.json` (written by `generate_view_ddl.py` alongside the DDL) and persists it on `:ServingDefinition.summaryJson`. The UI renders the structured warnings (multiplication risks, auto-bridge picks, SCD lowering state, under-specified aggregates) from this field; if the sidecar is missing the storage still succeeds, the UI just won't show the callout. Pass `--summary-file` to override the auto-located path.

### Step 5 — Report

Show the user:
- The number of views emitted and their names
- For each view: which source tables it draws from, how many product columns it serves
- Any output datasets that were skipped (zero approved mappings) — these are gaps
- The full DDL
