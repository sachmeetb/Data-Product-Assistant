---
name: data-quality-failure-analysis
description: Summarises failing Great Expectations (GX Core) data quality tests by reading the run's result JSON, pulling column/rule context from Neo4j, and producing a human-readable markdown report grouped by table and column. Surfaces the top-N unexpected values per failed expectation (captured at test execution time) so reviewers can see WHAT failed, not just HOW MANY rows. Use this skill when the user has already run DQ tests and wants a narrative explanation of the failures, or when triggered by the "DQ Failure Analysis" stage.
---

## What this skill does

Turns "column X had 47 unexpected rows" into "column `country` had 47 unexpected rows — top offenders: `'US '` (trailing space, 22×), `'USA'` (15×), `'united states'` (10×)". PII-tagged columns are redacted.

Reads result files produced by the `data-quality-testing-gx` skill and the rule/column metadata stored by the ODCS + DCAT subgraphs. Does NOT re-execute tests and does NOT touch the source database.

## Prerequisites

- `data-quality-testing-gx` has run at least once for this project (so `dq_tests_gx/results/*.json` exist).
- The result files contain a `samples` array per failed expectation (captured by the updated GX runner).
- Neo4j is reachable and the project's graph has been populated by data discovery (so `:Column` nodes with `pii` / descriptions exist).
- `neo4j` and `pyyaml` packages installed.

## Workflow

### Step 1 — Run the analyser

```bash
python {skill_dir}/scripts/analyze_failures.py \
  --project-dir {project_dir} \
  --host {neo4j_host} --bolt-port {neo4j_port} \
  --username {neo4j_user} --password {neo4j_password} \
  --database {neo4j_database} \
  --project-code {project_code}
```

Output:
- `{project_dir}/dq_tests_gx/failure_analysis.md` — human-readable report.
- `{project_dir}/dq_tests_gx/failure_analysis.json` — the same data in structured form (optional downstream use).

### Step 2 — Return the report inline

Read the full contents of `failure_analysis.md` and include them in your response so the user sees the report in the Output tab (the Results tab also renders the markdown).

**STOP HERE.** Do not generate SQL remediations or modify the graph. That is handled by separate remediation stages.

## Report shape

Grouped by table → expectation, with for each failed expectation:

- Rule name / expectation type and column
- Evaluated / unsuccessful counts and pass rate
- **Top-N unexpected values with occurrence counts** (redacted for `pii: true` columns)
- Column description (if one has been approved)
- Business rule context pulled from the `:DataContractQuality` or `:PropertyShape` node if available

If no samples are available for an expectation (e.g. table-level row-count checks) the report shows the observed scalar (`observed_value`) instead.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/analyze_failures.py` | Reads result JSON + Neo4j, writes `failure_analysis.{md,json}` |

## Notes on PII

`:Column` and `:DataContractProperty` both carry a `pii` boolean. When `pii=true`, the sample values are replaced with `<redacted — column marked PII>` and only the counts are shown. The raw result files on disk still carry the samples; only the report is sanitised.
