# Data Product Spec Writer

Guides the user through authoring a **source-aligned data product specification** in YAML format. Derives defaults from the knowledge graph (DCAT-2 discovery + DQV profiling + SHACL-inspired DQ rules) and asks the user only for information that cannot be inferred automatically.

The spec follows a minimal-viable ODPS/DPROD-compatible structure with 8 sections. One YAML file is written per invocation: `<product_id>_spec.yaml` in the current working directory.

After the YAML is written, a `:DataProduct` node is registered in the Neo4j graph and linked to its `:Dataset` nodes via `[:INCLUDES_DATASET]`. On future runs, those datasets are automatically excluded — only uncovered datasets are eligible for new specs.

---

## Graph model — DataProduct node

```
(:DataProduct {
    uri, id, name, domain, type, status, version,
    owner, steward_team, spec_file, created_at,
    dcatType: 'dprod:DataProduct'
})

(:DataProduct)-[:INCLUDES_DATASET]->(:Dataset)
(:DataProduct)-[:GOVERNED_BY]->(:NodeShape)
(:DataProduct)-[:FROM_CATALOG]->(:Catalog)
```

**Coverage check query** (used internally by the extraction script):
```cypher
MATCH (dp:DataProduct)-[:INCLUDES_DATASET]->(ds:Dataset)
RETURN ds.uri, dp.id, dp.name, dp.status, dp.version
```

Datasets returned by this query are **skipped** unless `--include-covered` is passed.

---

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/extract_spec_defaults.py` | Queries Neo4j; returns eligible (uncovered) datasets with all derivable defaults |
| `scripts/register_data_product.py` | Creates `:DataProduct` node and relationship links after the spec is written |

Both support `--help` and share the same connection flags:
`--host`, `--bolt-port`, `--username`, `--password`, `--database`

---

## extract_spec_defaults.py

```bash
python ${CLAUDE_SKILL_DIR}/scripts/extract_spec_defaults.py [output_file] \
  [--schema SCHEMA] \
  [--include-covered] \
  [--password your_password]
```

**JSON output per eligible table:**
- Row counts, column names, data types, nullability, primary keys
- Foreign keys and back-references (from `:REFERENCES` relationships)
- Profiling metrics (null rates, distinct counts, min/max, percentiles)
- Top frequent values (from `:TopValue` nodes)
- Suggested grain description (derived from PK structure)
- Suggested sensitivity classification (per column, from name heuristics)
- Derived example values (from top_values or percentile_50)
- DQ rules with quality dimensions already mapped (from `:PropertyShape` nodes)
- Quality dimension summary per table
- `skipped[]` — datasets excluded because they already have a `:DataProduct`

---

## register_data_product.py

```bash
python ${CLAUDE_SKILL_DIR}/scripts/register_data_product.py \
  --product-id dp_employees_v1 \
  --product-name "Employees (Source-aligned)" \
  --domain "Human Resources" \
  --status draft \
  --version "1.0.0" \
  --owner "jane.doe@example.com" \
  --steward-team "data-platform" \
  --spec-file "/path/to/dp_employees_v1_spec.yaml" \
  --datasets employees.department employees.employee employees.salary \
             employees.department_employee employees.department_manager employees.title \
  --password your_password
```

- `--datasets` accepts either `schema.table` or `dataset:schema.table` URIs.
- Use `--force` to replace an existing DataProduct with the same ID.
- Exit code 2 if the product ID already exists and `--force` is not set.

---

## Workflow

### Step 1 — Check graph coverage and extract defaults

```bash
python ${CLAUDE_SKILL_DIR}/scripts/extract_spec_defaults.py \
  /tmp/spec_defaults.json \
  --password your_password
```

Parse the JSON output. Then:

**If `skipped` is non-empty**, inform the user:
```
These datasets already belong to a data product and will be skipped:
  employees.department → dp_employees_v1 (Employees v1, status: active)
  ...
```

**If `tables` is empty**, stop and tell the user:
> "All discovered datasets are already covered by existing data products. Nothing to do.
> Use `--include-covered` to re-process them anyway."

**If `tables` is non-empty**, show:
```
Eligible datasets (no DataProduct yet):
  employees.department       (9 rows,     2 columns,  6 DQ rules)
  employees.employee         (300,024 rows, 6 columns, 10 DQ rules)
  ...
Total: X table(s), Y column(s), Z DQ rules
```

---

### Step 1b — Target platform

**Ask this immediately after showing eligible datasets, before any other section.**

> "Which platform will this data product be served from?"
> ```
> 1. Snowflake
> 2. Databricks (Unity Catalog)
> 3. PostgreSQL
> ```

Store the answer as `target_platform`. Use it throughout the remaining steps to tailor:
- `operations.platform_location` format and defaults
- `access.enforcement` platform name and role naming conventions
- Masking rule descriptions and idioms
- Any platform-specific field hints shown to the user

**Platform-specific defaults:**

| Field | Snowflake | Databricks | PostgreSQL |
|---|---|---|---|
| `access.enforcement.platform` | `Snowflake` | `Databricks Unity Catalog` | `PostgreSQL` |
| Role name convention | `DP_<PRODUCT>_READ` (uppercase) | `dp_<product>_read` (Unity Catalog group) | `dp_<product>_read` (role) |
| `platform_location` format | `<DATABASE>.<SCHEMA>` | `<CATALOG>.<SCHEMA>` | `<SCHEMA> on <host>` |
| Masking idiom | "Snowflake Dynamic Data Masking policy" | "Databricks column mask function" | "PostgreSQL column-level privilege / view" |
| Enforcement mechanism | "RBAC via Snowflake grants; row/column policies" | "Unity Catalog table ACLs and column masks" | "PostgreSQL GRANT / RLS policy" |

When asking Section 6 (Access) and the user says "use defaults", substitute the platform-appropriate values automatically.

---

### Step 2 — Section 1: Identity & ownership

Ask the user (in one message) for the following. Show suggested values where available.

| Field | Derived? | Notes |
|---|---|---|
| `name` | No | Business-friendly product name |
| `id` | Suggested | `dp_<schema>_v1` — confirm or override |
| `domain` | No | Business domain / area |
| `type` | Fixed | `source-aligned` |
| `status` | Default `draft` | draft / active / deprecated |
| `owner` | No | Person or role (email preferred) |
| `steward_team` | No | Engineering team responsible |
| `contact_channel` | No | Slack channel, email list, etc. |

Present as: "Here are my defaults — please fill in the blanks or correct anything:"

---

### Step 3 — Section 2: Purpose & consumers

Ask in one message:

1. **Business purpose** (1–3 sentences): Why does this data product exist?
2. **Primary consumers**: Which teams or roles will use this?
3. **Key use cases**: Bullet list of 2–5 use cases.
4. **Non-goals**: What does this product intentionally NOT cover? (optional)

---

### Step 4 — Section 3: Data scope & semantics

**4a — Source systems**
Ask: "What are the upstream source systems of record? (e.g., 'CRM PostgreSQL', 'SAP HR module')"

**4b — Retention policy**
Ask: "What is the data retention policy? (e.g., '13 months', 'indefinite', '7 years')"

**4c — Per-table grain**
For each table from the extracted JSON, show the suggested grain and ask to confirm:
```
Table: employee  (300,024 rows)
  PK: [id]
  Suggested grain: "1 row per employee"
  → Correct? If not, please provide the grain description.
```

**4d — Per-column business metadata**
For each table, present a compact column table pre-filled from graph data and ask for `business_name`, `definition`, and any sensitivity override:

```
Table: employee
  # | name        | type    | nullable | sensitivity     | example
  --+-------------+---------+----------+-----------------+---------
  1 | id          | bigint  | false    | internal        | 10001
  2 | birth_date  | date    | false    | restricted      | 1953-09-02
  3 | first_name  | varchar | false    | confidential    | Georgi
  4 | last_name   | varchar | false    | confidential    | Facello
  5 | gender      | char    | false    | confidential    | M
  6 | hire_date   | date    | false    | internal        | 1986-06-26

  Please provide for each column:
    - business_name (or confirm physical name is fine)
    - definition (short, consumer-friendly)
    - sensitivity override if the suggestion is wrong
    - Any columns needing masking or tokenization?
```

Allow free-form responses. If the user says "use defaults", keep physical names and mark definitions as `# TODO`.

---

### Step 5 — Section 4: Quality expectations

Pre-fill from the `:PropertyShape` nodes in the extracted JSON. Show per table:
```
Table: employee
  Dimensions covered: completeness (6 cols), validity (3 cols), uniqueness (1 col)
  Rules: 10 total  (mandatory: 6, range: 3, unique: 1)
  No referential integrity rules (no outbound FK from this table)
```

Then ask:
1. **Overall quality SLA**: e.g., "No more than 0.1% rows failing critical checks per load."
2. **Data issue handling**: How are incidents raised, tracked, communicated?
3. **Test execution point**: ingest / post-transform / daily / on-demand?

---

### Step 6 — Section 5: Operational characteristics

Ask in one message:
1. **Refresh pattern**: schedule and mode (batch / CDC / streaming)
2. **Latency target**: "Data within X minutes/hours of source"
3. **Availability SLA**: e.g., "99.5% monthly"
4. **Platform location**: e.g., "PostgreSQL employees schema on prod-db-01"
5. **Load window / unavailability** (optional)
6. **Backfill behavior** (optional)

---

### Step 7 — Section 6: Access, security & compliance

Ask in one message. Pre-fill enforcement fields using `target_platform` defaults (from Step 1b):
1. **Product-level sensitivity**: public / internal / confidential / restricted
2. **Access model**: open / request-approval / role-based / restricted
3. **Enforcement platform**: pre-filled from `target_platform` — confirm or override
4. **Role name**: suggest `DP_<PRODUCT_ID_UPPER>_READ` (Snowflake) / `dp_<product_id>_read` (Databricks/PostgreSQL) — confirm or override
5. **Compliance constraints** (optional): GDPR, HIPAA, PCI, residency, etc.
6. **Use restrictions** (optional)
7. For columns flagged `confidential` or `restricted`: any masking rules needed?
   - If yes, describe them using the platform's masking idiom (from Step 1b defaults)

---

### Step 8 — Section 7: Lineage & dependencies

Pre-fill FK relationships from the extracted JSON's `foreign_keys` and `referenced_by` fields. Show the user:
```
FK relationships found in graph:
  department_employee.employee_id  → employee.id
  department_employee.department_id → department.id
  salary.employee_id               → employee.id
  title.employee_id                → employee.id
  department_manager.employee_id   → employee.id
  department_manager.department_id → department.id
```

Then ask:
1. **Upstream source system(s)**: names and owners
2. **Ingestion pipeline(s)**: job names, repo links, orchestration IDs
3. **Transformations applied** relative to source (type casting, renames, masking, etc.)
4. **Downstream critical consumers** (optional)

---

### Step 9 — Section 8: Change & versioning

Ask in one message:
1. **Version**: default `1.0.0`
2. **Initial changelog entry**: one line for this version
3. **Breaking change policy**: suggest: "Removal or rename of columns, semantic changes to PKs, or grain changes require a major version bump and 60-day deprecation notice."
4. **Deprecation strategy** (optional)
5. **Planned changes** (optional)

---

### Step 10 — Write the YAML spec

Assemble all answers and graph defaults into the final YAML. Write to:
```
<current_working_directory>/<product_id>_spec.yaml
```

Use `null` for any field the user did not provide. Use `# TODO` comments for fields that need follow-up.

Tell the user the output path and a brief summary (tables, columns, rules, sections written).

---

### Step 11 — Register the DataProduct in the graph

After the YAML is written, run the registration script:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/register_data_product.py \
  --product-id <product_id> \
  --product-name "<product_name>" \
  --domain "<domain>" \
  --status <status> \
  --version "<version>" \
  --owner "<owner>" \
  --steward-team "<steward_team>" \
  --spec-file "<absolute_path_to_yaml>" \
  --datasets <schema.table1> <schema.table2> ... \
  --password your_password
```

Use the values from the user's answers. The `--datasets` list should include all tables included in the product.

Report what was created:
```
Registered in graph:
  :DataProduct  uri=dataproduct:<product_id>
  [:INCLUDES_DATASET] × N datasets
  [:GOVERNED_BY]      × N NodeShapes
  [:FROM_CATALOG]     × N Catalogs
```

These datasets will now be excluded from future spec-writer runs.

---

## YAML Output Structure

```yaml
product:
  id: dp_employees_v1
  name: "Employees (Source-aligned)"
  domain: Human Resources
  type: source-aligned
  status: draft
  owner: null  # TODO
  steward_team: null  # TODO
  contact_channel: null  # TODO

  purpose:
    business_purpose: >
      TODO — describe why this product exists.
    primary_consumers:
      - TODO
    key_use_cases:
      - TODO
    non_goals: []

  operations:
    source_systems:
      - name: TODO
        owner: TODO
    refresh_pattern: null  # TODO
    latency_target_minutes: null  # TODO
    availability_sla: null  # TODO
    retention: null  # TODO
    platform_location: null  # TODO
    load_window: null
    backfill_behavior: null

  access:
    classification: internal
    access_model: request-approval
    enforcement:
      platform: null  # TODO
      roles: []
    masking_rules: []
    compliance_constraints: []
    restrictions: []

  quality:
    overall_sla: null  # TODO
    issue_handling: null  # TODO
    test_execution_point: null  # e.g. ingest / daily

  tables:
    - name: employee
      label: "Employee"
      grain: "1 row per employee"
      primary_key: [id]
      row_count: 300024
      upstream_sources:
        - TODO
      foreign_keys: []
      referenced_by:
        - from_table: salary
          columns: [employee_id]
          referenced_columns: [id]
      columns:
        - name: id
          business_name: "Employee ID"
          definition: "Unique identifier for an employee"
          data_type: bigint
          nullable: false
          classification: internal
          example_value: "10001"
          quality:
            dimensions: [completeness, uniqueness]
            tests:
              - id: rule_employees_employee_id_mandatory
                rule: "id must not be null"
                severity: critical
                threshold: "100%"
                execution_point: ingest
              - id: rule_employees_employee_id_unique
                rule: "id values must be unique"
                severity: critical
                threshold: "100%"
                execution_point: ingest

  lineage:
    upstream_systems:
      - name: TODO
        owner: TODO
    ingestion_pipelines:
      - name: TODO
        repo: null
        orchestration_id: null
    transformations_applied:
      - TODO
    downstream_consumers: []

  versioning:
    version: "1.0.0"
    changelog:
      - version: "1.0.0"
        date: null  # TODO
        description: "Initial version"
    breaking_change_policy: >
      Removal or rename of columns, semantic changes to primary keys,
      or changes to grain require a major version bump and 60-day
      deprecation notice.
    deprecation_strategy: null
    planned_changes: []
```

---

## Field derivation reference

| YAML field | Source in graph |
|---|---|
| `tables[].name` | `:Dataset.name` |
| `tables[].row_count` | `:Dataset.row_count` |
| `tables[].primary_key` | `:Column.primaryKey = true` |
| `tables[].foreign_keys` | `(:Dataset)-[:REFERENCES]->(:Dataset)` |
| `tables[].referenced_by` | Reverse `:REFERENCES` relationships |
| `columns[].data_type` | `:Column.dataType` |
| `columns[].nullable` | `:Column.nullable` |
| `columns[].example_value` | `:TopValue` (top by frequency) or `percentile_50` |
| `columns[].classification` | Heuristic from column name (name patterns) |
| `columns[].quality.dimensions` | Mapped from `:PropertyShape.ruleType` |
| `columns[].quality.tests[].rule` | Derived from `:PropertyShape` description + thresholds |
| `columns[].quality.tests[].severity` | `sh:Violation` → `critical`, `sh:Warning` → `warning` |
| `columns[].quality.tests[].threshold` | From `null_rate`, `uniqueness_threshold`, `coverage` |

## Severity mapping

- `sh:Violation` → `critical`
- `sh:Warning` → `warning`

## Rule type → plain-language template

| Rule type | Template |
|---|---|
| `mandatory` | `"<col> must not be null"` |
| `range` | `"<col> must be between <min> and <max>"` |
| `unique` | `"<col> values must be unique (ratio ≥ <threshold>)"` |
| `allowedValues` | `"<col> must be one of {<values>}"` |
| `referentialIntegrity` | `"<col> must reference a valid <target_table>"` |

---

## Notes

- Re-generating is safe — the YAML file is overwritten. The graph registration uses `--force` only if explicitly asked.
- If no profiling or DQ rules are found in the graph, the spec is still generated with structural defaults; quality tests will contain `# TODO`.
- The spec uses plain-language rules, not SQL/Cypher — it is a governance document. Link to `dq_tests_python/` or `dq_tests_gx/` for runnable tests.
- To re-process already-covered datasets: pass `--include-covered` to the extraction script and `--force` to the registration script.
