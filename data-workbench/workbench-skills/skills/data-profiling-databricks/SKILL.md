---
name: data-profiling-databricks
description: Profiles the actual data in Databricks tables and produces machine-readable YAML summaries of column-level statistics. Builds on the data-discovery-databricks skill — use this skill whenever the user wants to understand what's actually in their Databricks database (not just its structure), including null rates, value distributions, min/max ranges, top frequent values, string length stats, and numeric percentiles. Trigger when the user mentions "data profiling", "profile my tables", "what does the data look like", "column statistics", "data quality", "value distributions", "null analysis", or asks to analyze the content of Databricks database tables. Also trigger when the user has already run Databricks data discovery and now wants to go deeper into the data itself.
---

# Data Profiling — Databricks

Reads the YAML metadata files produced by the data-discovery-databricks skill, runs profiling queries against the actual table data, and writes one profile YAML per table to the current working directory.

All profiling logic is in the bundled script — do not rewrite it. Just run it with the right arguments.

## Script

`scripts/profile_table.py`

```
python ${CLAUDE_SKILL_DIR}/scripts/profile_table.py \
  "<connection_string>" \
  "<output_dir>" \
  <yaml_file1> [yaml_file2 ...] \
  [--limit N] \
  [--top-n N]
```

- `connection_string`: Databricks connection string — `databricks://token:ACCESS_TOKEN@HOSTNAME?http_path=HTTP_PATH&catalog=CATALOG`
- `output_dir`: where to write profile YAMLs (use current working directory)
- `yaml_file(s)`: one or more discovery YAML files (e.g. `sales__orders.yaml`)
- `--limit N`: max rows to sample per table (default: 100000; use 0 for full scan)
- `--top-n N`: how many top frequent values to report per column (default: 10)

The script self-installs its dependencies (`databricks-sql-connector`, `pyyaml`).

## Workflow

### Step 1: Get the connection string

Use the Databricks connection string already provided:
```
databricks://token:ACCESS_TOKEN@HOSTNAME?http_path=HTTP_PATH&catalog=CATALOG
```

### Step 2: Find discovery YAML files

List `*__*.yaml` files in the current directory, excluding any that already end in `__profile.yaml` (those are outputs of this skill, not inputs).

Show the user the available tables, numbered:
```
Available tables:
  1. sales.orders        (sales__orders.yaml)
  2. sales.customers     (sales__customers.yaml)
  ...
```

If no discovery YAML files are found, tell the user to run data-discovery-databricks first.

### Step 3: Ask which tables to profile

"Which tables would you like to profile? List numbers, names, or say 'all'."

### Step 4: Ask about sampling (optional)

Only ask if the user seems to care about performance or mentions large tables. Otherwise use the defaults (100k row limit, top 10 values).

### Step 5: Run the profiling script

```bash
python ${CLAUDE_SKILL_DIR}/scripts/profile_table.py \
  "<connection_string>" \
  "<output_dir>" \
  <yaml_file1> [yaml_file2 ...] \
  --limit 100000 \
  --top-n 10
```

### Step 6: Report results

Tell the user which profile YAML files were written. Give a brief summary (table names, row counts).

## Output YAML Format

Each output file is named `<schema>__<table>__profile.yaml` and has the same structure as the PostgreSQL and MySQL profilers — **fully compatible** with `data-profiling-to-dqv-neo4j` and all downstream skills.

```yaml
schema: sales
table: orders
profiled_at: "2026-03-05T10:00:00"
row_count: 284404
sample_size: 100000
columns:
  - name: order_id
    type: bigint
    null_count: 0
    null_rate: 0.0
    distinct_count: 284404
    min: 1
    max: 284404
    mean: 142202.5
    stddev: 82074.3
    percentile_25: 71101
    percentile_50: 142202
    percentile_75: 213303

  - name: status
    type: string
    null_count: 0
    null_rate: 0.0
    distinct_count: 4
    min_length: 4
    max_length: 9
    avg_length: 6.12
    top_values:
      - value: "shipped"
        count: 98201
        frequency: 0.98201
```

## Metrics by Column Type

| Type | Metrics |
|------|---------|
| Numeric (bigint, int, double, float, decimal, long) | null_count, null_rate, distinct_count, min, max, mean, stddev, percentile_25, percentile_50, percentile_75 |
| Text (string, varchar, char) | null_count, null_rate, distinct_count, min_length, max_length, avg_length, top_values |
| Date / Timestamp | null_count, null_rate, distinct_count, min, max |
| Boolean | null_count, null_rate, distinct_count, top_values |
| Complex (array, map, struct) | null_count, null_rate, distinct_count only |

**Note:** Percentiles use `PERCENTILE_APPROX(col, p)` — approximate but efficient on large Delta tables. Sampling uses `WHERE RAND() < fraction` (Bernoulli sampling per row).

## Error Handling

- Connection failure: show the error and ask the user to verify the connection string and http_path
- Per-column failure: log a warning and continue — don't abort the whole table
- Per-table failure: log a warning and continue — don't abort other tables
- Missing discovery YAML: report which file was not found and skip it
