# Frontend CLAUDE.md

Frontend-specific detail for `workbench/frontend/`. The root `CLAUDE.md` at the repo root covers orientation, SA/CF distinction, graph isolation, and architecture overview — read that first.

## Role-based access (`RoleContext.tsx`, `types.ts`)

| Role | Can run | Can review |
|---|---|---|
| Data Product Owner | Initiate, ODCS Specification, marketplace **Deploy** + **Score OSI now** | Source product validation (names / descriptions / tables / relationships / rules — the `PoValidationPage` gate), Domain rules |
| Data Engineer | Discovery, Profiling, ODCS → dprod, Mapping, Serving (virtual + materialized), Deploy View, Metadata Enrichment, Column Name Standardization, Mark Discovery Complete, Synthesize ODCS from Graph, Auto-Map Source Columns, Deployment Reflection, Mark Engineering Complete | Unmapped columns |
| Data Steward | Reflect on Reviews | Descriptions (non-SA only), Transformation Escalations |
| Data Quality Analyst | DQ Rules, DQ Testing, DQ Failure Analysis, Scoring, Domain Rules, Remediation, Remediation Planning, Rescore | Domain Rules |
| Reviewer | — | Descriptions, Mappings |

`ROLE_STAGE_PERMISSIONS_BY_ID` in `types.ts` is the source of truth — missing a stage_id from the engineer list disables the Run button silently.

## Apply protocol (Product Workbench wizard)

Product chat assistant emits ` ```suggestion ` JSON blocks with `applies_to`:
- `idea` / `domain` / `name` / `dataset_name` / `description` / `purpose` — replace wizard field
- `schema_add_columns` — append net-new columns to `customColumns`
- `schema_pick_columns` — replace `selectedColumns` with catalog column names (unknown names silently dropped)
- `rule_decisions` — approve/reject existing pending rules by `rule_uri`
- `rule_create` — net-new rules; wizard mints `rule_uri`s server-side via `persist-user-rules` and auto-approves. **Never invent a `rule_uri` client-side.**
- `column_transform_set` — set a transform hint on a single custom column (kind / inputs / params / decorators). Catalog-picked columns are NOT eligible (recommend `schema_add_columns` first).
- `shape_set` — dataset-level Shape step authoring (Phase 3+4+5): `{shape: {grain_prose?, filter?, scd_policy?, grouping_keys?, suppressed_columns?}}`. Partial-update semantics — only present keys mutate wizard state. `grouping_keys` and `suppressed_columns` are filtered against current schema names (unknown dropped); PK names in `suppressed_columns` are silently stripped (view-DDL would refuse anyway). `scd_policy` accepts either a bare type string (`'latest_only'` / `'snapshot'` / `'scd2'` / `''`) OR an object `{type:'scd2', effective_column, expiration_column, add_is_current}` for full SCD-2 authoring; object-form column names are filtered against current schema.
- `osi_metric_create` / `osi_relationship_create` / `osi_ai_context_set` — OSI semantic-model authoring cards emitted by `data-product-osi-advisor` on the Readiness Review step (metrics, entity relationships, AI usage context).
- `revision_notes` / `change_kind` — edit-mode revision metadata (what changed + why), carried on re-submit of a versioned contract.

Apply is async: "Applying..." → "Applied" / "Failed". File attachments (`.txt`/`.json`/`.csv`, ≤2MB, ≤10/turn) materialise to `scope_dir/chat-attachments/<token>/`.

## Engineer dashboard (`ProjectDashboard.tsx`)

Stat cards. Per-archetype filter via `CONSUMER_HIDDEN_CARDS` set: `dpe-cf` projects hide `datasets`, `columns`, `descriptions`, `final_descriptions`, `profiling`, `dq_rules`, `allowed_values`, `quality_score`, `relationships` (none of those exist for a consumer project — it reads FK relationship semantics from upstream sources via `:CONSUMES`). The new **Inputs** card surfaces CONSUMES'd source products (count + dataset + column count); each row links via `target=_blank` to the source product's marketplace detail.

Detail row tables (`/api/projects/{id}/summary/detail?card=...`) anchor on the project's own `:DProdColumn` (URI-prefix scoped) for mapping-related queries, so consumer-aligned projects with `:DProdColumn` sources show real rows (not the empty list a `:Column`-only path produces).
