export interface ConfigField {
  key: string;
  label: string;
  type: "text" | "select" | "multiselect" | "playbook_select";
  placeholder?: string;
  required?: boolean;
  options_key?: string;
  /** When set, this field's selected values filter another field's options by description match */
  filters_key?: string;
}

export interface PlaybookOption {
  version: number;
  summary: string;
  item_count: number | null;
  updated_at?: string;
}

export interface PlaybookOptions {
  baseline: PlaybookOption;
  refined: PlaybookOption | null;
}

export interface ConfigOption {
  value: string;
  label: string;
  uri?: string;
  description?: string;
}

export interface StageInfo {
  id: number;
  stage_number: number;
  stage_name: string;
  stage_id?: string;
  workflow_id?: string;
  status: "pending" | "running" | "awaiting_review" | "complete" | "failed";
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  cost_usd: number | null;
  owner_role?: string;
  requires_llm?: boolean;
  agentic?: boolean;
  has_review?: boolean;
  review_type?: "descriptions" | "mappings" | "domain_rules" | "source_product_validation" | null;
  config_fields?: ConfigField[];
  description?: string;
}

export interface WorkflowInfo {
  workflow_id: string;
  name: string;
  description?: string;
  order: number;
  repeatable: boolean;
  stages: StageInfo[];
}

export interface Archetype {
  slug: string;
  name: string;
  description: string;
  prefix: string;
  implemented: boolean;
}

export interface WorkflowStep {
  stage_id: string;
  order: number;
  optional: boolean;
  enabled: boolean;
  exclusive_group?: string;
  name?: string;
  owner_role?: string;
  has_review?: boolean;
  review_type?: string;
  config_fields?: ConfigField[];
  sub_stages?: string[];
}

export interface ProjectInfo {
  id: number;
  project_code: string;
  name: string;
  archetype: string;
  domain: string | null;
  product_idea?: string | null;
  parent_intake_submission_id?: number | null;
  data_connectivity_mode?: string;
  discovery_complete_at?: string | null;
  neo4j_host: string;
  neo4j_port: number;
  neo4j_user: string;
  neo4j_password: string;
  neo4j_database: string;
  multi_workflow: boolean;
  current_stage: number;
  role_assignments: string;
  created_at: string;
  stages: StageInfo[];
  workflows: WorkflowInfo[];
  // dmig-task only (estate/discovery fork): links this per-row execution project
  // back to the dmig discovery project + fixture row it was spawned from.
  dmig_parent_project_id?: number | null;
  dmig_row_id?: string | null;
  dmig_migration_approach?: string | null;
}

export interface WSMessage {
  type: "text_delta" | "tool_use" | "tool_result" | "thinking" | "stage_started" | "stage_complete" | "stage_status_changed" | "agent_question" | "agent_timeout" | "error" | "log_truncated";
  text?: string;
  tool?: string;
  id?: string;
  input?: Record<string, unknown>;
  message?: string;
  stage_number?: number;
  stage_name?: string;
  run_id?: string;
  workflow_id?: string;
  started_at?: string;
  status?: string;
  cost_usd?: number;
  is_error?: boolean;
  at_event?: number;
  // Agent question fields
  question_id?: string;
  message_type?: "notification" | "free_text" | "yes_no" | "multiple_choice" | "checklist";
  prompt?: string;
  context?: string;
  options?: AgentOption[];
  default_value?: string;
  timeout_seconds?: number;
  default_used?: boolean;
}

export interface StageExecutionSummary {
  id: number;
  run_id: string;
  stage_number: number;
  workflow_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  status: string;
  cost_usd: number | null;
  event_count: number;
  tool_counts: Record<string, number>;
  truncated: boolean;
}

export interface StageExecutionDetail extends StageExecutionSummary {
  session_id: string | null;
  error_message: string | null;
  events: WSMessage[];
}

export interface AgentOption {
  value: string;
  label: string;
  description?: string;
}

// ── Consumer-aligned ingest + wizard source binding ─────────────────────
// Used by ResolveAndBindSourcesStep (shared between the wizard's step 7 and
// the ingest cf path) and the IngestExistingProductPage classification banner.

export interface IngestClassification {
  kind: "source" | "consumer";
  confidence: number;
  rationale: string;
  signals: string[];
  inferred_dependencies: Array<{
    name: string;
    domain?: string | null;
    encompasses?: string | null;
  }>;
  _fallback?: boolean;
  _error?: string;
}

export interface ResolveSlotCandidate {
  uri: string;
  contract_id: string | null;
  name: string;
  domain?: string | null;
  description?: string | null;
  purpose?: string | null;
  // 'source' | 'aggregate' | 'consumer' — a consumer may build on any published
  // product (multi-hop chains). Drives the ProductKindChip in the picker rows.
  product_kind?: string;
  match_score: number;
  match_kind: "exact_uri" | "exact_name" | "semantic";
  rationale: string;
}

export interface ResolveSlotGapSuggestion {
  name: string;
  domain?: string | null;
  encompasses: string;
  // The classifier's synthesize_missing_source returns objects, not strings.
  minimal_columns: Array<{ name: string; type?: string; purpose?: string }>;
}

export interface ResolveSlot {
  slot_id: string;
  source: "spec_inputs" | "inferred" | "manual";
  declared: { dprod_uri: string | null; name: string | null };
  encompasses_hint?: string | null;
  candidates: ResolveSlotCandidate[];
  preselected_candidate_uri: string | null;
  // Backend-emitted confidence band derived from the top candidate's
  // match_score relative to the preselect threshold:
  //   strong    — preselected AND top score ≥ 80 (high-confidence match)
  //   tentative — preselected AND top score in [threshold, 80) (borderline)
  //   gap       — no preselect (UI surfaces candidates[] as "considered
  //               (low confidence)" if any, plus the gap_suggestion)
  // Optional for back-compat with payloads from before this field landed.
  confidence_band?: "strong" | "tentative" | "gap";
  gap_suggestion: ResolveSlotGapSuggestion | null;
  // Local UI state (not in match-inputs response):
  resolution?: "matched" | "gap";
  selected_uri?: string | null;
  selected_contract_id?: string | null;
  selected_name?: string | null;
  spawned_request_id?: number | null;
  spawned_project_id?: number | null;
}

export interface IngestDraftRow {
  id: number;
  owner_email: string;
  created_at: string | null;
  updated_at: string | null;
  source_filename: string | null;
  parsed_spec_json: string;
  classification_json: string | null;
  archetype_choice: string | null;
  input_selections_json: string | null;
  status: string;
  committed_project_id: number | null;
}

/**
 * Per-consumer-column status from the step-8 pre-flight gap analysis. The
 * skill (or its heuristic fallback) compares each consumer schema column
 * against the per-column metadata of the picked candidate source-aligned
 * data products.
 */
export interface GapAnalysisColumnResult {
  column_name: string;
  status: "covered" | "derivable" | "ambiguous" | "gap";
  confidence: number;
  source_evidence: string[];
  rationale: string;
}

export interface GapAnalysisResponse {
  gaps: GapAnalysisColumnResult[];
  summary: string;
  _fallback?: boolean;
  _error?: string;
  /** True when the call ran against zero candidate sources — every column
   *  comes back as a gap. UI uses this to render a clearer empty-state. */
  _no_sources?: boolean;
}

/**
 * Engineer→PO request asking the PO to identify (or create) one or more
 * source-aligned data products for a consumer-aligned project. Returned by
 * GET /api/my-products/source-candidate-requests. The PO Acknowledges via
 * MyProductsDashboard, which deep-links into the consumer wizard's
 * Confirm-candidate-sources step (?step=9&from_request=<id>).
 */
export interface SourceCandidateRequest {
  request_id: number;
  project_id: number;
  project_code: string;
  project_name: string;
  contract_id: string;
  engineer: string;
  submitted_at: string | null;
  notes: string | null;
  /** Set when escalated from a column-level surface (MappingReviewPanel /
   *  UnmappedColumnsPanel). Empty for empty-picker triggers. */
  gap_column_uri: string | null;
  gap_reason: string | null;
}

export type Role =
  | "Data Product Owner"
  | "Data Engineer"
  | "Data Steward"
  | "Data Quality Analyst"
  | "Reviewer";

export const ROLES: Role[] = [
  "Data Product Owner",
  "Data Engineer",
  "Data Steward",
  "Data Quality Analyst",
  "Reviewer",
];

// Role → stage permissions by stage_id
export const ROLE_STAGE_PERMISSIONS_BY_ID: Record<Role, string[]> = {
  "Data Product Owner": ["initiate", "odcs_specification", "publish"],
  "Data Engineer": [
    "odcs_to_dprod", "select_data_source",
    "data_discovery", "data_discovery_composite",
    "load_schema",
    "data_profiling", "data_profiling_composite", "load_profiles",
    "metadata_enrichment",
    "data_mapping",
    "serving_virtual_view", "serving_physical_copy", "serving_lakehouse_export", "serving_transfer",
    "file_export", "profile_parquet",
    "deploy_virtual_view", "deploy_physical_copy", "deploy_lakehouse", "deploy_transfer",
    "deployment_reflection",
    "mark_engineering_complete",
    // dpe-sa engineer stages — added with the source-aligned archetype.
    // Without these the Run button renders disabled on the engineer's
    // pipeline view.
    "data_discovery_offline",
    "column_name_standardization",
    "mark_discovery_complete",
    "synthesize_odcs_from_graph",
    "auto_mapping_sa",
    "configure_serving",
    "configure_transfer_placement",
    // dmig (data migration) engineer stages. Without these the Run button
    // renders disabled on the engineer's migration pipeline view.
    "dmig_import_schema",
    "dmig_configure",
    "dmig_assess_plan",
    "dmig_generate_pipeline",
    "dmig_execute_transfer",
    "dmig_reconcile",
    // cmig (code migration) engineer stages. Without these the Run button
    // renders disabled on the engineer's code-migration pipeline view.
    "cmig_link",
    "cmig_import_code",
    "cmig_configure",
    "cmig_reverse_engineer",
    "cmig_forward_engineer",
    "cmig_package",
  ],
  "Data Steward": ["reflect_on_reviews"],
  "Data Quality Analyst": ["configure_dq", "dq_rule_generation", "dq_test_generation_gx", "dq_test_generation_python", "dq_test_execution", "dq_failure_analysis", "data_scoring", "data_remediation", "data_remediation_planning", "rescore_composite"],
  "Reviewer": [],
};

// Legacy number-based permissions (for old projects without stage_id)
export const ROLE_STAGE_PERMISSIONS: Record<Role, number[]> = {
  "Data Product Owner": [1],
  "Data Engineer": [2, 3, 4, 5, 8],
  "Data Steward": [6],
  "Data Quality Analyst": [7, 9, 10],
  "Reviewer": [],
};

export type ReviewType =
  | "descriptions"
  | "mappings"
  | "domain_rules"
  | "transformation_escalations"
  | "unmapped_columns"
  | "source_product_validation"
  | "code_spec";

// "unmapped_columns" is technically a creation task, not a review, but it
// fits the same UI surface so we route it through the review panel.
// "source_product_validation" is the dpe-sa combined names/descriptions/rules
// gate — owned by the Data Product Owner.
export const ROLE_REVIEW_PERMISSIONS: Record<Role, ReviewType[]> = {
  "Data Product Owner": ["domain_rules", "source_product_validation"],
  "Data Engineer": ["unmapped_columns", "code_spec"],
  "Data Steward": ["descriptions", "transformation_escalations"],
  "Data Quality Analyst": [],
  "Reviewer": ["descriptions", "mappings"],
};

export function canRunStage(role: Role, stageNumber: number, stageId?: string): boolean {
  if (stageId) {
    return ROLE_STAGE_PERMISSIONS_BY_ID[role].includes(stageId);
  }
  return ROLE_STAGE_PERMISSIONS[role].includes(stageNumber);
}

export function canReview(role: Role, reviewType: ReviewType): boolean {
  return ROLE_REVIEW_PERMISSIONS[role].includes(reviewType);
}

// Review types

export interface PendingDescription {
  schema: string;
  table_name: string;
  col_uri: string;
  col_name: string;
  data_type: string;
  ordinal: number;
  desc_uri: string;
  description_text: string;
}

// Tagged-union of structured transformation kinds. Aligns with the SQL
// compilation table in data-serving-virtual-view/SKILL.md.
export type TransformKind =
  | "direct"
  | "cast"
  | "format"
  | "concat"
  | "split"
  | "substring"
  | "case"
  | "arithmetic"
  | "lookup"
  | "literal"
  | "expression"
  // Phase 1 of implementingdatatransformations.md — privacy + discretization
  // column-level kinds. tokenize deferred (needs vault).
  | "bucket"
  | "mask"
  | "hash"
  // Phase 6 — window function. References a named entry in
  // :DatasetTransform.window_specs via transformParams.window. Compiles
  // to `<FUNCTION>(<arg>) OVER (<window>)` at view-DDL time.
  | "window"
  // Phase 7 of transform-portability.md — neutral date-difference op with an
  // explicit `semantics` discriminator. Renders per-platform at view-DDL time
  // (e.g. AGE on Postgres, TIMESTAMPDIFF on MySQL) — supersedes hand-written
  // AGE()/DATEDIFF() so the same mapping is portable across served engines.
  | "date_difference";

// Lookup-strategy values that light up extra params on the lookup arm.
// `equi` is the default and the historical behavior; the rest extend the
// kind without spawning new top-level kinds (see implementingdatatransformations.md §2).
export type LookupSelectionStrategy =
  | "equi"
  | "latest"
  | "aggregate"
  | "exists";

export const LOOKUP_STRATEGY_LABELS: Record<LookupSelectionStrategy, string> = {
  equi:      "Equi-join (1:1)",
  latest:    "Most-recent row per key",
  aggregate: "Aggregate per key",
  exists:    "Existence flag (boolean)",
};

export const LOOKUP_AGGREGATE_FUNCTIONS = [
  "SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT",
] as const;

// Phase 3 of implementingdatatransformations.md — column-level aggregate
// function. Lights up only when the product :DatasetTransform has
// grouping_keys set; ignored otherwise. FIRST/LAST are renderer-portable
// (lowered to MIN/MAX over the order_by partition in v1).
export const COLUMN_AGGREGATE_FUNCTIONS = [
  "SUM", "COUNT", "AVG", "MIN", "MAX", "COUNT_DISTINCT", "FIRST", "LAST",
] as const;
export type ColumnAggregateFunction = typeof COLUMN_AGGREGATE_FUNCTIONS[number];

export type TransformAuthor =
  | "po_hint"
  | "ai_suggestion"
  | "engineer"
  | "steward_catalog";

export interface MappingSource {
  uri: string | null;
  schema: string | null;
  table: string | null;
  name: string | null;
  dataType: string | null;
  description: string | null;
}

// Shape returned by GET /api/projects/{id}/reviews/mappings/source-columns.
// Both UnmappedColumnsPanel (creating a mapping from scratch) and
// MappingReviewPanel (editing the source set of an existing mapping)
// consume it. URI prefix (column: vs dprod:col:) tells the caller which
// underlying Neo4j label backed each row.
//
// `table_description` and `relationship_kind` come from the column's parent
// :Dataset / :DProdOutputDataset and let the source-picker UI surface
// context that disambiguates similarly-named columns across tables.
export interface SourceColumn {
  uri: string;
  /** Dotted display name (schema.table.column) — convenient for rows. */
  name: string;
  data_type: string | null;
  description: string | null;
  table_description?: string | null;
  relationship_kind?: string | null;
  /** PO-approved outgoing FK relationship descriptions on this column's
   *  dataset (from metadata-enrichment's write_relationship_descriptions).
   *  Each entry names the target table + a semantic nature
   *  (belongs_to / categorises / audit_log_for / references) + the prose
   *  description. Mapping skill and source picker can render this to
   *  disambiguate which side of a FK to traverse. Empty list when the
   *  PO has not yet approved any relationship descriptions for this
   *  dataset. */
  outgoing_relationships?: Array<{
    nature: string;
    to_schema: string;
    to_table: string;
    text: string;
  }>;
  /** Phase 1 sensitivity enum — surfaced as a chip per row. */
  sensitivity?: string | null;
  /** Decomposed parts so the TransformEditor lookup arm can offer table /
   *  column pickers backed by the same payload — derive distinct
   *  `<schema>.<table>` for the table dropdown and filter columns by the
   *  selected table for the key/value/order-by-column dropdowns.
   *  Optional for back-compat with older /source-columns payloads. */
  table_schema?: string | null;
  table_name?: string | null;
  column_name?: string | null;
}

export interface PendingMapping {
  mapping_uri: string;
  status?: string;  // pending_review | approved | rejected | steward_review
  similarity_score: number | null;
  rationale: string | null;
  mapping_type: string | null;  // legacy: "direct" or "derived"

  // Structured transformation contract (added by Phase 1 backend).
  transform_kind: TransformKind | null;
  transform_expression: string | null;
  // JSON-encoded arrays/objects: parse on first read in the editor.
  transform_inputs_json: string | null;
  transform_params_json: string | null;
  transform_decorators_json: string | null;
  transform_author: TransformAuthor | null;
  transform_confidence: number | null;
  transform_escalation_reason: string | null;

  // Phase 3 of implementingdatatransformations.md — aggregation. Both fields
  // are no-ops at the SELECT level unless the product :DatasetTransform has
  // grouping_keys set; the renderer wraps non-grouping columns in agg_fn().
  aggregate_function: string | null;
  grouping_key: boolean | null;

  // First-source flat fields (preserved for backward-compatible single-source UI).
  source_schema: string;
  source_table: string;
  source_col_uri: string;
  source_col_name: string;
  source_col_type: string;
  source_description: string | null;
  // Structured PII/sensitivity of the source column ('none' when unset).
  source_sensitivity?: string | null;
  // A1: recommended protective transform when a sensitive source column is
  // mapped without mask/hash/suppress. Null when none is needed.
  recommended_protection?: {
    kind: TransformKind;
    transform_params: Record<string, unknown>;
    transform_inputs: string[];
    reason: string;
    basis: "sensitivity" | "heuristic";
  } | null;

  // Full source list — multi-source derived mappings have N entries.
  // Single-source mappings have exactly one entry mirroring the flat fields.
  sources: MappingSource[];

  product_uri: string;
  product_name: string;
  product_col_uri: string;
  product_col_name: string;
  product_col_description: string | null;

  // Snapshot of the AI's original transform fragments, populated from the
  // earliest :ProvActivity with priorAuthor='ai_suggestion' attached to the
  // mapping. Null when the mapping was never AI-suggested OR has not yet been
  // replaced (i.e. transform_author still === 'ai_suggestion'). When present
  // alongside transform_author === 'engineer', the reviewer is looking at a
  // mapping that explicitly replaced the AI's pick.
  original_ai_suggestion: {
    transform_kind: TransformKind | null;
    transform_expression: string | null;
    transform_inputs_json: string | null;
    transform_params_json: string | null;
    transform_decorators_json: string | null;
  } | null;
}

export interface ProductColumn {
  uri: string;
  name: string;
  description: string | null;
}

export const DESCRIPTION_REJECTION_CATEGORIES = [
  { value: "incorrect_meaning", label: "Incorrect meaning" },
  { value: "too_vague", label: "Too vague" },
  { value: "too_specific", label: "Too specific" },
  { value: "incorrect_constraint", label: "Incorrect constraint" },
  { value: "incorrect_values", label: "Incorrect values" },
  { value: "incorrect_fk_reference", label: "Incorrect FK reference" },
  { value: "other", label: "Other" },
];

// DQ test run types

export type EvidenceKind = "test" | "profile" | "rule" | "meta";

export interface DQTestRun {
  id: number;
  stage_run_id: number | null;
  framework: string;
  batch_id: string;
  started_at: string | null;
  completed_at: string | null;
  total_expectations: number;
  successful: number;
  unsuccessful: number;
  tables_tested: number;
  pass_rate: number;
  results_path: string;
  status: string;
}

export interface TestRunSummary {
  has_runs: boolean;
  batch_id?: string;
  executed_at?: string | null;
  framework?: string;
  total_expectations?: number;
  successful?: number;
  unsuccessful?: number;
  pass_rate?: number;
  rules_total?: number;
  rules_tested?: number;
  coverage?: number;
}

export type Tier = 1 | 2 | 3;

// Chat types

export interface ChatSessionInfo {
  id: number;
  project_id: number;
  title: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface ChatToolEvent {
  type: "tool_use";
  tool?: string;
  id?: string;
  input?: Record<string, unknown>;
}

export interface ChatMessageInfo {
  id: number;
  session_id: number;
  role: "user" | "assistant";
  content: string;
  tool_events: ChatToolEvent[];
  created_at: string | null;
}

export interface ChatWSMessage {
  type: "turn_started" | "text_delta" | "tool_use" | "thinking" | "chat_complete" | "turn_complete" | "error";
  text?: string;
  tool?: string;
  id?: string;
  input?: Record<string, unknown>;
  session_id?: number;
  project_code?: string;
  cost_usd?: number;
  duration_ms?: number;
  is_error?: boolean;
  message?: string;
}

export const MAPPING_REJECTION_CATEGORIES = [
  { value: "edited_default", label: "Edited the default" },
  { value: "incorrect_mapping", label: "Incorrect mapping" },
  { value: "incomplete_transformation", label: "Incomplete transformation" },
  { value: "wrong_target_column", label: "Wrong target column" },
  { value: "too_low_confidence", label: "Too low confidence" },
  { value: "no_match_exists", label: "No match exists" },
  { value: "duplicate_mapping", label: "Duplicate mapping" },
  { value: "other", label: "Other" },
];

export interface UnmappedColumn {
  product_uri: string;
  product_name: string;
  dataset_name: string | null;
  column_uri: string;
  column_name: string;
  data_type: string | null;
  logical_type: string | null;
  description: string | null;
  // Decoded server-side from :DProdColumn.transformHint when present.
  transform_hint?: {
    kind?: string;
    inputs?: string[];
    separator?: string;
    params?: Record<string, unknown>;
    decorators?: { standardization?: string[]; default_if_null?: string };
    expression?: string;
  } | null;
}

export const TRANSFORM_KIND_LABELS: Record<TransformKind, string> = {
  direct: "Direct (1:1)",
  cast: "Cast (type conversion)",
  format: "Format (case / date / trim)",
  concat: "Concatenate columns",
  split: "Split string",
  substring: "Substring",
  case: "Case / when",
  arithmetic: "Arithmetic",
  lookup: "Lookup table",
  literal: "Literal value (constant)",
  expression: "Raw SQL expression",
  bucket: "Bucket (binning into bands)",
  mask: "Mask (format-preserving redaction)",
  hash: "Hash (irreversible digest)",
  window: "Window function (LAG / RANK / running aggregate / …)",
  date_difference: "Date difference (age / elapsed — portable)",
};

// Reasons the engineer can pick when escalating a mapping to the steward.
// These appear in the `note` of the escalation_reason text.
export const TRANSFORM_ESCALATION_REASONS = [
  { value: "needs_reference_data", label: "Needs reference data (lookup table)" },
  { value: "ambiguous_business_logic", label: "Ambiguous business logic" },
  { value: "unclear_column_semantics", label: "Unclear column semantics" },
  { value: "missing_source_column", label: "Missing source column" },
  { value: "other", label: "Other" },
];

export const TRANSFORM_AUTHOR_LABELS: Record<TransformAuthor, string> = {
  po_hint: "PO hint",
  ai_suggestion: "AI suggestion",
  engineer: "Engineer",
  steward_catalog: "Steward catalog",
};

// Decorator option lists for the editor.
export const STANDARDIZATION_OPTIONS = [
  { value: "trim", label: "Trim whitespace" },
  { value: "upper", label: "Uppercase" },
  { value: "lower", label: "Lowercase" },
  { value: "normalize_whitespace", label: "Collapse whitespace" },
];


// ═══════════════════════════════════════════════════════════════════════
// Estate / Discovery feature (discovery table + estate graph + import).
// Appended from the estate fork. ProjectInfo already exists above (extended
// with the dmig_* fields); VariantScorecardResult intentionally omitted.
// ═══════════════════════════════════════════════════════════════════════
// ── Object-grain estate inventory (the unified discovery-table + DAG feed) ──
// DAG node type (colours the node); `object_type` is the report-facing label.
export type EstateNodeType = "database" | "table" | "query" | "view" | "snapshot" | "report" | "product";
export type EstateStatus = "fresh" | "warn" | "stale";  // data freshness (dot)
export type EstateDisposition = "modernize" | "migrate" | "retire" | "remain";

// A single data-bearing column sample + its optional profiling snapshot.
export interface SampleColumn { name: string; type: string; profile?: Record<string, number | boolean> }
export type SampleRow = Record<string, string | number | boolean | null>;

// ── Estate objects as a discriminated union on `type` ──
// Every estate object shares BaseEstateObject; the type-specific fields live only
// on the variant they make sense for (columns on data-bearing objects, code/run
// metadata on jobs, consumer metadata on reports, …). Narrow on `.type` — or use
// the `isDataBearing` / `isCodeObject` guards below — to reach the extra fields.
export interface BaseEstateObject {
  id: string;
  type: EstateNodeType;      // the discriminant (overridden by each variant)
  name: string;              // display name (schema-qualified where applicable)
  object_name: string;       // bare object name (report column)
  object_type: string;       // USER_TABLE / VIEW / MATERIALIZED VIEW / JOB / REPORT
  domain: string;
  application: string;
  instance: string;
  database: string;
  schema: string;
  size_gb: number;
  active: boolean;
  phi_pii: boolean;
  compatibility: string;     // Compatible | Incompatible
  // Destination PLATFORM for migrate/modernize (free string: "Databricks",
  // "Snowflake", "BigQuery", …) — required for migrate so the plan can name a
  // destination; the backend resolver reads it. For retire it's a fate
  // ("Decommission" | "Archive"). Not read by the frontend.
  target: string;
  disposition: EstateDisposition;   // canonical 4-way state (source of truth: DAG ring + chip + the "recommendation" table column derives from this)
  reads: string[];
  team: string;
  platform?: string;
  status: EstateStatus;
  metric: string;
  panel_rows: [string, string][];
  panel_note?: string;
  migration_approach?: string;   // present ⇒ actionable (row/node click spawns)
  // "core" = the curated, dispositioned working set (drives the DAG's unfocused
  // view + the tuned product/cluster demo); "discovered" = generated halo shown
  // in Object Lineage + the full table. Defaults to "core" server-side.
  scope?: "core" | "discovered";
}

// Mock sample data for data-bearing objects — drives the DAG sample-data strip
// + seeds discovery on cluster-create.
export interface DataBearingFields {
  sample_columns?: SampleColumn[];
  sample_rows?: SampleRow[];
}
// The legacy engine a to-be-migrated object's code/definition currently lives in
// (Teradata, Hive, SAS) — the DAG node face + transpile source dialect.
export interface CodeObjectFields {
  legacy_platform?: string;
  sample_source?: string;
}

export interface TableObject extends BaseEstateObject, DataBearingFields { type: "table" }
export interface ViewObject extends BaseEstateObject, DataBearingFields, CodeObjectFields { type: "view" }
export interface SnapshotObject extends BaseEstateObject, DataBearingFields {
  type: "snapshot";
  last_run?: string;
}
export interface QueryObject extends BaseEstateObject, CodeObjectFields {
  type: "query";
  // Run metadata for query/job objects (shown on the DAG node face).
  last_run?: string;        // "Today 06:41", "14 days ago", "Never"
  adhoc?: boolean;          // true ⇒ unscheduled / manually-triggered job
}
export interface ReportObject extends BaseEstateObject, DataBearingFields {
  type: "report";
  // A report exposes attributes (its output columns) via sample_columns, but is a
  // consumer, not a stored dataset — so it's not part of `isDataBearing`.
  // Consumer metadata for report objects (Pipeline DAG V2 report detail card).
  consumer_kind?: string;   // e.g. Application | Report
  access_method?: string;   // e.g. Direct DB Query | Governed Extract
  user_id?: string;
  last_run?: string;
}
// A source database / platform hub node — no columns, code or disposition of its
// own; it just anchors the tables beneath it.
export interface DatabaseObject extends BaseEstateObject { type: "database" }
// A (proposed or existing) governed data product node.
export interface ProductObject extends BaseEstateObject, DataBearingFields { type: "product" }

// One physical estate object — discriminated on `type`.
export type EstateObject =
  | TableObject | ViewObject | SnapshotObject | QueryObject
  | ReportObject | DatabaseObject | ProductObject;

// Variants that carry column samples (table / view / snapshot / product). Use the
// guard so `sample_columns` narrows without hand-written `.type` checks.
export type DataBearingObject = TableObject | ViewObject | SnapshotObject | ProductObject;
const DATA_BEARING_TYPES: ReadonlySet<EstateNodeType> = new Set(["table", "view", "snapshot", "product"]);
export function isDataBearing(o: EstateObject): o is DataBearingObject {
  return DATA_BEARING_TYPES.has(o.type);
}
// Variants that carry a code/definition artifact (view / query).
export type CodeObject = ViewObject | QueryObject;
export function isCodeObject(o: EstateObject): o is CodeObject {
  return o.type === "view" || o.type === "query";
}
// Column samples for ANY object that carries them (everything but a database
// hub), narrowing on the property so call sites don't need a `.type` check.
export function columnsOf(o: EstateObject): SampleColumn[] {
  return "sample_columns" in o && o.sample_columns ? o.sample_columns : [];
}

// Canonical reference data product model (Pipeline DAG V2). Report attributes
// are matched against these governed target schemas.
export interface ReferenceModel {
  id: string;
  name: string;
  domain: string;
  kind: string;
  description: string;
  attributes: { name: string; type: string; concept?: string }[];
}

export interface InventoryResponse {
  scenario: string;
  description: string;
  domain: string;
  objects: EstateObject[];
  // Published source-aligned data products, rendered on the DAG (not the table)
  // as green product nodes hanging off their source table.
  product_nodes?: EstateObject[];
  edges: [string, string][];
  facets: Record<string, string[]>;
  // Whether the estate came from a per-project import or the demo fixture —
  // drives the "sample estate" banner + import CTA on the Discovery view.
  source?: "imported" | "fixture";
}

// LLM (+ heuristic) proposal for a data product composed from a DAG selection.
// Returned by /discovery/propose-product-from-cluster and carried through
// the DAG preview into the wizard prefill.
export interface ClusterProposal {
  name: string;
  domain: string;
  idea: string;
  archetype: string;   // "dpe-sa" (source-aligned) | "dpe-cf" (consumer-aligned)
  purpose?: string;    // concrete business outcome, from downstream reports
  dataset_name?: string;
  // Schema seed flattened from the cluster's upstream source-table columns —
  // consumer wizard hydrates these as customColumns so step 4 lands populated.
  columns?: {
    name: string;
    logical_type: string;
    physical_type: string;
    description: string;
    primary_key: boolean;
  }[];
  // Published source products the cluster's upstream tables resolve to (the same
  // green product nodes the DAG shows wired in). Consumer wizard hydrates these
  // as selectedSourceInputs so match-inputs preselects them by exact dprod_uri
  // (domain-agnostic) instead of guessing by text/domain similarity.
  inputs?: {
    dprod_uri: string;
    contract_id: string;
    name: string;
  }[];
  // Raw legacy source tables behind the cluster (name + columns). Offered in
  // the modernization-seeded wizard as selectable RAW input references — these
  // have no governed product yet, so they are NOT :CONSUMES'd; the engineer
  // maps from the raw catalog. Kept separate from `inputs` (products).
  source_tables?: {
    name: string;
    columns?: { name: string; type?: string }[];
  }[];
  // Optional column-similarity breakdown of the selected sources vs the
  // reference data product this preview consolidates into. Computed client-side
  // (basic heuristics) in ModernizePanel and rendered as a radar in the
  // DAG preview panel. Illustrative only — never persisted or sent to the wizard.
  similarity?: ProductSimilarity;
}

export interface ProductSimilarity {
  title: string;
  subtitle?: string;
  match: number;   // 0-100 aggregate
  // value is null when an axis is "unknown" (e.g. S4/S5 needs profiling data
  // the product doesn't have) — rendered as "n/a" rather than a faked number.
  // value is null when an axis wasn't measured; `reason` says why.
  dimensions: { key: string; label: string; value: number | null; hint: string; reason?: string }[];
  variant?: string;              // scorer variant that produced this
  embeddings_available?: boolean; // false = semantic axis used the token fallback
}
