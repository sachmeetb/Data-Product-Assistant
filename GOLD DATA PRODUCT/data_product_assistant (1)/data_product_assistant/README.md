# AI Retail Data Agent

An AI-powered pipeline that translates STTM (Source-to-Target Mapping) and data contracts into production-ready Databricks pipeline code, validates it, and publishes the resulting tables to the Databricks Unity Catalog.

---

## Architecture

All user interaction flows through the **Orchestrator**. The Orchestrator holds state and drives three specialist agents internally — users never communicate with sub-agents directly.

```
User
 │
 ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Orchestrator Agent                        │
│  • Single user-facing interface                                  │
│  • Collects pipeline spec (STTM, contracts, table definitions)   │
│  • Holds full pipeline state                                     │
│  • Drives sub-agents and reports results to user                 │
└─────────────────────────────────────────────────────────────────┘
         │              ▲              ▲              ▲
         │ spec         │ pass         │ feedback     │ publish
         ▼              │              │              │ report
┌──────────────┐   ┌──────────┐       │        ┌─────────────┐
│   Pipeline   │──▶│  Test    │───────┘        │  Publisher  │
│  Generator   │   │  Agent   │── pass ───────▶│   Agent     │
└──────────────┘   └──────────┘                └─────────────┘
  Generates DLT /    Validates:                  Writes tables
  Spark code for     schema, quality,            to Databricks
  Bronze→Silver→Gold DLT expectations,           Unity Catalog
  Medallion arch.    referential integrity.
                     Loops up to 5×.
```

---

## Agents

### Orchestrator
The sole entry point for users. Collects the pipeline specification (STTM, source/target schemas, data contracts, domain, pipeline type), drives the internal agent pipeline, and communicates all results back in plain language. Maintains full pipeline state across the session.

### Pipeline Generator
Translates approved silver/gold table specifications, STTM (Source-to-Target Mapping), and data contracts into executable Databricks pipeline code. Produces DLT (Delta Live Tables) notebooks, Spark batch jobs, and orchestration DAGs that move data through Bronze → Silver → Gold per the Medallion architecture. Code is production-grade, parameterized for environment promotion, and targets the Databricks Unity Catalog.

### Test Agent
Generates the validation suite for the Pipeline Generator's output: schema conformance tests against approved silver/gold schemas, row-count reconciliation between layers, data quality checks against the contract, DLT expectation behaviour, referential integrity checks, and business rule assertions derived from the requirement. Produces a structured `TestReport` that determines whether the pipeline is fit to publish. If validation fails, the report is sent back to the Pipeline Generator with detailed fix guidance (loop repeats up to 5 times).

### Publisher Agent
Receives validated code (code that passed all Test Agent checks) and writes the utility catalog tables into Databricks Unity Catalog. Registers table definitions (DDL), applies domain/classification/ownership tags, configures job schedules aligned to the data contract SLA, and makes the data product discoverable for downstream consumers.

---

## Pipeline Flow

```
1. User provides STTM / data contract / table specs
2. Orchestrator validates and extracts structured spec
3. Pipeline Generator → generates PySpark/DLT code
4. Test Agent → validates code
   ├─ FAIL → sends failure report + code back to Pipeline Generator
   │          (repeats until pass or 5 iterations reached)
   └─ PASS → Orchestrator forwards code to Publisher Agent
5. Publisher Agent → writes tables to Databricks Unity Catalog
6. Orchestrator → reports published tables and tags to user
```

---

## Getting Started

### Prerequisites
- Python 3.11+
- Access to an Azure-hosted Claude endpoint (via Azure AI Services)
- Databricks workspace with Unity Catalog enabled

### 1. Clone & install dependencies

```bash
git clone https://aiRetailAnalytics@dev.azure.com/aiRetailAnalytics/aiRetailAnalytics/_git/airetail_data_agent
cd airetail_data_agent/backend
pip install -r requirements.txt
```

### 2. Configure environment

Create `backend/.env`:

```env
# Anthropic SDK — Azure-hosted Claude
ANTHROPIC_ENDPOINT=https://<your-azure-endpoint>/anthropic/
ANTHROPIC_API_KEY=<your-azure-api-key>
ANTHROPIC_MODEL=claude-sonnet-4-6

# Azure identity (for Unity Catalog / monitoring)
AZURE_CLIENT_ID=<service-principal-client-id>
AZURE_TENANT_ID=<tenant-id>
AZURE_CLIENT_SECRET=<client-secret>

# Application Insights (optional — for telemetry)
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...
```

### 3. Run the backend

```bash
cd backend
python main.py
# or: uvicorn server:app --reload --port 8000
```

### 4. Run the CLI pipeline runner (no frontend)

```bash
cd backend
python pipeline.py
```

---

## API Reference

### `POST /chat`
Unified conversational endpoint. All pipeline interaction happens here.

**Request**
```json
{
  "session_id": "optional-uuid",
  "message": "I need a DLT pipeline from bronze.raw_orders to silver.orders_clean..."
}
```

**Response**
```json
{
  "session_id": "uuid",
  "text": "I need a few more details before I can start...",
  "chips": ["Confirm", "Edit"],
  "is_complete": false
}
```

### `GET /health`
Returns API status and registered agents.

---

## What to Provide to the Orchestrator

| Field | Description | Example |
|-------|-------------|---------|
| `source_tables` | Bronze/raw table names and schema | `catalog.bronze.raw_orders` with columns |
| `target_tables` | Silver/Gold table definitions | `catalog.silver.orders_clean` with types and constraints |
| `sttm` | Source → target column mappings with transformations | `order_id → order_id CAST AS BIGINT` |
| `data_contract` | Quality rules, SLA, row-count expectations | `not_null: order_id`, `daily` freshness |
| `domain` | Business domain | `retail`, `finance`, `supply-chain` |
| `pipeline_type` | Output format | `dlt`, `batch`, or `dag` |

---

## Project Structure

```
airetail_data_agent/
├── backend/
│   ├── main.py                      # Entry point (starts Uvicorn)
│   ├── server.py                    # FastAPI app — /chat and /health endpoints
│   ├── pipeline.py                  # CLI runner for the full pipeline
│   ├── requirements.txt
│   ├── agents/
│   │   ├── base.py                  # Anthropic SDK agent runner (with prompt caching)
│   │   ├── orchestrator.py          # User-facing orchestrator
│   │   ├── pipeline_generator.py    # Code generation agent
│   │   ├── test_agent.py            # Code validation agent
│   │   └── publisher_agent.py       # Unity Catalog publisher agent
│   └── prompts/
│       ├── orchestrator/SKILL.md
│       ├── pipeline-generator/SKILL.md
│       ├── test-agent/SKILL.md
│       └── publisher/SKILL.md
└── startup.sh                       # Gunicorn startup for Azure App Service
```

---

## Telemetry

Application Insights telemetry is enabled when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set. Each agent call emits a span with `agent.skill`, `agent.model`, `agent.input_tokens`, and `agent.output_tokens` attributes. The pipeline runner emits a root `pipeline-run` span with `pipeline.thread_id`.

---

## Deployment (Azure App Service)

The included `startup.sh` runs Gunicorn with Uvicorn workers:

```bash
gunicorn -w 4 -k uvicorn.workers.UvicornWorker server:app --bind 0.0.0.0:8000
```

Set all `.env` values as App Service application settings before deploying.
