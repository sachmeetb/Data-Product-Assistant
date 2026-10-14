# Data Product Assistant (Bronze + Silver + Gold) × DATA-WORKBENCH
## Full Comparative Analysis

---

## 1. SILVER DATA PRODUCT — What It Is

### Core Purpose
Silver takes a banking business requirement and generates the **conformed canonical layer** — standardized, SCD Type-2 versioned, DQ-flagged entity schemas that transform raw Bronze source data into `banking_silver.slv_<entity>` tables. It answers: *"What conformed BigQuery tables turn raw source data into BIAN/ISO 20022/FIBO-aligned canonical entities?"*

### Pipeline Architecture — Same 7-stage structure as Bronze, Silver-specific in stages 3–4

| Stage | Agent | Silver-specific purpose |
|---|---|---|
| 1 | `bank_profile_agent` | Identical to Bronze |
| 2 | `requirement_understanding_agent` | Identical to Bronze |
| 3 | **`domain_scoping_agent`** | Maps to BIAN service domains; selects business semantic common blocks per Silver table |
| 4 | **`silver_product_engine`** | Expands selected common blocks into full column definitions with SCD2 envelope, surrogate keys, lineage, DQ columns |
| 5 | `spec_generator_agent` | Generates BigQuery DDL for `banking_silver` dataset with `slv_` prefix tables |
| 6 | `validator_agent` | Standards compliance check |
| 7 | `BigQueryPublisher` | Executes DDL |

### How Silver Consumes Bronze Output
Silver does **NOT** programmatically consume Bronze output. It takes the same inputs (bank description + NL requirement) and generates Silver schemas independently. The conceptual dependency is manual: a practitioner runs Bronze first to land raw tables, then runs Silver to create conformed tables that reference those Bronze tables. The generated STTM shows `raw_core.<col>` → `banking_silver.slv_<entity>.<col>` but these references are AI-generated, not validated against actual Bronze DDL.

### What Silver ADDS vs Bronze

| Dimension | Bronze | Silver |
|---|---|---|
| **Target dataset** | `banking_bronze` (raw landing) | `banking_silver` (conformed) |
| **Table naming** | `raw_<system>_<table>` | `slv_<entity>` (BIAN-aligned) |
| **Technical envelope** | Ingestion: `ingest_batch_id`, `ingest_ts`, `source_file_name`, `raw_payload_hash` | SCD2: `surrogate_key`, `effective_from_ts`, `effective_to_ts`, `is_current`, `version_number`, `is_deleted`, `record_hash`, `dq_status` (PASS/WARN/QUARANTINE), `dq_score`, `failed_rules`, `is_enriched` |
| **Common blocks** | Modality-specific: `mainframe-copybook`, `swift-envelope`, `sap-idoc`, `api-crm-metadata`, `unstructured-object-metadata` | Business semantics: `money`, `identifier`, `code-value`, `postal-address`, `party-name`, `contact-point`, `temporal`, `rate`, `quantity` |
| **Transformations** | CAST_TO_STRING/NUMERIC/TIMESTAMP only | UPPER(TRIM()), CAST, SCD Type-2 logic, deduplication, CDC via `record_hash` |
| **Data quality** | Validator checks schema standards | Per-row `dq_status` (PASS/WARN/QUARANTINE), `failed_rules` JSON array |
| **Domain knowledge** | Source modality (SWIFT/mainframe/SAP/API) | BIAN service domains, ISO 20022, FIBO entities, regulatory column derivation (IFRS9 ECL stage, PEP flag, Basel III PD score) |

Silver's `technical-metadata.yaml` defines the full conformation envelope: Lineage + Versioning (SCD2) + DataQuality sub-schemas.

### LLM / GCP / Deployment
Identical to Bronze: Gemini 2.5 Flash on Vertex AI, BigQuery (`banking_silver` dataset), Dataplex Knowledge Catalog, Cloud Run, Cloud Build.

### Outputs

| Artifact | Silver version |
|---|---|
| BigQuery Silver DDL | `slv_banking_schema_*.sql` — SCD2 partitioned by `DATE(silver_load_ts)`, clustered by `surrogate_key` |
| Silver Data Contract | `slv_banking_contract_v1.0_*.yaml` — **OpenDataContract Spec 0.9.3** (NOT ODCS v2.2 like Bronze) |
| STTM Mapping Workbook | `slv_sttm_mapping_v1_*.xlsx` — `raw_core.*` → `banking_silver.slv_*` with transforms |
| Governance Metadata | `slv_metadata_v1_*.xlsx` — 2-tab: table metadata + column metadata |
| Sample Transformation Audit | `slv_sample_run_audit_*.xlsx` — 3-tab: sample data + transformation trace + DQ results |
| Dataplex Manifest | `slv_dataplex_catalog_manifest_*.json` |
| Airflow DAG | `slv_pipeline_dag_*.py` (hidden from frontend by design) |
| PDFs | Bank Profile Brief, Requirements Brief, Data Availability Report |

### Frontend Differences from Bronze
Silver adds `ChallengerCard.jsx`, `StartingPointCards.jsx` (template starting points), `EditRequirementsForm.jsx`. Server has `/catalog/domains` endpoint returning 6 banking domains (`core_banking`, `payments`, `cards`, `lending`, `customer_360`, `compliance_risk`) with derived metrics per domain (NIM, LTV, DTI, DAB).

---

## 2. GOLD DATA PRODUCT — What It Is

### Core Purpose
Gold is **architecturally distinct** from Bronze and Silver. It is a **Data Product Identification (DPI) system** — not a schema generator. It takes a business user's NL requirement and **discovers what data already exists across Bronze/Silver/Gold tables**, producing a Data Product definition: KPI computability, dimension coverage, gaps, recommended tables per layer, and a Challenger review. It answers: *"Do the data products to fulfill this requirement already exist, and what exactly needs to be built new?"*

### Pipeline Architecture — 4-Agent DPI Pipeline

**Main pipeline (Claude Code skills in `.skills/`):**

| Step | Agent | Output |
|---|---|---|
| 1 | Requirements Understanding Agent (up to 8 clarifying Q&As, one per turn, business language only) | `kpis-<name>.json` |
| 2 | Use Case Classification Agent | `classification-output.json` (type: analytics/data_science/genai/digital_nosql/conformed_data/agentic + schema_design_pattern: star_schema/wide_flat/flat_denorm/event_schema/entity_schema) |
| 3 | Discovery Agent (scans Unity Catalog: `reuse` / `extend` / `build_new` per table) | `discovery-output.json` |
| 4 | Visual Diagram Agent | `visual-output.md` (Mermaid lineage diagrams) |

**Post-confirmation pipeline (Python agents):**

```
challenger.run()   → ChallengerReview {verdict: clean|concerns|blockers,
                       design_queue.curated  = Bronze/Silver tables to design FIRST,
                       design_queue.enriched = Gold tables to design SECOND}
  ↓
kpi_derivation.enrich()  → validates KPI formula against real catalog columns
                            (anti-hallucination: removes cited columns that don't exist)
  ↓
data_product.generate()  → full Data Product definition
                            kpis: {status: computable|needs_new_columns|build_new}
                            dimensions: {status: available|missing}
                            recommended_tables: {gold|silver|bronze: best match}
                            gaps: [{type: missing_kpi|missing_dimension, ...}]
                            build_actions: [{action: extend_table, add_columns: [...]}]
```

### How Gold Relates to Silver/Bronze
Gold does NOT consume DDL programmatically. Gold's Discovery Agent scans the **Unity Catalog** (Databricks) for existing tables. The correct execution order is actually **Gold first → Bronze/Silver after**: Gold tells you what's needed; `design_queue.curated` lists which Bronze/Silver tables to build; practitioners then trigger those agents.

### What Gold ADDS vs Silver

| Dimension | Silver | Gold |
|---|---|---|
| **Purpose** | Generate schema artifacts | Identify data product needs, assess catalog coverage |
| **Input** | Bank description + requirement | NL business requirement only |
| **Output** | DDL, ODCS, STTM, PDFs | Data Product definition JSON, Mermaid lineage, gap analysis, Challenger review |
| **Discovery** | None — generates from scratch | Scans Unity Catalog for existing tables (reuse/extend/build_new) |
| **KPI handling** | Includes KPIs as column specs | Validates KPI computability against real catalog columns (anti-hallucination guard) |
| **Governance** | None | Challenger Agent (devil's advocate review of consistency + completeness) |
| **Design queue** | None | `design_queue.curated` (what Bronze/Silver to build first) + `design_queue.enriched` (what Gold to build) |
| **Use case typing** | None | 6 types: analytics, data_science, genai, digital_nosql, conformed_data, agentic |

### LLM Usage — Key Pivot
**Model: Claude Sonnet (claude-sonnet-4-6)** via Azure AI Services — NOT Gemini. Gold predates the Bronze/Silver stack or was built by a different team. Uses `base.py` with Anthropic SDK + prompt caching. Agent skills defined as `.skills/*.md` consumed by the Claude Agent SDK.

**Anti-hallucination guard** in `kpi_derivation.validate()`: removes any `source_columns` cited by the LLM that don't actually exist in Unity Catalog, moving them to `needs_new_columns`.

### GCP Services
**Minimal to none.** Gold targets **Databricks Unity Catalog**, not BigQuery. `databricks_tool.py` executes against Databricks SQL Statement Execution API. No Vertex AI, no BQ, no Dataplex.

### Deployment
**Azure App Service** (not Cloud Run). `startup.sh` uses Gunicorn + Uvicorn. `.env` uses `ANTHROPIC_ENDPOINT` + Azure AD (`AZURE_CLIENT_ID/TENANT_ID/CLIENT_SECRET`) for Databricks Unity Catalog. `APPLICATIONINSIGHTS_CONNECTION_STRING` for telemetry. **Gold is the only DPA layer targeting Azure.**

---

## 3. BRONZE DATA PRODUCT — What It Is

### Core Purpose
A BFSI-specialized AI agent that takes a natural-language description of a bank and a business requirement, then generates the complete **Bronze landing-zone artifact set** — the raw-ingest layer of a medallion data architecture. It answers: *"What BigQuery tables, ingestion mappings, and data contracts do I need to land this source system data into GCP?"*

### Pipeline Architecture — 7-Stage Sequential Pipeline

| Stage | Agent | Purpose |
|---|---|---|
| 1 | `bank_profile_agent` | Extracts bank name, regions, products, source systems, regulatory frameworks (BIAN, ISO 20022, Basel III) |
| 2 | `requirement_understanding_agent` | Converts NL business requirement → structured: domain, use case, data points, granularity, freshness |
| 3 | `source_scoping_agent` | Maps requirement to source tables; selects `common/` block composition per table |
| 4 | `bronze_product_engine` | Expands common blocks into full BigQuery column definitions ("lego brick" composition) |
| 5 | `spec_generator_agent` | Generates BigQuery DDL + Iceberg DDL + Object Table DDL |
| 6 | `validator_agent` | Checks ingestion-standards compliance; drives feedback loop (up to 3 iterations) |
| 7 | `BigQueryPublisher` | Executes DDL against GCP or dry-runs it |

**Common Blocks** (`common/` directory) — canonical Bronze metadata envelope every table must include:
- `ingestion-metadata` — `ingest_batch_id`, `ingest_ts`, `source_file_name`, `raw_payload_hash` (mandatory on every table)
- `source-identifier`, `temporal`, `quality-flags`, `raw-payload`, `lineage-tracking`, `file-metadata`
- Modality-specific: `api-crm-metadata`, `mainframe-copybook`, `sap-idoc-metadata`, `swift-envelope`, `unstructured-object-metadata`

### LLM Usage
- **Model**: Gemini 2.5 Flash on Vertex AI via `google.genai` (NOT Google ADK — explicitly bypassed)
- **Pattern**: Each agent has a dedicated Markdown system prompt (`prompts/*.md`) + structured JSON output parsing
- **Token budgets**: 4K (profile) → 8K (requirement) → 12K (scoping) → 16K (engine) → 24K (spec) → 8K (validator)
- **Retry loop**: Up to 3 agent retries with structured feedback; up to 3 spec-validation iterations

### GCP Services

| Service | Role |
|---|---|
| Vertex AI (Gemini 2.5 Flash) | All LLM inference |
| BigQuery | DDL execution, BigLake Iceberg external tables, BigLake Object Tables |
| Cloud Storage | Iceberg and Object Table backing store |
| Dataplex Knowledge Catalog | Data product registration via REST API |
| Cloud Run | Hosting (FastAPI + nginx-served React) |
| Cloud Build | CI/CD build pipeline |

### Data Flow

```
User (chat) → bank description + file upload
  ↓ Bank Profile Agent → structured bank profile
  ↓ Requirement Understanding Agent → structured requirement
  ↓ Source Scoping Agent → required_tables + common_block selections
  ↓ Bronze Product Engine → full column specs from common/ blocks
  ↓ Spec Generator Agent → BQ DDL + Iceberg DDL + Object Table DDL
  ↓ Validator Agent → compliance check (up to 3 iterations)
  ↓ BigQuery Publisher → live DDL execution OR dry run
  ↓ artifact_generator → Bank Profile PDF, Requirements Brief PDF,
                          STTM Excel, Business Glossary, Airflow DAG,
                          Dataplex manifest JSON
  ↓ datacontract_generator → ODCS v2.2 Data Contract (.yaml)
  ↓ Dataplex KC API → registered data product with BQ table assets
```

### Frontend
React + Vite SPA with guided chat + artifact downloads. Key components: `DataContractCard` (ODCS viewer/editor), `STTMCard`, `MermaidBlock` (lineage), `BusinessGlossaryCard`, `ClassificationCard`, `FilePreviewCard` (xlsx/csv upload preview). Chat chips drive the flow: bank profile confirm → requirement confirm → generate schema → tweak → publish → KC commit.

### Deployment
Cloud Run (backend + nginx-served React), two separate Cloud Build pipelines, `deploy.ps1`. Session: in-memory by default, Redis for multi-replica.

---

## 4. The Combined Data Product Assistant Stack

### How Bronze → Silver → Gold Hand Off

There is **no programmatic handoff between layers.** Each is a standalone application.

| Handoff | Mechanism | Current state |
|---|---|---|
| Bronze → Silver | Silver's STTM shows `raw_core.*` → `banking_silver.slv_*` but does not parse Bronze DDL | Manual — practitioner maps by convention |
| Silver → Gold | Gold Discovery scans Unity Catalog for existing `slv_*` tables | Indirect — if Silver is deployed, Gold finds those tables |
| Gold → Bronze/Silver | `design_queue.curated` lists tables to build | Manual — practitioner must trigger Bronze/Silver agents per table |

### Is There a Shared Orchestration Layer?
**No.** Each layer has its own `pipeline.py`, `server.py`, and `main.py`. No master orchestrator, no cross-layer session store, no shared API gateway.

### Shared Components

| Component | Bronze | Silver | Gold |
|---|---|---|---|
| `banking_ontology.json` | ✓ | ✓ (copied) | ✗ |
| `bian_service_domains.json` | ✓ | ✓ (copied) | ✗ |
| `session_store.py` | ✓ | ✓ (same code) | ✗ (in-memory dict) |
| `bigquery_tool.py` | ✓ | ✓ (same code) | ✗ (uses `databricks_tool.py`) |
| `artifact_generator.py` | ✓ | ✓ (same code) | ✗ |
| `bank_profile_agent.py` | ✓ | ✓ (same code) | ✗ |
| LLM model | Gemini 2.5 Flash | Gemini 2.5 Flash | Claude Sonnet (Azure) |
| Common blocks | Bronze-specific (ingestion envelope) | Silver-specific (SCD2 + business semantics) | None |

**Bronze and Silver share ~80% of their codebase as copy-paste — not as a shared library. Gold is architecturally distinct.**

### Unified User Journey (as designed, not as implemented)

```
1. GOLD → Business analyst describes requirement
   → DPI discovers existing catalog coverage
   → "You need slv_customer, slv_loan; raw_t24_account doesn't exist yet"
   → design_queue.curated: [raw_t24_account, raw_t24_loan_master]
   → design_queue.enriched: [gld_customer_360]

2. BRONZE → Architect runs for each table in design_queue.curated
   → Generates: raw_t24_account DDL + ODCS v2.2 + STTM + Airflow DAG + Dataplex manifest

3. SILVER → Architect runs for conformed entities
   → Generates: slv_customer DDL (SCD2) + ODCS v0.9.3 + STTM + Dataplex manifest

4. GOLD → Re-runs discovery after Bronze/Silver are deployed
   → KPIs now show "computable" instead of "build_new"
```

### Critical Gaps Between Layers

1. No Gold → Bronze/Silver trigger: `design_queue` outputs names, no API to run the agents
2. No Bronze → Silver contract validation: Silver's STTM references `raw_core.*` generically — doesn't parse actual Bronze DDL
3. **Platform mismatch**: Bronze/Silver target GCP BigQuery; Gold targets Databricks Unity Catalog
4. **Model mismatch**: Bronze/Silver use Gemini; Gold uses Claude on Azure
5. **No shared contract registry**: Bronze = ODCS v2.2, Silver = OpenDataContract 0.9.3, Gold = none — three different formats
6. **No runtime dependency**: `slv_` entities don't reference `raw_` tables in any executable way
7. ~80% code duplication between Bronze and Silver as copy-paste

---

## 5. Data Product Assistant vs Data-Workbench — Full Comparison Table

| Dimension | DPA (Bronze+Silver+Gold combined) | DATA-WORKBENCH |
|---|---|---|
| **Purpose** | Generate Bronze/Silver schema artifacts + identify data product coverage | Orchestrate full data product lifecycle source → marketplace |
| **Users** | Data architect, DE (Bronze/Silver); Business analyst (Gold) | Data Engineer + Data Product Owner |
| **Input** | NL bank description + NL requirement (Bronze/Silver); NL requirement only (Gold) | Live database connection OR offline extraction manifest |
| **Output** | DDL, ODCS contracts, STTM Excel, PDFs, Airflow DAGs, Dataplex manifests, DPI JSON | Virtual views / dbt / Parquet, ODCS contracts, Neo4j graph, DQ test suites, marketplace |
| **AI model** | Gemini 2.5 Flash (Bronze+Silver) + Claude Sonnet on Azure (Gold) | Claude throughout (Anthropic / Azure Foundry / CC OAuth) |
| **AI pattern** | Sequential 7-agent generation pipeline (Bronze/Silver); 4-agent DPI + Challenger (Gold) | ~44 skills, SDK stage-based; interactive mid-stage Q&A |
| **GCP surface** | Vertex AI, BigQuery, BigLake Iceberg, Dataplex KC, Cloud Run | GCS as one platform option; no native BQ/Dataplex/Vertex |
| **Azure surface** | Claude endpoint, App Insights, Azure AD (Gold) | Azure ADLS as storage option |
| **Databricks surface** | Unity Catalog (Gold — discovery + publish) | Discovery source + serving target |
| **State management** | In-memory/Redis session (ephemeral, per-conversation) | SQLite (relational) + Neo4j (knowledge graph) — persistent |
| **Deployment** | Cloud Run GCP (Bronze/Silver) + Azure App Service (Gold) | Docker Compose or GCP VM |
| **Platform scope** | GCP-native (Bronze/Silver) + Azure/Databricks (Gold) — NOT multi-platform | Postgres, MySQL, Snowflake, Databricks, Oracle, SQL Server, Parquet, GCS, S3, ADLS |
| **Data quality** | Schema standards validation; Silver DQ column schema (no rules generated) | Full DQ lifecycle: observation, domain, user, spec rules; GX/Pandera test suites; 8-dimension scoring |
| **Lineage** | STTM workbooks + Dataplex manifests (artifact-level) | Neo4j column-level (`:ColumnMapping`, `:CONSUMES`, PROV-O) |
| **Governance** | None — no approval gates, no review workflows, no RBAC | Full governance: PO validation gate, DE review, Steward escalation, PROV-O, role-based persona access |
| **Knowledge graph** | None | Neo4j (DCAT-2, DQV, SHACL, PROV-O, ODCS, DPROD ontologies) |
| **BFSI specificity** | Deep (banking_ontology, BIAN, ISO 20022, Basel III, T24/SWIFT/Flexcube/SAP) in Bronze+Silver | Domain-agnostic core with 47 banking ODCS templates in Blueprint Library |
| **Contract standard** | ODCS v2.2 (Bronze) / OpenDataContract 0.9.3 (Silver) / none (Gold) | ODCS v3.1.0 throughout |
| **Multi-layer arch** | 3 separate apps, manual handoff | dpe-sa → dpe-cf → marketplace (`:CONSUMES` DAG, single system) |
| **MCP/API surface** | REST API per layer (/v1/sessions, /chat) — no MCP | /mcp (142 DE tools) + /po-mcp (68 PO tools) |
| **Discovery** | Gold only (Unity Catalog / Databricks) | Connected Estate (live DB scan + offline); feasibility grading (ready/adaptable/assemblable/absent) |
| **Serving** | DDL execution only — creates tables | Virtual views (multi-dialect), dbt materialization, Parquet lakehouse |
| **Marketplace** | None | Neo4j-backed marketplace + Semantic Q&A (NL→SQL) + OSI scoring |
| **Shared library** | ~80% copy-paste between Bronze and Silver — no shared package | Fully integrated monorepo |

---

## 6. Complementary Capabilities — Full Analysis

### BRONZE: What workbench provides / what Bronze provides uniquely

**Workbench capabilities Bronze needs:**

| Workbench capability | Bronze gap |
|---|---|
| Connected Estate `evaluate_feasibility` | `source_scoping_agent` guesses tables from static `knowledge/source_systems_catalog.json` — no validation against what exists |
| `data-discovery-*` skills | No live DB profiling after tables are deployed |
| `data-quality-rule-generation` skill | Validator only checks schema standards, not data quality rules |
| ODCS v3.1.0 canonical format | Bronze generates v2.2 |
| Neo4j lineage graph | Dataplex manifest + STTM are artifacts, not queryable lineage |
| `ingest_odcs_spec` MCP tool | No import path into workbench |

**Unique Bronze capabilities workbench lacks:**

| Bronze capability | Workbench gap | File |
|---|---|---|
| Ingestion modality detection (SWIFT/mainframe/SAP/API/unstructured) | workbench discovers physical columns; doesn't classify ingestion pattern | `tools/schema_loader.py:classify_source_modality()` |
| Bronze envelope enforcement (`ingest_batch_id`/`ingest_ts`/`raw_payload_hash` mandatory on every raw table) | No concept of mandatory Bronze envelope | `common/ingestion-metadata.yaml` |
| Iceberg + Object Table DDL | workbench doesn't generate BigLake external table DDL | `agents/spec_generator_agent.py` |
| Dataplex KC registration | workbench publishes to Neo4j marketplace + git; no KC integration | `server.py:commit_data_contract()`, `tools/dataplex_tool.py` |
| Bank profile extraction | No institution-level profiling | `agents/bank_profile_agent.py` |
| STTM + PDF artifact generation | Markdown reports only | `tools/artifact_generator.py:generate_sttm_excel()`, `generate_bank_profile_pdf()` |

---

### SILVER: What workbench provides / what Silver provides uniquely

**Workbench capabilities Silver needs:**

| Workbench capability | Silver gap |
|---|---|
| `data-mapping-neo4j` skill | Silver's STTM references `raw_core.*` generically — not validated against actual Bronze columns |
| `column-name-standardizer` + PO validation gate | No human review gate on generated BIAN-conformant names |
| `data-serving-virtual-view` skill | Generates DDL for tables; doesn't generate the Bronze→Silver transformation view DDL |
| `data-quality-rule-generation` skill | `dq_status` column exists in schema but no rules to populate it are generated |
| Review/approval workflow | No validation gate — DDL is published without human review |
| Blueprint Library | Silver generates entities from scratch without reference to what Gold products need downstream |

**Unique Silver capabilities workbench lacks:**

| Silver capability | Workbench gap | File |
|---|---|---|
| SCD Type-2 physical common block (full envelope) | workbench supports SCD-2 in `:DatasetTransform` but doesn't generate the physical SCD2 columns on tables | `common/technical-metadata.yaml` Versioning sub-schema |
| Per-row DQ quarantine column (`dq_status` PASS/WARN/QUARANTINE) | workbench has 8-dimension quality scores but not per-row DQ status columns in the schema | `common/technical-metadata.yaml` DataQuality sub-schema |
| Regulatory column derivation (IFRS9/Basel III) | workbench domain rules don't know IFRS9/Basel III required columns | `agents/domain_scoping_agent.py:regulatory_columns_required` |
| Multi-money prefixing pattern | workbench handles column-level transforms but not multi-instance block prefixing | `prompts/silver_product_engine.md` |
| Domain framework API (`/catalog/domains`) | 6 banking domains with derived metrics (NIM, LTV, DTI, DAB) | `server.py:get_domain_registry()` |

---

### GOLD: What workbench provides / what Gold provides uniquely

**Workbench capabilities Gold needs:**

| Workbench capability | Gold gap |
|---|---|
| `evaluate_feasibility` + Connected Estate | Gold's Discovery Agent only queries Unity Catalog (Databricks); no GCP BigQuery estate support |
| Neo4j graph | Gold builds Data Product definitions as JSON files; no persistent knowledge graph |
| Blueprint Library | Gold classifies use cases but has no reference to standard Gold product templates |
| `create_consumer_product` + `match_inputs` | `design_queue.enriched` lists Gold products to build — no path to create them in a governed system |
| Multi-hop DAG (`:CONSUMES`) | Gold identifies which Silver tables feed a Gold product but doesn't enforce the DAG |
| Marketplace + Semantic Q&A | Gold produces a DPI JSON but no discoverable marketplace entry |

**Unique Gold capabilities workbench lacks:**

| Gold capability | Workbench gap | File |
|---|---|---|
| Use case type classification (6 types + schema design pattern) | workbench creates projects by archetype but doesn't classify business requirements | `agents/use_case_classification.py:VALID_USE_CASE_TYPES` |
| Challenger review (devil's advocate AI) | workbench has human PO validation but no AI Challenger evaluating internal DPI consistency | `agents/challenger.py` → `design_queue.curated` + `design_queue.enriched` |
| KPI derivation with anti-hallucination guard | workbench feasibility grades by schema matching; doesn't validate KPI formulaic derivability | `agents/kpi_derivation.py:validate()` |
| Business-facing clarification dialogue (8-question max, one-per-turn) | workbench has MCP tools for DE/PO but no conversational clarification for raw business requirements | `backend/.skills/requirements-agent.md` |
| Schema design pattern recommendation (star_schema/wide_flat/event_schema) | workbench creates source/consumer products but doesn't recommend physical schema patterns | `agents/use_case_classification.py:SCHEMA_DESIGN_PATTERNS` |

---

## 7. Agentic Stack Architecture — Revised Theory

### Verdict: DPA and data-workbench are complementary, not alternatives.

- **Bronze + Silver** are **GCP-native BFSI schema generators** — they generate artifacts but have no persistence, discovery, or governance
- **Gold** is a **cross-platform DPI tool** — it bridges business requirements to existing catalog coverage but generates nothing
- **data-workbench** is the **governed orchestration and serving platform** — it has discovery, governance, serving, and marketplace but lacks BFSI deep domain knowledge and GCP-native generation

More accurate layering:
- **Gold sits ALONGSIDE workbench** (both do discovery/planning; Gold is BFSI-specialized, workbench is multi-domain + governed)
- **Bronze/Silver sit BELOW workbench** (they generate raw inputs that workbench governs, maps, and serves)
- **data-workbench is the governed serving layer; DPA is the domain-expert generation layer that feeds it**

### Integration Architecture

```
╔══════════════════════════════════════════════════════════════════════╗
║                    BUSINESS REQUIREMENT                              ║
║       "Customer Risk Dashboard for retail banking"                   ║
╚══════════════════════════════════════════════════════════════════════╝
                              │
                              ▼
╔══════════════════════════════════════════════════════════════════════╗
║                 GOLD DPA — DPI + Discovery Layer                     ║
║  Requirements Clarification → Classification → Discovery             ║
║  Discovery queries: workbench /mcp (list_marketplace_products)       ║
║                   + workbench evaluate_feasibility()                 ║
║  Output: design_queue.curated  → [raw_t24_customer, slv_customer]    ║
║          design_queue.enriched → [gld_customer_360]                  ║
╚══════════════════════════════════════════════════════════════════════╝
        │ design_queue.curated                │ design_queue.enriched
        ▼                                     ▼
╔══════════════════════╗          ╔═══════════════════════════════════╗
║  BRONZE DPA          ║          ║  DATA-WORKBENCH (dpe-cf)          ║
║  raw_t24_customer    ║          ║  create_consumer_product(         ║
║  bank_profile →      ║          ║    template=agg-customer-360)     ║
║  bronze_engine →     ║          ║  match_inputs (Silver sources)    ║
║  BQ DDL + ODCS v2.2  ║─ingest──▶║  data-mapping-neo4j               ║
║  + Dataplex KC       ║  odcs    ║  data-serving-virtual-view        ║
╚══════════════════════╝          ║  → published Gold product         ║
                                  ║  → marketplace + Semantic Q&A     ║
╔══════════════════════╗          ╚═══════════════════════════════════╝
║  SILVER DPA          ║                       ▲
║  slv_customer        ║                       │ :CONSUMES edges
║  domain_scoping →    ║──ingest──▶────────────┘
║  silver_engine →     ║  odcs
║  BQ DDL + ODCS v0.9.3║
║  + Dataplex KC       ║
╚══════════════════════╝
                              ▼
╔══════════════════════════════════════════════════════════════════════╗
║                  DATA-WORKBENCH (shared backbone)                    ║
║  Neo4j graph (lineage, contracts, marketplace)                       ║
║  /mcp (142 DE tools) + /po-mcp (68 PO tools)                        ║
║  Blueprint Library (47 banking ODCS templates)                       ║
║  Connected Estate (feasibility: ready/adaptable/assemblable/absent)  ║
║  Marketplace (published products + Semantic Q&A NL→SQL)              ║
║  DQ lifecycle (observation rules → GX/Pandera testing → scoring)     ║
╚══════════════════════════════════════════════════════════════════════╝
```

### What Each System Owns Exclusively

| System | Exclusive ownership |
|---|---|
| **BRONZE DPA** | Bronze ingestion envelope (common/ blocks), modality detection (SWIFT/mainframe/SAP), Iceberg/Object Table DDL, BigLake registration, bank profile extraction, STTM/PDF artifacts |
| **SILVER DPA** | SCD Type-2 physical common block, per-row `dq_status` column, regulatory column derivation (IFRS9/Basel III), multi-money/identifier prefixing, banking domain framework API |
| **GOLD DPA** | Business clarification dialogue, use case type classification (6 types + schema pattern), Challenger review, KPI computability validation with anti-hallucination |
| **DATA-WORKBENCH** | Neo4j knowledge graph (persistent lineage), multi-persona governance (DE/PO/Steward/DQA), DQ test suites (GX/Pandera), marketplace + Semantic Q&A, Connected Estate feasibility grading, Blueprint Library (47 banking templates), MCP front doors (210 tools), multi-platform serving, `:CONSUMES` DAG enforcement |

### Exact Integration Seams

| Seam | Mechanism | Priority |
|---|---|---|
| **Bronze ODCS → workbench** | ODCS v2.2 → v3.1.0 shim → `POST /api/ingest-products/from-odcs` → creates `dpe-sa` project with Bronze tables as offline source | High |
| **Silver ODCS → workbench** | Same shim → `ingest_odcs_spec` MCP tool → creates `dpe-sa` project with Silver tables | High |
| **Gold discovery → workbench** | Gold's Discovery Agent calls `mcp__workbench__evaluate_feasibility` instead of (or in addition to) Unity Catalog | High |
| **Gold design_queue → workbench products** | `design_queue.enriched` item → `mcp__workbench__create_consumer_product` with Blueprint Library template | Medium |
| **Bronze source scoping → workbench estate** | Before Gemini call in `source_scoping_agent.py`, call `mcp__workbench__scan_estate` to ground table selection | Medium |
| **Banking ontology → workbench semantic layer** | Load `knowledge/banking_ontology.json` + `knowledge/bian_service_domains.json` as `BusinessConcept` nodes | Medium |
| **workbench BigQuery platform** | Add `platform/manifests/bigquery.yaml` + `platform/providers/bigquery.py` to workbench | High |
| **ODCS v2.2 / v0.9.3 → v3.1.0 migration** | Write `tools/odcs_bridge.py` shared utility in Bronze/Silver | High |

---

## 8. Recommended Build Plan

### Priority 1 — ODCS Bridge (1–2 days)
Write `banking-commons/tools/odcs_bridge.py` shared by Bronze and Silver:

```python
def upgrade_to_v3_1(contract_dict: dict) -> dict:
    # Converts Bronze v2.2 or Silver v0.9.3 → workbench v3.1.0
    # Map: dataContractSpecification → specVersion, models → schema.tables
```

Add `POST /v1/sessions/{id}/export-to-workbench` in both Bronze and Silver `server.py` that calls the bridge and POSTs to workbench's `ingest_odcs_spec`.

### Priority 2 — BigQuery Platform Provider for workbench (3–5 days)
Create `workbench/backend/platform/providers/bigquery.py`:
- Implement `QueryExecutor` using `google.cloud.bigquery`
- Implement `DeploymentProvider.deploy_view()` for `CREATE OR REPLACE VIEW` DDL
- Add `platform/manifests/bigquery.yaml` with connection config fields

**This is the most critical missing piece** — without it, workbench cannot discover, govern, or serve what Bronze and Silver build on GCP.

### Priority 3 — Extract Shared Banking Commons Library (1 sprint)
Create `banking-commons/` Python package shared by Bronze, Silver, and workbench:
- `banking_commons/knowledge/` — single source of truth for `banking_ontology.json`, `bian_service_domains.json`
- `banking_commons/common_blocks/` — Bronze and Silver `common/` YAML files
- `banking_commons/session_store.py`, `artifact_generator.py`, `bigquery_tool.py`

Install as `pip install -e ./banking-commons` in all three apps — eliminates the ~80% copy-paste debt.

### Priority 4 — Ground Bronze/Silver Source Scoping in workbench Estate (1–2 days)
In `BRONZE DATA PRODUCT/agents/source_scoping_agent.py` and `SILVER DATA PRODUCT/agents/domain_scoping_agent.py`, before the Gemini call, call workbench's `evaluate_feasibility` to ground table selection in live estate reality instead of the static `knowledge/source_systems_catalog.json`.

### Priority 5 — Banking Ontology → workbench Semantic Layer (1–2 days)
Write a one-time migration script that reads `knowledge/banking_ontology.json` + `knowledge/bian_service_domains.json` and creates `BusinessConcept` entity/attribute nodes in workbench's Neo4j graph. Gives workbench's NL→SQL Semantic Q&A BFSI vocabulary out of the box.

### Priority 6 — Gold Discovery → workbench Feasibility (1–2 days)
In Gold `agents/orchestrator.py` Step 3 (Discovery), add a workbench feasibility call alongside the Unity Catalog scan. Merge results: workbench for GCP BigQuery tables, Unity Catalog for Databricks tables.

### Priority 7 — Gold `design_queue` → workbench Product Creation (1–2 days)
In Gold `agents/challenger.py:run()`, after producing `design_queue.enriched`, for each item call `mcp__workbench__create_consumer_product` with the matching Blueprint Library template (e.g., `agg-customer-banking-360.yaml`).

### Priority 8 — Unified Auth Model (1 sprint)
Adopt workbench's JWT + MCP bearer token model across Bronze and Silver. Register Bronze/Silver sessions as `AppUser` in workbench SQLite. Share `WB_MCP_TOKENS` bearer token for DPA layers to call workbench MCP tools.

### Priority 9 — End-to-End Validation (1 sprint)
Run a complete BFSI scenario end-to-end:
1. Run Gold on "Customer Risk Dashboard for Retail Banking" → `design_queue`
2. Gold calls workbench `evaluate_feasibility` → stoplight for target products
3. Gold calls workbench `create_consumer_product` for `gld_customer_360` (Blueprint Library `retail-banking/agg-customer-banking-360.yaml`)
4. Run Bronze for `raw_t24_customer` → publish to BQ → call workbench `ingest_odcs_spec`
5. Run Silver for `slv_customer` → publish to BQ → call workbench `ingest_odcs_spec`
6. workbench `data-discovery-bigquery` profiles Bronze/Silver tables
7. workbench PO maps Silver columns → Gold product columns via `dpe-cf` mapping
8. workbench `data-serving-virtual-view` emits BQ view DDL for Gold product
9. workbench marketplace lists Gold product; Semantic Q&A answers "How many high-risk customers are in the UK?"

---

## Key Finding

The three DPA layers and data-workbench were built independently but the architecture is convergent. The two largest missing pieces are:

1. **BigQuery platform provider in workbench** (Priority 2) — without it, workbench cannot discover, govern, or serve what Bronze and Silver build on GCP
2. **ODCS version bridge** (Priority 1) — Bronze is v2.2, Silver is v0.9.3, workbench expects v3.1.0

Once those two bridges exist, the ODCS hand-off connects the two systems and the rest of the integration falls into place naturally through the 210 MCP tools workbench already exposes.
