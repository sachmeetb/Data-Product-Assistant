---
name: odcs-to-graph
description: Persists ODCS (Open Data Contract Standard) YAML specs to a Neo4j knowledge graph, materialises YAML from graph, and transforms ODCS contracts to DPROD data product representations. Use this skill when the user wants to save a data contract to Neo4j, reconstruct an ODCS YAML from the graph, or generate a dprod subgraph from an ODCS contract.
---

# ODCS-to-Graph

Reads ODCS v3.1 data contract YAML files and persists them as a graph substructure in Neo4j. Can also reconstruct ODCS YAML from the graph and transform contracts into DPROD data product representations.

The save script normalises common v3.1 authoring variations (`fields` vs `properties`, `nullable` vs `required`, schema-level `primaryKeys`, `sla` vs `slaProperties`, `quality[].type` vs `.rule`, `customProperties.terms` lifting, etc.) to a single canonical shape before writing.

## When to Use

- After authoring or editing an ODCS YAML data contract
- To persist a data contract to the knowledge graph
- To reconstruct YAML from an existing contract in the graph
- To generate a DPROD data product representation from an ODCS contract
- To publish a data product (set status to 'published')

## Prerequisites

- ODCS YAML file (v3.1 format) or existing contract in Neo4j
- `neo4j` and `pyyaml` Python packages installed
- Neo4j connection details

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/save_odcs_to_graph.py` | Canonicalize ODCS v3.1 YAML and persist to Neo4j (detach-delete + recreate substructure) |
| `scripts/read_odcs_from_graph.py` | Read contract from graph by ID, reconstruct ODCS v3.1 YAML |
| `scripts/odcs_to_dprod.py` | Transform ODCS contract to DPROD representation, optionally publish |

All scripts support `--help` and `--dry-run`.

**Dependency:** all scripts require `neo4j` and `pyyaml` — install with `pip install neo4j pyyaml`.

## Graph Model (v3.1)

### ODCS Contract Subgraph

Top-level v3.1 fields live on `:DataContract` itself (no `:DataContractInfo` node). Extended sections that have open-ended sub-structure (`support`, `customProperties`, server `datasets`, property `logicalTypeOptions`, schema `foreignKeys`) are persisted as JSON strings so nothing is silently dropped on round-trip. Unknown top-level fields are captured as `dc.extras` (JSON).

```
(:DataContract {
    id, name, version, status, apiVersion, kind,
    domain, dataProduct, description, purpose, limitations,
    tags, support (json), customProperties (json), extras (json), updatedAt
  })
  -[:HAS_OWNER]-> (:DataContractOwner {username, name, role, email})
  -[:HAS_STEWARD]-> (:DataContractSteward {name, email, role, username})
  -[:HAS_TEAM_MEMBER]-> (:DataContractTeamMember {username, role, name, email})
  -[:HAS_ROLE]-> (:DataContractRole {role, description, access, datasets})
  -[:HAS_SERVER]-> (:DataContractServer {name, environment, type, account, database, schema, datasets (json)})
  -[:HAS_SCHEMA]-> (:DataContractSchema {name, physicalName, physicalType, description, foreignKeys (json)})
    -[:HAS_PROPERTY]-> (:DataContractProperty {
        name, physicalName, logicalName, logicalType, physicalType, description,
        primaryKey, required, criticalDataElement,
        pii, classification, examples, logicalTypeOptions (json)
    })
  -[:HAS_QUALITY_RULE]-> (:DataContractQuality {rule, name, description, severity, dimension, businessImpact})
  -[:HAS_SLA_PROPERTY]-> (:DataContractSLAProperty {property, value, unit})
  -[:HAS_TERMS]-> (:DataContractTerms {usage, limitations, billing, noticePeriod})
```

### ODCS → DPROD Transformation

DProdColumn keeps DPROD-spec field names (`dataType`, `isPrimaryKey`) and is populated from the v3.1 ODCS `:DataContractProperty` fields (`physicalType`, `primaryKey`).

```
(:DataContract) -[:MATERIALISES_AS]-> (:DProdDataProduct {uri, name, status})
  -[:DPROD_OUTPUT_PORT]-> (:DProdOutputPort)
    -[:DPROD_OUTPUT_DATASET]-> (:DProdOutputDataset)
      -[:HAS_PRODUCT_COLUMN]-> (:DProdColumn {name, logicalName, dataType, logicalType, description, isPrimaryKey})
```

## Workflow

### Step 1 — Get connection details

| Setting | Default |
|---------|---------|
| Host | `localhost` |
| Bolt port | `7687` |
| Username | `neo4j` |
| Password | `your_password` |
| Database | `neo4j` |

### Step 2 — Save ODCS spec to graph

```bash
python ${CLAUDE_SKILL_DIR}/scripts/save_odcs_to_graph.py \
  contract.yaml \
  --contract-id "my-contract-id" \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

If `--contract-id` is omitted, the `id` field from the YAML is used.

### Step 3 — Materialise YAML from graph (optional)

```bash
python ${CLAUDE_SKILL_DIR}/scripts/read_odcs_from_graph.py \
  --contract-id "my-contract-id" \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db> \
  --output materialised_contract.yaml
```

### Step 4 — Transform to DPROD (optional)

```bash
python ${CLAUDE_SKILL_DIR}/scripts/odcs_to_dprod.py \
  --contract-id "my-contract-id" \
  --host <host> --bolt-port <port> --username <user> --password <pass> --database <db>
```

Add `--publish` to also set the data product status to 'published'.

### Step 5 — Report

After each operation, report:
- Contract ID and name
- Number of schemas, columns, quality rules, SLAs created/read
- For DPROD transformation: the generated dprod URI
