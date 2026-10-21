---
name: data-discovery-snowflake
description: Extracts and documents Snowflake database metadata for data discovery and cataloging. Use this skill whenever the user wants to explore, catalog, document, or extract schema information from a Snowflake database — including tables, columns, data types, primary keys, foreign keys, unique constraints, and check constraints. Trigger when the user mentions "data discovery", "database schema", "extract metadata", "catalog tables", "document my database", "what tables do I have", or wants to understand the structure of a Snowflake database. Also trigger when the user provides a Snowflake connection string and wants schema information.
---

# Data Discovery — Snowflake

Connects to a Snowflake database, lets the user browse and select schemas and tables, then extracts full metadata for each selected table into one YAML file per table in the current working directory.

All extraction logic is in bundled scripts — do not rewrite them. Just run them with the right arguments.

## Scripts

All scripts are in the `scripts/` directory relative to this SKILL.md file.

| Script | Purpose |
|--------|---------|
| `discover_schemas.py` | Lists all non-system schemas in the connected database |
| `discover_tables.py` | Lists all tables in selected schemas |
| `extract_metadata.py` | Extracts full metadata and writes YAML files |

Each script self-installs its own dependencies (`snowflake-connector-python`, `pyyaml`) if missing.

## Connection string format

```
snowflake://user:password@account/database?warehouse=COMPUTE_WH&role=SYSADMIN
```

- `account`: Snowflake account identifier (e.g. `myorg-myaccount` or `xy12345.us-east-1`)
- `database`: the Snowflake database to connect to (schemas are discovered within it)
- `warehouse`: optional — SQL warehouse to use for queries
- `role`: optional — Snowflake role to assume

> **Credential containment (Data Workbench pipeline).** When this skill runs as a
> Workbench stage, the credential-bearing DSN is provided through the
> **`WB_SOURCE_DSN` environment variable** — NOT the prompt. Every script reads
> it automatically. Invoke each script with `"$WB_SOURCE_DSN"` as the connection
> argument (the shell expands it at run time) — or omit the argument entirely and
> the script falls back to the env var. **Never** paste a literal DSN with a
> password into a command; the password must stay out of the transcript.

## Workflow

> **FAST PATH (Data Workbench pipeline) — skip the browse steps.** When the prompt
> already tells you WHICH tables to discover (Data Workbench always provides the
> engineer's selected `schema.table` list, e.g. via `discovery_tables`), do **not**
> run `discover_schemas.py` or `discover_tables.py` — those are for a human browsing
> an unknown database, and each opens a *separate* Snowflake connection (~3s each).
> Go **straight to Step 6** (`extract_metadata.py`) with the given tables. Only fall
> back to the interactive browse (Steps 2–5) when NO tables were specified.

### Step 1: Get the connection string

When run by Data Workbench, use `"$WB_SOURCE_DSN"` as the connection argument (or
omit it) — the DSN is in the environment. Only if a human is driving this skill
interactively should you ask for a literal connection string:
```
snowflake://user:password@account/database?warehouse=COMPUTE_WH&role=SYSADMIN
```

### Step 2: Discover schemas  *(interactive/browse only — skip when tables are given)*

```bash
python ${CLAUDE_SKILL_DIR}/scripts/discover_schemas.py "$WB_SOURCE_DSN"
```

Show the user the numbered list of schemas. The `INFORMATION_SCHEMA` system schema is automatically excluded.

### Step 3: Ask which schemas to include

"Which schemas would you like to extract metadata from? You can name specific ones or say 'all'."

### Step 4: List tables

```bash
python ${CLAUDE_SKILL_DIR}/scripts/discover_tables.py "$WB_SOURCE_DSN" <schema1> [schema2 ...]
```

Show the user the tables grouped by schema.

### Step 5: Ask which tables to extract

"Which tables would you like to extract? List specific ones or say 'all'."

### Step 6: Extract metadata and write YAML

```bash
python ${CLAUDE_SKILL_DIR}/scripts/extract_metadata.py "$WB_SOURCE_DSN" "<output_dir>" <schema1.table1> [schema2.table2 ...]
```

- `output_dir` is the user's current working directory
- Each table is passed as `schema.table` (e.g. `sales.orders`)
- One YAML file per table is written as `<schema>__<table>.yaml`

Tell the user which files were written when done.

## YAML Output Format

Each file follows this structure:

```yaml
schema: sales
table: orders
comment: null

columns:
  - name: id
    ordinal: 1
    type: NUMBER
    character_maximum_length: null
    numeric_precision: 38
    numeric_scale: 0
    nullable: false
    default: null
    comment: null

primary_key:
  constraint_name: SYS_CONSTRAINT_xxxx
  columns: [id]

foreign_keys:
  - constraint_name: orders_customer_id_fkey
    columns: [customer_id]
    referenced_schema: sales
    referenced_table: customers
    referenced_columns: [id]
    on_delete: NO ACTION
    on_update: NO ACTION

unique_constraints: []
indexes: []
check_constraints: []
```

## Notes

- In Snowflake, constraints (PRIMARY KEY, FOREIGN KEY, UNIQUE, CHECK) are unenforced by default — they exist as metadata only. The scripts extract them as-is from `INFORMATION_SCHEMA`.
- Snowflake semi-structured types (`VARIANT`, `ARRAY`, `OBJECT`) appear in the `type` field as-is.
- All `INFORMATION_SCHEMA` queries are scoped to the database specified in the connection string.
- The output YAML format is identical to the PostgreSQL and MySQL `data-discovery` skills, making it compatible with the `data-discovery-to-dcat-neo4j`, `data-profiling-snowflake`, and all downstream skills.

## Error Handling

- Connection failure: show the error and ask the user to verify the connection string
- Table extraction failure: the script prints a warning and continues — don't abort the whole run
- Missing dependencies: scripts self-install via pip
