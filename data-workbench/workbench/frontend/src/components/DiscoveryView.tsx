import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import api from "../api/client";
import { DISPOSITION } from "../lib/dispositionColors";
import type { ClusterProposal, EstateObject, EstateDisposition, InventoryResponse, ProjectInfo, ReferenceModel } from "../types";
import EstateGraphView from "./estate-graph/EstateGraphView";
import ExtLinkIcon from "./ExtLinkIcon";
import ReferenceDataProductMatch from "./ReferenceDataProductMatch";
import ModernizePanel from "./ModernizePanel";
import MigratePanel from "./MigratePanel";
import RetirePanel from "./RetirePanel";

// Two top-level lenses over the same estate: the Estate Graph and the
// disposition Table. (Object Lineage and the DAG Tree were folded into the
// Estate Graph, which now carries drill-in lineage + recommendation planning.)
type ViewTab = "estate" | "discovery";

// Synthetic id for the estate-lens product what-if preview node (see
// estatePreviewReq) — folded into the estate inventory while a preview is live.
const ESTATE_PREVIEW_PRODUCT_ID = "estate-preview-product";

// Display label for the disposition/"recommendation" cell — derived purely from
// `disposition` (the canonical 4-way state) plus the Data-Product modernize
// variant. There is no separate `recommendation` field on the object anymore.
function recommendationValue(o: EstateObject): string {
  switch (o.disposition) {
    case "migrate": return "Migrate";
    case "modernize": return o.migration_approach === "Data Product" ? "Create Data Product" : "Modernize";
    case "retire": return "Review & Retire";
    case "remain": return "Out of Scope";
    default: return o.disposition;
  }
}

// Colorized recommendation pills, keyed by the DISPLAY label from
// recommendationValue(). Colors come from the shared disposition palette so the
// pill, the panels, and the graph legend can't drift apart.
const RECOMMENDATION_COLORS: Record<string, { bg: string; fg: string }> = {
  "Migrate": { bg: DISPOSITION.migrate.pill, fg: DISPOSITION.migrate.fg },
  "Modernize": { bg: DISPOSITION.modernize.pill, fg: DISPOSITION.modernize.fg },
  "Create Data Product": { bg: DISPOSITION.modernize.pill, fg: DISPOSITION.modernize.fg },
  "Review & Retire": { bg: DISPOSITION.retire.pill, fg: DISPOSITION.retire.fg },
  "Out of Scope": { bg: DISPOSITION.remain.pill, fg: DISPOSITION.remain.fg },
};


// The slicer bar dimensions (mirrors the report's Domain/Application/... filters).
const FILTERS: { key: string; label: string }[] = [
  { key: "domain", label: "Domain" },
  { key: "type", label: "Type" },
  { key: "application", label: "Application" },
  { key: "instance", label: "Instance" },
  { key: "recommendation", label: "Recommendation" },
  { key: "compatibility", label: "Compatibility" },
  { key: "active", label: "Active/Inactive" },
  { key: "phi_pii", label: "PHI/PII" },
];

// The value used to match an object against a given slicer.
function facetValue(obj: EstateObject, key: string): string {
  if (key === "active") return obj.active ? "Active" : "Inactive";
  if (key === "phi_pii") return obj.phi_pii ? "Yes" : "No";
  if (key === "type") return obj.type ? obj.type.charAt(0).toUpperCase() + obj.type.slice(1) : "";
  if (key === "recommendation") return recommendationValue(obj);
  return String((obj as unknown as Record<string, unknown>)[key] ?? "");
}

// The deterministic migration→intake envelope the frontend assembles for one
// node (node summary + schema + 1-hop lineage). The backend to-intake bridge
// serializes it and the intake worker parses it into a migration blueprint.
export type SendToIntakeBody = {
  disposition: string;
  object_ids: string[];
  title: string;
  hints: Record<string, unknown>;
  content: { kind: string; title: string; body: unknown }[];
};

interface Props {
  project: ProjectInfo;
  onProposeProduct?: (ids: string[]) => Promise<ClusterProposal>;
  onDispositionAction?: (ids: string[], disposition: EstateDisposition) => void | Promise<void>;
  // Migrate + modernize → stage the node/cluster into the intake pipeline (host
  // POSTs to-intake + chooses navigate-vs-stay). Assembly happens here (we hold
  // the inventory + fold in the propose result for modernize).
  onSendToIntake?: (rowId: string, body: SendToIntakeBody) => void | Promise<void>;
}

export default function DiscoveryView({ project, onProposeProduct, onSendToIntake }: Props) {
  // The top-level lens is persisted to the URL query params so a refresh or
  // navigating away and back restores the view. Initialized lazily from the URL.
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTab] = useState<ViewTab>(() => (searchParams.get("view") === "discovery" ? "discovery" : "estate"));
  // The graph mounts on first activation and then STAYS mounted (hidden via
  // display:none) so its selection/drill/camera survive tab switches. Deferred
  // so React Flow never initializes inside a zero-size hidden container.
  const [estateMounted, setEstateMounted] = useState(tab === "estate");
  useEffect(() => { if (tab === "estate") setEstateMounted(true); }, [tab]);
  // Table row multi-select (checkboxes) — the set the user pulls into the graph.
  const [tableSelected, setTableSelected] = useState<Set<string>>(new Set());
  const [tableSearch, setTableSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [inv, setInv] = useState<InventoryResponse | null>(null);
  const [filters, setFilters] = useState<Record<string, string>>({});
  // Canonical reference models for the recommendation-planning panels.
  const [referenceModels, setReferenceModels] = useState<ReferenceModel[]>([]);
  // Single source of truth for the disposition what-if (shared by the graph
  // overlay and the bottom panel's buttons so they never drift). null = no preview.
  const [dispoPreview, setDispoPreview] = useState<{ id: string; kind: "migrate" | "retire"; target?: string } | null>(null);
  // Node ids the current migration plan flags in its cautions — the graph badges
  // these (⚠) so the plan's upstream/downstream dependencies are visible. Set by
  // the migrate panel when a plan is generated; cleared when the node changes.
  const [cautionNodeIds, setCautionNodeIds] = useState<string[]>([]);
  // Richer caution payload from the migration plan (classification + the model's
  // free-text cautions + target) — drives the Estate Graph's per-⚠ tooltips.
  const [estateCautionInfo, setEstateCautionInfo] = useState<{ blocking_upstream: string[]; gating_downstream: string[]; cautions: string[]; target: string } | null>(null);
  // Persist the view to the URL (replace, so it doesn't spam history). Legacy
  // params from the retired Object Lineage / DAG Tree surfaces are dropped.
  useEffect(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set("view", tab);
      next.delete("sub");
      next.delete("sel");
      return next;
    }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  // Estate Graph "Recommendation planning" → open the adaptive analysis panel
  // (migrate conversion / retire impact / product comparison) INSIDE the Estate
  // Graph view. The panel tracks the LIVE graph selection: it opens on the
  // button, follows deselections, and closes when the selection is cleared.
  const [estatePlanSel, setEstatePlanSel] = useState<string[]>([]); // selection the panel analyses (snapshot)
  const [estatePlanOpen, setEstatePlanOpen] = useState(false);
  const [estateConfirmClose, setEstateConfirmClose] = useState(false); // clicked off → confirm before closing
  const estatePanelRef = useRef<HTMLDivElement>(null);
  const estateGraphRef = useRef<HTMLDivElement>(null);
  // Clear the plan's caution badges whenever the selection isn't a single node
  // (deselect / multi-select) — a fresh single-node plan repopulates them.
  useEffect(() => { if (estatePlanSel.length !== 1 && !dispoPreview) setCautionNodeIds([]); }, [estatePlanSel, dispoPreview]);
  // Only one shared, actionable recommendation is plannable (see the button gate).
  const estatePlannableIds = (ids: string[]) => {
    if (!ids.length) return false;
    const disps = new Set(ids.map((id) => objById[id]?.disposition ?? "__none__"));
    if (disps.size !== 1) return false;
    const d = [...disps][0];
    return d === "modernize" || d === "migrate" || d === "retire";
  };
  const onEstateSelectionChange = (ids: string[]) => {
    if (estatePlannableIds(ids)) {
      setEstatePlanSel(ids);       // panel tracks the live selection while it stays plannable
      setEstateConfirmClose(false);
    } else if (estatePlanOpen) {
      setEstateConfirmClose(true); // cleared / mixed while open → confirm before closing (keep the panel up)
    }
  };
  const planRecommendation = (ids: string[]) => {
    if (!estatePlannableIds(ids)) return;
    setEstatePlanSel(ids);
    setEstateConfirmClose(false);
    setEstatePlanOpen(true);
    requestAnimationFrame(() => estatePanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };
  // Product what-if preview rendered INSIDE the Estate Graph — the estate-lens
  // analogue of dagPreviewReq. The proposed governed product + its consolidation
  // edges are folded into estateInventory as a transient overlay until the user
  // Proceeds to the wizard or cancels.
  const [estatePreviewReq, setEstatePreviewReq] = useState<{ ids: string[]; proposal: ClusterProposal; nonce: number } | null>(null);
  // Closing the panel must clear EVERY live what-if: the product preview
  // (estatePreviewReq) AND the migrate/retire preview (dispoPreview) — otherwise
  // the dashed outlines / added node linger on the graph after the panel is gone.
  // Closing clears the what-if state IN PLACE — the preview styling / added node
  // just disappears, leaving the user's current pan/zoom untouched (no reframe).
  const closeEstatePlan = () => { setEstatePlanOpen(false); setEstateConfirmClose(false); setEstatePreviewReq(null); setDispoPreview(null); };
  // Disposition table → Estate Graph: drill into the object(s) in the merged
  // graph. Nonce lets re-selecting the same row re-trigger the drill.
  const [estateFocus, setEstateFocus] = useState<{ ids: string[]; nonce: number }>({ ids: [], nonce: 0 });
  const viewInEstate = (idOrIds: string | string[]) => {
    setEstateFocus({ ids: Array.isArray(idOrIds) ? idOrIds : [idOrIds], nonce: Date.now() });
    setTab("estate");
  };
  // Row checkbox toggle (stops the row's spawn-on-click).
  const toggleRowSelected = (id: string) => setTableSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  // Object Lineage → Table: jump to the table with the object's name pre-searched.
  const findInTable = (name: string) => {
    setTableSearch(name);
    setTab("discovery");
  };

  useEffect(() => {
    let alive = true;
    api.get(`/api/projects/${project.id}/discovery/inventory`)
      .then((res) => { if (alive) setInv(res.data); })
      .finally(() => { if (alive) setLoading(false); });
    api.get(`/api/projects/${project.id}/discovery/reference-data-products`)
      .then((res) => { if (alive) setReferenceModels(res.data?.models || []); })
      .catch(() => {});
    return () => { alive = false; };
  }, [project.id]);

  // Transient (view-only) retirements: nodes the user retired from the DAG/table
  // this session. NOT persisted — the estate fixture is untouched, so a new
  // migration project still loads the full scenario. Reset when the project or
  // the loaded inventory changes.
  const [retiredIds, setRetiredIds] = useState<Set<string>>(new Set());
  // Applied-in-view migrations: the new Databricks node(s) + their edges that
  // replace a migrated legacy node. Same transient contract as retiredIds — the
  // fixture is never touched, so a new project still loads the full scenario.
  const [addedNodes, setAddedNodes] = useState<EstateObject[]>([]);
  const [addedEdges, setAddedEdges] = useState<[string, string][]>([]);
  // Superseded-by-migration: the legacy node stays visible after "Apply changes"
  // but is flipped to a `retire` disposition — the user fully retires it in a
  // separate step (it isn't auto-removed).
  const [supersededIds, setSupersededIds] = useState<Set<string>>(new Set());
  useEffect(() => { setRetiredIds(new Set()); setAddedNodes([]); setAddedEdges([]); setSupersededIds(new Set()); setEstatePreviewReq(null); }, [project.id]);

  const objById = useMemo(() => {
    const m: Record<string, EstateObject> = {};
    (inv?.objects || []).forEach((o) => { m[o.id] = o; });
    (inv?.product_nodes || []).forEach((o) => { m[o.id] = o; });
    addedNodes.forEach((o) => { m[o.id] = o; });
    return m;
  }, [inv, addedNodes]);

  const objects = useMemo(
    () => [
      ...(inv?.objects || [])
        .filter((o) => !retiredIds.has(o.id))
        // A node we've migrated FROM keeps rendering but drops its disposition
        // entirely (no chip) — it's no longer a pending "migrate" and isn't
        // auto-flipped to "retire" either. Full retirement is a separate step.
        .map((o) => (supersededIds.has(o.id) ? { ...o, disposition: "remain" as const } : o)),
      ...addedNodes,
    ],
    [inv, retiredIds, supersededIds, addedNodes],
  );

  // Current edge set (base minus retired-touching, plus applied-migration edges).
  // Shared by the DAG and the retire-impact analysis so a superseded node
  // correctly sees its migrated replacement as a surviving downstream feeder.
  const visibleEdges = useMemo(
    () => [
      ...(inv?.edges || []).filter(([a, b]) => !retiredIds.has(a) && !retiredIds.has(b)),
      ...addedEdges,
    ],
    [inv, retiredIds, addedEdges],
  );
  const facets = inv?.facets || {};
  // Type facet is derived client-side (backend doesn't emit one) — distinct
  // logical types (Report / View / Snapshot / Table / Query), capitalized to
  // match facetValue's Type output.
  const typeOptions = useMemo(
    () => Array.from(new Set(objects.map((o) => o.type).filter(Boolean)))
      .map((t) => String(t).charAt(0).toUpperCase() + String(t).slice(1))
      .sort(),
    [objects],
  );
  // Recommendation facet is derived client-side too, so the split
  // Migrate/Modernize values (see recommendationValue) match the cell labels
  // instead of the backend's combined "Modernize/Migrate".
  const recommendationOptions = useMemo(
    () => Array.from(new Set(objects.map((o) => recommendationValue(o)).filter(Boolean))).sort(),
    [objects],
  );

  const filtered = useMemo(() => {
    const active = Object.entries(filters).filter(([, v]) => v);
    const q = tableSearch.trim().toLowerCase();
    return objects.filter((o) => {
      if (active.length && !active.every(([k, v]) => facetValue(o, k) === v)) return false;
      if (q && !`${o.name} ${o.object_name} ${o.schema} ${o.domain} ${o.application}`.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [objects, filters, tableSearch]);

  // Unified estate what-if overlay — mirrors the DAG's preview for EVERY
  // recommendation-planning type. Produces the transient nodes/edges to fold in
  // plus which ids read as NEW (add) vs RETIRING (retire):
  //   • modernize (product) → new governed product; consolidated sources retire
  //   • migrate             → new migrated node; the legacy node retires
  //   • retire              → the target node retires
  const estatePreview = useMemo(() => {
    const addNodes: EstateObject[] = [];
    const addEdges: [string, string][] = [];
    const addIds: string[] = [];
    const retireIds: string[] = [];

    // modernize → new governed product consolidating the analysed objects
    if (estatePreviewReq) {
      const template = objById[estatePreviewReq.ids[0]];
      if (template) {
        const p = estatePreviewReq.proposal;
        const n = estatePreviewReq.ids.length;
        addNodes.push({
          ...template,
          id: ESTATE_PREVIEW_PRODUCT_ID,
          type: "product",
          name: p.name || "New data product",
          object_name: p.name || "New data product",
          domain: p.domain || template.domain,
          disposition: "remain",
          status: "fresh",
          active: true,
          metric: `Proposed · consolidates ${n} object${n > 1 ? "s" : ""}`,
        });
        // Wire the product into the real lineage the way the DAG does: it REPLACES
        // the consolidated objects, so it picks up their upstream feeders and
        // serves their downstream consumers — but there is NO edge from a
        // retiring object TO the product (they roll up into it, they don't feed
        // it). The retiring sources keep their own (now severed / red) edges.
        const selSet = new Set(estatePreviewReq.ids);
        const upstream = new Set<string>();
        const downstream = new Set<string>();
        visibleEdges.forEach(([a, b]) => {
          if (selSet.has(b) && !selSet.has(a)) upstream.add(a);
          if (selSet.has(a) && !selSet.has(b)) downstream.add(b);
        });
        [...upstream].forEach((u) => addEdges.push([u, ESTATE_PREVIEW_PRODUCT_ID]));
        [...downstream].forEach((d) => addEdges.push([ESTATE_PREVIEW_PRODUCT_ID, d]));
        addIds.push(ESTATE_PREVIEW_PRODUCT_ID);
        estatePreviewReq.ids.forEach((id) => retireIds.push(id));
      }
    }

    // migrate → new migrated node reading the same sources + feeding the same
    // downstream; the legacy node reads as RETIRING (being replaced).
    if (dispoPreview?.kind === "migrate") {
      const legacy = objById[dispoPreview.id];
      if (legacy) {
        const newId = `migrate-preview:${dispoPreview.id}`;
        addNodes.push({
          ...legacy,
          id: newId,
          platform: dispoPreview.target || "Databricks",
          legacy_platform: undefined,
          disposition: "remain",
          status: "fresh",
          active: true,
          metric: `Proposed migration → ${dispoPreview.target || "Databricks"}`,
        } as EstateObject);
        visibleEdges.filter(([, b]) => b === dispoPreview.id).forEach(([a]) => addEdges.push([a, newId]));
        visibleEdges.filter(([a]) => a === dispoPreview.id).forEach(([, b]) => addEdges.push([newId, b]));
        addIds.push(newId);
        retireIds.push(dispoPreview.id);
      }
    }

    // retire → the target node reads as RETIRING (no new node)
    if (dispoPreview?.kind === "retire") {
      retireIds.push(dispoPreview.id);
    }

    return { addNodes, addEdges, addIds, retireIds };
  }, [estatePreviewReq, dispoPreview, objById, visibleEdges]);

  // Per-node ⚠ tooltip text: prefer the MODEL's own caution sentence when it
  // names the node, else a clear templated reason (blocking upstream must go
  // first / gating downstream must be repointed). Keyed by node id.
  const estateCautionDetails = useMemo(() => {
    const m: Record<string, string> = {};
    const info = estateCautionInfo;
    if (!info) return m;
    const target = info.target || "the new platform";
    const modelReason = (name: string) =>
      name ? info.cautions.find((c) => c.toLowerCase().includes(name.toLowerCase())) : undefined;
    info.blocking_upstream.forEach((id) => {
      const nm = objById[id]?.name || objById[id]?.object_name || id;
      m[id] = modelReason(nm) || `${nm} must be migrated / available on ${target} before this can run.`;
    });
    info.gating_downstream.forEach((id) => {
      const nm = objById[id]?.name || objById[id]?.object_name || id;
      m[id] = modelReason(nm) || `${nm} reads this — repoint it to the new ${target} output and reconcile before cutover.`;
    });
    return m;
  }, [estateCautionInfo, objById]);

  // The Estate Graph renders the LIVE estate: apply a migration
  // or retire a node in the panel and it shows here too (derived objects/edges,
  // not the raw fixture). So the estate graph fully stands in for the DAG Tree.
  const estateInventory = useMemo(() => {
    if (!inv) return null;
    if (estatePreview.addNodes.length || estatePreview.addEdges.length) {
      return { ...inv, objects: [...objects, ...estatePreview.addNodes], edges: [...visibleEdges, ...estatePreview.addEdges] };
    }
    return { ...inv, objects, edges: visibleEdges };
  }, [inv, objects, visibleEdges, estatePreview]);

  const setFilter = (key: string, value: string) => setFilters((f) => ({ ...f, [key]: value }));
  const anyFilter = Object.values(filters).some((v) => v);

  // Header checkbox: select / clear every row currently passing the filters.
  const filteredIds = useMemo(() => filtered.map((o) => o.id), [filtered]);
  const allFilteredSelected = filteredIds.length > 0 && filteredIds.every((id) => tableSelected.has(id));
  const toggleAllFiltered = () => setTableSelected((prev) => {
    if (filteredIds.every((id) => prev.has(id))) {
      const next = new Set(prev); filteredIds.forEach((id) => next.delete(id)); return next;
    }
    return new Set([...prev, ...filteredIds]);
  });

  // Assemble the deterministic migration→intake envelope for one node: a
  // summary, its schema, and its 1-hop upstream/downstream lineage (from the
  // loaded inventory). The intake worker parses this into a migration blueprint
  // (project_name + datasets); the to-intake bridge just serializes it.
  const buildMigrationIntake = (obj: EstateObject): SendToIntakeBody => {
    const platformOf = (o: EstateObject) => o.platform || (o as { legacy_platform?: string }).legacy_platform || o.database || "";
    const up = visibleEdges.filter(([, b]) => b === obj.id).map(([a]) => objById[a]).filter(Boolean) as EstateObject[];
    const down = visibleEdges.filter(([a]) => a === obj.id).map(([, b]) => objById[b]).filter(Boolean) as EstateObject[];
    const cols = (obj as { sample_columns?: { name: string; type: string }[] }).sample_columns || [];
    const srcPlatform = (obj as { legacy_platform?: string }).legacy_platform || obj.platform || obj.database || "";
    const oneLine = (o: EstateObject) => `${o.name} (${o.type}${platformOf(o) ? `, ${platformOf(o)}` : ""})`;
    const content: SendToIntakeBody["content"] = [
      {
        kind: "text",
        title: "Object to migrate",
        body: [
          `Name: ${obj.name}`,
          `Type: ${obj.object_type || obj.type}`,
          `Domain: ${obj.domain}`,
          `Source platform: ${srcPlatform || "unknown"}`,
          `Instance / database / schema: ${obj.instance} / ${obj.database} / ${obj.schema}`,
          `Target platform: ${obj.target || "not specified"}`,
          `Size: ${obj.size_gb} GB`,
          `Active: ${obj.active ? "yes" : "no"}`,
          obj.metric ? `Usage: ${obj.metric}` : "",
        ].filter(Boolean).join("\n"),
      },
    ];
    if (cols.length) content.push({ kind: "table", title: `Schema — ${obj.object_name || obj.name}`, body: cols.map((c) => ({ column: c.name, type: c.type })) });
    if (up.length) content.push({ kind: "text", title: "Upstream (1 hop)", body: up.map(oneLine).join("\n") });
    if (down.length) content.push({ kind: "text", title: "Downstream (1 hop)", body: down.map(oneLine).join("\n") });
    return {
      disposition: "migrate",
      object_ids: [obj.id],
      title: obj.name || obj.object_name || obj.id,
      hints: { source_platform: srcPlatform, target_platform: obj.target || "", domain: obj.domain || "" },
      content,
    };
  };
  // Route a migrate node to the Migration intake (Engineer workbench).
  const sendMigrateToIntake = (obj: EstateObject) => onSendToIntake?.(obj.id, buildMigrationIntake(obj));

  // Assemble the modernization→intake envelope for a cluster: the proposed
  // product (name/domain/purpose/columns from the propose pass, when present),
  // the source objects, their schema, and the cluster's 1-hop lineage.
  const buildModernizationIntake = (ids: string[], proposal?: ClusterProposal): SendToIntakeBody => {
    const objs = ids.map((id) => objById[id]).filter(Boolean) as EstateObject[];
    const idSet = new Set(ids);
    const platformOf = (o: EstateObject) => o.platform || (o as { legacy_platform?: string }).legacy_platform || o.database || "";
    const oneLine = (o: EstateObject) => `${o.name} (${o.type}${platformOf(o) ? `, ${platformOf(o)}` : ""})`;
    const up = visibleEdges.filter(([a, b]) => idSet.has(b) && !idSet.has(a)).map(([a]) => objById[a]).filter(Boolean) as EstateObject[];
    const down = visibleEdges.filter(([a, b]) => idSet.has(a) && !idSet.has(b)).map(([, b]) => objById[b]).filter(Boolean) as EstateObject[];
    const domain = proposal?.domain || objs[0]?.domain || "";
    const name = proposal?.name || (objs[0] ? `${objs[0].object_name || objs[0].name} product` : "New data product");
    const propCols = (proposal?.columns || []).map((c) => ({ column: c.name, type: c.physical_type || c.logical_type || "" }));
    const objCols = objs.flatMap((o) => ((o as { sample_columns?: { name: string; type: string }[] }).sample_columns || []).map((c) => ({ column: c.name, type: c.type })));
    const cols = propCols.length ? propCols : objCols;
    const content: SendToIntakeBody["content"] = [
      {
        kind: "text",
        title: "Product to create (modernization)",
        body: [
          `Proposed name: ${name}`,
          `Domain: ${domain}`,
          proposal?.purpose ? `Purpose: ${proposal.purpose}` : "",
          proposal?.idea ? `Idea: ${proposal.idea}` : "",
          `Consolidates ${objs.length} source object(s): ${objs.map((o) => o.name).join(", ")}`,
        ].filter(Boolean).join("\n"),
      },
    ];
    if (cols.length) content.push({ kind: "table", title: "Proposed columns", body: cols });
    if (objs.length) content.push({ kind: "text", title: "Source objects", body: objs.map(oneLine).join("\n") });
    if (up.length) content.push({ kind: "text", title: "Upstream (1 hop)", body: up.map(oneLine).join("\n") });
    if (down.length) content.push({ kind: "text", title: "Downstream (1 hop)", body: down.map(oneLine).join("\n") });
    return {
      disposition: "modernize",
      object_ids: ids,
      title: name,
      hints: { domain, product_name: name },
      content,
    };
  };
  // Route a modernize cluster to the intake bridge (product intake); falls back
  // to a plain row action if the host didn't wire onSendToIntake.
  const sendModernizeToIntake = (ids: string[], proposal?: ClusterProposal) => {
    if (!ids.length) return;
    return onSendToIntake?.(ids[0], buildModernizationIntake(ids, proposal));
  };

  // Adaptive recommendation panel for a selection — the migrate-conversion /
  // retire-impact / product-comparison / reference-model analysis, hosted by
  // the Estate Graph lens. `scrollRef` is the container the panel's
  // preview/apply actions scroll back into view.
  const renderRecommendationPanel = (ids: string[], scrollRef: React.RefObject<HTMLDivElement | null>, broaden = false) => {
    const scrollInto = () => requestAnimationFrame(() => scrollRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    const singleId = dispoPreview?.id ?? (ids.length === 1 ? ids[0] : null);
    if (singleId) {
      const o = objById[singleId];
      if (o && o.disposition === "migrate") {
        return (
          <MigratePanel
            projectId={project.id}
            object={o}
            proposed={dispoPreview?.kind === "migrate" && dispoPreview?.id === o.id}
            onCautionNodes={setCautionNodeIds}
            onCautionInfo={setEstateCautionInfo}
            onPreview={(obj, target) => {
              setDispoPreview({ id: obj.id, kind: "migrate", target });
              // Jump UP to the diagram, but leave the graph's view + selection
              // intact — re-framing would cull nodes the user wants to keep. The
              // newly-added target node shows highlighted in the current view.
              requestAnimationFrame(() => estateGraphRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
            }}
            onCancelPreview={() => setDispoPreview(null)}
            onProceed={sendMigrateToIntake}
          />
        );
      }
      if (o && o.disposition === "retire") {
        return (
          <RetirePanel
            projectId={project.id}
            object={o}
            objById={objById}
            edges={visibleEdges}
            proposed={dispoPreview?.kind === "retire" && dispoPreview?.id === o.id}
            onCautionNodes={setCautionNodeIds}
            onCautionInfo={setEstateCautionInfo}
            onPreview={(obj) => { setDispoPreview({ id: obj.id, kind: "retire" }); scrollInto(); }}
            onRetireInView={(obj) => {
              setRetiredIds((prev) => new Set(prev).add(obj.id));
              setDispoPreview(null);
              setEstatePlanSel((sel) => sel.filter((id) => id !== obj.id));
            }}
          />
        );
      }
    }
    const hasModernize = ids.some((id) => {
      const o = objById[id];
      return o && ["table", "view", "snapshot"].includes(o.type) && o.disposition === "modernize";
    });
    const hasReports = !hasModernize && ids.some((id) => objById[id]?.type === "report");
    return hasReports ? (
      <ReferenceDataProductMatch
        selectedIds={ids}
        objById={objById}
        edges={inv?.edges || []}
        referenceModels={referenceModels}
        onProposeProduct={onProposeProduct}
        onCreateProduct={sendModernizeToIntake}
      />
    ) : (
      <ModernizePanel
        selectedIds={ids}
        objById={objById}
        edges={inv?.edges || []}
        referenceModels={referenceModels}
        projectId={project.id}
        includeAllData={broaden}
        onProposeProduct={onProposeProduct}
        onCreateProduct={sendModernizeToIntake}
        previewSurface="Estate Graph"
        previewActive={!!estatePreviewReq}
        onCancelPreview={() => setEstatePreviewReq(null)}
        onPreviewProduct={(pids, proposal) => {
          setEstatePreviewReq({ ids: pids, proposal, nonce: Date.now() });
          // Preview in place — do NOT reframe/drill the graph; just bring
          // the graph area into the page viewport so the change is visible.
          requestAnimationFrame(() => estateGraphRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
        }}
      />
    );
  };

  return (
    <div>
      <div style={{ marginBottom: 16 }}>
        {inv?.scenario && <p style={{ margin: 0, fontSize: 13, color: "#64748b" }}>{inv.scenario}</p>}
      </div>

      {/* Top-level lenses + estate intake actions */}
      <div style={{ display: "flex", gap: 0, alignItems: "center", borderBottom: "2px solid #e2e8f0", marginBottom: 16 }}>
        {(["estate", "discovery"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            style={{
              padding: "10px 16px", border: "none", background: "none", cursor: "pointer",
              fontSize: 13, fontWeight: 600, marginBottom: -2,
              borderBottom: tab === t ? "2px solid #3b82f6" : "2px solid transparent",
              color: tab === t ? "#1e293b" : "#64748b",
            }}
          >
            {t === "estate" ? "Graph" : "Table"}
          </button>
        ))}
        <div style={{ flex: 1 }} />
      </div>

      {/* Both lenses stay MOUNTED once shown — switching tabs only toggles
          visibility, so the graph keeps its selection/drill/camera and the
          table keeps its filters/checkboxes across switches. The graph is
          deferred until first activation so React Flow never initializes
          inside a display:none container. */}
      {loading ? (
        <div style={{ color: "#94a3b8", fontSize: 13 }}>Loading...</div>
      ) : (
        <>
      {estateMounted ? (
      <div style={{ display: tab === "estate" ? undefined : "none" }}>
      {estateInventory ? (
          <>
            <div ref={estateGraphRef} style={{ scrollMarginTop: 12 }}>
              <EstateGraphView inventory={estateInventory} onOpenRow={findInTable} onPlan={planRecommendation} onSelectionChange={onEstateSelectionChange} focusIds={estateFocus.ids} focusNonce={estateFocus.nonce} previewAddIds={estatePreview.addIds.length ? estatePreview.addIds : undefined} previewRetireIds={estatePreview.retireIds.length ? estatePreview.retireIds : undefined} previewCautionIds={dispoPreview?.kind === "migrate" && cautionNodeIds.length ? cautionNodeIds : undefined} previewCautionDetails={dispoPreview?.kind === "migrate" ? estateCautionDetails : undefined} projectId={project.id} />
            </div>
            {estatePlanOpen && estatePlanSel.length > 0 && (
              <div ref={estatePanelRef} style={{ marginTop: 16 }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 8 }}>
                  <span style={{ fontSize: 13, fontWeight: 700, color: "#1e293b" }}>Recommendation planning</span>
                  {estateConfirmClose ? (
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
                      <span style={{ color: "#64748b" }}>Close this analysis?</span>
                      <button onClick={() => setEstateConfirmClose(false)} style={{ fontSize: 12, fontWeight: 600, color: "#334155", background: "#fff", border: "1px solid #cbd5e1", borderRadius: 6, cursor: "pointer", padding: "4px 10px" }}>Keep open</button>
                      <button onClick={closeEstatePlan} style={{ fontSize: 12, fontWeight: 700, color: "#fff", background: "#b91c1c", border: "1px solid #b91c1c", borderRadius: 6, cursor: "pointer", padding: "4px 10px" }}>Close</button>
                    </span>
                  ) : (
                    <button onClick={closeEstatePlan} style={{ fontSize: 12, fontWeight: 600, color: "#64748b", background: "none", border: "1px solid #e2e8f0", borderRadius: 6, cursor: "pointer", padding: "4px 10px" }}>
                      Close
                    </button>
                  )}
                </div>
                {renderRecommendationPanel(estatePlanSel, estatePanelRef, true)}
              </div>
            )}
          </>
      ) : <div style={{ color: "#94a3b8", fontSize: 13 }}>No inventory.</div>}
      </div>
      ) : null}
        <div style={{ display: tab === "discovery" ? undefined : "none" }}>
          <>
              {/* Slicer bar + free-text search */}
              <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end", marginBottom: 16 }}>
                <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.3 }}>
                  Search
                  <input
                    value={tableSearch}
                    onChange={(e) => setTableSearch(e.target.value)}
                    placeholder="Object, schema, domain…"
                    style={{ fontSize: 13, fontWeight: 500, textTransform: "none", letterSpacing: 0, padding: "5px 8px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff", color: "#1e293b", minWidth: 200 }}
                  />
                </label>
                {FILTERS.map(({ key, label }) => {
                  const opts = key === "type" ? typeOptions : key === "recommendation" ? recommendationOptions : (facets[key] || []);
                  return (
                    <label key={key} style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.3 }}>
                      {label}
                      <select
                        value={filters[key] || ""}
                        onChange={(e) => setFilter(key, e.target.value)}
                        style={{ fontSize: 13, fontWeight: 500, textTransform: "none", letterSpacing: 0, padding: "5px 8px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff", color: "#1e293b", minWidth: 130 }}
                      >
                        <option value="">All</option>
                        {opts.map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                    </label>
                  );
                })}
                {(anyFilter || tableSearch) && (
                  <button onClick={() => { setFilters({}); setTableSearch(""); }} style={{ fontSize: 12, fontWeight: 600, color: "#3b82f6", background: "none", border: "none", cursor: "pointer", padding: "6px 4px" }}>
                    Clear filters
                  </button>
                )}
                <span style={{ fontSize: 12, color: "#94a3b8", padding: "6px 0" }}>{filtered.length} of {objects.length} objects</span>
              </div>

              {/* Multi-select action bar — pull the checked rows into the graph together. */}
              {tableSelected.size > 0 && (
                <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12, padding: "8px 12px", background: "#eff6ff", border: "1px solid #bfdbfe", borderRadius: 8 }}>
                  <span style={{ fontSize: 12.5, fontWeight: 700, color: "#1e293b" }}>{tableSelected.size} selected</span>
                  <button
                    onClick={() => viewInEstate([...tableSelected])}
                    title="Drill into the selected objects and their combined lineage in the Estate Graph"
                    style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12.5, fontWeight: 700, color: "#fff", background: "#2563eb", border: "none", borderRadius: 7, padding: "6px 14px", cursor: "pointer" }}
                  >
                    View {tableSelected.size} in Graph <ExtLinkIcon />
                  </button>
                  <button onClick={() => setTableSelected(new Set())} style={{ fontSize: 12, fontWeight: 600, color: "#64748b", background: "none", border: "none", cursor: "pointer" }}>
                    Clear selection
                  </button>
                </div>
              )}

              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                  <thead>
                    <tr style={{ textAlign: "left", borderBottom: "1px solid #e2e8f0" }}>
                      <th style={{ ...thStyle, width: 28 }}>
                        <input type="checkbox" checked={allFilteredSelected} onChange={toggleAllFiltered} title="Select all shown" style={{ cursor: "pointer" }} />
                      </th>
                      {["Domain", "Application_Name", "Instance_Name", "Database_Name", "Schema_Name", "Object_Name", "Object_Type", "Size (GB)", "Recommendation", "Active/Inactive", "Adhoc_Flag", "Compatibility_Flag", "PHI_PII_Flag", ""].map((h, hi) => (
                        <th key={h || `act${hi}`} style={thStyle}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((o, i) => (
                      <tr
                        key={o.id}
                        onClick={() => { if (o.disposition === "migrate") sendMigrateToIntake(o); else if (o.disposition === "modernize") sendModernizeToIntake([o.id]); }}
                        style={{
                          backgroundColor: tableSelected.has(o.id) ? "#eff6ff" : i % 2 === 0 ? "#fff" : "#f8fafc",
                          borderBottom: "1px solid #f1f5f9",
                          cursor: "pointer",
                        }}
                      >
                        <td style={{ ...tdStyle, width: 28 }} onClick={(e) => e.stopPropagation()}>
                          <input type="checkbox" checked={tableSelected.has(o.id)} onChange={() => toggleRowSelected(o.id)} style={{ cursor: "pointer" }} />
                        </td>
                        <td style={tdStyle}>{o.domain}</td>
                        <td style={tdStyle}>{o.application}</td>
                        <td style={{ ...tdStyle, fontFamily: "monospace", color: "#64748b" }}>{o.instance}</td>
                        <td style={tdStyle}>{o.database}</td>
                        <td style={tdStyle}>{o.schema}</td>
                        <td style={{ ...tdStyle, fontWeight: 600 }}>{o.object_name}</td>
                        <td style={{ ...tdStyle, color: "#64748b" }}>{o.object_type}</td>
                        <td style={{ ...tdStyle, textAlign: "right", fontFamily: "monospace" }}>{o.size_gb.toFixed(2)}</td>
                        <td style={tdStyle}>
                          {(() => {
                            const label = recommendationValue(o);
                            const c = RECOMMENDATION_COLORS[label] || { bg: "#f1f5f9", fg: "#475569" };
                            return (
                              <span style={{ display: "inline-block", padding: "2px 10px", borderRadius: 10, fontSize: 12, fontWeight: 600, backgroundColor: c.bg, color: c.fg, whiteSpace: "nowrap" }}>
                                {label}
                              </span>
                            );
                          })()}
                        </td>
                        <td style={tdStyle}>{o.active ? "Active" : "Inactive"}</td>
                        <td style={tdStyle}>{("adhoc" in o && o.adhoc) ? "Yes" : "No"}</td>
                        <td style={tdStyle}>{o.compatibility}</td>
                        <td style={tdStyle}>{o.phi_pii ? "Yes" : "No"}</td>
                        <td style={{ ...tdStyle, whiteSpace: "nowrap" }}>
                          <button
                            onClick={(e) => { e.stopPropagation(); viewInEstate(o.id); }}
                            title="Drill into this object's lineage in the Estate Graph"
                            style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, fontWeight: 600, color: "#3b82f6", background: "none", border: "1px solid #bfdbfe", borderRadius: 6, cursor: "pointer", padding: "3px 8px" }}
                          >
                            View in Graph <ExtLinkIcon size={12} />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {filtered.length === 0 && (
                  <div style={{ color: "#94a3b8", fontSize: 13, padding: "16px 0" }}>No objects match the current filters.</div>
                )}
              </div>
            </>
        </div>
        </>
      )}
    </div>
  );
}

const thStyle: React.CSSProperties = { padding: "8px 12px", fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.3, whiteSpace: "nowrap" };
const tdStyle: React.CSSProperties = { padding: "8px 12px", verticalAlign: "top" };
