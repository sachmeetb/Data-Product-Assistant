// Product Assembly workspace — the "Work this product" screen over one feasibility
// score. The Attributes tab lets the PO curate the reference-spec TEMPLATE against
// their estate: accept / remap (ranked or browse-any-column) / derive (a prose hint)
// / defer / exclude, each informed by the generated column + table descriptions and a
// castable-type hint. Decisions are a thin overlay persisted to the assembly plan.
// Source clustering (Phase 2) and scaffold (Phase 3) layer onto this same screen.
import { Fragment, useEffect, useMemo, useState, type CSSProperties } from "react";
import { useParams, useNavigate } from "react-router-dom";
import api from "../../api/client";
import MarkdownMessage from "../../components/chat/MarkdownMessage";

const TIER: Record<string, { bg: string; fg: string; label: string }> = {
  ready: { bg: "#dcfce7", fg: "#166534", label: "Ready" },
  adaptable: { bg: "#dbeafe", fg: "#1e40af", label: "Adaptable" },
  assemblable: { bg: "#fef9c3", fg: "#854d0e", label: "Assemblable" },
  absent: { bg: "#e2e8f0", fg: "#475569", label: "Absent" },
};
// Effective per-attribute status → chip palette + label.
const STATUS: Record<string, [string, string, string]> = {
  matched: ["#dcfce7", "#166534", "matched"],
  derived: ["#e0f2fe", "#0369a1", "derived"],
  remapped: ["#ede9fe", "#5b21b6", "remapped"],
  missing: ["#fee2e2", "#991b1b", "missing"],
  deferred: ["#fef3c7", "#92400e", "deferred"],
  excluded: ["#e2e8f0", "#64748b", "excluded"],
};

type Alt = {
  column?: string; table?: string; schema?: string; column_score?: number; adjusted?: number;
  chosen?: boolean; reason?: string; column_description?: string; table_description?: string;
};
type Decision = {
  decision: "accept" | "remap" | "derive" | "defer" | "exclude";
  column_ref?: string; table_ref?: string; schema_ref?: string;
  prose_hint?: string; source_cols?: string[];
};
type Plan = { attributes: Record<string, Decision>; clusters: any[]; shortlist_override: any; attribute_groups?: { name: string; attribute_names: string[]; rationale?: string }[] };
type Assembly = {
  id: number; run_id: number; estate_id?: number; spec_id: string; spec_name: string;
  domain?: string | null; status: string;
  tier?: string; required_coverage?: number; evidence?: any; plan?: Plan; plan_revision?: number;
  report_generated_at?: string | null;
};
type ScopeCol = { schema: string; table: string; column: string; type: string; description: string };

const s: Record<string, any> = {
  wrap: { maxWidth: 1120, margin: "0 auto", padding: "18px 20px" },
  chip: (bg: string, fg: string): CSSProperties => ({ fontSize: 11, fontWeight: 700, padding: "2px 9px", borderRadius: 5, background: bg, color: fg, whiteSpace: "nowrap" }),
  th: { textAlign: "left", fontSize: 11, color: "#64748b", fontWeight: 700, padding: "6px 8px", background: "#f1f5f9" },
  td: { fontSize: 12, color: "#334155", padding: "5px 8px", borderTop: "1px solid #eef2f7", verticalAlign: "top" },
  tab: (a: boolean): CSSProperties => ({ fontSize: 13, fontWeight: 700, padding: "7px 14px", borderRadius: "8px 8px 0 0", cursor: "pointer", border: "1px solid #e2e8f0", borderBottom: a ? "1px solid #fff" : "1px solid #e2e8f0", background: a ? "#fff" : "#f8fafc", color: a ? "#5b21b6" : "#64748b", marginRight: 4 }),
  act: { fontSize: 11, fontWeight: 600, padding: "2px 7px", borderRadius: 5, border: "1px solid #e2e8f0", background: "#fff", color: "#475569", cursor: "pointer", marginRight: 4 },
  save: (dirty: boolean): CSSProperties => ({ fontSize: 12, fontWeight: 700, padding: "6px 14px", borderRadius: 6, border: "none", background: dirty ? "#7c3aed" : "#e2e8f0", color: dirty ? "#fff" : "#94a3b8", cursor: dirty ? "pointer" : "default" }),
  modal: { position: "fixed", inset: 0, background: "rgba(15,23,42,0.35)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50 } as CSSProperties,
  modalBox: { background: "#fff", borderRadius: 10, padding: 16, width: 560, maxHeight: "70vh", overflow: "auto", boxShadow: "0 10px 40px rgba(0,0,0,0.2)" } as CSSProperties,
};

function attrIndex(id: string): number { const m = /^#(\d+):/.exec(id || ""); return m ? Number(m[1]) : 1e9; }

export default function AssemblyPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [a, setA] = useState<Assembly | null>(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState<"attributes" | "sources" | "gaps">("attributes");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [decisions, setDecisions] = useState<Record<string, Decision>>({});
  const [revision, setRevision] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deriveFor, setDeriveFor] = useState<string | null>(null);
  const [deriveHint, setDeriveHint] = useState("");
  const [deriveCols, setDeriveCols] = useState("");
  const [browseFor, setBrowseFor] = useState<string | null>(null);
  const [scopeCols, setScopeCols] = useState<ScopeCol[]>([]);
  const [browseQ, setBrowseQ] = useState("");
  // Sources tab (clusters + schema-scope override).
  type Cluster = { cluster_id: string; name: string; table_refs: string[]; rationale?: string };
  const [clusters, setClusters] = useState<Cluster[]>([]);
  const [autoClusters, setAutoClusters] = useState<Cluster[]>([]);
  const [shortlist, setShortlist] = useState<any[]>([]);
  const [tableMeta, setTableMeta] = useState<Record<string, { schemas: string[]; description: string; ambiguous: boolean }>>({});
  const [clustersLoaded, setClustersLoaded] = useState(false);
  const [inc, setInc] = useState<Set<string>>(new Set());
  const [exc, setExc] = useState<Set<string>>(new Set());
  const [rescoring, setRescoring] = useState(false);
  const [refiningAi, setRefiningAi] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [confirmScaffold, setConfirmScaffold] = useState(false);
  const [report, setReport] = useState<{ markdown: string; generated_at?: string | null } | null>(null);
  const [reportBusy, setReportBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [tableDetail, setTableDetail] = useState<any | null>(null);
  const [attrGroups, setAttrGroups] = useState<{ name: string; attribute_names: string[]; rationale?: string }[]>([]);
  const [groupingAi, setGroupingAi] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  // The grouping is a slow per-spec LLM call computed in the background + cached;
  // kick it off and poll until ready. `silent` (mount) doesn't show errors/spinner.
  async function groupByTheme(silent = false) {
    if (!a) return;
    if (!silent) { setGroupingAi(true); setErr(""); }
    const started = Date.now();
    const poll = async (force = false): Promise<void> => {
      const r = await api.get(`/api/assembly/${a.id}/attribute-groups`, { params: force ? { force: true } : {} });
      const st = r.data.status;
      if (st === "ready") { setAttrGroups(r.data.groups || []); if (!silent) setDirty(true); setGroupingAi(false); return; }
      if (st === "failed") { if (!silent) setErr("Theme grouping failed — try again."); setGroupingAi(false); return; }
      if (st === "unavailable") { setGroupingAi(false); return; }
      if (silent) { setGroupingAi(false); return; }  // computing on mount: don't block; user can click
      if (Date.now() - started > 200000) { setErr("Grouping is taking too long — try again later."); setGroupingAi(false); return; }
      setTimeout(() => { poll().catch(() => { setErr("Theme grouping failed."); setGroupingAi(false); }); }, 6000);
    };
    try { await poll(); } catch { if (!silent) { setErr("Theme grouping failed."); setGroupingAi(false); } }
  }

  async function openTableDetail(tbl: string) {
    if (!a) return;
    const m = tableMeta[tbl];
    setTableDetail({ table: tbl, loading: true });
    try {
      const r = await api.get(`/api/assembly/${a.id}/table`, {
        params: { table: tbl, ...(m?.schemas?.length ? { schema: m.schemas[0] } : {}) },
      });
      setTableDetail(r.data);
    } catch { setTableDetail({ table: tbl, error: true }); }
  }

  // On open, silently pick up an already-cached theme grouping (and warm the cache).
  useEffect(() => { if (a?.id) groupByTheme(true); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [a?.id]);

  // Preload clusters when EITHER the Sources OR the Gaps tab first opens, so the
  // readiness cluster count, the report, and the confirm dialog show real numbers even
  // if the PO never opened Sources.
  useEffect(() => {
    if ((tab !== "sources" && tab !== "gaps") || !a || clustersLoaded) return;
    api.get(`/api/assembly/${a.id}/clusters`).then((r) => {
      setAutoClusters(r.data.auto || []);
      setClusters((r.data.user && r.data.user.length ? r.data.user : r.data.auto) || []);
      setShortlist(r.data.schema_shortlist || []);
      setTableMeta(r.data.table_meta || {});
      setClustersLoaded(true);
    }).catch(() => setClustersLoaded(true));
  }, [tab, a, clustersLoaded]);

  function editClusters(next: Cluster[]) { setClusters(next); setDirty(true); }
  function moveTable(table: string, toCid: string) {
    editClusters(clusters.map((c) => ({
      ...c,
      table_refs: c.cluster_id === toCid
        ? Array.from(new Set([...c.table_refs, table]))
        : c.table_refs.filter((t) => t !== table),
    })));
  }
  function renameCluster(cid: string, name: string) { editClusters(clusters.map((c) => c.cluster_id === cid ? { ...c, name } : c)); }
  function createCluster() { editClusters([...clusters, { cluster_id: `u${Date.now()}`, name: "New source product", table_refs: [] }]); }
  function deleteCluster(cid: string) { editClusters(clusters.filter((c) => c.cluster_id !== cid)); }
  function resetClusters() { editClusters(autoClusters.map((c) => ({ ...c }))); }
  async function refineClusters() {
    if (!a) return;
    setRefiningAi(true);
    try {
      const r = await api.get(`/api/assembly/${a.id}/clusters`, { params: { refine: true } });
      if (r.data.table_meta) setTableMeta(r.data.table_meta);
      if (r.data.refined_by_ai) { setAutoClusters(r.data.auto || []); editClusters(r.data.auto || []); }
      else setErr("AI refinement unavailable or produced no valid change.");
    } catch { setErr("AI refinement failed."); }
    setRefiningAi(false);
  }

  const skey = (db: string, sch: string) => `${db} ${sch}`;
  function desiredIncluded(row: any): boolean {
    const k = skey(row.database || "", row.schema || "");
    if (exc.has(k)) return false; if (inc.has(k)) return true; return !!row.included;
  }
  function toggleSchema(row: any) {
    const k = skey(row.database || "", row.schema || "");
    if (desiredIncluded(row)) { setExc((p) => new Set(p).add(k)); setInc((p) => { const n = new Set(p); n.delete(k); return n; }); }
    else { setInc((p) => new Set(p).add(k)); setExc((p) => { const n = new Set(p); n.delete(k); return n; }); }
    setDirty(true);
  }
  async function rescore() {
    if (!a) return;
    setRescoring(true);
    const toPair = (k: string) => k.split(" ");
    try {
      await api.post(`/api/feasibility/runs/${a.run_id}/scores/${a.spec_id}/reevaluate`, {
        schema_include: [...inc].map(toPair), schema_exclude: [...exc].map(toPair),
      });
      // Reload the score evidence + clusters after the re-score. KEEP the accumulated
      // override (inc/exc) so previously-excluded schemas don't come back on the next
      // re-score; they persist to the plan on Save.
      const r = await api.get(`/api/assembly/${a.id}`);
      setA(r.data); setClustersLoaded(false);
    } catch { setErr("Re-score failed."); }
    setRescoring(false);
  }

  useEffect(() => {
    if (!id) return;
    api.get(`/api/assembly/${id}`).then((r) => {
      setA(r.data);
      setDecisions((r.data.plan?.attributes as Record<string, Decision>) || {});
      setRevision(r.data.plan_revision || 0);
      // Seed the schema-scope override from the saved plan so exclusions persist
      // across reloads and accumulate across re-scores.
      const so = r.data.plan?.shortlist_override || {};
      setInc(new Set((so.include || []).map((k: string[]) => `${k[0]} ${k[1]}`)));
      setExc(new Set((so.exclude || []).map((k: string[]) => `${k[0]} ${k[1]}`)));
      setAttrGroups(r.data.plan?.attribute_groups || []);
    }).catch((e) => setErr(e?.response?.data?.detail || "Could not load the assembly."));
  }, [id]);

  const raw = a?.evidence?.raw_candidates || {};
  const alternatives: Record<string, Alt[]> = raw.alternatives || {};
  const rows = useMemo(() => {
    const out: any[] = [];
    for (const m of (raw.assignment || [])) out.push({ ...m, matched: true });
    for (const g of (raw.gaps || [])) out.push({ ...g, matched: false, status: g.status || "missing" });
    out.sort((x, y) => attrIndex(x.attr_id) - attrIndex(y.attr_id));
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [a]);

  function setDecision(attrId: string, d: Decision | null) {
    setDecisions((prev) => {
      const n = { ...prev };
      if (!d || d.decision === "accept") delete n[attrId]; else n[attrId] = d;
      return n;
    });
    setDirty(true);
  }
  function toggle(attrId: string) { setExpanded((p) => { const n = new Set(p); if (n.has(attrId)) n.delete(attrId); else n.add(attrId); return n; }); }
  function toggleSel(attrId: string) { setSelected((p) => { const n = new Set(p); if (n.has(attrId)) n.delete(attrId); else n.add(attrId); return n; }); }
  function bulkDecision(dec: "exclude" | "defer" | "accept") {
    setDecisions((prev) => {
      const n = { ...prev };
      for (const id of selected) { if (dec === "accept") delete n[id]; else n[id] = { decision: dec }; }
      return n;
    });
    setDirty(true); setSelected(new Set());
  }

  async function save() {
    if (!a || !dirty) return;
    setSaving(true);
    try {
      const r = await api.patch(`/api/assembly/${a.id}/plan`, {
        expected_revision: revision,
        plan: {
          attributes: decisions,
          clusters: clustersLoaded ? clusters : (a.plan?.clusters || []),
          shortlist_override: { include: [...inc].map((k) => k.split(" ")), exclude: [...exc].map((k) => k.split(" ")) },
          attribute_groups: attrGroups,
        },
      });
      setRevision(r.data.plan_revision);
      setDirty(false);
    } catch (e: any) {
      if (e?.response?.status === 409) setErr("This workspace changed elsewhere — reload to get the latest.");
      else setErr("Could not save changes.");
    }
    setSaving(false);
  }

  async function openBrowse(attrId: string) {
    setBrowseFor(attrId); setBrowseQ(""); setScopeCols([]);
    try { const r = await api.get(`/api/assembly/${a!.id}/columns`); setScopeCols(r.data.columns || []); } catch { /* best-effort */ }
  }
  function chooseBrowse(attrId: string, c: ScopeCol) {
    setDecision(attrId, { decision: "remap", column_ref: c.column, table_ref: c.table, schema_ref: c.schema });
    setBrowseFor(null);
  }
  function startDerive(attrId: string) {
    const d = decisions[attrId];
    setDeriveFor(attrId);
    setDeriveHint(d?.decision === "derive" ? (d.prose_hint || "") : "");
    setDeriveCols(d?.decision === "derive" ? (d.source_cols || []).join(", ") : "");
  }
  function saveDerive(attrId: string) {
    setDecision(attrId, { decision: "derive", prose_hint: deriveHint.trim(),
      source_cols: deriveCols.split(",").map((x) => x.trim()).filter(Boolean) });
    setDeriveFor(null);
  }

  // The deterministic functional report (markdown + mermaid), built + cached server-side.
  async function loadReport(regenerate: boolean) {
    if (!a) return;
    setReportBusy(true); setErr("");
    try {
      const r = await api.get(`/api/assembly/${a.id}/report`, { params: regenerate ? { regenerate: true } : {} });
      setReport({ markdown: r.data.markdown, generated_at: r.data.generated_at });
    } catch { setErr("Could not generate the functional report."); }
    setReportBusy(false);
  }
  // A previously-generated report re-appears on reopen (without regenerating it).
  useEffect(() => {
    if (a?.id && a.report_generated_at && !report && !reportBusy) loadReport(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [a?.id]);
  function copyReport() {
    if (!report) return;
    navigator.clipboard.writeText(report.markdown).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1800); }).catch(() => {});
  }
  function downloadReport() {
    if (!report || !a) return;
    const blob = new Blob([report.markdown], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const el = document.createElement("a");
    el.href = url; el.download = `product-assembly-${a.spec_id}-report.md`;
    document.body.appendChild(el); el.click(); document.body.removeChild(el);
    URL.revokeObjectURL(url);
  }

  async function commitPortfolio() {
    if (!a) return;
    if (dirty) { setErr("Save your changes before scaffolding."); return; }
    setCommitting(true);
    try {
      const r = await api.post(`/api/intake/from-assembly`, { assembly_id: a.id });
      if (r.data.review_path) window.location.assign(r.data.review_path);
    } catch (e: any) { setErr(e?.response?.data?.detail || "Could not stage the portfolio."); setConfirmScaffold(false); }
    setCommitting(false);
  }

  // Effective view of one attribute after its decision overlay.
  function eff(row: any): { status: string; label: string; mapped?: string } {
    const d = decisions[row.attr_id];
    if (d?.decision === "exclude") return { status: "excluded", label: "excluded" };
    if (d?.decision === "defer") return { status: "deferred", label: "deferred" };
    if (d?.decision === "derive") return { status: "derived", label: `derive: ${(d.source_cols || []).join(" + ") || "hint"}` };
    if (d?.decision === "remap") return { status: "remapped", label: `${d.table_ref || ""}.${d.column_ref}` };
    if (row.matched) return { status: row.match_kind === "composite" ? "derived" : "matched", label: `${row.dataset_table || ""}.${row.column}` };
    return { status: "missing", label: "—" };
  }

  if (err && !a) return <div style={s.wrap}><p style={{ color: "#991b1b" }}>{err}</p></div>;
  if (!a) return <div style={s.wrap}><p style={{ color: "#64748b" }}>Loading workspace…</p></div>;

  const t = TIER[a.tier || "absent"] || TIER.absent;
  const tally = rows.reduce((acc: Record<string, number>, r) => { const st = eff(r).status; acc[st] = (acc[st] || 0) + 1; return acc; }, {});

  // Grouped ordering for the Attributes tab (when AI themes are applied).
  const firstOfGroup = new Map<string, string>();   // attr_id -> group name (marks a header)
  const rowGroup = new Map<string, string>();        // attr_id -> group name
  let orderedRows = rows;
  if (attrGroups.length) {
    orderedRows = [];
    const placed = new Set<string>();
    for (const g of attrGroups) {
      const grows = rows.filter((r) => g.attribute_names.includes(r.spec_attr) && !placed.has(r.attr_id));
      grows.forEach((r, idx) => { rowGroup.set(r.attr_id, g.name); placed.add(r.attr_id); if (idx === 0) firstOfGroup.set(r.attr_id, g.name); });
      orderedRows.push(...grows);
    }
    const leftover = rows.filter((r) => !placed.has(r.attr_id));
    leftover.forEach((r, idx) => { rowGroup.set(r.attr_id, "Other"); if (idx === 0) firstOfGroup.set(r.attr_id, "Other"); });
    orderedRows.push(...leftover);
  }

  return (
    <div style={s.wrap}>
      <button onClick={() => navigate(-1)} style={{ fontSize: 12, color: "#64748b", background: "none", border: "none", cursor: "pointer", padding: 0, marginBottom: 8 }}>← Back</button>
      <div style={{ fontSize: 11, fontWeight: 700, color: "#7c3aed", letterSpacing: 0.6, textTransform: "uppercase" }}>Product Assembly</div>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 4 }}>
        <h2 style={{ margin: 0, fontSize: 26, color: "#0f172a" }}>{a.spec_name}</h2>
        <span style={s.chip(t.bg, t.fg)}>{t.label}</span>
        <span style={{ fontSize: 13, color: "#64748b" }}>{a.domain || "—"}</span>
        <span style={{ flex: 1 }} />
        {err && <span style={{ fontSize: 12, color: "#991b1b" }}>{err}</span>}
        <button style={s.save(dirty && !saving)} disabled={!dirty || saving} onClick={save}>{saving ? "Saving…" : dirty ? "Save changes" : "Saved"}</button>
      </div>
      <p style={{ fontSize: 12, color: "#64748b", marginTop: 0 }}>
        The reference spec is a <b>template</b> — curate it to what your estate provides. Matched {tally.matched || 0} ·
        remapped {tally.remapped || 0} · derived {tally.derived || 0} · deferred {tally.deferred || 0} · excluded {tally.excluded || 0} · missing {tally.missing || 0}.
      </p>

      <div style={{ display: "flex", borderBottom: "1px solid #e2e8f0", marginTop: 10 }}>
        <span style={s.tab(tab === "attributes")} onClick={() => setTab("attributes")}>Attributes</span>
        <span style={s.tab(tab === "sources")} onClick={() => setTab("sources")}>Sources</span>
        <span style={s.tab(tab === "gaps")} onClick={() => setTab("gaps")}>Gaps &amp; readiness</span>
      </div>

      {tab === "attributes" && (
        <div style={{ border: "1px solid #e2e8f0", borderTop: "none", borderRadius: "0 0 8px 8px", overflow: "hidden" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, padding: "6px 10px", background: "#f8fafc", borderBottom: "1px solid #e2e8f0", fontSize: 12, flexWrap: "wrap" }}>
            <span style={{ color: "#64748b" }}>Select:</span>
            <button style={s.act} onClick={() => setSelected(new Set(rows.map((r) => r.attr_id)))}>All</button>
            <button style={s.act} onClick={() => setSelected(new Set(rows.filter((r) => eff(r).status === "missing").map((r) => r.attr_id)))}>Missing</button>
            <button style={s.act} onClick={() => setSelected(new Set(rows.filter((r) => r.required).map((r) => r.attr_id)))}>Required</button>
            <button style={s.act} onClick={() => setSelected(new Set())}>None</button>
            {selected.size > 0 && (
              <>
                <span style={{ color: "#5b21b6", fontWeight: 700, marginLeft: 8 }}>{selected.size} selected →</span>
                <button style={{ ...s.act, background: "#e2e8f0" }} onClick={() => bulkDecision("exclude")}>Exclude</button>
                <button style={{ ...s.act, background: "#fef3c7" }} onClick={() => bulkDecision("defer")}>Defer</button>
                <button style={s.act} onClick={() => bulkDecision("accept")}>Reset to auto</button>
              </>
            )}
            <span style={{ flex: 1 }} />
            {attrGroups.length > 0 && <button style={s.act} onClick={() => { setAttrGroups([]); setDirty(true); }}>Ungroup</button>}
            <button style={{ ...s.act, background: "#ede9fe", color: "#5b21b6", border: "1px solid #ddd6fe" }} disabled={groupingAi} onClick={() => groupByTheme()} title="Group the attributes into themes (Identity, Contact, Marketing…) so this long list is easier to scan and act on.">{groupingAi ? "Grouping…" : "✨ Auto group"}</button>
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr>
              <th style={s.th}></th>
              <th style={s.th}>Attribute</th><th style={s.th}>Status</th><th style={s.th}>→ Mapped to</th>
              <th style={s.th}>Description</th><th style={s.th}>Actions</th>
            </tr></thead>
            <tbody>
              {orderedRows.map((r) => {
                const gName = firstOfGroup.get(r.attr_id);
                const groupOfRow = rowGroup.get(r.attr_id);
                const isCollapsed = groupOfRow ? collapsed.has(groupOfRow) : false;
                const groupHeader = gName ? (() => {
                  const grp = attrGroups.find((g) => g.name === gName);
                  const gRows = orderedRows.filter((x) => rowGroup.get(x.attr_id) === gName);
                  const gMatched = gRows.filter((x) => ["matched", "remapped", "derived"].includes(eff(x).status)).length;
                  const gAttrIds = gRows.map((x) => x.attr_id);
                  const isC = collapsed.has(gName);
                  return (
                    <tr key={`h-${gName}`} style={{ background: "#f5f3ff" }}>
                      <td style={s.td}><input type="checkbox" checked={gAttrIds.every((id) => selected.has(id))} onChange={(ev) => setSelected((p) => { const n = new Set(p); if (ev.target.checked) gAttrIds.forEach((id) => n.add(id)); else gAttrIds.forEach((id) => n.delete(id)); return n; })} title="Select this theme" /></td>
                      <td colSpan={5} style={{ ...s.td, fontWeight: 700, color: "#5b21b6" }}>
                        <span onClick={() => setCollapsed((p) => { const n = new Set(p); if (n.has(gName)) n.delete(gName); else n.add(gName); return n; })} style={{ cursor: "pointer" }}>{isC ? "▸" : "▾"} {gName}</span>
                        <span style={{ fontWeight: 400, color: "#64748b", marginLeft: 8, fontSize: 11 }}>{gRows.length} attrs · {gMatched} aligned{grp?.rationale ? ` · ${grp.rationale}` : ""}</span>
                      </td>
                    </tr>
                  );
                })() : null;
                if (isCollapsed) return <Fragment key={r.attr_id}>{groupHeader}</Fragment>;
                const alts = alternatives[r.attr_id] || [];
                const chosen = alts.find((x) => x.chosen) || (r.matched ? alts[0] : undefined);
                const e = eff(r);
                const [bg, fg, lbl] = STATUS[e.status] || STATUS.missing;
                const isExp = expanded.has(r.attr_id);
                const hasOverride = !!decisions[r.attr_id];
                return (
                  <Fragment key={r.attr_id}>
                    {groupHeader}
                    <tr style={{ background: selected.has(r.attr_id) ? "#f5f3ff" : undefined }}>
                      <td style={{ ...s.td, width: 24 }}><input type="checkbox" checked={selected.has(r.attr_id)} onChange={() => toggleSel(r.attr_id)} /></td>
                      <td style={s.td}>
                        {(alts.length > 0) ? <span onClick={() => toggle(r.attr_id)} style={{ color: "#94a3b8", marginRight: 4, cursor: "pointer" }}>{isExp ? "▾" : "▸"}</span> : null}
                        {r.spec_attr}{r.required ? " ★" : ""}
                      </td>
                      <td style={s.td}><span style={s.chip(bg, fg)}>{lbl}</span></td>
                      <td style={{ ...s.td, fontFamily: "monospace", fontSize: 11 }}>
                        {e.mapped || (e.status === "excluded" || e.status === "deferred" || e.status === "missing" ? <span style={{ color: "#cbd5e1" }}>{e.label}</span> : e.label)}
                        {r.matched && r.name_only && <span title="Rests on names alone." style={{ ...s.chip("#f1f5f9", "#475569"), marginLeft: 5, fontFamily: "sans-serif" }}>name-only</span>}
                        {r.cast_hint?.needed && <span title={`Needs a ${r.cast_hint.cost} cast: ${r.cast_hint.from} → ${r.cast_hint.to}`} style={{ ...s.chip("#fef3c7", "#78350f"), marginLeft: 5, fontFamily: "sans-serif" }}>cast</span>}
                      </td>
                      <td style={{ ...s.td, color: "#64748b", maxWidth: 320 }}>{chosen?.column_description || <span style={{ color: "#cbd5e1" }}>—</span>}</td>
                      <td style={s.td}>
                        {hasOverride && <button style={s.act} onClick={() => setDecision(r.attr_id, null)} title="Revert to the deterministic pick">Accept</button>}
                        <button style={s.act} onClick={() => openBrowse(r.attr_id)}>Map…</button>
                        <button style={s.act} onClick={() => startDerive(r.attr_id)}>Derive</button>
                        <button style={s.act} onClick={() => setDecision(r.attr_id, { decision: "defer" })}>Defer</button>
                        <button style={s.act} onClick={() => setDecision(r.attr_id, { decision: "exclude" })}>Exclude</button>
                      </td>
                    </tr>
                    {deriveFor === r.attr_id && (
                      <tr><td colSpan={6} style={{ ...s.td, background: "#f0f9ff" }}>
                        <div style={{ fontSize: 11, color: "#0369a1", marginBottom: 4 }}>Derived from other columns — describe the intent for the engineer (no SQL needed):</div>
                        <input placeholder="source columns, comma-separated (e.g. first_name, last_name)" value={deriveCols} onChange={(ev) => setDeriveCols(ev.target.value)} style={{ width: "60%", fontSize: 12, padding: "4px 8px", border: "1px solid #cbd5e1", borderRadius: 5, marginRight: 6 }} />
                        <br />
                        <textarea placeholder="intent hint, e.g. full name = first_name + ' ' + last_name" value={deriveHint} onChange={(ev) => setDeriveHint(ev.target.value)} style={{ width: "80%", fontSize: 12, padding: "4px 8px", border: "1px solid #cbd5e1", borderRadius: 5, marginTop: 6, height: 44 }} />
                        <div style={{ marginTop: 6 }}>
                          <button style={{ ...s.act, background: "#7c3aed", color: "#fff", border: "none" }} onClick={() => saveDerive(r.attr_id)}>Set derived</button>
                          <button style={s.act} onClick={() => setDeriveFor(null)}>Cancel</button>
                        </div>
                      </td></tr>
                    )}
                    {isExp && alts.length > 0 && (
                      <tr><td colSpan={6} style={{ ...s.td, background: "#f8fafc" }}>
                        <div style={{ fontSize: 11, color: "#64748b", marginBottom: 4 }}>Candidates — remap by picking one (descriptions guide the choice):</div>
                        {alts.map((alt, k) => (
                          <div key={k} style={{ padding: "4px 6px", borderTop: k ? "1px solid #eef2f7" : "none", display: "flex", gap: 8, alignItems: "baseline" }}>
                            <button style={s.act} onClick={() => setDecision(r.attr_id, { decision: "remap", column_ref: alt.column, table_ref: alt.table, schema_ref: alt.schema })}>Use</button>
                            <span style={{ fontFamily: "monospace", fontSize: 11, color: alt.chosen ? "#166534" : "#334155" }}>{alt.chosen ? "✓ " : ""}{[alt.schema, alt.table].filter(Boolean).join(".")}.{alt.column}</span>
                            <span style={{ fontSize: 11, color: "#94a3b8" }}>adj {Math.round(alt.adjusted || 0)} · {alt.reason}</span>
                            {alt.column_description && <span style={{ fontSize: 11, color: "#475569" }}>— {alt.column_description}</span>}
                          </div>
                        ))}
                      </td></tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {tab === "sources" && (
        <div style={{ border: "1px solid #e2e8f0", borderTop: "none", borderRadius: "0 0 8px 8px", padding: 14 }}>
          {!clustersLoaded && <p style={{ fontSize: 12, color: "#94a3b8" }}>Detecting source clusters…</p>}
          {clustersLoaded && (
            <>
              <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 6 }}>
                <b style={{ fontSize: 13, color: "#0f172a" }}>Schema scope</b>
                <span style={{ fontSize: 11, color: "#64748b", flex: 1 }}>This drives everything below. Toggling a schema off and re-scoring removes its tables from the candidate pool — the attribute matches (tier/coverage) AND the clusters recompute. Use it to drop a duplicate schema (⚠ overlaps) so a table isn't sourced from two places.</span>
                <button style={{ ...s.act, background: (inc.size || exc.size) ? "#7c3aed" : "#e2e8f0", color: (inc.size || exc.size) ? "#fff" : "#94a3b8", border: "none" }} disabled={!(inc.size || exc.size) || rescoring} onClick={rescore}>{rescoring ? "Re-scoring…" : "Re-score"}</button>
              </div>
              <table style={{ width: "100%", borderCollapse: "collapse", marginBottom: 18 }}>
                <thead><tr><th style={s.th}>In scope</th><th style={s.th}>Schema</th><th style={s.th}>Score</th><th style={s.th}>Band</th><th style={s.th}>Why</th></tr></thead>
                <tbody>
                  {shortlist.map((r, k) => (
                    <tr key={k}>
                      <td style={s.td}><input type="checkbox" checked={desiredIncluded(r)} onChange={() => toggleSchema(r)} /></td>
                      <td style={{ ...s.td, fontFamily: "monospace", fontSize: 11 }}>{[r.database, r.schema].filter(Boolean).join(".")}</td>
                      <td style={s.td}>{Math.round(r.score || 0)}</td>
                      <td style={s.td}>{r.band}</td>
                      <td style={{ ...s.td, color: "#94a3b8" }}>
                        {r.included ? (r.override === "user_included" ? "you added" : "shortlisted") : (r.excluded_reason || "")}
                        {Array.isArray(r.overlaps) && r.overlaps.length > 0 && (
                          <span title={`Shares table names with: ${r.overlaps.map((o: any) => `${o.schema} (${o.shared})`).join(", ")}. Likely a duplicate schema — keep only one.`} style={{ color: "#b45309", marginLeft: 6, fontWeight: 600 }}>⚠ overlaps {r.overlaps.map((o: any) => o.schema).join(", ")}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                  {shortlist.length === 0 && <tr><td colSpan={5} style={{ ...s.td, color: "#94a3b8" }}>Whole-estate matching (no schema shortlist) or estate unreachable.</td></tr>}
                </tbody>
              </table>
              <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginBottom: 6 }}>
                <b style={{ fontSize: 13, color: "#0f172a" }}>Source products (clusters)</b>
                <span style={{ fontSize: 11, color: "#64748b", flex: 1 }}>Each cluster becomes a source-aligned product to build first. Reassign a table, rename, or split/merge.</span>
                <button style={{ ...s.act, background: "#ede9fe", color: "#5b21b6", border: "1px solid #ddd6fe" }} disabled={refiningAi} onClick={refineClusters} title="Reason over the tables' descriptions to refine the clusters (helps when column names are per-table-prefixed).">{refiningAi ? "Refining…" : "✨ Refine with AI"}</button>
                <button style={s.act} onClick={createCluster}>+ Cluster</button>
                <button style={s.act} onClick={resetClusters} title="Reset to the auto-detected seams">Reset</button>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 8 }}>
                {clusters.map((c) => (
                  <div key={c.cluster_id} style={{ border: "1px solid #e2e8f0", borderRadius: 8, padding: 8, background: "#fbfcff" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <input value={c.name} onChange={(e) => renameCluster(c.cluster_id, e.target.value)} style={{ fontSize: 13, fontWeight: 700, color: "#5b21b6", border: "1px solid transparent", background: "none", flex: 1 }} />
                      {c.table_refs.length === 0 && <button style={s.act} onClick={() => deleteCluster(c.cluster_id)}>✕</button>}
                    </div>
                    {c.rationale && <div style={{ fontSize: 10, color: "#94a3b8", marginBottom: 4 }}>{c.rationale}</div>}
                    {c.table_refs.length === 0 && <div style={{ fontSize: 11, color: "#cbd5e1" }}>empty — move a table here</div>}
                    {c.table_refs.map((tbl) => {
                      const m = tableMeta[tbl];
                      return (
                        <div key={tbl} style={{ display: "flex", alignItems: "flex-start", gap: 6, padding: "3px 0", borderTop: "1px solid #f1f5f9" }}>
                          <span style={{ flex: 1 }}>
                            <span onClick={() => openTableDetail(tbl)} title="View table details, columns, and relationships" style={{ fontFamily: "monospace", fontSize: 11, color: "#5b21b6", cursor: "pointer", textDecoration: "underline dotted" }}>{tbl}</span>
                            {m && m.schemas.length > 0 && (m.ambiguous
                              ? <span title={`Exists in ${m.schemas.length} in-scope schemas (${m.schemas.join(", ")}) — likely DUPLICATE schemas (e.g. tpcds_sf1 vs tpcds_sf1000). Drop all but one in Schema scope above.`} style={{ ...s.chip("#fef3c7", "#92400e"), marginLeft: 6, fontFamily: "sans-serif" }}>⚠ {m.schemas.length} schemas</span>
                              : <span style={{ fontSize: 10, color: "#94a3b8", marginLeft: 6 }}>{m.schemas[0]}</span>)}
                            {m?.description && <div style={{ fontSize: 10, color: "#94a3b8", lineHeight: 1.3 }}>{m.description}</div>}
                          </span>
                          <select value={c.cluster_id} onChange={(e) => moveTable(tbl, e.target.value)} style={{ fontSize: 10, color: "#64748b", border: "1px solid #e2e8f0", borderRadius: 4 }}>
                            {clusters.map((o) => <option key={o.cluster_id} value={o.cluster_id}>{o.name}</option>)}
                          </select>
                        </div>
                      );
                    })}
                  </div>
                ))}
              </div>

            </>
          )}
        </div>
      )}
      {tab === "gaps" && (() => {
        const grainKeys: string[] = a.evidence?.grain_keys || [];
        const mappedNames = new Set(rows.filter((r) => ["matched", "remapped"].includes(eff(r).status)).map((r) => r.spec_attr));
        const unmetGrain = grainKeys.filter((k) => !mappedNames.has(k));
        const clusterCount = (clustersLoaded ? clusters : []).filter((c) => c.table_refs.length > 0).length;
        return (
          <div style={{ border: "1px solid #e2e8f0", borderTop: "none", borderRadius: "0 0 8px 8px", padding: 16 }}>
            <b style={{ fontSize: 13, color: "#0f172a" }}>Readiness &amp; scaffold</b>
            <p style={{ fontSize: 12, color: "#64748b", marginTop: 4 }}>
              Scaffolding stages a structured intake submission: it creates the <b>{clusterCount || "…"}</b> source-aligned
              product(s) (built first, pre-filled with the tables + columns this aggregate needs) and a pre-filled
              <b> {a.spec_name}</b> aggregate draft. You confirm it in the intake queue, then Approve to create the projects.
            </p>
            <ul style={{ fontSize: 12, color: "#334155", lineHeight: 1.7 }}>
              <li><b>{tally.matched || 0}</b> matched · <b>{tally.remapped || 0}</b> remapped · <b>{tally.derived || 0}</b> derived (hint) · <b>{tally.deferred || 0}</b> deferred · <b>{tally.excluded || 0}</b> excluded · <b>{tally.missing || 0}</b> still missing.</li>
              <li>Source products to build first: <b>{clusterCount}</b> (see the Sources tab).</li>
              {grainKeys.length > 0 && (unmetGrain.length === 0
                ? <li style={{ color: "#166534" }}>✓ Grain key(s) mapped: {grainKeys.join(", ")}.</li>
                : <li style={{ color: "#b45309" }}>⚠ Grain key(s) not directly mapped: <b>{unmetGrain.join(", ")}</b> — the aggregate can't identify its rows until these map to a real column. Map them in Attributes, or proceed and fix during engineering.</li>)}
            </ul>
            <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid #eef2f7" }}>
              <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
                <b style={{ fontSize: 13, color: "#0f172a" }}>Functional report</b>
                <span style={{ fontSize: 11, color: "#64748b", flex: 1 }}>A deterministic, socialize-ready briefing (themes, multi-table source products, a mermaid flow) to plan with, then return to scaffold. The mermaid diagram renders when pasted into GitHub or a markdown viewer.</span>
                {!report
                  ? <button style={{ ...s.act, background: "#ede9fe", color: "#5b21b6", border: "1px solid #ddd6fe" }} disabled={reportBusy} onClick={() => loadReport(false)}>{reportBusy ? "Generating…" : "Generate functional report"}</button>
                  : <>
                      <button style={s.act} disabled={reportBusy} onClick={() => loadReport(true)}>{reportBusy ? "Regenerating…" : "Regenerate"}</button>
                      <button style={s.act} onClick={copyReport}>{copied ? "Copied ✓" : "Copy"}</button>
                      <button style={s.act} onClick={downloadReport}>Download (.md)</button>
                    </>}
              </div>
              {report && (
                <>
                  {report.generated_at && <div style={{ fontSize: 10, color: "#94a3b8", margin: "4px 0" }}>Generated {new Date(report.generated_at).toLocaleString()}</div>}
                  <div style={{ maxHeight: 520, overflow: "auto", background: "#fff", border: "1px solid #e2e8f0", borderRadius: 6, padding: "10px 14px", marginTop: 6, fontSize: 12, color: "#334155" }}>
                    <MarkdownMessage content={report.markdown} />
                  </div>
                </>
              )}
            </div>

            {dirty && <p style={{ fontSize: 12, color: "#b45309", marginTop: 14 }}>You have unsaved changes — Save first.</p>}
            <button style={{ ...s.save(!dirty && !committing), fontSize: 13, padding: "8px 18px", marginTop: dirty ? 0 : 14 }} disabled={dirty || committing} onClick={() => setConfirmScaffold(true)}>
              Scaffold this portfolio →
            </button>
          </div>
        );
      })()}

      {browseFor && (
        <div style={s.modal} onClick={() => setBrowseFor(null)}>
          <div style={s.modalBox} onClick={(ev) => ev.stopPropagation()}>
            <div style={{ display: "flex", alignItems: "center", marginBottom: 8 }}>
              <b style={{ fontSize: 14, color: "#0f172a", flex: 1 }}>Map to any in-scope column</b>
              <button style={s.act} onClick={() => setBrowseFor(null)}>✕</button>
            </div>
            <input autoFocus placeholder="Search column / table…" value={browseQ} onChange={(e) => setBrowseQ(e.target.value)} style={{ width: "100%", fontSize: 13, padding: "6px 10px", border: "1px solid #cbd5e1", borderRadius: 6, marginBottom: 8 }} />
            {scopeCols.length === 0 && <p style={{ fontSize: 12, color: "#94a3b8" }}>No columns loaded (the estate may be unreachable).</p>}
            {scopeCols.filter((c) => { const q = browseQ.toLowerCase(); return !q || c.column.toLowerCase().includes(q) || c.table.toLowerCase().includes(q); }).slice(0, 200).map((c, k) => (
              <div key={k} onClick={() => chooseBrowse(browseFor, c)} style={{ padding: "5px 6px", borderTop: k ? "1px solid #eef2f7" : "none", cursor: "pointer" }}>
                <span style={{ fontFamily: "monospace", fontSize: 11, color: "#334155" }}>{[c.schema, c.table].filter(Boolean).join(".")}.{c.column}</span>
                <span style={{ fontSize: 11, color: "#94a3b8", marginLeft: 6 }}>{c.type}</span>
                {c.description && <div style={{ fontSize: 11, color: "#64748b" }}>{c.description}</div>}
              </div>
            ))}
          </div>
        </div>
      )}

      {tableDetail && (
        <div style={s.modal} onClick={() => setTableDetail(null)}>
          <div style={s.modalBox} onClick={(ev) => ev.stopPropagation()}>
            <div style={{ display: "flex", alignItems: "center", marginBottom: 6 }}>
              <b style={{ fontSize: 15, color: "#0f172a", flex: 1, fontFamily: "monospace" }}>
                {tableDetail.schema ? `${tableDetail.schema}.` : ""}{tableDetail.table}
              </b>
              <button style={s.act} onClick={() => setTableDetail(null)}>✕</button>
            </div>
            {tableDetail.loading && <p style={{ fontSize: 12, color: "#94a3b8" }}>Loading…</p>}
            {tableDetail.error && <p style={{ fontSize: 12, color: "#991b1b" }}>Could not load table details.</p>}
            {tableDetail.found === false && <p style={{ fontSize: 12, color: "#94a3b8" }}>Table not found in the current scan.</p>}
            {tableDetail.found && (
              <>
                {tableDetail.description && <p style={{ fontSize: 12, color: "#475569", marginTop: 0 }}>{tableDetail.description}</p>}
                {(tableDetail.references_out?.length > 0 || tableDetail.references_in?.length > 0) && (
                  <div style={{ fontSize: 11, color: "#64748b", marginBottom: 8 }}>
                    <b>Relationships:</b>{" "}
                    {(tableDetail.references_out || []).map((r: any, k: number) => <span key={"o" + k} style={{ marginRight: 8 }}>→ {r.table}{r.on?.length ? ` (${r.on.join(",")})` : ""}</span>)}
                    {(tableDetail.references_in || []).map((r: any, k: number) => <span key={"i" + k} style={{ marginRight: 8, color: "#94a3b8" }}>← {r.table}{r.on?.length ? ` (${r.on.join(",")})` : ""}</span>)}
                  </div>
                )}
                <div style={{ fontSize: 11, color: "#64748b", fontWeight: 700, marginBottom: 2 }}>Columns ({tableDetail.columns?.length || 0}){tableDetail.row_count != null ? ` · ~${Number(tableDetail.row_count).toLocaleString()} rows` : ""}</div>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <tbody>
                    {(tableDetail.columns || []).map((c: any, k: number) => (
                      <tr key={k}>
                        <td style={{ ...s.td, fontFamily: "monospace", fontSize: 11, whiteSpace: "nowrap", verticalAlign: "top" }}>{c.name}</td>
                        <td style={{ ...s.td, fontSize: 10, color: "#94a3b8", whiteSpace: "nowrap", verticalAlign: "top" }}>{c.type}</td>
                        <td style={{ ...s.td, fontSize: 11, color: "#475569" }}>{c.description || <span style={{ color: "#cbd5e1" }}>—</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </div>
        </div>
      )}

      {confirmScaffold && (() => {
        const n = (clustersLoaded ? clusters : []).filter((c) => c.table_refs.length > 0).length;
        return (
          <div style={s.modal} onClick={() => !committing && setConfirmScaffold(false)}>
            <div style={s.modalBox} onClick={(ev) => ev.stopPropagation()}>
              <b style={{ fontSize: 15, color: "#0f172a" }}>Stage this portfolio for review?</b>
              <p style={{ fontSize: 12.5, color: "#334155", lineHeight: 1.6 }}>
                Stages a structured intake submission — it creates <b>{n}</b> source-aligned
                product(s) (built first, pre-filled with each cluster's tables + the columns the
                aggregate needs) and a pre-filled <b>{a.spec_name}</b> aggregate draft.
              </p>
              <div style={{ fontSize: 12, color: "#334155", background: "#f8fafc", border: "1px solid #e2e8f0", borderRadius: 6, padding: "8px 10px" }}>
                <b style={{ color: "#0f172a" }}>Next steps</b>
                <ol style={{ margin: "6px 0 0", paddingLeft: 18, lineHeight: 1.6 }}>
                  <li>Review + confirm it in the <b>Intake queue</b>.</li>
                  <li>On <b>Approve</b>, DW creates the projects — source products land in the <b>Engineering queue</b>; the aggregate becomes a pre-filled wizard draft.</li>
                  <li><b>Nothing is created until you Approve</b> — staging is reversible (reject the submission).</li>
                </ol>
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 12 }}>
                <button style={s.act} disabled={committing} onClick={() => setConfirmScaffold(false)}>Cancel</button>
                <button style={{ ...s.act, background: "#7c3aed", color: "#fff", border: "none", fontWeight: 700 }} disabled={committing} onClick={commitPortfolio}>{committing ? "Staging…" : "Stage for review →"}</button>
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
}
