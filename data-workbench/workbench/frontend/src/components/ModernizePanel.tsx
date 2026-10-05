import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { ClusterProposal, EstateObject, ProductSimilarity, ReferenceModel } from "../types";
import { columnsOf } from "../types";
import SimilarityRadar from "./SimilarityRadar";
import ExtLinkIcon from "./ExtLinkIcon";
import { DISPOSITION } from "../lib/dispositionColors";

// This panel is the modernize surface — one tone, pulled from the shared palette.
const P = DISPOSITION.modernize;
import { fetchColumnSimilarity } from "../lib/similarity";

/**
 * ModernizePanel — Pipeline DAG V2 panel for MODERNIZE tables/views.
 *
 * Driven by the DAG's current selection, restricted to modernize-disposition
 * table/view/snapshot nodes. It answers: which governed data product can these
 * selected nodes be consolidated into, and how well do they fit?
 *
 *  • Left rail: every reference data product, ranked by aggregate match score
 *    (sideways bars), searchable and selectable.
 *  • Main: a 4-column comparison table. Rows are grouped by each selected table,
 *    listing ALL of that table's columns (not just the ones the product covers):
 *      1. Selected source column + match (vs the selected product)
 *      2. Data product attribute it maps to (— if the column isn't in the product)
 *      3. Report attributes used — downstream reports that read this column
 *         (overlap-native: one column, N report chips)
 *      4. Industry concept (of the mapped product attribute)
 *    A trailing section lists product attributes NOT covered by the selection.
 *  • Propose the winning product from the selected nodes.
 */

const norm = (s: string) => s.trim().toLowerCase();

type Attr = { name: string; type: string; concept?: string };

const CARD = { border: "1px solid #e2e8f0", borderRadius: 10, background: "#fff", padding: "12px 14px" };
const th: React.CSSProperties = { textAlign: "left", padding: "7px 10px", fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.4, borderBottom: "1px solid #e2e8f0", whiteSpace: "nowrap" };
const td: React.CSSProperties = { padding: "8px 10px", fontSize: 12.5, color: "#1e293b", borderBottom: "1px solid #f1f5f9", verticalAlign: "top" };
const dash = <span style={{ color: "#cbd5e1" }}>—</span>;

// Green gradient keyed to the match score: pale green (low) → deep green (high).
function matchColor(m: number): { bg: string; fg: string } {
  const t = Math.max(0, Math.min(1, (m - 65) / 33));
  const light = 90 - t * 52; // 90% pale → 38% deep
  return { bg: `hsl(145 52% ${light}%)`, fg: light < 62 ? "#fff" : "#14532d" };
}
// Red → yellow → green ramp for the SELECTED product's fit percentage: hue sweeps
// 0° (red, poor fit) → 60° (amber) → 130° (green, strong fit). High saturation and
// a lightness that lifts through the amber midpoint keep it vivid — the dull olive
// you get from a flat lightness ramp made it read washed-out next to the match
// greens.
function rygColor(pct: number): string {
  const t = Math.max(0, Math.min(1, pct / 100));
  const hue = Math.round(130 * t);
  const light = 46 + 9 * Math.sin(Math.PI * t); // peak lightness at the amber middle
  return `hsl(${hue} 90% ${light}%)`;
}
// Unselected products keep the neutral scale: light slate-gray at low percentages
// deepening to dark navy at high ones (lightness 66% → 20%).
function navyScale(pct: number): string {
  const t = Math.max(0, Math.min(1, pct / 100));
  return `hsl(215 ${18 + t * 12}% ${66 - t * 46}%)`;
}
const badgeStyle = (m: number, belowThreshold = false): React.CSSProperties => {
  // Below the match threshold → neutral slate (not a "green = good match" pill),
  // so a score that scored against something but didn't clear the bar reads as a
  // non-match at a glance.
  const c = belowThreshold ? { bg: "#e2e8f0", fg: "#475569" } : matchColor(m);
  return { display: "inline-flex", alignItems: "center", justifyContent: "center", minWidth: 30, height: 18, borderRadius: 5, fontSize: 10.5, fontWeight: 800, background: c.bg, color: c.fg };
};
function conceptCell(concept?: string) {
  if (!concept) return dash;
  return <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: "#1e293b" }}><span style={{ width: 9, height: 9, borderRadius: "50%", background: "#334155", flex: "none" }} />[{concept}]</span>;
}
function attrCell(name: string, type: string) {
  return <><span style={{ fontFamily: "ui-monospace, monospace", fontWeight: 700 }}>{name}</span><span style={{ color: "#94a3b8", marginLeft: 6, fontSize: 11 }}>{type}</span></>;
}

interface Props {
  selectedIds: string[];
  objById: Record<string, EstateObject>;
  edges: [string, string][];
  referenceModels: ReferenceModel[];
  onProposeProduct?: (ids: string[]) => Promise<ClusterProposal>;
  onCreateProduct?: (ids: string[], proposal?: ClusterProposal) => void | Promise<void>;
  // Show the what-if preview in the DAG (base case after adding the product)
  // instead of jumping straight to the wizard. Preferred when provided.
  onPreviewProduct?: (ids: string[], proposal: ClusterProposal) => void;
  // True while the host surface is showing THIS panel's what-if preview. Keeps
  // the Propose button in lockstep with the preview banner: it reads
  // "Previewing in <surface>" (disabled) while live, and the panel's proposed
  // strip clears the moment the preview is dismissed (Cancel / Proceed) via the
  // parent flipping this false.
  previewActive?: boolean;
  // Human name of the surface the what-if preview renders on ("DAG" for the DAG
  // Tree, "Estate Graph" for the estate lens). Only affects the button + strip
  // copy so the panel reads correctly wherever it's mounted.
  previewSurface?: string;
  // Dismiss the active what-if preview (the button's "Exit preview" state).
  onCancelPreview?: () => void;
  // When set, the preview spider is computed by the backend schema-DNA scorer
  // (embedding-backed semantic + real instance axes when profiled) instead of
  // the offline heuristic.
  projectId?: number | string;
  // When true, analyse ANY selected table/view/snapshot regardless of its
  // disposition (used by the Estate Graph so recommendation planning shows up
  // for nodes with a different recommendation, or no recommendation at all).
  // Default (false) keeps the DAG's modernize-only behaviour.
  includeAllData?: boolean;
}

export default function ModernizePanel({ selectedIds, objById, edges, referenceModels, onProposeProduct, onCreateProduct, onPreviewProduct, previewActive, previewSurface = "DAG", onCancelPreview, projectId, includeAllData = false }: Props) {
  const [selectedProductId, setSelectedProductId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [proposal, setProposal] = useState<ClusterProposal | null>(null);
  const [busy, setBusy] = useState(false);
  // Attribute-level hover radar: explains one row's match score. Anchored to the
  // hovered row (fixed-position, clamped to the viewport). A short show-delay
  // stops it flickering while the pointer sweeps across rows; once visible it
  // retargets instantly and GLIDES to the next row (left/top transition).
  const [hoverSim, setHoverSim] = useState<{ key: string; sim: ProductSimilarity; muted: boolean; x: number; y: number } | null>(null);
  // Backend schema-DNA scores for the SELECTED model, keyed per source column
  // (norm name). Re-fetched whenever the scorer `variant` or the selection
  // changes — this is what makes the table + spiders respond to the scorer
  // picker. null = not loaded / endpoint unavailable → offline heuristic.
  // Per-source-column backend result for one reference model.
  type ByCol = Map<string, { match: number; sim: ProductSimilarity; target: { name: string; type: string; concept?: string } | null; matched: boolean }>;
  // Full analysis of ONE reference model — computed as a unit (fit + the
  // per-column table together), so a product is never shown half-analysed.
  type ModelResult = { score: number; matchedCount: number; productCoverage: number; byCol: ByCol };
  // Row expanded to show WHICH downstream reports read the column (the table
  // cell itself shows only a count so wide estates stay compact).
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hoverLastMove = useRef(0);
  const tableWrapRef = useRef<HTMLDivElement>(null);
  useEffect(() => () => { if (hoverTimer.current) clearTimeout(hoverTimer.current); }, []);
  // Smooth collapse on deselect: remember the analysis panel's rendered height
  // so the empty state can shrink to it gradually instead of snapping (which
  // made everything below the DAG jump). collapseH: null = natural height.
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const lastPanelH = useRef(0);
  const [collapseH, setCollapseH] = useState<number | null>(null);
  // Single clearance path for the hover radar — cancels a pending show AND hides
  // an open popup. Wired to the row, the table container (rows can miss fast
  // exits), and any scroll (rows move under a stationary pointer).
  const clearHover = () => {
    if (hoverTimer.current) { clearTimeout(hoverTimer.current); hoverTimer.current = null; }
    setHoverSim(null);
  };
  useEffect(() => {
    if (!hoverSim) return;
    const onScroll = () => clearHover();
    // Global catch-all: any pointer move whose target is outside the table
    // wrapper ends the hover. Reliably handles fast exits, leaving through the
    // window edge, or moving onto a sibling panel that never fired the row's
    // own onMouseLeave.
    const onDocMove = (e: MouseEvent) => {
      const wrap = tableWrapRef.current;
      if (wrap && e.target instanceof Node && wrap.contains(e.target)) return;
      clearHover();
    };
    const onLeaveWindow = () => clearHover();
    window.addEventListener("scroll", onScroll, { capture: true, passive: true });
    document.addEventListener("mousemove", onDocMove, { capture: true });
    document.addEventListener("mouseleave", onLeaveWindow);
    window.addEventListener("blur", onLeaveWindow);
    return () => {
      window.removeEventListener("scroll", onScroll, { capture: true });
      document.removeEventListener("mousemove", onDocMove, { capture: true });
      document.removeEventListener("mouseleave", onLeaveWindow);
      window.removeEventListener("blur", onLeaveWindow);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [!hoverSim]);
  // Popup position from the CURSOR (offset right/below it, flipped above when
  // near the viewport bottom) so the radar trails the mouse instead of sitting
  // at a fixed row anchor.
  const tipPosFor = (cx: number, cy: number) => {
    const TIP_W = 336, TIP_H = 316;
    const below = cy + 22;
    return {
      x: Math.max(8, Math.min(cx + 20, window.innerWidth - TIP_W)),
      y: below + TIP_H <= window.innerHeight - 8 ? below : Math.max(8, cy - TIP_H - 16),
    };
  };

  const model = useMemo(() => {
    const fwd: Record<string, string[]> = {};
    edges.forEach(([a, b]) => { (fwd[a] = fwd[a] || []).push(b); });
    const real = (id: string) => !!objById[id] && !id.startsWith("db_");

    // Selection's data objects. Normally restricted to modernize disposition;
    // `includeAllData` drops that filter (any table/view/snapshot).
    const picks = selectedIds
      .map((id) => objById[id])
      .filter((o): o is EstateObject => !!o && ["table", "view", "snapshot"].includes(o.type) && (includeAllData || o.disposition === "modernize"));

    // Column index: attribute-name → the selected node column that provides it
    // (first wins), for the "selected source" cell.
    const colIndex = new Map<string, { source: string; type: string; profile?: Record<string, number | boolean> }>();
    picks.forEach((o) => columnsOf(o).forEach((c) => {
      const k = norm(c.name);
      if (!colIndex.has(k)) colIndex.set(k, { source: `${o.name || o.object_name}.${c.name}`, type: c.type, profile: c.profile });
    }));

    // Downstream reports of the selection (traverse fwd, hop through non-reports)
    // → the report objects + their columns, for the readiness section.
    const downstreamReports: { name: string; cols: { name: string; type: string }[] }[] = [];
    const seenR = new Set<string>();
    const stack = picks.flatMap((o) => fwd[o.id] || []);
    while (stack.length) {
      const n = stack.pop()!;
      if (seenR.has(n)) continue;
      seenR.add(n);
      if (!real(n)) continue;
      const o = objById[n];
      if (o.type === "report") downstreamReports.push({ name: o.name || o.object_name, cols: o.sample_columns || [] });
      (fwd[n] || []).forEach((m) => stack.push(m));
    }

    return { picks, colIndex, downstreamReports };
  }, [selectedIds, objById, edges, includeAllData]);

  // Signature of the selected source columns — drives the backend re-fetches.
  const colSig = [...model.colIndex.keys()].join(",");

  // INCREMENTAL, per-product analysis. Instead of one slow "score every model"
  // call (which returned all fits at the end, after the table had already
  // painted), we analyse each reference model as a COMPLETE UNIT — its fit AND
  // its per-column table together — and render it the moment it resolves. The
  // currently-selected product is analysed FIRST (so the main table + fit show
  // immediately), then the rest stream in. No cache: every run recomputes, so
  // changing source data or the scorer variant always reflects live results.
  const [rankResults, setRankResults] = useState<Map<string, ModelResult>>(new Map());
  useEffect(() => {
    if (projectId == null || !model.colIndex.size) { setRankResults(new Map()); return; }
    let cancelled = false;
    const sourceCols = [...model.colIndex.entries()].map(([k, v]) => ({ name: k, type: v.type, profile: v.profile }));
    const attrCount = new Map(referenceModels.map((m) => [m.id, m.attributes?.length ?? 0]));
    const ids = referenceModels.map((m) => m.id);
    // Selected product first (its table + fit land immediately), then the rest.
    const firstId = selectedProductId && ids.includes(selectedProductId) ? selectedProductId : ids[0];
    const ordered = firstId ? [firstId, ...ids.filter((id) => id !== firstId)] : ids;
    setRankResults(new Map());

    const analyzeOne = async (mid: string) => {
      const res = await fetchColumnSimilarity(projectId, sourceCols, { modelId: mid }, "feature_based");
      if (cancelled) return;
      const byCol: ByCol = new Map();
      let matched = 0, qualitySum = 0;
      for (const m of (res.matches || [])) {
        const raw = m as unknown as { source?: { name?: string }; target?: { name: string; type: string; concept?: string } | null; matched?: boolean };
        const src = raw.source?.name;
        if (src) byCol.set(norm(src), { match: m.match, sim: m, target: raw.target ?? null, matched: !!raw.matched });
        if (raw.matched) { matched++; qualitySum += m.match; }
      }
      const total = (res.matches || []).length;
      // FIT = coverage-weighted (unmatched columns count as 0 across the whole
      // selection), matching the old backend rank score.
      const score = total ? Math.round(qualitySum / total) : 0;
      const attrs = attrCount.get(mid) ?? 0;
      setRankResults((prev) => {
        const next = new Map(prev);
        next.set(mid, { score, matchedCount: matched, productCoverage: attrs ? matched / attrs : 0, byCol });
        return next;
      });
    };

    (async () => {
      // The selected/first product fully, on its own, first.
      if (ordered[0]) { try { await analyzeOne(ordered[0]); } catch { /* skip */ } }
      // Then the remaining products, a few at a time, each rendered as it lands.
      const rest = ordered.slice(1);
      const CONC = 4;
      let i = 0;
      await Promise.all(Array.from({ length: Math.min(CONC, rest.length) }, async () => {
        while (!cancelled && i < rest.length) {
          const mid = rest[i++];
          try { await analyzeOne(mid); } catch { /* skip a failed model */ }
        }
      }));
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, colSig, referenceModels]);

  const isRanking = model.colIndex.size > 0 && rankResults.size < referenceModels.length;

  // Effective ranking: models scored so far bubble to the top (by fit); the
  // rest show unscored ("?") until their per-product analysis lands.
  type Ranked = { model: ReferenceModel; matchedCount: number | null; score: number | null; productCoverage: number };
  const ranked = useMemo<Ranked[]>(() => {
    return referenceModels
      .map((m) => {
        const r = rankResults.get(m.id);
        return { model: m, matchedCount: r ? r.matchedCount : null, score: r ? r.score : null, productCoverage: r ? r.productCoverage : 0 };
      })
      .sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  }, [rankResults, referenceModels]);

  // Default the selected product to the best match whenever the ranking changes.
  const bestId = ranked.find((r) => (r.matchedCount ?? 0) > 0)?.model.id || ranked[0]?.model.id || null;
  useEffect(() => { setSelectedProductId(bestId); setProposal(null); setExpandedKey(null); }, [bestId]);

  // The DAG's preview was dismissed (Cancel / Proceed) → drop the proposed strip
  // so this panel and the DAG banner never show a stale live-preview state.
  useEffect(() => { if (!previewActive) setProposal(null); }, [previewActive]);

  // Measure the filled panel on every render so we know what height the empty
  // state should collapse FROM when the selection is cleared.
  useEffect(() => {
    if (model.picks.length && wrapRef.current) lastPanelH.current = wrapRef.current.offsetHeight;
  });
  // Selection cleared: pin the wrapper to the last measured height, animate it
  // down to the empty card, then release to natural height.
  useEffect(() => {
    if (model.picks.length) { setCollapseH(null); return; }
    if (lastPanelH.current < 140) return;
    setCollapseH(lastPanelH.current);
    const shrink = setTimeout(() => setCollapseH(100), 30);
    const release = setTimeout(() => { setCollapseH(null); lastPanelH.current = 0; }, 400);
    return () => { clearTimeout(shrink); clearTimeout(release); };
  }, [model.picks.length]);

  const selected = ranked.find((r) => r.model.id === selectedProductId) || ranked.find((r) => r.model.id === bestId);

  // The selected product's per-column table comes from the SAME per-product
  // analysis that produced its fit (no second fetch) — so table + fit are always
  // in lockstep. `null` until that product's unit has been analysed.
  const cmp = useMemo(() => {
    const r = selected ? rankResults.get(selected.model.id) : undefined;
    return r ? { byCol: r.byCol } : null;
  }, [rankResults, selected]);

  const rows = useMemo(() => {
    if (!selected) return { selected: [] as ReturnType<typeof rowFor>[] };
    // Downstream usage per column: which reports actually READ this column —
    // surfaces "maps to the product but nothing downstream consumes it".
    const reportsByCol = new Map<string, string[]>();
    model.downstreamReports.forEach((r) => r.cols.forEach((c) => {
      const k = norm(c.name);
      reportsByCol.set(k, [...(reportsByCol.get(k) || []), r.name]);
    }));
    function rowFor([k, v]: [string, { source: string; type: string }]) {
      // The backend schema-DNA scorer is the ONLY source of truth. Until its
      // scores load, the row is `loading` (match/attribute render as "?") — we
      // never fabricate a score from an offline heuristic. Once loaded, the
      // matched attribute it found (fuzzy/semantic, not just exact-name) drives
      // both the score and the "Data Product Attribute" columns.
      const backend = cmp?.byCol.get(k);
      if (!backend) {
        return { key: k, source: v.source, type: v.type, pa: null as Attr | null, closest: null as Attr | null, match: null as number | null, belowThreshold: false, loading: true, reports: reportsByCol.get(k) || [] };
      }
      const tgt: Attr | null = backend.target
        ? { name: backend.target.name, type: backend.target.type, concept: backend.target.concept }
        : null;
      // A confident match populates `pa` (rendered normally). Below the bar we
      // still keep the closest candidate as `closest` — surfaced GREYED so the
      // engineer sees what it nearly mapped to without it reading as a match.
      const pa: Attr | null = backend.matched ? tgt : null;
      const closest: Attr | null = backend.matched ? null : tgt;
      return { key: k, source: v.source, type: v.type, pa, closest, match: Math.round(backend.match), belowThreshold: !backend.matched, loading: false, reports: reportsByCol.get(k) || [] };
    }
    // A single combined "Selected" list — every distinct column across the
    // selected nodes (deduped, source labelled node.column), matched first.
    const selRows = [...model.colIndex.entries()].map(rowFor)
      .sort((a, b) => (b.pa ? 1 : 0) - (a.pa ? 1 : 0));
    return { selected: selRows };
  }, [selected, model, cmp]);

  // Downstream report readiness: for each report consuming the selection, how
  // much of it the SELECTED SOURCES provide (the data going into the product),
  // plus which upstream source product could be connected to fill each gap.
  const readiness = useMemo(() => {
    if (!selected) return [] as { name: string; pct: number; met: string[]; gaps: { col: string }[] }[];
    // Columns provided by the selected source nodes (what the new product will carry).
    const pset = new Set([...model.colIndex.keys()]);
    return model.downstreamReports.map((r) => {
      const met = r.cols.filter((c) => pset.has(norm(c.name)));
      // Attribute gaps only — the report columns the selection doesn't yet cover.
      const gaps = r.cols.filter((c) => !pset.has(norm(c.name))).map((c) => ({ col: c.name }));
      const pct = r.cols.length ? Math.round((100 * met.length) / r.cols.length) : 0;
      return { name: r.name, pct, met: met.map((c) => c.name), gaps };
    }).sort((a, b) => b.pct - a.pct);
  }, [selected, model]);

  // Selection columns that NO downstream report reads — candidate columns the
  // new product could carry but nothing consumes yet (only meaningful when the
  // selection actually has downstream reports to measure against).
  const unusedDownstream = useMemo(() => {
    if (!model.downstreamReports.length) return [] as { col: string; source: string }[];
    const used = new Set<string>();
    model.downstreamReports.forEach((r) => r.cols.forEach((c) => used.add(norm(c.name))));
    return [...model.colIndex.entries()]
      .filter(([k]) => !used.has(k))
      .map(([k, v]) => ({ col: v.source.includes(".") ? v.source.split(".").pop()! : k, source: v.source }));
  }, [model]);

  // Propose from the FULL selection → open the DAG's what-if preview (the base
  // case AFTER adding this product: it consolidates the sources, serves the
  // downstream reports, and marks replaced objects to retire). The user reviews
  // that preview and hits "Proceed to wizard" there — same flow as the DAG's own
  // Create button. Falls back to a direct create only when no preview handler is
  // wired.
  const propose = async () => {
    if (busy) return;
    const ids = model.picks.map((o) => o.id);
    if (!ids.length) return;
    setBusy(true); setProposal(null);
    try {
      const p = onProposeProduct ? await onProposeProduct(ids) : undefined;
      // Name the preview after the reference data product the user picked in the
      // analysis below (the ranked selection) — not the LLM's composed name — so
      // the preview node reflects the product they chose to consolidate into.
      let similarity: ProductSimilarity | undefined;
      if (p && selected && projectId != null) {
        // Only the backend schema-DNA scorer — no fabricated offline fallback.
        // If it's unavailable the preview simply carries no similarity spider.
        try {
          const sourceCols = [...model.colIndex.entries()].map(([k, v]) => ({ name: k, type: v.type, profile: v.profile }));
          const res = await fetchColumnSimilarity(projectId, sourceCols, { modelId: selected.model.id }, "feature_based");
          similarity = { ...res.aggregate, title: selected.model.name };
        } catch {
          similarity = undefined;
        }
      }
      const named = p && selected ? { ...p, name: selected.model.name, similarity } : p;
      setProposal(named ?? null);
      if (onPreviewProduct && named) onPreviewProduct(ids, named);
      else await onCreateProduct?.(ids, named);
    } finally { setBusy(false); }
  };

  if (!model.picks.length) {
    return (
      // Collapse smoothly from the analysis panel's last height down to the
      // empty-state card, instead of snapping and making the page jump.
      <div style={{ height: collapseH ?? undefined, overflow: "hidden", transition: "height 320ms cubic-bezier(0.33,1,0.68,1)" }}>
        <div style={{ ...CARD, color: "#94a3b8", fontSize: 13, textAlign: "center", padding: "28px 14px" }}>
          {includeAllData
            ? "Select one or more tables, views, or snapshots in the graph above to analyze them and plan a data product."
            : "Select one or more nodes with a similar disposition in the graph above to analyze the reasoning behind their disposition and plan how to handle them."}
        </div>
      </div>
    );
  }

  // Top 10 by score by default; a search box query filters across ALL products.
  const q = search.trim().toLowerCase();
  const scoreBarFilter = q
    ? ranked.filter((r) => r.model.name.toLowerCase().includes(q))
    : ranked.slice(0, 10);

  return (
    <div ref={wrapRef} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* Attribute-level hover radar — explains the hovered row's match score.
          Fades/scales in on open, then glides between rows while it stays open. */}
      {hoverSim && (
        <>
          <style>{`@keyframes simRadarIn { from { opacity: 0; transform: translateY(5px) scale(0.97); } to { opacity: 1; transform: translateY(0) scale(1); } }`}</style>
          <div style={{
            position: "fixed", left: hoverSim.x, top: hoverSim.y, zIndex: 60, width: 300,
            background: "#fff", border: "1px solid #e2e8f0", borderRadius: 10,
            boxShadow: "0 10px 32px rgba(15,23,42,0.18), 0 2px 8px rgba(15,23,42,0.08)",
            padding: "10px 14px", pointerEvents: "none",
            animation: "simRadarIn 140ms ease-out",
            transition: "left 160ms cubic-bezier(0.33,1,0.68,1), top 160ms cubic-bezier(0.33,1,0.68,1)",
          }}>
            <div style={{ fontSize: 12, fontWeight: 800, color: hoverSim.muted ? "#94a3b8" : P.fg, textAlign: "center", marginBottom: 2 }}>{hoverSim.sim.title}</div>
            <SimilarityRadar data={hoverSim.sim} compact layout="bars" muted={hoverSim.muted} />
          </div>
        </>
      )}
      {/* Selection summary */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: "#334155" }}>Analyzing {model.picks.length} {includeAllData ? "" : "modernize "}node{model.picks.length !== 1 ? "s" : ""}:</span>
        {model.picks.map((o) => (
          <span key={o.id} style={{ fontSize: 11.5, fontFamily: "ui-monospace, monospace", padding: "2px 9px", borderRadius: 999, background: "#eff6ff", border: "1px solid #bfdbfe", color: "#1e293b" }}>
            {o.name || o.object_name}
          </span>
        ))}
      </div>

      <div style={{ display: "flex", gap: 14, alignItems: "flex-start", flexWrap: "wrap" }}>
        {/* Left rail — searchable, score-ranked data products (sideways bars) */}
        <div style={{ ...CARD, width: 300, flex: "none", padding: "12px" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#334155", textTransform: "uppercase", letterSpacing: 0.4 }}>Reference data products</div>
            {isRanking && (
              <>
                <div style={{ width: 13, height: 13, borderRadius: "50%", border: "2px solid #e2e8f0", borderTopColor: "#334155", animation: "pcp-spin 0.9s linear infinite", flexShrink: 0 }} />
                <style>{`@keyframes pcp-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }`}</style>
              </>
            )}
          </div>
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search products…"
            style={{ width: "100%", boxSizing: "border-box", padding: "6px 10px", borderRadius: 7, border: "1px solid #e2e8f0", fontSize: 12.5, marginBottom: 10 }} />
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {scoreBarFilter.map((r) => {
              const active = r.model.id === selected?.model.id;
              // score === null → backend hasn't scored yet; show "?" and a flat
              // neutral bar instead of a fabricated percentage.
              const pending = r.score == null;
              return (
                <button key={r.model.id} onClick={() => { setSelectedProductId(r.model.id); setProposal(null); setExpandedKey(null); }}
                  style={{ textAlign: "left", background: active ? "#eff6ff" : "#fff", border: `1px solid ${active ? "#1e293b" : "#e2e8f0"}`, borderRadius: 8, padding: "8px 10px", cursor: "pointer" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 12.5, fontWeight: 700, color: "#1e293b", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.model.name}</span>
                    <span style={pending
                      ? { fontSize: 11.5, fontWeight: 700, color: "#94a3b8" }
                      : active
                        ? { fontSize: 11.5, fontWeight: 700, color: "#1e293b", background: rygColor(r.score!), border: "1px solid rgba(15,23,42,0.65)", borderRadius: 6, padding: "0 5px" }
                        : { fontSize: 11.5, fontWeight: 700, color: navyScale(r.score!) }}>{pending ? "?" : `${r.score}%`}</span>
                  </div>
                  <div style={{ height: 7, borderRadius: 4, background: "#f1f5f9", marginTop: 5, overflow: "hidden" }}>
                    <div style={{ width: pending ? "0%" : `${r.score}%`, height: "100%", boxSizing: "border-box", borderRadius: 4, background: pending ? "transparent" : active ? rygColor(r.score!) : navyScale(r.score!), border: active && !pending ? "1px solid rgba(15,23,42,0.65)" : "none" }} />
                  </div>
                  <div style={{ fontSize: 10.5, color: "#94a3b8", marginTop: 4 }}>{r.matchedCount ?? "?"} matched · {r.model.attributes.length} attrs · {r.model.kind === "aggregated" ? "360" : r.model.domain}</div>
                </button>
              );
            })}
          </div>
        </div>

        {/* Main — comparison table for the selected product */}
        <div style={{ ...CARD, flex: 1, minWidth: 420, padding: 0, overflow: "hidden" }}>
          {selected && (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", padding: "12px 14px", borderBottom: "1px solid #e2e8f0", background: "#eff6ff" }}>
                <div>
                  <div style={{ fontWeight: 800, fontSize: 15, color: "#1e293b" }}>{selected.model.name}</div>
                  <div style={{ fontSize: 11.5, color: "#475569" }}>{selected.model.domain} · {selected.model.kind} · {selected.score == null
                    ? <b style={{ color: "#94a3b8" }}>? fit</b>
                    : <b style={{ color: "#1e293b", background: rygColor(selected.score), border: "1px solid rgba(15,23,42,0.65)", borderRadius: 6, padding: "0 5px" }}>{selected.score}% fit</b>}</div>
                </div>
                <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
                  <button onClick={() => (previewActive ? onCancelPreview?.() : propose())} disabled={busy || (!previewActive && !onProposeProduct)}
                    style={{ fontSize: 12, fontWeight: 700, color: "#fff", background: previewActive ? "#64748b" : P.fg, border: "none", borderRadius: 7, padding: "7px 13px", cursor: busy ? "default" : "pointer", opacity: busy ? 0.7 : 1 }}>
                    {busy ? "Composing…" : previewActive ? "✕ Exit preview" : "✦ Preview proposed change"}
                  </button>
                  <button onClick={() => onCreateProduct?.(model.picks.map((o) => o.id), proposal ?? undefined)} disabled={busy || !onCreateProduct}
                    style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, fontWeight: 700, color: "#1e293b", background: "#fff", border: "1px solid #cbd5e1", borderRadius: 7, padding: "7px 13px", cursor: busy ? "default" : "pointer" }}>
                    Send to intake <ExtLinkIcon />
                  </button>
                </div>
              </div>
              {proposal && (
                <div style={{ padding: "9px 14px", background: "#f8fafc", borderBottom: "1px solid #bfdbfe", fontSize: 12, color: "#1e293b", display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                  Proposed <b>{proposal.name}</b> · {(proposal.columns || []).length} columns
                  {onPreviewProduct && <span style={{ color: "#475569" }}>↑ Preview shown in the {previewSurface} above.</span>}
                </div>
              )}
              <div ref={tableWrapRef} style={{ overflowX: "auto" }} onMouseLeave={clearHover}>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <thead>
                    <tr>
                      <th style={th} rowSpan={2}>Selected source · match</th>
                      <th style={{ ...th, textAlign: "center", borderBottom: "none" }} colSpan={2}>Data product attribute</th>
                      <th style={th} rowSpan={2}>Used in reports</th>
                    </tr>
                    <tr>
                      <th style={{ ...th, fontWeight: 600 }}>Industry concept</th>
                      <th style={{ ...th, fontWeight: 600 }}>Attribute</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.selected.map(({ key, source, type, pa, closest, match, belowThreshold, loading, reports }) => (
                      <Fragment key={key}>
                      <tr
                        onClick={() => { if (reports.length) setExpandedKey((cur) => (cur === key ? null : key)); }}
                        onMouseEnter={(e) => {
                          if (hoverTimer.current) { clearTimeout(hoverTimer.current); hoverTimer.current = null; }
                          // Only the backend scorer's real per-column axes drive the
                          // spider — no offline fallback. No sim → no popup.
                          const sim = cmp?.byCol.get(key)?.sim;
                          // Show the metric spider for ANY scored row — including
                          // below-threshold rows (the closest match) — not just
                          // confident matches. Only a still-loading row (no sim) is
                          // suppressed.
                          if (match == null || !sim) { setHoverSim(null); return; }
                          // Below-threshold rows render the spider GREY (muted).
                          const next = { key, sim, muted: belowThreshold, ...tipPosFor(e.clientX, e.clientY) };
                          // Already open → retarget instantly (the popup glides);
                          // otherwise wait a beat so sweeping the table doesn't flash it.
                          setHoverSim((cur) => { if (cur) return next; hoverTimer.current = setTimeout(() => setHoverSim(next), 130); return cur; });
                        }}
                        onMouseMove={(e) => {
                          if (match == null) return;
                          // Trail the cursor (~30fps throttle; the popup's left/top
                          // transition smooths the movement into a soft follow).
                          const now = performance.now();
                          if (now - hoverLastMove.current < 33) return;
                          hoverLastMove.current = now;
                          const pos = tipPosFor(e.clientX, e.clientY);
                          setHoverSim((cur) => (cur && cur.key === key ? { ...cur, ...pos } : cur));
                        }}
                        onMouseLeave={clearHover}
                        style={{
                          cursor: match != null ? "help" : undefined,
                          background: hoverSim?.key === key ? P.wash : undefined,
                          transition: "background 120ms ease",
                        }}
                      >
                        <td style={td}>
                          <span style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
                            {loading
                              // Not scored YET (still fetching) → grey "?" — the
                              // only case that's genuinely unknown.
                              ? <span style={{ ...badgeStyle(0, true), background: "#f1f5f9", color: "#94a3b8" }}>?</span>
                              : (match != null && !belowThreshold)
                                // Confident match → the coloured score.
                                ? <span style={badgeStyle(match)}>{match}</span>
                                // Below the bar → the GREYED closest score (so the
                                // engineer sees how near it came), never on the
                                // red/yellow/green scale. No candidate at all → dash.
                                : match != null
                                  ? <span style={{ ...badgeStyle(0, true), background: "#f1f5f9", color: "#94a3b8" }}>{match}</span>
                                  : <span style={{ ...badgeStyle(0, true), background: "#f1f5f9", color: "#cbd5e1" }}>—</span>}
                            <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 11.5, color: pa ? "#1e293b" : "#64748b" }}>{source}</span>
                            <span style={{ color: "#94a3b8", fontSize: 11 }}>{type}</span>
                          </span>
                        </td>
                        <td style={td}>{loading
                          ? <span style={{ color: "#cbd5e1" }}>?</span>
                          : pa ? conceptCell(pa.concept)
                          // Below threshold: the closest candidate's concept,
                          // greyed — important context, clearly not a match.
                          : closest ? <span style={{ opacity: 0.55, filter: "grayscale(1)" }}>{conceptCell(closest.concept)}</span>
                          : <span style={{ color: "#cbd5e1" }}>— not in product</span>}</td>
                        <td style={td}>{loading
                          ? <span style={{ color: "#cbd5e1" }}>?</span>
                          : pa ? attrCell(pa.name, pa.type)
                          : closest ? (
                              <span style={{ color: "#94a3b8" }} title="Closest candidate — below the match threshold">
                                <span style={{ fontFamily: "ui-monospace, monospace", fontWeight: 700 }}>{closest.name}</span>
                                <span style={{ marginLeft: 6, fontSize: 11 }}>{closest.type}</span>
                                <span style={{ marginLeft: 6, fontSize: 10, fontStyle: "italic" }}>· closest</span>
                              </span>
                            )
                          : dash}</td>
                        <td style={{ ...td, whiteSpace: "nowrap" }}>
                          {reports.length ? (
                            <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 10.5, fontWeight: 700, padding: "2px 9px", borderRadius: 999, background: "#f0fdf4", border: "1px solid #bbf7d0", color: "#166534" }}>
                              {reports.length} report{reports.length !== 1 ? "s" : ""}
                              <span style={{ fontSize: 9, transform: expandedKey === key ? "rotate(180deg)" : undefined, transition: "transform 140ms ease", display: "inline-block" }}>▾</span>
                            </span>
                          ) : model.downstreamReports.length ? (
                            <span style={{ fontSize: 10.5, fontWeight: 700, padding: "2px 8px", borderRadius: 999, background: "#fffbeb", border: "1px solid #fde68a", color: "#92400e" }}>⚠ unused</span>
                          ) : dash}
                        </td>
                      </tr>
                      {expandedKey === key && reports.length > 0 && (
                        <tr>
                          <td colSpan={4} style={{ ...td, background: "#fafcfa", borderLeft: "3px solid #bbf7d0", padding: "8px 12px" }}>
                            <span style={{ fontSize: 10.5, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.4, marginRight: 8 }}>Read by</span>
                            <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 5, verticalAlign: "middle" }}>
                              {reports.map((rn) => (
                                <span key={rn} style={{ fontSize: 10.5, fontWeight: 600, padding: "2px 9px", borderRadius: 999, background: "#f0fdf4", border: "1px solid #bbf7d0", color: "#166534", whiteSpace: "nowrap" }}>{rn}</span>
                              ))}
                            </span>
                          </td>
                        </tr>
                      )}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>

        {/* Downstream report readiness — its own column on the right: % of each
            report met by this product + the source product to fill each gap. */}
        {selected && readiness.length > 0 && (
          <div style={{ ...CARD, width: 340, flex: "none" }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#334155", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 12 }}>Downstream report readiness</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {readiness.map((r) => (
                <div key={r.name}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 12.5, fontWeight: 700, color: "#1e293b", flex: 1, minWidth: 0 }}>{r.name}</span>
                    <span style={{ fontSize: 11.5, fontWeight: 700, color: "#15803d" }}>{r.pct}%</span>
                  </div>
                  <div style={{ width: "100%", height: 7, borderRadius: 4, background: "#f1f5f9", overflow: "hidden", margin: "5px 0 6px" }}>
                    <div style={{ width: `${r.pct}%`, height: "100%", background: matchColor(r.pct).bg }} />
                  </div>
                  <div style={{ fontSize: 11, color: "#64748b" }}>
                    <span style={{ fontWeight: 600, color: "#15803d" }}>Met: </span>
                    {r.met.length ? r.met.join(", ") : "—"}
                  </div>
                  {r.gaps.length > 0 && (
                    <div style={{ marginTop: 6, fontSize: 11 }}>
                      <div style={{ fontWeight: 600, color: "#b91c1c", marginBottom: 4 }}>Gap</div>
                      <div style={{ display: "flex", flexDirection: "column", gap: 3, borderLeft: "2px solid #fecaca", paddingLeft: 9 }}>
                        {r.gaps.map((g) => (
                          <div key={g.col} style={{ display: "flex", alignItems: "baseline", gap: 6, lineHeight: 1.35 }}>
                            <span style={{ fontFamily: "ui-monospace, monospace", color: "#991b1b", flex: "none" }}>{g.col}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
            {unusedDownstream.length > 0 && (
              <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid #e2e8f0" }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: "#334155", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 3 }}>Not used downstream</div>
                <div style={{ fontSize: 10.5, color: "#94a3b8", marginBottom: 6 }}>Selected source columns no downstream report reads.</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 3, borderLeft: "2px solid #e2e8f0", paddingLeft: 9 }}>
                  {unusedDownstream.map((u) => (
                    <div key={u.source} style={{ display: "flex", alignItems: "baseline", gap: 6, fontSize: 11, lineHeight: 1.35 }}>
                      <span style={{ fontFamily: "ui-monospace, monospace", color: "#475569", flex: "none" }}>{u.col}</span>
                      <span style={{ color: "#cbd5e1", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{u.source}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
