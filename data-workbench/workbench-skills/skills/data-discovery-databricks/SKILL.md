---
name: data-discovery-databricks
description: Extracts and documents Databricks database metadata for data discovery and cataloging. Use this skill whenever the user wants to explore, catalog, document, or extract schema information from a Databricks database — including tables, columns, data types, primary keys, foreign keys, unique constraints, and check constraints. Works with both Unity Catalog (3-level namespace) and legacy Hive Metastore. Trigger when the user mentions "data discovery", "database schema", "extract metadata", "catalog tables", "document my database", "what tables do I have", or wants to understand the structure of a Databricks database. Also trigger when the user provides a Databricks connection string and wants schema information.
---

# Data Discovery — Databricks

Connects to a Databricks SQL warehouse, lets the user browse and select schemas and tables, then extracts full metadata for each selected table into one YAML file per table in the current working directory.

Works with both **Unity Catalog** (3-level namespace: catalog.schema.table) and legacy **Hive Metastore** (2-level: schema.table).

All extraction logic is in bundled scripts — do not rewrite them. Just run them with the right arguments.

## Scripts

All scripts are in the `scripts/` directory relative to this SKILL.md file.

| Script | Purpose |
|--------|---------|
| `discover_schemas.py` | Lists all non-system schemas (databases) |
| `discover_tables.py` | Lists all tables in selected schemas |
| `extract_metadata.py` | Extracts full metadata and writes YAML files |

Each script self-installs its own dependencies (`databricks-sql-connector`, `pyyaml`) if missing.

## Connection string format

```
databricks://token:ACCESS_TOKEN@HOSTNAME?http_path=HTTP_PATH&catalog=CATALOG
```

- `ACCESS_TOKEN`: Databricks personal access token
- `HOSTNAME`: Databricks workspace hostname (e.g. `adb-1234567890123456.7.azuredatabricks.net`)
- `http_path`: SQL warehouse HTTP path (e.g. `/sql/1.0/warehouses/abcdef1234567890`)
- `catalog`: optional — Unity Catalog catalog name (omit for legacy Hive Metastore)
- `schema`: optional — default schema for the session

## Workflow

### Step 1: Get the connection string

Ask the user for their Databricks connection string:
```
databricks://token:ACCESS_TOKEN@HOSTNAME?http_path=HTTP_PATH&catalog=CATALOG
```
If they already provided it, use it directly.

### Step 2: Discover schemas

```bash
python ${CLAUDE_SKILL_DIR}/scripts/discover_schemas.py "<connection_string>"
```

Show the user the numbered list of schemas. System schemas (`information_schema`, `default` if empty) are automatically excluded.

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
    type: bigint
    character_maximum_length: null
    numeric_precision: null
    numeric_scale: null
    nullable: false
    default: null
    comment: null

primary_key:
  constraint_name: orders_pk
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

- **Unity Catalog**: If a `catalog` is specified in the connection string, constraint metadata (PRIMARY KEY, FOREIGN KEY, UNIQUE, CHECK) is extracted from `information_schema`. The scripts auto-detect Unity Catalog mode.
- **Hive Metastore**: Without a catalog, schemas are listed via `SHOW SCHEMAS`, tables via `SHOW TABLES IN schema`, and columns via `DESCRIBE TABLE`. Constraints will be empty lists (Hive Metastore does not enforce constraints).
- Column types follow Databricks/Spark SQL conventions: `bigint`, `string`, `double`, `decimal(p,s)`, `date`, `timestamp`, `array<...>`, `map<...>`, `struct<...>`.
- The output YAML format is identical to the PostgreSQL and MySQL `data-discovery` skills, making it compatible with the `data-discovery-to-dcat-neo4j`, `data-profiling-databricks`, and all downstream skills.

## Error Handling

- Connection failure: show the error and ask the user to verify the connection string and http_path
- Table extraction failure: the script prints a warning and continues — don't abort the whole run
- Missing dependencies: scripts self-install via pip
