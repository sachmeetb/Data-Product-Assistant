---
name: data-remediation
description: Analyzes data quality issues by comparing profiling measurements against domain rules, estimates score impact of candidate remediations, and generates SQL fix scripts for user-controlled execution. Use this skill when the user wants to fix data quality issues, remediate data problems, generate remediation scripts, or estimate the impact of data fixes on quality scores. Requires data-scoring and domain-rule-enhancement to have been run first.
---

## What this skill does

Identifies data quality violations by comparing profiling evidence against domain rules in the Neo4j knowledge graph, produces a structured analysis report with estimated score improvements, and lets the user choose which remediations to apply.

**IMPORTANT: This skill NEVER auto-executes remediation SQL. The user must explicitly choose what to apply.**

## Workflow — 3 Phases

### Phase 1: Analyze Issues

Run the analysis script to identify violations:

```bash
python {skill_dir}/scripts/analyze_issues.py \
  --host {neo4j_host} --bolt-port {neo4j_port} \
  --username {neo4j_user} --password {neo4j_password} \
  --database {neo4j_database} \
  --pg-connection "{pg_connection}"
```

This produces:
- `remediation/analysis_report.json` — structured report with all issues, candidate remediations, and per-issue score impact
- `remediation/analysis_report.md` — human-readable markdown version

### Phase 2: Estimate Score Impact

Run the score estimation script:

```bash
python {skill_dir}/scripts/estimate_scores.py \
  --host {neo4j_host} --bolt-port {neo4j_port} \
  --username {neo4j_user} --password {neo4j_password} \
  --database {neo4j_database} \
  --report remediation/analysis_report.json
```

This appends a `score_estimation` section to `analysis_report.json` and updates `analysis_report.md` with a score impact summary table.

### Phase 3: Present Results and Ask User

After phases 1-2 complete:

1. Read `remediation/analysis_report.md` and summarize the key findings in your response:
   - Number of issues found, grouped by type
   - Estimated overall score improvement (current -> estimated)
   - Top 3-5 most impactful remediations

2. Ask the user what they want to do using the agent messaging system:

```bash
python {script_path} --run-id {run_id} \
  --type multiple_choice \
  --prompt "How would you like to proceed with the {issue_count} identified remediations?" \
  --options "apply_all:Apply all recommended remediations" \
            "scripts_only:Generate SQL scripts only (review before executing)" \
            "skip:Skip remediation for now" \
  --timeout 600 \
  --default skip
```

**The timeout MUST be 600 seconds and the default MUST be "skip".** Never auto-execute remediations.

3. Based on user response:

   **If "apply_all":**
   ```bash
   python {skill_dir}/scripts/generate_remediation_sql.py --report remediation/analysis_report.json
   python {skill_dir}/scripts/apply_remediation.py \
     --pg-connection "{pg_connection}" \
     --host {neo4j_host} --bolt-port {neo4j_port} \
     --username {neo4j_user} --password {neo4j_password} \
     --database {neo4j_database} \
     --sql-dir remediation/sql
   ```

   **If "scripts_only":**
   ```bash
   python {skill_dir}/scripts/generate_remediation_sql.py --report remediation/analysis_report.json
   ```
   Then tell the user: "SQL scripts have been generated in the remediation/sql/ directory. You can review them in the Artifacts tab and execute them manually when ready."

   **If "skip":**
   Tell the user: "Remediation skipped. The analysis report is available in the Artifacts tab at remediation/analysis_report.md."

## Null Handling

Columns with high null rates are ALWAYS flagged as "Recommended: manual review" in the report. The skill NEVER generates SQL to fill in or remove null values. Null issues appear in the report but are excluded from auto-remediation options.

## Issue Types

| Issue Type | Description | Remediation Action |
|---|---|---|
| `out_of_range` | Numeric/date values outside domain rule bounds | Set to NULL or clamp to boundary |
| `future_dates` | Timestamp values in the future | Set to current timestamp or NULL |
| `invalid_values` | Values not in domain-approved allowed list | Set to NULL |
| `high_null_rate` | Column has >20% null rate on a mandatory column | Flag for manual review only |

## Graph Model (Provenance)

When remediations are applied, the skill records:

| Node | Description |
|---|---|
| `:RemediationAction` | One per applied fix: actionType, columnUri, description, rowsAffected, sql, appliedAt, batchId |

| Relationship | Meaning |
|---|---|
| `(:Column)-[:HAS_REMEDIATION]->(:RemediationAction)` | Links fix to column |
| `(:RemediationAction)-[:PROV_WAS_GENERATED_BY]->(:ProvActivity)` | Audit trail |
| `(:ProvActivity)-[:PROV_WAS_ASSOCIATED_WITH]->(:ProvAgent)` | Who/what applied it |

## Scripts

| Script | Purpose |
|---|---|
| `scripts/analyze_issues.py` | Query Neo4j for domain rule violations, produce JSON + markdown report |
| `scripts/estimate_scores.py` | Calculate current vs estimated scores, append to report |
| `scripts/generate_remediation_sql.py` | Generate per-table SQL fix scripts from report |
| `scripts/apply_remediation.py` | Execute SQL scripts against PostgreSQL, record provenance in Neo4j |

All scripts support `--help`.
