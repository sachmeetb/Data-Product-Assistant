---
name: data-remediation-analysis
description: Analyzes data quality issues by comparing profiling measurements against domain rules and estimates the score impact of candidate remediations. Produces a structured report (JSON + markdown) but does NOT generate SQL scripts or execute any remediations. Use this skill when the user wants to analyze data quality issues, assess remediation candidates, or estimate score improvement potential.
---

## What this skill does

Identifies data quality violations by comparing profiling evidence against domain rules in the Neo4j knowledge graph, then estimates what the quality scores would be if those issues were fixed. Produces a report — no SQL is generated or executed.

## Workflow

### Step 1: Run issue analysis

```bash
python {skill_dir}/scripts/analyze_issues.py \
  --host {neo4j_host} --bolt-port {neo4j_port} \
  --username {neo4j_user} --password {neo4j_password} \
  --database {neo4j_database} \
  --project-code {project_code} \
  --pg-connection "{pg_connection}"
```

This produces:
- `remediation/analysis_report.json` — structured report
- `remediation/analysis_report.md` — human-readable markdown

### Step 2: Estimate score impact

```bash
python {skill_dir}/scripts/estimate_scores.py \
  --host {neo4j_host} --bolt-port {neo4j_port} \
  --username {neo4j_user} --password {neo4j_password} \
  --database {neo4j_database} \
  --report remediation/analysis_report.json
```

This appends score estimation to both the JSON and markdown reports.

### Step 3: Summarize in output

Read `remediation/analysis_report.md` and present the key findings in your response:
- Number of issues found by type
- Estimated overall score improvement (current -> estimated)
- Top issues by impact

**STOP HERE.** Do NOT proceed to generate SQL scripts or ask the user about applying remediations. That is handled by a separate stage (Remediation Planning).

## Scripts

| Script | Purpose |
|---|---|
| `scripts/analyze_issues.py` | Query Neo4j for domain rule violations, produce JSON + markdown report |
| `scripts/estimate_scores.py` | Calculate current vs estimated scores, append to report |
