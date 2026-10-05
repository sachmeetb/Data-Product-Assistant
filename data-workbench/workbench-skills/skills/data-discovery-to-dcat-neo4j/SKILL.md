---
name: data-discovery-to-dcat-neo4j
description: Generates a Cypher script to load data-discovery YAML metadata into a Neo4j knowledge graph using the W3C DCAT-2 ontology. Use this skill whenever the user has run data discovery and wants to represent their database schema as a knowledge graph, export to Neo4j, generate Cypher queries from YAML metadata, map tables to DCAT ontology, or create a graph-based representation of their data catalog. Also trigger when the user mentions "knowledge graph", "DCAT", "Neo4j", "Cypher script", or wants to convert database metadata into graph format.
---

## What this skill does

Reads `<schema>__<table>.yaml` files produced by the `data-discovery` skill and generates a `catalog.cypher` file containing Cypher `CREATE` statements that populate a Neo4j knowledge graph. The graph models the database schema using the [W3C DCAT-2 ontology](https://www.w3.org/TR/vocab-dcat-2/).

## DCAT-2 Mapping

| Database concept | Neo4j node/label |
|---|---|
| Schema | `:Catalog` (represents `dcat:Catalog`) |
| Table | `:Dataset` (represents `dcat:Dataset`) |
| Column | `:Column` (custom node, linked via `[:HAS_COLUMN]`) |

| Relationship | Cypher relationship |
|---|---|
| Schema contains table | `(:Catalog)-[:DCAT_DATASET]->(:Dataset)` |
| Table contains column | `(:Dataset)-[:HAS_COLUMN]->(:Column)` |
| Foreign key reference (table-level) | `(:Dataset)-[:REFERENCES {constraintName, columns, referencedColumns, onDelete, onUpdate}]->(:Dataset)` |
| Foreign key reference (column-level) | `(:Column)-[:FK_REFERENCES {constraintName}]->(:Column)` — pair-wise edge from each FK column to its referenced PK column. Used by ERD-style visualizations to route edges between specific columns inside table cards. |

Every node carries:
- `uri` — a unique, human-readable identifier (e.g. `dataset:employees.employee`, `column:employees.employee.id`)
- `dcatType` — the ontology class string (e.g. `dcat:Catalog`, `dcat:Dataset`)

## Workflow

### Step 1 — Generate the Cypher script

1. Confirm the working directory contains data-discovery YAML files (pattern `<schema>__<table>.yaml`; exclude `*__profile.yaml` files).
2. Run:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/generate_cypher.py [yaml_dir] [output_file] --project-code <project_code>
```

- `yaml_dir` — directory containing the YAML files (defaults to `.`)
- `output_file` — path for the generated Cypher script (defaults to `catalog.cypher` in `yaml_dir`)
- `--project-code` — **REQUIRED when provided in the prompt.** Scopes all URIs to this project (e.g., `dataset:{project_code}:{schema}.{table}`) and creates the `:Project` node with `:HAS_CATALOG` links.

3. Report to the user:
   - Full path of the generated `catalog.cypher`
   - Summary counts: schemas (Catalogs), tables (Datasets), columns, and FK relationships
4. Show the first ~20 lines of the output so the user can verify the structure looks correct.

### Step 2 — Load into Neo4j

Use the bundled loader script to execute the generated Cypher against the Neo4j instance:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/run_cypher.py [cypher_file] [options]
```

**Default connection settings** (matching the configured instance):

| Setting | Default |
|---|---|
| Host | `localhost` |
| Bolt port | `7687` |
| Username | `neo4j` |
| Password | `your_password` |
| Database | `neo4j` |

Override any default with flags:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/run_cypher.py catalog.cypher \
  --host localhost \
  --bolt-port 7687 \
  --username neo4j \
  --password your_password \
  --database neo4j
```

Use `--dry-run` to preview what statements would be executed without touching the database.

The script prints per-statement feedback (nodes created, relationships created) and exits non-zero if any statement fails.

**Dependency:** requires the `neo4j` Python driver — install with `pip install neo4j` if not present.

## Notes

- The script uses `CREATE` (not `MERGE`), so running it twice against the same Neo4j database will produce duplicates. To re-run cleanly, first clear existing nodes:
  ```cypher
  MATCH (n) WHERE n:Catalog OR n:Dataset OR n:Column DETACH DELETE n;
  ```
- Profile YAML files (`*__profile.yaml`) are automatically ignored — they contain data statistics, not schema metadata.
- If a YAML file references a foreign key to a table that has no corresponding YAML file, the `REFERENCES` relationship will still be generated; Neo4j will simply not find the target node if it wasn't created.
