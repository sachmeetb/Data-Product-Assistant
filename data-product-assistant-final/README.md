# data-product-assistant-final

A cross-domain, agentic data product assistant on GCP. Consolidates two
predecessor products — a CPG/campaign-oriented **GOLD** assistant and a BFSI
**SILVER** assistant — onto one canonical model, with two capabilities neither
had: **structured editing with propagation**, and **bronze as a produced layer**.

## Status

Scaffolding. The core model, edit engine, flow graph, dependency DAG, pack
loader, pipeline IR, ODCS export and Knowledge Catalog publish planner are
implemented and tested. The agents, API, frontend and renderers are stubs with
stated contracts.

```
127 passed
```

## Why this exists

Both predecessors worked, and both had the same structural problems: control
lived in a ~1400-line `if/elif` chain, production state was in-process memory,
validation was self-reported by the model rather than evaluated, artifacts were
read-only, and hardcoded demo fallbacks were presented to users as generated
output. See [docs/03-common-points-and-seams.md](docs/03-common-points-and-seams.md).

## The spine: Domain → Table → Attribute

One model, read and written by every agent. It is also the edit surface and the
API shape.

```
DataProductDesign
 └── Domain[]        name, industry, standards[], pack_ref, + pack enrichments
      └── Table[]    layer (BRONZE|SILVER|GOLD), entity_type, grain, physical_ref
           └── Attribute[]  canonical_type, nullable, is_pk, fk_ref,
                            provenance, block, standard_refs[], source_ref
```

Three properties make this different from what came before:

**Canonical types.** Nothing stores an engine-native type. The inherited domain
frameworks declared `DOUBLE` — a Spark type, invalid in BigQuery — which is
exactly the failure platform-agnostic rendering has to prevent.

**Provenance on every field.** Each attribute records its origin
(`PACK_ENTITY`, `PACK_BLOCK`, `LLM`, `HUMAN`, `LIVE_CATALOG`) and whether it is
locked. Regeneration preserves protected fields and records what it wanted to
change as a withheld suggestion, rather than silently overwriting user work.

**The catalog is an input.** The designed tree is reconciled against live
BigQuery, so "this column exists but you didn't include it" is a feature.

## Editing

Three levels, matching the spine:

| Level | Add sources |
|---|---|
| Domain | pack framework · blank |
| Table | pack entity · **live catalog table** · block composition |
| Attribute | **live catalog column** · pack block · pack entity · user-defined |

`core/catalog/reconcile.py` diffs design against catalog and returns a `Delta`
of addable columns, addable tables, addable domains, type mismatches, and things
the design declares that do not exist yet. Applying an addable column lands it
as `LIVE_CATALOG` + locked, so the next regeneration cannot clobber it.

`core/artifacts/graph.py` holds the dependency DAG, so an edit invalidates only
what is downstream of it — editing the design tree marks the STTM, IR, SQL and
publish stale, and leaves discovery and the requirement fresh.

## Domain packs

12 domains across 5 industries, loaded at runtime. **No domain logic in core,
prompts or UI.**

```
packs/
├── registry.json          industries → domains → {path, detection_signals, standards}
├── _blocks/               10 universal JSON Schema attribute blocks
├── cpg/                   campaign, sales, loyalty, trade_promotions, pricing, digital_commerce
├── banking/               retail_banking, wealth_management (+ knowledge/: BIAN, ontology, crosswalk)
├── healthcare/            patient_journey
├── retail/                merchandise_planning
└── cross_industry/        supply_chain, finance
```

84 tables and 512 attributes total. Two inherited framework formats are both
supported: the unversioned CPG packs (which carry richer `derived_metrics`,
`source_platform_mappings` and value normalisation) and the `schema_version: 2.0`
subdirectory packs. The loader normalises both and preserves the extras.

The 10 blocks — money, identifier, temporal, postal-address, contact-point,
quantity, rate, code-value, party-name, technical-metadata — came from the BFSI
product but are industry-neutral, so they live in core rather than in the
banking pack. They are JSON Schema draft 2020-12 with `$ref`/`$defs` resolution
and an `x-canonical-type` escape hatch for cases where the wire format differs
from the warehouse type (`money.amount` is a string to preserve decimal
precision).

## Three representations, one job each

The bespoke `utility_catalog` JSON both predecessors carried is gone as a
published format.

| | Format | Job |
|---|---|---|
| Design-time | the canonical tree | edit, provenance, layers, lineage, pre-materialisation design |
| GCP-native | **Knowledge Catalog data product** | governance, discovery, access control |
| Buyer-facing | **ODCS v3.2.0** | the deliverable the purchasing organisation ingests directly |

Knowledge Catalog (Dataplex Universal Catalog, renamed April 2026) models a data
product as a grouping of assets that *already exist*, plus governance metadata.
It has no notion of a pre-materialisation design, a bronze→silver→gold mapping,
or per-field edit provenance — so publishing is a post-materialisation step and
the design-time model stays ours. `plan_publish` enforces the documented limits
up front: max 50 assets per product, same-location assets, nothing published
before it is materialised, and bronze never published at all.

ODCS is the export because the buyer ingests it directly and may not be on GCP;
Knowledge Catalog's own `contract` aspect is GCP-shaped and cannot travel. The
mapping turns out near-lossless — ODCS carries the STTM natively as
`transformSourceObjects` / `transformLogic` / `transformDescription`, the
BIAN/FIBO/ISO refs as `authoritativeDefinitions`, and our canonical-vs-rendered
type split as `logicalType` / `physicalType`.

See [docs/04 §7](docs/04-target-architecture.md) for the full mapping.

## Pipelines are platform-agnostic

Both predecessors had an LLM emit BigQuery SQL directly, which is why GOLD
needed `_rewrite_sql_to_bq` to undo Databricks table references after the fact.
Here agents emit a declarative IR — sources, targets, typed steps, DQ rules,
attribute-level lineage — and renderers translate it per engine. No SQL appears
in `pipelines/ir.py`.

Renderer order: BigQuery/Dataform → Airflow → dbt → Spark.

## Layout

```
core/         domain-agnostic engine — no domain logic, ever
  model/      tree · provenance · canonical types
  catalog/    live BQ introspection · reconciler (the edit engine)
  artifacts/  versioned store · dependency DAG · typed patches
  flow/       declarative graph — guarded edges, no fallthrough
  contracts/  ODCS v3.2.0 export (buyer-facing)
  publish/    Knowledge Catalog data product (GCP-native)
  llm/        agent runtime            [stub]
  validation/ Python rule engine      [stub]
  state/      Firestore session store [stub]
packs/        domain packs — data only
agents/       pipeline agents         [stub]
bronze/       envelope · synth · ingest
pipelines/    IR + renderers
api/          FastAPI surface         [stub]
tests/
```

## Getting started

```bash
pip install -e ".[dev]"
pytest tests -q
```

Live catalog reconciliation needs `google-cloud-bigquery` and credentials; the
rest of the core runs offline. `core/catalog/live.InMemoryCatalog` is the test
double.

## Documentation

| Doc | Contents |
|---|---|
| [01-current-gold-flow.md](docs/01-current-gold-flow.md) | GOLD as-implemented: 13 stages, dead code, landmines |
| [02-current-silver-flow.md](docs/02-current-silver-flow.md) | SILVER as-implemented: 6 stages, knowledge layer, scaffolding |
| [03-common-points-and-seams.md](docs/03-common-points-and-seams.md) | Capability matrix, shared pathologies, what to port/rebuild/drop |
| [04-target-architecture.md](docs/04-target-architecture.md) | This design, and the decisions behind it |
| [05-agentic-architecture.md](docs/05-agentic-architecture.md) | Diagrams: every agent, colour-coded by whether it comes from GOLD, SILVER, both, or is new |
| [06-roadmap.md](docs/06-roadmap.md) | Phased build order |

## Two things to action outside this project

- `GOLD DATA PRODUCT/.../backend/.env` contains live-looking `ANTHROPIC_API_KEY`,
  `AZURE_CLIENT_SECRET` and `DATABRICKS_TOKEN` committed beside source. **Rotate them.**
- `SILVER DATA PRODUCT/tools/gcp_auth.py:29` disables SSL verification
  process-wide, in code that ships to Cloud Run. Not carried into this project.
