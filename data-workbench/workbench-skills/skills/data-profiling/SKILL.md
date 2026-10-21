---
name: data-profiling
description: Profiles the actual data in PostgreSQL tables and produces machine-readable YAML summaries of column-level statistics. Builds on the data-discovery skill — use this skill whenever the user wants to understand what's actually in their database (not just its structure), including null rates, value distributions, min/max ranges, top frequent values, string length stats, and numeric percentiles. Trigger when the user mentions "data profiling", "profile my tables", "what does the data look like", "column statistics", "data quality", "value distributions", "null analysis", or asks to analyze the content of database tables. Also trigger when the user has already run data discovery and now wants to go deeper into the data itself.
---

# Data Profiling — PostgreSQL

Reads the YAML metadata files produced by the data-discovery skill, runs profiling queries against the actual table data, and writes one profile YAML per table to the current working directory.

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

- `connection_string`: PostgreSQL connection string
- `output_dir`: where to write profile YAMLs (use current working directory)
- `yaml_file(s)`: one or more discovery YAML files (e.g. `employees__salary.yaml`)
- `--limit N`: max rows to sample per table (default: 100000; use 0 for full scan)
- `--top-n N`: how many top frequent values to report per column (default: 10)

The script self-installs its dependencies (`psycopg2-binary`, `pyyaml`).

## Workflow

### Step 1: Get the connection string

Ask the user for their PostgreSQL connection string if not already provided:
```
postgresql://user:password@host:port/database
```

### Step 2: Find discovery YAML files

List `*__*.yaml` files in the current directory, excluding any that already end in `__profile.yaml` (those are outputs of this skill, not inputs).

Show the user the available tables, numbered:
```
Available tables:
  1. employees.department        (employees__department.yaml)
  2. employees.department_employee  (employees__department_employee.yaml)
  3. employees.employee          (employees__employee.yaml)
  4. employees.salary            (employees__salary.yaml)
  ...
```

If no discovery YAML files are found, tell the user to run data discovery first.

### Step 3: Ask which tables to profile

"Which tables would you like to profile? List numbers, names, or say 'all'."

### Step 4: Ask about sampling (optional)

Only ask if the user seems to care about performance or mentions large tables. Otherwise use the defaults (100k row limit, top 10 values). You can mention: "I'll sample up to 100,000 rows per table by default — let me know if you want a full scan or a different limit."

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

Tell the user which profile YAML files were written. Give a brief summary of what was profiled (table names, row counts).

## Output YAML Format

Each output file is named `<schema>__<table>__profile.yaml` and has this structure:

```yaml
schema: employees
table: salary
profiled_at: "2026-03-05T10:00:00"
row_count: 2844047          # actual total rows in table
sample_size: 100000         # rows examined (may be less than row_count if sampled)
columns:
  - name: emp_id
    type: bigint
    null_count: 0
    null_rate: 0.0
    distinct_count: 87312
    min: 10001
    max: 499999
    mean: 253874.5
    stddev: 86400.2
    percentile_25: 149923
    percentile_50: 253874
    percentile_75: 373812

  - name: amount
    type: numeric
    null_count: 0
    null_rate: 0.0
    distinct_count: 52340
    min: 38623
    max: 158220
    mean: 63810.744
    stddev: 16904.1
    percentile_25: 50689
    percentile_50: 61758
    percentile_75: 74999

  - name: from_date
    type: date
    null_count: 0
    null_rate: 0.0
    distinct_count: 3868
    min: "1985-01-01"
    max: "2002-08-01"

  - name: dept_name
    type: character varying
    null_count: 0
    null_rate: 0.0
    distinct_count: 9
    min_length: 5
    max_length: 20
    avg_length: 11.23
    top_values:
      - value: "Development"
        count: 28381
        frequency: 0.28381
      - value: "Production"
        count: 24341
        frequency: 0.24341
```

## Metrics by Column Type

| Type | Metrics |
|------|---------|
| Numeric (int, bigint, numeric, float, etc.) | null_count, null_rate, distinct_count, min, max, mean, stddev, percentile_25, percentile_50, percentile_75 |
| Text (varchar, text, char, etc.) | null_count, null_rate, distinct_count, min_length, max_length, avg_length, top_values |
| Date / Timestamp | null_count, null_rate, distinct_count, min, max |
| Boolean | null_count, null_rate, distinct_count, top_values |
| Enum / USER-DEFINED | null_count, null_rate, distinct_count, top_values |
| Low-cardinality numeric (distinct_count <= 2×top_n) | All numeric metrics plus top_values |

## Error Handling

- Connection failure: show the error and ask the user to verify the connection string
- Per-column failure: log a warning and continue — don't abort the whole table
- Per-table failure: log a warning and continue — don't abort other tables
- Missing discovery YAML: report which file was not found and skip it
