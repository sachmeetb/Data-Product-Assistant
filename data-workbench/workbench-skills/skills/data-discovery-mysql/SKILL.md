---
name: data-discovery-mysql
description: Extracts and documents MySQL database metadata for data discovery and cataloging. Use this skill whenever the user wants to explore, catalog, document, or extract schema information from a MySQL database — including tables, columns, data types, primary keys, foreign keys, indexes, unique constraints, and check constraints. Trigger when the user mentions "data discovery", "database schema", "extract metadata", "catalog tables", "document my database", "what tables do I have", or wants to understand the structure of a MySQL database. Also trigger when the user provides a MySQL connection string and wants schema information.
---

# Data Discovery — MySQL

Connects to a MySQL server, lets the user browse and select schemas (databases) and tables, then extracts full metadata for each selected table into one YAML file per table in the current working directory.

All extraction logic is in bundled scripts — do not rewrite them. Just run them with the right arguments.

## Scripts

All scripts are in the `scripts/` directory relative to this SKILL.md file.

| Script | Purpose |
|--------|---------|
| `discover_schemas.py` | Lists all non-system schemas (databases) |
| `discover_tables.py` | Lists all tables in selected schemas |
| `extract_metadata.py` | Extracts full metadata and writes YAML files |

Each script self-installs its own dependencies (`pymysql`, `pyyaml`) if missing.

## Connection string format

```
mysql://user:password@host:port/database
```

The database component is optional for discovery — the scripts connect at the server level to list all accessible schemas.

## Workflow

### Step 1: Get the connection string

Ask the user for their MySQL connection string:
```
mysql://user:password@host:port/database
```
If they already provided it, use it directly.

### Step 2: Discover schemas

```bash
python ${CLAUDE_SKILL_DIR}/scripts/discover_schemas.py "<connection_string>"
```

Show the user the numbered list of schemas. System schemas (`information_schema`, `mysql`, `performance_schema`, `sys`) are automatically excluded.

### Step 3: Ask which schemas to include

"Which schemas would you like to extract metadata from? You can name specific ones or say 'all'."

### Step 4: List tables

```bash
python ${CLAUDE_SKILL_DIR}/scripts/discover_tables.py "<connection_string>" <schema1> [schema2 ...]
```

Show the user the tables grouped by schema.

### Step 5: Ask which tables to extract

"Which tables would you like to extract? List specific ones or say 'all'."

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
schema: employees
table: orders
comment: null

columns:
  - name: id
    ordinal: 1
    type: int
    character_maximum_length: null
    numeric_precision: 10
    numeric_scale: 0
    nullable: false
    default: null
    comment: null

primary_key:
  constraint_name: PRIMARY
  columns: [id]

foreign_keys:
  - constraint_name: orders_customer_id_fkey
    columns: [customer_id]
    referenced_schema: mydb
    referenced_table: customers
    referenced_columns: [id]
    on_delete: CASCADE
    on_update: NO ACTION

unique_constraints: []
indexes: []
check_constraints: []
```

## Notes

- In MySQL, "schema" and "database" are synonymous. Each schema in the list corresponds to one MySQL database.
- Check constraints require MySQL 8.0.16+. On older versions, `check_constraints` will be an empty list.
- The output YAML format is identical to the PostgreSQL `data-discovery` skill, making it compatible with the `data-discovery-to-dcat-neo4j`, `data-profiling`, and downstream skills.

## Error Handling

- Connection failure: show the error and ask the user to verify the connection string
- Table extraction failure: the script prints a warning and continues — don't abort the whole run
- Missing dependencies: scripts self-install via pip
