// Connected Estate — the PO self-service surface: create an estate, attach a
// registered connection as a catalog-scoped source, browse its live schemas and
// pick which to scan (a saved, editable selection), launch an async metadata
// scan, edit/remove sources, and watch per-scan outcomes. Feeds the Feasibility
// page. A SEPARATE surface from the Pulse Discovery page.
import { useEffect, useState, type CSSProperties } from "react";
import api from "../../api/client";
import { downloadBlobZip } from "../../lib/download";
import EstateTableDescriptionsModal from "../../components/EstateTableDescriptionsModal";

interface Estate { id: number; name: string; domain: string | null; status: string }
interface Conn { id: number; connection_name: string; platform_type: string }
interface Source {
  id: number; name: string; platform: string; connection_id: number | null;
  catalog: string; enabled: boolean; namespace_policy: any; ingest_mode?: string;
}
// Offline-manifest upload preview (POST /sources/{id}/import-manifest/preview).
interface ManifestPreview {
  platform: string; catalog: string; depth: string;
  counts: { schemas: number; relations: number; columns: number;
    profiled_columns: number; redacted_columns: number; fk_edges: number;
    code_assets: number; pii_columns: number };
  schemas: { schema: string; relations: number; columns: number }[];
  redaction: Record<string, number>; pii_columns: string[]; warnings: string[];
}
const OFFLINE_PLATFORMS = ["postgres", "mysql", "snowflake", "databricks", "duckdb"];
interface EnrichProgress { schemas_total?: number; schemas_done?: number; current_schema?: string | null; current_database?: string | null; tables_total?: number; tables_done?: number; columns_done?: number; errors?: string[] }
interface ScanProgress { stage?: string; namespaces_total?: number; namespaces_done?: number; current_namespace?: string | null; relations_found?: number; columns_found?: number }
interface Scan { id: number; scan_version: number; state: string; stats: any; source_id?: number; namespace_outcomes?: any[]; started_at?: string | null; finished_at?: string | null; scan_progress?: ScanProgress; enrichment_state?: string | null; enriched_at?: string | null; enrichment_progress?: EnrichProgress; enrichment_summary?: { schema_descriptions?: Record<string, string> } }
interface Ns { name: string; parts: string[]; relation_count?: number | null }
// One catalog offered by GET /connections/{id}/catalogs. `schemas` is null +
// `schemas_enumerated` false when the provider couldn't list them (→ scan all).
interface CatalogEntry { catalog: string; schemas: string[] | null; schemas_enumerated: boolean }
// Scan drill-down payloads (GET /scans/{id} + /scans/{id}/datasets + /scans/{id}/assets).
interface NsOutcome { namespace: string; outcome: string; relation_count: number | null; column_count: number | null; detail: string | null }
interface ScanCol { uri: string; name: string; data_type: string | null; nullable: boolean | null; classification: string | null; description?: string | null }
interface ScanDataset { uri: string; database: string; schema: string; table: string; relation_kind: string; row_count: number | null; size_bytes: number | null; last_modified: string | null; row_count_is_estimate: boolean | null; description?: string | null; columns: ScanCol[] }
interface CodeAsset { uri: string; name: string; asset_kind: string; namespace: string | null; language: string | null; schedule: string | null; definition_preview: string | null; depends_on: string[] }
// `enrichSig` stamps the enrichment signature (state + enriched_at) the
// datasets were fetched under, so a later enrichment invalidates this cache.
interface ScanDetail { loading: boolean; error?: string; outcomes: NsOutcome[]; datasets: ScanDataset[]; assets: CodeAsset[]; enrichSig?: string }

const s: Record<string, any> = {
  page: { maxWidth: 1040, margin: "0 auto", padding: "8px 0", display: "grid", gridTemplateColumns: "300px 1fr", gap: 18 },
  h1: { fontSize: 22, fontWeight: 800, color: "#0f172a", marginBottom: 2 },
  sub: { fontSize: 13, color: "#64748b", marginBottom: 14 },
  panel: { border: "1px solid #e2e8f0", borderRadius: 8, background: "#fff", padding: 14 },
  label: { fontSize: 11, color: "#64748b", fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.3, display: "block", marginBottom: 3 },
  input: { fontSize: 13, padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 6, width: "100%", marginBottom: 8, boxSizing: "border-box" },
  select: { fontSize: 13, padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 6, width: "100%", marginBottom: 8, background: "#fff", boxSizing: "border-box" },
  btn: { fontSize: 13, fontWeight: 700, padding: "7px 14px", borderRadius: 6, border: "none", background: "#7c3aed", color: "#fff", cursor: "pointer" },
  btn2: { fontSize: 12, fontWeight: 600, padding: "5px 10px", borderRadius: 5, border: "1px solid #cbd5e1", background: "#fff", color: "#334155", cursor: "pointer" },
  btnDanger: { fontSize: 12, fontWeight: 600, padding: "5px 10px", borderRadius: 5, border: "1px solid #fecaca", background: "#fff", color: "#b91c1c", cursor: "pointer" },
  tabOn: { fontSize: 12, fontWeight: 700, padding: "5px 12px", borderRadius: 6, border: "1px solid #7c3aed", background: "#7c3aed", color: "#fff", cursor: "pointer" },
  tabOff: { fontSize: 12, fontWeight: 600, padding: "5px 12px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff", color: "#334155", cursor: "pointer" },
  modalBackdrop: { position: "fixed", inset: 0, background: "rgba(15,23,42,0.45)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 50 } as CSSProperties,
  modalCard: { background: "#fff", borderRadius: 10, padding: 18, width: 560, maxWidth: "92vw", maxHeight: "88vh", overflowY: "auto", boxShadow: "0 12px 40px rgba(0,0,0,0.25)" } as CSSProperties,
  estateRow: (active: boolean): CSSProperties => ({
    padding: "8px 10px", borderRadius: 6, cursor: "pointer", marginBottom: 4,
    background: active ? "#ede9fe" : "#f8fafc", border: `1px solid ${active ? "#c4b5fd" : "#eef2f7"}`,
    fontWeight: 600, color: "#0f172a", fontSize: 13,
  }),
  chip: (bg: string, fg: string): CSSProperties => ({ fontSize: 10, fontWeight: 700, padding: "1px 7px", borderRadius: 4, background: bg, color: fg, textTransform: "uppercase" }),
  th: { textAlign: "left", fontSize: 11, color: "#64748b", fontWeight: 700, padding: "3px 8px" },
  td: { fontSize: 12, color: "#334155", padding: "3px 8px", borderTop: "1px solid #eef2f7" },
};

const SCAN_CHIP: Record<string, [string, string]> = {
  queued: ["#e2e8f0", "#475569"], running: ["#dbeafe", "#1e40af"],
  completed: ["#dcfce7", "#166534"], partial: ["#fef3c7", "#92400e"],
  failed: ["#fee2e2", "#991b1b"], cancelled: ["#e2e8f0", "#475569"],
};

// Per-namespace scan outcome → [bg, fg, label]. Mirrors estate_scan.OUTCOME_*.
const NS_CHIP: Record<string, [string, string, string]> = {
  scanned: ["#dcfce7", "#166534", "scanned"],
  successful_empty: ["#e2e8f0", "#475569", "empty"],
  inaccessible_namespace: ["#fee2e2", "#991b1b", "inaccessible"],
  partial_scan: ["#fef3c7", "#92400e", "partial"],
  connection_failure: ["#fee2e2", "#991b1b", "connection failed"],
};

function fmtBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(1)} MB`;
  return `${(b / 1024 ** 3).toFixed(2)} GB`;
}

// Elapsed wall-clock between two ISO timestamps, e.g. "3m 12s" / "8s".
function fmtDuration(startIso?: string | null, endIso?: string | null): string {
  if (!startIso || !endIso) return "";
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (!isFinite(ms) || ms < 0) return "";
  const secs = Math.round(ms / 1000);
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

// The trailing dotted segment of a namespace ('cat.schema' → 'schema') — the
// dataset rows key on the bare schema, so this bridges the two payloads.
function schemaKey(namespace: string): string {
  const parts = (namespace || "").split(".").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : namespace;
}

// A 3-level platform (Unity Catalog / database container) needs a catalog pick.
function selectionFromPolicy(policy: any, all: string[]): Set<string> {
  const mode = policy?.mode || "all";
  const names: string[] = policy?.namespaces || [];
  if (mode === "include") return new Set(all.filter((n) => names.includes(n)));
  if (mode === "exclude") return new Set(all.filter((n) => !names.includes(n)));
  return new Set(all);
}
function policyFromSelection(selected: Set<string>, all: string[]): any {
  if (all.length > 0 && selected.size === all.length) return { mode: "all", namespaces: [] };
  return { mode: "include", namespaces: [...selected] };
}

export default function EstatePage() {
  const [estates, setEstates] = useState<Estate[]>([]);
  const [sel, setSel] = useState<Estate | null>(null);
  const [conns, setConns] = useState<Conn[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [scans, setScans] = useState<Scan[]>([]);
  const [newName, setNewName] = useState("");
  const [newDomain, setNewDomain] = useState("");
  const [connId, setConnId] = useState<number | null>(null);
  // add-source catalog→schema tree (3-level platforms). `catalogs` is null when
  // the platform is not catalog-scoped (2-level: postgres/mysql), [] when it is
  // but the provider enumerated none (free-text fallback), else the entry list.
  const [catalogs, setCatalogs] = useState<CatalogEntry[] | null>(null);
  const [catLoading, setCatLoading] = useState(false);
  const [newCatalog, setNewCatalog] = useState("");     // free-text fallback
  const [checkedCats, setCheckedCats] = useState<Set<string>>(new Set());
  const [catSchemaSel, setCatSchemaSel] = useState<Record<string, Set<string>>>({});
  const [expandedCats, setExpandedCats] = useState<Set<string>>(new Set());
  const [addResults, setAddResults] = useState<{ catalog: string; ok: boolean; msg: string }[] | null>(null);
  const [adding, setAdding] = useState(false);
  // Offline source add + manifest import.
  const [addMode, setAddMode] = useState<"live" | "offline">("live");
  const [offlinePlatform, setOfflinePlatform] = useState("postgres");
  const [importSource, setImportSource] = useState<Source | null>(null);
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importPreview, setImportPreview] = useState<ManifestPreview | null>(null);
  const [importErr, setImportErr] = useState("");
  const [importing, setImporting] = useState(false);
  // browse/select panel state (per-source)
  const [browseId, setBrowseId] = useState<number | null>(null);
  const [nsTree, setNsTree] = useState<Ns[] | null>(null);
  const [nsLoading, setNsLoading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [saveMsg, setSaveMsg] = useState("");
  // scan drill-down: which scan rows / tables are expanded + a per-scan cache.
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [scanDetail, setScanDetail] = useState<Record<number, ScanDetail>>({});
  const [assetOpen, setAssetOpen] = useState<Set<string>>(new Set());
  const [openTables, setOpenTables] = useState<Set<string>>(new Set());
  // per-table descriptions modal (reads already-loaded drill-down data)
  const [descModal, setDescModal] = useState<ScanDataset | null>(null);

  const load = () => api.get("/api/estates").then((r) => setEstates(r.data.estates || [])).catch(() => {});
  useEffect(() => { load(); api.get("/api/connections").then((r) => setConns(r.data.connections || [])).catch(() => {}); }, []);

  const refreshSources = async () => {
    if (!sel) return;
    const r = await api.get(`/api/estates/${sel.id}`);
    setSources(r.data.sources || []); setScans(r.data.scans || []);
  };

  useEffect(() => {
    if (!sel) return;
    refreshSources().catch(() => {});
    setBrowseId(null); setNsTree(null);
  }, [sel]);

  // When the add-source connection changes, probe for catalogs (3-level only).
  useEffect(() => {
    setCatalogs(null); setNewCatalog(""); setCheckedCats(new Set());
    setCatSchemaSel({}); setExpandedCats(new Set()); setAddResults(null);
    if (!sel || connId == null) return;
    setCatLoading(true);
    api.get(`/api/estates/${sel.id}/connections/${connId}/catalogs`)
      .then((r) => {
        if (!r.data.supported) { setCatalogs(null); return; }
        const entries: CatalogEntry[] = (r.data.catalogs || []).map((c: any) => ({
          catalog: c.catalog, schemas: c.schemas ?? null, schemas_enumerated: !!c.schemas_enumerated,
        }));
        setCatalogs(entries);
        // Default per-catalog schema selection = all enumerated schemas.
        const sel0: Record<string, Set<string>> = {};
        for (const e of entries) if (e.schemas_enumerated && e.schemas) sel0[e.catalog] = new Set(e.schemas);
        setCatSchemaSel(sel0);
      })
      .catch(() => setCatalogs(null))
      .finally(() => setCatLoading(false));
  }, [sel, connId]);

  async function createEstate() {
    if (!newName.trim()) return;
    const r = await api.post("/api/estates", { name: newName.trim(), domain: newDomain || null });
    setNewName(""); setNewDomain(""); await load(); setSel(r.data);
  }

  // Single add (2-level platform, or a supported-but-not-enumerated free-text catalog).
  async function addSource() {
    if (!sel || connId == null) return;
    const cat = newCatalog.trim();
    try {
      await api.post(`/api/estates/${sel.id}/sources`, { connection_id: connId, catalog: cat });
      setAddResults([{ catalog: cat || "(connection database)", ok: true, msg: "added" }]);
    } catch (e: any) {
      setAddResults([{ catalog: cat || "(connection database)", ok: false,
                       msg: e?.response?.data?.detail?.message || e?.response?.data?.detail || "failed" }]);
    }
    setNewCatalog("");
    await refreshSources();
  }

  function toggleCat(cat: string) {
    setCheckedCats((prev) => { const n = new Set(prev); n.has(cat) ? n.delete(cat) : n.add(cat); return n; });
  }
  function toggleAllCats(entries: CatalogEntry[], existing: Set<string>) {
    const addable = entries.filter((e) => !existing.has(e.catalog)).map((e) => e.catalog);
    setCheckedCats((prev) => {
      const allOn = addable.length > 0 && addable.every((c) => prev.has(c));
      return allOn ? new Set() : new Set(addable);
    });
  }
  function toggleCatExpand(cat: string) {
    setExpandedCats((prev) => { const n = new Set(prev); n.has(cat) ? n.delete(cat) : n.add(cat); return n; });
  }
  function toggleCatSchema(cat: string, schema: string) {
    setCatSchemaSel((prev) => {
      const cur = new Set(prev[cat] || []);
      cur.has(schema) ? cur.delete(schema) : cur.add(schema);
      return { ...prev, [cat]: cur };
    });
  }

  // Multi-catalog add: one source per checked catalog (each with its own per-schema
  // policy), reporting per-catalog success/failure so a single 409 can't kill the batch.
  async function addSelectedSources(entries: CatalogEntry[]) {
    if (!sel || connId == null) return;
    setAdding(true);
    const results: { catalog: string; ok: boolean; msg: string }[] = [];
    for (const e of entries) {
      if (!checkedCats.has(e.catalog)) continue;
      let policy: any = { mode: "all", namespaces: [] };
      if (e.schemas_enumerated && e.schemas) {
        policy = policyFromSelection(catSchemaSel[e.catalog] || new Set(e.schemas), e.schemas);
      }
      try {
        await api.post(`/api/estates/${sel.id}/sources`,
          { connection_id: connId, catalog: e.catalog, namespace_policy: policy });
        results.push({ catalog: e.catalog, ok: true, msg: "added" });
      } catch (err: any) {
        results.push({ catalog: e.catalog, ok: false,
                       msg: err?.response?.data?.detail?.message || err?.response?.data?.detail || "failed" });
      }
    }
    setAddResults(results);
    setCheckedCats(new Set());
    setAdding(false);
    await refreshSources();
  }

  async function browse(source: Source) {
    setBrowseId(source.id); setNsTree(null); setNsLoading(true); setSaveMsg("");
    try {
      const r = await api.get(`/api/estates/${sel!.id}/sources/${source.id}/namespaces?with_counts=true`);
      const ns: Ns[] = r.data.namespaces || [];
      setNsTree(ns);
      setSelected(selectionFromPolicy(source.namespace_policy, ns.map((n) => n.name)));
    } catch (e: any) {
      alert(e?.response?.data?.detail || "Could not browse the source.");
      setBrowseId(null);
    } finally {
      setNsLoading(false);
    }
  }

  function toggle(name: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  }

  async function saveSelection(source: Source) {
    if (!nsTree) return;
    const policy = policyFromSelection(selected, nsTree.map((n) => n.name));
    await api.patch(`/api/estates/${sel!.id}/sources/${source.id}`, { namespace_policy: policy });
    setSaveMsg("Saved."); await refreshSources();
    setTimeout(() => setSaveMsg(""), 2000);
  }

  // ── offline extraction (client-run kit + manifest upload) ──────────────────
  async function downloadKit(source: Source) {
    try {
      await downloadBlobZip(
        `/api/estates/sources/${source.id}/extraction-package?format=zip`,
        `extraction-kit-${source.platform}-${source.id}.zip`);
    } catch { alert("Could not download the extraction kit."); }
  }

  async function chooseManifest(source: Source) {
    const input = document.createElement("input");
    input.type = "file"; input.accept = ".yaml,.yml";
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      setImportSource(source); setImportFile(file); setImportPreview(null); setImportErr("");
      const fd = new FormData(); fd.append("file", file);
      try {
        const r = await api.post(`/api/estates/sources/${source.id}/import-manifest/preview`, fd);
        setImportPreview(r.data);
      } catch (e: any) {
        setImportErr(e?.response?.data?.detail?.detail || e?.response?.data?.detail || "Manifest failed validation.");
      }
    };
    input.click();
  }

  async function confirmImport() {
    if (!importSource || !importFile) return;
    setImporting(true);
    const fd = new FormData(); fd.append("file", importFile);
    try {
      await api.post(`/api/estates/sources/${importSource.id}/import-manifest`, fd);
      setImportSource(null); setImportFile(null); setImportPreview(null);
      await refreshScans();
    } catch (e: any) {
      setImportErr(e?.response?.data?.detail?.detail || e?.response?.data?.detail || "Import failed.");
    } finally { setImporting(false); }
  }

  async function addOfflineSource() {
    if (!sel) return;
    setAdding(true); setAddResults(null);
    const cat = newCatalog.trim();
    try {
      await api.post(`/api/estates/${sel.id}/sources`, {
        ingest_mode: "offline", platform: offlinePlatform, catalog: cat,
      });
      setAddResults([{ catalog: cat || offlinePlatform, ok: true, msg: "offline source added" }]);
      setNewCatalog("");
      await refreshSources();
    } catch (e: any) {
      const d = e?.response?.data?.detail;
      setAddResults([{ catalog: cat || offlinePlatform, ok: false,
        msg: (d && (d.message || (typeof d === "string" ? d : null))) || "add failed" }]);
    } finally { setAdding(false); }
  }

  async function launchScan(source: Source) {
    // If the browse panel is open for this source, scan exactly the ticked
    // schemas (persists the selection too); otherwise use the saved policy.
    const body: any = { source_id: source.id, depth: "metadata" };
    if (browseId === source.id && nsTree) {
      body.namespace_policy = policyFromSelection(selected, nsTree.map((n) => n.name));
    }
    try {
      await api.post(`/api/estates/${sel!.id}/scans`, body);
    } catch (e: any) { alert(e?.response?.data?.detail?.message || "Could not launch scan."); }
    await refreshScans();
  }

  async function toggleEnabled(source: Source) {
    await api.patch(`/api/estates/${sel!.id}/sources/${source.id}`, { enabled: !source.enabled });
    await refreshSources();
  }

  async function rename(source: Source) {
    const name = window.prompt("Rename source", source.name);
    if (name == null) return;
    await api.patch(`/api/estates/${sel!.id}/sources/${source.id}`, { name });
    await refreshSources();
  }

  async function removeSource(source: Source) {
    if (!window.confirm(`Remove source "${source.name}"? This deletes its scans and their data from the estate.`)) return;
    try {
      await api.delete(`/api/estates/${sel!.id}/sources/${source.id}`);
    } catch (e: any) { alert(e?.response?.data?.detail?.message || "Could not remove the source."); return; }
    if (browseId === source.id) { setBrowseId(null); setNsTree(null); }
    await refreshSources();
  }

  async function refreshScans() {
    if (!sel) return;
    const r = await api.get(`/api/estates/${sel.id}/scans`);
    setScans(r.data.scans || []);
  }

  async function enrichEstate() {
    if (!sel) return;
    try {
      await api.post(`/api/estates/${sel.id}/enrich`);
      await refreshScans();
    } catch (e: any) {
      alert(e?.response?.data?.detail?.message || e?.response?.data?.detail || "Could not start enrichment.");
    }
  }

  // The enrichment signature of a scan — bumps whenever enrichment state
  // advances (idle → enriching → enriched), so cached drill-down that predates
  // the latest enrichment can be detected and re-fetched.
  const enrichSigOf = (sc: Scan) => `${sc.enrichment_state ?? ""}:${sc.enriched_at ?? ""}`;

  // Fetch a scan's drill-down (outcomes + datasets + assets), stamped with the
  // enrichment signature it was fetched under. `background=true` re-fetches in
  // place without flashing the loading state and keeps the prior data on error
  // (used when enrichment completes on an already-expanded scan).
  async function fetchScanDetail(scanId: number, sig: string, background = false) {
    if (!background) {
      setScanDetail((prev) => ({ ...prev, [scanId]: { loading: true, outcomes: [], datasets: [], assets: [], enrichSig: sig } }));
    }
    try {
      const [meta, ds, ca] = await Promise.all([
        api.get(`/api/estates/scans/${scanId}`),
        api.get(`/api/estates/scans/${scanId}/datasets`),
        api.get(`/api/estates/scans/${scanId}/assets`),
      ]);
      setScanDetail((prev) => ({
        ...prev,
        [scanId]: {
          loading: false,
          outcomes: meta.data.namespace_outcomes || [],
          datasets: ds.data.datasets || [],
          assets: ca.data.assets || [],
          enrichSig: sig,
        },
      }));
    } catch (e: any) {
      if (background) {
        // Keep the stale-but-usable data; just re-stamp so we don't re-fetch in a loop.
        setScanDetail((prev) => (prev[scanId] ? { ...prev, [scanId]: { ...prev[scanId], enrichSig: sig } } : prev));
      } else {
        setScanDetail((prev) => ({
          ...prev,
          [scanId]: { loading: false, error: e?.response?.data?.detail || "Could not load scan detail.", outcomes: [], datasets: [], assets: [], enrichSig: sig },
        }));
      }
    }
  }

  // Expand a scan row → fetch its outcomes + datasets (cached per scan id).
  async function toggleScanExpand(scanId: number) {
    const wasOpen = expanded.has(scanId);
    setExpanded((prev) => {
      const next = new Set(prev);
      wasOpen ? next.delete(scanId) : next.add(scanId);
      return next;
    });
    if (wasOpen || scanDetail[scanId]) return; // collapsing, or already cached
    const sc = scans.find((x) => x.id === scanId);
    await fetchScanDetail(scanId, sc ? enrichSigOf(sc) : "");
  }

  function toggleTable(uri: string) {
    setOpenTables((prev) => {
      const next = new Set(prev);
      next.has(uri) ? next.delete(uri) : next.add(uri);
      return next;
    });
  }

  // Poll while any scan is active or enrichment is in progress.
  useEffect(() => {
    const active = scans.some((x) =>
      ["queued", "running"].includes(x.state) || x.enrichment_state === "enriching"
    );
    if (!active) return;
    const t = setInterval(refreshScans, 2500);
    return () => clearInterval(t);
  }, [scans, sel]);

  // After metadata enrichment completes (or any enrichment-state change), the
  // cached drill-down still holds the pre-enrichment columns (empty
  // descriptions). Re-fetch detail in-place for any expanded scan whose
  // enrichment signature advanced past the one we cached, so "View
  // descriptions" shows fresh text without a full browser refresh.
  useEffect(() => {
    for (const sc of scans) {
      const cached = scanDetail[sc.id];
      if (!cached || cached.loading) continue;
      const sig = enrichSigOf(sc);
      if (cached.enrichSig !== undefined && cached.enrichSig !== sig) {
        fetchScanDetail(sc.id, sig, true);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scans]);

  // Keep an already-open descriptions modal pointed at the freshest dataset, so
  // a refetch triggered by enrichment completing updates the modal in place too
  // (the modal holds a dataset snapshot captured at click time).
  useEffect(() => {
    if (!descModal) return;
    const openUri = descModal.uri;
    for (const d of Object.values(scanDetail) as ScanDetail[]) {
      const fresh = d.datasets.find((x) => x.uri === openUri);
      if (fresh && fresh !== descModal) { setDescModal(fresh); return; }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scanDetail]);

  const browseSource = sources.find((x) => x.id === browseId) || null;

  // Live enrichment progress bar (namespace-granular; copy is level-neutral since
  // `current_schema` is a database name on MySQL). Falls back to static text when
  // no progress payload has been persisted yet (e.g. pre-migration scans).
  function renderEnrichProgress(p: EnrichProgress | undefined) {
    const schemasTotal = p?.schemas_total ?? 0;
    if (!p || !schemasTotal) {
      return (
        <div style={{ fontSize: 11, color: "#1e40af", marginTop: 3, marginLeft: 18 }}>
          Generating metadata descriptions… (this may take a few minutes)
        </div>
      );
    }
    const tablesTotal = p.tables_total ?? 0;
    const tablesDone = p.tables_done ?? 0;
    const schemasDone = p.schemas_done ?? 0;
    const frac = tablesTotal > 0 ? tablesDone / tablesTotal : (schemasTotal > 0 ? schemasDone / schemasTotal : 0);
    const pct = Math.max(0, Math.min(100, Math.round(frac * 100)));
    return (
      <div style={{ marginTop: 4, marginLeft: 18, maxWidth: 460 }}>
        <div style={{ fontSize: 11, color: "#1e40af", marginBottom: 3 }}>
          {p.current_schema ? <>Enriching <b>{p.current_schema}</b> · </> : "Enriching · "}
          {schemasDone}/{schemasTotal} groups
          {tablesTotal > 0 ? ` · ${tablesDone}/${tablesTotal} tables` : ""}
          {p.columns_done ? ` · ${p.columns_done} columns described` : ""}
        </div>
        <div style={{ height: 6, borderRadius: 3, background: "#e2e8f0", overflow: "hidden" }}>
          <div style={{ width: `${pct}%`, height: "100%", background: "#3b82f6", transition: "width 0.4s ease" }} />
        </div>
        {p.errors && p.errors.length > 0 && (
          <div style={{ fontSize: 10, color: "#b45309", marginTop: 3 }}>{p.errors.length} group(s) had issues — see failed state on completion.</div>
        )}
      </div>
    );
  }

  // Live metadata-scan progress bar (schema-granular; one _record_namespace per
  // schema). Falls back to static text before the first progress payload lands.
  function renderScanProgress(p: ScanProgress | undefined) {
    const nsTotal = p?.namespaces_total ?? 0;
    if (!p || !nsTotal) {
      return (
        <div style={{ fontSize: 11, color: "#1e40af", marginTop: 3, marginLeft: 18 }}>
          Scanning namespaces… (deterministic metadata scan)
        </div>
      );
    }
    const done = p.namespaces_done ?? 0;
    const frac = nsTotal > 0 ? done / nsTotal : 0;
    const pct = Math.max(0, Math.min(100, Math.round(frac * 100)));
    return (
      <div style={{ marginTop: 4, marginLeft: 18, maxWidth: 460 }}>
        <div style={{ fontSize: 11, color: "#1e40af", marginBottom: 3 }}>
          {p.current_namespace ? <>Scanning <b>{p.current_namespace}</b> · </> : "Scanning · "}
          {done}/{nsTotal} schemas · {p.relations_found ?? 0} tables · {p.columns_found ?? 0} columns
        </div>
        <div style={{ height: 6, borderRadius: 3, background: "#e2e8f0", overflow: "hidden" }}>
          <div style={{ width: `${pct}%`, height: "100%", background: "#3b82f6", transition: "width 0.4s ease" }} />
        </div>
      </div>
    );
  }

  // Render the expanded body of one scan: per-schema sections (outcome + counts)
  // with each schema's tables (from the datasets payload) drilling to columns.
  function renderScanDetail(detail: ScanDetail | undefined, sc: Scan) {
    if (!detail || detail.loading) return <div style={{ fontSize: 12, color: "#64748b" }}>Loading scan detail…</div>;
    if (detail.error) return <div style={{ fontSize: 12, color: "#b91c1c" }}>{detail.error}</div>;
    const bySchema: Record<string, ScanDataset[]> = {};
    for (const d of detail.datasets) (bySchema[d.schema] ||= []).push(d);
    // One section per recorded namespace outcome; fold in any dataset-only schema.
    const sections = detail.outcomes.map((o) => ({ ns: o.namespace, key: schemaKey(o.namespace), outcome: o }));
    const covered = new Set(sections.map((x) => x.key));
    for (const schema of Object.keys(bySchema)) {
      if (!covered.has(schema)) sections.push({ ns: schema, key: schema, outcome: null as any });
    }
    if (sections.length === 0)
      return <div style={{ fontSize: 12, color: "#94a3b8" }}>No schemas recorded for this scan.</div>;
    return (
      <div style={{ display: "grid", gap: 8 }}>
        {renderCodeAssets(detail)}
        {sections.map((sec) => {
          const o = sec.outcome;
          const [nbg, nfg, nlabel] = o ? (NS_CHIP[o.outcome] || ["#e2e8f0", "#475569", o.outcome]) : ["#e2e8f0", "#475569", "scanned"];
          const tables = bySchema[sec.key] || [];
          const tableCount = o?.relation_count ?? tables.length;
          const colCount = o?.column_count ?? tables.reduce((n, t) => n + t.columns.length, 0);
          // Schema-level rollup description, keyed by the bare-schema `sec.key`
          // (NOT the full `sec.ns` namespace) so 3-level platforms resolve.
          const schemaDesc = sc.enrichment_summary?.schema_descriptions?.[sec.key];
          return (
            <div key={sec.ns} style={{ border: "1px solid #eef2f7", borderRadius: 6, padding: "8px 10px", background: "#f8fafc" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: "#0f172a" }}>{sec.ns}</span>
                <span style={s.chip(nbg, nfg)}>{nlabel}</span>
                <span style={{ fontSize: 11, color: "#64748b" }}>{tableCount} tables · {colCount} columns</span>
              </div>
              {schemaDesc ? <div style={{ fontSize: 11.5, color: "#475569", marginTop: 4, lineHeight: 1.4, fontStyle: "italic" }}>{schemaDesc}</div> : null}
              {o?.detail ? <div style={{ fontSize: 11, color: "#b45309", marginTop: 3 }}>{o.detail}</div> : null}
              {tables.length > 0 && (
                <div style={{ marginTop: 6, display: "grid", gap: 2 }}>
                  {tables.map((t) => {
                    const isOpen = openTables.has(t.uri);
                    // Descriptions available once enriched (or if any description
                    // already landed on this table's columns / the table itself).
                    const hasDesc = sc.enrichment_state === "enriched" || !!t.description || t.columns.some((c) => c.description);
                    return (
                      <div key={t.uri}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", padding: "2px 0" }} onClick={() => toggleTable(t.uri)}>
                          <span style={{ fontSize: 11, color: "#94a3b8", width: 10 }}>{isOpen ? "▾" : "▸"}</span>
                          <span style={{ fontSize: 12, color: "#334155", fontWeight: 600 }}>{t.table}</span>
                          <span style={{ fontSize: 10, color: "#94a3b8" }}>{t.relation_kind}</span>
                          <span style={{ fontSize: 11, color: "#64748b", marginLeft: "auto" }}>
                            {t.columns.length} cols
                            {t.row_count != null ? ` · ${t.row_count.toLocaleString()}${t.row_count_is_estimate ? "~" : ""} rows` : ""}
                            {t.size_bytes != null ? ` · ${fmtBytes(t.size_bytes)}` : ""}
                          </span>
                          {hasDesc && (
                            <button
                              style={{ ...s.btn2, fontSize: 11, padding: "3px 8px" }}
                              onClick={(e) => { e.stopPropagation(); setDescModal(t); }}
                              title="View LLM-generated table + column descriptions"
                            >
                              View descriptions
                            </button>
                          )}
                        </div>
                        {isOpen && (
                          <table style={{ borderCollapse: "collapse", width: "100%", marginLeft: 18, marginBottom: 4 }}>
                            <thead><tr><th style={s.th}>Column</th><th style={s.th}>Type</th><th style={s.th}></th></tr></thead>
                            <tbody>{t.columns.map((c) => (
                              <tr key={c.uri}>
                                <td style={s.td}>{c.name}</td>
                                <td style={s.td}>{c.data_type || "—"}{c.nullable === false ? " · not null" : ""}</td>
                                <td style={s.td}>{c.classification === "pii" ? <span style={s.chip("#fee2e2", "#991b1b")}>PII</span> : null}</td>
                              </tr>
                            ))}</tbody>
                          </table>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
    );
  }

  function renderCodeAssets(detail: ScanDetail) {
    if (!detail.assets || detail.assets.length === 0) return null;
    const byKind: Record<string, CodeAsset[]> = {};
    for (const a of detail.assets) (byKind[a.asset_kind] ||= []).push(a);
    return (
      <div style={{ marginTop: 10 }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: "#64748b", textTransform: "uppercase", letterSpacing: 0.3, marginBottom: 6 }}>
          Code Assets ({detail.assets.length})
        </div>
        {Object.entries(byKind).map(([kind, assets]) => (
          <div key={kind} style={{ marginBottom: 8 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#475569", marginBottom: 3 }}>{kind}</div>
            {assets.map((a) => {
              const isOpen = assetOpen.has(a.uri);
              return (
                <div key={a.uri} style={{ border: "1px solid #eef2f7", borderRadius: 5, marginBottom: 3 }}>
                  <div
                    style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 8px", cursor: a.definition_preview ? "pointer" : "default" }}
                    onClick={() => {
                      if (!a.definition_preview) return;
                      setAssetOpen((prev) => { const n = new Set(prev); n.has(a.uri) ? n.delete(a.uri) : n.add(a.uri); return n; });
                    }}
                  >
                    {a.definition_preview ? <span style={{ fontSize: 10, color: "#94a3b8", width: 10 }}>{isOpen ? "▾" : "▸"}</span> : <span style={{ width: 10 }} />}
                    <span style={{ fontSize: 12, fontWeight: 600, color: "#0f172a" }}>{a.name}</span>
                    {a.namespace && <span style={{ fontSize: 11, color: "#7c3aed" }}>{a.namespace}</span>}
                    {a.language && <span style={{ ...s.chip("#dbeafe", "#1e40af") }}>{a.language}</span>}
                    {a.schedule && <span style={{ fontSize: 11, color: "#64748b" }}>⏱ {a.schedule}</span>}
                    {a.depends_on?.length > 0 && <span style={{ fontSize: 11, color: "#64748b", marginLeft: "auto" }}>{a.depends_on.length} deps</span>}
                  </div>
                  {isOpen && a.definition_preview && (
                    <pre style={{ fontSize: 11, color: "#334155", background: "#f8fafc", margin: 0, padding: "6px 10px", borderTop: "1px solid #eef2f7", overflowX: "auto", maxHeight: 200 }}>
                      {a.definition_preview}
                    </pre>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    );
  }

  return (
    <div>
      <div style={s.h1}>Connected Estate</div>
      <div style={s.sub}>
        Scan a live platform directly, then evaluate top-down which data products are buildable on the
        Feasibility page. One source per catalog — add several to assess more. (Separate from Pulse Discovery.)
      </div>
      <div style={s.page}>
        {/* Left: estate list + create */}
        <div>
          <div style={s.panel}>
            <span style={s.label}>Your estates</span>
            {estates.map((e) => (
              <div key={e.id} style={s.estateRow(sel?.id === e.id)} onClick={() => setSel(e)}>
                {e.name}{e.domain ? <span style={{ color: "#94a3b8", fontWeight: 400 }}> · {e.domain}</span> : null}
              </div>
            ))}
            {estates.length === 0 && <div style={{ fontSize: 12, color: "#94a3b8" }}>No estates yet.</div>}
            <div style={{ borderTop: "1px solid #eef2f7", marginTop: 10, paddingTop: 10 }}>
              <span style={s.label}>New estate</span>
              <input style={s.input} placeholder="Name" value={newName} onChange={(e) => setNewName(e.target.value)} />
              <input style={s.input} placeholder="Domain (optional)" value={newDomain} onChange={(e) => setNewDomain(e.target.value)} />
              <button style={s.btn} onClick={createEstate}>Create estate</button>
            </div>
          </div>
        </div>

        {/* Right: selected estate detail */}
        <div>
          {!sel && <div style={s.panel}>Select or create an estate to add a source and scan.</div>}
          {sel && (
            <>
              <div style={{ ...s.panel, marginBottom: 14 }}>
                <span style={s.label}>Sources</span>
                {sources.map((src) => (
                  <div key={src.id} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, opacity: src.enabled ? 1 : 0.55 }}>
                    <span style={{ fontSize: 13, fontWeight: 600, flex: 1 }}>
                      {src.name}
                      {src.catalog ? <span style={{ color: "#7c3aed", fontWeight: 700 }}> · {src.catalog}</span> : null}
                      <span style={{ color: "#94a3b8", fontWeight: 400 }}> · {src.platform}</span>
                      {src.ingest_mode === "offline"
                        ? <span style={{ color: "#0891b2", fontWeight: 700 }}> · offline</span> : null}
                      {!src.enabled ? <span style={{ color: "#94a3b8", fontWeight: 400 }}> · disabled</span> : null}
                    </span>
                    <button style={s.btn2} title="Rename" onClick={() => rename(src)}>✎</button>
                    <button style={s.btn2} onClick={() => toggleEnabled(src)}>{src.enabled ? "Disable" : "Enable"}</button>
                    {src.ingest_mode === "offline" ? (
                      <>
                        <button style={s.btn2} title="Download the client-run extraction kit"
                          onClick={() => downloadKit(src)}>⤓ Scan Kit</button>
                        <button style={s.btn} title="Upload an extraction manifest"
                          onClick={() => chooseManifest(src)}>Import scan</button>
                      </>
                    ) : (
                      <>
                        <button style={s.btn2} onClick={() => browse(src)}>Browse</button>
                        <button style={s.btn} onClick={() => launchScan(src)}>Scan</button>
                      </>
                    )}
                    <button style={s.btnDanger} onClick={() => removeSource(src)}>Remove</button>
                  </div>
                ))}
                {sources.length === 0 && <div style={{ fontSize: 12, color: "#94a3b8", marginBottom: 6 }}>No sources yet.</div>}
                <div style={{ marginTop: 8, borderTop: "1px solid #eef2f7", paddingTop: 10 }}>
                  <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                    <button style={addMode === "live" ? s.tabOn : s.tabOff}
                      onClick={() => setAddMode("live")}>Live connection</button>
                    <button style={addMode === "offline" ? s.tabOn : s.tabOff}
                      onClick={() => setAddMode("offline")}>Can't connect — offline</button>
                  </div>

                  {addMode === "offline" && (
                    <div>
                      <div style={{ fontSize: 12, color: "#64748b", marginBottom: 6 }}>
                        No live connection needed. Add the source, download the extraction kit,
                        run it in your environment, and upload the reviewed manifest.
                      </div>
                      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                        <select style={{ ...s.select, width: "auto", marginBottom: 0 }} value={offlinePlatform}
                          onChange={(e) => setOfflinePlatform(e.target.value)}>
                          {OFFLINE_PLATFORMS.map((p) => <option key={p} value={p}>{p}</option>)}
                        </select>
                        <input style={{ ...s.input, width: 180, marginBottom: 0 }} placeholder="catalog / database (optional)"
                          value={newCatalog} onChange={(e) => setNewCatalog(e.target.value)} />
                        <button style={s.btn} disabled={adding} onClick={addOfflineSource}>
                          {adding ? "Adding…" : "Add offline source"}
                        </button>
                      </div>
                      {addResults && (
                        <div style={{ fontSize: 12, marginTop: 6 }}>
                          {addResults.map((r, i) => (
                            <div key={i} style={{ color: r.ok ? "#059669" : "#dc2626" }}>
                              {r.ok ? "✓" : "✗"} {r.catalog}: {r.msg}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {addMode === "live" && (<>
                  <span style={s.label}>Add a source (registered connection)</span>
                  <select style={s.select} value={connId ?? ""} onChange={(e) => setConnId(e.target.value ? Number(e.target.value) : null)}>
                    <option value="">Select a connection…</option>
                    {conns.map((c) => <option key={c.id} value={c.id}>{c.connection_name} ({c.platform_type})</option>)}
                  </select>
                  {catLoading && <div style={{ fontSize: 12, color: "#94a3b8" }}>Probing catalogs…</div>}

                  {/* Catalog-scoped platform with enumerated catalogs → tree + multi-add */}
                  {connId != null && catalogs != null && catalogs.length > 0 && (() => {
                    const existing = new Set(sources.filter((x) => x.connection_id === connId).map((x) => x.catalog));
                    const addable = catalogs.filter((c) => !existing.has(c.catalog));
                    const allChecked = addable.length > 0 && addable.every((c) => checkedCats.has(c.catalog));
                    return (
                      <div>
                        <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 4 }}>
                          <label style={{ fontSize: 12, color: "#334155", display: "inline-flex", gap: 6, alignItems: "center", cursor: addable.length ? "pointer" : "default" }}>
                            <input type="checkbox" checked={allChecked} disabled={addable.length === 0} onChange={() => toggleAllCats(catalogs, existing)} />
                            Select all catalogs
                          </label>
                          <span style={{ fontSize: 11, color: "#94a3b8" }}>{checkedCats.size} selected</span>
                        </div>
                        <div style={{ maxHeight: 260, overflowY: "auto", border: "1px solid #e2e8f0", borderRadius: 6 }}>
                          {catalogs.map((c) => {
                            const isExisting = existing.has(c.catalog);
                            const isExp = expandedCats.has(c.catalog);
                            const schemaSel = catSchemaSel[c.catalog] || new Set(c.schemas || []);
                            return (
                              <div key={c.catalog} style={{ borderBottom: "1px solid #f1f5f9" }}>
                                <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "5px 8px" }}>
                                  <input type="checkbox" checked={checkedCats.has(c.catalog)} disabled={isExisting} onChange={() => toggleCat(c.catalog)} />
                                  {c.schemas_enumerated
                                    ? <span style={{ fontSize: 12, color: "#94a3b8", width: 10, cursor: "pointer" }} onClick={() => toggleCatExpand(c.catalog)}>{isExp ? "▾" : "▸"}</span>
                                    : <span style={{ width: 10 }} />}
                                  <span style={{ fontSize: 13, fontWeight: 600, color: isExisting ? "#94a3b8" : "#0f172a" }}>{c.catalog}</span>
                                  {isExisting && <span style={s.chip("#e2e8f0", "#475569")}>already added</span>}
                                  <span style={{ fontSize: 11, color: "#94a3b8", marginLeft: "auto" }}>
                                    {c.schemas_enumerated ? `${schemaSel.size}/${c.schemas?.length ?? 0} schemas` : "all schemas (not enumerated)"}
                                  </span>
                                </div>
                                {isExp && c.schemas_enumerated && c.schemas && (
                                  <div style={{ padding: "2px 8px 6px 34px", display: "grid", gap: 1 }}>
                                    <div style={{ display: "flex", gap: 8, marginBottom: 2 }}>
                                      <button style={{ ...s.btn2, padding: "1px 7px" }} onClick={() => setCatSchemaSel((p) => ({ ...p, [c.catalog]: new Set(c.schemas!) }))}>all</button>
                                      <button style={{ ...s.btn2, padding: "1px 7px" }} onClick={() => setCatSchemaSel((p) => ({ ...p, [c.catalog]: new Set() }))}>none</button>
                                    </div>
                                    {c.schemas.map((sch) => (
                                      <label key={sch} style={{ fontSize: 12, color: "#334155", display: "inline-flex", gap: 6, alignItems: "center", cursor: "pointer" }}>
                                        <input type="checkbox" checked={schemaSel.has(sch)} onChange={() => toggleCatSchema(c.catalog, sch)} />
                                        {sch}
                                      </label>
                                    ))}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                        <button
                          style={{ ...s.btn, marginTop: 8, opacity: checkedCats.size === 0 || adding ? 0.5 : 1 }}
                          disabled={checkedCats.size === 0 || adding}
                          onClick={() => addSelectedSources(catalogs)}
                        >{adding ? "Adding…" : `Add selected (${checkedCats.size})`}</button>
                      </div>
                    );
                  })()}

                  {/* Catalog-scoped but none enumerated → free-text single add */}
                  {connId != null && catalogs != null && catalogs.length === 0 && !catLoading && (
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      <input style={{ ...s.input, marginBottom: 0, flex: 1 }} placeholder="Catalog (e.g. samples)" value={newCatalog} onChange={(e) => setNewCatalog(e.target.value)} />
                      <button style={{ ...s.btn, opacity: !newCatalog.trim() ? 0.5 : 1 }} disabled={!newCatalog.trim()} onClick={addSource}>Add</button>
                    </div>
                  )}

                  {/* Not catalog-scoped (2-level: postgres/mysql) → single add */}
                  {connId != null && catalogs == null && !catLoading && (
                    <button style={s.btn} onClick={addSource}>Add source</button>
                  )}

                  {catalogs != null && (
                    <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 6 }}>
                      This platform is catalog-scoped — one source per catalog. Tick catalogs (and optionally
                      their schemas) to add several at once.
                    </div>
                  )}
                  {addResults && addResults.length > 0 && (
                    <div style={{ marginTop: 6, display: "grid", gap: 2 }}>
                      {addResults.map((r, i) => (
                        <div key={i} style={{ fontSize: 11, color: r.ok ? "#166534" : "#b91c1c" }}>
                          {r.ok ? "✓" : "✗"} {r.catalog}{r.msg && r.msg !== "added" ? ` — ${r.msg}` : ""}
                        </div>
                      ))}
                    </div>
                  )}
                  </>)}
                </div>
              </div>

              {browseSource && (
                <div style={{ ...s.panel, marginBottom: 14 }}>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
                    <span style={s.label}>
                      Schemas in {browseSource.catalog || browseSource.name} — pick what to scan
                    </span>
                    <button style={{ ...s.btn2, padding: "2px 8px" }} onClick={() => { setBrowseId(null); setNsTree(null); }}>Close</button>
                  </div>
                  {nsLoading && <div style={{ fontSize: 12, color: "#64748b" }}>Loading schemas… (a serverless warehouse may take a moment to wake)</div>}
                  {nsTree && nsTree.length === 0 && <div style={{ fontSize: 12, color: "#94a3b8" }}>No schemas found for this catalog.</div>}
                  {nsTree && nsTree.length > 0 && (
                    <>
                      <div style={{ display: "flex", gap: 10, marginBottom: 6 }}>
                        <button style={{ ...s.btn2, padding: "2px 8px" }} onClick={() => setSelected(new Set(nsTree.map((n) => n.name)))}>Select all</button>
                        <button style={{ ...s.btn2, padding: "2px 8px" }} onClick={() => setSelected(new Set())}>Select none</button>
                        <span style={{ fontSize: 12, color: "#64748b", alignSelf: "center" }}>{selected.size} of {nsTree.length} selected</span>
                      </div>
                      <table style={{ borderCollapse: "collapse", width: "100%" }}>
                        <thead><tr><th style={s.th}></th><th style={s.th}>Schema</th><th style={s.th}>Relations</th></tr></thead>
                        <tbody>{nsTree.map((n) => (
                          <tr key={n.name}>
                            <td style={s.td}><input type="checkbox" checked={selected.has(n.name)} onChange={() => toggle(n.name)} /></td>
                            <td style={s.td}>{n.name}</td>
                            <td style={s.td}>{n.relation_count ?? "—"}</td>
                          </tr>
                        ))}</tbody>
                      </table>
                      <div style={{ display: "flex", gap: 8, marginTop: 10, alignItems: "center" }}>
                        <button style={s.btn2} onClick={() => saveSelection(browseSource)}>Save selection</button>
                        <button style={s.btn} onClick={() => launchScan(browseSource)}>Scan selected</button>
                        {saveMsg && <span style={{ fontSize: 12, color: "#166534" }}>{saveMsg}</span>}
                      </div>
                    </>
                  )}
                </div>
              )}

              <div style={s.panel}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
                  <span style={s.label}>Scans</span>
                  {scans.some((sc) => ["completed", "partial"].includes(sc.state)) && (
                    <button
                      style={{ ...s.btn2, fontSize: 12 }}
                      onClick={enrichEstate}
                      title="Generate LLM descriptions for all estate columns and tables to improve feasibility matching accuracy"
                    >
                      Enrich metadata
                    </button>
                  )}
                </div>
                {scans.map((sc) => {
                  const [bg, fg] = SCAN_CHIP[sc.state] || SCAN_CHIP.queued;
                  const src = sources.find((x) => x.id === sc.source_id) || null;
                  const isOpen = expanded.has(sc.id);
                  const hasDetail = ["completed", "partial", "failed"].includes(sc.state);
                  const enrichState = sc.enrichment_state;
                  return (
                    <div key={sc.id} style={{ padding: "8px 0", borderTop: "1px solid #eef2f7" }}>
                      <div
                        style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", cursor: hasDetail ? "pointer" : "default" }}
                        onClick={hasDetail ? () => toggleScanExpand(sc.id) : undefined}
                      >
                        <span style={{ fontSize: 12, color: "#94a3b8", width: 10 }}>{hasDetail ? (isOpen ? "▾" : "▸") : ""}</span>
                        <span style={{ fontSize: 13, fontWeight: 700 }}>v{sc.scan_version}</span>
                        <span style={s.chip(bg, fg)}>{sc.state}</span>
                        {src && (
                          <span style={{ fontSize: 12, color: "#334155", fontWeight: 600 }}>
                            {src.name}
                            {src.catalog ? <span style={{ color: "#7c3aed" }}> · {src.catalog}</span> : null}
                          </span>
                        )}
                        <span style={{ fontSize: 12, color: "#64748b", marginLeft: "auto" }}>
                          {sc.stats?.datasets ?? 0} datasets · {sc.stats?.columns ?? 0} columns
                          {sc.stats?.total_size_bytes ? ` · ${fmtBytes(sc.stats.total_size_bytes)}` : ""}
                          {sc.stats?.code_assets_written ? ` · ${sc.stats.code_assets_written} assets` : ""}
                          {sc.stats?.new ? ` · +${sc.stats.new} new` : ""}
                          {sc.stats?.deleted ? ` · ${sc.stats.deleted} tombstoned` : ""}
                        </span>
                      </div>
                      {sc.state === "running" && renderScanProgress(sc.scan_progress)}
                      {sc.finished_at && ["completed", "partial", "failed"].includes(sc.state) && (
                        <div style={{ fontSize: 11, color: "#64748b", marginTop: 3, marginLeft: 18 }}>
                          Scanned {new Date(sc.finished_at).toLocaleString()}
                          {fmtDuration(sc.started_at, sc.finished_at) ? ` · ${fmtDuration(sc.started_at, sc.finished_at)}` : ""}
                        </div>
                      )}
                      {enrichState === "enriching" && renderEnrichProgress(sc.enrichment_progress)}
                      {enrichState === "enriched" && (
                        <div style={{ fontSize: 11, color: "#166534", marginTop: 3, marginLeft: 18 }}>
                          ✓ Metadata enriched
                          {sc.enriched_at ? ` · ${new Date(sc.enriched_at).toLocaleString()}` : ""}
                        </div>
                      )}
                      {enrichState === "failed" && (
                        <div style={{ fontSize: 11, color: "#b91c1c", marginTop: 3, marginLeft: 18 }}>
                          Enrichment failed — check logs. You can retry from the "Enrich metadata" button.
                        </div>
                      )}
                      {isOpen && <div style={{ marginTop: 8, marginLeft: 18 }}>{renderScanDetail(scanDetail[sc.id], sc)}</div>}
                    </div>
                  );
                })}
                {scans.length === 0 && <div style={{ fontSize: 12, color: "#94a3b8" }}>No scans yet — add a source and click Scan.</div>}
              </div>
            </>
          )}
        </div>
      </div>
      {descModal && (
        <EstateTableDescriptionsModal dataset={descModal} onClose={() => setDescModal(null)} />
      )}

      {importSource && (
        <div style={s.modalBackdrop} onClick={() => !importing && setImportSource(null)}>
          <div style={s.modalCard} onClick={(e) => e.stopPropagation()}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <span style={{ fontSize: 15, fontWeight: 700 }}>Import extraction manifest → {importSource.name}</span>
              <button style={s.btn2} onClick={() => setImportSource(null)}>Close</button>
            </div>
            <div style={{ fontSize: 12, color: "#64748b", marginBottom: 10 }}>
              {importFile ? <>File: <code>{importFile.name}</code></> : "Choose the estate-manifest-<ts>.yaml your client produced."}
            </div>
            {importErr && <div style={{ fontSize: 12, color: "#b91c1c", marginBottom: 8 }}>{importErr}</div>}
            {importPreview && (
              <div style={{ marginBottom: 12 }}>
                <div style={{ fontSize: 12, color: "#334155", marginBottom: 6 }}>
                  <b>{importPreview.platform}</b>{importPreview.catalog ? ` · ${importPreview.catalog}` : ""} · <b>{importPreview.depth}</b> scan
                </div>
                <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13, marginBottom: 8 }}>
                  <span>{importPreview.counts.schemas} schemas</span>
                  <span>{importPreview.counts.relations} relations</span>
                  <span>{importPreview.counts.columns} columns</span>
                  <span>{importPreview.counts.fk_edges} FK edges</span>
                  <span>{importPreview.counts.profiled_columns} profiled</span>
                </div>
                <div style={{ fontSize: 12, color: "#64748b", marginBottom: 6 }}>
                  Redaction (values withheld):{" "}
                  {Object.keys(importPreview.redaction || {}).length
                    ? Object.entries(importPreview.redaction).map(([k, v]) => `${k}: ${v}`).join(" · ")
                    : "none"}
                  {" · "}{importPreview.counts.pii_columns} PII-flagged column{importPreview.counts.pii_columns === 1 ? "" : "s"}
                </div>
                {importPreview.warnings?.length > 0 && (
                  <div style={{ fontSize: 12, color: "#b45309" }}>
                    ⚠ {importPreview.warnings.slice(0, 5).join("; ")}
                  </div>
                )}
                <div style={{ maxHeight: 160, overflowY: "auto", border: "1px solid #e2e8f0", borderRadius: 6, marginTop: 8 }}>
                  {importPreview.schemas.map((sc) => (
                    <div key={sc.schema} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, padding: "3px 8px", borderBottom: "1px solid #f1f5f9" }}>
                      <span style={{ fontWeight: 600 }}>{sc.schema}</span>
                      <span style={{ color: "#64748b" }}>{sc.relations} tables · {sc.columns} cols</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button style={s.btn2} onClick={() => chooseManifest(importSource)}>Choose a different file…</button>
              <button style={{ ...s.btn, opacity: importPreview && !importing ? 1 : 0.5 }}
                disabled={!importPreview || importing} onClick={confirmImport}>
                {importing ? "Importing…" : "Confirm import"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
