---
name: data-profiling-to-dqv-neo4j
description: Generates a Cypher script from data-profiling YAML files and loads column-level statistics into a Neo4j knowledge graph using the W3C Data Quality Vocabulary (DQV). Use this skill whenever the user wants to enrich a Neo4j knowledge graph with profiling data, load quality measurements into Neo4j, attach null rates, distinct counts, or column statistics to an existing DCAT graph, or represent data quality metrics as graph nodes. Also trigger when the user says "load profiling data to graph", "enrich the graph with profiling stats", "add DQV measurements", or "profile data to Neo4j". Requires the data-discovery-to-dcat-neo4j skill to have been run first — this skill enriches existing :Dataset and :Column nodes.
---

## What this skill does

Reads `<schema>__<table>__profile.yaml` files produced by the `data-profiling` skill and:

1. Generates a `dqv_profile.cypher` file with Cypher statements that enrich the existing knowledge graph with column-level quality measurements.
2. Loads the script into Neo4j, checking at load time that target nodes exist and are not already enriched.

The graph uses the [W3C Data Quality Vocabulary (DQV)](https://www.w3.org/TR/vocab-dqv/) for quality measurements, linked to the existing DCAT-2 graph created by `data-discovery-to-dcat-neo4j`.

## Graph Model

### Nodes added

| Node | Description |
|------|-------------|
| `:Metric` | Shared metric definition (e.g. `null_rate`, `distinct_count`). Created with `MERGE` — safe to re-run. |
| `:QualityMeasurement` | A specific measured value for one column and one metric. Created with `CREATE`. |
| `:TopValue` | A frequently-occurring value for a column, with its count and frequency. Created with `CREATE`. Only present for columns where the profiler captured top values (text, boolean, low-cardinality numeric). |

### Properties added to existing nodes

| Node | Properties added |
|------|-----------------|
| `:Dataset` | `row_count`, `sample_size`, `profiled_at` |

### Relationships added

| Relationship | Meaning |
|---|---|
| `(:Column)-[:HAS_QUALITY_MEASUREMENT]->(:QualityMeasurement)-[:ON_METRIC]->(:Metric)` | Column has a measured value for a specific metric |
| `(:Column)-[:HAS_TOP_VALUE]->(:TopValue)` | Column has a frequently-occurring value with count and frequency |

### Metric URIs

Metrics follow the pattern `metric:<name>`:
- `metric:null_count`, `metric:null_rate`
- `metric:distinct_count`, `metric:min`, `metric:max`
- `metric:mean`, `metric:stddev`
- `metric:percentile_25`, `metric:percentile_50`, `metric:percentile_75`
- `metric:min_length`, `metric:max_length`, `metric:avg_length`

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/generate_dqv_cypher.py` | Reads profile YAMLs, writes a DQV Cypher enrichment script |
| `scripts/run_dqv_cypher.py` | Loads the script into Neo4j with pre-flight existence and enrichment checks |

Both scripts support `--help`.

**Dependencies:**
- `generate_dqv_cypher.py` requires `pyyaml` — install with `pip install pyyaml`
- `run_dqv_cypher.py` requires the `neo4j` Python driver — install with `pip install neo4j`

## Workflow

### Step 1 — Get the connection string

If not already provided, ask the user for their Neo4j connection details. Defaults are:

| Setting | Default |
|---------|---------|
| Host | `localhost` |
| Bolt port | `7687` |
| Username | `neo4j` |
| Password | `your_password` |
| Database | `neo4j` |

### Step 2 — Find profile YAML files

List `*__*__profile.yaml` files in the working directory. Show the user the available tables, numbered:

```
Available tables:
  1. employees.department        (employees__department__profile.yaml)
  2. employees.employee          (employees__employee__profile.yaml)
  ...
```

If none are found, tell the user to run the `data-profiling` skill first.

### Step 3 — Ask which tables to include

"Which tables would you like to load profiling data for? List numbers, names, or say 'all'."

### Step 4 — Generate the Cypher script

```bash
python ${CLAUDE_SKILL_DIR}/scripts/generate_dqv_cypher.py [yaml_dir] [output_file] --project-code <project_code>
```

- `yaml_dir` — directory containing the `*__*__profile.yaml` files (default: `.`)
- `output_file` — output path (default: `<yaml_dir>/dqv_profile.cypher`)
- `--project-code` — **REQUIRED when provided in the prompt.** Scopes dataset/column URIs to match the DCAT nodes (e.g., `dataset:{project_code}:{schema}.{table}`).

Report:
- Full path of the generated file
- Summary counts: datasets, columns, total quality measurements
- Show the first ~20 lines so the user can verify the structure

### Step 5 — Load into Neo4j

```bash
python ${CLAUDE_SKILL_DIR}/scripts/run_dqv_cypher.py [cypher_file] [options]
```

Override connection defaults with flags:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/run_dqv_cypher.py dqv_profile.cypher \
  --host localhost \
  --bolt-port 7687 \
  --username neo4j \
  --password your_password \
  --database neo4j
```

Use `--dry-run` to preview without executing.

**Pre-flight checks (done at load time, per table):**

1. The `:Dataset` node must exist in the graph. If not, the table is skipped with a warning — run `data-discovery-to-dcat-neo4j` first.
2. The `:Dataset`'s columns must not already have `[:HAS_QUALITY_MEASUREMENT]` relationships. If they do, the table is skipped — it is already enriched.

The loader prints per-statement feedback (nodes created, relationships created) and exits non-zero if any statement fails.

## Notes

- `:Metric` nodes use `MERGE` and are always safe to re-run.
- `:QualityMeasurement` nodes use `CREATE`, so loading an already-enriched table would produce duplicates — the pre-flight check prevents this.
- Top-value distributions are intentionally not stored in the graph (they remain in the profile YAML files).
- To clear profiling data and re-run cleanly:
  ```cypher
  MATCH (n:QualityMeasurement) DETACH DELETE n;
  MATCH (n:TopValue) DETACH DELETE n;
  MATCH (n:Metric) DETACH DELETE n;
  MATCH (ds:Dataset) WHERE ds.row_count IS NOT NULL
    REMOVE ds.row_count, ds.sample_size, ds.profiled_at;
  ```
