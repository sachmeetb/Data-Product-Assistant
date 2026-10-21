---
name: data-scoring
description: Computes data quality scores from an enriched Neo4j knowledge graph (DCAT-2 + DQV + SHACL) and persists them as :QualityScore nodes. Use this skill whenever the user wants to score data quality, compute quality metrics, assess data health, generate a quality baseline, or track quality over time. Also trigger when the user says "score data", "data quality score", "quality baseline", "assess data health", or "compute quality metrics". Requires data-discovery-to-dcat-neo4j, data-profiling-to-dqv-neo4j, and data-quality-rule-generation to have been run first.
---

## What this skill does

Queries the knowledge graph built by the discovery, profiling, rule generation, and (optionally) DQ testing skills, computes data quality scores across **seven dimensions**, and writes those scores back as `:QualityScore` nodes linked to columns, datasets, and catalogs.

Each scoring run creates a timestamped batch, so previous scores are retained for trend tracking.

## Tiered Evidence Model

Scores are organized into three tiers, each adding a new kind of evidence:

1. **Tier 1 — Internal consistency (baseline).** Validity, Consistency, and Rule Coverage draw from profiling + rule presence. If `:TestResult` nodes exist (from the `data-quality-testing-gx` loader), Validity and Consistency consume their actual pass rates.
2. **Tier 2 — Shared meaning.** The **Documentation** dimension rewards columns with human-approved descriptions.
3. **Tier 3 — External grounding.** The **Grounding** dimension rewards columns whose rules are anchored to external/domain authoritative sources and human-approved (`ruleSource IN ('domain','external')` AND `status='approved'`).

Composite across tiers is not apples-to-apples: moving up a tier can push the composite down because sharper evidence reveals issues that were invisible before. Read the dimension *shape*, not the composite number.

## Scoring Dimensions

| Dimension | Weight | Evidence Source | Column Score |
|-----------|--------|----------------|--------------|
| Completeness | 0.20 | DQV `null_rate` | `1.0 - null_rate` (0.0 if not profiled) |
| Uniqueness | 0.16 | DQV `distinct_count` + `row_count` | ratio for PK/unique columns; 1.0 for low-cardinality; 1.0 for non-unique |
| Validity | 0.16 | `:TestResult` pass rate (test evidence) → `allowedValues.coverage` → range rule | latest test pass rate if present, else profiling coverage, else 1.0 |
| Consistency | 0.10 | `:TestResult` pass rate for RI → `referentialIntegrity` rule presence | 1.0 if FK has test evidence at pass rate; 1.0 if rule exists; 0.5 if FK but no rule; 1.0 if not FK |
| Schema Conformance | 0.08 | DCAT-2 + QualityMeasurement | 0.333 each for: has dataType, has nullable, has profiling |
| Rule Coverage | 0.08 | PropertyShape presence | 1.0 if column has any rule; 0.0 otherwise |
| Documentation | 0.12 | `:ColumnDescription` status | 1.0 approved; 0.35 pending/draft; 0.10 rejected; 0.0 none |
| **Grounding** (new) | **0.10** | `:PropertyShape` `ruleSource` + `status` | fraction of the column's rules with `ruleSource IN ('domain','external')` AND `status='approved'`; 0.0 if no rules or none grounded |

A **composite** score at each level is the weighted average of all dimension scores. Weights sum to 1.0.

## Evidence property

Each `:QualityScore` carries an `evidence` property (column/dataset/overall levels, dimension-scoped):

- `test` — dimension derived from `:TestResult` pass rates
- `profile` — dimension derived from profiling measurements
- `rule` — dimension derived from rule presence alone
- `meta` — dimension derived from schema/description metadata

The UI uses this to badge which dimensions are test-driven vs. rule-only.

## Graph Model

### Nodes added

| Node | Description |
|------|-------------|
| `:QualityDimension` | 7 shared dimension definitions (idempotent via MERGE) |
| `:QualityScore` | One per column×dimension, dataset×dimension, overall×dimension, plus composites |

### QualityScore properties

- `uri` — unique identifier
- `level` — `column`, `dataset`, or `overall`
- `dimension` — one of the six dimensions or `composite`
- `score` — float 0.0–1.0
- `weight` — the weight used in composite calculation
- `scoredAt` — ISO datetime timestamp
- `batchId` — groups all scores from one scoring run

### Relationships added

| Relationship | Meaning |
|---|---|
| `(:Column)-[:HAS_QUALITY_SCORE]->(:QualityScore)` | Column-level score |
| `(:Dataset)-[:HAS_QUALITY_SCORE]->(:QualityScore)` | Dataset-level aggregate |
| `(:Catalog)-[:HAS_QUALITY_SCORE]->(:QualityScore)` | Overall/catalog-level aggregate |
| `(:QualityScore)-[:SCORED_ON_DIMENSION]->(:QualityDimension)` | Links to dimension definition |

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/generate_scores_cypher.py` | Queries graph, computes scores, writes `dq_scores.cypher` |
| `scripts/run_scores_cypher.py` | Loads the Cypher into Neo4j with pre-flight checks |

Both scripts support `--help`.

**Dependency:** requires the `neo4j` Python driver — install with `pip install neo4j`.

## Workflow

### Step 1 — Confirm prerequisites

The graph must already contain:
- `:Dataset` and `:Column` nodes (from `data-discovery-to-dcat-neo4j`)
- `:QualityMeasurement` and `:TopValue` nodes (from `data-profiling-to-dqv-neo4j`)
- `:PropertyShape` nodes (from `data-quality-rule-generation`)

Check (use the project code to scope): `MATCH (:Project {projectCode: '<project_code>'})-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset) WHERE ds.row_count IS NOT NULL RETURN count(ds)` — should be > 0.

### Step 2 — Generate the Cypher script

```bash
python ${CLAUDE_SKILL_DIR}/scripts/generate_scores_cypher.py [output_file] --project-code <project_code> [options]
```

- `output_file` — output path (default: `./cypher_scripts/dq_scores.cypher`)
- `--project-code` — **REQUIRED when provided in the prompt.** Scopes dataset queries through `:Project` node and prefixes score URIs.
- Connection flags: `--host`, `--bolt-port`, `--username`, `--password`, `--database`

The script connects to Neo4j, queries enriched datasets scoped to the project, computes scores across all six dimensions, and writes the Cypher file.

Report:
- Full path of the generated file
- Dataset count, column count, total score nodes
- Show the first ~25 lines so the user can verify structure

### Step 3 — Load into Neo4j

```bash
python ${CLAUDE_SKILL_DIR}/scripts/run_scores_cypher.py [cypher_file] [options]
```

Default connection settings match the other skills in this suite (`localhost:7687`, `neo4j`/`your_password`).

Use `--dry-run` to preview without executing.

**Pre-flight checks (per table at load time):**
1. The `:Dataset` node must exist.
2. The `:Dataset` must not already have scores for this batch (prevents duplicate loading).

## Notes

- Each scoring run creates a new batch. To remove all scores:
  ```cypher
  MATCH (n:QualityScore) DETACH DELETE n;
  MATCH (n:QualityDimension) DETACH DELETE n;
  ```
- Scores reflect evidence at scoring time — re-run after profiling or rule changes to update.
- The composite score uses fixed weights. Weights are stored on `:QualityDimension` nodes as `defaultWeight`, so they can be queried and adjusted via Cypher.
