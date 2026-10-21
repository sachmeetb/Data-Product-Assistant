# data-product-spec-to-dprod-neo4j

Reads a data product spec YAML (produced by `data-product-spec-writer`) and registers it in Neo4j as a full **DPROD-aligned** knowledge graph, using the [DPROD Data Product Ontology](https://ekgf.github.io/dprod/).

This skill is the graph-registration counterpart to the spec-writer. Where the spec-writer asks questions and writes YAML, this skill interprets that YAML and creates the formal DPROD node structure in the graph.

---

## Source vs. product dataset separation

DPROD draws a clear boundary between **source data** (what was discovered) and **product output data** (what is promised to consumers). This skill enforces that boundary:

| Concept | Node label | Meaning |
|---|---|---|
| Source dataset (DCAT) | `:Dataset` | Discovered from PostgreSQL by data-discovery skill. Never modified. |
| Product output dataset | `:DProdOutputDataset` | Created by this skill. Represents the product's output contract. |

The key relationships:

```
(:DProdInputPort)      -[:DPROD_INPUT_DATASET]->  (:Dataset)              ← source side
(:DProdOutputPort)     -[:DPROD_OUTPUT_DATASET]-> (:DProdOutputDataset)   ← product side
(:DProdOutputDataset)  -[:DERIVED_FROM]->          (:Dataset)              ← lineage link
```

Existing `:NodeShape` and `:Catalog` nodes are also linked without duplication:

| Existing node | Relationship added |
|---|---|
| `:NodeShape` (SHACL rules) | `(:DProdOutputPort)-[:HAS_QUALITY_SHAPE]->(:NodeShape)` |
| `:Catalog` (dcat:Catalog) | `(:DProdOutputPort)-[:FROM_CATALOG]->(:Catalog)` |
| `:DataProduct` (registry) | `(:DProdDataProduct)-[:REGISTERED_AS]->(:DataProduct)` |

Dataset links are resolved at load time via the registry `:DataProduct` node's `[:INCLUDES_DATASET]` relationships, which correctly scopes table names to the right schema without ambiguity.

---

## DPROD Graph Model

### Nodes created

| Label | URI pattern | Description |
|---|---|---|
| `:DProdDataProduct` | `dprod:<product_id>` | Core data product node |
| `:DProdInputPort` | `dprod:<product_id>:inputPort:<n>` | One per upstream source system |
| `:DProdOutputPort` | `dprod:<product_id>:outputPort:main` | Single output port serving all tables |
| `:DProdOutputDataset` | `dprod:<product_id>:outputDataset:<table>` | One per table — product contract node (distinct from DCAT `:Dataset`) |
| `:DProdLifecycleStatus` | `dprod:lifecycle:<status>` | Shared status nodes (MERGE — idempotent) |
| `:DProdVersion` | `dprod:<product_id>:version:<ver>` | One per changelog entry |
| `:DProdAccessPolicy` | `dprod:<product_id>:access` | Access control and classification |

### Relationships added

| Relationship | Meaning |
|---|---|
| `(:DProdDataProduct)-[:DPROD_LIFECYCLE_STATUS]->(:DProdLifecycleStatus)` | Product status |
| `(:DProdDataProduct)-[:DPROD_INPUT_PORT]->(:DProdInputPort)` | Source ingestion interface |
| `(:DProdDataProduct)-[:DPROD_OUTPUT_PORT]->(:DProdOutputPort)` | Consumer-facing interface |
| `(:DProdDataProduct)-[:DPROD_HAS_VERSION]->(:DProdVersion)` | Version history |
| `(:DProdDataProduct)-[:HAS_ACCESS_POLICY]->(:DProdAccessPolicy)` | Access and security |
| `(:DProdDataProduct)-[:REGISTERED_AS]->(:DataProduct)` | Links to registry marker |
| `(:DProdInputPort)-[:DPROD_INPUT_DATASET]->(:Dataset)` | **Source side: links to DCAT discovered tables** |
| `(:DProdOutputPort)-[:DPROD_OUTPUT_DATASET]->(:DProdOutputDataset)` | **Product side: links to product contract nodes** |
| `(:DProdOutputDataset)-[:DERIVED_FROM]->(:Dataset)` | **Source-aligned lineage: product → source** |
| `(:DProdOutputPort)-[:HAS_QUALITY_SHAPE]->(:NodeShape)` | **Reuses existing DQ rule containers** |
| `(:DProdOutputPort)-[:FROM_CATALOG]->(:Catalog)` | **Reuses existing DCAT Catalog node** |

### Key properties on :DProdDataProduct

`id`, `name`, `domain`, `type`, `version`, `status`, `owner`, `stewardTeam`, `contactChannel`, `businessPurpose`, `primaryConsumers[]`, `keyUseCases[]`, `overallQualitySla`, `issueHandling`, `testExecutionPoint`, `refreshPattern`, `latencyTargetMinutes`, `availabilitySla`, `retention`, `dcatType: 'dprod:DataProduct'`

### Key properties on :DProdOutputPort

`platform`, `classification`, `accessModel`, `enforcementPlatform`, `enforcementRoles[]`, `complianceConstraints[]`, `restrictions[]`, `hasMaskingRules`, `dcatType: 'dprod:OutputPort'`

---

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/generate_dprod_cypher.py` | Reads spec YAML → writes `<spec_stem>_dprod.cypher` |
| `scripts/run_dprod_cypher.py` | Loads Cypher into Neo4j with pre-flight checks |

---

## generate_dprod_cypher.py

```bash
python ${CLAUDE_SKILL_DIR}/scripts/generate_dprod_cypher.py \
  <spec_yaml_file> \
  [output_cypher_file]
```

- `spec_yaml_file` — the spec YAML produced by `data-product-spec-writer`
- `output_cypher_file` — defaults to `<spec_stem>_dprod.cypher` in the same directory

No Neo4j connection needed — generates pure Cypher. Dataset resolution happens at load time via MATCH statements.

---

## run_dprod_cypher.py

```bash
python ${CLAUDE_SKILL_DIR}/scripts/run_dprod_cypher.py \
  <cypher_file> \
  [--host localhost] [--bolt-port 7687] \
  [--username neo4j] [--password your_password] \
  [--database neo4j] \
  [--dry-run] [--force]
```

**Pre-flight checks:**
1. No `:DProdDataProduct` with the same URI already exists — blocked unless `--force`
2. Registry `:DataProduct` node exists — warns if missing (dataset links will fail)
3. At least one `[:INCLUDES_DATASET]` link exists — warns if zero found

**`--force`**: skips the duplicate check. To fully re-load, first delete existing DPROD nodes:
```cypher
MATCH (n) WHERE n:DProdDataProduct OR n:DProdInputPort OR n:DProdOutputPort
           OR n:DProdVersion OR n:DProdAccessPolicy
DETACH DELETE n;
MATCH (n:DProdLifecycleStatus) DETACH DELETE n;
```

---

## Workflow

### Step 1 — Find the spec YAML

Look for `*_spec.yaml` files in the current working directory. If multiple are found, list them and ask the user which to use. If only one exists, proceed automatically.

### Step 2 — Generate DPROD Cypher

```bash
python ${CLAUDE_SKILL_DIR}/scripts/generate_dprod_cypher.py \
  <spec_yaml>
```

Show the user a summary:
```
Generated: dp_employees_v1_spec_dprod.cypher
  Product:    dp_employees_v1
  Tables:     6  (→ 6 DPROD_OUTPUT_DATASET links to existing :Dataset nodes)
  Statements: N  (MERGE: 3, CREATE: N)
```

Show the first ~20 lines so the user can verify structure.

### Step 3 — Load into Neo4j

```bash
python ${CLAUDE_SKILL_DIR}/scripts/run_dprod_cypher.py \
  dp_employees_v1_spec_dprod.cypher \
  --password your_password
```

Report per-statement feedback and final summary:
```
Registered in graph:
  :DProdDataProduct        uri=dprod:dp_employees_v1
  :DProdInputPort          × 1
  :DProdOutputPort         × 1  (→ 6 :Dataset links, 6 :NodeShape links)
  :DProdLifecycleStatus    × 3  (MERGE — shared)
  :DProdVersion            × 1
  :DProdAccessPolicy       × 1
  [:REGISTERED_AS]         → :DataProduct registry node
```

---

## Spec YAML → DPROD field mapping

| Spec field | DPROD node / property |
|---|---|
| `product.id` | `:DProdDataProduct.id`, URI suffix |
| `product.name` | `:DProdDataProduct.name` |
| `product.domain` | `:DProdDataProduct.domain` |
| `product.type` | `:DProdDataProduct.type` |
| `product.status` | `[:DPROD_LIFECYCLE_STATUS]->(:DProdLifecycleStatus)` |
| `product.owner` | `:DProdDataProduct.owner` |
| `product.steward_team` | `:DProdDataProduct.stewardTeam` |
| `product.purpose.business_purpose` | `:DProdDataProduct.businessPurpose` |
| `product.purpose.primary_consumers` | `:DProdDataProduct.primaryConsumers[]` |
| `product.purpose.key_use_cases` | `:DProdDataProduct.keyUseCases[]` |
| `product.operations.platform_location` | `:DProdOutputPort.platform` |
| `product.operations.refresh_pattern` | `:DProdDataProduct.refreshPattern` |
| `product.operations.latency_target_minutes` | `:DProdDataProduct.latencyTargetMinutes` |
| `product.operations.availability_sla` | `:DProdDataProduct.availabilitySla` |
| `product.operations.retention` | `:DProdDataProduct.retention` |
| `product.access.classification` | `:DProdOutputPort.classification`, `:DProdAccessPolicy.classification` |
| `product.access.access_model` | `:DProdOutputPort.accessModel` |
| `product.access.enforcement.platform` | `:DProdAccessPolicy.enforcementPlatform` |
| `product.access.enforcement.roles` | `:DProdAccessPolicy.enforcementRoles[]` |
| `product.access.masking_rules` | `:DProdAccessPolicy.maskingRules[]` |
| `product.access.compliance_constraints` | `:DProdAccessPolicy.complianceConstraints[]` |
| `product.quality.overall_sla` | `:DProdDataProduct.overallQualitySla` |
| `product.lineage.upstream_systems` | `:DProdInputPort.sourceName`, `.sourceOwner` |
| `product.lineage.ingestion_pipelines` | `:DProdInputPort.ingestionPipeline`, `.pipelineRepo` |
| `product.lineage.transformations_applied` | `:DProdInputPort.transformationsApplied[]` |
| `product.tables[*].name` | `[:DPROD_OUTPUT_DATASET]->(:Dataset {name})` |
| `product.versioning.version` | `:DProdVersion.version` |
| `product.versioning.changelog[*]` | `:DProdVersion` node per entry |
| `product.versioning.breaking_change_policy` | `:DProdVersion.breakingChangePolicy` |

---

## Notes

- `TODO` and `null` values in the spec are preserved as `null` in the graph — the product node is still created and fully linked.
- `:DProdLifecycleStatus` nodes use `MERGE` and are shared across all products — safe to load multiple times.
- All other nodes use `CREATE` — the pre-flight check prevents duplicates on re-run.
- The `[:DPROD_OUTPUT_DATASET]` and `[:HAS_QUALITY_SHAPE]` links require the registry `:DataProduct` node (from `data-product-spec-writer` Step 11). If it doesn't exist, those links won't be created (the MATCH finds nothing), but all other nodes are still created.
- To re-run cleanly after a failed partial load, use the DETACH DELETE query above before re-running.
