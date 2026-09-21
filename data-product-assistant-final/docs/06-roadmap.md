# Roadmap

Ordered to match the stated priorities — edit, bronze, unified requirements, then
source→bronze pipelines last — with technical prerequisites called out where
something genuinely has to come first.

---

## Phase 0 — Understand and scaffold ✅ DONE

- [x] Map both predecessors as-implemented ([01](01-current-gold-flow.md), [02](02-current-silver-flow.md))
- [x] Capability matrix, shared pathologies, port/rebuild/drop ([03](03-common-points-and-seams.md))
- [x] Target architecture ([04](04-target-architecture.md))
- [x] Canonical model: Domain → Table → Attribute with provenance and canonical types
- [x] Provenance merge policy — regeneration cannot clobber human edits
- [x] Catalog reconciler — addable columns / tables / domains, type mismatches
- [x] Declarative flow graph — guarded edges, terminal states refuse re-entry
- [x] Artifact dependency DAG — selective downstream invalidation
- [x] Pack loader — 12 domains, 5 industries, both inherited formats, 84 tables / 512 attributes
- [x] Block library — 10 universal JSON Schema blocks with `$ref`/`$defs` resolution
- [x] Bronze envelope, defined once so the emitter and the check cannot diverge
- [x] Platform-agnostic pipeline IR
- [x] Contract/publish split decided and scaffolded ([04 §7](04-target-architecture.md)):
      ODCS v3.2.0 export + Knowledge Catalog data product plan builder with its
      50-asset, co-location and post-materialisation guardrails
- [x] 127 tests

---

## Phase 1 — Make it run (prerequisite for everything below)

Nothing above is wired into a running service yet.

- [ ] **Agent runtime** — port GOLD `backend/agents/base.py`: Vertex `google.genai`,
      per-skill model and token budgets, `ThinkingConfig` capped on flash,
      Cloud Trace spans. Strip the ADK/MAF docstrings that describe a runtime
      that was removed.
- [ ] **Firestore state** — one document per session, artifacts as a
      subcollection. Kills both predecessors' in-process stores, the
      never-persisted per-agent LLM history, and SILVER's two incompatible
      shapes on one store.
- [ ] **Flow definition** — express the Phase-0→5 sequence from
      [04 §8](04-target-architecture.md) as a `FlowGraph`; `validate()` must pass in CI.
- [ ] **FastAPI surface** — `/chat`, `/design`, `/artifacts`, `/reconcile`.
- [ ] Node handler resolution and the runner (`core/flow/runner.py`).

## Phase 2 — Unified requirements agent

- [ ] Port `requirements_gate.py` **verbatim**, keeping its golden-invariant test.
      It is the best-tested code in either product and deliberately ignores the
      model's own `handoff_ready` flag.
- [ ] Merge the two agent contracts onto one core schema
      ([03 §3](03-common-points-and-seams.md)); normalise the synonym pairs
      (`consumer_role`/`primary_consumers`, `data_freshness`/`latency_requirement`,
      `granularity`/`target_grain`, `data_sources`/`source_systems`).
- [ ] Keep GOLD's 4-state `field_status` — SILVER's 3-state cannot express
      "the user told us they don't know", which is what lets the gate block
      without a bypass.
- [ ] Pack extension mechanism: optional closed domain enum, extra fields,
      prerequisite profile stage.
- [ ] **Delete the server-side override.** SILVER forced `is_ready=True` on a chip
      and fabricated `consumer_role`/`granularity`/`filters`, so users were shown
      a partly invented card.
- [ ] Generalise `bank_profile_agent` → org profile, driven by the active pack.

## Phase 3 — Discovery against a real catalog

- [ ] Port `discovery.py` + `catalog_tool.py` (deterministic, no LLM — keep it that way).
- [ ] **Move the `sample_data` fence out of `prompts/DPI/discovery/SKILL.md`** into a
      real data file first. `catalog_tool.py:66` parses that prompt at import
      time, so editing it as a prompt silently breaks the catalog.
- [ ] Wire `hydration/bigquery_hydrator.py` into the app — it already hydrates a
      catalog from BigQuery but its own README says "not wired into the app".
- [ ] Discovery reads the live catalog, not a static `utility_catalog.json`.
- [ ] Port `challenger.py` and `use_case_classification.py`.
- [ ] Decide the non-analytics question: GOLD hard-stops everything ≠ `analytics`
      and **discards the requirement**. Widen it, or keep the stop and say so in the UI.

## Phase 4 — Design phase on the canonical tree

- [ ] Port `gold_layer_agent.build_er` and `silver_layer_agent.build_sttm` /
      `build_silver_transformation` to read and write `DataProductDesign` instead
      of bespoke per-agent JSON.
- [ ] Silver schema synthesis by block composition, using
      `BlockLibrary.expand` plus SILVER's deterministic repair path
      (`_enrich_plan_with_block_columns`) when the model returns no columns.
- [ ] Surface the three levels explicitly in the flow: domain selection →
      table set → attributes.
- [ ] Feed pack `source_mappings` and `value_normalisation` into the STTM agent
      as context — only the CPG packs carry these today, and they are exactly
      what stops the agent re-deriving mappings from prose.

## Phase 5 — The edit function

Engine is done; this is the API, propagation and UI.

- [ ] `core/artifacts/patch.py` — typed patch ops at all three levels.
- [ ] `core/artifacts/store.py` — versioned artifact store on Firestore.
- [ ] `PATCH /artifacts/{id}` with typed paths (`domain/table/attribute`).
- [ ] Propagation: apply a patch → `ArtifactGraph.mark_changed` → show the
      stale set → **diff preview** → regenerate on confirm.
- [ ] `GET /reconcile` — surface the `Delta` as add-able items in the UI.
- [ ] Suggestion review UI for changes withheld against locked fields.
- [ ] Make `STTMCard` and the schema views editable — both predecessors'
      versions are strictly read-only despite `BusinessGlossaryCard` promising
      "Edit any line, or accept all to lock the glossary".
- [ ] Replace `TweakModal`'s single free-text box, which posted prose that GOLD
      turned into a full regeneration and SILVER discarded entirely.
- [ ] Stop flattening `EditRequirementsForm` to a markdown bullet list —
      structured data is currently destroyed at the browser boundary, and GOLD's
      server has no `action=="edit"` branch to receive it anyway.

## Phase 6 — Bronze

- [ ] `bronze/synth/` — synthetic bronze per pack: deliberately raw, with
      inconsistent casing, nulls, string-typed numerics, duplicates and late
      arrivals, so the DQ and quarantine paths are exercised. Replaces the single
      static fixture two design agents currently depend on.
- [ ] `bronze/ingest/` — register → profile → land → quarantine →
      **register into catalog**. The last step closes the loop so discovery
      returns real `bronze_matches`.
- [ ] Reuse GOLD's `file_extractor.py` + `dpi_file_metrics` as the front half.
- [ ] Rewrite the three DPB prompts that contradict each other on whether bronze
      exists (`publisher`: "there is none"; `pipeline-generator`: creates all
      three datasets; `test-agent`: must be one of the three).
- [ ] Keep `gold-er`'s bronze-only branch — a real sample run took that path.

## Phase 7 — Validation that actually validates

- [ ] `core/validation/engine.py` — evaluate rules in **Python** against real
      DDL and real BigQuery schemas.
- [ ] Split SILVER's `validation_rules.yaml`: naming (GR006/7) and BigQuery
      syntax (BQ001-4) → `core/validation/rules/`; AML/KYC and regulatory
      guardrails → `packs/banking/knowledge/`.
- [ ] Envelope rule derives from `bronze/envelope.py` — already enforced by test.
- [ ] Keep GOLD's generate→test→repair loop (`MAX_TEST_ITERATIONS=5`) and its
      live BigQuery sample query.
- [ ] Remove the permissive pass gate: SILVER passed a malformed report with no
      `checks` key, then published "anyway with warnings".

## Phase 8 — Build and publish

- [ ] `pipelines/renderers/bigquery.py` — IR → BigQuery SQL / Dataform.
- [ ] Port `BigQueryPublisher`, keeping SILVER's `dry_run|live` modes and GOLD's
      layer-aware skip. Drop `_rewrite_sql_to_bq` — it exists only to undo
      Databricks references the IR will never emit.
- [ ] **Knowledge Catalog publish** — wire `KnowledgeCatalogClient.apply` for real
      (`dataProducts.create` → `dataAssets.create` per table → `entries.patch` for
      aspects). The plan builder and its guardrails are already done and tested.
- [ ] Register the four custom aspect types in the target project before first
      publish: `data-product-domain`, `medallion-layers`, `attribute-lineage`,
      and a provenance type.
- [ ] Wire access groups → Google Groups / service accounts → IAM. Publishing
      grants real access, so keep it an explicit reviewed action — never
      something an agent triggers implicitly.
- [ ] **ODCS v3.2.0 export** — generator is done and tested; add JSON Schema
      validation against the pinned `apiVersion`, and confirm the `servers`
      entry's `project`/`dataset` keys are spec-valid rather than
      `customProperties` (flagged in `core/contracts/odcs.py`).
- [ ] Confirm data-product granularity against the widest real design — one
      product per domain should stay under the 50-asset limit, but verify.
- [ ] Retire SILVER's `dataContractSpecification 0.9.3` generator; keep its
      Dataplex manifest emitter only if Knowledge Catalog does not subsume it.

## Phase 9 — Source→bronze pipelines, platform-agnostic

The stated final goal.

- [ ] Source connector specs: object store, relational, external table, stream, upload.
- [ ] Source→bronze IR generation with attribute-level lineage.
- [ ] `pipelines/renderers/airflow.py` — port SILVER's `generate_airflow_dag_code`.
- [ ] `pipelines/renderers/dbt.py`, `pipelines/renderers/spark.py`.
- [ ] Register a second `CanonicalType` target via `register_target` to prove the
      type layer is genuinely portable (it refuses partial coverage).

## Phase 10 — Frontend

- [ ] Start from GOLD's — it is the superset, and SILVER's is a fork of it with
      the same components and the same dead code.
- [ ] Remove the campaign hardcoding: `STTMCard.jsx:205-306`'s `CAMPAIGN_FRAMEWORK`
      and the `detectFramework()` fallback that silently shows an invented
      11-entity schema whenever "campaign" appears in the text.
- [ ] Delete dead components: `DomainFrameworkModal` (302 lines, never imported),
      `LeftPane`, `RightPane`.
- [ ] Point it at this backend — `frontend/.env.production` and the hardcoded
      `API_BASE` fallback in `api/chat.js:1` still target Azure.

## Ongoing — cleanup that should not wait

- [ ] **Rotate the credentials in `GOLD/backend/.env`** (Anthropic, Azure SP, Databricks PAT).
- [ ] Never port `SILVER/tools/gcp_auth.py:29`'s process-wide SSL bypass.
- [ ] Strip Databricks residue: `databricks_tool.py`, Unity-Catalog checks in
      `test_agent`, `DOUBLE` types, the Databricks-era README.
- [ ] Strip Azure residue: `azure-pipelines-*.yaml`, `cicd.yaml`, `startup.sh`.
- [ ] Remove every silent fallback — the canned DDL, the 9 hardcoded STTM dicts,
      the fabricated "100% matched" availability PDF. Fail visibly.
- [ ] Fix the mojibake in `DDI/silver-transformation/SKILL.md` (`â†'` for →).
- [ ] Pick one expansion for DDI, or drop the acronym — GOLD uses "Data Designer"
      and "Data Design Initiative" in different files.
