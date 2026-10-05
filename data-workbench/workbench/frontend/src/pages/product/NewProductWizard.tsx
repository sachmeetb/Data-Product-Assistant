import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import api from "../../api/client";
import { useCurrentUserEmail, useCurrentUserName } from "../../AuthContext";
import { productTheme } from "../../theme";
import ProductChatPanel, { type GuideMeRequest, type AppliedSuggestion } from "../../components/chat/ProductChatPanel";
import GuideMeButton from "../../components/chat/GuideMeButton";
import DiscoveryPanel, {
  type DiscoveryResult,
  type MatchingTemplate,
} from "../../components/wizard/DiscoveryPanel";
import EditDiffPanel from "../../components/wizard/EditDiffPanel";
import ImpactPreviewPanel from "../../components/ImpactPreviewPanel";
import type { SpecSnapshot as EditDiffSpecSnapshot } from "../../lib/editDiffImpact";
import { titleCaseDomain } from "../../lib/domainLabel";
import OsiAnalysisPanel from "../../components/OsiAnalysisPanel";
import PreflightPanel from "../../components/PreflightPanel";
import QuestionsPanel from "../../components/QuestionsPanel";
import ServingStrategyAdvice from "../../components/ServingStrategyAdvice";
import ResolveAndBindSourcesStep from "./shared/ResolveAndBindSourcesStep";
import ProductKindChip from "../../components/ProductKindChip";
import { allSlotsMatched, hydrateSlotFromMatch } from "./shared/resolveSlotHelpers";
import GapAnalysisSection from "./shared/GapAnalysisSection";
import { useConfirm } from "../../components/dialogContext";
import type { ServingMode } from "../../lib/servingModes";
import type { GapAnalysisResponse, ResolveSlot } from "../../types";

interface CatalogSummary {
  domain: string;
  label?: string;
  description: string;
  column_count: number;
}

interface CatalogColumn {
  name: string;
  logical_type: string;
  physical_type: string;
  primary_key: boolean;
  description: string;
  recommended_rules: Array<Record<string, unknown>>;
  // common  — canonical column from common.yaml (id, created_at, ...)
  // domain  — domain-specific source-system column
  // derived — consumer-aligned derivation template (time bucket,
  //           per-period aggregate, ranking, ratio, SCD-2 dating).
  //           Authored in common.yaml or per-domain catalog with
  //           `source_category: derived`.
  source_category: "common" | "domain" | "derived";
  // When set (e.g. "scd2"), this column only makes sense under a matching
  // dataset-level scd_policy. The advisor gates it out of recommendations
  // otherwise; the Shape step renders a "NEEDS SCD-2" chip if it's selected
  // while shapeScdPolicy disagrees.
  requires_scd_policy?: string;
}

interface CatalogDetail {
  domain: string;
  description: string;
  columns: CatalogColumn[];
}

// Per-column annotation from the schema-advisor's Step 3→4 re-fire (POST
// /api/domain-catalogs/{domain}/recommend with full context). Drives picker
// ordering, the (?) rationale tooltip, and the feasibility chip rendered on
// each selected row + the "Not sourceable from your inputs" sub-section.
//
// When feasibility=="sourced" everywhere (or column_details is empty —
// happens on first fire before source inputs are picked), the wizard hides
// all feasibility surfacing so we don't display UI for an empty signal.
interface ColumnDetail {
  relevance: number;             // [0,100]
  why: string;                   // one-sentence rationale
  feasibility: "sourced" | "derivable" | "missing_source";
  // Grain alignment against the declared shape.grain. Drives the GRAIN
  // MISMATCH / ROLLUP NEEDED chips in the picker. "aligned" / "unknown"
  // render no chip — the latter is the default when no grain is declared
  // (the backend forces it everywhere so the chip stays dormant).
  grain_alignment: "aligned" | "coarser" | "rollup_required" | "finer" | "unknown";
  source_evidence: string[];     // "<product>.<source_column>" hints
}

interface RecommendResponse {
  columns: string[];
  rationale: string;
  dropped: number;
  column_details: Array<{ name: string } & ColumnDetail>;
  source_input_count: number;
  source_column_count: number;
  error: string | null;
}

interface CustomColumnTransformHint {
  kind: string;             // "concat" | "cast" | "format" | "lookup" | ...
  inputs: string[];         // expected source-column names (logical, not URIs)
  separator?: string;       // concat separator
  params?: Record<string, unknown>;
  decorators?: { standardization?: string[]; default_if_null?: string };
  expression?: string;      // optional pre-filled SQL fragment
}

interface CustomColumn {
  name: string;
  logical_type: string;
  physical_type: string;
  description: string;
  primary_key: boolean;
  transform?: CustomColumnTransformHint;
}

interface SpecProperty {
  name?: string;
  physicalName?: string;
  logicalType?: string;
  physicalType?: string;
  description?: string;
  primaryKey?: boolean;
}

// Step 6 "Operations & Support" row shapes (ODCS servers / SLA / team / roles).
interface ServerRow {
  name: string;
  environment: string;
  type: string;
  account: string;
  database: string;
  schema: string;
  datasets: string[];
}
interface SlaRow { property: string; value: string; unit: string; inheritedFrom?: string; }
interface TeamRow { name: string; role: string; email: string; }
interface RoleRow { role: string; access: string; description: string; }

interface RejectionInfo {
  category: string | null;
  reason: string | null;
  by: string | null;
  at: string | null;
}

const REJECTION_LABELS: Record<string, string> = {
  missing_context: "Missing domain context",
  too_broad: "Schema too broad",
  too_narrow: "Schema too narrow",
  unclear_purpose: "Unclear purpose",
  unclear_quality_rules: "Data quality requirements unclear",
  duplicate: "Duplicate of existing product",
  out_of_scope: "Out of scope",
  other: "Other",
};

interface SuggestedRule {
  rule_uri: string;
  dprod_col_uri: string;
  col_name: string;
  dataset_physical_name: string;
  rule_type: string;
  severity: string;
  description: string;
  params: Record<string, unknown>;
  /** "playbook/domain_catalogs/<domain>.yaml" for catalog rules,
   *  "user" for chat-driven rule_create or PO-authored rules. */
  source: string;
  /** Current :PropertyShape.status as of this fetch. 'pending_review' on a
   *  brand-new product, 'approved' / 'rejected' when the wizard is re-opened
   *  in edit mode and the PO has prior decisions in the graph. */
  status?: string;
}

interface SettingsResponse {
  neo4j_host: string;
  neo4j_port: number;
  neo4j_user: string;
  neo4j_password: string;
  neo4j_database: string;
}

// One published product available as an upstream input — source, aggregate, or
// consumer (a consumer may build on any published product; multi-hop chains are
// supported). Fetched from GET /api/marketplace (no product_kind filter; scoped
// to the domain client-side) and persisted into ODCS spec.inputs[] with the same
// shape; the backend MERGEs a :CONSUMES edge per item.
interface SourceInputCandidate {
  dprod_uri: string;
  contract_id: string;
  name: string;
  description?: string | null;
  column_count?: number | null;
  // 'source' | 'aggregate' | 'consumer' — a consumer may build on any of them.
  product_kind?: string;
  // Contract lifecycle: only DEPLOYED_STATES are bindable (the save-time DAG
  // guard rejects consuming a not-yet-deployed product). PRE_DEPLOY_STATES are
  // surfaced disabled with a "Deploy first" note rather than hidden.
  lifecycle_state?: string;
}

// A product is bindable (consumable) only once it has a published/superseded
// version — the same test the marketplace listing and the backend DAG guard
// (_contract_versioning.CONSUMABLE_LIFECYCLE_STATES) pin to. Products still on
// their way (approved / in-engineering / …) are shown disabled so the PO can
// see them and knows to Deploy them first, instead of them silently vanishing.
const DEPLOYED_STATES = new Set(["published", "superseded"]);
const PRE_DEPLOY_STATES = new Set([
  "approved",
  "in_engineering",
  "submitted",
  "ingesting",
]);

// Subset of /api/marketplace product row we need for the Inputs picker.
interface MarketplaceSourceRow {
  uri: string;
  contract_id: string | null;
  name: string;
  description: string | null;
  column_count: number;
  product_kind: string;
  domain?: string | null;
  lifecycle_state?: string | null;
}

type Step = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10;
// Two source-input touchpoints: an OPTIONAL mid-wizard step 3 ("Suggest
// candidate sources") that informs the schema advisor's feasibility
// ranking, and a REQUIRED step 9 ("Confirm candidate sources") that's the
// final submit gate. Both bind to the same wizardSlots state — the PO can
// declare candidates early or defer until the end.
const STEP_LABELS: Record<Step, string> = {
  1: "Describe & Choose Domain",
  2: "Shape",
  3: "Suggest candidate sources",
  4: "Shape the Schema",
  5: "Product Details",
  6: "Operations & Support",
  7: "Rule Coach",
  8: "Readiness Review",
  9: "Confirm candidate sources",
  10: "Submitted",
};

// Lifecycle states where opening an existing product defaults to read-only
// "View" mode. These are exactly the non-draft states where a save would
// BRANCH a new contract version (v(n+1)) — so paging through the wizard just
// to demo the config must not write. Draft/submitted/rejected open editable
// (their saves are in-place, low-risk). Mirrors the backend boundary in
// `_contract_versioning.determine_save_mode` (DRAFT_STATES).
const VIEW_DEFAULT_STATES = new Set(["published", "approved", "superseded"]);

export default function NewProductWizard() {
  const navigate = useNavigate();
  const confirm = useConfirm();
  const CURRENT_USER_EMAIL = useCurrentUserEmail();
  const CURRENT_USER_NAME = useCurrentUserName();
  const { projectId: editingProjectIdParam } = useParams<{ projectId?: string }>();
  const editingProjectId = editingProjectIdParam ? parseInt(editingProjectIdParam) : null;
  const isEditMode = editingProjectId !== null;
  const [searchParams] = useSearchParams();

  // `?step=N` deep-link — used by the PO acknowledgement flow on
  // source-candidate-needed requests to jump straight to step 9 (Confirm
  // candidate sources). Clamped to legal range; ignored on fresh sessions.
  const initialStepFromUrl = (() => {
    const raw = searchParams.get("step");
    if (!raw) return null;
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 1 || n > 10) return null;
    return n as Step;
  })();
  // `?from_request=ID` — engineer's source-candidates-needed request id.
  // On next saveDraft we POST a resolution back so the request flips to
  // 'complete' once new candidates land in the spec.
  const fromRequestParam = searchParams.get("from_request");
  const fromRequestId = fromRequestParam ? Number(fromRequestParam) : null;
  const resolvedFromRequestRef = useRef<boolean>(false);

  // `?consume=<dprod_uri>&from_spec=<spec_id>` — the feasibility `adaptable` deep-link
  // (build_action's wizard_path). Seeds the upstream product to CONSUME so the pick
  // isn't dropped. One-shot; new-mode only (edit hydrates from the saved spec).
  const consumeParam = searchParams.get("consume");
  const seededConsumeRef = useRef<boolean>(false);
  // `?from_spec=<spec_id>` — the feasibility `adaptable` deep-link also carries the
  // reference spec the verdict agreed on. New-mode only; seeds the schema so Step 4
  // confirms the agreed columns instead of the advisor's generic catalog set.
  const fromSpecParam = searchParams.get("from_spec");
  const seededFromSpecRef = useRef<boolean>(false);

  const [step, setStep] = useState<Step>(initialStepFromUrl ?? 1);
  const [hydrating, setHydrating] = useState<boolean>(isEditMode);
  const [savingDraft, setSavingDraft] = useState(false);

  // Step 1
  const [catalogs, setCatalogs] = useState<CatalogSummary[]>([]);
  const [domain, setDomain] = useState<string>("");
  const [productIdea, setProductIdea] = useState<string>("");
  const [hoveredDomain, setHoveredDomain] = useState<string | null>(null);
  const [catalogDetailCache, setCatalogDetailCache] = useState<Record<string, CatalogDetail>>({});
  // Selectable readiness rubric. 'osi' is the recommended default; the
  // alternative shipped today is 'ai_ready'. Hydrated from
  // spec.scoringRubric in edit mode; persisted via buildSpec() →
  // _save_odcs_to_graph → :DataContract.scoringRubric.
  const [selectedRubric, setSelectedRubric] = useState<string>("osi");
  // Catalog of rubrics fetched from /api/scoring-rubrics. Used to render
  // the picker cards and labels.
  const [rubricCatalog, setRubricCatalog] = useState<Array<{
    id: string;
    label: string;
    short_label: string;
    description: string;
  }>>([]);
  // True once the contract has moved past draft — locks the rubric picker
  // so we don't fragment :OsiEvaluation history. Wizard renders a badge
  // instead of the radio cards in this case.
  const [rubricLocked, setRubricLocked] = useState<boolean>(false);

  // Step 2
  const [catalogDetail, setCatalogDetail] = useState<CatalogDetail | null>(null);
  const [selectedColumns, setSelectedColumns] = useState<Set<string>>(new Set());
  const [customColumns, setCustomColumns] = useState<CustomColumn[]>([]);
  const [draft, setDraft] = useState<CustomColumn>(blankCustomColumn());
  // Inline error for the "Add a custom column" form — set when the chosen
  // name collides with an existing catalog pick or custom column.
  const [addColumnError, setAddColumnError] = useState<string | null>(null);
  const [deriveExpandedIdx, setDeriveExpandedIdx] = useState<number | null>(null);
  /** Free-text filter on the "Add from catalog" picker (Step 2). Matches
   *  name + description. Only used when the picker is expanded. */
  const [catalogPickerQuery, setCatalogPickerQuery] = useState<string>("");
  /** Whether the "Add from catalog" picker is expanded. Default collapsed
   *  so the recommended columns are the focal point. */
  const [catalogPickerOpen, setCatalogPickerOpen] = useState<boolean>(false);
  /** Per-column annotations from the Step 3→4 advisor re-fire. Keyed by
   *  column name. See ColumnDetail. */
  const [columnDetails, setColumnDetails] = useState<Record<string, ColumnDetail>>({});
  /** True when shape/inputs/envelope have changed since the last advisor
   *  call. Pulses a "refresh recommendations" hint next to the button.
   *  Debounced so it doesn't flap on every keystroke in name/description. */
  const [recommendationsStale, setRecommendationsStale] = useState<boolean>(false);
  /** In-flight flag for the manual Refresh button + Step 3→4 auto-refire. */
  const [refreshingRecommendations, setRefreshingRecommendations] = useState<boolean>(false);
  /** Last advisor rationale text — shown as small caption under the
   *  Recommended columns header so the PO can see the lens the advisor
   *  inferred. Cleared when discovery re-fires from Step 1. */
  const [recommendationsRationale, setRecommendationsRationale] = useState<string>("");
  /** Names the PO explicitly removed from selectedColumns during this wizard
   *  session. Sticky — a Refresh / Step 3→4 re-fire never re-adds them, so
   *  the PO doesn't have to keep dismissing the same column. Cleared only
   *  when the idea changes (Step 1→2 fresh discovery) or a template is
   *  cloned. The badge surfaces "(Z previously removed)" so the PO can
   *  opt back in by manually picking them. */
  const [userRemovedColumns, setUserRemovedColumns] = useState<Set<string>>(new Set());
  /** Toggle: when true, the per-column rationale (`columnDetails[name].why`)
   *  is rendered inline under each column in both the Recommended section
   *  and the picker. When false, falls back to a `(?)` glyph with the
   *  rationale in a hover tooltip. Default ON because the rationales are
   *  the whole point of the shape-aware re-fire — hiding them defeats it. */
  const [showRationales, setShowRationales] = useState<boolean>(true);
  /** Step 1 → Step 2 transition state: true while the discovery endpoint
   *  is running. Renders an intermediate analyzing panel that blocks Step 2
   *  entry until results land (or fails). */
  const [transitioning, setTransitioning] = useState<boolean>(false);
  const [transitionError, setTransitionError] = useState<string | null>(null);
  /** Discovery payload from /discover. When at least one similar product or
   *  matching template comes back, the wizard renders DiscoveryPanel as an
   *  intermediate step before Step 2 so the PO sees reuse options first.
   *  Both lists empty → the wizard auto-advances with the recommended
   *  columns and never shows the panel. */
  const [discoveryResult, setDiscoveryResult] = useState<DiscoveryResult | null>(null);
  /** Set when the PO clones from a template so Step 3 can render a
   *  "Started from <template_id>" banner. Cleared on Back-to-Step-1 or when
   *  starting fresh. */
  const [templateApplied, setTemplateApplied] = useState<{ id: string } | null>(null);
  /** Spec properties captured during edit-mode hydration, waiting for the
   *  catalog fetch to finish so we can split into selected vs custom. */
  const [pendingHydrationProperties, setPendingHydrationProperties] = useState<SpecProperty[] | null>(null);
  /** Rejection details surfaced to the PO when they reopen a rejected
   *  product. Cleared once they submit a revision. */
  const [rejection, setRejection] = useState<RejectionInfo | null>(null);
  const [rejectionDismissed, setRejectionDismissed] = useState(false);
  // Captured from /api/projects/{id}/odcs at hydration time. Drives the
  // "editing a deployed product" banner that warns the PO their first save
  // will branch a new draft version while the deployed v1 stays live.
  const [hydratedLifecycleState, setHydratedLifecycleState] = useState<string | null>(null);
  // True when the wizard is confirming an ALREADY-AGREED schema — either an
  // intake-scaffolded edit-mode draft (seeded from feasibility via Product
  // Assembly / the /act path) OR an adaptable-from-spec new-mode session
  // (`?from_spec=`). In this mode Step 4 is a confirm surface: the auto-advisor
  // is suppressed (it would union-add generic catalog columns that were never
  // part of the agreed set), the copy is reframed, and the advisor is exposed as
  // an explicit opt-in Refresh instead.
  const [schemaConfirmMode, setSchemaConfirmMode] = useState(false);
  // Read-only "View" mode. Opening an existing product in a version-branching
  // state (published/approved/superseded) defaults to read-only so a demo
  // walk-through can't silently spawn v(n+1). Default to `isEditMode` (assume
  // read-only until hydration proves the state is drafty) — conservative and
  // safe. The PO clicks "Enable editing" to opt in, which sets this false.
  const [viewOnly, setViewOnly] = useState<boolean>(isEditMode);
  const [editingDeployedDismissed, setEditingDeployedDismissed] = useState(false);
  // Frozen snapshot of the deployed contract for diffing in the wizard's
  // EditDiffPanel. Captured once at hydration; never mutates afterwards.
  const [deployedSnapshot, setDeployedSnapshot] = useState<EditDiffSpecSnapshot | null>(null);
  // PO must explicitly acknowledge the diff before submitting an edit.
  const [editChangesAcknowledged, setEditChangesAcknowledged] = useState(false);
  // Phase 1 change-management: PO authors revision notes that ride on the
  // :ContractVersion sidecar (or :ProvActivity ContractPatch for cosmetic
  // patches). changeKindOverride is non-null only when the PO explicitly
  // forced a kind that differs from the system recommendation.
  const [revisionNotes, setRevisionNotes] = useState<string>("");
  const [changeKindOverride, setChangeKindOverride] = useState<"cosmetic" | "schema" | "breaking" | null>(null);

  // Step 7 (Source Inputs, post-reorder) — bind upstream source-aligned
  // products. `sourceInputs` is the marketplace pool fetched for the domain,
  // used by the "+ Add a source" affordance. `selectedSourceInputs` is the
  // canonical list of bindings that flows into ODCS spec.inputs[] (the
  // backend MERGEs a :CONSUMES edge per entry).
  //
  // The shared `ResolveAndBindSourcesStep` component renders `wizardSlots`,
  // which is kept in sync with `selectedSourceInputs` via a useEffect below.
  // Slots carry richer per-binding state (matcher rationale, gap suggestion,
  // spawned-source linkage) than the legacy flat list.
  const [sourceInputs, setSourceInputs] = useState<SourceInputCandidate[]>([]);
  const [selectedSourceInputs, setSelectedSourceInputs] = useState<SourceInputCandidate[]>([]);
  const [sourceInputsLoading, setSourceInputsLoading] = useState(false);
  const [sourceInputsError, setSourceInputsError] = useState<string | null>(null);
  const [wizardSlots, setWizardSlots] = useState<ResolveSlot[]>([]);
  const [wizardSlotsMatching, setWizardSlotsMatching] = useState(false);
  const [showAddSourcePicker, setShowAddSourcePicker] = useState(false);

  // Step 6 "Operations & Support" — ODCS operational fields the PO authors.
  // `servers` is read-only: auto-derived from the deployment on publish and
  // hydrated here so it round-trips through versioned saves. SLA / team /
  // roles are PO-editable. SLA defaults are inherited from the CONSUMES'd
  // source products (a consumer view has no independent refresh cycle).
  const [servers, setServers] = useState<ServerRow[]>([]);
  const [slaProps, setSlaProps] = useState<SlaRow[]>([]);
  const [teamMembers, setTeamMembers] = useState<TeamRow[]>([]);
  const [roles, setRoles] = useState<RoleRow[]>([]);
  const [slaSuggestLoading, setSlaSuggestLoading] = useState(false);
  const opsSeededRef = useRef<boolean>(false);
  // Step 8 pre-flight gap-analysis result, cached so it survives step-back
  // and so the submit soft-confirm dialog can read it. Cleared whenever the
  // PO modifies candidate sources (the result would be stale).
  const [gapAnalysisResult, setGapAnalysisResult] = useState<GapAnalysisResponse | null>(null);

  // Step 3 (Shape — Phases 2-5 dataset-shape authoring). Writes
  // schema-level transform block on the ODCS spec. Backend canonicalizer
  // (_canonicalize_dataset_transform) round-trips this to :DatasetTransform.
  //
  //   grain         — free-text prose like "one row per customer". Used as
  //                   PO context; grouping_keys is filled later (step 4) once
  //                   product columns are known.
  //   filter        — raw SQL fragment for filterPredicate
  //   scdPolicy     — 'latest_only' | 'scd2' | 'snapshot' | '' (none).
  //                   These are the canonical tokens every consumer reads
  //                   (Scd2PolicyPanel, buildSpec, the schema advisor's
  //                   requires_scd_policy gating, serving-strategy, the dbt
  //                   emitter). The radio MUST emit these exact values — a
  //                   non-canonical token (e.g. the old 'full_history') makes
  //                   the scd2-gated effective-date columns get suppressed
  //                   instead of recommended.
  const [shapeGrain, setShapeGrain] = useState<string>("");
  // shapeFilter holds the COMPILED SQL predicate (what the view uses). The PO
  // now authors plain language in shapeFilterIntent; "Check filter" interprets
  // it and sets shapeFilter silently. The engineer finalizes the SQL later.
  const [shapeFilter, setShapeFilter] = useState<string>("");
  const [shapeFilterIntent, setShapeFilterIntent] = useState<string>("");
  const [filterReadback, setFilterReadback] = useState<string>("");
  const [filterCheckConfidence, setFilterCheckConfidence] = useState<number | null>(null);
  const [filterCheckWarnings, setFilterCheckWarnings] = useState<string[]>([]);
  const [filterChecking, setFilterChecking] = useState<boolean>(false);
  const [filterCheckFallback, setFilterCheckFallback] = useState<boolean>(false);
  // Set when edit-mode hydrates a legacy product whose filter was authored as
  // raw SQL (no plain-language intent recorded).
  const [filterLegacyNote, setFilterLegacyNote] = useState<boolean>(false);
  const [shapeScdPolicy, setShapeScdPolicy] = useState<string>("");

  // Step 4 (Shape the Schema) — Phase 3 PO authoring. Set of product column
  // NAMES the PO has marked as GROUP BY keys. Flows into the schema-level
  // transform.grouping_keys list at save time; view-DDL's `grouped` CTE
  // fires whenever this is non-empty.
  const [groupingKeys, setGroupingKeys] = useState<Set<string>>(new Set());

  // Step 4 (Shape the Schema) — Phase 5 PO authoring. Set of product column
  // NAMES the PO has marked as suppressed from the materialized view. The
  // columns remain in the contract / graph (lineage, mapping intact) — only
  // the served view omits them. Flows into transform.suppressed_columns at
  // save time; view-DDL refuses to suppress PK columns regardless.
  const [suppressedColumns, setSuppressedColumns] = useState<Set<string>>(new Set());

  // Step 4 (Shape the Schema) — Phase 5 SCD-2 sub-policy authoring. Active
  // only when shapeScdPolicy === 'scd2'. Three fields:
  //   effective_column   — product column carrying start-of-validity
  //   expiration_column  — product column carrying end-of-validity
  //   add_is_current     — when true, view-DDL appends a derived is_current
  //                        boolean column computed from the expiration column
  // The wizard hides this block when SCD-2 isn't selected. View-DDL only
  // honors fields that resolve to a real product column; unknown names land
  // in summary['scd_warning'].
  const [scdEffectiveColumn, setScdEffectiveColumn] = useState<string>("");
  const [scdExpirationColumn, setScdExpirationColumn] = useState<string>("");
  const [scdAddIsCurrent, setScdAddIsCurrent] = useState<boolean>(false);
  // PO serving preference (Readiness step) — persisted to the contract's
  // customProperties and shown to the engineer as a recommendation.
  const [servingPreference, setServingPreference] = useState<ServingMode | null>(null);
  const [servingReason, setServingReason] = useState<string>("");

  // Step 4 (Shape the Schema) — Phase 6 window specs authoring. Each entry
  // is a named window definition the PO declares ahead of column-level
  // mappings (engineer references them via transformKind='window' +
  // transformParams.window=<name>). Stored as an array for UI ordering;
  // buildSpec flattens to the {name: spec} dict shape view-DDL expects.
  // partition_by + order_by columns may reference SOURCE column names that
  // aren't necessarily on the product schema — windows partition / order on
  // the underlying source, not the projected view.
  type WindowSpecDraft = {
    name: string;
    partition_by: string;  // comma-separated UI text; parsed at save time
    order_by: string;      // comma-separated "col [direction]" UI text
    frame: string;
  };
  const [windowSpecs, setWindowSpecs] = useState<WindowSpecDraft[]>([]);

  // Step 4 (Shape the Schema)
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [purpose, setPurpose] = useState("");
  // Free-form product tags (Product-Details step) → :DataContract.tags. Drive
  // the marketplace tag filter + group-by; editable later via re-open.
  const [tags, setTags] = useState<string[]>([]);
  const [tagDraft, setTagDraft] = useState("");
  // Autocomplete suggestions = union of tags already in the marketplace, so
  // the PO reuses existing labels instead of minting near-duplicates.
  const [tagSuggestions, setTagSuggestions] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    api.get("/api/marketplace").then((res) => {
      if (cancelled) return;
      const seen = new Map<string, string>();
      for (const p of (res.data?.products || [])) {
        for (const t of (p.tags || [])) {
          const key = String(t).trim().toLowerCase();
          if (key && !seen.has(key)) seen.set(key, String(t).trim());
        }
      }
      setTagSuggestions(Array.from(seen.values()).sort((a, b) => a.localeCompare(b)));
    }).catch(() => { /* suggestions are optional */ });
    return () => { cancelled = true; };
  }, []);
  const addTag = (raw: string) => {
    const label = raw.trim();
    if (!label) return;
    setTags((cur) => (cur.some((t) => t.toLowerCase() === label.toLowerCase()) ? cur : [...cur, label]));
    setTagDraft("");
  };
  const [datasetName, setDatasetName] = useState("");

  // Step 4 — wizard provisions its own project; we hold the id once created
  const [createdProjectId, setCreatedProjectId] = useState<number | null>(null);
  const [provisioningProject, setProvisioningProject] = useState(false);
  // This product's own contract id (edit mode only) — used to exclude it from
  // the upstream picker so a PO can't wire a self-consuming :CONSUMES edge (the
  // DAG guard would reject it at save; excluding it up front is clearer UX).
  const [selfContractId, setSelfContractId] = useState<string>("");

  // Product-Details intent (Phase 3 taxonomy): is this a reusable building block
  // meant to be consumed further (aggregate) or a fit-for-purpose leaf (consumer)?
  // Drives spec.productKind + the aggregate materialize-by-default serving
  // recommendation. PO-editable; auto-hinted from shape signals until touched.
  const [productKind, setProductKind] = useState<"aggregate" | "consumer">("consumer");
  const [productKindTouched, setProductKindTouched] = useState(false);
  const [rules, setRules] = useState<SuggestedRule[]>([]);
  /** Rules created by the PO during the wizard — chat-driven rule_create
   *  Apply or (later) a manual composer. Kept separate from catalog rules
   *  so the rules step can render them in their own group. They're persisted
   *  to Neo4j with ruleSource='user' as soon as Apply is clicked, so by the
   *  time finalize() runs they're indistinguishable from domain rules in
   *  the approve loop. */
  const [userRules, setUserRules] = useState<SuggestedRule[]>([]);
  const [rulesLoading, setRulesLoading] = useState(false);
  const [approvedRules, setApprovedRules] = useState<Set<string>>(new Set());
  const [rejectedRules, setRejectedRules] = useState<Set<string>>(new Set());

  // Step 5
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submittedRequestId, setSubmittedRequestId] = useState<number | null>(null);

  // Chat (Guide me) — sessions are now user-scoped and persisted server-side.
  // The user manages them via the session bar inside the panel; Guide-me
  // just drops a prefill into the active session's composer.
  const [chatOpen, setChatOpen] = useState(false);
  const [guideRequest, setGuideRequest] = useState<GuideMeRequest | null>(null);

  const getWizardContext = (): Record<string, unknown> => ({
    step,
    idea: productIdea,
    domain,
    selected_columns: Array.from(selectedColumns),
    custom_columns: customColumns,
    name,
    dataset_name: datasetName,
    description,
    purpose,
    // Dataset-level shape authoring (Phases 2-5). Mirrors the `shape_set`
    // suggestion payload the skill emits, so the assistant can read and
    // update the same envelope. Without this the Shape step's first
    // Guide-me call has no shape state to anchor on and tends to wander.
    shape: {
      grain: shapeGrain,
      filter: shapeFilter,
      scd_policy: shapeScdPolicy,
      scd_effective_column: scdEffectiveColumn,
      scd_expiration_column: scdExpirationColumn,
      scd_add_is_current: scdAddIsCurrent,
      grouping_keys: Array.from(groupingKeys),
      suppressed_columns: Array.from(suppressedColumns),
    },
    // Pending rules (present on step 4). The chat uses this to make
    // rule_decisions suggestions — the rule_uri in each decision maps
    // back to approvedRules / rejectedRules sets on Apply. User-authored
    // rules are merged in with source='user' so the chat doesn't propose
    // duplicates of rules the PO has already accepted via rule_create.
    pending_rules: [
      ...rules.map((r) => ({
        rule_uri: r.rule_uri,
        column: r.col_name,
        rule_type: r.rule_type,
        severity: r.severity,
        description: r.description,
        source: "domain",
        current_state: approvedRules.has(r.rule_uri)
          ? "approved"
          : rejectedRules.has(r.rule_uri)
          ? "rejected"
          : "undecided",
      })),
      ...userRules.map((r) => ({
        rule_uri: r.rule_uri,
        column: r.col_name,
        rule_type: r.rule_type,
        severity: r.severity,
        description: r.description,
        source: "user",
        current_state: approvedRules.has(r.rule_uri)
          ? "approved"
          : rejectedRules.has(r.rule_uri)
          ? "rejected"
          : "undecided",
      })),
    ],
  });

  const guideMe = (field: string, intent: string) => {
    const prefill = buildGuidePrefill(field, intent);
    setGuideRequest({ prefill, nonce: Date.now() });
    setChatOpen(true);
  };

  const handleApply = async (s: AppliedSuggestion): Promise<string> => {
    // Read-only View mode: block every chat "Apply" — both the graph-writing
    // kinds (rule_create, osi_*) and the local-state mutations (idea, schema,
    // shape, …). The PO must opt into editing first.
    if (viewOnly) return "Enable editing to apply changes";
    switch (s.applies_to) {
      case "idea":
        if (s.value) setProductIdea(s.value);
        return "Applied to idea";
      case "domain": {
        const picked = (s.value || "").trim().toLowerCase();
        const known = catalogs.map((c) => c.domain);
        if (!known.includes(picked)) return `No catalog for "${s.value}"`;
        setDomain(picked);
        return `Domain set to ${picked}`;
      }
      case "name":
        if (s.value) setName(s.value);
        return "Applied to product name";
      case "dataset_name":
        if (s.value) setDatasetName(s.value);
        return "Applied to dataset name";
      case "description":
        if (s.value) setDescription(s.value);
        return "Applied to description";
      case "purpose":
        if (s.value) setPurpose(s.value);
        return "Applied to purpose";
      case "schema_add_columns": {
        const incoming = (s.columns || []).map((c) => ({
          name: c.name || "",
          logical_type: c.logical_type || "string",
          physical_type: c.physical_type || "varchar(255)",
          description: c.description || "",
          primary_key: Boolean(c.primary_key),
        }));
        setCustomColumns((prev) => {
          const existingNames = new Set([
            ...Array.from(selectedColumns),
            ...prev.map((c) => c.name),
          ]);
          const deduped = incoming.filter((c) => c.name && !existingNames.has(c.name));
          return [...prev, ...deduped];
        });
        return `Added ${incoming.length} column${incoming.length === 1 ? "" : "s"}`;
      }
      case "schema_pick_columns": {
        const incoming = s.column_names || [];
        if (!catalogDetail) return "Catalog not loaded yet";
        const catalogNames = new Set(catalogDetail.columns.map((c) => c.name));
        const valid = incoming.filter((n) => catalogNames.has(n));
        const dropped = incoming.length - valid.length;
        setSelectedColumns(new Set(valid));
        const base = `Selected ${valid.length} catalog column${valid.length === 1 ? "" : "s"}`;
        return dropped > 0 ? `${base} (${dropped} not in catalog ignored)` : base;
      }
      case "rule_create": {
        const incoming = s.rules || [];
        if (incoming.length === 0) return "No rules to add";
        if (!createdProjectId) return "Reach the Rules step before adding rules";
        // Dedupe against rules we already track (catalog + user) on the
        // (column, rule_type) tuple — the chat shouldn't be able to drop
        // a duplicate notNull on the same column.
        const existing = new Set(
          [...rules, ...userRules].map((r) => `${r.col_name}::${r.rule_type}`)
        );
        const fresh = incoming.filter(
          (r) => r.column && r.rule_type && !existing.has(`${r.column}::${r.rule_type}`)
        );
        if (fresh.length === 0) return "All proposed rules already exist";
        try {
          const res = await api.post(
            `/api/projects/${createdProjectId}/odcs/persist-user-rules`,
            { rules: fresh, created_by: CURRENT_USER_EMAIL }
          );
          const persisted: SuggestedRule[] = (res.data.persisted || []).map((p: SuggestedRule) => ({
            ...p,
            source: "user",
          }));
          if (persisted.length === 0) {
            const skipped = res.data.skipped || [];
            return skipped.length > 0
              ? `Couldn't add: ${skipped[0].reason}`
              : "No rules persisted";
          }
          setUserRules((prev) => [...prev, ...persisted]);
          // PO is the author — auto-approve so they don't need to click
          // again in the rules list.
          setApprovedRules((prev) => {
            const next = new Set(prev);
            for (const r of persisted) next.add(r.rule_uri);
            return next;
          });
          return `Added ${persisted.length} rule${persisted.length === 1 ? "" : "s"}`;
        } catch (e) {
          return e instanceof Error ? `Failed: ${e.message}` : "Failed to persist rules";
        }
      }
      case "rule_decisions": {
        const decisions = s.decisions || [];
        const knownUris = new Set([...rules, ...userRules].map((r) => r.rule_uri));
        let approved = 0;
        let rejected = 0;
        setApprovedRules((prev) => {
          const next = new Set(prev);
          for (const d of decisions) {
            if (!knownUris.has(d.rule_uri)) continue;
            if (d.action === "approve") {
              next.add(d.rule_uri);
              approved++;
            } else {
              next.delete(d.rule_uri);
            }
          }
          return next;
        });
        setRejectedRules((prev) => {
          const next = new Set(prev);
          for (const d of decisions) {
            if (!knownUris.has(d.rule_uri)) continue;
            if (d.action === "reject") {
              next.add(d.rule_uri);
              rejected++;
            } else {
              next.delete(d.rule_uri);
            }
          }
          return next;
        });
        return `${approved} approved, ${rejected} rejected`;
      }
      case "column_transform_set": {
        // The chat declares a column-level derivation hint; we mirror it onto
        // matching customColumns. Net-new columns can be added separately via
        // schema_add_columns. If the named column doesn't exist yet, the apply
        // is rejected — POs add the column first, then ask for its derivation.
        const colName = (s.column || "").trim();
        if (!colName) return "No column named";
        if (!s.transform || !s.transform.kind) return "No transform kind";
        const match = customColumns.findIndex((c) => c.name === colName);
        if (match < 0) {
          // Selected-from-catalog columns aren't editable here; surface that.
          if (selectedColumns.has(colName)) {
            return `${colName} is a catalog column — derivations apply to custom columns only`;
          }
          return `No column named ${colName} in current schema`;
        }
        const hint: CustomColumnTransformHint = {
          kind: s.transform.kind,
          inputs: s.transform.inputs || [],
          separator: s.transform.separator,
          params: s.transform.params,
          decorators: s.transform.decorators,
          expression: s.transform.expression,
        };
        setCustomColumns((prev) => {
          const next = [...prev];
          next[match] = { ...next[match], transform: hint };
          return next;
        });
        return `Set ${hint.kind} derivation on ${colName}`;
      }
      case "shape_set": {
        // Dataset-level Shape step authoring. Partial-update semantics —
        // only fields present on the payload mutate wizard state; absent
        // keys preserve current values so a PO can incrementally narrow.
        const sh = s.shape || {};
        const applied: string[] = [];

        if (typeof sh.grain_prose === "string") {
          setShapeGrain(sh.grain_prose);
          applied.push(`grain="${sh.grain_prose.slice(0, 40)}${sh.grain_prose.length > 40 ? "…" : ""}"`);
        }
        if (typeof sh.filter_intent === "string") {
          // Preferred path: chat authored plain-language intent. Set the prose
          // and interpret it into a grounded predicate (PO sees the readback).
          setShapeFilterIntent(sh.filter_intent);
          setFilterLegacyNote(false);
          if (sh.filter_intent.trim()) {
            void checkFilter(sh.filter_intent);
            applied.push(`filter intent set ("${sh.filter_intent.slice(0, 40)}${sh.filter_intent.length > 40 ? "…" : ""}") — interpreting`);
          } else {
            setShapeFilter("");
            setFilterReadback("");
            applied.push("filter cleared");
          }
        } else if (typeof sh.filter === "string") {
          // Legacy path: chat set a raw SQL predicate directly. Keep it for
          // back-compat; the deploy-time safety gate validates it. The PO has
          // no readback here, so flag it as engineer-validated.
          setShapeFilter(sh.filter);
          applied.push(sh.filter ? `filter set (SQL — engineering will validate)` : `filter cleared`);
        }
        if (typeof sh.scd_policy === "string") {
          // Validate against the same set the wizard accepts. View-DDL only
          // lowers `latest_only` + `scd2` today; `snapshot` parses but
          // no-ops, so accept it but be loud if anything else.
          const valid = new Set(["", "latest_only", "snapshot", "scd2"]);
          if (!valid.has(sh.scd_policy)) {
            return `Unknown scd_policy "${sh.scd_policy}" — expected latest_only / snapshot / scd2`;
          }
          setShapeScdPolicy(sh.scd_policy);
          // Bare string with type='scd2' clears any prior sub-fields; the
          // chat is signaling "pick the policy" without naming columns.
          if (sh.scd_policy === "scd2") {
            setScdEffectiveColumn("");
            setScdExpirationColumn("");
            setScdAddIsCurrent(false);
          }
          applied.push(sh.scd_policy ? `scd=${sh.scd_policy}` : `scd cleared`);
        } else if (sh.scd_policy && typeof sh.scd_policy === "object") {
          // Object form — chat is naming the SCD-2 columns. Validate the
          // type, then push the sub-fields. Unknown column names get
          // filtered against current schema state (PO can still see them
          // in the chat note).
          const obj = sh.scd_policy;
          const valid = new Set(["latest_only", "snapshot", "scd2"]);
          if (!obj.type || !valid.has(obj.type)) {
            return `Unknown scd_policy.type "${obj.type}" — expected latest_only / snapshot / scd2`;
          }
          setShapeScdPolicy(obj.type);
          if (obj.type === "scd2") {
            const knownNamesLocal = new Set<string>([
              ...Array.from(selectedColumns),
              ...customColumns.map((c) => c.name),
            ]);
            const eff = obj.effective_column;
            const exp = obj.expiration_column;
            const effOk = eff && knownNamesLocal.has(eff);
            const expOk = exp && knownNamesLocal.has(exp);
            setScdEffectiveColumn(effOk ? eff : "");
            setScdExpirationColumn(expOk ? exp : "");
            setScdAddIsCurrent(!!obj.add_is_current && !!expOk);
            const scdParts: string[] = ["scd=scd2"];
            if (effOk) scdParts.push(`effective=${eff}`);
            else if (eff) scdParts.push(`effective ignored: "${eff}" not in schema`);
            if (expOk) scdParts.push(`expiration=${exp}`);
            else if (exp) scdParts.push(`expiration ignored: "${exp}" not in schema`);
            if (obj.add_is_current && expOk) scdParts.push("add_is_current=true");
            applied.push(scdParts.join("; "));
          } else {
            // Non-scd2 object form is unusual but tolerated. Clear scd2
            // sub-fields so they don't leak.
            setScdEffectiveColumn("");
            setScdExpirationColumn("");
            setScdAddIsCurrent(false);
            applied.push(`scd=${obj.type}`);
          }
        }

        // For grouping_keys / suppressed_columns we filter against the
        // current product columns so a typo or stale name doesn't poison
        // the Set. Existing schema-side state is the source of truth; chat
        // is the authoring surface, not the schema definer.
        const knownNames = new Set<string>([
          ...Array.from(selectedColumns),
          ...customColumns.map((c) => c.name),
        ]);
        const pkNames = new Set<string>([
          ...((catalogDetail?.columns || [])
            .filter((c) => c.primary_key && selectedColumns.has(c.name))
            .map((c) => c.name)),
          ...customColumns.filter((c) => c.primary_key).map((c) => c.name),
        ]);

        if (Array.isArray(sh.grouping_keys)) {
          const valid = sh.grouping_keys.filter((n): n is string => typeof n === "string" && knownNames.has(n));
          const dropped = sh.grouping_keys.length - valid.length;
          setGroupingKeys(new Set(valid));
          applied.push(
            `group_by=[${valid.join(", ")}]${dropped > 0 ? ` (${dropped} unknown ignored)` : ""}`
          );
        }
        if (Array.isArray(sh.suppressed_columns)) {
          const present = sh.suppressed_columns.filter(
            (n): n is string => typeof n === "string" && knownNames.has(n)
          );
          const unknownCount = sh.suppressed_columns.length - present.length;
          // PKs can't be suppressed (view-DDL refuses anyway) — strip with
          // a clear note so the PO sees why their suggestion was trimmed.
          const pkBlocked = present.filter((n) => pkNames.has(n));
          const final = present.filter((n) => !pkNames.has(n));
          setSuppressedColumns(new Set(final));
          const notes: string[] = [];
          if (pkBlocked.length > 0) notes.push(`${pkBlocked.length} PK skipped`);
          if (unknownCount > 0) notes.push(`${unknownCount} unknown ignored`);
          applied.push(
            `suppress=[${final.join(", ")}]${notes.length ? ` (${notes.join(", ")})` : ""}`
          );
        }

        if (applied.length === 0) return "No shape fields provided";
        return `Shape: ${applied.join("; ")}`;
      }
      case "osi_metric_create": {
        if (!createdProjectId) return "Reach Step 6 before adding OSI metrics";
        const name = (s.value || "").trim() ||
          // The advisor sometimes puts the name in `value` and sometimes
          // in a hand-rolled "name" property when rendering — accept both.
          ((s as unknown as { name?: string }).name || "").trim();
        const expression = (s.expression || "").trim();
        if (!name || !expression) return "Metric needs name and expression";
        try {
          await api.post(`/api/projects/${createdProjectId}/osi/metrics`, {
            name,
            expression,
            dialect: s.dialect || "ANSI_SQL",
            description: s.value || s.rationale || "",
          });
          return `Added OSI metric ${name}`;
        } catch (e) {
          return e instanceof Error ? `Failed: ${e.message}` : "Failed to add metric";
        }
      }
      case "osi_relationship_create": {
        if (!createdProjectId) return "Reach Step 6 before adding OSI relationships";
        const name = (s.value || "").trim() ||
          ((s as unknown as { name?: string }).name || "").trim();
        if (!name || !s.from_dataset || !s.to_dataset) {
          return "Relationship needs name, from_dataset, and to_dataset";
        }
        if (!(s.from_columns?.length) || !(s.to_columns?.length)) {
          return "Relationship needs from_columns and to_columns";
        }
        try {
          await api.post(`/api/projects/${createdProjectId}/osi/relationships`, {
            name,
            from_dataset: s.from_dataset,
            to_dataset: s.to_dataset,
            from_columns: s.from_columns,
            to_columns: s.to_columns,
          });
          return `Added OSI relationship ${name}`;
        } catch (e) {
          return e instanceof Error ? `Failed: ${e.message}` : "Failed to add relationship";
        }
      }
      case "osi_ai_context_set": {
        if (!createdProjectId) return "Reach Step 6 before setting AI context";
        if (!s.instructions && !s.synonyms?.length && !s.examples?.length) {
          return "AI context needs instructions, synonyms, or examples";
        }
        try {
          await api.post(`/api/projects/${createdProjectId}/osi/ai-context`, {
            instructions: s.instructions || null,
            synonyms: s.synonyms || [],
            examples: s.examples || [],
          });
          return "AI context set";
        } catch (e) {
          return e instanceof Error ? `Failed: ${e.message}` : "Failed to set AI context";
        }
      }
      // Phase 5: chat assistant can draft release notes for the wizard.
      // Hydrates the same revisionNotes state ImpactPreviewPanel writes to.
      case "revision_notes": {
        const notes = (s.value || "").trim();
        if (!notes) return "No notes provided";
        setRevisionNotes(notes);
        return "Revision notes set";
      }
      // Phase 5: chat assistant can hint the change kind based on the
      // diff it sees. Pre-sets the override so the user can confirm/edit
      // it in the ImpactPreviewPanel before saving.
      case "change_kind": {
        const kind = (s.value || "").trim().toLowerCase();
        if (!["cosmetic", "schema", "breaking"].includes(kind)) {
          return `Unknown change kind: ${s.value}`;
        }
        setChangeKindOverride(kind as "cosmetic" | "schema" | "breaking");
        return `Change kind set to ${kind}`;
      }
      default:
        return "Applied";
    }
  };

  useEffect(() => {
    api.get("/api/domain-catalogs").then((res) => {
      setCatalogs(res.data.catalogs || []);
    });
  }, []);

  // Fetch the rubric catalog once for the Step 1 picker. Doesn't depend on
  // project state — same list for every wizard mount.
  useEffect(() => {
    api
      .get("/api/scoring-rubrics")
      .then((res) => {
        const rubrics = (res.data?.rubrics || []) as Array<{
          id: string;
          label: string;
          short_label: string;
          description: string;
        }>;
        setRubricCatalog(rubrics);
      })
      .catch(() => {
        // Fall back to the OSI-only single-card if the catalog endpoint is
        // unreachable — keeps the picker functional rather than blank.
        setRubricCatalog([
          { id: "osi", label: "OSI Readiness", short_label: "OSI", description: "Open Semantic Interchange readiness." },
        ]);
      });
  }, []);

  // Edit mode: hydrate wizard state from the existing contract. We use
  // project.domain to drive the catalog fetch, then sort each spec column
  // into selected_columns (matches the catalog) or custom_columns (PO-added).
  useEffect(() => {
    if (!editingProjectId) return;
    let cancelled = false;
    (async () => {
      try {
        const [projectRes, odcsRes] = await Promise.all([
          api.get(`/api/projects/${editingProjectId}`),
          api.get(`/api/projects/${editingProjectId}/odcs`),
        ]);
        if (cancelled) return;
        const proj = projectRes.data;
        const spec = odcsRes.data?.spec;
        setCreatedProjectId(editingProjectId);
        setSelfContractId(
          (spec?.id || (proj?.project_code ? `${proj.project_code}-contract` : "")) as string
        );
        // Hydrate the taxonomy intent so an edit-mode reopen keeps the PO's
        // prior aggregate/consumer choice (touched=true stops the auto-hint).
        setProductKind((spec?.productKind || "").toLowerCase() === "aggregate" ? "aggregate" : "consumer");
        setProductKindTouched(true);
        const d: string = (proj?.domain || spec?.domain || "").toLowerCase();
        if (d) setDomain(d);
        setName(spec?.name || "");
        setDescription(spec?.description || "");
        setPurpose(spec?.purpose || "");
        setTags(Array.isArray(spec?.tags) ? (spec.tags as unknown[]).map((t) => String(t)) : []);
        // Hydrate the rubric selection from the contract (defaults to 'osi'
        // for pre-rubric contracts). Lock the picker for non-draft
        // lifecycle states so we don't fragment :OsiEvaluation history.
        const persistedRubric = (spec?.scoringRubric || "osi") as string;
        setSelectedRubric(persistedRubric);
        const lifecycleState = (proj?.lifecycle_state || spec?.status || "").toLowerCase();
        const isDraftish = !lifecycleState || lifecycleState === "draft" || lifecycleState === "ingesting";
        setRubricLocked(!isDraftish);
        // Default to read-only View mode for version-branching states; drafty /
        // submitted / rejected products open editable. The PO opts in via
        // "Enable editing". Use the ODCS endpoint's authoritative
        // `lifecycle_state` (same source as `hydratedLifecycleState` + the
        // deployed-edit banner) — NOT `spec.status`, which is the ODCS
        // contract status field and reads 'draft' even for an approved product.
        const viewLifecycle = (odcsRes.data?.lifecycle_state || proj?.lifecycle_state || spec?.status || "").toLowerCase();
        setViewOnly(VIEW_DEFAULT_STATES.has(viewLifecycle));
        const firstSchema = (spec?.schema || [])[0];
        setDatasetName(firstSchema?.physicalName || firstSchema?.name || (d ? `${d}_core` : ""));

        // Operations & Support (step 6) — hydrate so edits round-trip. Servers
        // are display-only (derived on deploy) but re-emitted by buildSpec so a
        // version branch doesn't drop the derived entry.
        setServers(
          (spec?.servers || []).map((s: Record<string, unknown>) => ({
            name: String(s.name || ""),
            environment: String(s.environment || ""),
            type: String(s.type || ""),
            account: String(s.account || ""),
            database: String(s.database || ""),
            schema: String(s.schema || ""),
            datasets: Array.isArray(s.datasets) ? (s.datasets as unknown[]).map(String) : [],
          }))
        );
        setSlaProps(
          (spec?.slaProperties || []).map((s: Record<string, unknown>) => ({
            property: String(s.property || ""),
            value: String(s.value ?? ""),
            unit: String(s.unit || ""),
          }))
        );
        setTeamMembers(
          (spec?.team || []).map((t: Record<string, unknown>) => ({
            name: String(t.name || ""),
            role: String(t.role || ""),
            email: String(t.email || ""),
          }))
        );
        setRoles(
          (spec?.roles || []).map((r: Record<string, unknown>) => ({
            role: String(r.role || ""),
            access: String(r.access || ""),
            description: String(r.description || ""),
          }))
        );
        // Anything hydrated counts as already-seeded — don't clobber with
        // defaults when the PO reaches step 6.
        if ((spec?.slaProperties || []).length || (spec?.team || []).length) {
          opsSeededRef.current = true;
        }

        // PO serving preference (Readiness step) — written to customProperties by
        // buildSpec; hydrate it back so an edit-mode reopen keeps the prior pick
        // (otherwise re-submit would drop it). Value set matches ServingMode.
        const cp = (spec?.customProperties || {}) as Record<string, unknown>;
        const prevServing = cp.po_serving_preference;
        if (prevServing === "virtual" || prevServing === "materialized"
            || prevServing === "lakehouse" || prevServing === "transfer") {
          setServingPreference(prevServing);
        }
        if (typeof cp.po_serving_reason === "string") {
          setServingReason(cp.po_serving_reason);
        }

        // Hydrate Shape step fields from the schema-level transform block,
        // if present. The backend canonicaliser preserves these on
        // round-trip; we just project them back into PO-facing state.
        const xform = (firstSchema?.transform || {}) as Record<string, unknown>;
        setShapeGrain(typeof xform.grain_prose === "string" ? xform.grain_prose : "");
        const hydratedFilter = typeof xform.filter === "string" ? xform.filter : "";
        const hydratedIntent = typeof xform.filter_intent === "string" ? xform.filter_intent : "";
        setShapeFilter(hydratedFilter);
        setShapeFilterIntent(hydratedIntent);
        // Legacy product: a compiled filter exists but no plain-language intent
        // was ever recorded. Flag it so the PO knows re-checking will replace it.
        setFilterLegacyNote(!!hydratedFilter && !hydratedIntent);
        setFilterReadback("");
        setFilterCheckConfidence(null);
        setFilterCheckWarnings([]);
        const scd = xform.scd_policy as Record<string, unknown> | undefined;
        setShapeScdPolicy(scd && typeof scd.type === "string" ? scd.type : "");
        // Phase 5 SCD-2 sub-fields — only meaningful when type='scd2', but
        // safe to hydrate unconditionally (the wizard hides them otherwise
        // and buildSpec only emits them in the type='scd2' path).
        setScdEffectiveColumn(scd && typeof scd.effective_column === "string" ? scd.effective_column : "");
        setScdExpirationColumn(scd && typeof scd.expiration_column === "string" ? scd.expiration_column : "");
        setScdAddIsCurrent(scd && scd.add_is_current === true);
        // Phase 3 grouping_keys — list of product column names. Restore so
        // the per-row "Group by" checkboxes pre-tick.
        const gks = xform.grouping_keys as unknown;
        if (Array.isArray(gks)) {
          setGroupingKeys(new Set(gks.filter((k): k is string => typeof k === "string")));
        }
        // Phase 5 suppressed_columns — list of product column names dropped
        // from the served view. Restore so the per-row "Suppress" checkboxes
        // pre-tick.
        const sup = xform.suppressed_columns as unknown;
        if (Array.isArray(sup)) {
          setSuppressedColumns(new Set(sup.filter((k): k is string => typeof k === "string")));
        }
        // Phase 6 window_specs — dict { name: {partition_by, order_by, frame} }.
        // Flatten to the wizard's array-of-drafts shape, joining the per-spec
        // structured lists back into the comma-separated UI text.
        const winSpecs = xform.window_specs as unknown;
        if (winSpecs && typeof winSpecs === "object" && !Array.isArray(winSpecs)) {
          const drafts: WindowSpecDraft[] = [];
          for (const [nm, spec] of Object.entries(winSpecs as Record<string, unknown>)) {
            if (!spec || typeof spec !== "object") continue;
            const s = spec as Record<string, unknown>;
            const partition = Array.isArray(s.partition_by) ? s.partition_by.filter((p): p is string => typeof p === "string") : [];
            const orderArr = Array.isArray(s.order_by) ? s.order_by : [];
            const orderStr = orderArr
              .map((ob) => {
                if (!ob || typeof ob !== "object") return "";
                const col = (ob as Record<string, unknown>).column;
                const dir = (ob as Record<string, unknown>).direction;
                if (typeof col !== "string" || !col) return "";
                return typeof dir === "string" && dir.toLowerCase() === "desc" ? `${col} desc` : col;
              })
              .filter(Boolean)
              .join(", ");
            drafts.push({
              name: nm,
              partition_by: partition.join(", "),
              order_by: orderStr,
              frame: typeof s.frame === "string" ? s.frame : "",
            });
          }
          setWindowSpecs(drafts);
        }

        // Figure out which properties are catalog-canonical vs custom.
        // We have to wait for the catalog fetch to complete before
        // splitting, so stash the spec properties for a follow-up effect.
        const seededProps = (firstSchema?.properties || []) as SpecProperty[];
        setPendingHydrationProperties(seededProps);
        // Confirm-only Step 4 for an intake-scaffolded draft carrying a seeded
        // schema. Gating on `seededProps.length > 0` is belt-and-suspenders: if a
        // seed ever failed (best-effort scaffold, or a legacy pre-seed project),
        // the advisor still fires rather than stranding the PO on an empty Step 4.
        setSchemaConfirmMode(proj?.parent_intake_submission_id != null && seededProps.length > 0);
        // inputs[] from the existing contract → seed the Inputs step. The
        // Step-2 fetch effect later confirms each entry is still in the
        // marketplace listing and drops stale ones.
        const specInputs = (spec?.inputs || []) as Array<{ dprod_uri?: string; contract_id?: string; name?: string }>;
        if (Array.isArray(specInputs) && specInputs.length > 0) {
          const seeded: SourceInputCandidate[] = specInputs
            .filter((i) => Boolean(i.dprod_uri))
            .map((i) => ({
              dprod_uri: i.dprod_uri || "",
              contract_id: i.contract_id || "",
              name: i.name || "",
            }));
          setSelectedSourceInputs(seeded);
        }
        // Bring the user to step 2 — domain is already locked and they
        // can tweak the schema or advance to details.
        setProductIdea(spec?.description || "");
        // Rejection banner: surface what the engineer said so the PO
        // knows what to change.
        if (odcsRes.data?.lifecycle_state === "rejected" && odcsRes.data?.rejection) {
          setRejection(odcsRes.data.rejection as RejectionInfo);
        }
        if (odcsRes.data?.lifecycle_state) {
          setHydratedLifecycleState(odcsRes.data.lifecycle_state);
        }
        // Snapshot the contract for the EditDiffPanel. We freeze a snapshot
        // when editing a state that branches on save (published / approved)
        // — those are the cases where the previous version is preserved and
        // a meaningful "before vs after" exists. Other states edit in place.
        if (odcsRes.data?.lifecycle_state === "published"
            || odcsRes.data?.lifecycle_state === "approved") {
          const props = (firstSchema?.properties || []) as SpecProperty[];
          // Snapshot inputs[] from the deployed contract so the diff panel
          // can show added/removed source products. spec.inputs[] is read
          // back from the graph by _read_odcs_from_graph (walks :CONSUMES);
          // empty for non-consumer-aligned products.
          const deployedInputsRaw = (spec?.inputs || []) as Array<{ dprod_uri?: string; name?: string }>;
          setDeployedSnapshot({
            name: spec?.name || "",
            description: spec?.description || "",
            purpose: spec?.purpose || "",
            datasetName: firstSchema?.physicalName || firstSchema?.name || "",
            columns: props.map((p) => ({
              name: p.name,
              physicalType: p.physicalType ?? null,
              logicalType: p.logicalType ?? null,
              description: p.description ?? null,
              primaryKey: p.primaryKey ?? false,
            })),
            inputs: deployedInputsRaw
              .filter((i) => Boolean(i.dprod_uri))
              .map((i) => ({ dprod_uri: i.dprod_uri || "", name: i.name || "" })),
          });
        }
      } catch (e) {
        console.error("Failed to hydrate wizard for edit", e);
      } finally {
        if (!cancelled) setHydrating(false);
      }
    })();
    return () => { cancelled = true; };
  }, [editingProjectId]);

  useEffect(() => {
    if (!domain) {
      setCatalogDetail(null);
      return;
    }
    const cached = catalogDetailCache[domain];
    const apply = (detail: CatalogDetail) => {
      setCatalogDetail(detail);
      // Default selection is empty for fresh wizard entries — the catalog
      // can be ~100+ columns, and "select all" overwhelms. The PO either
      // ticks columns manually or asks Guide me for a starter set
      // (which emits a schema_pick_columns Apply card).
      // Edit-mode hydration runs after this in a separate effect.
      if (pendingHydrationProperties == null) {
        setSelectedColumns(new Set());
      }
    };
    if (cached) {
      apply(cached);
    } else {
      api.get(`/api/domain-catalogs/${domain}`).then((res) => {
        const detail: CatalogDetail = res.data;
        setCatalogDetailCache((prev) => ({ ...prev, [domain]: detail }));
        apply(detail);
      });
    }
    if (!datasetName) setDatasetName(`${domain}_core`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domain]);

  // Second leg of edit-mode hydration: once the catalog is loaded, split
  // the spec's properties into selected (catalog-matching) vs custom
  // (PO-added). Clears the pending marker so we don't re-split on reload.
  useEffect(() => {
    if (!pendingHydrationProperties || !catalogDetail) return;
    const catalogNames = new Set(catalogDetail.columns.map((c) => c.name));
    const selected = new Set<string>();
    const custom: CustomColumn[] = [];
    for (const p of pendingHydrationProperties) {
      const nm = p.physicalName || p.name;
      if (!nm) continue;
      if (catalogNames.has(nm)) {
        selected.add(nm);
      } else {
        custom.push({
          name: nm,
          logical_type: p.logicalType || "string",
          physical_type: p.physicalType || "varchar(255)",
          description: p.description || "",
          primary_key: Boolean(p.primaryKey),
        });
      }
    }
    setSelectedColumns(selected);
    setCustomColumns(custom);
    setPendingHydrationProperties(null);
  }, [pendingHydrationProperties, catalogDetail]);

  // Source-input steps: fetch published products (any kind) for the chosen
  // domain each time the PO lands on the step. Re-runs if domain changes — empty
  // selectedSourceInputs if the prior selections aren't in the new domain.
  useEffect(() => {
    // Fetch when the user lands on either source-input step: the optional
    // mid-wizard "Suggest candidate sources" (step 3) OR the required
    // "Confirm candidate sources" (step 9). Both bind to wizardSlots. Gating on
    // step 9 (not 8) matters for the ?step=9 deep-link, which lands directly on
    // Confirm without passing through Readiness Review.
    if ((step !== 3 && step !== 9) || !domain) return;
    setSourceInputsLoading(true);
    setSourceInputsError(null);
    api
      .get<{ products: MarketplaceSourceRow[] }>(`/api/marketplace`)
      .then((r) => {
        // Any product (source / aggregate / consumer) is a candidate upstream —
        // multi-hop chains are supported. list_published has no server-side
        // domain filter, so scope by domain client-side; drop this product's own
        // contract so it can't consume itself. We keep BOTH deployed rows
        // (bindable) and pre-deploy rows (approved / in-engineering / … —
        // rendered disabled with a "Deploy first" note by AddSourceAffordance)
        // so not-yet-deployed products are visible instead of silently missing;
        // only truly-unfinished/dead states (draft, rejected, empty) are hidden.
        const rows = (r.data?.products || [])
          .filter((p) => {
            const ls = (p.lifecycle_state || "").toLowerCase();
            return DEPLOYED_STATES.has(ls) || PRE_DEPLOY_STATES.has(ls);
          })
          .filter((p) => !domain || (p.domain || "").toLowerCase() === domain.toLowerCase())
          .filter((p) => (p.contract_id || "") !== selfContractId)
          .map<SourceInputCandidate>((p) => ({
            dprod_uri: `dprod:${p.contract_id}`,
            contract_id: p.contract_id || "",
            name: p.name,
            description: p.description,
            column_count: p.column_count,
            product_kind: p.product_kind || "",
            lifecycle_state: (p.lifecycle_state || "").toLowerCase(),
          }))
          .filter((p) => Boolean(p.contract_id));
        setSourceInputs(rows);
        // Drop any prior selections no longer present in the fetched set
        // (domain change, deleted product, etc.).
        setSelectedSourceInputs((prev) => prev.filter((s) => rows.some((r2) => r2.dprod_uri === s.dprod_uri)));
      })
      .catch((e) => setSourceInputsError(`Couldn't load candidate products: ${e instanceof Error ? e.message : String(e)}`))
      .finally(() => setSourceInputsLoading(false));
  }, [step, domain, selfContractId]);

  // Auto-hint the product kind from shape signals until the PO overrides it (or
  // edit-mode hydration sets it): a product composed from 2+ upstream products
  // reads as a reusable building block (aggregate); a single-source /
  // fit-for-purpose product reads as a consumer leaf. Never fights a manual pick.
  useEffect(() => {
    if (productKindTouched || isEditMode) return;
    const distinctSources = new Set(selectedSourceInputs.map((s) => s.dprod_uri)).size;
    setProductKind(distinctSources >= 2 ? "aggregate" : "consumer");
  }, [selectedSourceInputs, productKindTouched, isEditMode]);

  // On source-input step entry, run the same classifier + matcher chain the
  // ingest path uses. Even when the PO didn't pick any sources earlier, the
  // classifier reads the wizard's in-flight spec (idea / description / schema)
  // and returns inferred upstream dependencies; the matcher then ranks
  // marketplace products against those hints. Re-entry preserves any existing
  // slot state (resolutions, spawned-request linkage) so step-back doesn't blow
  // away the PO's work.
  useEffect(() => {
    // Fire when entering either source-input step: optional mid-wizard
    // step 3 OR required "Confirm candidate sources" step 9. The wizardSlots
    // gate prevents duplicate fetches when stepping back and forward between
    // them. Gating on step 9 (not 8) matters for the ?step=9 deep-link.
    if (step !== 3 && step !== 9) return;
    if (wizardSlots.length > 0) return;

    // Build a partial ODCS-shaped spec from wizard state. The classifier
    // tolerates missing fields; only `name` + at least one schema entry are
    // required by the backend validator (we don't validate here — the spec
    // is just analysis input).
    const partialSpec: Record<string, unknown> = {
      name: name || productIdea.slice(0, 60) || "(unnamed consumer)",
      domain: domain || "",
      description: description || productIdea || "",
      purpose: purpose || "",
      schema: [
        {
          name: datasetName || (name || "main").toLowerCase().replace(/\s+/g, "_"),
          properties: [
            ...selectedCatalogColumns.map((c) => ({ name: c })),
            ...customColumns.map((c) => ({
              name: c.name,
              description: c.description,
            })),
          ],
        },
      ],
      inputs: selectedSourceInputs.map((s) => ({
        dprod_uri: s.dprod_uri,
        contract_id: s.contract_id,
        name: s.name,
      })),
    };

    setWizardSlotsMatching(true);
    (async () => {
      // Step A: if no explicit inputs were authored, ask the classifier what
      // upstream products this consumer plausibly depends on. Skipped when
      // the PO already declared bindings (edit-mode hydration) — those are
      // authoritative and the classifier would only add noise.
      let inferred: Array<{ name: string; domain?: string; encompasses?: string }> = [];
      if (selectedSourceInputs.length === 0) {
        try {
          const cls = await api.post<{
            inferred_dependencies?: Array<{ name: string; domain?: string; encompasses?: string }>;
          }>("/api/ingest-products/classify-archetype", { spec: partialSpec });
          inferred = cls.data?.inferred_dependencies || [];
        } catch {
          inferred = [];
        }
      }

      // Step B: rank marketplace candidates for each declared/inferred slot.
      try {
        const r = await api.post("/api/ingest-products/match-inputs", {
          spec: partialSpec,
          inferred_dependencies: inferred,
        });
        const slots = (r.data?.slots || []) as Parameters<typeof hydrateSlotFromMatch>[0][];
        setWizardSlots(slots.map(hydrateSlotFromMatch));
      } catch {
        // Matcher unavailable — fall back to synthesized slots for any
        // pre-existing selections so the PO still sees their bindings.
        if (selectedSourceInputs.length > 0) {
          setWizardSlots(
            selectedSourceInputs.map((s, idx) => ({
              slot_id: `seed-${idx}`,
              source: "spec_inputs",
              declared: { dprod_uri: s.dprod_uri, name: s.name },
              encompasses_hint: s.description || null,
              candidates: [
                {
                  uri: s.dprod_uri,
                  contract_id: s.contract_id,
                  name: s.name,
                  description: s.description,
                  match_score: 100,
                  match_kind: "exact_uri" as const,
                  rationale: "Pre-existing binding.",
                },
              ],
              preselected_candidate_uri: s.dprod_uri,
              gap_suggestion: null,
              resolution: "matched" as const,
              selected_uri: s.dprod_uri,
              selected_contract_id: s.contract_id,
              selected_name: s.name,
              spawned_request_id: null,
              spawned_project_id: null,
            })),
          );
        }
      } finally {
        setWizardSlotsMatching(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  // Sync matched slots → selectedSourceInputs so buildSpec keeps emitting the
  // right spec.inputs[]. Runs whenever slots change. Also invalidates the
  // gap-analysis cache — any change to the candidate set means the prior
  // analysis is stale.
  useEffect(() => {
    if (wizardSlots.length === 0) return;
    const next: SourceInputCandidate[] = wizardSlots
      .filter((s) => s.resolution === "matched" && !!s.selected_uri)
      .map((s) => ({
        dprod_uri: s.selected_uri || "",
        contract_id: s.selected_contract_id || "",
        name: s.selected_name || "",
      }));
    setSelectedSourceInputs(next);
    setGapAnalysisResult(null);
  }, [wizardSlots]);

  const handleSlotChange = (slotId: string, update: Partial<ResolveSlot>) => {
    setWizardSlots((prev) =>
      prev.map((s) => (s.slot_id === slotId ? { ...s, ...update } : s)),
    );
  };

  const handleCreateGap = (slot: ResolveSlot) => {
    // The wizard doesn't persist a draft to return to (in-memory state only),
    // so the spawned source product can't auto-relink. Prefill the source
    // wizard with the gap suggestion so the PO can author the missing product
    // and manually navigate back to this wizard afterwards.
    const qs = new URLSearchParams({
      prefill_idea: slot.gap_suggestion?.encompasses || slot.encompasses_hint || "",
      prefill_domain: slot.gap_suggestion?.domain || domain || "",
      prefill_name: slot.gap_suggestion?.name || slot.declared.name || "",
    });
    navigate(`/product/new/source?${qs.toString()}`);
  };

  const addSourceFromMarketplace = (src: SourceInputCandidate) => {
    // Append a fresh slot pre-bound to the picked marketplace product. The
    // sync effect above will mirror it into selectedSourceInputs.
    if (wizardSlots.some((s) => s.selected_uri === src.dprod_uri)) {
      // Already bound — ignore duplicate add.
      setShowAddSourcePicker(false);
      return;
    }
    const newSlot: ResolveSlot = {
      slot_id: `manual-${Date.now()}-${src.contract_id}`,
      source: "manual",
      declared: { dprod_uri: src.dprod_uri, name: src.name },
      encompasses_hint: src.description || null,
      candidates: [
        {
          uri: src.dprod_uri,
          contract_id: src.contract_id,
          name: src.name,
          description: src.description,
          product_kind: src.product_kind || "",
          match_score: 100,
          match_kind: "exact_uri",
          rationale: "Selected from the marketplace.",
        },
      ],
      preselected_candidate_uri: src.dprod_uri,
      gap_suggestion: null,
      resolution: "matched",
      selected_uri: src.dprod_uri,
      selected_contract_id: src.contract_id,
      selected_name: src.name,
      spawned_request_id: null,
      spawned_project_id: null,
    };
    setWizardSlots((prev) => [...prev, newSlot]);
    setShowAddSourcePicker(false);
  };

  const removeSlot = (slotId: string) => {
    setWizardSlots((prev) => prev.filter((s) => s.slot_id !== slotId));
  };

  // Feasibility `adaptable` deep-link seed: resolve `?consume=<uri>` to a marketplace
  // product and pre-bind it as an upstream source (was previously dropped). One-shot,
  // new-mode only, best-effort (a bad/absent uri is a silent no-op).
  useEffect(() => {
    if (!consumeParam || isEditMode || seededConsumeRef.current) return;
    seededConsumeRef.current = true;
    (async () => {
      try {
        const r = await api.get(`/api/marketplace/detail`, { params: { uri: consumeParam } });
        const d = r.data || {};
        if (!d.uri && !d.contract_id) return;
        addSourceFromMarketplace({
          dprod_uri: d.uri || consumeParam,
          contract_id: d.contract_id || "",
          name: d.name || consumeParam,
          description: d.description || null,
          product_kind: d.product_kind || "",
        });
      } catch { /* deep-link seed is best-effort */ }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [consumeParam, isEditMode]);

  // Feasibility `adaptable` deep-link seed (Path A′): resolve `?from_spec=<spec_id>`
  // to the reference spec's project-bound consumer ODCS and seed the wizard so Step 4
  // CONFIRMS the agreed schema (schemaConfirmMode) instead of the advisor's generic
  // catalog columns. New-mode only, one-shot, best-effort (a bad/absent spec id is a
  // silent no-op). Runs alongside the `?consume=` effect above, which binds the
  // matched upstream product; the two seed disjoint state.
  useEffect(() => {
    if (!fromSpecParam || isEditMode || seededFromSpecRef.current) return;
    seededFromSpecRef.current = true;
    (async () => {
      try {
        const r = await api.get(`/api/feasibility/specs/${encodeURIComponent(fromSpecParam)}/odcs`);
        const odcs = r.data || {};
        const firstSchema = (odcs.schema || [])[0] || {};
        const props = (firstSchema.properties || []) as SpecProperty[];
        if (props.length === 0) return;  // nothing to confirm → leave the fresh flow intact
        const d = String(odcs.domain || "").toLowerCase();
        setName(String(odcs.name || ""));
        setDescription(String(odcs.description || ""));
        setPurpose(String(odcs.purpose || odcs.description || ""));
        setProductIdea(String(odcs.description || ""));
        setProductKind(String(odcs.productKind || "").toLowerCase() === "aggregate" ? "aggregate" : "consumer");
        setProductKindTouched(true);
        setDatasetName(String(firstSchema.physicalName || firstSchema.name || (d ? `${d}_core` : "")));
        // Shape fields from the transform block (grain + SCD + grouping keys) —
        // same projection the edit-mode hydration applies.
        const xform = (firstSchema.transform || {}) as Record<string, unknown>;
        setShapeGrain(typeof xform.grain_prose === "string" ? xform.grain_prose : "");
        const scd = xform.scd_policy as Record<string, unknown> | undefined;
        setShapeScdPolicy(scd && typeof scd.type === "string" ? scd.type : "");
        setScdEffectiveColumn(scd && typeof scd.effective_column === "string" ? scd.effective_column : "");
        setScdExpirationColumn(scd && typeof scd.expiration_column === "string" ? scd.expiration_column : "");
        setScdAddIsCurrent(scd ? scd.add_is_current === true : false);
        const gks = xform.grouping_keys as unknown;
        if (Array.isArray(gks)) {
          setGroupingKeys(new Set(gks.filter((k): k is string => typeof k === "string")));
        }
        // Set pendingHydrationProperties TOGETHER with domain (same React batch) so
        // the catalog-load effect (keyed on domain) sees a non-null pending marker and
        // does NOT reset selectedColumns; the split effect then hydrates selected vs
        // custom exactly like an edit-mode reopen.
        setPendingHydrationProperties(props);
        setSchemaConfirmMode(true);
        if (d) setDomain(d);
      } catch { /* deep-link seed is best-effort */ }
    })();
  }, [fromSpecParam, isEditMode]);

  // Staleness watcher: when the PO changes inputs / shape / envelope while
  // sitting on Step 4, set recommendationsStale so the Refresh button pulses.
  // Debounced 1.5s so typing in name/description doesn't flap on every
  // keystroke. Only active on Step 4 — Steps 1-3 don't yet have a populated
  // columnDetails, and Step 5+ has moved on.
  const staleSourceInputsKey = selectedSourceInputs.map((s) => s.contract_id).sort().join("|");
  const staleGroupingKey = Array.from(groupingKeys).sort().join("|");
  const columnDetailsCount = Object.keys(columnDetails).length;
  useEffect(() => {
    // Staleness watcher fires on the Schema step (post mid-wizard insert:
    // Schema is now at step 4, after the optional Candidate sources at 3).
    if (step !== 4) return;
    if (columnDetailsCount === 0) return;
    const t = setTimeout(() => setRecommendationsStale(true), 1500);
    return () => clearTimeout(t);
  }, [
    step,
    columnDetailsCount,
    staleSourceInputsKey,
    shapeGrain,
    shapeFilter,
    shapeScdPolicy,
    staleGroupingKey,
    name,
    description,
    purpose,
  ]);

  const runDiscovery = async () => {
    // Skip discovery entirely when the schema is already populated
    // (edit-mode hydration, or the user navigated back from step 3) so we
    // don't blow away their existing selections by re-running the advisor.
    // schemaConfirmMode covers the adaptable-from-spec new-mode seed, whose
    // selectedColumns are hydrated asynchronously and might still be empty here.
    if (selectedColumns.size > 0 || customColumns.length > 0 || isEditMode || schemaConfirmMode) {
      setStep(2);
      return;
    }
    if (!productIdea.trim() || !domain) {
      setStep(2);
      return;
    }
    setTransitionError(null);
    setDiscoveryResult(null);
    setTransitioning(true);
    // Fresh discovery means the PO redefined intent — clear sticky removals
    // and any stale per-column annotations from a prior Step 3→4 fire.
    setUserRemovedColumns(new Set());
    setColumnDetails({});
    setRecommendationsRationale("");
    setRecommendationsStale(false);
    try {
      const res = await api.post(`/api/domain-catalogs/${domain}/discover`, {
        idea: productIdea.trim(),
      });
      const data = res.data as DiscoveryResult;
      const cols = data.recommended_columns || [];
      const hasMatches =
        (data.similar_products?.length || 0) + (data.matching_templates?.length || 0) > 0;
      if (!hasMatches) {
        // No reuse hits → behave exactly like the legacy /recommend flow:
        // pre-select the recommended columns and drop the PO into Step 2.
        setSelectedColumns(new Set(cols));
        if (data.error) {
          setTransitionError(data.error);
        } else if (cols.length === 0) {
          setTransitionError(
            "We couldn't suggest a starter set — pick what you need from the catalog or use Guide me."
          );
        }
        setTransitioning(false);
        setStep(2);
        return;
      }
      // Render DiscoveryPanel — the PO chooses between similar products,
      // templates, or "start fresh" before we leave Step 1.
      setDiscoveryResult(data);
      setTransitioning(false);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Discovery unavailable";
      setTransitionError(`Couldn't analyze your idea: ${msg}`);
      setTransitioning(false);
      setStep(2);
    }
  };

  /** Drop catalog columns whose `requires_scd_policy` disagrees with the currently
   *  authored SCD policy from the selection. Reversible (NOT recorded in
   *  userRemovedColumns) so flipping the policy back re-admits them on the next
   *  advisor fire. Shared by the advisor merge and the confirm-mode advance; a
   *  no-op for a faithful feasibility seed (its columns and its scd_policy are
   *  mutually consistent — feasibility_spec_to_odcs writes a matching
   *  transform.scd_policy). It only bites if the PO changes SCD policy on Step 2,
   *  where dropping the now-invalid SCD columns is correct and reversible. */
  const applyScdPrune = (): void => {
    const scdMismatch = new Set(
      (catalogDetail?.columns || [])
        .filter((c) => c.requires_scd_policy && c.requires_scd_policy !== shapeScdPolicy)
        .map((c) => c.name)
    );
    if (scdMismatch.size === 0) return;
    setSelectedColumns((prev) => {
      const next = new Set(prev);
      for (const nm of scdMismatch) next.delete(nm);
      return next;
    });
  };

  /** Step 3 → Step 4 transition: re-fire the schema advisor with the full
   *  context (idea + envelope + shape + source_contract_ids) so Step 4 opens
   *  with shape-aware ranking + per-column rationale + feasibility chips.
   *
   *  Behaviour:
   *  - Never wipes the existing selectedColumns / customColumns. Newly
   *    recommended names get added unless the PO previously removed them
   *    (userRemovedColumns is sticky).
   *  - Auto-expands the catalog picker when fresh ranking lands.
   *  - Advisor failure surfaces as a non-blocking banner above Step 4 — the
   *    wizard still advances and prior columnDetails (if any) are preserved.
   *  - Skipped only when editing a published/approved contract (the deployed
   *    schema is the source of truth and the advisor would noise up an
   *    already-curated picker). Draft re-entry still re-fires the advisor —
   *    the PO is mid-authoring and adding a new candidate source on step 3
   *    should produce fresh feasibility-aware ranking on step 4.
   *
   *  Reused by the manual "Refresh recommendations" button — same fetch,
   *  different setRefreshing flag.
   */
  const refreshShapeRecommendations = async (mode: "advance" | "manual"): Promise<void> => {
    const lcs = (hydratedLifecycleState || "").toLowerCase();
    const isDraftyEdit = !lcs || lcs === "draft" || lcs === "ingesting";
    if (isEditMode && !isDraftyEdit) return;
    if (!domain || !productIdea.trim()) return;
    setRefreshingRecommendations(true);
    setTransitionError(null);
    try {
      const res = await api.post(`/api/domain-catalogs/${domain}/recommend`, {
        idea: productIdea.trim(),
        name: name || null,
        description: description || null,
        purpose: purpose || null,
        shape: {
          grain: shapeGrain,
          filter: shapeFilter,
          scd_policy: shapeScdPolicy,
          grouping_keys: Array.from(groupingKeys),
        },
        source_contract_ids: selectedSourceInputs.map((s) => s.contract_id).filter(Boolean),
      });
      const data = res.data as RecommendResponse;
      if (data.error) {
        setTransitionError(data.error);
      }
      const recommended = Array.isArray(data.columns) ? data.columns : [];
      // SCD-policy-gated columns whose requirement disagrees with the authored
      // policy (e.g. effective_from/effective_to/is_current under latest_only).
      // The advisor already excludes these from `recommended`, but the discovery
      // pass (which runs before the Shape step) may have auto-selected them, and
      // the union-merge below never removes anything — so prune them here, on
      // the advisor fire, as the authoritative re-recommendation. Empty when the
      // policy is scd2 (requirement matches), so scd2 products keep their SCD
      // columns. NOT added to userRemovedColumns — pruning must be reversible so
      // switching back to scd2 re-adds them on the next fire.
      // Merge into selectedColumns: prune policy-mismatched SCD columns (shared with
      // the confirm-mode advance via applyScdPrune), then add new recommendations the
      // PO hasn't explicitly removed; never wipe other picks. Skip names the PO
      // already authored as custom columns — adding them here would put the same name
      // in both collections (duplicate render + duplicate spec property). The custom
      // authoring wins.
      applyScdPrune();
      const customNames = new Set(customColumns.map((c) => c.name));
      setSelectedColumns((prev) => {
        const next = new Set(prev);
        for (const nm of recommended) {
          if (userRemovedColumns.has(nm)) continue;
          if (customNames.has(nm)) continue;
          next.add(nm);
        }
        return next;
      });
      // Index column_details by name for O(1) lookup in the picker.
      const detailMap: Record<string, ColumnDetail> = {};
      for (const d of data.column_details || []) {
        if (!d || !d.name) continue;
        detailMap[d.name] = {
          relevance: d.relevance,
          why: d.why,
          feasibility: d.feasibility,
          grain_alignment: d.grain_alignment || "unknown",
          source_evidence: d.source_evidence || [],
        };
      }
      setColumnDetails(detailMap);
      setRecommendationsRationale(data.rationale || "");
      setRecommendationsStale(false);
      // Auto-expand the picker on a fresh re-fire so the PO sees the ranking
      // immediately. Manual Refresh leaves the picker state alone — if it
      // was already open it stays open; if not, the user can open it.
      if (mode === "advance") {
        setCatalogPickerOpen(true);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Recommendation service unavailable";
      setTransitionError(`Couldn't refresh recommendations: ${msg}`);
    } finally {
      setRefreshingRecommendations(false);
    }
  };

  /** Wraps Step 3 → Step 4 navigation. Transitions visually *first* so the PO
   *  sees immediate progress, then kicks off the advisor in the background.
   *  Step 4 itself renders an analyzing panel while refreshingRecommendations
   *  is true — without this fire-and-let-render-show-progress flow, the
   *  PO would stare at a frozen Step 3 for the 5–30s the advisor takes. */
  // Step 2 (Shape) advance — go to step 3 (optional Candidate sources).
  // No advisor call yet; that runs when leaving step 3 into Schema (step 4).
  const advanceFromShape = (): void => {
    setStep(3);
  };

  // OPTIONAL comprehension check: read the PO's plain-language filter back to
  // them so they can confirm we understood the intent. This deliberately does
  // NOT produce or persist a SQL predicate — at authoring time the product is
  // contract-first (no sources bound, no real column names), so any predicate
  // would be speculative. The engineer compiles the intent into grounded SQL
  // automatically at serving time. We only ever persist `filterIntent`.
  const checkFilter = async (intentOverride?: string): Promise<void> => {
    const intent = (intentOverride ?? shapeFilterIntent).trim();
    setFilterReadback("");
    setFilterCheckConfidence(null);
    setFilterCheckWarnings([]);
    setFilterCheckFallback(false);
    if (!intent) return;
    setFilterChecking(true);
    try {
      const columns = [
        ...Array.from(selectedColumns).map((name) => ({ name })),
        ...customColumns.map((c) => ({ name: c.name })),
      ];
      const res = await api.post<{
        readback: string; predicate: string; confidence: number;
        warnings: string[]; grounded_columns: string[]; _fallback?: boolean;
      }>(`/api/filter-intent/interpret`, {
        intent,
        columns,
        dialect: "postgres",
        project_id: createdProjectId ?? undefined,
      });
      setFilterReadback(res.data.readback || "");
      setFilterCheckConfidence(typeof res.data.confidence === "number" ? res.data.confidence : null);
      setFilterCheckWarnings(res.data.warnings || []);
      setFilterCheckFallback(!!res.data._fallback);
      // NB: intentionally do NOT setShapeFilter — the readback is a preview of
      // understanding only; the engineer produces the real predicate later.
      setFilterLegacyNote(false);
    } catch (e) {
      setFilterReadback("");
      setFilterCheckWarnings([e instanceof Error ? e.message : "Could not check the filter — try again."]);
    } finally {
      setFilterChecking(false);
    }
  };

  // Step 3 (optional Candidate sources) advance — go to step 4 (Schema) and
  // fire the schema advisor with the PO's candidate picks (if any) as
  // feasibility context. refreshShapeRecommendations already passes
  // `source_contract_ids` from selectedSourceInputs.
  const advanceToSchema = (): void => {
    setStep(4);
    // Confirm-only for a feasibility/intake-seeded schema: keep SCD hygiene but
    // suppress the auto-advisor (it would union-add generic catalog columns that
    // were never part of the agreed set). The PO can still opt into the advisor via
    // the Refresh button. Leave the catalog picker collapsed — the agreed columns
    // render regardless; the picker is the "add more" affordance.
    if (schemaConfirmMode) {
      applyScdPrune();
      setCatalogPickerOpen(false);
      return;
    }
    void refreshShapeRecommendations("advance");
  };

  /** "Continue with these columns" — same outcome as the legacy flow:
   *  recommended columns become the Step 2 selection. */
  const acceptRecommendation = () => {
    if (discoveryResult) {
      setSelectedColumns(new Set(discoveryResult.recommended_columns || []));
    }
    // Fresh acceptance clears any prior per-column annotations / removals
    // so the Step 3→4 re-fire starts clean.
    setUserRemovedColumns(new Set());
    setColumnDetails({});
    setDiscoveryResult(null);
    setStep(2);
  };

  /** Dismiss the discovery panel and go back to Step 1 (idea / domain). */
  const cancelDiscovery = () => {
    setDiscoveryResult(null);
    setTransitioning(false);
    setTransitionError(null);
  };

  /** Clone a template into the wizard. Properties from the template's first
   *  schema get split into selected (catalog-matching) vs custom by reusing
   *  the same hydration effect that handles edit mode. Step 3 fields are
   *  populated from the template's textual fields. */
  const useTemplate = (template: MatchingTemplate) => {
    const spec = template.spec || {};
    const schemas = (spec as { schema?: unknown }).schema;
    const firstSchema = Array.isArray(schemas) ? (schemas[0] as Record<string, unknown> | undefined) : undefined;
    const properties = (firstSchema?.properties as SpecProperty[] | undefined) || [];
    setPendingHydrationProperties(properties);
    const specName = (spec as { name?: string }).name;
    const specDescription = (spec as { description?: string }).description;
    const specPurpose = (spec as { purpose?: string }).purpose;
    if (specName) setName(specName);
    if (specDescription) setDescription(specDescription);
    if (specPurpose) setPurpose(specPurpose);
    const dsName = (firstSchema?.physicalName as string | undefined) || (firstSchema?.name as string | undefined);
    if (dsName) setDatasetName(dsName);
    setTemplateApplied({ id: template.template_id });
    // Template clone resets the curation slate — clear sticky removals and
    // stale annotations from any prior fresh-start path.
    setUserRemovedColumns(new Set());
    setColumnDetails({});
    setDiscoveryResult(null);
    setStep(2);
  };

  const prefetchCatalog = (d: string) => {
    if (catalogDetailCache[d]) return;
    api
      .get(`/api/domain-catalogs/${d}`)
      .then((res) => {
        setCatalogDetailCache((prev) => ({ ...prev, [d]: res.data }));
      })
      .catch(() => {});
  };

  // Names authored as custom columns. selectedColumns (catalog picks) and
  // customColumns MUST stay disjoint — both the render below (two separate
  // sections) and buildSpec (which concatenates the two lists into one
  // `properties` array) assume a name appears in exactly one. If a name leaks
  // into both — e.g. the schema-advisor re-fire on a step 3↔4 round-trip adds
  // a recommended catalog name that the PO had already authored as custom —
  // the column renders twice AND the ODCS spec gets two same-physicalName
  // properties (a duplicate :DProdColumn). Custom wins: it carries the PO's
  // authored description / transform, so we drop the overlap from the catalog
  // side here, which heals both the render and buildSpec in one place.
  const customColumnNames = useMemo(
    () => new Set(customColumns.map((c) => c.name)),
    [customColumns]
  );
  // Defensive name-dedup: catalogDetail.columns should already be unique by
  // name (the /domain-catalogs/{domain} endpoint dedups common vs domain
  // overlaps), but selection is a Set<name> and the render is keyed on c.name,
  // so a single duplicate name here would render the column twice AND make
  // buildSpec emit two same-physicalName properties (a duplicate :DProdColumn).
  // Keep the first occurrence so a stale cached catalog can't resurface the
  // ghost-row bug.
  const selectedCatalogColumns = useMemo(() => {
    const seen = new Set<string>();
    const out: CatalogDetail["columns"] = [];
    for (const c of catalogDetail?.columns || []) {
      if (!selectedColumns.has(c.name) || customColumnNames.has(c.name)) continue;
      if (seen.has(c.name)) continue;
      seen.add(c.name);
      out.push(c);
    }
    return out;
  }, [catalogDetail, selectedColumns, customColumnNames]);

  // Live snapshot of wizard state for EditDiffPanel. Combines catalog
  // selections + custom additions + the consumer-aligned inputs[] in the
  // same shape EditDiffPanel expects. inputs is empty for non-consumer
  // wizard runs which is the correct no-op signal for the diff lib.
  const currentSnapshot = useMemo<EditDiffSpecSnapshot>(() => ({
    name: name.trim(),
    description: description.trim(),
    purpose: purpose.trim(),
    datasetName: datasetName.trim(),
    columns: [
      ...selectedCatalogColumns.map((c) => ({
        name: c.name,
        physicalType: c.physical_type,
        logicalType: c.logical_type,
        description: c.description,
        primaryKey: c.primary_key,
      })),
      ...customColumns.map((c) => ({
        name: c.name,
        physicalType: c.physical_type,
        logicalType: c.logical_type,
        description: c.description,
        primaryKey: c.primary_key,
      })),
    ],
    inputs: selectedSourceInputs.map((s) => ({ dprod_uri: s.dprod_uri, name: s.name })),
  }), [name, description, purpose, datasetName, selectedCatalogColumns, customColumns, selectedSourceInputs]);

  const toggleColumn = (nm: string) => {
    setSelectedColumns((prev) => {
      const next = new Set(prev);
      if (next.has(nm)) {
        next.delete(nm);
        // Sticky: a deliberate removal means Refresh shouldn't silently
        // re-add this column. The PO can still pick it back manually from
        // the catalog picker (toggling it on clears it from userRemoved).
        setUserRemovedColumns((removed) => {
          if (removed.has(nm)) return removed;
          const r = new Set(removed);
          r.add(nm);
          return r;
        });
      } else {
        next.add(nm);
        setUserRemovedColumns((removed) => {
          if (!removed.has(nm)) return removed;
          const r = new Set(removed);
          r.delete(nm);
          return r;
        });
      }
      return next;
    });
  };

  const addCustomColumn = () => {
    const nm = draft.name.trim();
    if (!nm) return;
    // Keep selectedColumns (catalog) and customColumns disjoint, and don't add
    // a second custom column with the same name — either would render twice
    // and emit a duplicate spec property. Mirrors the schema_add_columns dedup.
    if (selectedColumns.has(nm) || customColumns.some((c) => c.name === nm)) {
      setAddColumnError(`"${nm}" is already in your schema.`);
      return;
    }
    setAddColumnError(null);
    setCustomColumns((prev) => [...prev, { ...draft, name: nm }]);
    setDraft(blankCustomColumn());
  };

  const removeCustomColumn = (idx: number) => {
    setCustomColumns((prev) => prev.filter((_, i) => i !== idx));
    // Also drop from groupingKeys + suppressedColumns so we don't leave a
    // dangling reference.
    const removed = customColumns[idx]?.name;
    if (removed) {
      setGroupingKeys((prev) => {
        if (!prev.has(removed)) return prev;
        const next = new Set(prev);
        next.delete(removed);
        return next;
      });
      setSuppressedColumns((prev) => {
        if (!prev.has(removed)) return prev;
        const next = new Set(prev);
        next.delete(removed);
        return next;
      });
    }
  };

  const toggleSuppressedColumn = (nm: string) => {
    setSuppressedColumns((prev) => {
      const next = new Set(prev);
      if (next.has(nm)) next.delete(nm);
      else next.add(nm);
      return next;
    });
  };

  const toggleGroupingKey = (nm: string) => {
    setGroupingKeys((prev) => {
      const next = new Set(prev);
      if (next.has(nm)) next.delete(nm);
      else next.add(nm);
      return next;
    });
  };

  /** Either reuses the existing project (already created, or hydrated in
   *  edit mode) or provisions a fresh DPE-CF project. Always persists the
   *  current wizard spec as the contract and refreshes the DPROD view so
   *  downstream suggest-domain-rules has :DProdColumn anchors. */
  const ensureProjectAndSaveSpec = async (): Promise<number> => {
    // Hard write-suppression in read-only View mode. This is the single choke
    // point every persistence path funnels through (saveDraft, the step 5→6 /
    // 6→7 auto-advances, finalize). Bailing here guarantees no PUT /odcs and
    // therefore no unintended version branch, regardless of which trigger
    // fired. In View mode we are always in edit mode, so createdProjectId is
    // set from hydration.
    if (viewOnly) {
      return createdProjectId ?? 0;
    }
    if (createdProjectId) {
      await api.put(`/api/projects/${createdProjectId}/odcs`, {
        spec: buildSpec(),
        submitted_by: CURRENT_USER_EMAIL,
        // Phase 1: thread the PO's override + notes through. ``auto`` lets
        // the backend classifier decide based on the diff against the
        // current view; an explicit kind forces the save mode.
        change_kind: changeKindOverride ?? "auto",
        revision_notes: revisionNotes || "",
      });
      // Skip the dprod regenerate when branching off a previously-completed
      // product (published or approved): generate-dprod wipes :DProdColumn
      // nodes (which are NOT versioned alongside :DataContract) and rebuilds
      // them from the new head. For 'published' that would flip the
      // marketplace listing to render the in-flight draft's columns even
      // though we pinned the contract metadata to v1; for 'approved' it
      // would orphan the engineer's existing :ColumnMapping nodes whose
      // target URIs reference DProdColumn nodes about to be deleted.
      // Either way, defer dprod regeneration to the engineer's odcs_to_dprod
      // stage, which they'll run as part of accepting the edit.
      const branchingState = hydratedLifecycleState === "published"
        || hydratedLifecycleState === "approved";
      if (!branchingState) {
        await api.post(`/api/projects/${createdProjectId}/odcs/generate-dprod`);
      }
      return createdProjectId;
    }
    const settings = (await api.get<SettingsResponse>("/api/settings")).data;
    const projectName = name.trim() || `${domain}-product`;
    const createRes = await api.post("/api/projects", {
      name: projectName,
      archetype: "dpe-cf",
      domain,
      owner_email: CURRENT_USER_EMAIL,
      owner_name: CURRENT_USER_NAME,
      neo4j_host: settings.neo4j_host,
      neo4j_port: settings.neo4j_port,
      neo4j_user: settings.neo4j_user,
      neo4j_password: settings.neo4j_password,
      neo4j_database: settings.neo4j_database,
    });
    const projectId = createRes.data.id as number;
    setCreatedProjectId(projectId);
    await api.put(`/api/projects/${projectId}/odcs`, {
      spec: buildSpec(),
      submitted_by: CURRENT_USER_EMAIL,
      change_kind: changeKindOverride ?? "auto",
      revision_notes: revisionNotes || "",
    });
    await api.post(`/api/projects/${projectId}/odcs/generate-dprod`);
    return projectId;
  };

  /** Save the current wizard state as a draft without advancing or
   *  submitting. The contract stays in ``lifecycleState='draft'`` so the
   *  user can resume via My Products → Edit. */
  const saveDraft = async () => {
    if (savingDraft) return;
    // Read-only View mode never saves (the Save Draft button is hidden). Guard
    // defensively in case saveDraft is reached by any other path.
    if (viewOnly) return;
    if (!domain) {
      setSubmitError("Pick a domain before saving a draft.");
      return;
    }
    setSavingDraft(true);
    setSubmitError(null);
    try {
      await ensureProjectAndSaveSpec();
      // If the PO arrived here from an engineer's source-candidates-needed
      // request, flip the request to 'complete' once new candidates have
      // been persisted. One-shot — only the first successful saveDraft fires
      // the resolution.
      if (fromRequestId && !resolvedFromRequestRef.current) {
        resolvedFromRequestRef.current = true;
        try {
          await api.post(`/api/my-products/source-candidate-requests/resolve`, {
            request_id: fromRequestId,
            action: "complete",
            resolution_note: `PO updated candidate sources via wizard edit mode.`,
          });
        } catch {
          // Non-fatal — the dashboard will retry resolution on next view.
        }
      }
      navigate("/product/my-products");
    } catch (e: unknown) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setSavingDraft(false);
    }
  };

  const buildSpec = (): Record<string, unknown> => {
    const properties = [
      ...selectedCatalogColumns.map((c) => ({
        name: c.name,
        physicalName: c.name,
        logicalName: c.name,
        logicalType: c.logical_type,
        physicalType: c.physical_type,
        description: c.description,
        primaryKey: c.primary_key,
        required: c.primary_key,
      })),
      ...customColumns.map((c) => {
        const out: Record<string, unknown> = {
          name: c.name,
          physicalName: c.name,
          logicalName: c.name,
          logicalType: c.logical_type,
          physicalType: c.physical_type,
          description: c.description,
          primaryKey: c.primary_key,
          required: c.primary_key,
        };
        // Only emit transform when it carries a meaningful intent — kind is required.
        if (c.transform && c.transform.kind) {
          out.transform = c.transform;
        }
        return out;
      }),
    ];
    const spec: Record<string, unknown> = {
      name: name.trim() || `${domain}-product`,
      version: "1.0.0",
      status: "draft",
      domain,
      // Selected readiness rubric — persists to :DataContract.scoringRubric
      // and drives which rubric's criteria the evaluator runs against.
      scoringRubric: selectedRubric || "osi",
      // Taxonomy intent (Product-Details step) — persists to
      // :DataContract.productKind ('aggregate' vs 'consumer'; dpe-sa stays
      // 'source'). Aggregate drives the materialize-by-default serving hint.
      productKind,
      // Free-form product tags → :DataContract.tags (trimmed + deduped
      // server-side). Emitted unconditionally so clearing every tag on a
      // re-save actually clears them.
      tags,
      description: description.trim() || productIdea.trim(),
      purpose: purpose.trim(),
      owners: [
        {
          username: CURRENT_USER_EMAIL,
          name: CURRENT_USER_NAME,
          role: "Data Product Owner",
          email: CURRENT_USER_EMAIL,
        },
      ],
      schema: [
        {
          name: datasetName || `${domain}_core`,
          physicalName: datasetName || `${domain}_core`,
          physicalType: "table",
          description: `${domain} product dataset`,
          properties,
          // Schema-level transform block from the Shape step (Phase 3+4+5
          // PO authoring) plus the column-level grouping_keys collected in
          // step 4. Backend _canonicalize_dataset_transform lowers these
          // fields to :DatasetTransform; filter + dedupe + grouping_keys
          // activate immediately in view-DDL (grouped CTE), scd_policy is
          // Phase-5-reserved. Only emit the block when at least one field
          // is set — keeps contracts clean for products that don't author
          // shape.
          ...((shapeGrain.trim() || shapeFilter.trim() || shapeFilterIntent.trim() || shapeScdPolicy || groupingKeys.size > 0 || suppressedColumns.size > 0 || windowSpecs.length > 0) ? {
            transform: {
              ...(shapeFilter.trim() ? { filter: shapeFilter.trim() } : {}),
              ...(shapeFilterIntent.trim() ? { filter_intent: shapeFilterIntent.trim() } : {}),
              ...(groupingKeys.size > 0 ? { grouping_keys: Array.from(groupingKeys) } : {}),
              ...(suppressedColumns.size > 0 ? { suppressed_columns: Array.from(suppressedColumns) } : {}),
              ...(shapeScdPolicy ? {
                // SCD-2 carries the richer sub-policy (effective / expiration
                // / add_is_current). Other types just declare the type — the
                // sub-fields would be ignored anyway, but keeping the payload
                // minimal stops them from accumulating stale state across
                // type changes.
                scd_policy: shapeScdPolicy === "scd2"
                  ? {
                      type: "scd2",
                      ...(scdEffectiveColumn ? { effective_column: scdEffectiveColumn } : {}),
                      ...(scdExpirationColumn ? { expiration_column: scdExpirationColumn } : {}),
                      ...(scdAddIsCurrent ? { add_is_current: true } : {}),
                    }
                  : { type: shapeScdPolicy },
              } : {}),
              ...(shapeGrain.trim() ? { grain_prose: shapeGrain.trim() } : {}),
              ...(windowSpecs.length > 0 ? {
                // Phase 6: flatten the UI draft array into the {name: spec}
                // dict view-DDL expects. partition_by is comma-split; order_by
                // is parsed as "col [direction]" tokens. Entries with empty
                // name OR no partition/order/frame get dropped — a window
                // declaration with no PARTITION / ORDER / FRAME is pointless.
                window_specs: windowSpecs.reduce<Record<string, unknown>>((acc, w) => {
                  const name = w.name.trim();
                  if (!name) return acc;
                  const partition_by = w.partition_by.split(",").map(s => s.trim()).filter(Boolean);
                  const order_by = w.order_by.split(",").map(s => s.trim()).filter(Boolean).map(tok => {
                    const parts = tok.split(/\s+/);
                    const col = parts[0];
                    const dir = (parts[1] || "asc").toLowerCase();
                    return { column: col, direction: (dir === "desc" ? "desc" : "asc") };
                  });
                  const frame = w.frame.trim();
                  if (partition_by.length === 0 && order_by.length === 0 && !frame) return acc;
                  acc[name] = { partition_by, order_by, frame };
                  return acc;
                }, {}),
              } : {}),
            },
          } : {}),
        },
      ],
    };
    // Consumer-aligned inputs[] — the backend MERGEs :CONSUMES edges per
    // entry. Only emit when present so the canonicaliser's empty-block
    // handling doesn't leave a stray inputs key on source-only or empty
    // contracts.
    if (selectedSourceInputs.length > 0) {
      spec.inputs = selectedSourceInputs.map((s) => ({
        dprod_uri: s.dprod_uri,
        contract_id: s.contract_id,
        name: s.name,
      }));
    }

    // PO serving preference (Readiness step) — round-trips through the
    // contract's customProperties; the engineer sees it as a recommendation
    // banner. Only emitted when the PO explicitly chose.
    if (servingPreference) {
      spec.customProperties = {
        po_serving_preference: servingPreference,
        ...(servingReason ? { po_serving_reason: servingReason } : {}),
      };
    }

    // Operations & Support (step 6). Emit each section only when non-empty so
    // the contract stays terse. Servers are derived/read-only but re-emitted
    // here so a versioned save keeps the derived entry. SLA/team/roles are
    // PO-authored.
    const cleanServers = servers.filter((s) => s.name.trim() || s.schema.trim() || s.database.trim());
    if (cleanServers.length > 0) {
      spec.servers = cleanServers.map((s) => ({
        name: s.name,
        environment: s.environment,
        type: s.type,
        account: s.account,
        database: s.database,
        schema: s.schema,
        datasets: s.datasets,
      }));
    }
    const cleanSlas = slaProps.filter((s) => s.property.trim());
    if (cleanSlas.length > 0) {
      spec.slaProperties = cleanSlas.map((s) => ({
        property: s.property.trim(),
        value: s.value,
        unit: s.unit.trim(),
      }));
    }
    const cleanTeam = teamMembers.filter((t) => t.name.trim() || t.email.trim());
    if (cleanTeam.length > 0) {
      spec.team = cleanTeam.map((t) => ({ name: t.name.trim(), role: t.role.trim(), email: t.email.trim() }));
    }
    const cleanRoles = roles.filter((r) => r.role.trim());
    if (cleanRoles.length > 0) {
      spec.roles = cleanRoles.map((r) => ({ role: r.role.trim(), access: r.access.trim(), description: r.description.trim() }));
    }
    return spec;
  };

  // Seed Operations & Support defaults the first time the PO reaches step 6:
  // contacts default to the owner; SLA inherits from the CONSUMES'd sources
  // (a consumer view has no independent refresh cycle). Runs once per session
  // and never overwrites values the PO already authored / hydrated.
  useEffect(() => {
    if (step !== 6 || opsSeededRef.current) return;
    opsSeededRef.current = true;
    setTeamMembers((prev) =>
      prev.length > 0 ? prev : [{ name: CURRENT_USER_NAME, role: "owner", email: CURRENT_USER_EMAIL }]
    );
    if (slaProps.length === 0 && createdProjectId) {
      setSlaSuggestLoading(true);
      api
        .get(`/api/projects/${createdProjectId}/odcs/suggest-sla`)
        .then((r) => {
          const slas = (r.data?.slas || []) as Array<{
            property: string; value: string; unit: string; inherited_from?: string;
          }>;
          if (slas.length > 0) {
            setSlaProps(
              slas.map((s) => ({
                property: s.property,
                value: String(s.value ?? ""),
                unit: s.unit || "",
                inheritedFrom: s.inherited_from,
              }))
            );
          }
        })
        .catch(() => {})
        .finally(() => setSlaSuggestLoading(false));
    }
  }, [step, createdProjectId, slaProps.length]);

  const advanceToRuleCoach = async () => {
    setProvisioningProject(true);
    setSubmitError(null);
    try {
      const projectId = await ensureProjectAndSaveSpec();
      setRulesLoading(true);
      // In View mode, suggest without persisting so the Rule Coach still
      // renders the domain rules read-only (no :PropertyShape writes).
      const res = await api.post(`/api/projects/${projectId}/odcs/suggest-domain-rules`, {
        persist: !viewOnly,
      });
      const fetched: SuggestedRule[] = res.data.rules || [];
      setRules(fetched);
      // Hydrate approval state from the graph status returned by the backend:
      //   - status='rejected'                       -> rejectedRules (preserve PO decision)
      //   - status='approved' or 'pending_review'   -> approvedRules (pre-approve default)
      // For a brand-new product all rules are freshly created with
      // status='pending_review', so the default behaviour is unchanged (every
      // rule lands in approvedRules). For edit-mode re-entry, prior PO
      // approve / reject decisions persist so the PO doesn't have to redo them.
      setApprovedRules(
        new Set(
          fetched
            .filter((r) => (r.status ?? "pending_review") !== "rejected")
            .map((r) => r.rule_uri)
        )
      );
      setRejectedRules(
        new Set(
          fetched
            .filter((r) => r.status === "rejected")
            .map((r) => r.rule_uri)
        )
      );
      // advanceToRuleCoach fires when leaving Operations & Support (step 6):
      // it saves the spec (incl. SLA/team/roles) + loads rules, landing on
      // Rule Coach (step 7).
      setStep(7);
    } catch (e: unknown) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setProvisioningProject(false);
      setRulesLoading(false);
    }
  };

  const approve = (uri: string) => {
    setRejectedRules((r) => {
      const n = new Set(r);
      n.delete(uri);
      return n;
    });
    setApprovedRules((a) => new Set(a).add(uri));
  };
  const reject = (uri: string) => {
    setApprovedRules((a) => {
      const n = new Set(a);
      n.delete(uri);
      return n;
    });
    setRejectedRules((r) => new Set(r).add(uri));
  };

  const finalize = async () => {
    if (!createdProjectId) return;
    // Read-only View mode never submits (posts rule reviews + product-request).
    // The Submit button is hidden in View mode; this is a belt-and-suspenders
    // guard in case finalize is reached by any other path.
    if (viewOnly) return;

    // Soft-confirm gate: if a cached gap-analysis result has unresolved
    // gaps, warn the PO before submitting. Only fires when the PO has
    // actually run the check — never blocks if they skipped it.
    if (gapAnalysisResult) {
      const gapRows = gapAnalysisResult.gaps.filter((g) => g.status === "gap");
      if (gapRows.length > 0) {
        const bullets = gapRows.map((g) => g.column_name).join("\n");
        const proceed = await confirm({
          title: "Unresolved gaps found",
          message: (
            <>
              Pre-flight gap check found {gapRows.length} column(s) the engineer probably can't map from your selected sources:
              <pre style={{ whiteSpace: "pre-wrap", margin: "8px 0", fontFamily: "inherit" }}>{bullets}</pre>
              Submit anyway? You can also go back and add more candidate sources, or remove these columns from your schema.
            </>
          ),
          confirmLabel: "Submit anyway",
        });
        if (!proceed) return;
      }
    }

    setSubmitting(true);
    setSubmitError(null);
    try {
      for (const uri of approvedRules) {
        await api.post(`/api/projects/${createdProjectId}/reviews/domain_rules`, {
          action: "approve",
          rule_uri: uri,
          reviewer: CURRENT_USER_EMAIL,
          quality: 2,
        });
      }
      for (const uri of rejectedRules) {
        await api.post(`/api/projects/${createdProjectId}/reviews/domain_rules`, {
          action: "reject",
          rule_uri: uri,
          reviewer: CURRENT_USER_EMAIL,
          category: "other",
          detail: "Product Owner declined at authoring time",
        });
      }
      const res = await api.post(`/api/projects/${createdProjectId}/product-requests/submit`, {
        kind: isEditMode ? "edit" : "new",
        submitted_by: CURRENT_USER_EMAIL,
        notes: `Domain '${domain}'. Idea: ${productIdea || "(none)"}. `
          + `${approvedRules.size} rules approved, ${rejectedRules.size} rejected.`,
      });
      setSubmittedRequestId(res.data.id);
      // Submitted is the final step (10) after inserting Operations & Support.
      setStep(10);
    } catch (e: unknown) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const rulesByColumn = useMemo(() => {
    const map = new Map<string, SuggestedRule[]>();
    for (const r of [...rules, ...userRules]) {
      const list = map.get(r.col_name) || [];
      list.push(r);
      map.set(r.col_name, list);
    }
    return map;
  }, [rules, userRules]);

  return (
    <div style={{ maxWidth: 960, margin: "0 auto", paddingRight: chatOpen ? 440 : 0, transition: "padding-right 0.15s" }}>
      {hydrating && (
        <div style={{ padding: 24, color: "#64748b", fontSize: 14 }}>
          Loading draft…
        </div>
      )}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: "#0f172a" }}>
          {isEditMode
            ? (viewOnly ? "View Product" : (rejection && !rejectionDismissed ? "Revise Product" : "Edit Product"))
            : "Request a New Product"}
        </h1>
        <GuideMeButton
          prominent
          label={chatOpen ? "Hide assistant" : "Guide me"}
          onClick={() => {
            if (chatOpen) {
              setChatOpen(false);
            } else {
              guideMe("general", "General guidance on my current wizard step");
            }
          }}
        />
      </div>
      {viewOnly && (
        <div
          style={{
            margin: "12px 0",
            padding: 14,
            borderRadius: 10,
            border: "1px solid #fcd34d",
            backgroundColor: "#fffbeb",
            color: "#78350f",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span aria-hidden style={{ fontSize: 16 }}>👁</span>
              <div>
                <div style={{ fontWeight: 700, fontSize: 14 }}>
                  Viewing {hydratedLifecycleState || "this"} product — read-only
                </div>
                <div style={{ fontSize: 13, marginTop: 4, lineHeight: 1.5 }}>
                  Page through every step to review the configuration. Nothing is saved and no
                  new version is created. Enable editing to make changes.
                </div>
              </div>
            </div>
            <button
              type="button"
              onClick={async () => {
                const proceed = await confirm({
                  title: "Enable editing",
                  message:
                    hydratedLifecycleState === "published"
                      ? "Saving changes will branch a NEW draft version (v(n+1)); the deployed product stays live until you deploy the new version."
                      : hydratedLifecycleState === "approved"
                      ? "Saving changes will branch a NEW draft version and send it back to engineering. The current version is preserved as a snapshot."
                      : "Saving changes will branch a new version of this product.",
                  confirmLabel: "Enable editing",
                });
                if (proceed) setViewOnly(false);
              }}
              style={{
                flexShrink: 0,
                fontSize: 12,
                fontWeight: 700,
                padding: "8px 14px",
                borderRadius: 6,
                border: "1px solid #d97706",
                backgroundColor: "#d97706",
                color: "#fff",
                cursor: "pointer",
              }}
            >
              Enable editing
            </button>
          </div>
        </div>
      )}
      {isEditMode
        && !viewOnly
        && (hydratedLifecycleState === "published" || hydratedLifecycleState === "approved")
        && !editingDeployedDismissed
        && !rejection && (
        <div
          style={{
            margin: "12px 0",
            padding: 14,
            borderRadius: 10,
            border: "1px solid #93c5fd",
            backgroundColor: "#eff6ff",
            color: "#1e3a8a",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
            <div style={{ fontWeight: 700, fontSize: 14 }}>
              {hydratedLifecycleState === "published"
                ? "Editing a deployed product"
                : "Editing a product the engineer just signed off"}
            </div>
            <button
              type="button"
              onClick={() => setEditingDeployedDismissed(true)}
              style={{
                fontSize: 11,
                padding: "2px 8px",
                borderRadius: 4,
                border: "1px solid #93c5fd",
                backgroundColor: "#fff",
                color: "#1e40af",
                cursor: "pointer",
              }}
            >
              Dismiss
            </button>
          </div>
          <div style={{ fontSize: 13, marginTop: 6, lineHeight: 1.5 }}>
            {hydratedLifecycleState === "published" ? (
              <>Saving will create a new draft version. The deployed product stays live in the
              marketplace until the new version is deployed by you.</>
            ) : (
              <>Saving will create a new draft version and send it back to engineering for
              another round. The previous "Ready to deploy" version is preserved as a
              historical snapshot.</>
            )}
          </div>
        </div>
      )}
      {rejection && !rejectionDismissed && (
        <div
          style={{
            margin: "12px 0",
            padding: 14,
            borderRadius: 10,
            border: "1px solid #fca5a5",
            backgroundColor: "#fef2f2",
            color: "#7f1d1d",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
            <div style={{ fontWeight: 700, fontSize: 14 }}>
              Engineering returned this request
            </div>
            <button
              type="button"
              onClick={() => setRejectionDismissed(true)}
              style={{
                fontSize: 11,
                padding: "2px 8px",
                borderRadius: 4,
                border: "1px solid #fca5a5",
                backgroundColor: "#fff",
                color: "#991b1b",
                cursor: "pointer",
              }}
            >
              Dismiss
            </button>
          </div>
          <div style={{ fontSize: 13, marginTop: 6 }}>
            <strong>Reason category:</strong>{" "}
            {rejection.category ? REJECTION_LABELS[rejection.category] || rejection.category : "—"}
          </div>
          {rejection.reason && (
            <div style={{ fontSize: 13, marginTop: 4, whiteSpace: "pre-wrap" }}>
              <strong>Detail:</strong> {rejection.reason}
            </div>
          )}
          {rejection.by && (
            <div style={{ fontSize: 11, color: "#991b1b", marginTop: 6, opacity: 0.8 }}>
              from {rejection.by}{rejection.at ? ` · ${new Date(rejection.at).toLocaleString()}` : ""}
            </div>
          )}
        </div>
      )}
      <Stepper step={step} />

      {step === 1 && transitioning && (
        <SchemaAdvisorTransition
          domain={domain}
          idea={productIdea}
          onCancel={cancelDiscovery}
        />
      )}

      {step === 1 && !transitioning && discoveryResult && (
        <DiscoveryPanel
          domain={domain}
          idea={productIdea}
          result={discoveryResult}
          onUseTemplate={useTemplate}
          onStartFresh={acceptRecommendation}
          onCancel={cancelDiscovery}
        />
      )}

      {step === 1 && !transitioning && !discoveryResult && (
        <StepCard
          title="Describe the product and choose a domain"
          description="A short description helps us and reviewers understand the intent. Pick the domain closest to what you're building — the starter schema and rule suggestions are domain-driven."
          canAdvance={!!domain && !!productIdea.trim()}
          onAdvance={runDiscovery}
          onSaveDraft={viewOnly ? undefined : saveDraft}
          savingDraft={savingDraft}
          canSaveDraft={!!domain}
        >
          <Field label="What are you trying to build?" onGuide={() => guideMe("idea", "Shape the idea into a crisp description")}>
            <textarea
              value={productIdea}
              onChange={(e) => setProductIdea(e.target.value)}
              rows={3}
              placeholder="e.g. A unified view of active employees for downstream HR analytics, joining headcount with payroll grades"
              style={{ ...inputStyle, resize: "vertical" }}
            />
          </Field>
          <Field
            label="Domain"
            onGuide={productIdea.trim() ? () => guideMe("domain", "Recommend the best-fit domain for my idea") : undefined}
          >
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
              {catalogs.map((c) => (
                <div
                  key={c.domain}
                  style={{ position: "relative" }}
                  onMouseEnter={() => {
                    setHoveredDomain(c.domain);
                    prefetchCatalog(c.domain);
                  }}
                  onMouseLeave={() => setHoveredDomain((prev) => (prev === c.domain ? null : prev))}
                >
                  <button
                    type="button"
                    onClick={() => setDomain(c.domain)}
                    style={{
                      width: "100%",
                      textAlign: "left",
                      padding: 14,
                      borderRadius: 8,
                      border: `1px solid ${domain === c.domain ? productTheme.accent : "#e2e8f0"}`,
                      backgroundColor: domain === c.domain ? productTheme.accentSoft : "#fff",
                      cursor: "pointer",
                    }}
                  >
                    <div style={{ fontWeight: 700, color: "#0f172a" }}>{c.label || titleCaseDomain(c.domain)}</div>
                    <div style={{ fontSize: 12, color: "#475569", marginTop: 4 }}>{c.description}</div>
                    <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 6 }}>{c.column_count} columns · hover to preview</div>
                  </button>
                  {hoveredDomain === c.domain && catalogDetailCache[c.domain] && (
                    <DomainPreview detail={catalogDetailCache[c.domain]} />
                  )}
                </div>
              ))}
            </div>
          </Field>

          {/* Rubric picker — scoring rubric is product-level. OSI is the
              recommended default; AI-Ready is selectable from day one. Locks
              once the contract leaves draft so we don't fragment evaluation
              history; in that case render a static badge instead. */}
          <Field label="Scoring rubric">
            {rubricLocked ? (
              <div
                style={{
                  fontSize: 13,
                  color: "#475569",
                  padding: 10,
                  borderRadius: 6,
                  border: "1px solid #e2e8f0",
                  backgroundColor: "#f8fafc",
                }}
              >
                Rubric locked to{" "}
                <strong>
                  {rubricCatalog.find((r) => r.id === selectedRubric)?.label || selectedRubric}
                </strong>{" "}
                — switching rubric on a non-draft contract would fragment evaluation history.
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
                {(rubricCatalog.length > 0
                  ? rubricCatalog
                  : [{ id: "osi", label: "OSI Readiness", short_label: "OSI", description: "Open Semantic Interchange readiness." }]
                ).map((r) => {
                  const isSelected = selectedRubric === r.id;
                  const isRecommended = r.id === "osi";
                  return (
                    <button
                      key={r.id}
                      type="button"
                      onClick={() => setSelectedRubric(r.id)}
                      style={{
                        textAlign: "left",
                        padding: 14,
                        borderRadius: 8,
                        border: `1px solid ${isSelected ? productTheme.accent : "#e2e8f0"}`,
                        backgroundColor: isSelected ? productTheme.accentSoft : "#fff",
                        cursor: "pointer",
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <div style={{ fontWeight: 700, color: "#0f172a" }}>{r.label}</div>
                        {isRecommended && (
                          <span
                            style={{
                              fontSize: 10,
                              fontWeight: 700,
                              padding: "2px 6px",
                              borderRadius: 999,
                              backgroundColor: productTheme.accent,
                              color: "#fff",
                              letterSpacing: 0.3,
                            }}
                          >
                            RECOMMENDED
                          </span>
                        )}
                      </div>
                      <div style={{ fontSize: 12, color: "#475569", marginTop: 4, whiteSpace: "pre-line" }}>
                        {r.description}
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </Field>
        </StepCard>
      )}

      {step === 9 && (
        <StepCard
          title="Confirm candidate upstream products"
          description="Final review of the published products this product will draw from — source-aligned, aggregate, or consumer-aligned. Picks from step 3 are pre-selected; add any new sources you discovered while authoring the schema. The engineer cannot start mapping without at least one candidate."
          onBack={() => setStep(8)}
          canAdvance={
            wizardSlots.length > 0 &&
            allSlotsMatched(wizardSlots) &&
            !submitting
          }
          onAdvance={viewOnly ? undefined : finalize}
          advanceLabel={submitting ? "Submitting…" : "Submit for Engineering →"}
          disableAdvance={submitting || createdProjectId === null}
          onGuide={undefined}
          onSaveDraft={viewOnly ? undefined : saveDraft}
          savingDraft={savingDraft}
          canSaveDraft={!!domain}
        >
          {sourceInputsError && (
            <div style={{ color: "#dc2626", fontSize: 13, marginBottom: 10 }}>{sourceInputsError}</div>
          )}

          {wizardSlotsMatching && (
            <div
              style={{
                marginBottom: 6,
                padding: "14px 16px",
                borderRadius: 8,
                backgroundColor: "#fff",
                border: `1px solid ${productTheme.accent}`,
                boxShadow: "0 2px 8px rgba(15, 23, 42, 0.05)",
                display: "flex",
                alignItems: "center",
                gap: 12,
              }}
            >
              <div
                style={{
                  width: 16,
                  height: 16,
                  borderRadius: "50%",
                  border: `3px solid ${productTheme.accentSoft}`,
                  borderTopColor: productTheme.accent,
                  animation: "spin 0.9s linear infinite",
                  flexShrink: 0,
                }}
              />
              <div>
                <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a" }}>
                  Suggesting candidate upstream products…
                </div>
                <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>
                  Reading your idea, description, and schema to infer what this consumer consumes from, then ranking marketplace products against those hints. Usually takes 10–30 seconds.
                </div>
              </div>
              <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
            </div>
          )}

          <ResolveAndBindSourcesStep
            slots={wizardSlots}
            onSlotChange={handleSlotChange}
            onCreateGap={handleCreateGap}
            onRemove={removeSlot}
            mode="wizard"
            matching={wizardSlotsMatching || sourceInputsLoading}
          />

          {/* "+ Add a source" affordance: opens an inline picker showing
              marketplace products in the domain that haven't been
              bound yet. Picking one appends a pre-matched slot. */}
          <AddSourceAffordance
            available={sourceInputs}
            boundUris={new Set(wizardSlots.map((s) => s.selected_uri).filter(Boolean) as string[])}
            isOpen={showAddSourcePicker}
            onToggle={() => setShowAddSourcePicker((v) => !v)}
            onPick={addSourceFromMarketplace}
            onRequestNew={() => {
              const qs = new URLSearchParams({
                prefill_domain: domain || "",
                prefill_idea: "",
                prefill_name: "",
              });
              navigate(`/product/new/source?${qs.toString()}`);
            }}
            loading={sourceInputsLoading}
            domain={domain}
          />

          {/* Pre-flight gap check — optional, manual-fire. Surfaces consumer
              columns that probably can't be derived from the picked sources.
              On submit, if this cached result has any gaps, finalize() shows
              a soft-confirm dialog before proceeding. */}
          <GapAnalysisSection
            projectId={createdProjectId}
            consumerColumns={[
              ...selectedCatalogColumns.map((c) => ({
                name: c.name,
                logical_type: c.logical_type,
                description: c.description,
              })),
              ...customColumns.map((c) => ({
                name: c.name,
                logical_type: c.logical_type,
                description: c.description,
              })),
            ]}
            candidateContractIds={selectedSourceInputs
              .map((s) => s.contract_id)
              .filter(Boolean)}
            consumerIdea={productIdea}
            consumerDescription={description}
            consumerDomain={domain}
            cachedResult={gapAnalysisResult}
            onResult={setGapAnalysisResult}
          />
        </StepCard>
      )}

      {step === 2 && (
        <StepCard
          title="Shape"
          description="Tell us about the shape of this product — how rows roll up, what to keep, whether to track history. These choices write a schema-level :DatasetTransform that the consumer view-DDL reads at materialization time."
          onBack={() => setStep(1)}
          canAdvance={true}
          onAdvance={() => { advanceFromShape(); }}
          onGuide={() => guideMe("shape", "Help me think through the grain, filter, and history of this product")}
          onSaveDraft={viewOnly ? undefined : saveDraft}
          savingDraft={savingDraft}
          canSaveDraft={!!domain}
        >
          <ShapePanel
            grain={shapeGrain}
            onGrainChange={setShapeGrain}
            filterIntent={shapeFilterIntent}
            onFilterIntentChange={(s) => {
              setShapeFilterIntent(s);
              // Prose changed → the previous readback/predicate is stale.
              setFilterReadback("");
              setFilterCheckConfidence(null);
              setFilterCheckWarnings([]);
              setFilterLegacyNote(false);
            }}
            onCheckFilter={checkFilter}
            filterChecking={filterChecking}
            filterReadback={filterReadback}
            filterConfidence={filterCheckConfidence}
            filterWarnings={filterCheckWarnings}
            filterFallback={filterCheckFallback}
            filterLegacyNote={filterLegacyNote}
            scdPolicy={shapeScdPolicy}
            onScdPolicyChange={setShapeScdPolicy}
          />
        </StepCard>
      )}

      {step === 3 && (
        <StepCard
          title="Suggest candidate upstream products (optional)"
          description="If you already know which published products this one will draw from (source-aligned, aggregate, or consumer-aligned), pick them here. The schema advisor in the next step will use these as feasibility signal — columns that come from your chosen sources rank higher. You can also do this later at the final step."
          onBack={() => setStep(2)}
          canAdvance={!submitting}
          onAdvance={advanceToSchema}
          advanceLabel="Continue to Schema →"
          disableAdvance={submitting}
          onGuide={undefined}
          onSaveDraft={viewOnly ? undefined : saveDraft}
          savingDraft={savingDraft}
          canSaveDraft={!!domain}
        >
          {sourceInputsError && (
            <div style={{ color: "#dc2626", fontSize: 13, marginBottom: 10 }}>{sourceInputsError}</div>
          )}

          {wizardSlotsMatching && (
            <div
              style={{
                marginBottom: 6,
                padding: "14px 16px",
                borderRadius: 8,
                backgroundColor: "#fff",
                border: `1px solid ${productTheme.accent}`,
                boxShadow: "0 2px 8px rgba(15, 23, 42, 0.05)",
                display: "flex",
                alignItems: "center",
                gap: 12,
              }}
            >
              <div
                style={{
                  width: 16,
                  height: 16,
                  borderRadius: "50%",
                  border: `3px solid ${productTheme.accentSoft}`,
                  borderTopColor: productTheme.accent,
                  animation: "spin 0.9s linear infinite",
                  flexShrink: 0,
                }}
              />
              <div>
                <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a" }}>
                  Suggesting candidate upstream products…
                </div>
                <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>
                  Reading your idea, description, and domain to suggest marketplace products that might supply this consumer. Picks here feed the schema advisor on the next step.
                </div>
              </div>
              <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
            </div>
          )}

          <div
            style={{
              padding: 10,
              borderRadius: 8,
              backgroundColor: "#f1f5f9",
              color: "#334155",
              fontSize: 12,
              marginBottom: 4,
              lineHeight: 1.5,
            }}
          >
            <strong>Optional now</strong> — you can skip and pick at the final
            review step. Selecting candidates here improves the schema advisor's
            column ranking with source-feasibility hints.
          </div>

          <ResolveAndBindSourcesStep
            slots={wizardSlots}
            onSlotChange={handleSlotChange}
            onCreateGap={handleCreateGap}
            onRemove={removeSlot}
            mode="wizard"
            matching={wizardSlotsMatching || sourceInputsLoading}
          />

          <AddSourceAffordance
            available={sourceInputs}
            boundUris={new Set(wizardSlots.map((s) => s.selected_uri).filter(Boolean) as string[])}
            isOpen={showAddSourcePicker}
            onToggle={() => setShowAddSourcePicker((v) => !v)}
            onPick={addSourceFromMarketplace}
            onRequestNew={() => {
              const qs = new URLSearchParams({
                prefill_domain: domain || "",
                prefill_idea: "",
                prefill_name: "",
              });
              navigate(`/product/new/source?${qs.toString()}`);
            }}
            loading={sourceInputsLoading}
            domain={domain}
            // Step 3 is for considering existing candidates — the PO doesn't
            // author new source products until the final review at step 9.
            hideRequestNew={true}
          />
        </StepCard>
      )}

      {step === 4 && catalogDetail && (
        <StepCard
          title={schemaConfirmMode ? `Confirm the ${domain} schema` : `Shape the ${domain} schema`}
          description={
            schemaConfirmMode
              ? "These columns were agreed during your feasibility analysis. Confirm them, remove any you don't need, or add more from the catalog — then continue."
              : "The schema advisor pre-selected the columns most relevant to your idea. Remove any you don't need, add more from the catalog, or describe additional columns below."
          }
          onBack={() => setStep(3)}
          canAdvance={selectedCatalogColumns.length + customColumns.length > 0}
          onAdvance={() => setStep(5)}
          onGuide={() => guideMe("schema", "Review my schema and suggest improvements")}
          onSaveDraft={viewOnly ? undefined : saveDraft}
          savingDraft={savingDraft}
          canSaveDraft={!!domain}
        >
          {/* Evidence chip — shows whether the schema advisor's ranking was
              informed by candidate upstream products picked at
              step 3, or just the domain catalog. Pure render-side. */}
          <SourceFeasibilityEvidence
            selectedSources={selectedSourceInputs}
            domain={domain}
            onJumpToCandidates={() => setStep(3)}
          />

          {isEditMode && deployedSnapshot && (
            <EditDiffPanel deployed={deployedSnapshot} current={currentSnapshot} defaultCollapsed={true} />
          )}
          {templateApplied && (
            <div
              style={{
                marginBottom: 14,
                padding: 10,
                borderRadius: 8,
                backgroundColor: productTheme.accentSoft,
                border: `1px solid ${productTheme.accent}`,
                color: "#0f172a",
                fontSize: 12,
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: 12,
              }}
            >
              <div>
                Started from the <strong>{templateApplied.id}</strong> template — edit any column or
                field you don't want, or add more below.
              </div>
              <button
                type="button"
                onClick={() => {
                  setTemplateApplied(null);
                  setSelectedColumns(new Set());
                  setCustomColumns([]);
                  setName("");
                  setDescription("");
                  setPurpose("");
                }}
                style={{
                  padding: "4px 10px",
                  fontSize: 11,
                  color: "#475569",
                  backgroundColor: "#fff",
                  border: "1px solid #cbd5e1",
                  borderRadius: 6,
                  cursor: "pointer",
                }}
              >
                Reset
              </button>
            </div>
          )}
          {transitionError && (
            <div
              style={{
                marginBottom: 14,
                padding: 12,
                borderRadius: 8,
                backgroundColor: "#fff7ed",
                border: "1px solid #fdba74",
                color: "#9a3412",
                fontSize: 13,
                lineHeight: 1.5,
              }}
            >
              {transitionError}
            </div>
          )}

          {refreshingRecommendations && (
            <div
              style={{
                marginBottom: 14,
                padding: "14px 16px",
                borderRadius: 8,
                backgroundColor: "#fff",
                border: `1px solid ${productTheme.accent}`,
                boxShadow: "0 2px 8px rgba(15, 23, 42, 0.05)",
                display: "flex",
                alignItems: "center",
                gap: 12,
              }}
            >
              <div
                style={{
                  width: 16,
                  height: 16,
                  borderRadius: "50%",
                  border: `3px solid ${productTheme.accentSoft}`,
                  borderTopColor: productTheme.accent,
                  animation: "spin 0.9s linear infinite",
                  flexShrink: 0,
                }}
              />
              <div>
                <div style={{ fontSize: 13, fontWeight: 700, color: "#0f172a" }}>
                  Re-ranking columns against your idea and dataset shape…
                </div>
                <div style={{ fontSize: 12, color: "#64748b", marginTop: 2 }}>
                  The advisor is reading your idea, description, and dataset shape to surface a relevance-ranked starter set from the {domain} catalog. Source-product feasibility scoring runs later, once you bind upstream sources in the final step. This usually takes 10–30 seconds.
                </div>
              </div>
              <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
            </div>
          )}

          {shapeScdPolicy === "scd2" && (
            <Scd2PolicyPanel
              effectiveColumn={scdEffectiveColumn}
              expirationColumn={scdExpirationColumn}
              addIsCurrent={scdAddIsCurrent}
              productColumnNames={[
                ...Array.from(selectedColumns),
                ...customColumns.map((c) => c.name),
              ]}
              onEffectiveChange={setScdEffectiveColumn}
              onExpirationChange={setScdExpirationColumn}
              onAddIsCurrentChange={setScdAddIsCurrent}
            />
          )}

          <div style={{ marginBottom: 8, display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "#334155", textTransform: "uppercase", letterSpacing: 0.4 }}>
                {schemaConfirmMode ? "Agreed columns" : "Recommended columns"} ({selectedCatalogColumns.length})
              </div>
              {(!isEditMode || schemaConfirmMode) && (
                <button
                  type="button"
                  onClick={() => { void refreshShapeRecommendations("manual"); }}
                  disabled={refreshingRecommendations || !productIdea.trim()}
                  title={
                    recommendationsStale
                      ? "Inputs / shape / description changed since the last advisor call — refresh to re-rank."
                      : "Re-rank the catalog by the latest shape + inputs + description."
                  }
                  style={{
                    padding: "4px 10px",
                    fontSize: 11,
                    fontWeight: 600,
                    color: refreshingRecommendations ? "#94a3b8" : productTheme.accent,
                    backgroundColor: recommendationsStale ? productTheme.accentSoft : "#fff",
                    border: `1px solid ${recommendationsStale ? productTheme.accent : "#cbd5e1"}`,
                    borderRadius: 6,
                    cursor: refreshingRecommendations ? "wait" : "pointer",
                  }}
                >
                  {refreshingRecommendations ? "Refreshing…" : (recommendationsStale ? "⟳ Refresh (stale)" : "⟳ Refresh")}
                </button>
              )}
              {userRemovedColumns.size > 0 && (
                <span style={{ fontSize: 11, color: "#64748b" }}>
                  {userRemovedColumns.size} previously removed
                </span>
              )}
              {Object.keys(columnDetails).length > 0 && (
                <label
                  style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11, color: "#475569", cursor: "pointer" }}
                  title="Show or hide the per-column 'why suggested' rationales inline."
                >
                  <input
                    type="checkbox"
                    checked={showRationales}
                    onChange={() => setShowRationales((v) => !v)}
                  />
                  Show rationales
                </label>
              )}
            </div>
            <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
              {groupingKeys.size > 0 && (
                <div style={{ fontSize: 11, color: "#475569" }}>
                  {groupingKeys.size} grouping key{groupingKeys.size === 1 ? "" : "s"} marked — engineering aggregates non-key columns at materialization time
                </div>
              )}
              {suppressedColumns.size > 0 && (
                <div style={{ fontSize: 11, color: "#b45309" }}>
                  {suppressedColumns.size} column{suppressedColumns.size === 1 ? "" : "s"} suppressed — kept in contract / lineage, omitted from the served view
                </div>
              )}
            </div>
          </div>
          {recommendationsRationale && (
            <div style={{ marginTop: -4, marginBottom: 12, fontSize: 12, color: "#64748b", fontStyle: "italic", lineHeight: 1.5 }}>
              {recommendationsRationale}
            </div>
          )}
          {selectedCatalogColumns.length === 0 ? (
            <div
              style={{
                padding: 14,
                borderRadius: 8,
                backgroundColor: "#f8fafc",
                border: "1px dashed #cbd5e1",
                fontSize: 13,
                color: "#475569",
                lineHeight: 1.5,
              }}
            >
              No catalog columns selected yet. Open <strong>Add from catalog</strong> below to pick some, define a custom column, or use <strong>Guide me</strong> to get suggestions.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {selectedCatalogColumns.map((c) => (
                <div
                  key={c.name}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr auto auto auto auto auto",
                    gap: 10,
                    padding: 10,
                    borderRadius: 8,
                    border: "1px solid #e2e8f0",
                    backgroundColor: suppressedColumns.has(c.name) ? "#fafafa" : "#fff",
                    alignItems: "center",
                    opacity: suppressedColumns.has(c.name) ? 0.7 : 1,
                  }}
                >
                  <div>
                    <div style={{ fontWeight: 600, color: "#0f172a" }}>
                      {c.name}
                      {c.primary_key && (
                        <span style={{ marginLeft: 8, fontSize: 10, color: "#7c3aed", fontWeight: 700 }}>PK</span>
                      )}
                      {!showRationales && columnDetails[c.name]?.why && (
                        <span
                          title={columnDetails[c.name].why}
                          style={{ marginLeft: 8, fontSize: 11, color: "#94a3b8", cursor: "help", fontWeight: 400 }}
                        >
                          (?)
                        </span>
                      )}
                      {columnDetails[c.name]?.feasibility === "missing_source" && (
                        <span
                          title="The advisor found no source column in your selected inputs that backs this. Drop it, mark it derivable, or add another upstream product."
                          style={{ marginLeft: 8, fontSize: 10, color: "#d97706", fontWeight: 700, cursor: "help" }}
                        >
                          NO SOURCE
                        </span>
                      )}
                      {columnDetails[c.name]?.feasibility === "derivable" && (
                        <span
                          title="The advisor thinks this can be derived from one or more source columns."
                          style={{ marginLeft: 8, fontSize: 10, color: "#0891b2", fontWeight: 700, cursor: "help" }}
                        >
                          DERIVED
                        </span>
                      )}
                      {columnDetails[c.name]?.grain_alignment === "finer" && (
                        <span
                          title={`This column is finer-grain than your declared grain (${shapeGrain || "the declared grain"}). Including it would either explode rows or pick one row arbitrarily. Drop it, or add a roll-up (e.g. count(*) AS order_count) as a derived column.`}
                          style={{ marginLeft: 8, fontSize: 10, color: "#dc2626", fontWeight: 700, cursor: "help" }}
                        >
                          GRAIN MISMATCH
                        </span>
                      )}
                      {columnDetails[c.name]?.grain_alignment === "rollup_required" && (
                        <span
                          title={`This finer-grain source needs aggregation to fit your declared grain (${shapeGrain || "the declared grain"}). The advisor's rationale names the rollup.`}
                          style={{ marginLeft: 8, fontSize: 10, color: "#b45309", fontWeight: 700, cursor: "help" }}
                        >
                          ROLLUP NEEDED
                        </span>
                      )}
                      {c.requires_scd_policy === "scd2" && shapeScdPolicy !== "scd2" && (
                        <span
                          title={`This is an SCD-2 history column — it only makes sense when the Shape step's SCD policy is "scd2". Your current policy is "${shapeScdPolicy || "none"}", so it won't carry meaningful values. Switch the policy to scd2 or remove this column.`}
                          style={{ marginLeft: 8, fontSize: 10, color: "#dc2626", fontWeight: 700, cursor: "help" }}
                        >
                          NEEDS SCD-2
                        </span>
                      )}
                      {suppressedColumns.has(c.name) && (
                        <span style={{ marginLeft: 8, fontSize: 10, color: "#b45309", fontWeight: 700 }}>SUPPRESSED</span>
                      )}
                    </div>
                    <div style={{ fontSize: 12, color: "#475569" }}>{c.description || "—"}</div>
                    {showRationales && columnDetails[c.name]?.why && (
                      <div style={{ fontSize: 11, color: "#64748b", marginTop: 3, fontStyle: "italic", lineHeight: 1.4 }}>
                        Why: {columnDetails[c.name].why}
                      </div>
                    )}
                  </div>
                  <code style={{ fontSize: 11, color: "#64748b" }}>{c.physical_type}</code>
                  <label
                    style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, color: "#475569", cursor: "pointer" }}
                    title="Mark as a GROUP BY key — engineering aggregates non-key columns at materialization time."
                  >
                    <input
                      type="checkbox"
                      checked={groupingKeys.has(c.name)}
                      onChange={() => toggleGroupingKey(c.name)}
                    />
                    Group by
                  </label>
                  <label
                    style={{
                      display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11,
                      color: c.primary_key ? "#cbd5e1" : "#475569",
                      cursor: c.primary_key ? "not-allowed" : "pointer",
                    }}
                    title={c.primary_key
                      ? "Primary-key columns can't be suppressed — view-DDL would refuse anyway."
                      : "Drop this column from the served view. Still kept in the contract and lineage."}
                  >
                    <input
                      type="checkbox"
                      disabled={c.primary_key}
                      checked={suppressedColumns.has(c.name)}
                      onChange={() => toggleSuppressedColumn(c.name)}
                    />
                    Suppress
                  </label>
                  <span
                    style={{
                      fontSize: 10,
                      padding: "2px 6px",
                      borderRadius: 3,
                      backgroundColor: c.source_category === "common" ? "#f1f5f9"
                        : c.source_category === "derived" ? "#faf5ff"
                        : productTheme.accentSoft,
                      color: c.source_category === "common" ? "#475569"
                        : c.source_category === "derived" ? "#7e22ce"
                        : productTheme.accent,
                      textTransform: "uppercase",
                      fontWeight: 700,
                    }}
                  >
                    {c.source_category}
                  </span>
                  <button
                    type="button"
                    onClick={() => toggleColumn(c.name)}
                    title="Remove from selection"
                    style={{
                      width: 28,
                      height: 28,
                      borderRadius: 4,
                      border: "1px solid #e2e8f0",
                      backgroundColor: "#fff",
                      color: "#94a3b8",
                      fontSize: 16,
                      lineHeight: 1,
                      cursor: "pointer",
                    }}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

          {(() => {
            // Exclude both current catalog picks AND names already authored as
            // custom columns — picking a name that's already a custom column
            // would put it in both collections (duplicate render + duplicate
            // spec property).
            const unselected = catalogDetail.columns.filter(
              (c) => !selectedColumns.has(c.name) && !customColumnNames.has(c.name)
            );
            const f = catalogPickerQuery.trim().toLowerCase();
            const matched = f
              ? unselected.filter(
                  (c) =>
                    c.name.toLowerCase().includes(f) ||
                    (c.description || "").toLowerCase().includes(f)
                )
              : unselected;
            // Sort by advisor relevance (desc), missing_source rows pushed
            // out of the main bucket entirely so they don't dominate. Bare
            // catalog rows (no detail) rank below detailed ones via -1
            // sentinel, then fall back to alpha for stability.
            const sorted = matched.slice().sort((a, b) => {
              const ra = columnDetails[a.name]?.relevance ?? -1;
              const rb = columnDetails[b.name]?.relevance ?? -1;
              if (ra !== rb) return rb - ra;
              return a.name.localeCompare(b.name);
            });
            // Three sub-buckets the main picker draws from:
            //   - filtered: ranked-and-feasible main list
            //   - grainMisaligned: finer-grain rows that the backend safety
            //     net didn't drop (rare — usually only when the PO manually
            //     surfaced them; collapsed sub-section, NOT in main list)
            //   - notSourceable: missing_source rows (existing pattern)
            const notSourceable = sorted.filter(
              (c) => columnDetails[c.name]?.feasibility === "missing_source"
            );
            const grainMisaligned = sorted.filter(
              (c) => columnDetails[c.name]?.feasibility !== "missing_source"
                && columnDetails[c.name]?.grain_alignment === "finer"
            );
            const filtered = sorted.filter(
              (c) => columnDetails[c.name]?.feasibility !== "missing_source"
                && columnDetails[c.name]?.grain_alignment !== "finer"
            );
            return (
              <div style={{ marginTop: 18 }}>
                <button
                  type="button"
                  onClick={() => setCatalogPickerOpen((o) => !o)}
                  style={{
                    width: "100%",
                    textAlign: "left",
                    padding: "10px 12px",
                    borderRadius: 8,
                    border: `1px solid ${catalogPickerOpen ? productTheme.accent : "#cbd5e1"}`,
                    backgroundColor: catalogPickerOpen ? productTheme.accentSoft : "#fff",
                    color: "#0f172a",
                    fontSize: 13,
                    fontWeight: 600,
                    cursor: "pointer",
                  }}
                >
                  {catalogPickerOpen ? "▾" : "▸"} Add from catalog ({unselected.length} more available)
                </button>
                {catalogPickerOpen && (
                  <div
                    style={{
                      marginTop: 8,
                      padding: 12,
                      borderRadius: 8,
                      border: "1px solid #e2e8f0",
                      backgroundColor: "#f8fafc",
                    }}
                  >
                    <input
                      type="text"
                      value={catalogPickerQuery}
                      onChange={(e) => setCatalogPickerQuery(e.target.value)}
                      placeholder="Search by name or description…"
                      autoFocus
                      style={{
                        width: "100%",
                        padding: "8px 12px",
                        borderRadius: 6,
                        border: "1px solid #cbd5e1",
                        fontSize: 13,
                        marginBottom: 10,
                      }}
                    />
                    {filtered.length === 0 ? (
                      <div style={{ fontSize: 13, color: "#64748b", padding: "8px 4px" }}>
                        {f ? "No catalog matches — try the “Add a custom column” form below." : "Every catalog column is already selected."}
                      </div>
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 360, overflowY: "auto" }}>
                        {filtered.map((c) => (
                          <div
                            key={c.name}
                            style={{
                              display: "grid",
                              gridTemplateColumns: "auto 1fr auto auto",
                              gap: 10,
                              padding: 8,
                              borderRadius: 6,
                              border: "1px solid #e2e8f0",
                              backgroundColor: "#fff",
                              alignItems: "center",
                            }}
                          >
                            <button
                              type="button"
                              onClick={() => toggleColumn(c.name)}
                              title="Add to selection"
                              style={{
                                width: 28,
                                height: 28,
                                borderRadius: 4,
                                border: `1px solid ${productTheme.accent}`,
                                backgroundColor: productTheme.accent,
                                color: "#fff",
                                fontSize: 16,
                                lineHeight: 1,
                                cursor: "pointer",
                                fontWeight: 700,
                              }}
                            >
                              +
                            </button>
                            <div>
                              <div style={{ fontWeight: 600, color: "#0f172a", fontSize: 13 }}>
                                {c.name}
                                {c.primary_key && (
                                  <span style={{ marginLeft: 8, fontSize: 10, color: "#7c3aed", fontWeight: 700 }}>PK</span>
                                )}
                                {!showRationales && columnDetails[c.name]?.why && (
                                  <span
                                    title={columnDetails[c.name].why}
                                    style={{ marginLeft: 8, fontSize: 11, color: "#94a3b8", cursor: "help", fontWeight: 400 }}
                                  >
                                    (?)
                                  </span>
                                )}
                                {columnDetails[c.name]?.feasibility === "derivable" && (
                                  <span
                                    title="Advisor thinks this can be derived from one or more source columns."
                                    style={{ marginLeft: 8, fontSize: 10, color: "#0891b2", fontWeight: 700, cursor: "help" }}
                                  >
                                    DERIVED
                                  </span>
                                )}
                                {columnDetails[c.name]?.grain_alignment === "rollup_required" && (
                                  <span
                                    title={`This finer-grain source needs aggregation to fit your declared grain (${shapeGrain || "the declared grain"}). The advisor's rationale names the rollup.`}
                                    style={{ marginLeft: 8, fontSize: 10, color: "#b45309", fontWeight: 700, cursor: "help" }}
                                  >
                                    ROLLUP NEEDED
                                  </span>
                                )}
                              </div>
                              <div style={{ fontSize: 11, color: "#475569" }}>{c.description || "—"}</div>
                              {showRationales && columnDetails[c.name]?.why && (
                                <div style={{ fontSize: 11, color: "#64748b", marginTop: 3, fontStyle: "italic", lineHeight: 1.4 }}>
                                  Why: {columnDetails[c.name].why}
                                </div>
                              )}
                            </div>
                            <code style={{ fontSize: 11, color: "#64748b" }}>{c.physical_type}</code>
                            <span
                              style={{
                                fontSize: 10,
                                padding: "2px 6px",
                                borderRadius: 3,
                                backgroundColor: c.source_category === "common" ? "#f1f5f9"
                                  : c.source_category === "derived" ? "#faf5ff"
                                  : productTheme.accentSoft,
                                color: c.source_category === "common" ? "#475569"
                                  : c.source_category === "derived" ? "#7e22ce"
                                  : productTheme.accent,
                                textTransform: "uppercase",
                                fontWeight: 700,
                              }}
                            >
                              {c.source_category}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                    {grainMisaligned.length > 0 && (
                      <GrainMisalignedSubSection
                        rows={grainMisaligned}
                        columnDetails={columnDetails}
                        showRationales={showRationales}
                        declaredGrain={shapeGrain}
                        onPick={(nm) => toggleColumn(nm)}
                      />
                    )}
                    {notSourceable.length > 0 && (
                      <NotSourceableSubSection
                        rows={notSourceable}
                        columnDetails={columnDetails}
                        showRationales={showRationales}
                        onPick={(nm) => toggleColumn(nm)}
                      />
                    )}
                  </div>
                )}
              </div>
            );
          })()}

          {customColumns.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "#334155", marginBottom: 6, textTransform: "uppercase", letterSpacing: 0.4 }}>
                Custom columns
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {customColumns.map((c, i) => (
                  <div
                    key={c.name}
                    style={{
                      padding: 10,
                      borderRadius: 8,
                      border: `1px solid ${productTheme.accent}`,
                      backgroundColor: suppressedColumns.has(c.name) ? "#fafafa" : productTheme.accentSoft,
                      display: "flex",
                      flexDirection: "column",
                      gap: 8,
                      opacity: suppressedColumns.has(c.name) ? 0.7 : 1,
                    }}
                  >
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "1fr auto auto auto auto auto auto",
                        gap: 10,
                        alignItems: "center",
                      }}
                    >
                      <div>
                        <div style={{ fontWeight: 600, color: "#0f172a" }}>
                          {c.name}
                          {c.primary_key && (
                            <span style={{ marginLeft: 8, fontSize: 10, color: "#7c3aed", fontWeight: 700 }}>PK</span>
                          )}
                          {suppressedColumns.has(c.name) && (
                            <span style={{ marginLeft: 8, fontSize: 10, color: "#b45309", fontWeight: 700 }}>SUPPRESSED</span>
                          )}
                          {c.transform?.kind && (
                            <span style={{ marginLeft: 8, fontSize: 10, color: "#0369a1", fontWeight: 700 }}>
                              ↳ {c.transform.kind}
                            </span>
                          )}
                        </div>
                        <div style={{ fontSize: 12, color: "#475569" }}>{c.description || "—"}</div>
                      </div>
                      <code style={{ fontSize: 11, color: "#64748b" }}>{c.physical_type}</code>
                      <label
                        style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, color: "#475569", cursor: "pointer" }}
                        title="Mark as a GROUP BY key"
                      >
                        <input
                          type="checkbox"
                          checked={groupingKeys.has(c.name)}
                          onChange={() => toggleGroupingKey(c.name)}
                        />
                        Group by
                      </label>
                      <label
                        style={{
                          display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11,
                          color: c.primary_key ? "#cbd5e1" : "#475569",
                          cursor: c.primary_key ? "not-allowed" : "pointer",
                        }}
                        title={c.primary_key
                          ? "Primary-key columns can't be suppressed."
                          : "Drop this column from the served view; keep in contract for lineage."}
                      >
                        <input
                          type="checkbox"
                          disabled={c.primary_key}
                          checked={suppressedColumns.has(c.name)}
                          onChange={() => toggleSuppressedColumn(c.name)}
                        />
                        Suppress
                      </label>
                      <span
                        style={{
                          fontSize: 10, padding: "2px 6px", borderRadius: 3,
                          backgroundColor: "#fff", color: productTheme.accent,
                          textTransform: "uppercase", fontWeight: 700,
                        }}
                      >Custom</span>
                      <button
                        type="button"
                        onClick={() => setDeriveExpandedIdx(deriveExpandedIdx === i ? null : i)}
                        style={{ ...smallButtonStyle("#e0f2fe", "#075985") }}
                        title="Add or edit a derivation hint for this column"
                      >
                        {deriveExpandedIdx === i ? "Close" : (c.transform?.kind ? "Edit derive" : "Derive…")}
                      </button>
                      <button
                        type="button"
                        onClick={() => removeCustomColumn(i)}
                        style={{ ...smallButtonStyle("#fee2e2", "#991b1b") }}
                      >Remove</button>
                    </div>

                    {deriveExpandedIdx === i && (
                      <DeriveHintEditor
                        value={c.transform}
                        onChange={(next) => {
                          setCustomColumns((cols) =>
                            cols.map((col, idx) => (idx === i ? { ...col, transform: next } : col)),
                          );
                        }}
                        onClear={() => {
                          setCustomColumns((cols) =>
                            cols.map((col, idx) => (idx === i ? { ...col, transform: undefined } : col)),
                          );
                          setDeriveExpandedIdx(null);
                        }}
                      />
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          <div
            style={{
              marginTop: 16,
              padding: 12,
              borderRadius: 8,
              border: `1px dashed ${productTheme.accent}`,
              backgroundColor: "#fafaff",
            }}
          >
            <div style={{ fontSize: 13, fontWeight: 700, color: "#334155", marginBottom: 8 }}>
              Add a custom column
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr", gap: 8 }}>
              <input
                placeholder="name"
                value={draft.name}
                onChange={(e) => { setDraft({ ...draft, name: e.target.value }); setAddColumnError(null); }}
                style={inputStyle}
              />
              <input
                placeholder="logical type (string, integer...)"
                value={draft.logical_type}
                onChange={(e) => setDraft({ ...draft, logical_type: e.target.value })}
                style={inputStyle}
              />
              <input
                placeholder="physical type (varchar(50)...)"
                value={draft.physical_type}
                onChange={(e) => setDraft({ ...draft, physical_type: e.target.value })}
                style={inputStyle}
              />
            </div>
            <textarea
              placeholder="description"
              value={draft.description}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
              rows={2}
              style={{ ...inputStyle, marginTop: 8, resize: "vertical", width: "100%", boxSizing: "border-box" }}
            />
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 8 }}>
              <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, color: "#334155" }}>
                <input
                  type="checkbox"
                  checked={draft.primary_key}
                  onChange={(e) => setDraft({ ...draft, primary_key: e.target.checked })}
                />
                Primary key
              </label>
              <button
                type="button"
                onClick={addCustomColumn}
                disabled={!draft.name.trim()}
                style={{
                  ...smallButtonStyle(productTheme.accent, "#fff"),
                  opacity: draft.name.trim() ? 1 : 0.5,
                  cursor: draft.name.trim() ? "pointer" : "not-allowed",
                }}
              >
                + Add column
              </button>
            </div>
            {addColumnError && (
              <div style={{ marginTop: 8, fontSize: 12, color: "#b91c1c" }}>{addColumnError}</div>
            )}
          </div>

          <WindowSpecsEditor specs={windowSpecs} onChange={setWindowSpecs} />
        </StepCard>
      )}

      {step === 5 && (
        <StepCard
          title="Describe the product"
          description="These fields land on the ODCS contract. Keep descriptions crisp — consumers will read them."
          onBack={() => setStep(4)}
          canAdvance={name.trim().length > 0}
          onAdvance={async () => {
            // Provision the project + save the spec here so step 6 can query
            // /suggest-sla (which walks the just-saved :CONSUMES edges).
            setProvisioningProject(true);
            setSubmitError(null);
            try {
              await ensureProjectAndSaveSpec();
              setStep(6);
            } catch (e: unknown) {
              setSubmitError(e instanceof Error ? e.message : String(e));
            } finally {
              setProvisioningProject(false);
            }
          }}
          advanceLabel={provisioningProject ? "Saving…" : "Next: Operations & Support →"}
          disableAdvance={provisioningProject}
          onGuide={() => guideMe("autofill_step3", "Auto-fill all four fields from my idea + domain")}
          onSaveDraft={viewOnly ? undefined : saveDraft}
          savingDraft={savingDraft}
          canSaveDraft={!!domain}
        >
          <Field label="Product name" onGuide={() => guideMe("name", "Suggest product name options")}>
            <input value={name} onChange={(e) => setName(e.target.value)} style={inputStyle} placeholder={`e.g. Employee 360`} />
          </Field>
          <Field label="Dataset physical name" onGuide={() => guideMe("dataset_name", "Suggest snake_case dataset names")}>
            <input value={datasetName} onChange={(e) => setDatasetName(e.target.value)} style={inputStyle} />
          </Field>
          <Field label="Description" onGuide={() => guideMe("description", "Draft a clear product description")}>
            <textarea
              value={description || productIdea}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              style={{ ...inputStyle, resize: "vertical" }}
              placeholder="What does this product contain, at a high level?"
            />
          </Field>
          <Field label="Purpose" onGuide={() => guideMe("purpose", "Articulate purpose options")}>
            <textarea
              value={purpose}
              onChange={(e) => setPurpose(e.target.value)}
              rows={2}
              style={{ ...inputStyle, resize: "vertical" }}
              placeholder="Why does this product exist? Who uses it?"
            />
          </Field>
          <Field label="Tags">
            <div style={{ fontSize: 12, color: "#64748b", marginBottom: 6 }}>
              Free-form labels for grouping + discovery in the marketplace (e.g. finance, gold, pii). Press Enter or comma to add.
            </div>
            {tags.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 6 }}>
                {tags.map((t) => (
                  <span key={t} style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "2px 8px", borderRadius: 999, fontSize: 12, fontWeight: 600, backgroundColor: "#eef2ff", color: "#4338ca", border: "1px solid #c7d2fe" }}>
                    {t}
                    {!viewOnly && (
                      <button
                        type="button"
                        onClick={() => setTags((cur) => cur.filter((x) => x !== t))}
                        style={{ border: "none", background: "transparent", color: "#6366f1", cursor: "pointer", fontSize: 13, lineHeight: 1, padding: 0 }}
                        title="Remove tag"
                      >
                        ✕
                      </button>
                    )}
                  </span>
                ))}
              </div>
            )}
            {!viewOnly && (
              <>
                <input
                  value={tagDraft}
                  onChange={(e) => setTagDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === ",") {
                      e.preventDefault();
                      addTag(tagDraft);
                    } else if (e.key === "Backspace" && tagDraft === "" && tags.length > 0) {
                      setTags((cur) => cur.slice(0, -1));
                    }
                  }}
                  onBlur={() => addTag(tagDraft)}
                  list="wizard-tag-suggestions"
                  style={inputStyle}
                  placeholder="Add a tag…"
                />
                <datalist id="wizard-tag-suggestions">
                  {tagSuggestions
                    .filter((s) => !tags.some((t) => t.toLowerCase() === s.toLowerCase()))
                    .map((s) => <option key={s} value={s} />)}
                </datalist>
              </>
            )}
          </Field>
          <Field label="Product intent">
            <div style={{ fontSize: 12, color: "#475569", marginBottom: 8 }}>
              Will other data products build on this, or does it serve a specific consumer?
              {!productKindTouched && (
                <span style={{ color: "#94a3b8" }}> (auto-suggested from your sources — change any time)</span>
              )}
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {([
                {
                  key: "aggregate" as const,
                  label: "Aggregate",
                  blurb: "A reusable building block other products will consume further.",
                  activeBg: "#ede9fe", activeFg: "#6d28d9", activeBorder: "#c4b5fd",
                },
                {
                  key: "consumer" as const,
                  label: "Consumer-aligned",
                  blurb: "A fit-for-purpose product serving a specific consumer.",
                  activeBg: "#fde4d4", activeFg: "#b86a1d", activeBorder: "#fdba74",
                },
              ]).map((opt) => {
                const active = productKind === opt.key;
                return (
                  <button
                    key={opt.key}
                    type="button"
                    disabled={viewOnly}
                    onClick={() => { setProductKind(opt.key); setProductKindTouched(true); }}
                    style={{
                      flex: "1 1 220px",
                      textAlign: "left",
                      padding: "10px 12px",
                      borderRadius: 8,
                      cursor: viewOnly ? "default" : "pointer",
                      backgroundColor: active ? opt.activeBg : "#fff",
                      color: active ? opt.activeFg : "#334155",
                      border: `1px solid ${active ? opt.activeBorder : "#cbd5e1"}`,
                    }}
                  >
                    <div style={{ fontWeight: 700, fontSize: 13 }}>
                      {active ? "✓ " : ""}{opt.label}
                    </div>
                    <div style={{ fontSize: 11, color: active ? opt.activeFg : "#64748b", marginTop: 2 }}>
                      {opt.blurb}
                    </div>
                  </button>
                );
              })}
            </div>
            {productKind === "aggregate" && (
              <div style={{ fontSize: 11, color: "#6d28d9", marginTop: 6 }}>
                Aggregates default to a <strong>materialized</strong> serving mode so downstream
                consumers read a real table — you can still choose Virtual later.
              </div>
            )}
          </Field>
          {submitError && <div style={{ color: "#dc2626", fontSize: 13 }}>{submitError}</div>}
        </StepCard>
      )}

      {step === 6 && (
        <StepCard
          title="Operations & support"
          description="Operational details for the ODCS contract. All optional — servers are filled in automatically from where your product is deployed; SLAs default to what your upstream products promise."
          onBack={() => setStep(5)}
          canAdvance={true}
          onAdvance={advanceToRuleCoach}
          advanceLabel={provisioningProject ? "Saving…" : rulesLoading ? "Loading rules..." : "Next: Rule Coach →"}
          disableAdvance={provisioningProject || rulesLoading}
          onGuide={() => guideMe("operations", "Help me fill in SLAs, contacts, and roles for this product")}
          onSaveDraft={viewOnly ? undefined : saveDraft}
          savingDraft={savingDraft}
          canSaveDraft={true}
        >
          {/* Servers — read-only, derived from the deployment on publish. */}
          <div>
            <div style={opsSectionLabel}>Servers <span style={opsHint}>· auto-derived from deployment</span></div>
            {servers.length === 0 ? (
              <div style={opsEmpty}>
                No servers yet. This is filled in automatically from where your product is
                deployed (schema, database, and the served view names) the first time you deploy.
              </div>
            ) : (
              servers.map((s, i) => (
                <div key={i} style={opsReadonlyRow}>
                  <span style={{ fontWeight: 600, color: "#1e293b" }}>{s.name || s.type || "server"}</span>
                  <span style={opsHint}>
                    {[s.type, s.environment].filter(Boolean).join(" · ")}
                    {s.database ? ` · ${s.database}` : ""}{s.schema ? `.${s.schema}` : ""}
                    {s.datasets.length ? ` · ${s.datasets.length} view(s)` : ""}
                  </span>
                </div>
              ))
            )}
          </div>

          {/* SLA — editable, inherited from sources by default. */}
          <div>
            <div style={opsSectionLabel}>
              Service levels (SLA)
              {slaSuggestLoading && <span style={opsHint}> · checking sources…</span>}
            </div>
            {slaProps.map((s, i) => (
              <div key={i} style={opsEditRow}>
                <input
                  value={s.property}
                  onChange={(e) => setSlaProps((p) => p.map((r, j) => j === i ? { ...r, property: e.target.value } : r))}
                  style={{ ...inputStyle, flex: 2 }}
                  placeholder="property (e.g. freshness, availability)"
                />
                <input
                  value={s.value}
                  onChange={(e) => setSlaProps((p) => p.map((r, j) => j === i ? { ...r, value: e.target.value } : r))}
                  style={{ ...inputStyle, flex: 1 }}
                  placeholder="value"
                />
                <input
                  value={s.unit}
                  onChange={(e) => setSlaProps((p) => p.map((r, j) => j === i ? { ...r, unit: e.target.value } : r))}
                  style={{ ...inputStyle, flex: 1 }}
                  placeholder="unit"
                />
                {s.inheritedFrom && <span style={opsBadge} title={`Inherited from ${s.inheritedFrom}`}>inherited</span>}
                <button type="button" style={opsRemoveBtn} onClick={() => setSlaProps((p) => p.filter((_, j) => j !== i))}>✕</button>
              </div>
            ))}
            <button type="button" style={opsAddBtn} onClick={() => setSlaProps((p) => [...p, { property: "", value: "", unit: "" }])}>
              + Add SLA
            </button>
            {(() => {
              const recs = recommendedSlas(domain).filter((rec) => !slaProps.some((s) => s.property.trim().toLowerCase() === rec.property));
              if (recs.length === 0) return null;
              return (
                <div style={opsRecRow}>
                  <span style={opsHint}>Recommended:</span>
                  {recs.map((rec) => (
                    <button key={rec.property} type="button" style={opsRecChip}
                      onClick={() => setSlaProps((p) => [...p, rec])}
                      title={`Add a ${rec.property} SLA of ${rec.value} ${rec.unit}`}>
                      + {rec.property} {rec.value} {rec.unit}
                    </button>
                  ))}
                  <button type="button" style={opsRecAllBtn}
                    onClick={() => setSlaProps((p) => [...p, ...recs.filter((rec) => !p.some((s) => s.property.trim().toLowerCase() === rec.property))])}>
                    + Add all
                  </button>
                </div>
              );
            })()}
          </div>

          {/* Team / contacts — editable, defaults to the owner. */}
          <div>
            <div style={opsSectionLabel}>Team &amp; contacts</div>
            {teamMembers.map((t, i) => (
              <div key={i} style={opsEditRow}>
                <input
                  value={t.name}
                  onChange={(e) => setTeamMembers((p) => p.map((r, j) => j === i ? { ...r, name: e.target.value } : r))}
                  style={{ ...inputStyle, flex: 2 }}
                  placeholder="name"
                />
                <input
                  value={t.role}
                  onChange={(e) => setTeamMembers((p) => p.map((r, j) => j === i ? { ...r, role: e.target.value } : r))}
                  style={{ ...inputStyle, flex: 1 }}
                  placeholder="role"
                />
                <input
                  value={t.email}
                  onChange={(e) => setTeamMembers((p) => p.map((r, j) => j === i ? { ...r, email: e.target.value } : r))}
                  style={{ ...inputStyle, flex: 2 }}
                  placeholder="email"
                />
                <button type="button" style={opsRemoveBtn} onClick={() => setTeamMembers((p) => p.filter((_, j) => j !== i))}>✕</button>
              </div>
            ))}
            <button type="button" style={opsAddBtn} onClick={() => setTeamMembers((p) => [...p, { name: "", role: "", email: "" }])}>
              + Add contact
            </button>
          </div>

          {/* Roles — editable, optional access definitions. */}
          <div>
            <div style={opsSectionLabel}>Access roles <span style={opsHint}>· optional</span></div>
            {roles.map((r, i) => (
              <div key={i} style={opsEditRow}>
                <input
                  value={r.role}
                  onChange={(e) => setRoles((p) => p.map((x, j) => j === i ? { ...x, role: e.target.value } : x))}
                  style={{ ...inputStyle, flex: 1 }}
                  placeholder="role"
                />
                <input
                  value={r.access}
                  onChange={(e) => setRoles((p) => p.map((x, j) => j === i ? { ...x, access: e.target.value } : x))}
                  style={{ ...inputStyle, flex: 1 }}
                  placeholder="access (e.g. read)"
                />
                <input
                  value={r.description}
                  onChange={(e) => setRoles((p) => p.map((x, j) => j === i ? { ...x, description: e.target.value } : x))}
                  style={{ ...inputStyle, flex: 2 }}
                  placeholder="description"
                />
                <button type="button" style={opsRemoveBtn} onClick={() => setRoles((p) => p.filter((_, j) => j !== i))}>✕</button>
              </div>
            ))}
            <button type="button" style={opsAddBtn} onClick={() => setRoles((p) => [...p, { role: "", access: "", description: "" }])}>
              + Add role
            </button>
            {(() => {
              const recs = RECOMMENDED_ROLES.filter((rec) => !roles.some((r) => r.role.trim().toLowerCase() === rec.role));
              if (recs.length === 0) return null;
              return (
                <div style={opsRecRow}>
                  <span style={opsHint}>Recommended:</span>
                  {recs.map((rec) => (
                    <button key={rec.role} type="button" style={opsRecChip}
                      onClick={() => setRoles((p) => [...p, rec])}
                      title={rec.description}>
                      + {rec.role} ({rec.access})
                    </button>
                  ))}
                  <button type="button" style={opsRecAllBtn}
                    onClick={() => setRoles((p) => [...p, ...recs.filter((rec) => !p.some((r) => r.role.trim().toLowerCase() === rec.role))])}>
                    + Add all
                  </button>
                </div>
              );
            })()}
          </div>
          {submitError && <div style={{ color: "#dc2626", fontSize: 13 }}>{submitError}</div>}
        </StepCard>
      )}

      {step === 7 && (
        <StepCard
          title="Review suggested quality rules"
          description="Rules are sourced from the domain catalog and attached to your product columns. Approved rules ride along to engineering."
          onBack={() => setStep(6)}
          canAdvance={!isEditMode || !deployedSnapshot || editChangesAcknowledged}
          onAdvance={() => setStep(8)}
          advanceLabel="Continue to OSI Readiness →"
          disableAdvance={isEditMode && !!deployedSnapshot && !editChangesAcknowledged}
          onGuide={() => guideMe("rules", "Help me decide which rules to approve")}
          onSaveDraft={viewOnly ? undefined : saveDraft}
          savingDraft={savingDraft}
          canSaveDraft={true}
        >
          {isEditMode && deployedSnapshot && (
            <>
              <EditDiffPanel deployed={deployedSnapshot} current={currentSnapshot} reviewMode={true} />
              {createdProjectId && (
                <ImpactPreviewPanel
                  projectId={createdProjectId}
                  resolvedKind={null}
                  revisionNotes={revisionNotes}
                  onRevisionNotesChange={setRevisionNotes}
                  changeKindOverride={changeKindOverride}
                  onChangeKindOverrideChange={setChangeKindOverride}
                />
              )}
              <label
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  marginBottom: 14,
                  padding: 10,
                  borderRadius: 8,
                  backgroundColor: "#f0f9ff",
                  border: "1px solid #bae6fd",
                  fontSize: 13,
                  color: "#0c4a6e",
                  cursor: "pointer",
                }}
              >
                <input
                  type="checkbox"
                  checked={editChangesAcknowledged}
                  onChange={(e) => setEditChangesAcknowledged(e.target.checked)}
                />
                <span>I've reviewed the changes above and want to submit this revision for engineering rework.</span>
              </label>
            </>
          )}
          {rules.length === 0 && (
            <div style={{ color: "#64748b", fontSize: 13 }}>
              No catalog-driven rule suggestions for this schema. You can still submit; engineering will baseline rules from observed data.
            </div>
          )}
          {rules.length > 0 && (
            <div
              style={{
                padding: "8px 10px",
                borderRadius: 8,
                backgroundColor: "#f1f5f9",
                color: "#334155",
                fontSize: 12,
                marginBottom: 8,
                lineHeight: 1.5,
              }}
            >
              All catalog-suggested rules are <strong>pre-approved</strong>. Reject any that
              don&apos;t fit your product. Approved rules become DQ tests and contribute to the
              product&apos;s quality score once the engineer runs scoring.
            </div>
          )}
          {Array.from(rulesByColumn.entries()).map(([col, colRules]) => (
            <div key={col} style={{ marginBottom: 16 }}>
              <div style={{ fontWeight: 700, color: "#0f172a", marginBottom: 6 }}>{col}</div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {colRules.map((r) => {
                  const isApproved = approvedRules.has(r.rule_uri);
                  const isRejected = rejectedRules.has(r.rule_uri);
                  const isUserRule = r.source === "user";
                  return (
                    <div
                      key={r.rule_uri}
                      style={{
                        display: "grid",
                        gridTemplateColumns: "1fr auto auto",
                        gap: 10,
                        padding: 10,
                        borderRadius: 8,
                        border: `1px solid ${isApproved ? "#86efac" : isRejected ? "#fca5a5" : "#e2e8f0"}`,
                        backgroundColor: isApproved ? "#f0fdf4" : isRejected ? "#fef2f2" : "#fff",
                        alignItems: "center",
                      }}
                    >
                      <div>
                        <div style={{ fontSize: 13, color: "#0f172a", display: "flex", alignItems: "center", gap: 6 }}>
                          {r.description}
                          {isUserRule && (
                            <span
                              style={{
                                fontSize: 10,
                                fontWeight: 600,
                                color: productTheme.accent,
                                backgroundColor: productTheme.accentSoft,
                                padding: "1px 6px",
                                borderRadius: 4,
                                letterSpacing: 0.3,
                              }}
                              title="You authored this rule via the assistant"
                            >
                              YOU
                            </span>
                          )}
                        </div>
                        <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2 }}>
                          {r.rule_type} · {r.severity}
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => approve(r.rule_uri)}
                        style={smallButtonStyle(isApproved ? "#059669" : "#e2e8f0", isApproved ? "#fff" : "#334155")}
                      >
                        {isApproved ? "✓ Approved" : "Approve"}
                      </button>
                      <button
                        type="button"
                        onClick={() => reject(r.rule_uri)}
                        style={smallButtonStyle(isRejected ? "#dc2626" : "#e2e8f0", isRejected ? "#fff" : "#334155")}
                      >
                        {isRejected ? "✗ Rejected" : "Reject"}
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
          {submitError && <div style={{ color: "#dc2626", fontSize: 13 }}>{submitError}</div>}
        </StepCard>
      )}

      {step === 8 && (
        <StepCard
          title="Readiness Review (optional)"
          description="Two pre-flight checks before you confirm source candidates. Both are optional — skip either or both if you're ready to move on."
          onBack={() => setStep(7)}
          canAdvance={!submitting}
          onAdvance={() => setStep(9)}
          advanceLabel="Continue: Confirm sources →"
          disableAdvance={submitting}
          onGuide={() => guideMe("osi", "Help me improve my OSI score")}
          onSaveDraft={viewOnly ? undefined : saveDraft}
          savingDraft={savingDraft}
          canSaveDraft={true}
        >
          <ReviewSection
            title="Serving Strategy"
            blurb="How should this product be served? A live SQL view (simplest, always fresh), dbt-materialized tables/snapshots (for SCD2 history or heavy aggregation), a portable Parquet + DuckDB lakehouse file hop, or a cross-platform transfer when the target is a different engine. Pick a preference — the engineer confirms the concrete mode and target at Configure Serving."
          >
            <ServingStrategyAdvice
              signals={{
                archetype: "dpe-cf",
                cross_platform: false,
                product_kind: productKind,
                datasets: [{
                  name: datasetName || `${domain}_core`,
                  scd_policy: shapeScdPolicy,
                  grouping: groupingKeys.size > 0,
                }],
              }}
              chosen={servingPreference}
              onChoose={(mode, reason) => { setServingPreference(mode); setServingReason(reason); }}
            />
          </ReviewSection>

          {createdProjectId === null ? (
            <div style={{ color: "#64748b", fontSize: 13 }}>
              Both analyses are computed against the persisted contract. Save a draft from earlier steps to enable them.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <ReviewSection
                title="Pre-flight Coherence"
                blurb="Does the dataset's grain prose name concepts the schema actually delivers? And when grouping keys are set, do all non-grouping columns carry the aggregateFunction the grouped view needs to emit correct aggregates?"
              >
                <PreflightPanel projectId={createdProjectId} />
              </ReviewSection>

              <ReviewSection
                title="OSI Readiness Score"
                blurb="How semantically interchangeable is this product? A composite of completeness (descriptions, owners, terms) and conformance (ODCS structural validity)."
              >
                <OsiAnalysisPanel projectId={createdProjectId} scoreOnDemand={true} />
              </ReviewSection>

              <ReviewSection
                title="Question Analysis"
                blurb="What can consumers actually answer with this product? The analyzer generates representative questions a consumer might ask and flags near-miss gaps you might want to fill before publishing."
              >
                <QuestionsPanel
                  projectId={createdProjectId}
                  canRegenerate
                  canProbe
                  persistOnRegenerate={false}
                  isDeployed={false}
                />
              </ReviewSection>
            </div>
          )}
          {submitError && <div style={{ color: "#dc2626", fontSize: 13, marginTop: 12 }}>{submitError}</div>}
        </StepCard>
      )}

      {step === 10 && (
        <StepCard
          title="Submitted"
          description="Your product has been handed off to engineering. They'll run discovery, profiling, and mapping and report back."
          onAdvance={() => navigate("/product/my-products")}
          advanceLabel="View My Products"
          canAdvance={true}
        >
          <div
            style={{
              padding: 16,
              borderRadius: 10,
              backgroundColor: "#dcfce7",
              border: "1px solid #86efac",
              color: "#065f46",
            }}
          >
            Request <strong>#{submittedRequestId}</strong> submitted.
            {' '}Engineering will pick it up from the Incoming queue in the Engineering Workbench.
          </div>
        </StepCard>
      )}

      <ProductChatPanel
        open={chatOpen}
        onClose={() => setChatOpen(false)}
        ownerEmail={CURRENT_USER_EMAIL}
        projectId={createdProjectId}
        request={guideRequest}
        onApply={handleApply}
        getContext={getWizardContext}
      />
    </div>
  );
}

// Phase 3+4+5 PO authoring step: dataset-level shape. Writes the
// schema-level transform block on the ODCS spec, which backend
// canonicalisation lowers to :DatasetTransform (filter + dedupe shipped,
// grouping_keys + joins[] partial, scd_policy reserved).
//
// Four panels per design §4 — grain prose, sources/joins (auto-bridge note
// for v1), filter predicate, SCD history. The grain prose is captured as
// context now; column-level grouping_keys multi-select happens in step 4
// once product columns are known.
function ShapePanel({
  grain, onGrainChange,
  filterIntent, onFilterIntentChange, onCheckFilter,
  filterChecking, filterReadback, filterConfidence, filterWarnings,
  filterFallback, filterLegacyNote,
  scdPolicy, onScdPolicyChange,
}: {
  grain: string;
  onGrainChange: (s: string) => void;
  filterIntent: string;
  onFilterIntentChange: (s: string) => void;
  onCheckFilter: () => void;
  filterChecking: boolean;
  filterReadback: string;
  filterConfidence: number | null;
  filterWarnings: string[];
  filterFallback: boolean;
  filterLegacyNote: boolean;
  scdPolicy: string;
  onScdPolicyChange: (s: string) => void;
}) {
  const panelStyle: React.CSSProperties = {
    padding: 14,
    borderRadius: 8,
    border: "1px solid #e2e8f0",
    backgroundColor: "#fff",
    marginBottom: 14,
  };
  const headerStyle: React.CSSProperties = {
    fontSize: 13,
    fontWeight: 700,
    color: "#0f172a",
    marginBottom: 4,
  };
  const helpStyle: React.CSSProperties = {
    fontSize: 12,
    color: "#64748b",
    marginBottom: 10,
    lineHeight: 1.4,
  };
  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      <div style={panelStyle}>
        <div style={headerStyle}>1. One row per what?</div>
        <div style={helpStyle}>
          Declare the grain in plain language — &quot;one row per customer&quot;,
          &quot;one row per order-line&quot;, &quot;one row per (store, day)&quot;.
          If your sources are at a finer grain than the product, engineering will
          add aggregation or dedupe later.
        </div>
        <input
          value={grain}
          onChange={(e) => onGrainChange(e.target.value)}
          placeholder="one row per customer"
          style={{
            width: "100%", boxSizing: "border-box",
            padding: "8px 10px", borderRadius: 4,
            border: "1px solid #cbd5e1", fontSize: 13,
          }}
        />
      </div>

      <div style={panelStyle}>
        <div style={headerStyle}>2. How do the sources connect?</div>
        <div style={helpStyle}>
          You picked upstream products in the previous step. The view generator
          auto-discovers joins through their FK graphs and picks the best
          junction when multiple paths exist. You don&apos;t need to declare
          joins by hand for typical multi-table cases. If you ever need to
          override the inferred join (e.g. pick a non-default junction table),
          engineering can declare an explicit join via
          <code style={{ fontFamily: "ui-monospace, monospace", fontSize: 12 }}>
            {" "}:DatasetTransform.joins[]{" "}
          </code>
          on the dataset.
        </div>
        <div style={{
          fontSize: 11, color: "#64748b", padding: "8px 10px",
          backgroundColor: "#f8fafc", borderRadius: 4,
          border: "1px dashed #cbd5e1",
        }}>
          Auto-bridge is on. Engineer can override on the consumer side if needed.
        </div>
      </div>

      <div style={panelStyle}>
        <div style={headerStyle}>3. Which rows should appear?</div>
        <div style={helpStyle}>
          Describe in plain words which rows to include — e.g.
          &quot;active employees only&quot; or &quot;exclude deleted records&quot;.
          Click <strong>Check filter</strong> and we&apos;ll read it back to make sure we
          understood; engineering finalizes the exact SQL. Leave blank to include every row.
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
          <input
            value={filterIntent}
            onChange={(e) => onFilterIntentChange(e.target.value)}
            placeholder="active employees only"
            style={{
              flex: 1, boxSizing: "border-box",
              padding: "8px 10px", borderRadius: 4,
              border: "1px solid #cbd5e1", fontSize: 13,
            }}
          />
          <button
            type="button"
            onClick={onCheckFilter}
            disabled={filterChecking || !filterIntent.trim()}
            style={{
              padding: "8px 14px", borderRadius: 4, border: "1px solid #0ea5e9",
              backgroundColor: filterChecking || !filterIntent.trim() ? "#e2e8f0" : "#e0f2fe",
              color: "#075985", fontSize: 12, fontWeight: 600, whiteSpace: "nowrap",
              cursor: filterChecking || !filterIntent.trim() ? "not-allowed" : "pointer",
            }}
          >
            {filterChecking ? "Checking…" : "Check filter"}
          </button>
        </div>
        {filterLegacyNote && (
          <div style={{
            marginTop: 8, padding: "6px 10px", borderRadius: 4, fontSize: 11,
            backgroundColor: "#fffbeb", color: "#92400e", border: "1px solid #fde68a",
          }}>
            This filter was authored as SQL. Type a plain-language description above and
            click Check filter to replace it; otherwise the existing filter is kept as-is.
          </div>
        )}
        {filterReadback && (
          <div style={{
            marginTop: 8, padding: "8px 10px", borderRadius: 4,
            backgroundColor: "#f0fdf4", color: "#166534", fontSize: 12, lineHeight: 1.4,
          }}>
            <strong>✓ Reading as:</strong> {filterReadback}
            {typeof filterConfidence === "number" && (
              <span style={{ color: "#15803d", marginLeft: 6 }}>
                ({filterConfidence}% confident{filterFallback ? ", heuristic" : ""})
              </span>
            )}
            <div style={{ color: "#15803d", marginTop: 4, fontSize: 11 }}>
              Engineering will finalize the exact SQL.
            </div>
          </div>
        )}
        {filterWarnings.length > 0 && (
          <div style={{
            marginTop: 8, padding: "8px 10px", borderRadius: 4,
            backgroundColor: "#fffbeb", color: "#92400e", fontSize: 11, lineHeight: 1.4,
          }}>
            {filterWarnings.map((w, i) => <div key={i}>• {w}</div>)}
            {filterFallback && (
              <div style={{ marginTop: 4, color: "#a16207" }}>
                (heuristic interpretation — install the filter-intent-interpreter skill for richer grounding)
              </div>
            )}
          </div>
        )}
      </div>

      <div style={panelStyle}>
        <div style={headerStyle}>4. Show history or only the current state?</div>
        <div style={helpStyle}>
          Most analytical products want the current state. Pick &quot;Full history&quot;
          if downstream consumers need to see how values changed over time, or
          &quot;Daily snapshot&quot; if you want a point-in-time copy each day.
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {[
            { value: "", label: "Not specified", help: "Defer the decision; engineering picks a default." },
            { value: "latest_only", label: "Latest only (SCD-1)", help: "Overwrite older rows; the product holds the current state per natural key." },
            { value: "scd2", label: "Full history (SCD-2)", help: "Keep every version with effective_from / effective_to columns." },
            { value: "snapshot", label: "Daily snapshot", help: "Periodic full copy at a point in time." },
          ].map((opt) => (
            <label
              key={opt.value}
              style={{
                display: "flex", gap: 10, padding: "8px 10px",
                borderRadius: 4, cursor: "pointer",
                backgroundColor: scdPolicy === opt.value ? "#eff6ff" : "transparent",
                border: `1px solid ${scdPolicy === opt.value ? "#3b82f6" : "#e2e8f0"}`,
              }}
            >
              <input
                type="radio"
                checked={scdPolicy === opt.value}
                onChange={() => onScdPolicyChange(opt.value)}
                style={{ marginTop: 2 }}
              />
              <div>
                <div style={{ fontSize: 13, fontWeight: 600, color: "#0f172a" }}>{opt.label}</div>
                <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>{opt.help}</div>
              </div>
            </label>
          ))}
        </div>
        <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 10, fontStyle: "italic" }}>
          Note: SCD rendering is a Phase 5 item — your choice is persisted on the contract today but not yet rendered into the view DDL. The engineer side can layer it in once Phase 5 ships.
        </div>
      </div>
    </div>
  );
}

function blankCustomColumn(): CustomColumn {
  return { name: "", logical_type: "string", physical_type: "varchar(255)", description: "", primary_key: false };
}

const DERIVE_KINDS = [
  { value: "concat", label: "Concatenate columns" },
  { value: "cast", label: "Cast (type conversion)" },
  { value: "format", label: "Format (case / date / trim)" },
  { value: "split", label: "Split" },
  { value: "lookup", label: "Lookup table" },
  { value: "bucket", label: "Bucket (binning into bands)" },
  { value: "mask", label: "Mask (format-preserving redaction)" },
  { value: "hash", label: "Hash (irreversible digest)" },
  { value: "expression", label: "Custom expression (engineer fills SQL)" },
];

/**
 * Phase 6 window-specs editor. PO declares named windows (PARTITION BY +
 * ORDER BY + optional FRAME) that column-level mappings can reference via
 * transformKind='window'. Engineers wire the references at mapping time;
 * this panel is the declaration side.
 *
 * Collapsed by default — most products don't need windows. partition_by and
 * order_by are free-text comma fields because they often reference SOURCE
 * column names (not product columns) and the PO is expected to know the
 * underlying schema by the time they're declaring windows.
 */
function WindowSpecsEditor({
  specs, onChange,
}: {
  specs: { name: string; partition_by: string; order_by: string; frame: string }[];
  onChange: (next: typeof specs) => void;
}) {
  const [open, setOpen] = useState(specs.length > 0);
  const addSpec = () => onChange([...specs, { name: "", partition_by: "", order_by: "", frame: "" }]);
  const updateSpec = (i: number, patch: Partial<typeof specs[number]>) =>
    onChange(specs.map((s, idx) => (idx === i ? { ...s, ...patch } : s)));
  const removeSpec = (i: number) => onChange(specs.filter((_, idx) => idx !== i));
  return (
    <div style={{ marginTop: 14, border: "1px solid #cbd5e1", borderRadius: 8, backgroundColor: "#f8fafc" }}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        style={{
          width: "100%", padding: "8px 12px", border: "none", background: "transparent",
          textAlign: "left", cursor: "pointer", display: "flex", alignItems: "center",
          gap: 8, fontSize: 12, fontWeight: 700, color: "#334155",
          textTransform: "uppercase", letterSpacing: 0.4,
        }}
      >
        <span style={{ fontSize: 13 }}>{open ? "▾" : "▸"}</span>
        <span>Window specs (advanced)</span>
        {specs.length > 0 && (
          <span style={{ fontWeight: 400, color: "#64748b", textTransform: "none" }}>
            {specs.length} declared
          </span>
        )}
      </button>
      {open && (
        <div style={{ padding: "4px 12px 12px 12px" }}>
          <div style={{ fontSize: 12, color: "#475569", marginBottom: 10, lineHeight: 1.5 }}>
            Named window definitions for column-level transforms that need LAG / LEAD / ROW_NUMBER / running aggregates / etc.
            Each window compiles to a SQL <code>OVER (...)</code> clause. Engineers reference these by name when authoring mappings.
            Column references can be source-table columns (not necessarily on the product schema).
          </div>
          {specs.map((s, i) => (
            <div
              key={i}
              style={{
                marginBottom: 10, padding: 10, borderRadius: 6,
                border: "1px solid #cbd5e1", backgroundColor: "#fff",
                display: "grid", gridTemplateColumns: "1fr auto", gap: 8, alignItems: "start",
              }}
            >
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <input
                  type="text"
                  value={s.name}
                  onChange={(e) => updateSpec(i, { name: e.target.value })}
                  placeholder="window name (e.g. by_dept_hire_desc)"
                  style={{ padding: "5px 8px", fontSize: 13, fontWeight: 600, border: "1px solid #cbd5e1", borderRadius: 4 }}
                />
                <input
                  type="text"
                  value={s.partition_by}
                  onChange={(e) => updateSpec(i, { partition_by: e.target.value })}
                  placeholder="PARTITION BY columns (comma-separated, e.g. department_id, region)"
                  style={{ padding: "5px 8px", fontSize: 12, fontFamily: "'Fira Code', monospace", border: "1px solid #cbd5e1", borderRadius: 4 }}
                />
                <input
                  type="text"
                  value={s.order_by}
                  onChange={(e) => updateSpec(i, { order_by: e.target.value })}
                  placeholder="ORDER BY (comma-separated, e.g. hire_date desc, salary desc)"
                  style={{ padding: "5px 8px", fontSize: 12, fontFamily: "'Fira Code', monospace", border: "1px solid #cbd5e1", borderRadius: 4 }}
                />
                <input
                  type="text"
                  value={s.frame}
                  onChange={(e) => updateSpec(i, { frame: e.target.value })}
                  placeholder="frame clause, optional (e.g. ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
                  style={{ padding: "5px 8px", fontSize: 12, fontFamily: "'Fira Code', monospace", border: "1px solid #cbd5e1", borderRadius: 4 }}
                />
              </div>
              <button
                type="button"
                onClick={() => removeSpec(i)}
                style={{
                  padding: "4px 10px", borderRadius: 4, border: "1px solid #fecaca",
                  backgroundColor: "#fef2f2", color: "#991b1b", fontSize: 11,
                  fontWeight: 600, cursor: "pointer",
                }}
                title="Remove this window declaration"
              >
                Remove
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={addSpec}
            style={{
              padding: "4px 12px", borderRadius: 4, border: "1px solid #0ea5e9",
              backgroundColor: "#e0f2fe", color: "#075985", fontSize: 12,
              fontWeight: 600, cursor: "pointer",
            }}
          >
            + Add window
          </button>
        </div>
      )}
    </div>
  );
}


/**
 * SCD-2 sub-policy authoring. Visible only when shapeScdPolicy === 'scd2'.
 *
 * SCD-2 preserves history: the source table keeps multiple rows per natural
 * key, each carrying a validity period. The PO declares which product columns
 * carry that period so view-DDL can (a) validate they exist and (b) optionally
 * append a derived `is_current` boolean for consumers who want to filter to
 * current rows easily.
 *
 * Unlike SCD-1 (which view-DDL synthesizes from PK + temporal-pattern match),
 * SCD-2 requires explicit declaration — there's no way to guess which of
 * several timestamp-shaped columns is the validity start vs end.
 */
function Scd2PolicyPanel({
  effectiveColumn,
  expirationColumn,
  addIsCurrent,
  productColumnNames,
  onEffectiveChange,
  onExpirationChange,
  onAddIsCurrentChange,
}: {
  effectiveColumn: string;
  expirationColumn: string;
  addIsCurrent: boolean;
  productColumnNames: string[];
  onEffectiveChange: (v: string) => void;
  onExpirationChange: (v: string) => void;
  onAddIsCurrentChange: (v: boolean) => void;
}) {
  const empty = productColumnNames.length === 0;
  return (
    <div
      style={{
        marginBottom: 14, padding: 12, borderRadius: 8,
        backgroundColor: "#eff6ff", border: "1px solid #93c5fd",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 8 }}>
        <div style={{ fontSize: 12, fontWeight: 700, color: "#1e3a8a", textTransform: "uppercase", letterSpacing: 0.4 }}>
          SCD-2 policy
        </div>
        <span style={{ fontSize: 10, color: "#1e40af" }}>required for history-preserving views</span>
      </div>
      <div style={{ fontSize: 12, color: "#1e3a8a", marginBottom: 10, lineHeight: 1.5 }}>
        SCD-2 preserves history — multiple rows per natural key, each carrying a validity period.
        Pick which product columns carry the validity start / end so consumers can interpret the data.
      </div>
      {empty ? (
        <div style={{ fontSize: 12, color: "#475569", fontStyle: "italic" }}>
          Pick or add at least one product column below — the dropdowns populate from your schema.
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
            <span style={{ color: "#1e3a8a", fontWeight: 600 }}>Effective from</span>
            <select
              value={effectiveColumn}
              onChange={(e) => onEffectiveChange(e.target.value)}
              style={{ padding: "5px 8px", borderRadius: 4, border: "1px solid #93c5fd", fontSize: 13, backgroundColor: "#fff" }}
            >
              <option value="">— select column —</option>
              {productColumnNames.map((nm) => <option key={nm} value={nm}>{nm}</option>)}
            </select>
          </label>
          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
            <span style={{ color: "#1e3a8a", fontWeight: 600 }}>Expiration</span>
            <select
              value={expirationColumn}
              onChange={(e) => onExpirationChange(e.target.value)}
              style={{ padding: "5px 8px", borderRadius: 4, border: "1px solid #93c5fd", fontSize: 13, backgroundColor: "#fff" }}
            >
              <option value="">— select column —</option>
              {productColumnNames.map((nm) => <option key={nm} value={nm}>{nm}</option>)}
            </select>
          </label>
        </div>
      )}
      <label
        style={{ display: "inline-flex", alignItems: "center", gap: 6, marginTop: 10, fontSize: 12, color: "#1e3a8a", cursor: "pointer" }}
        title="Append a portable boolean column: (CASE WHEN expiration IS NULL OR expiration > CURRENT_TIMESTAMP THEN true ELSE false END)"
      >
        <input
          type="checkbox"
          checked={addIsCurrent}
          onChange={(e) => onAddIsCurrentChange(e.target.checked)}
          disabled={!expirationColumn}
        />
        Add derived <code style={{ background: "#fff", padding: "1px 4px", borderRadius: 3 }}>is_current</code> boolean column
        {!expirationColumn && (
          <span style={{ marginLeft: 6, color: "#64748b" }}>(needs an expiration column)</span>
        )}
      </label>
    </div>
  );
}

function DeriveHintEditor({
  value,
  onChange,
  onClear,
}: {
  value: CustomColumnTransformHint | undefined;
  onChange: (next: CustomColumnTransformHint) => void;
  onClear: () => void;
}) {
  const v: CustomColumnTransformHint = value ?? { kind: "concat", inputs: [], separator: " " };
  const inputsCsv = v.inputs.join(", ");
  return (
    <div
      style={{
        marginTop: 4,
        padding: 10,
        borderRadius: 6,
        backgroundColor: "#fff",
        border: "1px dashed #cbd5e1",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div style={{ fontSize: 11, color: "#64748b" }}>
        Tell the engineer how this column should be derived. They'll resolve the source columns
        when they run mapping. You don't need to write SQL.
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <div>
          <label style={hintLabelStyle}>Kind</label>
          <select
            value={v.kind}
            onChange={(e) => onChange({ ...v, kind: e.target.value })}
            style={hintInputStyle}
          >
            {DERIVE_KINDS.map((k) => (
              <option key={k.value} value={k.value}>{k.label}</option>
            ))}
          </select>
        </div>
        {v.kind === "concat" && (
          <div>
            <label style={hintLabelStyle}>Separator</label>
            <input
              value={v.separator ?? " "}
              onChange={(e) => onChange({ ...v, separator: e.target.value })}
              style={hintInputStyle}
              placeholder="' '"
            />
          </div>
        )}
        {v.kind === "cast" && (
          <div>
            <label style={hintLabelStyle}>Target SQL type</label>
            <input
              value={(v.params?.target_type as string) ?? ""}
              onChange={(e) =>
                onChange({ ...v, params: { ...(v.params ?? {}), target_type: e.target.value } })
              }
              style={hintInputStyle}
              placeholder="VARCHAR(50), DATE, NUMERIC(10,2)"
            />
          </div>
        )}
        {v.kind === "hash" && (
          <div>
            <label style={hintLabelStyle}>Algorithm</label>
            <select
              value={(v.params?.algorithm as string) ?? "md5"}
              onChange={(e) =>
                onChange({ ...v, params: { ...(v.params ?? {}), algorithm: e.target.value } })
              }
              style={hintInputStyle}
            >
              <option value="md5">md5</option>
              <option value="sha1">sha1 (pgcrypto)</option>
              <option value="sha256">sha256 (pgcrypto)</option>
            </select>
          </div>
        )}
        {v.kind === "mask" && (
          <div>
            <label style={hintLabelStyle}>Algorithm</label>
            <select
              value={(v.params?.algorithm as string) ?? "keep_last"}
              onChange={(e) =>
                onChange({ ...v, params: { ...(v.params ?? {}), algorithm: e.target.value } })
              }
              style={hintInputStyle}
            >
              <option value="keep_last">Keep last N chars</option>
              <option value="keep_first">Keep first N chars</option>
              <option value="middle">Keep first &amp; last N (mask middle)</option>
            </select>
          </div>
        )}
      </div>

      {v.kind === "bucket" && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <div>
            <label style={hintLabelStyle}>Boundaries (comma-separated numbers)</label>
            <input
              value={Array.isArray(v.params?.boundaries) ? (v.params!.boundaries as number[]).join(", ") : ""}
              onChange={(e) => {
                const nums = e.target.value.split(",").map((s) => Number(s.trim())).filter((n) => !Number.isNaN(n));
                onChange({ ...v, params: { ...(v.params ?? {}), boundaries: nums } });
              }}
              style={hintInputStyle}
              placeholder="25, 50, 100"
            />
          </div>
          <div>
            <label style={hintLabelStyle}>Labels (comma-separated, N+1)</label>
            <input
              value={Array.isArray(v.params?.labels) ? (v.params!.labels as string[]).join(", ") : ""}
              onChange={(e) => {
                const labs = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
                onChange({ ...v, params: { ...(v.params ?? {}), labels: labs } });
              }}
              style={hintInputStyle}
              placeholder="low, medium, high, very_high"
            />
          </div>
        </div>
      )}

      {(v.kind === "mask" || v.kind === "hash") && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          {v.kind === "mask" && (
            <>
              <div>
                <label style={hintLabelStyle}>Keep N characters</label>
                <input
                  type="number" min={1}
                  value={(v.params?.keep_n as number) ?? 4}
                  onChange={(e) =>
                    onChange({ ...v, params: { ...(v.params ?? {}), keep_n: Number(e.target.value) } })
                  }
                  style={hintInputStyle}
                />
              </div>
              <div>
                <label style={hintLabelStyle}>Mask character</label>
                <input
                  value={((v.params?.mask_char as string) ?? "X").slice(0, 1)}
                  maxLength={1}
                  onChange={(e) =>
                    onChange({ ...v, params: { ...(v.params ?? {}), mask_char: e.target.value.slice(0, 1) || "X" } })
                  }
                  style={hintInputStyle}
                />
              </div>
            </>
          )}
          {v.kind === "hash" && (
            <div style={{ gridColumn: "1 / span 2" }}>
              <label style={hintLabelStyle}>Salt (optional namespace prefix)</label>
              <input
                value={(v.params?.salt as string) ?? ""}
                onChange={(e) =>
                  onChange({ ...v, params: { ...(v.params ?? {}), salt: e.target.value } })
                }
                style={hintInputStyle}
                placeholder="my-namespace"
              />
              <div style={{ fontSize: 10, color: "#b45309", marginTop: 2 }}>
                Not a cryptographic primitive — salt here is for determinism, not protection.
              </div>
            </div>
          )}
        </div>
      )}

      <div>
        <label style={hintLabelStyle}>
          Source column names (comma-separated)
        </label>
        <input
          value={inputsCsv}
          onChange={(e) =>
            onChange({
              ...v,
              inputs: e.target.value.split(",").map((s) => s.trim()).filter(Boolean),
            })
          }
          style={hintInputStyle}
          placeholder="first_name, last_name"
        />
        <div style={{ fontSize: 10, color: "#94a3b8", marginTop: 2 }}>
          These are aspirational names — the engineer's mapping stage resolves them to real source columns.
        </div>
      </div>

      {v.kind === "expression" && (
        <div>
          <label style={hintLabelStyle}>Sample expression (optional)</label>
          <input
            value={v.expression ?? ""}
            onChange={(e) => onChange({ ...v, expression: e.target.value })}
            style={{ ...hintInputStyle, fontFamily: "ui-monospace, monospace" }}
            placeholder="first_name || ' ' || last_name"
          />
        </div>
      )}

      <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
        <button type="button" onClick={onClear} style={smallButtonStyle("#fee2e2", "#991b1b")}>
          Remove derive hint
        </button>
      </div>
    </div>
  );
}

const hintLabelStyle: React.CSSProperties = {
  display: "block", fontSize: 11, fontWeight: 600, color: "#475569",
  textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 2,
};
const hintInputStyle: React.CSSProperties = {
  display: "block", width: "100%", boxSizing: "border-box",
  padding: "5px 8px", borderRadius: 4, border: "1px solid #cbd5e1",
  fontSize: 12,
};

function SchemaAdvisorTransition({
  domain,
  idea,
  onCancel,
}: {
  domain: string;
  idea: string;
  onCancel: () => void;
}) {
  return (
    <div
      style={{
        padding: 32,
        borderRadius: 12,
        backgroundColor: "#fff",
        border: `1px solid ${productTheme.accent}`,
        boxShadow: "0 6px 18px rgba(15, 23, 42, 0.08)",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 16 }}>
        <div
          style={{
            width: 18,
            height: 18,
            borderRadius: "50%",
            border: `3px solid ${productTheme.accentSoft}`,
            borderTopColor: productTheme.accent,
            animation: "spin 0.9s linear infinite",
          }}
        />
        <div style={{ fontSize: 18, fontWeight: 700, color: "#0f172a" }}>
          Looking for a starting point in the <span>{titleCaseDomain(domain)}</span> domain…
        </div>
      </div>
      <div style={{ fontSize: 13, color: "#475569", marginBottom: 14, lineHeight: 1.6 }}>
        Before you start authoring, we're checking three things:
        <ul style={{ margin: "8px 0 0 0", paddingLeft: 20 }}>
          <li style={{ marginBottom: 4 }}>
            <strong style={{ color: "#0f172a" }}>Similar existing products</strong> — has another team already built
            something like this in the {domain} domain that you could reuse or extend?
          </li>
          <li style={{ marginBottom: 4 }}>
            <strong style={{ color: "#0f172a" }}>Matching templates</strong> — is there a curated blueprint close
            enough to your idea that you could clone and edit?
          </li>
          <li>
            <strong style={{ color: "#0f172a" }}>Recommended columns</strong> — which columns from the {domain}{" "}
            catalog best match what you described?
          </li>
        </ul>
      </div>
      <div
        style={{
          padding: 12,
          borderRadius: 8,
          backgroundColor: "#f8fafc",
          fontSize: 13,
          color: "#0f172a",
          fontStyle: "italic",
          marginBottom: 18,
          borderLeft: `3px solid ${productTheme.accent}`,
        }}
      >
        “{idea}”
      </div>
      <button
        type="button"
        onClick={onCancel}
        style={{
          padding: "8px 14px",
          fontSize: 13,
          color: "#475569",
          backgroundColor: "transparent",
          border: "1px solid #cbd5e1",
          borderRadius: 6,
          cursor: "pointer",
        }}
      >
        Cancel
      </button>
      <style>{`@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

/** Collapsed sub-section at the bottom of the Step 4 catalog picker that
 *  buckets catalog columns the advisor flagged as `feasibility=missing_source`
 *  for the PO's selected source inputs. Keeps the main ranked list scannable
 *  while preserving access — POs may legitimately want a placeholder column
 *  with no current source backing. */
function NotSourceableSubSection({
  rows,
  columnDetails,
  showRationales,
  onPick,
}: {
  rows: CatalogColumn[];
  columnDetails: Record<string, ColumnDetail>;
  showRationales: boolean;
  onPick: (name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: 12 }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        style={{
          width: "100%",
          textAlign: "left",
          padding: "8px 10px",
          borderRadius: 6,
          border: "1px dashed #fdba74",
          backgroundColor: open ? "#fff7ed" : "transparent",
          color: "#9a3412",
          fontSize: 12,
          fontWeight: 600,
          cursor: "pointer",
        }}
        title="Catalog columns the advisor couldn't trace to any of your selected source inputs. You can still pick them — they'll surface a NO SOURCE chip until you add an upstream product that backs them."
      >
        {open ? "▾" : "▸"} Not sourceable from your inputs ({rows.length})
      </button>
      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }}>
          {rows.map((c) => (
            <div
              key={c.name}
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr auto",
                gap: 10,
                padding: 8,
                borderRadius: 6,
                border: "1px solid #fed7aa",
                backgroundColor: "#fffbeb",
                alignItems: "center",
              }}
            >
              <button
                type="button"
                onClick={() => onPick(c.name)}
                title="Add to selection — note this column has no current source backing."
                style={{
                  width: 28,
                  height: 28,
                  borderRadius: 4,
                  border: "1px solid #fdba74",
                  backgroundColor: "#fff",
                  color: "#9a3412",
                  fontSize: 16,
                  lineHeight: 1,
                  cursor: "pointer",
                  fontWeight: 700,
                }}
              >
                +
              </button>
              <div>
                <div style={{ fontWeight: 600, color: "#0f172a", fontSize: 13 }}>
                  {c.name}
                  {!showRationales && columnDetails[c.name]?.why && (
                    <span
                      title={columnDetails[c.name].why}
                      style={{ marginLeft: 8, fontSize: 11, color: "#94a3b8", cursor: "help", fontWeight: 400 }}
                    >
                      (?)
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 11, color: "#475569" }}>{c.description || "—"}</div>
                {showRationales && columnDetails[c.name]?.why && (
                  <div style={{ fontSize: 11, color: "#64748b", marginTop: 3, fontStyle: "italic", lineHeight: 1.4 }}>
                    Why: {columnDetails[c.name].why}
                  </div>
                )}
              </div>
              <code style={{ fontSize: 11, color: "#64748b" }}>{c.physical_type}</code>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


/** Collapsed sub-section at the bottom of the Step 4 catalog picker that
 *  buckets catalog columns whose advisor `grain_alignment` is "finer" — i.e.
 *  rows that would explode or arbitrarily collapse the declared grain.
 *  The backend safety net drops these from the LLM's own recommendations,
 *  so this only surfaces when (a) a deployed older skill version returned
 *  one anyway, or (b) the PO manually picks one from the catalog. Kept
 *  selectable so an informed PO can override, with the GRAIN MISMATCH chip
 *  loud on the row. */
function GrainMisalignedSubSection({
  rows,
  columnDetails,
  showRationales,
  declaredGrain,
  onPick,
}: {
  rows: CatalogColumn[];
  columnDetails: Record<string, ColumnDetail>;
  showRationales: boolean;
  declaredGrain: string;
  onPick: (name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const grainLabel = declaredGrain ? `finer than ${declaredGrain}` : "finer-grain";
  return (
    <div style={{ marginTop: 12 }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        style={{
          width: "100%",
          textAlign: "left",
          padding: "8px 10px",
          borderRadius: 6,
          border: "1px dashed #fca5a5",
          backgroundColor: open ? "#fef2f2" : "transparent",
          color: "#991b1b",
          fontSize: 12,
          fontWeight: 600,
          cursor: "pointer",
        }}
        title={`Catalog columns the advisor flagged as finer-grain than your declared grain (${declaredGrain || "—"}). Picking one would either explode rows or pick one row arbitrarily — consider a roll-up instead.`}
      >
        {open ? "▾" : "▸"} Grain-misaligned ({grainLabel}) ({rows.length})
      </button>
      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }}>
          {rows.map((c) => (
            <div
              key={c.name}
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr auto",
                gap: 10,
                padding: 8,
                borderRadius: 6,
                border: "1px solid #fecaca",
                backgroundColor: "#fef2f2",
                alignItems: "center",
              }}
            >
              <button
                type="button"
                onClick={() => onPick(c.name)}
                title="Add to selection — note this column is finer-grain than your declared grain."
                style={{
                  width: 28,
                  height: 28,
                  borderRadius: 4,
                  border: "1px solid #fca5a5",
                  backgroundColor: "#fff",
                  color: "#991b1b",
                  fontSize: 16,
                  lineHeight: 1,
                  cursor: "pointer",
                  fontWeight: 700,
                }}
              >
                +
              </button>
              <div>
                <div style={{ fontWeight: 600, color: "#0f172a", fontSize: 13 }}>
                  {c.name}
                  <span
                    title={`This column is finer-grain than your declared grain (${declaredGrain || "the declared grain"}). Including it would either explode rows or pick one row arbitrarily. Drop it, or add a roll-up as a derived column.`}
                    style={{ marginLeft: 8, fontSize: 10, color: "#dc2626", fontWeight: 700, cursor: "help" }}
                  >
                    GRAIN MISMATCH
                  </span>
                  {!showRationales && columnDetails[c.name]?.why && (
                    <span
                      title={columnDetails[c.name].why}
                      style={{ marginLeft: 8, fontSize: 11, color: "#94a3b8", cursor: "help", fontWeight: 400 }}
                    >
                      (?)
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 11, color: "#475569" }}>{c.description || "—"}</div>
                {showRationales && columnDetails[c.name]?.why && (
                  <div style={{ fontSize: 11, color: "#64748b", marginTop: 3, fontStyle: "italic", lineHeight: 1.4 }}>
                    Why: {columnDetails[c.name].why}
                  </div>
                )}
              </div>
              <code style={{ fontSize: 11, color: "#64748b" }}>{c.physical_type}</code>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


function DomainPreview({ detail }: { detail: CatalogDetail }) {
  return (
    <div
      style={{
        position: "absolute",
        top: "100%",
        left: 0,
        right: 0,
        marginTop: 4,
        padding: 10,
        borderRadius: 8,
        backgroundColor: "#0f172a",
        color: "#f1f5f9",
        fontSize: 11,
        lineHeight: 1.4,
        zIndex: 20,
        boxShadow: "0 6px 18px rgba(15, 23, 42, 0.25)",
        maxHeight: 260,
        overflowY: "auto",
      }}
    >
      <div style={{ fontWeight: 700, fontSize: 12, marginBottom: 6 }}>
        Starter columns ({detail.columns.length})
      </div>
      {detail.columns.map((c) => (
        <div key={c.name} style={{ display: "flex", justifyContent: "space-between", gap: 8, padding: "2px 0" }}>
          <span>
            {c.name}
            {c.primary_key ? " 🔑" : ""}
          </span>
          <span style={{ color: "#94a3b8" }}>{c.physical_type || c.logical_type || ""}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * Step 6 sub-section wrapper. Used to give the OSI score and Question
 * Analysis panels matching section headers + visual structure so the two
 * optional checks read as peers rather than two ad-hoc embeds.
 */
function ReviewSection({
  title,
  blurb,
  children,
}: {
  title: string;
  blurb: string;
  children: React.ReactNode;
}) {
  return (
    <section
      style={{
        padding: 16,
        borderRadius: 10,
        backgroundColor: "#fff",
        border: "1px solid #e2e8f0",
      }}
    >
      <div style={{ marginBottom: 12 }}>
        <div style={{ fontSize: 14, fontWeight: 700, color: "#0f172a" }}>{title}</div>
        <div style={{ fontSize: 12, color: "#64748b", marginTop: 4, lineHeight: 1.5 }}>
          {blurb}
        </div>
      </div>
      {children}
    </section>
  );
}

/**
 * Bind-step picker: lets the PO add a marketplace product as a slot.
 * Shown inline so the PO never leaves the bind step; falls back to a "Request
 * a Source-aligned Product" CTA when no marketplace products are available in
 * the chosen domain.
 */
/**
 * Renders an evidence chip above the Schema step's column picker explaining
 * whether the schema advisor's recommendations were informed by candidate
 * upstream products (selected at step 3) or only the domain
 * catalog. Read-only — pure UX confirmation that the source picks flow into
 * column ranking via `refreshShapeRecommendations` → `source_contract_ids`.
 */
function SourceFeasibilityEvidence({
  selectedSources,
  domain,
  onJumpToCandidates,
}: {
  selectedSources: SourceInputCandidate[];
  domain: string;
  onJumpToCandidates: () => void;
}) {
  if (selectedSources.length > 0) {
    return (
      <div
        style={{
          marginBottom: 12,
          padding: "10px 12px",
          borderRadius: 8,
          backgroundColor: "#ecfdf5",
          border: "1px solid #86efac",
          color: "#065f46",
          fontSize: 12,
          lineHeight: 1.5,
        }}
      >
        <strong>Ranking informed by {selectedSources.length} candidate upstream product{selectedSources.length === 1 ? "" : "s"}:</strong>{" "}
        {selectedSources.map((s) => s.name).filter(Boolean).join(", ")}.
        Columns that map cleanly from these products rank higher.
      </div>
    );
  }
  return (
    <div
      style={{
        marginBottom: 12,
        padding: "10px 12px",
        borderRadius: 8,
        backgroundColor: "#fefce8",
        border: "1px solid #fde68a",
        color: "#854d0e",
        fontSize: 12,
        lineHeight: 1.5,
      }}
    >
      Ranking based on the <strong>{titleCaseDomain(domain)}</strong> catalog only —{" "}
      <button
        type="button"
        onClick={onJumpToCandidates}
        style={{
          background: "transparent",
          border: "none",
          padding: 0,
          color: "#854d0e",
          textDecoration: "underline",
          cursor: "pointer",
          fontSize: 12,
          fontWeight: 600,
        }}
      >
        confirm candidate sources at step 3
      </button>{" "}
      for feasibility-aware ranking.
    </div>
  );
}

function AddSourceAffordance({
  available,
  boundUris,
  isOpen,
  onToggle,
  onPick,
  onRequestNew,
  loading,
  domain,
  hideRequestNew = false,
}: {
  available: SourceInputCandidate[];
  boundUris: Set<string>;
  isOpen: boolean;
  onToggle: () => void;
  onPick: (src: SourceInputCandidate) => void;
  onRequestNew: () => void;
  loading: boolean;
  domain: string;
  /** When true, the "+ Request a new source-aligned data product" button is
   *  hidden. Step 3 (Suggest candidate sources) passes true — the request-new
   *  affordance belongs only at step 8 (Confirm candidate sources). */
  hideRequestNew?: boolean;
}) {
  const [query, setQuery] = useState<string>("");
  const unbound = available.filter((src) => !boundUris.has(src.dprod_uri));

  if (loading) {
    return (
      <div style={{ fontSize: 12, color: "#64748b" }}>
        Loading candidate products in <strong>{titleCaseDomain(domain)}</strong>…
      </div>
    );
  }

  if (available.length === 0) {
    return (
      <div
        style={{
          padding: 18,
          borderRadius: 10,
          border: "1px dashed #cbd5e1",
          backgroundColor: "#fff",
          textAlign: "center",
        }}
      >
        <div style={{ fontWeight: 600, color: "#0f172a", marginBottom: 8 }}>
          No products to build on in this domain yet.
        </div>
        <div style={{ fontSize: 13, color: "#475569", marginBottom: 14 }}>
          A derived product builds on already-published products (source, aggregate, or consumer). Request a source-aligned product to start from, then come back to this wizard once it's deployed.
        </div>
        <button
          type="button"
          onClick={onRequestNew}
          style={{
            padding: "8px 16px",
            borderRadius: 6,
            backgroundColor: productTheme.accent,
            color: "#fff",
            border: "none",
            fontSize: 13,
            fontWeight: 600,
            cursor: "pointer",
          }}
        >
          Request a new source-aligned data product →
        </button>
      </div>
    );
  }

  // Only DEPLOYED products are bindable; pre-deploy ones are shown disabled with
  // a "Deploy first" note. Both buckets honour the name/description search.
  const q = query.trim().toLowerCase();
  const matchesQuery = (src: SourceInputCandidate) =>
    !q ||
    src.name.toLowerCase().includes(q) ||
    (src.description || "").toLowerCase().includes(q);
  const isDeployed = (src: SourceInputCandidate) =>
    DEPLOYED_STATES.has((src.lifecycle_state || "").toLowerCase());
  const pickable = unbound.filter(isDeployed).filter(matchesQuery);
  const notDeployed = unbound.filter((s) => !isDeployed(s)).filter(matchesQuery);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {/* Two peer affordances: pick an existing upstream product from the marketplace,
          or request a brand-new one. Both are always visible so the PO
          isn't forced to scan the full marketplace before discovering the
          "request a new product" path. */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button
          type="button"
          onClick={onToggle}
          style={{
            flex: "1 1 240px",
            padding: "10px 14px",
            borderRadius: 8,
            backgroundColor: "#fff",
            color: productTheme.accent,
            border: `1px dashed ${productTheme.accent}`,
            fontSize: 13,
            fontWeight: 600,
            cursor: "pointer",
            textAlign: "left",
          }}
        >
          {isOpen
            ? "− Hide marketplace picker"
            : "+ Pick an existing upstream product"}
        </button>
        {!hideRequestNew && (
        <button
          type="button"
          onClick={onRequestNew}
          style={{
            flex: "1 1 240px",
            padding: "10px 14px",
            borderRadius: 8,
            backgroundColor: "#fff",
            color: "#475569",
            border: "1px dashed #94a3b8",
            fontSize: 13,
            fontWeight: 600,
            cursor: "pointer",
            textAlign: "left",
          }}
          title="Open the source-aligned data product wizard so the engineer can build a new one to feed this consumer."
        >
          + Request a new source-aligned data product
        </button>
        )}
      </div>
      {isOpen && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 6,
            padding: 10,
            borderRadius: 8,
            backgroundColor: "#f8fafc",
            border: "1px solid #e2e8f0",
          }}
        >
          <div style={{ fontSize: 11, color: "#475569", marginBottom: 4 }}>
            Products in <strong>{titleCaseDomain(domain)}</strong>
            {" — "}
            {pickable.length} ready to pick
            {notDeployed.length > 0 ? `, ${notDeployed.length} not deployed yet` : ""}
          </div>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search by name or description…"
            style={{
              width: "100%",
              padding: "8px 12px",
              borderRadius: 6,
              border: "1px solid #cbd5e1",
              fontSize: 13,
              marginBottom: 4,
              boxSizing: "border-box",
            }}
          />
          {pickable.length === 0 && notDeployed.length === 0 ? (
            <div style={{ fontSize: 12, color: "#94a3b8", fontStyle: "italic", padding: 6 }}>
              {q
                ? "No products match your search."
                : "No more existing products to pick from. Use the “Request a new source-aligned data product” button above if you need something new."}
            </div>
          ) : (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 6,
                maxHeight: 360,
                overflowY: "auto",
              }}
            >
              {pickable.map((src) => (
                <button
                  key={src.dprod_uri}
                  type="button"
                  onClick={() => onPick(src)}
                  style={{
                    textAlign: "left",
                    padding: 10,
                    borderRadius: 6,
                    backgroundColor: "#fff",
                    border: "1px solid #e2e8f0",
                    cursor: "pointer",
                    fontSize: 12,
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <span style={{ fontWeight: 600, color: "#0f172a" }}>{src.name}</span>
                    <ProductKindChip kind={src.product_kind} compact />
                  </div>
                  {src.description && (
                    <div style={{ fontSize: 11, color: "#475569", marginTop: 2 }}>
                      {src.description}
                    </div>
                  )}
                  <div style={{ fontSize: 10, color: "#94a3b8", marginTop: 4 }}>
                    {(src.column_count ?? 0)} columns · {src.contract_id || src.dprod_uri}
                  </div>
                </button>
              ))}

              {notDeployed.length > 0 && (
                <>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      margin: "6px 2px 2px",
                      fontSize: 10,
                      fontWeight: 600,
                      textTransform: "uppercase",
                      letterSpacing: 0.4,
                      color: "#94a3b8",
                    }}
                  >
                    <span style={{ flex: 1, height: 1, backgroundColor: "#e2e8f0" }} />
                    Not deployed yet
                    <span style={{ flex: 1, height: 1, backgroundColor: "#e2e8f0" }} />
                  </div>
                  {notDeployed.map((src) => (
                    <div
                      key={src.dprod_uri}
                      title="This product isn't deployed yet, so it can't be consumed."
                      style={{
                        textAlign: "left",
                        padding: 10,
                        borderRadius: 6,
                        backgroundColor: "#f1f5f9",
                        border: "1px dashed #cbd5e1",
                        cursor: "not-allowed",
                        fontSize: 12,
                        opacity: 0.75,
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <span style={{ fontWeight: 600, color: "#475569" }}>{src.name}</span>
                        <ProductKindChip kind={src.product_kind} compact />
                      </div>
                      {src.description && (
                        <div style={{ fontSize: 11, color: "#64748b", marginTop: 2 }}>
                          {src.description}
                        </div>
                      )}
                      <div style={{ fontSize: 10, color: "#b45309", marginTop: 4, fontWeight: 600 }}>
                        Deploy this product first to consume it.
                      </div>
                    </div>
                  ))}
                </>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Stepper({ step }: { step: Step }) {
  const steps: Step[] = [1, 2, 3, 4, 5, 6, 7, 8, 9];
  return (
    <div style={{ display: "flex", gap: 8, margin: "16px 0 24px" }}>
      {steps.map((s) => (
        <div
          key={s}
          style={{
            flex: 1,
            padding: 10,
            borderRadius: 8,
            backgroundColor: s === step ? productTheme.accentSoft : s < step ? "#f0fdf4" : "#f8fafc",
            border: `1px solid ${s === step ? productTheme.accent : "#e2e8f0"}`,
            color: s === step ? productTheme.accent : s < step ? "#059669" : "#94a3b8",
            fontSize: 12,
            fontWeight: 600,
          }}
        >
          {s < step ? "✓ " : ""}
          {s}. {STEP_LABELS[s]}
        </div>
      ))}
    </div>
  );
}

interface StepCardProps {
  title: string;
  description: string;
  children?: React.ReactNode;
  onBack?: () => void;
  onAdvance?: () => void;
  canAdvance?: boolean;
  advanceLabel?: string;
  disableAdvance?: boolean;
  onGuide?: () => void;
  onSaveDraft?: () => void;
  savingDraft?: boolean;
  canSaveDraft?: boolean;
}

function StepCard({
  title,
  description,
  children,
  onBack,
  onAdvance,
  canAdvance = false,
  advanceLabel = "Next →",
  disableAdvance = false,
  onGuide,
  onSaveDraft,
  savingDraft = false,
  canSaveDraft = true,
}: StepCardProps) {
  return (
    <div style={{ padding: 24, borderRadius: 12, backgroundColor: "#fff", border: "1px solid #e2e8f0" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <h2 style={{ fontSize: 18, fontWeight: 700, color: "#0f172a", margin: 0 }}>{title}</h2>
        {onGuide && <GuideMeButton onClick={onGuide} />}
      </div>
      <div style={{ fontSize: 13, color: "#64748b", margin: "6px 0 20px" }}>{description}</div>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>{children}</div>
      <div style={{ display: "flex", gap: 12, justifyContent: "flex-end", marginTop: 24, alignItems: "center" }}>
        {onSaveDraft && (
          <button
            type="button"
            onClick={onSaveDraft}
            disabled={savingDraft || !canSaveDraft}
            style={{
              ...buttonStyle("#fff"),
              color: productTheme.accent,
              border: `1px solid ${productTheme.accent}`,
              marginRight: "auto",
              opacity: savingDraft || !canSaveDraft ? 0.5 : 1,
              cursor: savingDraft || !canSaveDraft ? "not-allowed" : "pointer",
            }}
            title={!canSaveDraft ? "Pick a domain first" : "Save progress and return later"}
          >
            {savingDraft ? "Saving…" : "Save Draft"}
          </button>
        )}
        {onBack && (
          <button type="button" onClick={onBack} style={buttonStyle("#64748b")}>
            ← Back
          </button>
        )}
        {onAdvance && (
          <button
            type="button"
            onClick={onAdvance}
            disabled={!canAdvance || disableAdvance}
            style={{
              ...buttonStyle(productTheme.accent),
              opacity: !canAdvance || disableAdvance ? 0.5 : 1,
              cursor: !canAdvance || disableAdvance ? "not-allowed" : "pointer",
            }}
          >
            {advanceLabel}
          </button>
        )}
      </div>
    </div>
  );
}

function Field({ label, children, onGuide }: { label: string; children: React.ReactNode; onGuide?: () => void }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>{label}</span>
        {onGuide && <GuideMeButton onClick={onGuide} />}
      </span>
      {children}
    </label>
  );
}

/** User-facing labels for Guide-me field keys. The internal `field` slug
 *  routes the instruction text and the assistant's structured `applies_to`
 *  response, but the user sees the friendly label in the chat transcript
 *  and the session-picker title (which is derived from the first message).
 *  Keep keys in sync with the `case`s in `instructionFor`. */
const PREFILL_LABELS: Record<string, string> = {
  idea: "Idea",
  domain: "Domain",
  autofill_step3: "Product Details",
  name: "Product Name",
  dataset_name: "Dataset Name",
  description: "Description",
  purpose: "Purpose",
  schema: "Schema",
  shape: "Shape",
  rules: "Quality Rules",
  osi: "OSI Readiness",
  general: "Wizard Guidance",
};

function buildGuidePrefill(field: string, intent: string): string {
  // The wizard state travels out-of-band (WS context field) so it doesn't
  // clutter the chat transcript. The user-visible prefill is just the
  // intent + the field-specific instruction.
  const label = PREFILL_LABELS[field] ?? field;
  return (
    `Help me with the **${label}** field. Intent: ${intent}.\n\n` +
    instructionFor(field)
  );
}

function instructionFor(field: string): string {
  switch (field) {
    case "idea":
      return (
        "Please:\n" +
        "1. Reformulate my idea into 2–3 crisp elevator-pitch framings (who / what / why), and\n" +
        "2. List 3 clarifying questions I should answer to tighten the spec."
      );
    case "domain":
      return (
        "Pick the best-fit domain for my idea from the available catalogs (hr, customer, finance, products_sales, retail banking). " +
        "Compare my idea against each catalog's description and starter columns. " +
        "Emit exactly one suggestion block with applies_to='domain' and value=<domain slug>. " +
        "If no catalog fits well, say so and do not emit a suggestion."
      );
    case "autofill_step3":
      return (
        "Using the idea and domain in the wizard state, recommend values for all four step-3 fields " +
        "(name, dataset_name, description, purpose). Emit FOUR separate suggestion blocks — one per " +
        "field — each with a one-sentence rationale. The user will Apply each independently. " +
        "Invoke the data-product-name-advisor sub-skill for the name + dataset_name picks."
      );
    case "name":
    case "dataset_name":
      return (
        "Please invoke the `data-product-name-advisor` sub-skill with the wizard state above and " +
        "render its suggestions verbatim. Then recommend a top pick in one sentence."
      );
    case "description":
      return (
        "Please produce two description candidates: one consumer-facing (what the product is, " +
        "who uses it), and one governance-facing (boundaries, provenance, trust level). " +
        "Label each clearly and keep each to ≤ 3 sentences."
      );
    case "purpose":
      return (
        "Please produce three purpose statements using different framings (business outcome, " +
        "decision support, compliance/risk) and recommend which one fits best given my idea."
      );
    case "schema":
      return (
        "Please review the current selected_columns and custom_columns against the domain catalog. " +
        "Flag any obvious missing columns, call out anything that looks redundant, and suggest at " +
        "most three additional columns with name, type, and a one-line rationale."
      );
    case "shape":
      return (
        "Please review my current `shape` block (grain, filter, scd_policy, " +
        "scd_effective_column, scd_expiration_column, scd_add_is_current, grouping_keys, " +
        "suppressed_columns) against my idea, domain, and selected columns, and recommend " +
        "concrete values.\n\n" +
        "**You MUST emit a `shape_set` suggestion block with every field you are confident " +
        "about, even if that is only one field.** Partial fills are not just allowed, they " +
        "are the expected output — the wizard merges them onto current state and the PO " +
        "Applies once. Never emit an empty `shape: {}` payload; if you truly have no " +
        "confident recommendation for any field, do NOT emit a `shape_set` block at all " +
        "and instead ask one clarifying question in prose.\n\n" +
        "Field-specific rules:\n" +
        "• `grain_prose` — if the idea unambiguously implies a grain (e.g. \"one row per " +
        "customer\"), populate it.\n" +
        "• `scd_policy` — if the idea implies \"latest snapshot\" / \"current state per X\", " +
        "use `'latest_only'`. Use `'scd2'` only if the product itself needs to preserve " +
        "history. If `'scd2'`, return scd_policy as the object form " +
        "`{type:'scd2', effective_column, expiration_column, add_is_current?}`.\n" +
        "• `filter` — only populate if you are sure; otherwise omit and ask in prose.\n" +
        "• `grouping_keys` / `suppressed_columns` — only include names that already exist " +
        "in the current schema (use selected_columns + custom_columns from wizard state)."
      );
    case "rules":
      return (
        "Please categorise the rules on my columns into schema-oriented (types, nullability, " +
        "uniqueness) vs domain-oriented (business-meaning constraints). Flag the ones that need " +
        "my judgement."
      );
    case "osi":
      return (
        "Please review my current OSI band and checklist, then suggest the highest-impact "
        + "next steps using Apply cards. Prioritise metrics or relationships when those are "
        + "missing; otherwise propose ai_context. Cap suggestions at three."
      );
    default:
      return "Please help me with this field given the wizard state above.";
  }
}

const inputStyle: React.CSSProperties = {
  padding: "8px 12px",
  borderRadius: 6,
  border: "1px solid #cbd5e1",
  fontSize: 14,
  fontFamily: "inherit",
};

// Step 6 "Operations & Support" styles.
const opsSectionLabel: React.CSSProperties = { fontSize: 13, fontWeight: 700, color: "#334155", marginBottom: 8 };
const opsHint: React.CSSProperties = { fontSize: 12, fontWeight: 400, color: "#94a3b8" };
const opsEmpty: React.CSSProperties = { fontSize: 13, color: "#64748b", backgroundColor: "#f8fafc", border: "1px dashed #cbd5e1", borderRadius: 8, padding: "10px 12px" };
const opsReadonlyRow: React.CSSProperties = { display: "flex", alignItems: "baseline", gap: 8, padding: "6px 0", borderBottom: "1px solid #f1f5f9" };
const opsEditRow: React.CSSProperties = { display: "flex", alignItems: "center", gap: 8, marginBottom: 8 };
const opsBadge: React.CSSProperties = { fontSize: 11, fontWeight: 600, color: "#7c3aed", backgroundColor: "#f5f3ff", border: "1px solid #ddd6fe", borderRadius: 10, padding: "1px 8px", flexShrink: 0 };
const opsRemoveBtn: React.CSSProperties = { border: "none", background: "transparent", color: "#94a3b8", cursor: "pointer", fontSize: 14, flexShrink: 0 };
const opsAddBtn: React.CSSProperties = { border: "1px solid #c4b5fd", background: "#fff", color: "#7c3aed", borderRadius: 6, padding: "6px 12px", fontSize: 12, fontWeight: 600, cursor: "pointer" };
const opsRecRow: React.CSSProperties = { display: "flex", alignItems: "center", flexWrap: "wrap", gap: 6, marginTop: 6 };
const opsRecChip: React.CSSProperties = { border: "1px dashed #86efac", background: "#f0fdf4", color: "#15803d", borderRadius: 14, padding: "3px 10px", fontSize: 11, fontWeight: 600, cursor: "pointer" };
const opsRecAllBtn: React.CSSProperties = { border: "none", background: "#16a34a", color: "#fff", borderRadius: 6, padding: "4px 10px", fontSize: 11, fontWeight: 700, cursor: "pointer" };

// A3: starting-point recommendations the PO can one-click add. Retention skews
// longer for governed domains (HR/finance). These are suggestions — the PO edits
// or removes freely; they only appear when not already present.
const RECOMMENDED_ROLES: RoleRow[] = [
  { role: "data_consumer", access: "read", description: "Read-only access for analytics and reporting consumers." },
  { role: "data_product_owner", access: "admin", description: "Owns the product; approves changes and grants access." },
  { role: "data_steward", access: "govern", description: "Reviews data quality, lineage, and policy compliance." },
];
function recommendedSlas(domain: string): SlaRow[] {
  const governed = ["hr", "finance", "retail banking"].includes((domain || "").toLowerCase());
  return [
    { property: "availability", value: "99.5", unit: "percent" },
    { property: "freshness", value: "24", unit: "hours" },
    { property: "retention", value: governed ? "7" : "1", unit: "years" },
  ];
}

function buttonStyle(color: string): React.CSSProperties {
  return {
    padding: "10px 20px",
    borderRadius: 8,
    backgroundColor: color,
    color: "#fff",
    border: "none",
    fontSize: 14,
    fontWeight: 600,
    cursor: "pointer",
  };
}

function smallButtonStyle(bg: string, fg: string): React.CSSProperties {
  return {
    padding: "6px 12px",
    borderRadius: 6,
    backgroundColor: bg,
    color: fg,
    border: "none",
    fontSize: 12,
    fontWeight: 600,
    cursor: "pointer",
  };
}
