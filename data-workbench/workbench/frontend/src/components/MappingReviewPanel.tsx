import { useEffect, useMemo, useState } from "react";
import api from "../api/client";
import { emitToast } from "../lib/toastBus";
import {
  type PendingMapping,
  type TransformKind,
  type SourceColumn,
  type StageInfo,
  MAPPING_REJECTION_CATEGORIES,
  TRANSFORM_ESCALATION_REASONS,
  TRANSFORM_AUTHOR_LABELS,
} from "../types";
import TransformEditor, { type TransformEditorValue } from "./TransformEditor";
import TransformSuggestionCard, { type TransformInterpretResult } from "./TransformSuggestionCard";
import TransformEditorDialog from "./TransformEditorDialog";
import DatasetShapePanel from "./DatasetShapePanel";
import AddMappingDialog from "./AddMappingDialog";
import MappingGraphView from "./MappingGraphView";
import TransformPreflightCallout from "./TransformPreflightCallout";
import RelationshipKindChip from "./RelationshipKindChip";
import SensitivityChip from "./SensitivityChip";

interface Props {
  projectId: number;
  onReviewComplete: () => void;
  /**
   * data_mapping stage handle (parent supplies). When present + repeatable +
   * not currently running, the panel exposes a "Re-run mapping" affordance
   * with two modes — Refresh (reset + re-run, keeps existing rows) and
   * Start over (POST /reviews/mappings/wipe, then reset + re-run). Both
   * route through onRerunStage so we share the reset/run code path with
   * Pipeline.tsx.
   */
  dataMappingStage?: StageInfo | null;
  dataMappingRepeatable?: boolean;
  onRerunStage?: (stageNumber: number, workflowId?: string) => Promise<void>;
  /**
   * "Guide me" — when set, MappingReviewPanel exposes a button per
   * mapping that builds a column-scoped question and asks the parent to
   * open the project chat drawer with it pre-populated. Engineer can then
   * edit + send. No backend changes — purely an open + prefill into the
   * existing project chat.
   */
  onGuideMe?: (prefill: string) => void;
  /** Bumped by the parent on stage-complete so the mapping list refetches
   *  (a just-finished Data Mapping shows its rows without a page reload). */
  refreshKey?: number;
}

const safeParse = <T,>(s: string | null, fallback: T): T => {
  if (!s) return fallback;
  try {
    return JSON.parse(s) as T;
  } catch {
    return fallback;
  }
};

const toEditorValue = (m: PendingMapping): TransformEditorValue => ({
  transform_kind: (m.transform_kind ?? "direct") as TransformKind,
  transform_expression: m.transform_expression ?? "",
  transform_inputs: safeParse<string[]>(m.transform_inputs_json, m.sources.map((s) => s.uri).filter((u): u is string => !!u)),
  transform_params: safeParse<Record<string, unknown>>(m.transform_params_json, {}),
  transform_decorators: safeParse<{ standardization?: string[]; default_if_null?: string }>(
    m.transform_decorators_json,
    {},
  ),
});

/** Fetch the per-mapping rationale report (a markdown blob built from the
 *  latest data_mapping StageExecution log + current :ColumnMapping rows)
 *  and trigger a browser download. Filename embeds the project_code +
 *  UTC timestamp from the backend's Content-Disposition header so the
 *  engineer can keep a history of downloaded snapshots. */
const downloadRationaleReport = async (projectId: number): Promise<void> => {
  try {
    const res = await api.get(
      `/api/projects/${projectId}/reviews/mappings/rationale-report`,
      { responseType: "blob" },
    );
    // Pull the filename out of the Content-Disposition header; fall back to
    // a generic local name when the header isn't surfaced (proxy stripping).
    const disp = (res.headers["content-disposition"] || "") as string;
    const match = disp.match(/filename="?([^";]+)"?/);
    const filename = match?.[1] || `data-mapping-rationale-${projectId}.md`;
    const url = window.URL.createObjectURL(res.data as Blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  } catch (e) {
    console.error("Rationale report download failed", e);
    emitToast({ tone: "error", message: "Could not download the rationale report. See console for details." });
  }
};

export default function MappingReviewPanel({
  projectId,
  onReviewComplete,
  dataMappingStage = null,
  refreshKey,
  dataMappingRepeatable = false,
  onRerunStage,
}: Props) {
  const [items, setItems] = useState<PendingMapping[]>([]);
  const [loading, setLoading] = useState(true);
  const [currentIdx, setCurrentIdx] = useState(0);
  // When true, the GET passes include_approved=true so the panel shows
  // every current mapping (approved + pending + steward_review) — not
  // just pending. Engineers flip this on when they need to edit a
  // mapping AFTER the data_mapping stage has been approved (the
  // post-approval edit path). Replace works on any of them; the
  // backend's _reset_downstream_mapping_stages helper auto-resets
  // serving / deploy / reflection / mark-engineering when an edit lands
  // on a project whose downstream stages have already run.
  const [includeApproved, setIncludeApproved] = useState(false);
  // Last-edit cascade summary — populated by the post-replace_mapping
  // response. Shown as a banner so the engineer knows downstream stages
  // were reset and they need to re-run serving / deploy.
  const [lastCascadeReset, setLastCascadeReset] = useState<
    Array<{ stage_id: string; stage_number: number; workflow_id: string | null }>
  >([]);
  // "replace" replaces the legacy "reject" mode and absorbs in-place transform
  // edits + source-set edits + remap-to-different-source — one unified action
  // that always records a :ProvRejectionReason.
  const [mode, setMode] = useState<"view" | "replace" | "escalate" | "request_po">("view");
  const [poRequestReason, setPoRequestReason] = useState<string>("");
  const [poRequestSent, setPoRequestSent] = useState<Set<string>>(new Set());
  const [category, setCategory] = useState("edited_default");
  const [detail, setDetail] = useState("");
  const [escalationCategory, setEscalationCategory] = useState("needs_reference_data");
  const [escalationDetail, setEscalationDetail] = useState("");
  const [remapColumns, setRemapColumns] = useState<SourceColumn[]>([]);
  const [selectedRemap, setSelectedRemap] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [showOriginalAi, setShowOriginalAi] = useState(false);

  // Per-mapping in-session review outcome. Indexed by mapping_uri so back-
  // navigation can show "you already approved this — change your mind?"
  // without double-counting and without losing context when items reload.
  // Note: this is in-session only — refreshing the page wipes it. The
  // authoritative outcome lives in :ProvActivity on the mapping.
  type ClientOutcome = { kind: "approved" | "replaced" | "escalated" | "skipped"; quality?: number };
  const [outcomes, setOutcomes] = useState<Map<string, ClientOutcome>>(new Map());
  const recordOutcome = (uri: string, outcome: ClientOutcome) => {
    setOutcomes((prev) => {
      const next = new Map(prev);
      next.set(uri, outcome);
      return next;
    });
  };
  // Derived stats — counts each mapping by its latest outcome.
  const stats = useMemo(() => {
    let approved = 0, rejected = 0, escalated = 0, skipped = 0;
    outcomes.forEach((o) => {
      if (o.kind === "approved") approved++;
      else if (o.kind === "replaced") rejected++;
      else if (o.kind === "escalated") escalated++;
      else if (o.kind === "skipped") skipped++;
    });
    return { approved, rejected, escalated, skipped };
  }, [outcomes]);

  // The editor's working copy of the current mapping. Resets on advance.
  // Only mutates in "replace" mode; view mode reads from `current` directly.
  const [editorValue, setEditorValue] = useState<TransformEditorValue | null>(null);
  const [saveStatus, setSaveStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  /** Graph view (default) shows the full mapping universe; Cards view is the
   *  existing one-at-a-time carousel for pending items. Persisted in
   *  localStorage so the engineer's preference sticks across sessions. */
  const [viewMode, setViewMode] = useState<"graph" | "cards" | "shape">(() => {
    if (typeof window !== "undefined") {
      const stored = window.localStorage.getItem("mapping_review.viewMode");
      if (stored === "graph" || stored === "cards" || stored === "shape") return stored;
    }
    return "graph";
  });
  const setViewModePersisted = (m: "graph" | "cards" | "shape") => {
    setViewMode(m);
    // The "non-pending edge selected" flag is a graph-only concept; clear it when
    // leaving Graph so it can't hide the card in Cards (which has no Dismiss button).
    if (m !== "graph") setSelectedNonPendingUri(null);
    if (typeof window !== "undefined") {
      window.localStorage.setItem("mapping_review.viewMode", m);
    }
  };
  /** When a graph edge points at a non-pending mapping (already approved /
   *  rejected / under steward review), there's no editor card for it. We
   *  surface a small read-only summary instead. */
  const [selectedNonPendingUri, setSelectedNonPendingUri] = useState<string | null>(null);

  // Source-set picker — lives inside the Replace form. Available sources
  // come from /reviews/mappings/source-columns, the same endpoint
  // UnmappedColumnsPanel uses. Lazily fetched on first Replace click and
  // cached for the rest of the session.
  const [availableSources, setAvailableSources] = useState<SourceColumn[]>([]);
  const [pickedSourceUris, setPickedSourceUris] = useState<Set<string>>(new Set());
  // All mappings (incl. approved) for the canvas pop-up editor — so double-click /
  // drag-to-wire can edit ANY edge, not just the pending queue (no "Show all" needed).
  const [allMappings, setAllMappings] = useState<PendingMapping[]>([]);
  const [modalMapping, setModalMapping] = useState<PendingMapping | null>(null);
  const [modalIsNew, setModalIsNew] = useState(false);
  const [modalSeed, setModalSeed] = useState<string | null>(null);
  const [graphRefreshKey, setGraphRefreshKey] = useState(0);
  const [autoThreshold, setAutoThreshold] = useState(0.9);
  const [addMappingOpen, setAddMappingOpen] = useState(false);
  const [sourceSaveStatus, setSourceSaveStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");

  // Re-run mapping affordance. When the engineer wants the data_mapping
  // skill to take another pass (with or without first wiping existing
  // mappings), this state drives an inline confirmation panel + the wipe
  // + reset + run sequence.
  const [rerunPending, setRerunPending] = useState(false);
  const [rerunMode, setRerunMode] = useState<"refresh" | "start_over">("refresh");
  const [rerunBusy, setRerunBusy] = useState(false);
  const [rerunNotice, setRerunNotice] = useState<string | null>(null);
  const canRerun = !!dataMappingStage
    && !!onRerunStage
    && dataMappingRepeatable
    && dataMappingStage.status !== "running";

  const handleRerunConfirm = async () => {
    if (!dataMappingStage || !onRerunStage) return;
    setRerunBusy(true);
    setRerunNotice(null);
    try {
      let wipedCount = 0;
      if (rerunMode === "start_over") {
        const res = await api.post(`/api/projects/${projectId}/reviews/mappings/wipe`, {
          reviewer: "workbench-engineer",
        });
        wipedCount = res.data?.wiped_count ?? 0;
      }
      await onRerunStage(dataMappingStage.stage_number, dataMappingStage.workflow_id);
      // Local state cleanup — items will refresh once the skill finishes
      // and the parent's stage-status watcher triggers loadProject /
      // onReviewComplete. Clearing outcomes now avoids "Already actioned"
      // banners on stale URIs.
      setOutcomes(new Map());
      setItems([]);
      setCurrentIdx(0);
      setMode("view");
      setRerunPending(false);
      setRerunNotice(
        rerunMode === "start_over"
          ? `Wiped ${wipedCount} mapping${wipedCount === 1 ? "" : "s"}. Re-running the data_mapping skill — watch the stage output.`
          : "Re-running the data_mapping skill — watch the stage output.",
      );
    } catch (err) {
      console.error(err);
      setRerunNotice("Re-run failed. Check the console and try again.");
    }
    setRerunBusy(false);
  };

  const loadItems = async () => {
    setLoading(true);
    try {
      const res = await api.get(`/api/projects/${projectId}/reviews/mappings`, {
        params: includeApproved ? { include_approved: true } : undefined,
      });
      const list: PendingMapping[] = res.data.items;
      setItems(list);
      setCurrentIdx(0);
      setMode("view");
      setEditorValue(list[0] ? toEditorValue(list[0]) : null);
    } catch {
      setItems([]);
    }
    setLoading(false);
  };

  const loadAllMappings = async () => {
    try {
      const res = await api.get(`/api/projects/${projectId}/reviews/mappings`, {
        params: { include_approved: true },
      });
      setAllMappings(res.data.items || []);
    } catch {
      setAllMappings([]);
    }
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { loadItems(); loadAllMappings(); }, [projectId, includeApproved, refreshKey, graphRefreshKey]);

  const current = items[currentIdx];

  // Reset editor when navigating between mappings.
  useEffect(() => {
    setEditorValue(current ? toEditorValue(current) : null);
    setSaveStatus("idle");
    setSourceSaveStatus("idle");
    setShowOriginalAi(false);
    setPickedSourceUris(
      new Set(
        (current?.sources ?? [])
          .map((s) => s.uri)
          .filter((u): u is string => !!u),
      ),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.mapping_uri]);

  // In-replace-mode editor mutation. No auto-save — Save Replacement commits.
  const onEditorChange = (next: TransformEditorValue) => {
    setEditorValue(next);
  };

  // Apply an AI transform suggestion: pre-fill the editor + sync the source
  // picks so Save Replacement writes the resolved sources. The engineer still
  // eyeballs the live SQL preview and commits via Save Replacement — one write
  // path, no new mutation route.
  const applyTransformSuggestion = (r: TransformInterpretResult) => {
    if (!editorValue || !r.transform_kind) return;
    setEditorValue({
      ...editorValue,
      transform_kind: r.transform_kind as TransformEditorValue["transform_kind"],
      transform_expression: r.transform_expression || "",
      transform_inputs: r.transform_inputs,
      transform_params: r.transform_params || {},
      transform_decorators: r.transform_decorators || {},
    });
    if (r.transform_kind !== "literal" && r.transform_inputs.length > 0) {
      setPickedSourceUris(new Set(r.transform_inputs));
    }
  };

  const openReplaceMode = async () => {
    if (!current) return;
    setMode("replace");
    // Default rejection category — engineer can change it before Save.
    // "edited_default" makes sense when overriding an AI suggestion; for
    // mappings already authored by an engineer, the dropdown surfaces other
    // options.
    setCategory("edited_default");
    setDetail("");
    setSelectedRemap("");
    setSaveStatus("idle");
    setSourceSaveStatus("idle");
    // Seed source picker from current sources.
    setPickedSourceUris(
      new Set(
        (current.sources ?? [])
          .map((s) => s.uri)
          .filter((u): u is string => !!u),
      ),
    );
    // Lazy load source-column catalog used by both the source picker and
    // the legacy remap dropdown.
    if (availableSources.length === 0) {
      try {
        const res = await api.get(`/api/projects/${projectId}/reviews/mappings/source-columns`);
        setAvailableSources(res.data.columns || []);
        setRemapColumns(res.data.columns || []);
      } catch {
        setAvailableSources([]);
        setRemapColumns([]);
      }
    } else {
      setRemapColumns(availableSources);
    }
  };

  const cancelReplaceMode = () => {
    if (!current) return;
    setMode("view");
    // Restore editor to current state so re-entering Replace starts clean.
    setEditorValue(toEditorValue(current));
    setPickedSourceUris(
      new Set(
        (current.sources ?? [])
          .map((s) => s.uri)
          .filter((u): u is string => !!u),
      ),
    );
    setDetail("");
    setSelectedRemap("");
    setSaveStatus("idle");
    setSourceSaveStatus("idle");
  };

  const togglePickedSource = (uri: string) => {
    setPickedSourceUris((prev) => {
      const next = new Set(prev);
      if (next.has(uri)) next.delete(uri);
      else next.add(uri);
      return next;
    });
  };

  const handleSaveReplacement = async () => {
    if (!current || !editorValue) return;
    const newSources = Array.from(pickedSourceUris);
    // Source-required for non-literal kinds. Literal mappings have no
    // :MAPS_SOURCE_COLUMN edges.
    if (editorValue.transform_kind !== "literal" && newSources.length === 0 && !selectedRemap) {
      setSourceSaveStatus("error");
      return;
    }
    setSubmitting(true);
    setSaveStatus("saving");
    try {
      const resp = await api.post(`/api/projects/${projectId}/reviews/mappings`, {
        action: "replace_mapping",
        mapping_uri: current.mapping_uri,
        // When the engineer also picked a different source via the legacy
        // remap dropdown, that takes precedence — backend routes through the
        // REJECT + REMAP path and creates a fresh approved mapping pointing
        // at the new source column. Transform fields are ignored in that case.
        remap_source_uri: selectedRemap || undefined,
        transform_kind: editorValue.transform_kind,
        transform_expression: editorValue.transform_expression,
        transform_inputs: editorValue.transform_kind === "literal" ? [] : newSources,
        transform_params: editorValue.transform_params,
        transform_decorators: editorValue.transform_decorators,
        source_col_uris: editorValue.transform_kind === "literal" ? undefined : newSources,
        category,
        detail,
        reviewer: "workbench-user",
      });
      // Backend returns stages_reset[] when the post-approval cascade
      // fires (downstream stages that were complete are now pending).
      // Surface it as a banner so the engineer knows to re-run serving /
      // deploy. Empty when the edit happened before downstream ran (the
      // common in-stage edit path).
      const cascade = (resp.data?.stages_reset || []) as Array<{
        stage_id: string; stage_number: number; workflow_id: string | null;
      }>;
      setLastCascadeReset(cascade);
      const uri = current.mapping_uri;
      setSaveStatus("saved");
      recordOutcome(uri, { kind: "replaced" });
      setMode("view");
      // Reload to refresh original_ai_suggestion + transform_author, but
      // restore the engineer to the same mapping by URI lookup so back-
      // navigation still works after the action.
      try {
        const res = await api.get(`/api/projects/${projectId}/reviews/mappings`, {
          params: includeApproved ? { include_approved: true } : undefined,
        });
        const list: PendingMapping[] = res.data.items;
        setItems(list);
        const newIdx = list.findIndex((it) => it.mapping_uri === uri);
        // If the mapping moved out of pending_review (e.g. backend created a
        // fresh derived mapping for a remap), fall through to advance.
        if (newIdx >= 0) {
          setCurrentIdx(newIdx);
        } else {
          advance();
        }
      } catch {
        advance();
      }
    } catch (err) {
      console.error(err);
      setSaveStatus("error");
    }
    setSubmitting(false);
  };

  const handleApprove = async (quality: number) => {
    if (!current) return;
    const uri = current.mapping_uri;
    setSubmitting(true);
    try {
      await api.post(`/api/projects/${projectId}/reviews/mappings`, {
        action: "approve",
        mapping_uri: uri,
        quality,
        reviewer: "workbench-user",
      });
      recordOutcome(uri, { kind: "approved", quality });
      advance(uri);
    } catch (err) {
      console.error(err);
    }
    setSubmitting(false);
  };

  // A1: one-click apply the recommended PII protection. Reuses the unified
  // replace path (transform-only edit, same source) so provenance + downstream
  // cascade are identical to a manual Replace.
  const applyRecommendation = async () => {
    if (!current?.recommended_protection) return;
    const rec = current.recommended_protection;
    const uri = current.mapping_uri;
    setSubmitting(true);
    try {
      await api.post(`/api/projects/${projectId}/reviews/mappings`, {
        action: "replace_mapping",
        mapping_uri: uri,
        transform_kind: rec.kind,
        transform_params: rec.transform_params,
        transform_inputs: rec.transform_inputs,
        source_col_uris: rec.transform_inputs,
        category: "edited_default",
        detail: `Applied recommended PII protection (${rec.basis}): ${rec.reason}`,
        reviewer: "workbench-user",
      });
      recordOutcome(uri, { kind: "replaced" });
      try {
        const res = await api.get(`/api/projects/${projectId}/reviews/mappings`, {
          params: includeApproved ? { include_approved: true } : undefined,
        });
        const list: PendingMapping[] = res.data.items;
        setItems(list);
        const newIdx = list.findIndex((it) => it.mapping_uri === uri);
        if (newIdx >= 0) setCurrentIdx(newIdx);
      } catch { /* keep position on reload failure */ }
    } catch (err) {
      console.error(err);
    }
    setSubmitting(false);
  };

  const handleEscalate = async () => {
    if (!current) return;
    const uri = current.mapping_uri;
    const reason = `[${escalationCategory}] ${escalationDetail.trim() || TRANSFORM_ESCALATION_REASONS.find((r) => r.value === escalationCategory)?.label || ""}`;
    setSubmitting(true);
    try {
      await api.post(`/api/projects/${projectId}/reviews/mappings`, {
        action: "escalate_to_steward",
        mapping_uri: uri,
        escalation_reason: reason,
        reviewer: "workbench-user",
      });
      recordOutcome(uri, { kind: "escalated" });
      setMode("view");
      setEscalationDetail("");
      advance(uri);
    } catch (err) {
      console.error(err);
    }
    setSubmitting(false);
  };

  /** Send a `source_candidates_needed` ProductRequest back to the PO when
   *  the mapping under review can't be satisfied with the currently-bound
   *  source candidates. Distinct from Escalate-to-Steward, which is for
   *  reference-data gaps the Steward can resolve via catalog entries. */
  const handleRequestFromPo = async () => {
    if (!current || !poRequestReason.trim()) return;
    setSubmitting(true);
    try {
      await api.post(
        `/api/projects/${projectId}/product-requests/source-candidates-needed`,
        {
          engineer: "workbench-user",
          notes: "Engineer flagged a mapping that can't be satisfied with current source candidates.",
          // Identify the gap by the mapping uri + the column name it points
          // at. The PO will see this column reference in the request card so
          // they know which mapping the engineer couldn't satisfy.
          gap_column_uri: `${current.mapping_uri} (${current.product_name || ""}.${current.product_col_name || ""})`,
          gap_reason: poRequestReason.trim(),
        },
      );
      setPoRequestSent((prev) => new Set(prev).add(current.mapping_uri));
      setMode("view");
      setPoRequestReason("");
    } catch (err) {
      console.error(err);
    }
    setSubmitting(false);
  };

  const handleSkip = () => {
    if (current) {
      recordOutcome(current.mapping_uri, { kind: "skipped" });
      setMode("view");
      advance(current.mapping_uri);
    } else {
      setMode("view");
      advance();
    }
  };

  // Advance to the next item. If we're at the end, AND every item has a
  // client outcome (counting the just-actioned uri because recordOutcome's
  // setState hasn't flushed yet), surface the completion callback. Items
  // stay in the array so the engineer can back-navigate to change their mind.
  const advance = (justActionedUri?: string) => {
    if (currentIdx + 1 < items.length) {
      setCurrentIdx((i) => i + 1);
      setMode("view");
      setDetail("");
      setSelectedRemap("");
    } else {
      const allActioned = items.every(
        (it) => outcomes.has(it.mapping_uri) || it.mapping_uri === justActionedUri,
      );
      if (allActioned) onReviewComplete();
    }
  };

  const goPrev = () => {
    if (currentIdx > 0) {
      setCurrentIdx((i) => i - 1);
      setMode("view");
      setDetail("");
      setSelectedRemap("");
    }
  };

  const goNext = () => {
    if (currentIdx + 1 < items.length) {
      setCurrentIdx((i) => i + 1);
      setMode("view");
      setDetail("");
      setSelectedRemap("");
    }
  };

  const score = current?.similarity_score ?? null;
  const isDerived = useMemo(
    () => (current?.transform_kind && current.transform_kind !== "direct") || (current?.sources?.length ?? 0) > 1,
    [current?.transform_kind, current?.sources],
  );

  if (loading) return <div style={{ color: "#64748b" }}>Loading pending mappings...</div>;

  const handleGraphMappingSelect = (uri: string) => {
    const idx = items.findIndex((it) => it.mapping_uri === uri);
    if (idx >= 0) {
      setSelectedNonPendingUri(null);
      setCurrentIdx(idx);
      setMode("view");
      setEditorValue(toEditorValue(items[idx]));
    } else {
      // Graph showed a non-pending mapping. Surface a read-only summary
      // and leave the editor on the current pending item (if any).
      setSelectedNonPendingUri(uri);
    }
  };

  // Ensure the source-column catalog is loaded (for the pop-up picker + AI grounding).
  const ensureSources = async () => {
    if (availableSources.length > 0) return;
    try {
      const res = await api.get(`/api/projects/${projectId}/reviews/mappings/source-columns`);
      setAvailableSources(res.data.columns || []);
      setRemapColumns(res.data.columns || []);
    } catch {
      /* leave empty; the picker shows its guidance message */
    }
  };

  // Open the pop-up transform editor for a mapping. Uses `allMappings` so ANY edge is
  // editable — including already-approved ones (no "Show all" checkbox needed).
  const openTransformModal = async (mappingUri: string, seedSourceUri: string | null, isNew: boolean) => {
    const m = allMappings.find((x) => x.mapping_uri === mappingUri);
    if (!m) return;
    await ensureSources();
    setModalSeed(seedSourceUri);
    setModalIsNew(isNew);
    setModalMapping(m);
  };

  // Double-click an edge → edit that mapping in the pop-up.
  const handleEditMapping = (mappingUri: string) => {
    openTransformModal(mappingUri, null, false);
  };

  // "+ Add mapping (search)" — wire a mapping by searching columns instead of
  // hunting the graph (for products with many columns). Opens AddMappingDialog.
  const openAddMapping = async () => {
    await ensureSources();
    setAddMappingOpen(true);
  };

  const handleMappingCreated = async (productColUri: string) => {
    setAddMappingOpen(false);
    try {
      const res = await api.get(`/api/projects/${projectId}/reviews/mappings`, {
        params: { include_approved: true },
      });
      const all: PendingMapping[] = res.data.items || [];
      setAllMappings(all);
      setGraphRefreshKey((k) => k + 1);
      onReviewComplete();
      const created = all.find((x) => x.product_col_uri === productColUri);
      if (created) { setModalSeed(null); setModalIsNew(true); setModalMapping(created); }
    } catch {
      /* the axios interceptor surfaces the error toast */
    }
  };

  // Drag a source-column dot onto a product-column dot → open the pop-up for that
  // target mapping with the newly-wired source seeded in.
  const handleWireSource = async (sourceColumnUri: string, productColumnUri: string) => {
    const existing = allMappings.find((x) => x.product_col_uri === productColumnUri);
    if (existing) {
      openTransformModal(existing.mapping_uri, sourceColumnUri, true);
      return;
    }
    // No mapping on this product column yet (unmapped, e.g. after a delete) —
    // create a direct one, then open it for configuration.
    await ensureSources();
    try {
      await api.post(`/api/projects/${projectId}/reviews/unmapped_columns`, {
        product_col_uri: productColumnUri,
        source_col_uris: [sourceColumnUri],
        transform_kind: "direct",
        transform_expression: "",
        transform_inputs: [sourceColumnUri],
        transform_params: {},
        transform_decorators: {},
        rationale: "Wired on the canvas",
        reviewer: "workbench-engineer",
      });
      const res = await api.get(`/api/projects/${projectId}/reviews/mappings`, {
        params: { include_approved: true },
      });
      const all: PendingMapping[] = res.data.items || [];
      setAllMappings(all);
      setGraphRefreshKey((k) => k + 1);
      const created = all.find((x) => x.product_col_uri === productColumnUri);
      if (created) { setModalSeed(null); setModalIsNew(true); setModalMapping(created); }
    } catch {
      /* the axios interceptor surfaces the error toast */
    }
  };

  const ViewToggle = (
    <div style={{ display: "flex", gap: 4, padding: 2, backgroundColor: "#f1f5f9", borderRadius: 6, alignSelf: "flex-start" }}>
      {(["graph", "cards", "shape"] as const).map((m) => (
        <button
          key={m}
          type="button"
          onClick={() => setViewModePersisted(m)}
          style={{
            padding: "5px 14px",
            borderRadius: 4,
            border: "none",
            backgroundColor: viewMode === m ? "#fff" : "transparent",
            color: viewMode === m ? "#0f172a" : "#64748b",
            fontSize: 12,
            fontWeight: 600,
            cursor: "pointer",
            boxShadow: viewMode === m ? "0 1px 2px rgba(15,23,42,0.08)" : "none",
            textTransform: "capitalize",
          }}
        >
          {m === "graph" ? "Graph" : m === "cards" ? "Cards" : "Shape"}
        </button>
      ))}
    </div>
  );

  const hasPending = items.length > 0 && currentIdx >= 0 && currentIdx < items.length;
  const pendingCount = allMappings.filter((m) => m.status === "pending_review").length;

  // Bulk approve from the canvas: null min_score approves every pending mapping;
  // a threshold "auto-approves" only high-confidence ones and leaves the rest to review.
  const handleBulkApprove = async (minScore: number | null) => {
    try {
      await api.post(`/api/projects/${projectId}/reviews/mappings/approve-all`, {
        reviewer: "workbench-user", quality: 2, min_score: minScore,
      });
      setGraphRefreshKey((k) => k + 1);
      onReviewComplete();  // refresh the stage badge — the backend may have flipped it to complete
    } catch {
      /* the axios interceptor surfaces the error toast */
    }
  };
  const didReview = stats.approved + stats.rejected + stats.escalated > 0;
  const currentOutcome = current ? outcomes.get(current.mapping_uri) ?? null : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
        {ViewToggle}
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button
            type="button"
            onClick={goPrev}
            disabled={!hasPending || currentIdx <= 0}
            style={{
              padding: "4px 10px", borderRadius: 4, border: "1px solid #cbd5e1",
              backgroundColor: "#fff", color: !hasPending || currentIdx <= 0 ? "#cbd5e1" : "#334155",
              fontSize: 12, fontWeight: 600,
              cursor: !hasPending || currentIdx <= 0 ? "not-allowed" : "pointer",
            }}
            title="Previous mapping (re-review or change your mind)"
          >
            ← Prev
          </button>
          <h3 style={{ margin: 0, color: "#334155", fontSize: 15 }}>
            {hasPending
              ? `Mapping Review (${currentIdx + 1} / ${items.length})`
              : items.length === 0 && !didReview
              ? "Mapping Review"
              : "Mapping Review (all reviewed)"}
          </h3>
          <button
            type="button"
            onClick={goNext}
            disabled={!hasPending || currentIdx + 1 >= items.length}
            style={{
              padding: "4px 10px", borderRadius: 4, border: "1px solid #cbd5e1",
              backgroundColor: "#fff", color: !hasPending || currentIdx + 1 >= items.length ? "#cbd5e1" : "#334155",
              fontSize: 12, fontWeight: 600,
              cursor: !hasPending || currentIdx + 1 >= items.length ? "not-allowed" : "pointer",
            }}
            title="Next mapping"
          >
            Next →
          </button>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: "#64748b" }}>
            ✓ {stats.approved} | ✗ {stats.rejected} | ⇧ {stats.escalated} | – {stats.skipped}
            {viewMode === "graph" && ` | pending: ${items.length}`}
          </span>
          <label
            style={{
              display: "flex", alignItems: "center", gap: 4,
              fontSize: 11, color: "#475569", cursor: "pointer",
              userSelect: "none",
            }}
            title="When on, show every current mapping (approved + pending + steward_review) so you can Replace any of them — used when you need to edit a mapping after the data_mapping stage has been approved. Downstream stages (serving / deploy / reflection) auto-reset if they had already run."
          >
            <input
              type="checkbox"
              checked={includeApproved}
              onChange={(e) => setIncludeApproved(e.target.checked)}
              style={{ cursor: "pointer" }}
            />
            Show all (incl. approved)
          </label>
          <button
            type="button"
            onClick={() => downloadRationaleReport(projectId)}
            style={{
              padding: "4px 10px", borderRadius: 4, border: "1px solid #cbd5e1",
              backgroundColor: "#fff", color: "#334155",
              fontSize: 12, fontWeight: 600, cursor: "pointer",
            }}
            title="Download a markdown report explaining each mapping's source, transform, author, and the agent's reasoning from the last data_mapping run. Stateless — re-run to refresh."
          >
            ⤓ Rationale (.md)
          </button>
          {dataMappingStage && (
            <button
              type="button"
              onClick={() => {
                setRerunMode("refresh");
                setRerunNotice(null);
                setRerunPending(true);
              }}
              disabled={!canRerun || rerunBusy}
              style={{
                padding: "4px 10px", borderRadius: 4, border: "1px solid #cbd5e1",
                backgroundColor: canRerun && !rerunBusy ? "#fff" : "#f1f5f9",
                color: canRerun && !rerunBusy ? "#334155" : "#94a3b8",
                fontSize: 12, fontWeight: 600,
                cursor: canRerun && !rerunBusy ? "pointer" : "not-allowed",
              }}
              title={
                !dataMappingRepeatable
                  ? "This workflow's data_mapping stage isn't marked repeatable."
                  : dataMappingStage.status === "running"
                  ? "Stage is currently running — wait for it to finish before re-running."
                  : "Re-run the data_mapping skill — pick Refresh (keep existing) or Start over (wipe + re-run)."
              }
            >
              ↻ Re-run mapping
            </button>
          )}
        </div>
      </div>

      {/* Phase 5 transform-portability: surface capability errors (e.g. AGE() on
          Databricks) with remediation at author time, not at deploy. */}
      <TransformPreflightCallout projectId={projectId} refreshKey={refreshKey} />

      {lastCascadeReset.length > 0 && (
        <div style={{
          padding: "10px 12px", borderRadius: 6, fontSize: 12,
          backgroundColor: "#fef3c7", border: "1px solid #fde68a", color: "#92400e",
          marginBottom: 8,
        }}>
          <strong>Downstream stages reset.</strong> Editing this mapping invalidated
          {" "}
          {lastCascadeReset.map((s) => s.stage_id).join(", ")}
          {" "}— re-run them to publish the change.
          <button
            type="button"
            onClick={() => setLastCascadeReset([])}
            style={{
              marginLeft: 12, padding: "1px 8px", fontSize: 11, borderRadius: 4,
              border: "1px solid #fcd34d", backgroundColor: "#fff", color: "#92400e",
              cursor: "pointer",
            }}
          >
            Dismiss
          </button>
        </div>
      )}

      {rerunNotice && (
        <div style={{
          padding: "10px 12px", borderRadius: 6, fontSize: 12,
          backgroundColor: "#ecfeff", border: "1px solid #a5f3fc", color: "#0e7490",
        }}>
          {rerunNotice}
          <button
            type="button"
            onClick={() => setRerunNotice(null)}
            style={{
              marginLeft: 12, padding: "1px 8px", fontSize: 11, borderRadius: 4,
              border: "1px solid #67e8f9", backgroundColor: "#fff", color: "#0e7490",
              cursor: "pointer",
            }}
          >
            Dismiss
          </button>
        </div>
      )}

      {rerunPending && dataMappingStage && (
        <div style={{
          padding: 16, borderRadius: 8,
          backgroundColor: "#fefce8", border: "1px solid #fde68a",
          display: "flex", flexDirection: "column", gap: 10,
        }}>
          <div style={{ fontWeight: 700, fontSize: 14, color: "#713f12" }}>
            Re-run the data_mapping skill?
          </div>
          <label style={{ display: "flex", alignItems: "flex-start", gap: 8, cursor: "pointer", fontSize: 13, color: "#3f3f46" }}>
            <input
              type="radio"
              checked={rerunMode === "refresh"}
              onChange={() => setRerunMode("refresh")}
              disabled={rerunBusy}
              style={{ marginTop: 3 }}
            />
            <div>
              <div style={{ fontWeight: 600 }}>Refresh — keep existing mappings</div>
              <div style={{ fontSize: 12, color: "#52525b" }}>
                The skill may add new mappings for unmapped columns and update existing
                ones that share a URI. Approved decisions are preserved unless the new
                pass touches the same mapping.
              </div>
            </div>
          </label>
          <label style={{ display: "flex", alignItems: "flex-start", gap: 8, cursor: "pointer", fontSize: 13, color: "#3f3f46" }}>
            <input
              type="radio"
              checked={rerunMode === "start_over"}
              onChange={() => setRerunMode("start_over")}
              disabled={rerunBusy}
              style={{ marginTop: 3 }}
            />
            <div>
              <div style={{ fontWeight: 600, color: "#b45309" }}>Start over — wipe all current mappings for this project</div>
              <div style={{ fontSize: 12, color: "#52525b" }}>
                Every <code>:ColumnMapping</code> for this project is marked
                {" "}<code>status='superseded'</code>, <code>isCurrent=false</code>. The skill
                runs against an empty mapping graph and produces a fresh set of suggestions.
                PROV audit history is preserved. Marketplace listings on deployed contract
                versions are pinned and won't move.
              </div>
            </div>
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <button
              type="button"
              onClick={handleRerunConfirm}
              disabled={rerunBusy}
              style={{
                padding: "6px 14px", borderRadius: 6, border: "none",
                backgroundColor: rerunMode === "start_over" ? "#b45309" : "#0e7490",
                color: "#fff", fontWeight: 600, fontSize: 12,
                cursor: rerunBusy ? "not-allowed" : "pointer",
              }}
            >
              {rerunBusy
                ? "Working…"
                : rerunMode === "start_over"
                ? "Wipe and re-run"
                : "Re-run"}
            </button>
            <button
              type="button"
              onClick={() => setRerunPending(false)}
              disabled={rerunBusy}
              style={{
                padding: "6px 14px", borderRadius: 6,
                border: "1px solid #cbd5e1", backgroundColor: "#fff",
                color: "#64748b", fontWeight: 600, fontSize: 12, cursor: "pointer",
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {viewMode === "graph" && (
        <>
          {pendingCount > 0 && (
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap", padding: "8px 12px", backgroundColor: "#fffbeb", border: "1px solid #fde68a", borderRadius: 8, fontSize: 13, color: "#92400e" }}>
              <span>{pendingCount} mapping{pendingCount === 1 ? "" : "s"} pending — double-click an edge to review one, or:</span>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span style={{ fontSize: 12 }}>Auto-approve score ≥</span>
                <input
                  type="number" min={0} max={1} step={0.05} value={autoThreshold}
                  onChange={(e) => setAutoThreshold(Math.max(0, Math.min(1, Number(e.target.value))))}
                  style={{ width: 62, padding: "4px 6px", fontSize: 12, border: "1px solid #fcd34d", borderRadius: 6 }}
                  title="Only mappings scoring at or above this (0–1) are auto-approved"
                />
                <button
                  type="button"
                  onClick={() => handleBulkApprove(autoThreshold)}
                  style={{ padding: "6px 12px", fontSize: 12, fontWeight: 600, borderRadius: 6, border: "none", backgroundColor: "#16a34a", color: "#fff", cursor: "pointer", whiteSpace: "nowrap" }}
                >
                  ✓ Auto-approve
                </button>
                <button
                  type="button"
                  onClick={() => handleBulkApprove(null)}
                  style={{ padding: "6px 12px", fontSize: 12, fontWeight: 600, borderRadius: 6, border: "1px solid #16a34a", backgroundColor: "#fff", color: "#166534", cursor: "pointer", whiteSpace: "nowrap" }}
                >
                  Approve all
                </button>
              </div>
            </div>
          )}
          <MappingGraphView
            key={`mgv-${graphRefreshKey}`}
            projectId={projectId}
            selectedMappingUri={selectedNonPendingUri ?? (hasPending ? items[currentIdx].mapping_uri : null)}
            onMappingSelect={handleGraphMappingSelect}
            onWireSource={handleWireSource}
            onEditMapping={handleEditMapping}
          />
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <div style={{ fontSize: 11, color: "#94a3b8" }}>
              Tip: <strong>double-click an edge</strong> to configure its transform in a pop-up, or
              <strong> drag a source-column dot onto a product-column dot</strong> to wire a new source.
            </div>
            <button
              type="button"
              onClick={openAddMapping}
              style={{ padding: "6px 12px", fontSize: 12, fontWeight: 600, borderRadius: 6, border: "1px solid #c4b5fd", backgroundColor: "#faf5ff", color: "#7c3aed", cursor: "pointer", whiteSpace: "nowrap" }}
            >
              + Add mapping (search)
            </button>
          </div>
          {selectedNonPendingUri && (
            <div
              style={{
                padding: 12,
                borderRadius: 8,
                backgroundColor: "#f8fafc",
                border: "1px solid #e2e8f0",
                fontSize: 13,
                color: "#475569",
              }}
            >
              This mapping is no longer in the pending queue (likely approved, rejected, or under steward review). Click a different edge to inspect, or dismiss to keep editing the current pending item.
              <button
                type="button"
                onClick={() => setSelectedNonPendingUri(null)}
                style={{ marginLeft: 12, padding: "2px 10px", fontSize: 11, borderRadius: 4, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#475569", cursor: "pointer" }}
              >
                Dismiss
              </button>
            </div>
          )}
          {!hasPending && !selectedNonPendingUri && (
            <div
              style={{
                padding: 16,
                borderRadius: 8,
                backgroundColor: didReview ? "#f0fdf4" : "#fff7ed",
                color: didReview ? "#15803d" : "#9a3412",
                fontSize: 13,
                border: `1px solid ${didReview ? "#86efac" : "#fdba74"}`,
              }}
            >
              {didReview
                ? "All pending mappings have been reviewed. The graph above still shows the full topology — click any edge to inspect."
                : "No pending mappings yet. Run the Mapping & Transformation stage to populate this graph."}
            </div>
          )}
        </>
      )}

      {viewMode === "shape" && (
        <DatasetShapePanel projectId={projectId} />
      )}

      {addMappingOpen && (
        <AddMappingDialog
          projectId={projectId}
          availableSources={availableSources}
          onClose={() => setAddMappingOpen(false)}
          onCreated={handleMappingCreated}
        />
      )}

      {modalMapping && (() => {
        const base = toEditorValue(modalMapping);
        const initial =
          modalSeed && base.transform_kind !== "literal" && !base.transform_inputs.includes(modalSeed)
            ? { ...base, transform_inputs: [...base.transform_inputs, modalSeed] }
            : base;
        return (
          <TransformEditorDialog
            projectId={projectId}
            mappingUri={modalMapping.mapping_uri}
            productColName={modalMapping.product_col_name}
            initialValue={initial}
            availableSources={availableSources}
            isNew={modalIsNew}
            status={modalMapping.status}
            onClose={() => setModalMapping(null)}
            onSaved={(stagesReset) => {
              setLastCascadeReset(stagesReset);
              setModalMapping(null);
              setGraphRefreshKey((k) => k + 1);
              onReviewComplete();  // approve/save/delete may have emptied the queue → refresh the stage
            }}
          />
        );
      })()}

      {!hasPending && viewMode === "cards" && (
        <div style={{ padding: 16 }}>
          {didReview ? (
            <div style={{ color: "#22c55e", fontWeight: 600 }}>
              All mappings reviewed.
              <span style={{ color: "#64748b", fontWeight: 400, marginLeft: 12 }}>
                Approved: {stats.approved} | Rejected: {stats.rejected} | Escalated: {stats.escalated} | Skipped: {stats.skipped}
              </span>
            </div>
          ) : (
            <div style={{ color: "#f59e0b", fontSize: 14 }}>
              No mappings with <code>pending_review</code> status found in Neo4j.
              <div style={{ color: "#64748b", fontSize: 13, marginTop: 6 }}>
                Check that the data mapping skill created ColumnMapping nodes with
                {" "}<code>status: 'pending_review'</code> and <code>isCurrent: true</code>.
              </div>
              <button
                onClick={() => loadItems()}
                style={{ marginTop: 8, padding: "4px 12px", borderRadius: 5, border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#334155", fontSize: 12, fontWeight: 600, cursor: "pointer" }}
              >
                Retry
              </button>
            </div>
          )}
        </div>
      )}

      {hasPending && current && (viewMode === "cards" || (viewMode === "graph" && !selectedNonPendingUri)) && (
      <div style={{ backgroundColor: "#fff", borderRadius: 8, border: "1px solid #e2e8f0", padding: 20 }}>
        {/* Source → Target flow, left-to-right (the natural mapping direction).
            Source is view-mode only; in Replace mode the editable source picker
            lives in the form below. Literal-kind mappings have no source side. */}
        <div style={{ display: "flex", alignItems: "stretch", gap: 12, marginBottom: 16, flexWrap: "wrap" }}>
          {current.transform_kind !== "literal" && mode === "view" && (
            <>
              <div style={{ flex: "1 1 240px", minWidth: 220, border: "1px solid #e2e8f0", borderRadius: 8, padding: 12, backgroundColor: "#f8fafc" }}>
                <div style={{ fontSize: 12, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1, marginBottom: 4 }}>
                  Source Column{current.sources.length > 1 ? "s" : ""}
                </div>
                {current.sources.map((s, i) => (
                  <div key={s.uri ?? i} style={{ marginBottom: 4 }}>
                    <span style={{ fontWeight: 600, fontSize: 14 }}>
                      {s.schema}.{s.table}.{s.name}
                    </span>
                    {s.dataType && (
                      <span style={{ fontWeight: 400, color: "#64748b", marginLeft: 8, fontFamily: "monospace", fontSize: 12 }}>
                        ({s.dataType})
                      </span>
                    )}
                    {s.description && (
                      <div style={{ fontSize: 12, color: "#475569", marginTop: 2, fontStyle: "italic", paddingLeft: 16 }}>
                        {s.description}
                      </div>
                    )}
                  </div>
                ))}
              </div>
              <div style={{ alignSelf: "center", fontSize: 22, color: "#94a3b8" }} aria-hidden>→</div>
            </>
          )}
          <div style={{ flex: "1 1 240px", minWidth: 220, border: "1px solid #dbeafe", borderRadius: 8, padding: 12, backgroundColor: "#eff6ff" }}>
            <div style={{ fontSize: 12, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1, marginBottom: 4 }}>
              Data Product Column
              {isDerived && (
                <span style={{ marginLeft: 8, fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 4, backgroundColor: "#fef3c7", color: "#92400e", textTransform: "none", letterSpacing: 0 }}>
                  Derived / Composite
                </span>
              )}
              {current.transform_author && (
                <span style={{ marginLeft: 8, fontSize: 10, fontWeight: 600, padding: "1px 6px", borderRadius: 4, backgroundColor: "#e0f2fe", color: "#075985", textTransform: "none", letterSpacing: 0 }}>
                  {TRANSFORM_AUTHOR_LABELS[current.transform_author] ?? current.transform_author}
                  {current.transform_author === "engineer" && current.original_ai_suggestion
                    && " (replaced AI suggestion)"}
                </span>
              )}
            </div>
            <div style={{ fontWeight: 600, fontSize: 15, color: "#3b82f6" }}>
              {current.product_name}.{current.product_col_name}
            </div>
            {current.product_col_description && (
              <div style={{ fontSize: 13, color: "#475569", marginTop: 4, fontStyle: "italic" }}>
                {current.product_col_description}
              </div>
            )}
          </div>
        </div>

        {mode === "view" && current.recommended_protection && (
          <div style={{ margin: "0 0 12px", padding: "8px 12px", borderRadius: 8,
            border: "1px solid #fde68a", background: "#fffbeb", display: "flex",
            alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: "#92400e", whiteSpace: "nowrap" }}>
              ⚠ Sensitive source — recommend {current.recommended_protection.kind}
            </span>
            <span style={{ fontSize: 11, color: "#78716c", flex: 1, minWidth: 120 }}>
              {current.recommended_protection.reason}; this maps through unprotected. Apply the
              recommendation, or handle it yourself via Replace.
            </span>
            <button
              onClick={applyRecommendation}
              disabled={submitting}
              style={{ padding: "5px 12px", borderRadius: 6, border: "none",
                background: submitting ? "#cbd5e1" : "#b45309", color: "#fff",
                fontSize: 12, fontWeight: 700, cursor: submitting ? "default" : "pointer", whiteSpace: "nowrap" }}
              title={`Apply a ${current.recommended_protection.kind} transform to protect this sensitive column`}
            >
              {submitting ? "Applying…" : `✓ Apply ${current.recommended_protection.kind}`}
            </button>
          </div>
        )}

        {/* Match quality (always visible) */}
        <div style={{
          display: "flex", gap: 24, padding: 12, backgroundColor: "#f1f5f9",
          borderRadius: 6, marginBottom: 16, fontSize: 13,
        }}>
          <div>
            <span style={{ color: "#94a3b8" }}>Score: </span>
            <span style={{
              fontWeight: 700,
              color: score != null ? (score >= 0.8 ? "#22c55e" : score >= 0.5 ? "#f59e0b" : "#ef4444") : "#94a3b8",
            }}>
              {score != null ? score.toFixed(2) : "n/a"}
            </span>
          </div>
          {current.rationale && (
            <div>
              <span style={{ color: "#94a3b8" }}>Rationale: </span>
              <span>{current.rationale}</span>
            </div>
          )}
          <div style={{ marginLeft: "auto", color: saveStatus === "error" ? "#ef4444" : "#94a3b8", fontSize: 11 }}>
            {saveStatus === "saving" && "Saving replacement..."}
            {saveStatus === "saved" && "Replacement saved"}
            {saveStatus === "error" && "Save failed"}
          </div>
        </div>

        {/* View-mode transform summary: read-only single-line of kind +
            expression preview, so the reviewer can see what the AI / prior
            author committed without being able to mutate it. To change
            anything they must click Replace, which opens the full editor. */}
        {mode === "view" && (
          <div style={{
            marginBottom: 16, padding: 10, borderRadius: 6,
            backgroundColor: "#f8fafc", border: "1px solid #e2e8f0",
            fontSize: 12,
          }}>
            <div style={{ color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1, marginBottom: 4 }}>
              Transform
            </div>
            <div>
              <span style={{ fontFamily: "monospace", color: "#0f172a", fontWeight: 600 }}>
                {current.transform_kind ?? "direct"}
              </span>
              {current.transform_expression && (
                <span style={{ fontFamily: "monospace", color: "#475569", marginLeft: 8 }}>
                  — {current.transform_expression}
                </span>
              )}
            </div>
          </div>
        )}

        {/* Original-AI-suggestion disclosure: appears once an engineer has
            replaced an AI default. Shows the AI's prior fragments captured
            from the earliest transformation_authoring ProvActivity with
            priorAuthor='ai_suggestion'. Sources aren't preserved on the
            activity (only transform fragments), so this is fragment-only. */}
        {mode === "view"
          && current.transform_author === "engineer"
          && current.original_ai_suggestion && (
          <div style={{ marginBottom: 16 }}>
            <button
              type="button"
              onClick={() => setShowOriginalAi((v) => !v)}
              style={{
                background: "none", border: "none", padding: 0, cursor: "pointer",
                fontSize: 11, color: "#0369a1", fontWeight: 600,
              }}
            >
              {showOriginalAi ? "▾" : "▸"} Original AI suggestion (replaced)
            </button>
            {showOriginalAi && (
              <div style={{
                marginTop: 6, padding: 10, borderRadius: 6,
                backgroundColor: "#f0f9ff", border: "1px solid #bae6fd",
                fontSize: 12,
              }}>
                <div style={{ marginBottom: 4 }}>
                  <span style={{ color: "#0369a1", fontWeight: 600 }}>Kind: </span>
                  <span style={{ fontFamily: "monospace" }}>
                    {current.original_ai_suggestion.transform_kind || "direct"}
                  </span>
                </div>
                {current.original_ai_suggestion.transform_expression && (
                  <div style={{ marginBottom: 4 }}>
                    <span style={{ color: "#0369a1", fontWeight: 600 }}>Expression: </span>
                    <span style={{ fontFamily: "monospace" }}>
                      {current.original_ai_suggestion.transform_expression}
                    </span>
                  </div>
                )}
                {current.original_ai_suggestion.transform_params_json && current.original_ai_suggestion.transform_params_json !== "{}" && (
                  <div style={{ marginBottom: 4 }}>
                    <span style={{ color: "#0369a1", fontWeight: 600 }}>Params: </span>
                    <span style={{ fontFamily: "monospace", fontSize: 11 }}>
                      {current.original_ai_suggestion.transform_params_json}
                    </span>
                  </div>
                )}
                {current.original_ai_suggestion.transform_decorators_json && current.original_ai_suggestion.transform_decorators_json !== "{}" && (
                  <div style={{ marginBottom: 4 }}>
                    <span style={{ color: "#0369a1", fontWeight: 600 }}>Decorators: </span>
                    <span style={{ fontFamily: "monospace", fontSize: 11 }}>
                      {current.original_ai_suggestion.transform_decorators_json}
                    </span>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* Action buttons (view mode) */}
        {mode === "view" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 16 }}>
            {/* Prior-outcome banner: when the engineer navigated back to a
                mapping they already actioned in this session, surface what
                they did so they can decide whether to re-action. The buttons
                below remain enabled — re-actioning is allowed (the backend
                accepts another approve/replace and audits the change). */}
            {currentOutcome && (
              <div style={{
                fontSize: 12, color: "#0f172a",
                padding: "8px 10px",
                borderRadius: 4,
                backgroundColor:
                  currentOutcome.kind === "approved" ? "#dcfce7" :
                  currentOutcome.kind === "replaced" ? "#fee2e2" :
                  currentOutcome.kind === "escalated" ? "#fef3c7" : "#f1f5f9",
                border: "1px solid " + (
                  currentOutcome.kind === "approved" ? "#86efac" :
                  currentOutcome.kind === "replaced" ? "#fecaca" :
                  currentOutcome.kind === "escalated" ? "#fde68a" : "#cbd5e1"
                ),
              }}>
                <strong>Already actioned this session:</strong>{" "}
                {currentOutcome.kind === "approved" && (
                  <>Approved{currentOutcome.quality ? ` (${["Acceptable","Good","Excellent"][currentOutcome.quality - 1]})` : ""}</>
                )}
                {currentOutcome.kind === "replaced" && <>Replaced (prior suggestion rejected)</>}
                {currentOutcome.kind === "escalated" && <>Escalated to Steward</>}
                {currentOutcome.kind === "skipped" && <>Skipped</>}
                . You can re-action below to change your mind — the change is audited.
              </div>
            )}
            <div style={{
              fontSize: 11, color: "#64748b", fontStyle: "italic",
              padding: "8px 10px", backgroundColor: "#f8fafc",
              borderLeft: "3px solid #cbd5e1", borderRadius: 4,
            }}>
              Approve / Replace / Escalate signal what to do with the prior author's pick.
              Replace covers any change to the mapping — transform, sources, or target
              source column — and always records a rejection reason on the AI's
              (or prior author's) original suggestion. Edits return the mapping to
              {" "}<code>pending_review</code> for re-review.
            </div>
            <div style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5 }}>
              Approve with quality rating
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button onClick={() => handleApprove(1)} disabled={submitting} style={{ ...btnBase, backgroundColor: "#86efac", color: "#14532d" }} title="Meets minimum bar">Acceptable</button>
              <button onClick={() => handleApprove(2)} disabled={submitting} style={{ ...btnBase, backgroundColor: "#22c55e", color: "#fff" }} title="Solid mapping">Good</button>
              <button onClick={() => handleApprove(3)} disabled={submitting} style={{ ...btnBase, backgroundColor: "#15803d", color: "#fff" }} title="Exemplary">Excellent</button>
              <div style={{ borderLeft: "1px solid #e2e8f0", margin: "0 4px" }} />
              <button
                onClick={openReplaceMode}
                disabled={submitting}
                style={{ ...btnBase, backgroundColor: "#ef4444", color: "#fff" }}
                title="Override the prior author's pick — change transform, sources, or remap to a different source column"
              >Replace</button>
              <button
                onClick={() => setMode("escalate")}
                disabled={submitting}
                style={{ ...btnBase, backgroundColor: "#f59e0b", color: "#fff" }}
                title="Send this mapping to the Data Steward for help"
              >Escalate to Steward</button>
              <button
                onClick={() => setMode("request_po")}
                disabled={submitting || (current && poRequestSent.has(current.mapping_uri))}
                style={{ ...btnBase, backgroundColor: "#fef3c7", color: "#854d0e", border: "1px solid #fde68a" }}
                title="Ask the PO to identify (or create) an additional source-aligned data product because none of the currently-bound candidates fit this mapping"
              >
                {current && poRequestSent.has(current.mapping_uri) ? "✓ Sent to PO" : "Request from PO"}
              </button>
              <button
                onClick={handleSkip}
                style={{ ...btnBase, backgroundColor: "#fff", color: "#64748b", border: "1px solid #cbd5e1" }}
              >Skip</button>
              {/* "Guide me" removed — the pop-up editor's "describe this column's
                  derivation" box supersedes it (it applies a structured result,
                  not a prose question dumped into chat). */}
            </div>
          </div>
        )}

        {/* Replace form: unified target-remap + transform-edit + source-edit
            with a required rejection reason. Save Replacement commits the
            whole bundle in one POST via the replace_mapping backend action. */}
        {mode === "replace" && editorValue && (
          <div style={{ display: "flex", flexDirection: "column", gap: 12, borderTop: "1px solid #e2e8f0", paddingTop: 16, marginTop: 16 }}>
            <div style={{
              padding: 10, borderRadius: 6,
              backgroundColor: "#fef2f2", border: "1px solid #fecaca",
              fontSize: 12, color: "#7f1d1d",
            }}>
              You're replacing the prior author's suggestion. Save Replacement
              records a rejection reason on the original and flips the mapping
              to <code>pending_review</code> for re-approval.
            </div>

            {/* Inline AI assist: describe the derivation → one-click apply into
                the editor below. Writes nothing itself; Save Replacement commits. */}
            <TransformSuggestionCard
              sources={availableSources}
              targetColumn={current.product_col_name || ""}
              mappingUri={current.mapping_uri}
              disabled={submitting}
              onApply={applyTransformSuggestion}
            />

            {/* Source picker (hidden for literal kind — literals have no sources). */}
            {editorValue.transform_kind !== "literal" && (
              <div>
                <label style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>
                  Source Column(s)
                </label>
                <div style={{
                  maxHeight: 180, overflowY: "auto", marginTop: 4,
                  border: "1px solid #e2e8f0", borderRadius: 6,
                  backgroundColor: "#f8fafc", padding: 6,
                }}>
                  {availableSources.length === 0 && (
                    <div style={{ fontSize: 12, color: "#94a3b8", padding: 8 }}>
                      No source columns available. For dpe-cf, add CONSUMES'd source products in
                      the wizard. For dpe-sa, run Data Discovery first.
                    </div>
                  )}
                  {availableSources.map((s) => {
                    const checked = pickedSourceUris.has(s.uri);
                    return (
                      <label
                        key={s.uri}
                        style={{
                          display: "flex", alignItems: "flex-start", gap: 8,
                          padding: "4px 8px", borderRadius: 4, cursor: "pointer",
                          backgroundColor: checked ? "#eff6ff" : "transparent",
                        }}
                        title={s.table_description || undefined}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => togglePickedSource(s.uri)}
                          disabled={submitting}
                          style={{ marginTop: 3 }}
                        />
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                            <span style={{ fontSize: 12, fontFamily: "monospace" }}>{s.name}</span>
                            {s.data_type && (
                              <span style={{ fontSize: 10, color: "#94a3b8" }}>{s.data_type}</span>
                            )}
                            <SensitivityChip sensitivity={s.sensitivity} compact />
                            <RelationshipKindChip kind={s.relationship_kind} />
                          </div>
                          {s.description && (
                            <div style={{ fontSize: 11, color: "#64748b", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              col: {s.description}
                            </div>
                          )}
                          {s.table_description && (
                            <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2, fontStyle: "italic", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              table: {s.table_description}
                            </div>
                          )}
                        </div>
                      </label>
                    );
                  })}
                </div>
                {sourceSaveStatus === "error" && (
                  <div style={{ fontSize: 11, color: "#ef4444", marginTop: 4 }}>
                    Pick at least one source column (or use the remap dropdown below).
                  </div>
                )}
              </div>
            )}

            {/* Full transform editor — same component, fully editable. */}
            <TransformEditor
              value={editorValue}
              sources={current.sources}
              author={current.transform_author}
              confidence={current.transform_confidence}
              onChange={onEditorChange}
              saving={submitting}
              availableSources={availableSources}
            />

            {/* Optional remap-to-different-source dropdown. When set, the
                backend routes through REJECT + REMAP (creates a fresh mapping
                pointing at the new source column) rather than the in-place
                replacement path. */}
            {remapColumns.length > 0 && (
              <div>
                <label style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>
                  Remap to different source column (optional)
                </label>
                <select value={selectedRemap} onChange={(e) => setSelectedRemap(e.target.value)} style={selectStyle}>
                  <option value="">-- Keep current sources --</option>
                  {remapColumns.map((col) => (
                    <option key={col.uri} value={col.uri}>
                      {col.name}{col.data_type ? ` (${col.data_type})` : ""}{col.description ? ` — ${col.description.slice(0, 50)}` : ""}
                    </option>
                  ))}
                </select>
                <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 4 }}>
                  Picking a remap target supersedes the transform edits above and creates a fresh approved mapping pointing at the chosen source column.
                </div>
              </div>
            )}

            <div>
              <label style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>Rejection Category</label>
              <select value={category} onChange={(e) => setCategory(e.target.value)} style={selectStyle}>
                {MAPPING_REJECTION_CATEGORIES.map((c) => (
                  <option key={c.value} value={c.value}>{c.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>Detail (optional)</label>
              <input value={detail} onChange={(e) => setDetail(e.target.value)} style={selectStyle} placeholder="Why does the prior author's suggestion need replacing?" />
            </div>

            <div style={{ display: "flex", gap: 8 }}>
              <button
                onClick={handleSaveReplacement}
                disabled={submitting}
                style={{ ...btnBase, padding: "8px 20px", backgroundColor: "#ef4444", color: "#fff" }}
              >
                Save Replacement
              </button>
              <button
                onClick={cancelReplaceMode}
                disabled={submitting}
                style={{ ...btnBase, padding: "8px 20px", backgroundColor: "#fff", color: "#64748b", border: "1px solid #cbd5e1" }}
              >
                Cancel
              </button>
              <span style={{ marginLeft: "auto", alignSelf: "center", fontSize: 11, color: "#94a3b8" }}>
                Records a :ProvRejectionReason; mapping returns to pending_review.
              </span>
            </div>
          </div>
        )}

        {/* Source-candidates-needed (PO request) form */}
        {mode === "request_po" && current && (
          <div style={{ display: "flex", flexDirection: "column", gap: 12, borderTop: "1px solid #e2e8f0", paddingTop: 16, marginTop: 16 }}>
            <div style={{ fontSize: 12, color: "#475569" }}>
              Send a request to the PO asking them to identify a source-aligned
              data product that can supply <strong>{current.product_name}.{current.product_col_name}</strong>.
              The PO acknowledges from My Products and updates the consumer's
              candidate sources; you can re-run mapping once they save.
            </div>
            <div>
              <label style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>What's the gap?</label>
              <textarea
                value={poRequestReason}
                onChange={(e) => setPoRequestReason(e.target.value)}
                rows={3}
                style={{ ...selectStyle, fontFamily: "inherit", resize: "vertical" }}
                placeholder="e.g. None of the bound source products carry a `country_name` column. Need a localization or geography reference product added to the candidate list."
              />
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button onClick={handleRequestFromPo} disabled={submitting || !poRequestReason.trim()} style={{ ...btnBase, padding: "8px 20px", backgroundColor: "#0f172a", color: "#fff" }}>
                Send request to PO
              </button>
              <button
                onClick={() => { setMode("view"); setPoRequestReason(""); }}
                style={{ ...btnBase, padding: "8px 20px", backgroundColor: "#fff", color: "#64748b", border: "1px solid #cbd5e1" }}
              >Cancel</button>
            </div>
          </div>
        )}

        {/* Escalation form */}
        {mode === "escalate" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 12, borderTop: "1px solid #e2e8f0", paddingTop: 16, marginTop: 16 }}>
            <div>
              <label style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>What's missing?</label>
              <select value={escalationCategory} onChange={(e) => setEscalationCategory(e.target.value)} style={selectStyle}>
                {TRANSFORM_ESCALATION_REASONS.map((c) => (
                  <option key={c.value} value={c.value}>{c.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>Note for the Steward</label>
              <textarea
                value={escalationDetail}
                onChange={(e) => setEscalationDetail(e.target.value)}
                rows={3}
                style={{ ...selectStyle, fontFamily: "inherit", resize: "vertical" }}
                placeholder="e.g. Need ISO-2 → country name mapping. Source column country_code has values like 'US', 'CA', 'MX'..."
              />
            </div>
            <div style={{ fontSize: 11, color: "#94a3b8" }}>
              The mapping moves to <code>steward_review</code> and appears in the Steward's
              escalation queue. The Steward can answer inline or add a catalog entry.
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button onClick={handleEscalate} disabled={submitting || !escalationDetail.trim()} style={{ ...btnBase, padding: "8px 20px", backgroundColor: "#f59e0b", color: "#fff" }}>
                Send to Steward
              </button>
              <button
                onClick={() => { setMode("view"); setEscalationDetail(""); }}
                style={{ ...btnBase, padding: "8px 20px", backgroundColor: "#fff", color: "#64748b", border: "1px solid #cbd5e1" }}
              >Cancel</button>
            </div>
          </div>
        )}
      </div>
      )}
    </div>
  );
}

const btnBase: React.CSSProperties = {
  padding: "8px 16px", borderRadius: 6, border: "none",
  fontWeight: 600, cursor: "pointer", fontSize: 13,
};
const selectStyle: React.CSSProperties = {
  display: "block", width: "100%", padding: "6px 10px", borderRadius: 6,
  border: "1px solid #cbd5e1", fontSize: 13, marginTop: 4, boxSizing: "border-box",
};
