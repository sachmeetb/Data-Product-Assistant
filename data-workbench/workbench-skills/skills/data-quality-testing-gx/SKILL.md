---
name: data-quality-testing-gx
description: Generates Great Expectations (GX Core) data quality validation code from DQ rules stored in the Neo4j knowledge graph, then runs the validations against a relational database (PostgreSQL, MySQL, Snowflake, or Databricks). Use this skill whenever the user wants to use Great Expectations for data quality testing, generate GX expectation suites from graph rules, validate data with GX Core, or produce GX-based validation scripts. Also trigger when the user says "generate GX tests", "create Great Expectations validations", "use GX Core for DQ", "generate expectation suites", or "run GX validations from rules". Requires data-discovery-to-dcat-neo4j, data-profiling-to-dqv-neo4j, and data-quality-rule-generation to have been run first.
---

## What this skill does

Reads `:PropertyShape` DQ rule nodes from the Neo4j knowledge graph and generates a **Great Expectations (GX Core)** validation script. Uses an ephemeral GX Data Context (no filesystem configuration needed) with Pandas DataFrames loaded from the target database via SQLAlchemy.

## Platform Support

The generated `run_gx_validations.py` script detects the database platform from the `--conn-string` prefix and selects the appropriate SQLAlchemy driver and SQL identifier quoting automatically.

| Platform | Connection string prefix | Required driver |
|---|---|---|
| PostgreSQL | `postgresql://user:pass@host:port/db` | `psycopg2-binary` (default) |
| MySQL | `mysql://user:pass@host:port/db` | `pymysql>=1.0` |
| Snowflake | `snowflake://user:pass@account/db?warehouse=X` | `snowflake-sqlalchemy>=1.5` |
| Databricks | `databricks://token:TOKEN@HOST?http_path=PATH` | `databricks-sql-connector>=3.0.0` |

Install only the driver for your platform — all four are listed in the generated `requirements.txt` for reference. SQL identifier quoting uses double-quotes for PostgreSQL/Snowflake and backticks for MySQL/Databricks.

Output is written to `./dq_tests_gx/` (separate subfolder, safe to re-generate).

## Rule → GX Expectation mapping

| Rule type | GX Expectation |
|---|---|
| `mandatory` | `ExpectColumnValuesToNotBeNull` |
| `range` | `ExpectColumnValuesToBeBetween` |
| `unique` | `ExpectColumnProportionOfUniqueValuesToBeBetween` |
| `allowedValues` | `ExpectColumnValuesToBeInSet` |
| `referentialIntegrity` | Noted as comment — no native GX equivalent |

## Generated output structure

```
dq_tests_gx/
├── requirements.txt           — great-expectations, pandas, sqlalchemy + platform drivers
├── run_gx_validations.py      — self-contained GX validation script
└── results/                   — JSON result logs (created at runtime)
```

`run_gx_validations.py` contains one `setup_suite_<schema>__<table>(context)` function per table, then a `main()` that:
1. Creates an ephemeral GX Data Context
2. Registers all expectation suites
3. Loads each table as a Pandas DataFrame
4. Validates each batch against its suite
5. Saves a timestamped JSON result log

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/generate_gx_tests.py` | Queries Neo4j, writes GX validation script to output directory |
| `scripts/load_test_results_to_graph.py` | Reads the JSON output from `run_gx_validations.py` and writes `:TestRun` + `:TestResult` summary nodes to Neo4j so the data-scoring skill can consume pass/fail evidence |

Both scripts support `--help`.

**Dependencies:** `pip install neo4j` for the generator; generated code requires `pip install -r dq_tests_gx/requirements.txt`.

## Workflow — Generate (Workbench-driven)

This is the track the Workbench "Generate DQ Tests (GX)" stage invokes.
It is the ONLY track an agent should follow from inside Workbench.

### Step 1 — Confirm prerequisites

The graph must already contain:
- `:Dataset`, `:Column` nodes (from `data-discovery-to-dcat-neo4j`)
- `:QualityMeasurement`, `:TopValue` nodes (from `data-profiling-to-dqv-neo4j`)
- `:NodeShape`, `:PropertyShape` nodes (from `data-quality-rule-generation`)

### Step 2 — Generate the GX script

```bash
python ${CLAUDE_SKILL_DIR}/scripts/generate_gx_tests.py [output_dir] [options]
```

- `output_dir` — where to write generated files (default: `./dq_tests_gx`)
- Connection flags: `--host`, `--bolt-port`, `--username`, `--password`, `--database`

### Step 3 — Report generated files and stop

Report to the user:
- Full path of output directory
- Number of tables and expectations generated per table
- Files generated (`run_gx_validations.py`, `requirements.txt`)

### ⛔ STOP HERE when called from Workbench

**Do NOT** run `pip install`. Do NOT run `run_gx_validations.py`. Do NOT
invoke `load_test_results_to_graph.py`. Do NOT call `agent_ask.py` to ask
for a Postgres connection string. Those steps are handled by a separate,
non-LLM **"Run DQ Tests"** stage in Workbench that directly subprocess-
executes the generated script. Exit cleanly after reporting.

---

## Workflow — Execute (manual, outside Workbench)

> **Not used by Workbench.** Workbench's "Run DQ Tests" stage calls a
> backend subprocess that performs steps 4–6 directly, bypassing the
> skill. The following steps are reference material for users running
> the skill outside Workbench.

### Step 4 — Install dependencies

```bash
pip install -r dq_tests_gx/requirements.txt
```

### Step 5 — Run the validations

```bash
python dq_tests_gx/run_gx_validations.py \
  --conn-string <connection-string> \
  [--sample N] \
  [--output results]
```

Examples:
- PostgreSQL: `--conn-string postgresql://user:pass@host/db`
- MySQL: `--conn-string mysql://user:pass@host/db`
- Snowflake: `--conn-string snowflake://user:pass@account/db?warehouse=WH`
- Databricks: `--conn-string databricks://token:TOKEN@HOST?http_path=PATH`

- `--sample N` limits rows loaded per table
- `--output DIR` sets results directory (default: `results/` inside output dir)

The script:
- Creates a GX ephemeral Data Context
- Builds Pandas datasource + per-table DataframeAssets
- Validates each table's batch against its expectation suite
- Prints per-table pass/fail summary with GX result details
- Saves a timestamped JSON result log
- **Exits non-zero** if any expectation fails

### Step 6 — Load results into the knowledge graph

```bash
python ${CLAUDE_SKILL_DIR}/scripts/load_test_results_to_graph.py \
  --results-dir dq_tests_gx/results \
  --project-code <PROJECT_CODE> \
  --host <HOST> --bolt-port <PORT> \
  --username <USER> --password <PASS> --database <DB>
```

This reads the most-recent results JSON and creates:

- A single `:TestRun` node (summary of this execution, keyed by `batchId`)
- One `:TestResult` node per expectation, linked to its originating `:PropertyShape` (via `VALIDATES_RULE`) and the `:Column` it ran against (via `ON_COLUMN`)
- `(:Project)-[:HAS_TEST_RUN]->(:TestRun)-[:VALIDATED]->(:Dataset)`

These nodes are what the **data-scoring** skill reads to compute test-driven Validity scores. Without this step, scoring falls back to profiling-only evidence.

Always pass `--project-code` when the project uses project-scoped graph isolation.

### Step 7 — Report results

After running, report to the user:
- Path to the JSON result file
- Per-table summary: suite name, expectations run, passed/failed
- Any failed expectations with column, expectation type, and observed values
- Overall pass/fail status
- Confirmation that `:TestRun` / `:TestResult` nodes were loaded

## Notes

- Re-generating is always safe — the output file is overwritten.
- Uses **ephemeral mode** — no GX project directory or config files required.
- Referential integrity rules are not representable as standard GX expectations; they appear as comments in the generated code and are skipped at runtime.
- GX does not have a built-in uniqueness-ratio expectation in all versions; `ExpectColumnProportionOfUniqueValuesToBeBetween` is used where available.
- The generated script targets **GX Core v1.x** (`great-expectations>=1.0`). Pin `great-expectations==1.*` in requirements.txt to avoid breaking API changes.
- Results JSON mirrors the GX validation result structure, augmented with metadata from the graph (rule type, severity, confidence).
