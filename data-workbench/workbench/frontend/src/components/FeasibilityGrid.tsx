// Stoplight grid for a top-down feasibility run: one chip per reference spec
// (ready | adaptable | assemblable | absent) + an evaluation-state badge + a
// required-coverage bar, with a drill-down (unified filterable attribute table
// with per-attribute alternatives, expandable schema rationale, join plan,
// rationale) + a downloadable .md report + a tier action.
import { Fragment, useState, type CSSProperties } from "react";
import api from "../api/client";

export interface FeasibilityScore {
  score_id?: number;
  spec_id: string;
  spec_name: string;
  domain: string | null;
  tier: string;             // ready | adaptable | assemblable | absent
  evaluation_state: string; // completed | partial | insufficient_evidence | failed
  confidence: number;
  required_coverage: number;
  total_coverage: number;
  best_product_uri: string | null;
  adaptation_notes: string;
  rationale: string;
}

interface RunMeta {
  scoring_version?: string;
  corpus_version?: string;
  skill_version?: string;
  embedding_model?: string;
  scan_ids?: number[];
  created_at?: string;
  summary?: { description_coverage?: { weak_schemas?: string[] } };
}

// Calmer palette that complements the app's violet primary (#7c3aed).
// Absent is neutral slate (informational), not angry red.
const TIER: Record<string, { bg: string; fg: string; lightBg: string; lightFg: string; label: string; desc: string }> = {
  ready:       { bg: "#10b981", fg: "#fff",     lightBg: "#ecfdf5", lightFg: "#065f46", label: "Ready",       desc: "A published product already covers the required attributes — adopt it directly." },
  adaptable:   { bg: "#38bdf8", fg: "#0c4a6e",  lightBg: "#e0f2fe", lightFg: "#0369a1", label: "Adaptable",   desc: "A published product covers most attributes; a bounded transformation closes the gap." },
  assemblable: { bg: "#f59e0b", fg: "#fff",     lightBg: "#fef3c7", lightFg: "#78350f", label: "Assemblable", desc: "Raw estate data covers the requirements but isn't a governed product yet — needs engineering." },
  absent:      { bg: "#94a3b8", fg: "#fff",     lightBg: "#f1f5f9", lightFg: "#475569", label: "Absent",      desc: "No published product or estate raw data covers this spec's required attributes." },
};
const EVAL: Record<string, [string, string]> = {
  completed: ["#dcfce7", "#166534"],
  partial: ["#fef3c7", "#92400e"],
  insufficient_evidence: ["#e2e8f0", "#475569"],
  failed: ["#fee2e2", "#991b1b"],
};
// Per-attribute status chip palette + label.
const STATUS: Record<string, [string, string, string]> = {
  direct_match: ["#dcfce7", "#166534", "Direct"],
  derivable:    ["#e0f2fe", "#0369a1", "Derivable"],
  missing:      ["#fee2e2", "#991b1b", "Missing"],
  unknown:      ["#e2e8f0", "#475569", "Unknown"],
};
// Human labels for the alternative/gap rejection reason codes.
const REASON_LABEL: Record<string, string> = {
  chosen: "chosen",
  below_threshold: "below match threshold",
  identifier_mismatch: "identifier vs. category mismatch",
  type_incompatible: "type incompatible",
  wrong_entity: "wrong entity / weaker table",
  fk_carrier: "inferred FK-like carrier",
  insufficient_evidence: "no candidate columns",
  superseded_by_derivation: "superseded by a composite derivation",
};
// Human labels for the Stage-1 schema-shortlist excluded_reason codes. Kept SEPARATE
// from REASON_LABEL above (which labels attribute-level match reasons) because
// `below_threshold` means different things per surface: "below the shortlist
// threshold" for a schema vs. "below the match threshold" for an attribute.
const SCHEMA_REASON_LABEL: Record<string, string> = {
  below_floor: "below relevance floor",
  below_threshold: "below shortlist threshold",
  over_cap: "over max-schemas cap",
  user_excluded: "excluded by you",
  user_included: "included by you",
};

const s: Record<string, any> = {
  card: { border: "1px solid #e2e8f0", borderRadius: 8, marginBottom: 8, background: "#fff", overflow: "hidden" },
  head: { display: "flex", alignItems: "center", gap: 10, padding: "10px 12px", cursor: "pointer" },
  name: { fontWeight: 700, color: "#0f172a", fontSize: 14, flex: 1 },
  chip: (bg: string, fg: string): CSSProperties => ({
    fontSize: 11, fontWeight: 700, padding: "2px 9px", borderRadius: 5,
    background: bg, color: fg, whiteSpace: "nowrap",
  }),
  bar: { height: 8, borderRadius: 4, background: "#e2e8f0", width: 120, overflow: "hidden" },
  barFill: (pct: number, color: string): CSSProperties => ({ height: 8, width: `${Math.round(pct * 100)}%`, background: color }),
  detail: { padding: "10px 14px", borderTop: "1px solid #f1f5f9", background: "#f8fafc", fontSize: 13 },
  th: { textAlign: "left", fontSize: 11, color: "#64748b", fontWeight: 700, padding: "4px 8px", cursor: "help", background: "#f1f5f9", position: "sticky", top: 0, zIndex: 1 },
  td: { fontSize: 12, color: "#334155", padding: "3px 8px", borderTop: "1px solid #eef2f7", verticalAlign: "top" },
  btn: { fontSize: 12, fontWeight: 600, padding: "6px 12px", borderRadius: 6, border: "none", background: "#7c3aed", color: "#fff", cursor: "pointer", marginTop: 8 },
  saveBtn: (saved: boolean): CSSProperties => ({
    fontSize: 11, fontWeight: 600, padding: "4px 10px", borderRadius: 5, border: "none",
    background: saved ? "#dcfce7" : "#f1f5f9", color: saved ? "#065f46" : "#475569",
    cursor: "pointer", marginTop: 8, marginLeft: 8,
  }),
  dismissBtn: { fontSize: 11, fontWeight: 600, padding: "4px 8px", borderRadius: 5, border: "none", background: "none", color: "#94a3b8", cursor: "pointer", marginTop: 8 },
  noteInput: { fontSize: 12, padding: "4px 8px", border: "1px solid #cbd5e1", borderRadius: 5, width: 220, marginTop: 8 },
  filterChip: (active: boolean): CSSProperties => ({
    fontSize: 11, fontWeight: 700, padding: "3px 9px", borderRadius: 12, cursor: "pointer",
    border: active ? "1px solid #7c3aed" : "1px solid #e2e8f0",
    background: active ? "#ede9fe" : "#fff", color: active ? "#5b21b6" : "#64748b",
  }),
  textFilter: { fontSize: 12, padding: "4px 8px", border: "1px solid #cbd5e1", borderRadius: 5, width: 160 },
  reportBtn: { fontSize: 11, fontWeight: 600, padding: "4px 10px", borderRadius: 5, border: "1px solid #e2e8f0", background: "#fff", color: "#475569", cursor: "pointer" },
};

type CompositeDetail = {
  kind: string; operator?: string; separator?: string; table?: string; source?: string;
  components: { role: string; column: string; score: number }[];
};
type AttrRow = {
  attr_id: string; spec_attr: string; required: boolean; is_key?: boolean;
  status: string; matched: boolean; match_kind?: string; composite?: CompositeDetail;
  column?: string; score?: number; column_semantic?: number; table_affinity?: number;
  adjusted?: number; derivation?: string | null; is_fk_carrier?: boolean; fk_target_table?: string;
  dataset_platform?: string; dataset_database?: string; dataset_schema?: string;
  dataset_table?: string; product_name?: string; reason?: string;
  name_only?: boolean; matcher_pinned?: boolean;
  evidence_quality?: { column_described?: boolean; attribute_described?: boolean; name_only?: boolean };
};

// A compact "role → column (score)" summary of a composite derivation.
function compositeSummary(c: CompositeDetail): string {
  return (c.components || []).map((p) => `${p.role} → ${p.column} (${Math.round(p.score)})`).join(", ");
}

// Parse the leading index from a stable attribute id (`#3:customer_id`) for a
// stable ordering that survives duplicate names.
function attrIndex(attrId: string): number {
  const m = /^#(\d+):/.exec(attrId || "");
  return m ? Number(m[1]) : 1e9;
}

// Markdown table-cell escape: pipes + newlines would break a row / a cell.
function mdCell(v: unknown): string {
  return String(v ?? "").replace(/\|/g, "\\|").replace(/\r?\n/g, " ").trim();
}

function Bar({ value, tier }: { value: number; tier: string }) {
  const color = (TIER[tier] || TIER.absent).bg;
  const pct = Math.round(value * 100);
  return (
    <div
      title={`Required-attribute coverage: ${pct}% of this spec's required attributes are covered by the best-matching data source.`}
      style={s.bar}
    >
      <div style={s.barFill(value, color)} />
    </div>
  );
}

function sourceLabel(m: { product_name?: string; dataset_platform?: string; dataset_database?: string; dataset_schema?: string; dataset_table?: string }): string {
  if (m.product_name) return m.product_name;
  const parts: string[] = [];
  if (m.dataset_platform) parts.push(m.dataset_platform);
  const loc: string[] = [];
  if (m.dataset_database) loc.push(m.dataset_database);
  if (m.dataset_schema) loc.push(m.dataset_schema);
  if (m.dataset_table) loc.push(m.dataset_table);
  if (loc.length) parts.push(loc.join("."));
  return parts.join(" → ") || "—";
}

function SourceCell({ m }: { m: AttrRow }) {
  if (m.product_name) return <span style={{ color: "#7c3aed" }}>{m.product_name}</span>;
  return <span style={{ color: "#475569", fontFamily: "monospace", fontSize: 11 }}>{sourceLabel(m)}</span>;
}

function StatusChip({ status }: { status: string }) {
  const [bg, fg, label] = STATUS[status] || STATUS.unknown;
  return <span style={s.chip(bg, fg)}>{label}</span>;
}

export default function FeasibilityGrid({
  runId,
  scores,
  savedSpecIds = new Set<string>(),
  onSave,
  runMeta = {},
}: {
  runId: number;
  scores: FeasibilityScore[];
  savedSpecIds?: Set<string>;
  onSave?: (specId: string, saved: boolean) => void;
  runMeta?: RunMeta;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, any>>({});
  const [action, setAction] = useState<Record<string, any>>({});
  const [localSaved, setLocalSaved] = useState<Set<string>>(new Set());
  const [noteFor, setNoteFor] = useState<string | null>(null);
  const [noteText, setNoteText] = useState<string>("");
  const [saving, setSaving] = useState<string | null>(null);
  // Per-spec attribute-table filter state (keyed by spec_id).
  const [filter, setFilter] = useState<Record<string, string>>({});   // all | required | gaps | matched
  const [textFilter, setTextFilter] = useState<Record<string, string>>({});
  // Which attribute rows / schema chips are expanded (lazy alternatives + rationale).
  const [expandedAttr, setExpandedAttr] = useState<Set<string>>(new Set());
  const [expandedSchema, setExpandedSchema] = useState<Set<string>>(new Set());

  const isSaved = (specId: string) => savedSpecIds.has(specId) || localSaved.has(specId);

  async function toggle(specId: string) {
    if (open === specId) { setOpen(null); return; }
    setOpen(specId);
    if (!detail[specId]) {
      try {
        const r = await api.get(`/api/feasibility/runs/${runId}/scores/${specId}`);
        setDetail((d) => ({ ...d, [specId]: r.data }));
      } catch { /* ignore */ }
    }
  }

  async function act(specId: string) {
    try {
      const r = await api.get(`/api/feasibility/runs/${runId}/scores/${specId}/action`);
      setAction((a) => ({ ...a, [specId]: r.data }));
      const d = r.data;
      if (d.action === "adopt" && d.marketplace_path) window.location.assign(d.marketplace_path);
      else if (d.action === "adapt" && d.wizard_path) window.location.assign(d.wizard_path);
      else if (d.action === "assemble") {
        const res = await api.post(`/api/feasibility/runs/${runId}/scores/${specId}/act`);
        if (res.data.review_path) window.location.assign(res.data.review_path);
      }
    } catch (e: any) {
      setAction((a) => ({ ...a, [specId]: { error: e?.response?.data?.detail?.message || "Action failed" } }));
    }
  }

  // "Work this product" — open (or re-open) the Assembly workspace over a score.
  // Tier-aware routing: ready/adaptable keep their lighter flows via act(); the
  // assemblable AND absent tiers open the workspace (absent is the highest-value
  // case — the template is curated down to what the estate can provide).
  async function workThisProduct(specId: string) {
    try {
      const r = await api.post(`/api/assembly`, { run_id: runId, spec_id: specId });
      if (r.data?.id) window.location.assign(`/product/assembly/${r.data.id}`);
    } catch (e: any) {
      setAction((a) => ({ ...a, [specId]: { error: e?.response?.data?.detail?.message || e?.response?.data?.detail || "Could not open workspace" } }));
    }
  }

  function startSave(specId: string) { setNoteFor(specId); setNoteText(""); }

  async function confirmSave(sc: FeasibilityScore) {
    const scoreId = sc.score_id ?? detail[sc.spec_id]?.score_id;
    if (!scoreId) { setNoteFor(null); return; }
    setSaving(sc.spec_id);
    try {
      await api.post(`/api/feasibility/scores/${scoreId}/candidate`, { notes: noteText });
      setLocalSaved((prev) => new Set([...prev, sc.spec_id]));
      onSave?.(sc.spec_id, true);
    } catch { /* ignore */ }
    setNoteFor(null);
    setSaving(null);
  }

  async function dismissSave(sc: FeasibilityScore) {
    const scoreId = sc.score_id ?? detail[sc.spec_id]?.score_id;
    if (!scoreId) return;
    try {
      await api.delete(`/api/feasibility/scores/${scoreId}/candidate`);
      setLocalSaved((prev) => { const n = new Set(prev); n.delete(sc.spec_id); return n; });
      onSave?.(sc.spec_id, false);
    } catch { /* ignore */ }
  }

  // Build the full attribute list (matched ∪ gaps) keyed by stable attr id.
  function buildAttrRows(d: any): AttrRow[] {
    const rows: AttrRow[] = [];
    for (const m of (d?.matched || [])) {
      rows.push({ ...m, matched: true, status: m.status || (m.derivation ? "derivable" : "direct_match") });
    }
    for (const g of (d?.gaps || [])) {
      rows.push({ ...g, matched: false, status: g.status || "missing" });
    }
    rows.sort((a, b) => attrIndex(a.attr_id) - attrIndex(b.attr_id));
    return rows;
  }

  function toggleAttr(key: string) {
    setExpandedAttr((prev) => { const n = new Set(prev); if (n.has(key)) n.delete(key); else n.add(key); return n; });
  }
  function toggleSchema(key: string) {
    setExpandedSchema((prev) => { const n = new Set(prev); if (n.has(key)) n.delete(key); else n.add(key); return n; });
  }

  // Client-side .md report — all data is already in the loaded score detail.
  function downloadReport(sc: FeasibilityScore, d: any) {
    const ev = d?.evidence || {};
    const rows = buildAttrRows(d);
    const alts = ev?.raw_candidates?.alternatives || {};
    const reqCovPct = Math.round((sc.required_coverage || 0) * 100);
    const totCovPct = Math.round((sc.total_coverage || 0) * 100);
    const L: string[] = [];
    L.push(`# Feasibility report — ${sc.spec_name}`);
    L.push("");
    L.push(`- **Tier:** ${sc.tier}`);
    L.push(`- **Evaluation state:** ${sc.evaluation_state}`);
    L.push(`- **Domain:** ${sc.domain || "—"}`);
    L.push(`- **Required coverage:** ${reqCovPct}%`);
    L.push(`- **Total coverage:** ${totCovPct}%`);
    L.push(`- **Confidence:** ${Math.round((sc.confidence || 0) * 100)}%`);
    L.push("");
    L.push("## Provenance");
    L.push(`- Scoring version: ${runMeta.scoring_version || "—"}`);
    const corpusVer = ev.corpus_version || runMeta.corpus_version || "—";
    L.push(`- Spec source: ${corpusVer === "graph" ? "Blueprint Library (graph-live)" : `corpus v${corpusVer}`}`);
    L.push(`- Evaluator: ${runMeta.skill_version ? `skill ${runMeta.skill_version}` : "heuristic (no skill)"}`);
    L.push(`- Embeddings: ${runMeta.embedding_model || "—"}`);
    L.push(`- Catalogs assessed: ${runMeta.scan_ids?.length ?? 0}`);
    L.push(`- Run: #${runId}`);
    L.push(`- Evaluated at: ${runMeta.created_at || "—"}`);
    L.push("");
    L.push("## Rationale");
    L.push(sc.rationale ? sc.rationale.replace(/\r?\n/g, " ") : "—");
    if (sc.adaptation_notes) { L.push(""); L.push(`**Adaptation:** ${sc.adaptation_notes.replace(/\r?\n/g, " ")}`); }
    // Shortlisted schemas.
    const shortlist = Array.isArray(ev.schema_shortlist) ? ev.schema_shortlist : [];
    if (shortlist.length) {
      L.push(""); L.push("## Shortlisted schemas");
      L.push("| Schema | Score | Band | In scope | Matched tables | Rationale |");
      L.push("|---|---|---|---|---|---|");
      for (const sh of shortlist) {
        const loc = [sh.database, sh.schema].filter(Boolean).join(".") || sh.schema || "—";
        L.push(`| ${mdCell(loc)} | ${mdCell(Math.round(sh.score))} | ${mdCell(sh.band)} | ${sh.included ? "yes" : "no"} | ${mdCell((sh.matched_tables || []).join(", "))} | ${mdCell(sh.rationale || sh.excluded_reason || "")} |`);
      }
    }
    // Full attribute table.
    L.push(""); L.push(`## Attributes (${rows.length})`);
    L.push("| Spec attribute | Required | Status | → Column | Score | Table-fit | Derivation | Source |");
    L.push("|---|---|---|---|---|---|---|---|");
    for (const r of rows) {
      const tableFit = (r.matched && r.adjusted != null && r.column_semantic != null && Math.round(r.adjusted) !== Math.round(r.column_semantic)) ? String(Math.round(r.table_affinity ?? 0)) : "";
      const derivCell = r.composite
        ? `${r.derivation || r.composite.kind}${r.composite.operator ? ` (${r.composite.operator})` : ""}`
        : (r.derivation || "");
      L.push(`| ${mdCell(r.spec_attr)} | ${r.required ? "yes" : "no"} | ${mdCell(r.status)} | ${mdCell(r.matched ? r.column : "—")} | ${mdCell(r.matched ? Math.round(r.score ?? 0) : "")} | ${mdCell(tableFit)} | ${mdCell(derivCell)} | ${mdCell(r.matched ? sourceLabel(r) : (REASON_LABEL[r.reason || ""] || r.reason || ""))} |`);
    }
    // Description quality — name-only matches (rest on names alone) + failed-
    // enrichment estate schemas the run flagged for re-enrichment.
    const matchedRows = rows.filter((r) => r.matched);
    const nameOnly = matchedRows.filter((r) => r.name_only);
    const dc = runMeta.summary?.description_coverage;
    const weakSchemas: string[] = Array.isArray(dc?.weak_schemas) ? dc.weak_schemas : [];
    if (nameOnly.length || weakSchemas.length) {
      L.push(""); L.push("## Description quality");
      if (matchedRows.length) {
        L.push(`- ${nameOnly.length} of ${matchedRows.length} match(es) rest on column names alone (no description on either side) — lower confidence.`);
      }
      for (const r of nameOnly) L.push(`  - **${mdCell(r.spec_attr)}** → ${mdCell(r.column)} (name-only)`);
      if (weakSchemas.length) {
        L.push(`- Estate schemas with weak/failed descriptions (re-enrich recommended): ${mdCell(weakSchemas.join(", "))}`);
      }
    }
    // Composite derivations — operator + components (R3).
    const composites = rows.filter((r) => r.composite);
    if (composites.length) {
      L.push(""); L.push(`## Derivations (${composites.length})`);
      for (const r of composites) {
        const c = r.composite!;
        L.push(`- **${mdCell(r.spec_attr)}** = ${mdCell(c.kind)}${c.operator ? ` via ${mdCell(c.operator)}` : ""}${c.table ? ` on ${mdCell(c.table)}` : ""}: ${mdCell(compositeSummary(c))}`);
      }
    }
    // Join plan.
    const jp = ev?.raw_candidates?.join_plan;
    if (jp && !jp.single_dataset) {
      L.push(""); L.push("## Join plan");
      L.push(`- Joinable: ${jp.joinable ? "yes" : "no"}`);
      for (const p of (jp.paths || [])) L.push(`- ${mdCell(p.left)} ↔ ${mdCell(p.right)} on ${mdCell((p.on || []).join(", "))}`);
      for (const isl of (jp.missing || [])) L.push(`- Disconnected island: ${mdCell((isl || []).join(", "))}`);
    }
    // Gaps + their best alternatives.
    const gaps = rows.filter((r) => !r.matched);
    if (gaps.length) {
      L.push(""); L.push(`## Gaps (${gaps.length})`);
      for (const g of gaps) {
        const best = (alts[g.attr_id] || [])[0];
        const bestTxt = best ? ` — best near-miss: ${mdCell(best.table)}.${mdCell(best.column)} (${Math.round(best.adjusted)}, ${REASON_LABEL[best.reason] || best.reason})` : "";
        L.push(`- **${mdCell(g.spec_attr)}${g.required ? " *" : ""}** (${mdCell(REASON_LABEL[g.reason || ""] || g.reason || "")})${bestTxt}`);
      }
    }
    const blob = new Blob([L.join("\n")], { type: "text/markdown" });
    const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `feasibility-${sc.spec_id}-${ts}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  }

  const ordered = [...scores].sort((a, b) =>
    (a.domain || "").localeCompare(b.domain || "") ||
    ["ready", "adaptable", "assemblable", "absent"].indexOf(a.tier) -
      ["ready", "adaptable", "assemblable", "absent"].indexOf(b.tier));

  return (
    <div>
      {ordered.map((sc) => {
        const t = TIER[sc.tier] || TIER.absent;
        const [ebg, efg] = EVAL[sc.evaluation_state] || EVAL.completed;
        const d = detail[sc.spec_id];
        const ev = d?.evidence || {};
        const reqCovPct = Math.round(sc.required_coverage * 100);
        const totCovPct = Math.round(sc.total_coverage * 100);
        const confidencePct = Math.round(sc.confidence * 100);
        const saved = isSaved(sc.spec_id);
        const showNoteForm = noteFor === sc.spec_id;
        const alternatives = ev?.raw_candidates?.alternatives || {};
        const allRows = d ? buildAttrRows(d) : [];
        const matchedCount = allRows.filter((r) => r.matched).length;
        const gapCount = allRows.length - matchedCount;
        const requiredCount = allRows.filter((r) => r.required).length;
        const nameOnlyCount = allRows.filter((r) => r.matched && r.name_only).length;
        const activeFilter = filter[sc.spec_id] || "all";
        const q = (textFilter[sc.spec_id] || "").toLowerCase();
        const visibleRows = allRows.filter((r) => {
          if (activeFilter === "required" && !r.required) return false;
          if (activeFilter === "gaps" && r.matched) return false;
          if (activeFilter === "matched" && !r.matched) return false;
          if (q && !(`${r.spec_attr} ${r.column || ""}`.toLowerCase().includes(q))) return false;
          return true;
        });
        return (
          <div key={sc.spec_id} style={s.card}>
            <div style={s.head} onClick={() => toggle(sc.spec_id)}>
              <span style={s.chip(t.bg, t.fg)} title={t.desc}>{t.label}</span>
              <span style={s.name}>{sc.spec_name} <span style={{ color: "#94a3b8", fontWeight: 400 }}>· {sc.domain}</span></span>
              {sc.evaluation_state !== "completed" && (
                <span style={s.chip(ebg, efg)}>{sc.evaluation_state.replace(/_/g, " ")}</span>
              )}
              <span style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 2 }}>
                <span style={{ fontSize: 10, color: "#94a3b8" }}>req. coverage</span>
                <Bar value={sc.required_coverage} tier={sc.tier} />
              </span>
              <span
                style={{ fontSize: 11, color: "#64748b", width: 54, textAlign: "right" }}
                title={`Confidence score: ${confidencePct}%. Combines required-attribute coverage (${reqCovPct}%) with optional-attribute coverage (${totCovPct}%).`}
              >
                <span style={{ fontSize: 10, color: "#94a3b8", display: "block" }}>confidence</span>
                {confidencePct}%
              </span>
            </div>
            {open === sc.spec_id && (
              <div style={s.detail}>
                {/* Summary line + report download */}
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6, gap: 8, flexWrap: "wrap" }}>
                  <div style={{ fontSize: 12, color: "#475569" }}>
                    <strong>Required coverage:</strong> {reqCovPct}%
                    {d && <> · <strong>Matched:</strong> {matchedCount} · <strong>Gaps:</strong> {gapCount}</>}
                    {d && nameOnlyCount > 0 && (
                      <span title="Matches resting on column names alone (no description on either side) — weaker evidence. Enrich the estate to strengthen them." style={{ marginLeft: 6, color: "#64748b" }}>
                        · <strong>Name-only:</strong> {nameOnlyCount}
                      </span>
                    )}
                  </div>
                  {d && (
                    <button style={s.reportBtn} onClick={(e) => { e.stopPropagation(); downloadReport(sc, d); }} title="Download this feasibility verdict as a Markdown report">
                      ⤓ Report (.md)
                    </button>
                  )}
                </div>
                <div style={{ color: "#475569", marginBottom: 8 }}>{sc.rationale}</div>
                {sc.adaptation_notes && (
                  <div style={{ color: "#7c3aed", marginBottom: 8 }}><b>Adaptation:</b> {sc.adaptation_notes}</div>
                )}

                {/* Shortlisted schemas — each chip expands to its rationale + reason codes */}
                {Array.isArray(ev?.schema_shortlist) && ev.schema_shortlist.length > 0 && (
                  <div style={{ marginBottom: 10 }}>
                    <div
                      style={{ fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.3, marginBottom: 4 }}
                      title="Stage 1: how each estate schema scored for this product's subject area. Click a chip for its structured rationale. Only 'in scope' schemas contribute columns to the match below."
                    >
                      Shortlisted schemas
                    </div>
                    {ev.schema_scope_empty && (
                      <div style={{ fontSize: 12, color: "#78350f", background: "#fef3c7", border: "1px solid #fcd34d", borderRadius: 6, padding: "6px 10px", marginBottom: 6 }}>
                        No schema in the estate cleared the relevance floor for this product — lower the schema floor and re-run, or enrich the estate for better schema descriptions.
                      </div>
                    )}
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                      {ev.schema_shortlist.map((sh: any, i: number) => {
                        const key = `${sc.spec_id}::${sh.database || ""}.${sh.schema || ""}::${i}`;
                        const isExp = expandedSchema.has(key);
                        return (
                          <span
                            key={i}
                            onClick={() => toggleSchema(key)}
                            style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, padding: "3px 8px", borderRadius: 5, cursor: "pointer", border: isExp ? "1px solid #7c3aed" : "1px solid #e2e8f0", background: sh.included ? "#ecfdf5" : "#f8fafc" }}
                            title="Click for the structured rationale + reason codes"
                          >
                            <span style={{ fontFamily: "monospace", color: sh.included ? "#065f46" : "#64748b" }}>
                              {[sh.database, sh.schema].filter(Boolean).join(".") || sh.schema || "—"}
                            </span>
                            <span style={{ color: "#94a3b8" }}>{Math.round(sh.score)}</span>
                            {sh.band && <span style={{ color: "#cbd5e1" }}>{sh.band}</span>}
                            {sh.included
                              ? <span style={{ fontWeight: 700, color: "#065f46" }}>✓ in scope</span>
                              : <span style={{ color: "#cbd5e1" }}>excluded</span>}
                          </span>
                        );
                      })}
                    </div>
                    {ev.schema_shortlist.map((sh: any, i: number) => {
                      const key = `${sc.spec_id}::${sh.database || ""}.${sh.schema || ""}::${i}`;
                      if (!expandedSchema.has(key)) return null;
                      return (
                        <div key={`exp-${i}`} style={{ marginTop: 6, padding: "8px 10px", background: "#fff", border: "1px solid #e2e8f0", borderRadius: 6, fontSize: 12, color: "#475569" }}>
                          <div style={{ marginBottom: 4 }}>{sh.rationale || "—"}</div>
                          <div style={{ display: "flex", flexWrap: "wrap", gap: 10, fontSize: 11, color: "#64748b" }}>
                            <span>score <b>{Math.round(sh.score)}</b></span>
                            <span>band <b>{sh.band || "—"}</b></span>
                            <span>Δ floor <b>{sh.threshold_distance ?? "—"}</b></span>
                            {sh.matched_attr_count != null && <span>matched attrs <b>{sh.matched_attr_count}</b></span>}
                            {sh.unmatched_required_count != null && <span>required unmatched <b>{sh.unmatched_required_count}</b></span>}
                            <span>description <b>{sh.description_source || "none"}</b></span>
                            {sh.excluded_reason && <span>excluded: <b>{SCHEMA_REASON_LABEL[sh.excluded_reason] || sh.excluded_reason}</b></span>}
                            {sh.fallback && <span style={{ color: "#b45309" }}>best-available <b>low confidence</b></span>}
                          </div>
                          {Array.isArray(sh.matched_tables) && sh.matched_tables.length > 0 && (
                            <div style={{ marginTop: 4, fontFamily: "monospace", fontSize: 11, color: "#0369a1" }}>
                              tables: {sh.matched_tables.join(", ")}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}

                {/* Unified filterable attribute table (all attributes, stable ids) */}
                {d && allRows.length > 0 && (
                  <div style={{ marginBottom: 8 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", marginBottom: 6 }}>
                      {([
                        ["all", `All (${allRows.length})`],
                        ["required", `Required (${requiredCount})`],
                        ["gaps", `Gaps (${gapCount})`],
                        ["matched", `Matched (${matchedCount})`],
                      ] as [string, string][]).map(([k, label]) => (
                        <span key={k} style={s.filterChip(activeFilter === k)} onClick={() => setFilter((f) => ({ ...f, [sc.spec_id]: k }))}>{label}</span>
                      ))}
                      <input
                        style={s.textFilter}
                        placeholder="Filter attributes…"
                        value={textFilter[sc.spec_id] || ""}
                        onChange={(e) => setTextFilter((f) => ({ ...f, [sc.spec_id]: e.target.value }))}
                      />
                      <span style={{ fontSize: 11, color: "#94a3b8" }}>* = required</span>
                    </div>
                    <div style={{ maxHeight: 360, overflowY: "auto", border: "1px solid #e2e8f0", borderRadius: 6, background: "#fff" }}>
                      <table style={{ borderCollapse: "collapse", width: "100%" }}>
                        <thead>
                          <tr>
                            <th style={s.th} title="The attribute defined by the reference spec. An asterisk (*) marks required attributes.">Spec attribute</th>
                            <th style={s.th} title="Direct = matched as-is · Derivable = matched via an allowed transform · Missing = no candidate above threshold · Unknown = schema not scanned.">Status</th>
                            <th style={s.th} title="The estate column (or product column) chosen for this attribute.">→ Column</th>
                            <th style={s.th} title="Column semantic match (0–100). 'table-fit' shows the table-affinity component when the entity-aware adjusted rank diverges from the raw column score.">Score</th>
                            <th style={s.th} title="Allowed transform to satisfy the attribute (rename / cast / …). Empty = direct.">Derivation</th>
                            <th style={s.th} title="Where the chosen column lives (platform + db.schema.table), or the reason for a gap.">Source</th>
                          </tr>
                        </thead>
                        <tbody>
                          {visibleRows.map((r) => {
                            const alts = alternatives[r.attr_id] || [];
                            const isExp = expandedAttr.has(r.attr_id);
                            const expandable = alts.length > 0 || !!r.composite;
                            const tableFit = (r.matched && r.adjusted != null && r.column_semantic != null && Math.round(r.adjusted) !== Math.round(r.column_semantic));
                            return (
                              <Fragment key={r.attr_id}>
                                <tr onClick={() => expandable && toggleAttr(r.attr_id)} style={{ cursor: expandable ? "pointer" : "default" }}>
                                  <td style={s.td}>
                                    {expandable ? <span style={{ color: "#94a3b8", marginRight: 4 }}>{isExp ? "▾" : "▸"}</span> : null}
                                    {r.spec_attr}{r.required ? " *" : ""}
                                  </td>
                                  <td style={s.td}><StatusChip status={r.status} /></td>
                                  <td style={{ fontFamily: "monospace", fontSize: 11, ...s.td }}>
                                    {r.matched ? r.column : <span style={{ color: "#cbd5e1" }}>—</span>}
                                    {r.is_fk_carrier && r.matched && (
                                      <span title={`Inferred FK-like carrier pointing at the ${r.fk_target_table} table — not the authoritative source of this value.`} style={{ marginLeft: 5, fontSize: 10, fontWeight: 700, padding: "1px 5px", borderRadius: 3, background: "#fef3c7", color: "#78350f", fontFamily: "sans-serif", whiteSpace: "nowrap" }}>
                                        FK → {r.fk_target_table}
                                      </span>
                                    )}
                                    {r.matched && r.name_only && (
                                      <span title="This match rests on column names alone — neither the estate column nor the spec attribute carries a description, so the semantic signal is weak. Enrich the estate for a stronger match." style={{ marginLeft: 5, fontSize: 10, fontWeight: 700, padding: "1px 5px", borderRadius: 3, background: "#f1f5f9", color: "#475569", fontFamily: "sans-serif", whiteSpace: "nowrap" }}>
                                        name-only
                                      </span>
                                    )}
                                    {r.matched && r.matcher_pinned && (
                                      <span title="Adjudicated by the reasoning column-matcher (over the estate's generated descriptions), not the deterministic scorer alone." style={{ marginLeft: 5, fontSize: 10, fontWeight: 700, padding: "1px 5px", borderRadius: 3, background: "#ede9fe", color: "#5b21b6", fontFamily: "sans-serif", whiteSpace: "nowrap" }}>
                                        reasoned
                                      </span>
                                    )}
                                  </td>
                                  <td style={s.td} title={r.matched ? `column ${Math.round(r.column_semantic ?? r.score ?? 0)} · table-fit ${Math.round(r.table_affinity ?? 0)} · adjusted ${Math.round(r.adjusted ?? 0)}${r.is_fk_carrier ? " · FK-carrier demoted" : ""}` : undefined}>
                                    {r.matched ? Math.round(r.score ?? 0) : "—"}
                                    {tableFit && <span style={{ marginLeft: 4, fontSize: 10, color: "#0369a1" }}>table-fit {Math.round(r.table_affinity ?? 0)}</span>}
                                  </td>
                                  <td style={s.td}>{r.derivation || "—"}</td>
                                  <td style={s.td}>
                                    {r.matched ? <SourceCell m={r} /> : <span style={{ color: "#991b1b", fontSize: 11 }}>{REASON_LABEL[r.reason || ""] || r.reason || "gap"}</span>}
                                  </td>
                                </tr>
                                {isExp && expandable && (
                                  <tr>
                                    <td style={{ ...s.td, background: "#f8fafc" }} colSpan={6}>
                                      {r.composite && (
                                        <div style={{ marginBottom: 8, padding: "6px 9px", background: "#e0f2fe", border: "1px solid #bae6fd", borderRadius: 5 }}>
                                          <div style={{ fontSize: 11, fontWeight: 700, color: "#0369a1" }}>
                                            Composite derivation: {r.composite.kind}
                                            {r.composite.operator ? ` · ${r.composite.operator}` : ""}
                                            {r.composite.table ? ` on ${r.composite.table}` : ""}
                                            {r.composite.source === "advisor" ? " · advisor-proposed" : ""}
                                          </div>
                                          <div style={{ fontSize: 11, color: "#334155", marginTop: 3, fontFamily: "monospace" }}>
                                            {compositeSummary(r.composite)}
                                          </div>
                                        </div>
                                      )}
                                      {alts.length > 0 && (
                                      <div style={{ fontSize: 11, color: "#64748b", marginBottom: 3 }}>Ranked candidates for <b>{r.spec_attr}</b>:</div>
                                      )}
                                      <table style={{ borderCollapse: "collapse", width: "100%" }}>
                                        <tbody>
                                          {alts.map((a: any, ai: number) => (
                                            <tr key={ai} style={{ background: a.chosen ? "#ecfdf5" : "transparent" }}>
                                              <td style={{ fontFamily: "monospace", fontSize: 11, padding: "2px 8px", color: "#334155" }}>
                                                {[a.schema, a.table].filter(Boolean).join(".")}.{a.column}
                                                {a.is_fk_carrier && <span style={{ marginLeft: 4, fontSize: 9, color: "#92400e" }}>FK</span>}
                                              </td>
                                              <td style={{ fontSize: 11, padding: "2px 8px", color: "#64748b" }}>col {Math.round(a.column_score)} · table-fit {Math.round(a.table_affinity)} · adj {Math.round(a.adjusted)}</td>
                                              <td style={{ fontSize: 11, padding: "2px 8px", color: a.chosen ? "#065f46" : "#94a3b8", fontWeight: a.chosen ? 700 : 400 }}>
                                                {a.chosen ? "✓ chosen" : (REASON_LABEL[a.reason] || a.reason)}
                                              </td>
                                            </tr>
                                          ))}
                                        </tbody>
                                      </table>
                                    </td>
                                  </tr>
                                )}
                              </Fragment>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}

                {ev?.raw_candidates?.join_plan && !ev.raw_candidates.join_plan.single_dataset && (
                  <div style={{ fontSize: 12, color: "#475569", marginBottom: 8 }}>
                    <b>Join plan:</b> {ev.raw_candidates.join_plan.joinable ? "joinable" : "NOT joinable"} ·{" "}
                    {(ev.raw_candidates.join_plan.paths || []).map((p: any) => `${p.left}↔${p.right} on ${p.on.join(",")}`).join("; ") || "—"}
                  </div>
                )}

                <div style={{ display: "flex", alignItems: "center", gap: 0, flexWrap: "wrap" }}>
                  {(sc.tier === "ready" || sc.tier === "adaptable") && (
                    <button style={s.btn} onClick={() => act(sc.spec_id)}>
                      {sc.tier === "ready" ? "Adopt in marketplace" : "Author consumer product"}
                    </button>
                  )}
                  {/* assemblable AND absent open the Assembly workspace (absent had no
                      action before — it's the primary 'curate the template' entry). */}
                  {(sc.tier === "assemblable" || sc.tier === "absent") && (
                    <button style={s.btn} onClick={() => workThisProduct(sc.spec_id)}
                            title="Open the Product Assembly workspace: remap attributes with descriptions, cluster the source tables, then scaffold the portfolio.">
                      Work this product
                    </button>
                  )}
                  {!saved && !showNoteForm && (
                    <button style={s.saveBtn(false)} onClick={(e) => { e.stopPropagation(); startSave(sc.spec_id); }} title="Save as a candidate to work on later">
                      ★ Save
                    </button>
                  )}
                  {showNoteForm && (
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, marginTop: 8, marginLeft: 8 }}>
                      <input
                        style={s.noteInput}
                        value={noteText}
                        onChange={(e) => setNoteText(e.target.value)}
                        placeholder="Optional note…"
                        autoFocus
                        onKeyDown={(e) => { if (e.key === "Enter") confirmSave(sc); if (e.key === "Escape") setNoteFor(null); }}
                      />
                      <button style={{ ...s.saveBtn(false), marginTop: 0, background: "#7c3aed", color: "#fff" }} disabled={saving === sc.spec_id} onClick={() => confirmSave(sc)}>
                        {saving === sc.spec_id ? "Saving…" : "Save"}
                      </button>
                      <button style={{ ...s.saveBtn(false), marginTop: 0 }} onClick={() => setNoteFor(null)}>Cancel</button>
                    </span>
                  )}
                  {saved && !showNoteForm && (
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, marginTop: 8, marginLeft: 8 }}>
                      <span style={s.saveBtn(true)}>✓ Saved</span>
                      <button style={s.dismissBtn} onClick={(e) => { e.stopPropagation(); dismissSave(sc); }} title="Remove from candidates">×</button>
                    </span>
                  )}
                </div>
                {action[sc.spec_id]?.error && (
                  <div style={{ color: "#991b1b", marginTop: 6 }}>{action[sc.spec_id].error}</div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
