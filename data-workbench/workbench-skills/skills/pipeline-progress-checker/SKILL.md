---
name: pipeline-progress-checker
description: Checks the progress of a data engineering pipeline by querying Neo4j for completion statistics — description approval rates, mapping approval rates, DQ rule counts, and enrichment coverage. Use this skill when the user or agent needs to verify pipeline state, check if all tables are profiled, see approval percentages, or decide if the pipeline is ready to proceed to the next stage.
---

# Pipeline Progress Checker

Queries a Neo4j knowledge graph to report pipeline completion statistics: dataset counts, column counts, description and mapping approval rates, DQ rule counts, allowed value coverage, and playbook item counts. Computes a `ready_for_next_stage` indicator based on configurable approval thresholds.

## When to Use

- Before running a downstream stage (e.g., verify descriptions are approved before mapping)
- To report pipeline status to the user mid-workflow
- To decide whether to proceed or wait for more reviews

## Prerequisites

- Neo4j knowledge graph populated by earlier pipeline stages (discovery, profiling, enrichment)
- `neo4j` Python driver installed (`pip install neo4j`)

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/check_progress.py` | Query Neo4j for all pipeline stats, compute approval rates, output JSON summary |

All scripts support `--help`.

## Workflow

### Step 1 — Get connection details

If not already provided, the following defaults apply:

| Setting | Default |
|---------|---------|
| Host | `localhost` |
| Bolt port | `7687` |
| Username | `neo4j` |
| Password | `your_password` |
| Database | `neo4j` |

### Step 2 — Run progress check

```bash
python ${CLAUDE_SKILL_DIR}/scripts/check_progress.py \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db> \
  --domain "<domain>" \
  --threshold 0.95 \
  --output progress_report.json
```

### Step 3 — Interpret results

The output JSON has this structure:

```json
{
  "datasets": 12,
  "columns": 87,
  "graph_nodes": 1520,
  "graph_relationships": 3200,
  "descriptions": {
    "total": 87,
    "approved": 80,
    "rejected": 5,
    "pending": 2,
    "approval_pct": 0.92
  },
  "mappings": {
    "total": 45,
    "approved": 40,
    "rejected": 3,
    "pending": 2,
    "approval_pct": 0.89
  },
  "final_descriptions": 87,
  "dq_rules": 34,
  "allowed_values": 8,
  "playbook_items": 12,
  "learning_cycles": 2,
  "threshold": 0.95,
  "ready_for_next_stage": false,
  "blockers": ["descriptions approval at 92% (threshold: 95%)", "mappings approval at 89% (threshold: 95%)"]
}
```

Key fields:
- `ready_for_next_stage`: `true` when both description and mapping approval rates exceed the threshold (or when there are no pending items)
- `blockers`: human-readable list of what's preventing readiness
- `approval_pct`: 0.0–1.0 ratio of approved / total

### Step 4 — Report to user

Summarize the progress in plain language. If `ready_for_next_stage` is false, explain the blockers and suggest next actions (e.g., "2 descriptions are still pending review").
