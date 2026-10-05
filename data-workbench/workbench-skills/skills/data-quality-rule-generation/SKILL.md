---
name: data-quality-rule-generation
description: Queries an enriched Neo4j knowledge graph (DCAT-2 + DQV + TopValue) and generates SHACL-inspired data quality rules as a Cypher script, then loads them into the graph. Use this skill whenever the user wants to generate data quality rules, create validation constraints, produce data quality checks, or build a rule set from profiling evidence. Also trigger when the user says "generate rules", "create DQ rules", "build quality constraints", "what rules can we derive", or "generate validation rules from the graph". Requires data-discovery-to-dcat-neo4j and data-profiling-to-dqv-neo4j to have been run first.
---

## What this skill does

Queries the knowledge graph built by the discovery and profiling skills, derives data quality rules from the evidence already in the graph, and writes those rules back as SHACL-inspired `:NodeShape` and `:PropertyShape` nodes.

The rule vocabulary is SHACL-inspired (using `sh:` prefixes for severity levels and rule type names) but implemented as native Neo4j nodes — not formal RDF/SHACL.

## Rule Types

| Rule type | Evidence source | Default severity |
|---|---|---|
| `mandatory` | `null_rate = 0` + `nullable = false` | `sh:Violation` |
| `range` | `min` / `max` QualityMeasurements (numeric & date columns) | `sh:Warning` |
| `unique` | `distinct_count / row_count ≥ 0.99` | `sh:Violation` (PK), `sh:Warning` (non-PK) |
| `allowedValues` | TopValue coverage ≥ 95% | `sh:Violation` |
| `referentialIntegrity` | `[:REFERENCES]` FK relationship | `sh:Violation` |

## Graph Model

### Nodes added

| Node | Description |
|------|-------------|
| `:NodeShape` | One per table — container for all rules on that table |
| `:PropertyShape` | One per rule — holds rule type, severity, thresholds, and evidence values |

### Relationships added

| Relationship | Meaning |
|---|---|
| `(:Dataset)-[:HAS_SHAPE]->(:NodeShape)` | Table has a rule set |
| `(:NodeShape)-[:PROPERTY]->(:PropertyShape)` | Rule set contains a rule |
| `(:PropertyShape)-[:ON_COLUMN]->(:Column)` | Rule targets a specific column |
| `(:PropertyShape)-[:REFERENCES_DATASET]->(:Dataset)` | Referential integrity rule points to target table |
| `(:PropertyShape)-[:ALLOWED_VALUE]->(:TopValue)` | Allowed-values rule links to permitted value nodes |

### PropertyShape properties

Every `:PropertyShape` carries:
- `uri` — unique identifier (e.g. `rule:employees.salary.amount.range`)
- `ruleType` — one of the rule types above
- `path` — column name the rule targets
- `severity` — `sh:Violation` or `sh:Warning`
- `confidence` — float 0–1 reflecting strength of evidence
- `description` — human-readable rule description

Plus rule-type-specific evidence properties (e.g. `minInclusive`, `maxInclusive`, `uniquenessRatio`, `coverage`).

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/generate_rules_cypher.py` | Queries the graph, generates `dq_rules.cypher` |
| `scripts/run_rules_cypher.py` | Loads the script into Neo4j with pre-flight checks |

Both scripts support `--help`.

**Dependency:** requires the `neo4j` Python driver — install with `pip install neo4j`.

## Workflow

### Step 1 — Confirm prerequisites

The graph must already contain:
- `:Dataset` and `:Column` nodes (from `data-discovery-to-dcat-neo4j`)
- `:QualityMeasurement` and `:TopValue` nodes (from `data-profiling-to-dqv-neo4j`)

Check (use the project code to scope): `MATCH (:Project {projectCode: '<project_code>'})-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset) WHERE ds.row_count IS NOT NULL RETURN count(ds)` — should be > 0.

### Step 2 — Generate the Cypher script

```bash
python ${CLAUDE_SKILL_DIR}/scripts/generate_rules_cypher.py [output_file] --project-code <project_code> [options]
```

- `output_file` — output path (default: `./dq_rules.cypher`)
- `--project-code` — **REQUIRED when provided in the prompt.** Scopes dataset queries through `:Project` node and prefixes rule/shape URIs.
- Connection flags: `--host`, `--bolt-port`, `--username`, `--password`, `--database`

The script connects to Neo4j, queries enriched datasets scoped to the project, derives rules, and writes the Cypher file.

Report:
- Full path of the generated file
- Dataset count, total rule count, breakdown by rule type
- Show the first ~25 lines so the user can verify structure

### Step 3 — Load into Neo4j

```bash
python ${CLAUDE_SKILL_DIR}/scripts/run_rules_cypher.py [cypher_file] [options]
```

Default connection settings match the other skills in this suite (`localhost:7687`, `neo4j`/`your_password`).

Use `--dry-run` to preview without executing.

**Pre-flight checks (per table at load time):**
1. The `:Dataset` node must exist.
2. The `:Dataset` must not already have a `[:HAS_SHAPE]->(:NodeShape)` — if it does, the table is skipped (already has rules).

## Default Thresholds

| Threshold | Default | Stored on |
|---|---|---|
| Uniqueness ratio | 0.99 | `PropertyShape.uniquenessThreshold` |
| Allowed values coverage | 0.95 | `PropertyShape.coverageThreshold` |
| Mandatory null rate | 0.0 (exact) | `PropertyShape.evidenceNullRate` |

Thresholds are stored as properties on the generated `:PropertyShape` nodes, making them queryable and auditable directly from the graph.

## Notes

- Re-running is blocked by the pre-flight check. To regenerate rules cleanly:
  ```cypher
  MATCH (n:PropertyShape) DETACH DELETE n;
  MATCH (n:NodeShape) DETACH DELETE n;
  ```
- Rules reflect the *sampled* profiling data — for large tables sampled at 100k rows, range bounds and uniqueness ratios are estimates.
- `allowedValues` rules link directly to the existing `:TopValue` nodes in the graph via `[:ALLOWED_VALUE]` — no value data is duplicated.
