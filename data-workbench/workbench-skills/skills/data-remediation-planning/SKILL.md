---
name: data-remediation-planning
description: Executes data remediation actions based on the user's choice — apply all recommended remediations, generate SQL scripts only, or skip. Reads the analysis report produced by data-remediation-analysis and acts on it. Use this skill when the user wants to apply data fixes, generate remediation SQL, or complete the remediation planning step.
---

## What this skill does

Acts on the remediation analysis report produced by the data-remediation-analysis skill. The user has already chosen their action via the stage config form. This skill executes that choice.

## Workflow

### Step 1: Read the user's choice

The prompt includes `{remediation_action}` which is one of:
- `apply_all` — generate SQL scripts AND execute them against PostgreSQL
- `scripts_only` — generate SQL scripts only, do not execute
- `skip` — do nothing

### Step 2: Act on the choice

**If `apply_all`:**

```bash
python {skill_dir}/scripts/generate_remediation_sql.py \
  --report remediation/analysis_report.json

python {skill_dir}/scripts/apply_remediation.py \
  --pg-connection "{pg_connection}" \
  --sql-dir remediation/sql \
  --host {neo4j_host} --bolt-port {neo4j_port} \
  --username {neo4j_user} --password {neo4j_password} \
  --database {neo4j_database}
```

Report what was applied and how many rows were affected.

**If `scripts_only`:**

```bash
python {skill_dir}/scripts/generate_remediation_sql.py \
  --report remediation/analysis_report.json
```

Tell the user: "SQL scripts have been generated in the remediation/sql/ directory. You can review them in the Artifacts tab."

**If `skip`:**

Tell the user: "Remediation skipped. The analysis report is still available in the Artifacts tab at remediation/analysis_report.md."

## Scripts

| Script | Purpose |
|---|---|
| `scripts/generate_remediation_sql.py` | Generate per-table SQL fix scripts from analysis report |
| `scripts/apply_remediation.py` | Execute SQL scripts against PostgreSQL, record provenance in Neo4j |
