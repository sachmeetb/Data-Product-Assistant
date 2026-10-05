---
name: data-discovery
description: Extracts and documents PostgreSQL database metadata for data discovery and cataloging. Use this skill whenever the user wants to explore, catalog, document, or extract schema information from a PostgreSQL database — including tables, columns, data types, primary keys, foreign keys, indexes, unique constraints, and check constraints. Trigger when the user mentions "data discovery", "database schema", "extract metadata", "catalog tables", "document my database", "what tables do I have", or wants to understand the structure of a PostgreSQL database. Also trigger when the user provides a PostgreSQL connection string and wants schema information.
---

# Data Discovery — PostgreSQL

Connects to a PostgreSQL database, lets the user browse and select schemas and tables, then extracts full metadata for each selected table into one YAML file per table in the current working directory.

All extraction logic is in bundled scripts — do not rewrite them. Just run them with the right arguments.

## Scripts

All scripts are in the `scripts/` directory relative to this SKILL.md file.

| Script | Purpose |
|--------|---------|
| `discover_schemas.py` | Lists all non-system schemas |
| `discover_tables.py` | Lists all tables in selected schemas |
| `extract_metadata.py` | Extracts full metadata and writes YAML files |

Each script installs its own dependencies (`psycopg2-binary`, `pyyaml`) if missing.

## Workflow

### Step 1: Get the connection string

Ask the user for their PostgreSQL connection string:
```
postgresql://user:password@host:port/database
```
If they already provided it, use it directly.

### Step 2: Discover schemas

```bash
python ${CLAUDE_SKILL_DIR}/scripts/discover_schemas.py "<connection_string>"
```

Show the user the numbered list of schemas.

### Step 3: Ask which schemas to include

Ask the user which schemas to extract. If the agent messaging system is available (i.e. you have a `run_id`), use a `checklist` message type with each schema as an option so the user can select from checkboxes in the UI. Otherwise ask in plain text: "Which schemas would you like to extract metadata from? You can name specific ones or say 'all'."

### Step 4: List tables

```bash
python ${CLAUDE_SKILL_DIR}/scripts/discover_tables.py "<connection_string>" <schema1> [schema2 ...]
```

Show the user the tables grouped by schema.

### Step 5: Ask which tables to extract

Ask the user which tables to extract. If the agent messaging system is available (i.e. you have a `run_id`), use a `checklist` message type with each table as an option (use `schema.table` as the value, table name as the label, and schema as the description) so the user can select from checkboxes in the UI. Otherwise ask in plain text: "Which tables would you like to extract? List specific ones or say 'all'."

### Step 6: Extract metadata and write YAML

```bash
python ${CLAUDE_SKILL_DIR}/scripts/extract_metadata.py "<connection_string>" "<output_dir>" <schema1.table1> [schema2.table2 ...]
```

- `output_dir` is the user's current working directory
- Each table is passed as `schema.table` (e.g. `employees.salary`)
- One YAML file per table is written as `<schema>__<table>.yaml`

Tell the user which files were written when done.

## YAML Output Format

Each file follows this structure:

```yaml
schema: public
table: orders
comment: null

columns:
  - name: id
    ordinal: 1
    type: integer
    character_maximum_length: null
    numeric_precision: 32
    numeric_scale: 0
    nullable: false
    default: "nextval('orders_id_seq'::regclass)"
    comment: null

primary_key:
  constraint_name: orders_pkey
  columns: [id]

foreign_keys:
  - constraint_name: orders_customer_id_fkey
    columns: [customer_id]
    referenced_schema: public
    referenced_table: customers
    referenced_columns: [id]
    on_delete: CASCADE
    on_update: NO ACTION

unique_constraints: []
indexes: []
check_constraints: []
```

## Error Handling

- Connection failure: show the error and ask the user to verify the connection string
- Table extraction failure: the script prints a warning and continues — don't abort the whole run
- Missing dependencies: scripts self-install via pip
