---
name: data-quality-testing-python
description: Generates Python/Pandera data quality test code from DQ rules stored in the Neo4j knowledge graph, then runs the tests against a relational database (PostgreSQL, MySQL, Snowflake, or Databricks). Use this skill whenever the user wants to generate DQ test code, run data quality checks in pure Python, validate data using Pandera schemas, or produce executable validation scripts from graph-derived rules. Also trigger when the user says "generate Python DQ tests", "create Pandera validators", "test data quality with Python", "run validation checks", or "generate test code from rules". Requires data-discovery-to-dcat-neo4j, data-profiling-to-dqv-neo4j, and data-quality-rule-generation to have been run first.
---

## What this skill does

Reads `:PropertyShape` DQ rule nodes from the Neo4j knowledge graph and generates a runnable Python test suite using **Pandera** for schema/column validation and custom functions for referential integrity checks.

Output is written to `./dq_tests_python/` (separate subfolder, safe to re-generate).

## Platform Support

The generated `run_all.py` and per-table validator modules detect the database platform from the `--conn-string` prefix and select the appropriate SQLAlchemy driver and SQL identifier quoting automatically.

| Platform | Connection string prefix | Required driver |
|---|---|---|
| PostgreSQL | `postgresql://user:pass@host:port/db` | `psycopg2-binary` (default) |
| MySQL | `mysql://user:pass@host:port/db` | `pymysql>=1.0` |
| Snowflake | `snowflake://user:pass@account/db?warehouse=X` | `snowflake-sqlalchemy>=1.5` |
| Databricks | `databricks://token:TOKEN@HOST?http_path=PATH` | `databricks-sql-connector>=3.0.0` |

Install only the driver for your platform — all four are listed in the generated `requirements.txt` for reference. SQL identifier quoting uses double-quotes for PostgreSQL/Snowflake and backticks for MySQL/Databricks. Referential integrity checks also apply platform-correct quoting at runtime through the engine URL.

## Rule → Test mapping

| Rule type | Pandera / Python construct |
|---|---|
| `mandatory` | `Column(nullable=False)` |
| `range` | `Check.in_range(min, max)` |
| `unique` | `Check(lambda s: s.nunique() / len(s) >= threshold)` |
| `allowedValues` | `Check.isin(value_set)` |
| `referentialIntegrity` | Custom SQL check in `check_referential_integrity()` |

## Generated output structure

```
dq_tests_python/
├── requirements.txt          — pandas, pandera, sqlalchemy + platform drivers
├── run_all.py                — master runner (loads data, runs all validators)
├── validators/
│   ├── __init__.py
│   ├── <schema>_<table>.py  — one Pandera schema module per table
│   └── ...
└── results/                  — JSON result logs (created at runtime)
```

Each `validators/<schema>_<table>.py` contains:
- `build_schema()` → `DataFrameSchema` derived from graph rules
- `check_referential_integrity(df, engine)` → FK checks (if table has RI rules)
- `validate(df, engine)` → runs all checks, returns list of result dicts

Result dicts include: `table`, `column`, `rule_type`, `severity`, `status` (PASS/FAIL/ERROR), `detail`.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/generate_python_tests.py` | Queries Neo4j, writes test files to output directory |

Supports `--help`.

**Dependencies:** `pip install neo4j` for the generator; generated code requires `pip install -r dq_tests_python/requirements.txt`.

## Workflow — Generate (Workbench-driven)

This is the track the Workbench "Generate DQ Tests (Python)" stage invokes.
It is the ONLY track an agent should follow from inside Workbench.

### Step 1 — Confirm prerequisites

The graph must already contain:
- `:Dataset`, `:Column` nodes (from `data-discovery-to-dcat-neo4j`)
- `:QualityMeasurement`, `:TopValue` nodes (from `data-profiling-to-dqv-neo4j`)
- `:NodeShape`, `:PropertyShape` nodes (from `data-quality-rule-generation`)

### Step 2 — Generate the test suite

```bash
python ${CLAUDE_SKILL_DIR}/scripts/generate_python_tests.py [output_dir] [options]
```

- `output_dir` — where to write generated files (default: `./dq_tests_python`)
- Connection flags: `--host`, `--bolt-port`, `--username`, `--password`, `--database`

### Step 3 — Report generated files and stop

Report to the user:
- Full path of output directory
- Number of tables and total rules processed
- Files generated (`run_all.py`, per-table validators, `requirements.txt`)

### ⛔ STOP HERE when called from Workbench

**Do NOT** run `pip install`. Do NOT run `run_all.py`. Do NOT call
`agent_ask.py` to ask for a Postgres connection string. Those steps are
handled by a separate, non-LLM **"Run DQ Tests"** stage in Workbench that
directly subprocess-executes the generated script. Exit cleanly after
reporting.

---

## Workflow — Execute (manual, outside Workbench)

> **Not used by Workbench.** Workbench's "Run DQ Tests" stage calls a
> backend subprocess that performs steps 4–5 directly, bypassing the
> skill. The following steps are reference material for users running
> the skill outside Workbench.

### Step 4 — Install dependencies

```bash
pip install -r dq_tests_python/requirements.txt
```

### Step 5 — Run the tests

```bash
python dq_tests_python/run_all.py \
  --conn-string <connection-string> \
  [--sample N] \
  [--output results]
```

Examples:
- PostgreSQL: `--conn-string postgresql://user:pass@host/db`
- MySQL: `--conn-string mysql://user:pass@host/db`
- Snowflake: `--conn-string snowflake://user:pass@account/db?warehouse=WH`
- Databricks: `--conn-string databricks://token:TOKEN@HOST?http_path=PATH`

- `--sample N` limits rows loaded per table (useful for large tables)
- `--output DIR` sets results directory (default: `results/` inside output dir)

The runner:
- Loads each table into a DataFrame
- Runs Pandera schema validation (lazy mode — collects all failures)
- Runs RI checks (if applicable)
- Prints per-table pass/fail counts
- Saves a timestamped JSON result log
- **Exits non-zero** if any `sh:Violation` severity check fails

### Step 6 — Report results

After running, report to the user:
- Path to the JSON result file
- Summary: tables validated, total checks, pass/fail counts by severity
- List any FAIL results with table, column, rule type, and detail
- Exit code (0 = passed, 1 = violations found)

## Notes

- Re-generating is always safe — output files are overwritten, not appended.
- The `validators/` modules import only `pandas` and `pandera` — no Neo4j or DB connection needed at test time.
- Referential integrity checks require a live DB connection (passed as `engine` to `validate()`).
- For large tables, use `--sample` to limit memory usage.
- Results JSON can be used for trend analysis by comparing across runs (keyed on `run_at` timestamp).
