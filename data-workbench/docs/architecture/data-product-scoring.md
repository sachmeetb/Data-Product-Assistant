# Data Product Scoring

## Executive summary

The Data Workbench answers two different scoring questions, and it's important to keep them apart because consumers and producers care about each one for different reasons.

**1. Quality Scoring — "Is the data healthy?"**
Operational metric on the raw, source-aligned content of a project's tables. Computed by walking the project's enriched knowledge graph (schema + profiling + rules + test results + descriptions) and emitting a multi-dimensional 0–100% score at the column, dataset, and catalog level. Used by the Data Quality Analyst and Data Engineer during pipeline work to triage what to fix, and by the marketplace consumer to glance at a freshness/health indicator before subscribing. Evidence-graded (test > profile > rule > meta) and tiered (Tier 1 internal consistency → Tier 2 shared meaning → Tier 3 external grounding) so a consumer can tell whether the score is backed by real tests, profiling statistics, or just the presence of rules.

**2. OSI Scoring — "Is the product semantically consumable?"**
Readiness metric on a published data product — independent of the underlying data quality. OSI is the **Open Semantic Interchange** spec — a vendor-neutral standard (vendored at v0.1.1 in `playbook/osi/`, upstream at [github.com/open-semantic-interchange/OSI](https://github.com/open-semantic-interchange/OSI)) that asks whether the product's *contract* carries enough semantic information that a downstream BI or AI consumer can understand and use it without back-channelling the producer: do the datasets have descriptions, do the fields have rich expressions instead of bare passthroughs, are primary keys and relationships declared, are there metrics and an AI-context block? Produces a single product-level Red/Amber/Green band plus a per-criterion checklist with human-readable "what to fix" prose. Used by the Data Product Owner during authoring (wizard Step 7 — "Readiness Review") and by the marketplace consumer as a one-glance "is this thing safe to consume" badge.

**Why both matter.** A product can have green quality scores (clean data) but a red OSI band (no descriptions, no relationships, opaque expressions) — meaning the data is fine but unusable for anyone who isn't already in the team. Conversely, a product can have a green OSI band but a poor Validity dimension — meaning the contract is well-documented but the underlying source has data integrity issues. The two systems are deliberately independent and surface in different places so neither hides the other.

**Where they live.** Both write append-only nodes into Neo4j (`:QualityScore` for quality, `:OsiEvaluation` for OSI) keyed by a `batchId`, so historical batches are retained for trend tracking and version-to-version comparison. Both are project-scoped via the standard `:Project {projectCode}` entry point. Quality Scoring is a Claude-Code-skill (graph-in / graph-out, no LLM in the inner loop); OSI Scoring is pure Python with an optional LLM advisor for narrative and Apply-card suggestions.

---

## 1. Quality Scoring (`data-scoring` skill → `:QualityScore` nodes)

**What it answers:** "How healthy is the underlying data?" — operational, evidence-graded, per-column / per-dataset / per-catalog.

### How it's invoked

- Stage `data_scoring` in `workbench/backend/archetypes.py:311-327` — `requires_llm=True`, owner role **Data Quality Analyst**. The prompt template tells the SDK to load the `data-scoring` skill and run it with `--project-code {project_code}`.
- Skill lives at `workbench-skills/skills/data-scoring/` (`SKILL.md` + `scripts/generate_scores_cypher.py` + `scripts/run_scores_cypher.py`). Pure graph in / graph out — no LLM in the loop, just deterministic Cypher.
- Default position: in the DQ workflow group, after `data_rule_generation`, before `data_remediation_planning` (`archetypes.py:725, 742`).

### Inputs (what it reads from the graph)

The skill requires three earlier skills to have already enriched the graph:

| Input | Source | Used for |
|---|---|---|
| `:Dataset` / `:Column` w/ `row_count`, `dataType`, `nullable` | `data-discovery-to-dcat-neo4j` | Schema conformance + grain |
| `:QualityMeasurement` (`null_rate`, `distinct_count`, `allowedValues.coverage`, ranges) + `:TopValue` | `data-profiling-to-dqv-neo4j` | Completeness, Uniqueness, Validity (profile fallback) |
| `:PropertyShape` (rules) w/ `ruleSource`, `status` | `data-quality-rule-generation` + domain-rule enhancement | Rule Coverage, Grounding |
| `:TestResult` pass rates (optional) | `data-quality-testing-gx` | Validity, Consistency (preferred over profile evidence) |
| `:ColumnDescription` w/ `status` | `metadata-enrichment` | Documentation |

Scoping: every query enters through `(:Project {projectCode: $project_code})-[:HAS_CATALOG]->(:Catalog)-[:DCAT_DATASET]->(ds:Dataset)`, so it never crosses projects.

### The eight dimensions and weights

From `SKILL.md` and mirrored in `QualityScorePanel.tsx:53-62`:

| Dimension | Weight | Per-column formula |
|---|---|---|
| Completeness | 0.20 | `1.0 - null_rate` (0 if not profiled) |
| Uniqueness | 0.16 | distinct/row for PK/unique cols; 1.0 for low-cardinality or non-unique |
| Validity | 0.16 | latest `:TestResult` pass rate → else `allowedValues.coverage` → else 1.0 |
| Consistency | 0.10 | RI test pass rate → 1.0 if RI rule exists → 0.5 if FK but no rule → 1.0 if not FK |
| Schema Conformance | 0.08 | 0.333 each for hasDataType / hasNullable / hasProfiling |
| Rule Coverage | 0.08 | 1.0 if column has any `:PropertyShape`, else 0.0 |
| Documentation | 0.12 | 1.0 approved / 0.35 pending / 0.10 rejected / 0.0 none |
| Grounding | 0.10 | fraction of rules with `ruleSource ∈ {domain, external}` AND `status='approved'` |

Composite at every level = weighted average of dimension scores. Weights sum to 1.0.

### Tier badge (UI summary)

Computed at read time in `workbench/backend/routers/scoring.py:154-163`:

- **Tier 3** if any `grounding > 0` (externally-grounded approved rules exist)
- **Tier 2** if any `documentation > 0` (approved column descriptions exist)
- **Tier 1** baseline (profile / rule evidence only)

### Outputs (what it writes)

For each scoring run, a single `batchId` (`"<unix-millis>-<hex-random>"`) covers the whole batch. The script produces three levels of `:QualityScore` nodes:

```cypher
CREATE (qs:QualityScore {
  uri, level, dimension, score, weight, evidence, batchId, scoredAt
})
```

- `level ∈ {column, dataset, overall}`
- `dimension ∈ {completeness, uniqueness, validity, consistency,
   schema_conformance, rule_coverage, documentation, grounding, composite}`
- `evidence ∈ {test, profile, rule, meta}` — drives the badge on the dashboard so you can tell what's test-graded vs. profile-graded vs. just rule-presence

Relationships:
- `(:Column|:Dataset|:Catalog)-[:HAS_QUALITY_SCORE]->(:QualityScore)`
- `(:QualityScore)-[:SCORED_ON_DIMENSION]->(:QualityDimension)` (idempotent MERGE of the 8 dimension definitions, weights stored as `defaultWeight`)

Old batches are never deleted — that's the trend table.

### How it surfaces

Backend (`workbench/backend/routers/scoring.py`):
- `GET /api/projects/{id}/scoring` — latest batch, overall + per-dataset pivot, tier
- `GET /api/projects/{id}/scoring/dataset/{dataset_uri}` — per-column pivot for one dataset
- `GET /api/projects/{id}/scoring/trend` — historical composite per batch

Frontend: `QualityScorePanel.tsx` (gauge + dimension bars + dataset table + trend) mounted by the `quality_score` stat card on `ProjectDashboard.tsx:621-628`. Hidden on `dpe-cf` (consumer products don't profile raw data).

---

## 2. OSI Scoring — Open Semantic Interchange (`:OsiEvaluation` nodes)

**What it answers:** "Is this product semantically interchangeable / consumer-ready?" — single product-level band (red / amber / green).

### What OSI is and what the score represents

OSI = **Open Semantic Interchange**, an Apache-2.0 / CC-BY licensed specification published at [github.com/open-semantic-interchange/OSI](https://github.com/open-semantic-interchange/OSI) and vendored locally at `playbook/osi/spec_v0.1.1.md` + `playbook/osi/osi-schema.json`. Its premise is simple: if every producer publishes their semantic model in the same shape, BI tools, AI agents, and downstream pipelines can consume it directly instead of doing per-publisher translation. A semantic model in OSI's sense is the contract layer — dataset names + descriptions, field names + descriptions + SQL expressions, primary keys, relationships (FKs), business metrics (KPIs as SQL), and an `ai_context` block of synonyms / examples / instructions for AI agents.

The OSI score is therefore **not a data-quality score**. It does not look at null rates, value distributions, or test pass rates. It looks only at how well-described and self-explanatory the contract is. A product can have pristine data and a red OSI score (the data is fine but consumers can't tell what any of it means); a product can have green OSI and broken data (the contract is well-described but the underlying source is a mess).

The band thresholds (`GREEN_THRESHOLD`/`AMBER_THRESHOLD` constants at `workbench/backend/osi.py:76-77`; band assignment in `score_rubric()` ~`osi.py:1200`):
- **Red** — `< 50%` OR validation errors. The OSI document either doesn't satisfy the v0.1.1 schema, or it does but is too sparsely populated to be useful to a downstream consumer.
- **Amber** — `50%-79%` AND conformance passes. The product is workable but several OSI fields are missing — consumers will need to ask follow-up questions.
- **Green** — `≥ 80%` AND conformance passes. The product is semantically self-describing enough that a competent consumer (or AI agent) can use it without back-channelling the producer.

### The 7 checklist items, what each measures, and how to improve each

The OSI weights live in `WEIGHTS` (`workbench/backend/osi.py:65`) and are mirrored in the rubric YAML `playbook/scoring_rubrics/osi.yaml`. After the predicate/rubric refactor the per-criterion logic is no longer one inline block — each criterion resolves to a `PREDICATE_REGISTRY` callable dispatched by `score_rubric()` (`osi.py:1127`). Items, their weights, what they measure, and what raises each (split source-aligned vs consumer-aligned where the remediation path differs):

| # | Criterion | Weight | What "pass" means | How a source product (`dpe-sa`) raises it | How a consumer product (`dpe-cf`) raises it |
|---|---|---|---|---|---|
| 1 | **Dataset descriptions** | 15 | All datasets have a non-empty `description` | PO approves `:TableDescription` text in Reviews → Tables (`SourceProductValidationPanel`); propagated to `:DProdOutputDataset.description` during materialization | Authored in wizard Step 5 ("Product Details") — the dataset-name field carries through |
| 2 | **Field descriptions** | 25 | All fields have a non-empty `description` | PO approves `:ColumnDescription` rows in Reviews → Descriptions; auto-generated by `metadata-enrichment` skill, PO confirms | Same approval flow on `:DProdColumn` descriptions for the consumer product; chat "Guide me" can auto-fill |
| 3 | **Primary keys** | 10 | All datasets declare at least one PK | Catalog `:Column.primaryKey` flags propagate to `:DProdColumn.isPrimaryKey` during `synthesize_odcs_from_graph` | PO authors PK selection in wizard Step 4 ("Shape the Schema") |
| 4 | **Rich field expressions** | 20 | Every field has a non-trivial SQL expression (not a bare column reference) | **Often stays at fail for sources** — source products are 1:1 catalog passthroughs by construction; CAST / CONCAT / literal transforms only appear when an engineer has authored `:ColumnMapping` rows. Treat this slot as structurally low for sources, not a remediation. | Engineer authors `:ColumnMapping` rows in the `data_mapping` stage with explicit transform kinds (`cast`, `concat`, `literal`, `case`, `arithmetic`, `lookup`, ...) |
| 5 | **Relationships** | 10 | At least one FK declared (N/A if single dataset) | For multi-dataset sources, `_generate_dprod` mirrors catalog `:REFERENCES` edges onto `:DProdOutputDataset`. If discovery missed FKs, declare them via `:DataContractSchema.foreignKeys` | PO declares relationships in wizard Step 4, or accepts an `osi_relationship_create` Apply card from the OSI advisor |
| 6 | **Metrics** | 10 | At least one OSI metric exists (binary) | Use chat "Help me improve my OSI score" — advisor proposes 1-3 `:OsiMetric` candidates from your schema | Same chat flow; metrics are common KPIs (sum / count / ratio) expressed as SQL against the product's deployed view |
| 7 | **AI context** | 10 | `:DataContract.aiContextJson` is populated (binary) | Use chat → accept an `osi_ai_context_set` Apply card with synonyms / sample questions / agent instructions | Same chat flow |

Mechanics:
- Per-criterion `status ∈ {pass, partial, fail, na}`. Partial credit is proportional to the fraction satisfied (e.g. 3 of 4 datasets having descriptions earns 75% of the slot's weight).
- N/A items (e.g. Relationships when there's only one dataset) are **removed from the denominator** so they don't penalise — the remaining weights re-normalise to 100.
- Completeness = `round((Σ proportion × weight) / Σ active_weights × 100)`.

### Why source-aligned products typically sit at ~50%

A freshly-published source-aligned product that has gone through the full happy-path pipeline — discovery → metadata-enrichment → column-name standardization → PO validation (names + descriptions + tables + rules) → ODCS synthesis + auto-mapping — earns the first three slots automatically:

| Slot | Earned | Source of the credit |
|---|---|---|
| Dataset descriptions | **15** | PO-approved `:TableDescription` text propagated to `:DProdOutputDataset.description` |
| Field descriptions | **25** | PO-approved `:ColumnDescription` rows propagated to `:DProdColumn.description` |
| Primary keys | **10** | Catalog `:Column.primaryKey` flags carried into `:DProdColumn.isPrimaryKey` |
| Rich field expressions | 0 | Source products are 1:1 passthroughs — no `:ColumnMapping` rows authored yet |
| Relationships | 0 or N/A | Multi-dataset sources need FK discovery; single-dataset sources are N/A (slot removed from denominator) |
| Metrics | 0 | No `:OsiMetric` nodes until advisor / PO authors them |
| AI context | 0 | No `:DataContract.aiContextJson` until advisor / PO sets it |
| **Total** | **~50** | (of 100 active weights, or ~55-56% if Relationships is N/A) |

**This is by design, not a bug.** Source-aligned products are data-healthy and well-named but semantically opaque — until someone declares the metrics they support and the AI-agent context for using them, a downstream BI tool or LLM agent can't act on them without back-channelling the producer. The 50% floor is the system saying "the foundations are solid; the semantic-interchange layer hasn't been authored yet."

When this 50% floor is fine vs when to climb it:
- **Source product feeds only internal consumer-aligned products** → 50% Amber is acceptable. Consumers will declare their own metrics + ai_context on top of this source.
- **Source product is published directly to BI tools / AI agents** → climb to Green by authoring at least one metric + an `ai_context` block, and (for multi-dataset products) declaring at least one relationship. Use the chat "Guide me" affordance — the OSI advisor will propose plausible candidates from the schema.

### How it's invoked

- `POST /api/projects/{id}/osi/evaluate` (`workbench/backend/routers/osi.py:230-292`), body `{trigger, skip_advisor}`. Triggers: `manual` (marketplace button + wizard step), `submit` (wizard finalize), `signoff` (later gate).
- Marketplace "Score OSI now" button on the product detail header passes `skip_advisor=true` — pure deterministic scoring, no LLM.
- Wizard Step 7 ("Readiness Review") in `NewProductWizard.tsx` calls it with the advisor enabled, producing the narrative + Apply cards alongside the deterministic checklist.

### The pipeline (5 steps, all in `workbench/backend/osi.py`)

```
translate_to_osi() → validate_osi() → score_rubric() → [_run_osi_advisor()] → persist_evaluation()
```

> **Refactor note (predicate/rubric).** `score_osi(osi_dict, errors)` is now a **back-compat shim** (`osi.py:1218`) that calls the general `score_rubric()` (`osi.py:1127`) with the default OSI rubric. `score_rubric()` iterates the rubric's `criteria`, dispatches each predicate through `PREDICATE_REGISTRY`, and does the band assignment (~`osi.py:1200`) from the YAML's `band_thresholds` (defaulting to `GREEN_THRESHOLD`/`AMBER_THRESHOLD`). The line citations in the rest of this section predate that refactor — trust the symbol names, not the exact line ranges. See §3 below for the selectable-rubric (Design B) model.

#### Step 1 — `translate_to_osi()` (osi.py:336-539): build the OSI v0.1.1 dict

Reads from the **product graph** for one `:DataContract` (current version):

| Cypher constant | What it pulls |
|---|---|
| `READ_CONTRACT_HEAD` | `dc.name`, `dc.description`, `dc.purpose`, `dc.aiContextJson`, `dc.currentVersion` |
| `READ_DPROD_DATASETS` | `:DProdOutputDataset` (name, physicalName, description) + `:DProdColumn` (name, dataType, description, transformHint, primaryKey) |
| `READ_SCHEMA_FOREIGN_KEYS` | `:DataContractSchema.foreignKeys` JSON → OSI relationships |
| `READ_MAPPINGS_FOR_CONTRACT` | Engineer-authored `:ColumnMapping {isCurrent:true}` — `transformKind`, `transformExpression`, `transformParams`, `transformDecorators` |
| `READ_OSI_METRICS` / `READ_OSI_RELATIONSHIPS` | Advisor-applied extensions (`:OsiMetric`, `:OsiRelationship`) |

Per-field expression resolution (`_build_field_expression()`, osi.py:246-333) prefers, in order: engineer's canonical SQL → literal value (for `kind='literal'`) → decorator-wrapped target column → synthesised CAST → sentinel for non-trivial kinds → PO's `transformHint` → bare column name (trivial case). The non-trivial set — `cast, format, concat, split, substring, case, arithmetic, lookup, literal, expression` — is what counts as a "rich expression" in scoring.

Output: an OSI v0.1.1 dict with `semantic_model.datasets[].fields[]`, `relationships[]`, `metrics[]`, `ai_context`.

#### Step 2 — `validate_osi()` (osi.py:549-680)

Four-stage check producing a list of `{kind, path, message}` errors:

1. **JSON Schema** validation against the vendored OSI v0.1.1 schema (Draft 202012)
2. **Uniqueness** of dataset / field / metric / relationship names
3. **Reference resolution** — every relationship endpoint must exist in the model
4. **SQL parseability** — every field expression is parsed via sqlglot for its declared dialect

`conformance_pass = len(errors) == 0`.

#### Step 3 — `score_rubric()` (osi.py:1127, dispatched via the predicate registry): weighted completeness checklist

> `score_osi()` (`osi.py:1218`) remains as a back-compat shim that calls `score_rubric()` with the OSI rubric. Weights from `WEIGHTS` (`osi.py:65`):

| Criterion | Weight | Pass condition |
|---|---|---|
| Dataset descriptions | 15 | ≥1 dataset has description; partial credit by fraction |
| Field descriptions | 25 | ≥1 field per dataset with description; partial by coverage |
| Primary keys | 10 | ≥1 dataset declares a PK |
| Rich expressions | 20 | Field expression non-trivial (the kind set above) |
| Relationships | 10 | ≥1 declared; **N/A** if <2 datasets (weight removed from denominator) |
| Metrics | 10 | ≥1 declared (binary) |
| AI context | 10 | populated (binary) |

For each row: `status ∈ {pass, partial, fail, na}`, plus a `weight` and a human-readable `reason` like *"3/4 datasets have a description — fill in the rest to raise the score"*.

Final completeness: `(Σ proportion × weight) / Σ active_weights × 100` — N/A items reduce the denominator so they never penalise.

Band assignment (inside `score_rubric()`, ~`osi.py:1200`, thresholds from the rubric YAML / `GREEN_THRESHOLD=80` / `AMBER_THRESHOLD=50`):

```python
if not conformance_pass or completeness < amber_t:   band = "red"
elif completeness >= green_t:                        band = "green"
else:                                                band = "amber"
```

i.e. any validation error short-circuits to red regardless of completeness.

#### Step 4 — Advisor (optional, `routers/osi.py:152-224`)

When `skip_advisor=false`, the backend spawns Claude via the SDK with `allowed_tools=["Read","Skill"]`, `max_turns=4`, and a system prompt instructing it to load the `data-product-osi-advisor` skill. The skill (pure-text, no graph writes) reads the OSI dict + validation report + checklist and returns ONE fenced ```json block with `{narrative, suggestions[]}`. Suggestion types it can emit:

- `osi_metric_create` → `:OsiMetric` node
- `osi_relationship_create` → `:OsiRelationship` node
- `osi_ai_context_set` → populates `:DataContract.aiContextJson`

The wizard renders these as Apply cards; the PO accepts/rejects each, and the backend persists via `POST /api/projects/{id}/osi/{metrics|relationships|ai-context}`.

#### Step 5 — `persist_evaluation()` (osi.py:918-983)

Creates one `:OsiEvaluation` node per evaluation (append-only, batched):

```cypher
CREATE (oe:OsiEvaluation {
  uri:              'osi:eval:' + $versioned_id + ':' + $batch_id,
  band:             $band,
  completeness:     $completeness,
  conformancePass:  $conformance_pass,
  errors:           <JSON string>,
  checklist:        <JSON string>,
  narrative:        <markdown from advisor or null>,
  triggeredBy:      'manual'|'submit'|'signoff',
  evaluatorVersion: 'osi-v0.1.1',
  evaluatedAt:      datetime(),
  batchId:          <timestamp-micros>-<hex-random>
})
CREATE (dc)-[:HAS_OSI_EVAL]->(oe)
```

Latest read: `READ_LATEST_EVAL` (`MATCH ... ORDER BY oe.evaluatedAt DESC LIMIT 1`) — this constant lives in **`workbench/backend/routers/osi.py`** (the router), not `osi.py`.

> **Who else reads OSI evaluations.** `:OsiEvaluation` is consumed beyond the OSI panel: `qa.py` (which writes the sibling `:QAEvaluation` and whose questions feed the AI-Ready `ai_example_questions` predicate), `deployment_reflection.py` (reads the latest OSI band as one input to its verdict report), and `semantic_recommender.py` (reads OSI evals as a signal when recommending business concepts) all read them via `[:HAS_OSI_EVAL]`. See [`qa-and-reflection.md`](qa-and-reflection.md) and [`semantic-layer.md`](semantic-layer.md).

### How it surfaces

- `OsiAnalysisPanel.tsx` — full panel: band header, completeness gauge, per-criterion checklist with reasons, validation error list, advisor narrative.
- `OsiBadge` — compact band chip shown in `MyProductsDashboard.tsx:469-498` and on marketplace product cards.
- Marketplace product detail header carries the "Score OSI now" trigger (PO + owner-only).

---

## TL;DR — the two systems side by side

| | Quality Score | OSI Score |
|---|---|---|
| **Question** | Is the underlying data healthy? | Is the product semantically consumable? |
| **Grain** | column / dataset / catalog | one product (DataContract version) |
| **Inputs** | DQV measurements, PropertyShapes, TestResults, ColumnDescriptions | DataContract, DProdOutputDatasets/Columns, FKs, ColumnMappings, OsiMetrics/Relationships |
| **Engine** | Deterministic Cypher in `data-scoring` skill | Pure-Python in `workbench/backend/osi.py` |
| **LLM?** | No (skill is graph-in/graph-out) | Optional advisor (`data-product-osi-advisor`) for narrative + Apply cards |
| **Output nodes** | `:QualityScore` (one per level × dimension, append-only by `batchId`) | `:OsiEvaluation` (one per run, append-only by `batchId`) |
| **Verdict shape** | composite 0–1 per level, 8 dimensions, Tier 1/2/3 | completeness 0–100 + conformance bool → band Red/Amber/Green |
| **Primary UI** | `QualityScorePanel` on engineer dashboard | `OsiAnalysisPanel` in wizard + `OsiBadge` on marketplace |
| **Hidden on** | `dpe-cf` projects (consumer products don't profile raw data) | nothing — applies to every product |

Key takeaway from the skill docs themselves: composite scores **across tiers are not comparable** — adding the Documentation or Grounding dimension can pull composite down because it surfaces evidence that was invisible before. Read the dimension shape, not just the headline number.

---

## 3. Alternative scoring rubrics — Design B implemented

> Status: **Design B (selectable rubric per product) shipped on `feat/ai-ready-rubric`.** Section preserved for context on the design choice; tracks the as-built shape rather than a hypothesis.

**As built.** The contract carries a `:DataContract.scoringRubric` field (`'osi' | 'ai_ready' | ...`, defaulting to `'osi'`). Rubric configs live as YAML in `playbook/scoring_rubrics/` (one file per rubric, `id == filename stem`). The producer-side evaluator (`workbench/backend/osi.py`) is now predicate-driven: each `:criteria[].predicate` field in YAML resolves to a Python callable in `PREDICATE_REGISTRY`, dispatched from `score_rubric()`. The legacy `score_osi(osi_dict, errors)` is a back-compat shim that calls `score_rubric` with the OSI rubric. New endpoints `GET /api/scoring-rubrics` + `GET /api/scoring-rubrics/{id}` expose the catalog to the wizard's Step 1 rubric picker and `OsiAnalysisPanel`'s hint dictionary.

Each criterion's "how to fix" hint copy lives in the YAML alongside its weight/predicate, served to the frontend as part of the rubric metadata; the legacy `osiHints.ts` dictionary was removed.

The AI-Ready rubric ships with six predicates — `ai_description_quality`, `ai_lineage_traceability`, `ai_dq_rule_density`, `ai_example_questions` (graph-direct, reading `:DProdColumn` / `:ColumnDescription` / `:ColumnMapping` / `:PropertyShape` / `:QAEvaluation`) plus the OSI-shared `osi_primary_keys` and `osi_ai_context`. *Synonym coverage* and *PII classification coverage* from the illustrative sketch below are deferred — the underlying graph fields (`:Synonym` nodes, `:DProdColumn.piiClass`) don't exist yet; add the predicates when the schema lands.

**Persistence.** `:OsiEvaluation` keeps its label (renaming was deferred to a follow-up to avoid touching the five `[:HAS_OSI_EVAL]` matchsites). Each eval now carries `oe.rubric` + `oe.rubricLabel` + per-rubric `oe.evaluatorVersion` so historical evals remain attributable after a rubric switch. The detail endpoint surfaces a `rubric_changed_since_eval` boolean when the contract's current rubric differs from the latest eval's rubric.

**Lock-in semantics.** The wizard's Step 1 rubric picker is editable while the contract is in a draft state (`null` / `draft` / `ingesting`) and renders as a static badge once the contract progresses past that — mirrors the `:OsiEvaluation` append-only history model and prevents fragmenting the trend line.

### Why Design B over Design A



OSI is one specific rubric — it measures readiness for *semantic interchange*, where the consumer is a BI tool, AI agent, or downstream pipeline that needs to understand the contract without back-channelling the producer. That's not the only useful question a PO might want to score against. A reasonable alternative is **"AI-ready"** — readiness for direct LLM-agent consumption, where the rubric weights things like synonym coverage, example queries, PII flags, and lineage traceability that aren't in OSI's 7 items at all.

Two architectural shapes were considered for adding a second rubric. **Design B (selectable rubric per product)** was picked — see the "Why Design B over Design A" note above for the trade-offs we accepted (cross-rubric comparability) versus what we gained (one evaluator code path, one endpoint, drop-a-YAML extensibility).

### Design A — Parallel scoring (Quality + OSI + AI-Ready as siblings)

Mirrors the existing Quality-vs-OSI duality: add a third scoring system alongside the first two.

- New evaluator module (e.g. `workbench/backend/ai_readiness.py`) with its own translator / validator / scorer, producing an `:AiReadinessEvaluation` node alongside `:OsiEvaluation`.
- New endpoint family `/api/projects/{id}/ai-readiness/evaluate` + getter + Apply-card persistence parallel to `routers/osi.py`.
- Marketplace + readiness UI shows both bands side-by-side; new chip and new panel component.

**Pros**
- Clean conceptual separation — OSI answers "interchange-ready?", AI-Ready answers "agent-consumable?", and a product can be Green on one, Amber on the other.
- Mirrors existing pattern, easy to reason about.
- Both scores co-evolve and never fight for the same UI slot.
- Backward-compatible: every existing `:OsiEvaluation` keeps working unchanged.

**Cons**
- Roughly doubles surface area (backend module + endpoints + UI panel + chip + docs).
- Two scores can disagree and require explanation to consumers.
- Ongoing maintenance overhead per rubric.

### Design B — Selectable rubric at product creation

PO picks a rubric at wizard Step 1; that choice persists on the `:DataContract` and only that rubric's checklist is computed and rendered.

- New `:DataContract.scoringRubric: 'osi' | 'ai_ready' | ...` field (default `'osi'`).
- WEIGHTS + criteria loaded per-rubric from a config file (e.g. `playbook/scoring_rubrics/osi.yaml`, `playbook/scoring_rubrics/ai_ready.yaml`).
- One endpoint, branches internally on the contract's rubric.
- Wizard Step 1: rubric picker (OSI as recommended default).
- All readiness UI surfaces read rubric from the contract and display the relevant rubric's checklist.

**Pros**
- Lighter implementation — one evaluator code path, one endpoint, one UI panel.
- One score per product → no cross-comparison confusion.
- Adding a new rubric becomes "drop a YAML file" (assuming the new rubric's criteria can be expressed as graph predicates).

**Cons**
- No cross-rubric comparison — marketplace consumers can't compare two products on the same axis if they chose different rubrics. Two products might both be "Green" against entirely different criteria.
- Switching rubric mid-lifecycle fragments history (`:OsiEvaluation` for the OSI period, something else for the AI-Ready period).
- The "rubric" concept becomes load-bearing across every surface — chip, marketplace listing, comparison views — and has to be plumbed everywhere.

### Decision criteria

1. **Comparability**: will marketplace consumers need to compare two products that chose different rubrics on the same axis? *YES → lean Design A. NO → Design B is fine.*
2. **Conceptual orthogonality**: are the rubrics legitimately measuring different concepts (semantic-interchange-readiness ≠ AI-consumption-readiness), or are they different weightings of the same concept? *Different concepts → Design A. Just reweighting → Design B (or simpler: YAML override of OSI weights, no schema change needed).*
3. **Implementation budget**: how much PR-surface can we spend? *Small → Design B. Larger → Design A.*

### Illustrative sketch of an "AI-Ready" rubric (NOT a commitment)

Listed only to make the discussion concrete. Most of these are NOT in OSI's 7 items, which is what would make AI-Ready a legitimately different rubric rather than just an OSI re-weighting:

- Synonym coverage — % columns with `:Synonym` annotations or equivalent
- Example-question coverage — ≥3 representative NL questions provided per product
- PII classification coverage — every column classified or explicitly N/A
- Description quality — length + tone heuristic, or LLM-judged
- DQ rule density — approved rules per column
- Lineage traceability — % columns with provenance back to a source

### Recommendation (for the reader to react to)

- Lean **Design A** if "AI-Ready" really is a different concept from "semantically-interchangeable" — and on first inspection it is, since agent-grounding criteria like synonyms and example queries aren't in OSI at all.
- Lean **Design B** if the goal is mostly to give POs an opt-out from OSI's specifics for products where semantic interchange isn't the consumption goal — and you're willing to lose cross-product comparability.

### Where the as-built code lives

- **Rubric YAMLs**: `playbook/scoring_rubrics/osi.yaml`, `playbook/scoring_rubrics/ai_ready.yaml` (weights, criteria, predicate names, hint copy, band thresholds).
- **Predicate registry**: `workbench/backend/osi.py::PREDICATE_REGISTRY` (11 predicates: 7 OSI + 4 AI-Ready). New rubrics add a YAML file + as many new predicate Python functions as they need.
- **Score dispatcher**: `workbench/backend/osi.py::score_rubric()` iterates `rubric.criteria`, dispatches each through the registry, applies band thresholds from the YAML.
- **Eval endpoint**: `routers/osi.py::POST /api/projects/{id}/osi/evaluate` reads the contract's rubric, branches translate/validate/advise on `rubric.requires_translation` / `rubric.require_osi_conformance` / `rubric.advisor`.
- **Rubric catalog endpoint**: `routers/osi.py::GET /api/scoring-rubrics` (list) + `GET /api/scoring-rubrics/{id}` (detail) — public-facing slice (predicate names stripped).
- **Wizard picker**: `pages/product/NewProductWizard.tsx` Step 1, below the domain grid. Lock semantics: editable when `lifecycle_state ∈ {null, draft, ingesting}`, badge-only after.
- **Contract field**: `:DataContract.scoringRubric` set in all four `_contract_versioning.py` Cypher templates and read from `osi.READ_CONTRACT_HEAD` via `coalesce(dc.scoringRubric, 'osi')`.

### Not yet built — follow-ups

- Rename `:OsiEvaluation` → `:ReadinessEvaluation` plus the `[:HAS_OSI_EVAL]` rel. Five MATCH sites would need to migrate atomically.
- Move the URL family from `/osi/*` to `/readiness/*` once a third rubric makes the OSI-named path obviously misleading.
- Predicates for `synonym_coverage` and `pii_classification` once `:Synonym` / `:DProdColumn.piiClass` ship.
- An AI-Ready advisor skill (`data-product-ai-ready-advisor`) so AI-Ready products get LLM narratives + Apply cards the same way OSI does today.
