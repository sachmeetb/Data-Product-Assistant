# QA evaluation & deployment reflection

> **Read on demand.** Two sibling "does this product actually work?" subsystems, both modelled as append-only sidecar nodes on the `:DataContract` (mirroring `:OsiEvaluation`). Open when working on `routers/qa.py` + `qa.py` + `qa_execute.py`, `deployment_reflection.py` + the `deployment_reflection` stage, or the marketplace QA tab.

## QA evaluation (`qa.py`, `routers/qa.py`, `qa_execute.py`)

**What it answers:** "What questions can this product answer, and can it answer *this* one?" — independent of data quality / OSI readiness.

`build_qa_context()` walks the product's schema + DQ rules + dataset-level shape (grain, filter, dedupe, joins, SCD policy, grouping/aggregations, suppressed columns, window specs, CONSUMES'd source products) and feeds it to the **`data-product-question-analyzer`** skill in one of two modes:

- **generate** (`POST /api/projects/{id}/qa/evaluate`) — a curated list of natural-language questions the product can answer, plus `near_miss_gaps[]` (questions it *almost* answers and what's missing). By default this persists a `:QAEvaluation` sidecar (`PERSIST_EVAL_QUERY`) appended via `:HAS_QA_EVAL`; `?persist=false` returns the payload for wizard preview only. The node carries `questionsJson`, `nearMissGapsJson`, `narrative`, `generatedForVersion`, `triggeredBy`, `batchId`, `evaluatedAt`. `GET /api/projects/{id}/qa/evaluation` returns the head eval with a `stale` flag (derived from `:DataContract.lastSchemaChangeVersion` vs `generatedForVersion`).
- **probe** (`POST /api/projects/{id}/qa/probe`) — classifies a free-form consumer question as `answerable` / `partially` / `no` / `out_of_scope` with named gaps. Probe results are **not** persisted.

**Executing a question** (`qa_execute.py`, `POST /api/marketplace/products/{cid}/qa/execute` + a project-scoped wrapper): given a curated question from a `:QAEvaluation` (text + supporting columns + category) plus the product's deployed-view metadata, the **`data-product-question-executor`** skill authors a single SELECT. The backend validates it against an allow-list of view references and runs it through the **same `sql_executor.execute_select` gated path** the Phase-1 preview uses (single-SELECT + statement-count enforcement). The executor is the sibling of the analyzer: the analyzer produces the question, the executor answers it.

**Feeds the AI-Ready OSI predicate.** The `:QAEvaluation` question count is read by the AI-Ready rubric's `ai_example_questions` predicate (`osi.py:_pred_ai_example_questions`, ~`osi.py:1068`): ≥3 questions → pass, 1-2 → partial, 0 → fail. So generating QA questions directly raises a product's AI-Ready readiness score. See [`data-product-scoring.md`](data-product-scoring.md).

Surfaced on the marketplace product detail's QA tab (`PRODUCT_DETAIL` pulls the latest `:QAEvaluation` via `:HAS_QA_EVAL` — see [`marketplace.md`](marketplace.md)).

## Deployment reflection (`deployment_reflection.py`, the `deployment_reflection` stage)

**What it answers:** "Now that the view is deployed, does the real data match what we declared?"

The `deployment_reflection` stage (owner: Data Engineer; skill: **`data-product-deployment-reflector`**) runs after `deploy_virtual_view`. `gather_inputs()` pre-fetches the product's **declared shape** (the graph snapshot — `:DProdColumn`s, `:ColumnDescription`s, `:PropertyShape` DQ rules, `:DatasetTransform`, the latest `:QAEvaluation`, and the latest OSI band via `:HAS_OSI_EVAL`) and a **sample of preview rows** from the deployed virtual view. The pure-text skill (no graph/filesystem writes) compares the two and emits a structured report:

```
{ verdict, narrative, surprises[], description_alignment[], rule_alignment[], qa_alignment[], recommendations[] }
```

i.e. a verdict + narrative + cited surprises in the actual data + per-dimension alignment checks (declared descriptions / rules / QA questions vs observed rows) + recommendations.

The result is persisted **append-only** as a `:DeploymentReflection` node linked to the contract via `:HAS_DEPLOY_REFL` (`persist_reflection()`, mirroring the `:OsiEvaluation` / `:QAEvaluation` sidecar pattern). Endpoints: `POST /api/projects/{id}/reflection/run` (the stage trigger, `routers/serving.py`) and `GET /api/projects/{id}/reflection/latest`; the marketplace surfaces it at `GET /api/marketplace/products/{cid}/reflection/latest`. Reconciliation in `routers/edits.py` resets the `deployment_reflection` stage (alongside the other terminal stages) when an upstream edit fires, so a re-deploy re-runs the reflection.
