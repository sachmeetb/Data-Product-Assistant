// Top-down feasibility: pick an estate + a domain, choose which reference specs
// to evaluate, then read the stoplight grid. Independent of the Pulse surface.
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import api from "../../api/client";
import FeasibilityGrid, { type FeasibilityScore } from "../../components/FeasibilityGrid";

interface Estate { id: number; name: string; domain: string | null }
interface Scan { id: number; scan_version: number; state: string; stats: any; source_id: number; enrichment_state?: string | null }
interface Source { id: number; name: string; platform: string; catalog: string; enabled: boolean }
interface Progress {
  stage?: string; specs_total?: number; specs_done?: number;
  domains_total?: number; domains_done?: number;
  current_spec?: string | null; current_domain?: string | null;
  updated_at?: string; error?: string;
}
interface Run {
  id: number; state: string; used_skill: boolean; domain: string | null;
  corpus_version: string; skill_version: string; embedding_model: string;
  summary: any; scores?: FeasibilityScore[]; scan_ids?: number[]; spec_ids?: string[];
  schema_scoping?: { enabled?: boolean; floor?: number; cap?: number };
  scoring?: { scoring_version?: string; table_affinity_weight?: number; match_threshold?: number };
  progress?: Progress;
  created_at?: string;
}

const STAGE_LABEL: Record<string, string> = {
  shortlisting: "Shortlisting schemas",
  building_evidence: "Building evidence",
  deriving: "Proposing derivations",
  evaluating: "Evaluating specs",
  finalizing: "Finalizing",
  completed: "Completed",
  failed: "Failed",
};
interface SpecEntry {
  spec_id: string; name: string; domain: string; product_kind: string;
  description: string; required_count: number; total_count: number;
}

// Calmer tally palette matching FeasibilityGrid's TIER lightBg/lightFg.
const TALLY: Record<string, [string, string, string]> = {
  ready:       ["#ecfdf5", "#065f46", "A published product already covers the required attributes — adopt it directly."],
  adaptable:   ["#e0f2fe", "#0369a1", "A published product covers most attributes; a bounded transformation closes the gap."],
  assemblable: ["#fef3c7", "#78350f", "Raw estate data covers the requirements but isn't a governed product yet — needs engineering."],
  absent:      ["#f1f5f9", "#475569", "No published product or estate raw data covers this spec's required attributes."],
};

const s: Record<string, any> = {
  page: { maxWidth: 1000, margin: "0 auto", padding: "8px 0" },
  h1: { fontSize: 22, fontWeight: 800, color: "#0f172a", marginBottom: 2 },
  sub: { fontSize: 13, color: "#64748b", marginBottom: 16 },
  bar: { display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 16 },
  field: { display: "flex", flexDirection: "column", gap: 4 },
  label: { fontSize: 11, color: "#64748b", fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.3 },
  select: { fontSize: 13, padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 6, background: "#fff", minWidth: 180 },
  btn: { fontSize: 13, fontWeight: 700, padding: "8px 16px", borderRadius: 6, border: "none", background: "#7c3aed", color: "#fff", cursor: "pointer" },
  tallies: { display: "flex", gap: 8, marginBottom: 14, flexWrap: "wrap" },
  tally: (bg: string, fg: string): CSSProperties => ({ fontSize: 12, fontWeight: 700, padding: "4px 10px", borderRadius: 6, background: bg, color: fg, cursor: "default" }),
  meta: { fontSize: 11, color: "#94a3b8", marginBottom: 12 },
  pickerBox: { border: "1px solid #e2e8f0", borderRadius: 8, background: "#f8fafc", padding: "12px 14px", marginBottom: 16 },
  pickerHeader: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 },
  pickerTitle: { fontSize: 13, fontWeight: 700, color: "#0f172a" },
  pickerHint: { fontSize: 11, color: "#94a3b8" },
  domainGroup: { marginBottom: 10 },
  // Domain labels use structural slate — purple reserved for interactive CTAs.
  domainLabel: { fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: 0.4, marginBottom: 4 },
  specRow: { display: "flex", alignItems: "flex-start", gap: 8, marginBottom: 4, cursor: "pointer" },
  specName: { fontSize: 12, color: "#1e293b", fontWeight: 600 },
  specMeta: { fontSize: 11, color: "#94a3b8" },
  selActions: { display: "flex", gap: 8 },
  selBtn: { fontSize: 11, color: "#7c3aed", background: "none", border: "none", cursor: "pointer", padding: 0, fontWeight: 600 },
  // Loading cue for the graph-backed definitions list.
  loadingChip: { display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, fontWeight: 600, color: "#7c3aed" },
  spinner: { width: 12, height: 12, borderRadius: "50%", border: "2px solid #ddd6fe", borderTopColor: "#7c3aed", animation: "feas-spin 0.8s linear infinite", display: "inline-block", flexShrink: 0 },
  skelRow: { height: 12, borderRadius: 4, marginBottom: 9, background: "linear-gradient(90deg, #eef2f7 25%, #e2e8f0 37%, #eef2f7 63%)", backgroundSize: "400% 100%", animation: "feas-shimmer 1.4s ease infinite" } as CSSProperties,
  // Run history
  historyBox: { border: "1px solid #e2e8f0", borderRadius: 8, background: "#f8fafc", padding: "10px 14px", marginBottom: 16 },
  historyTitle: { fontSize: 12, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: 0.3, marginBottom: 8 },
  historyRow: (active: boolean): CSSProperties => ({
    display: "flex", alignItems: "center", gap: 8, padding: "6px 8px", borderRadius: 6, cursor: "pointer",
    background: active ? "#ede9fe" : "transparent", marginBottom: 2,
  }),
  historyRowDate: { fontSize: 11, color: "#64748b", minWidth: 72 },
  historyRowMeta: { fontSize: 11, color: "#94a3b8", flex: 1 },
};

export default function FeasibilityPage() {
  const [estates, setEstates] = useState<Estate[]>([]);
  const [estateId, setEstateId] = useState<number | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [scans, setScans] = useState<Scan[]>([]);
  const [domains, setDomains] = useState<string[]>([]);
  const [domain, setDomain] = useState<string>("");
  const [specIndex, setSpecIndex] = useState<SpecEntry[]>([]);
  // True while the reference-spec catalog is being read from the graph (initial
  // mount + every domain change) — drives the skeleton / "Refreshing…" cue.
  const [specsLoading, setSpecsLoading] = useState(false);
  const [selectedSpecIds, setSelectedSpecIds] = useState<Set<string>>(new Set());
  const [run, setRun] = useState<Run | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>("");
  // Tracks spec_ids that are saved candidates (for the save toggle).
  const [savedSpecIds, setSavedSpecIds] = useState<Set<string>>(new Set());
  const [assemblies, setAssemblies] = useState<any[]>([]);
  // Tiered schema-scoping levers (Stage 1: product → schema shortlist).
  const [scopeToSchemas, setScopeToSchemas] = useState(true);
  const [schemaFloor, setSchemaFloor] = useState(45);
  const [schemaThreshold, setSchemaThreshold] = useState(70);
  const [maxSchemas, setMaxSchemas] = useState(5);
  // AI "recommend definitions" note + busy flag.
  const [recommending, setRecommending] = useState(false);
  const [recommendNote, setRecommendNote] = useState<string>("");
  // When a recommend widens the domain to "all", the index reload should apply
  // THESE ids instead of its default select-all.
  const pendingRecommendRef = useRef<string[] | null>(null);
  // Live evaluation progress (interval-polled) + poll lifecycle refs.
  const [progress, setProgress] = useState<Progress | null>(null);
  const pollTimer = useRef<number | null>(null);
  const polling = useRef(false);        // guard against overlapping requests
  const activeRunId = useRef<number | null>(null);

  useEffect(() => {
    api.get("/api/estates").then((r) => setEstates(r.data.estates || [])).catch(() => {});
    api.get("/api/feasibility/specs").then((r) => setDomains(r.data.domains || [])).catch(() => {});
  }, []);

  // Load the lightweight spec index whenever domain changes.
  useEffect(() => {
    const params = domain ? `?domain=${encodeURIComponent(domain)}` : "";
    setSpecsLoading(true);
    api.get(`/api/feasibility/index${params}`).then((r) => {
      const specs: SpecEntry[] = r.data.specs || [];
      setSpecIndex(specs);
      if (pendingRecommendRef.current) {
        const want = new Set(pendingRecommendRef.current);
        setSelectedSpecIds(new Set(specs.filter((sp) => want.has(sp.spec_id)).map((sp) => sp.spec_id)));
        pendingRecommendRef.current = null;
      } else {
        setSelectedSpecIds(new Set(specs.map((s) => s.spec_id)));
      }
    }).catch(() => {}).finally(() => setSpecsLoading(false));
  }, [domain]);

  useEffect(() => {
    if (estateId == null) { setSources([]); setScans([]); setRuns([]); setRun(null); setSelectedRunId(null); return; }
    api.get(`/api/estates/${estateId}`).then((r) => {
      setSources(r.data.sources || []);
      setScans((r.data.scans || []).filter((x: Scan) => ["completed", "partial"].includes(x.state)));
    }).catch(() => {});
    // Load run history for this estate.
    api.get(`/api/feasibility/runs`, { params: { estate_id: estateId } }).then((r) => {
      const allRuns: Run[] = r.data.runs || [];
      setRuns(allRuns.slice(0, 10));
      // Auto-select the most recent completed/partial run.
      const latest = allRuns.find((run) => ["completed", "partial"].includes(run.state));
      if (latest) {
        loadRun(latest.id);
      } else {
        setRun(null);
        setSelectedRunId(null);
      }
    }).catch(() => {});
  }, [estateId]);

  async function loadRun(runId: number) {
    setSelectedRunId(runId);
    try {
      const r = await api.get(`/api/feasibility/runs/${runId}`);
      setRun(r.data);
    } catch { /* ignore */ }
  }

  const assessed = useMemo(() => {
    const bySource = new Map<number, Scan>();
    for (const sc of scans) {
      const prev = bySource.get(sc.source_id);
      if (!prev || sc.scan_version > prev.scan_version) bySource.set(sc.source_id, sc);
    }
    return sources
      .filter((src) => src.enabled && bySource.has(src.id))
      .map((src) => ({ src, scan: bySource.get(src.id)! }));
  }, [sources, scans]);

  // Group spec index by domain for the picker.
  const specsByDomain = useMemo(() => {
    const map = new Map<string, SpecEntry[]>();
    for (const spec of specIndex) {
      const d = spec.domain || "Other";
      if (!map.has(d)) map.set(d, []);
      map.get(d)!.push(spec);
    }
    return map;
  }, [specIndex]);

  function toggleSpec(specId: string) {
    setSelectedSpecIds((prev) => {
      const next = new Set(prev);
      if (next.has(specId)) next.delete(specId); else next.add(specId);
      return next;
    });
  }
  function selectAll() { setSelectedSpecIds(new Set(specIndex.map((s) => s.spec_id))); }
  function selectNone() { setSelectedSpecIds(new Set()); }

  async function recommend() {
    if (estateId == null || recommending) return;
    setRecommending(true); setErr(""); setRecommendNote("");
    try {
      const r = await api.post("/api/feasibility/recommend-specs", {
        estate_id: estateId,
        domain: domain || null,
      });
      const ids: string[] = r.data.recommended_spec_ids || [];
      const total: number = r.data.spec_count ?? specIndex.length;
      // Recommendations span every domain — widen the picker so the pre-checks show.
      if (domain) {
        // The index reload triggered by the domain change applies these ids.
        pendingRecommendRef.current = ids;
        setDomain("");
      } else {
        setSelectedSpecIds(new Set(ids));
      }
      setRecommendNote(
        `${r.data.used_skill ? "AI" : "Heuristic"} recommended ${ids.length} of ${total} definition${total === 1 ? "" : "s"} from your estate.`
      );
    } catch (e: any) {
      setErr(e?.response?.data?.detail?.message || e?.response?.data?.message || "Recommendation failed.");
    } finally {
      setRecommending(false);
    }
  }

  const canEvaluate = !busy && assessed.length > 0 && selectedSpecIds.size > 0;

  function stopPolling() {
    if (pollTimer.current != null) { window.clearInterval(pollTimer.current); pollTimer.current = null; }
    polling.current = false;
    activeRunId.current = null;
  }

  // One poll tick — guarded so overlapping/late responses (or a run switch mid-
  // flight) can't clobber state. Stops cleanly on completed | partial | failed.
  async function pollTick(runId: number) {
    if (polling.current || activeRunId.current !== runId) return;
    polling.current = true;
    try {
      const r = await api.get(`/api/feasibility/runs/${runId}`);
      if (activeRunId.current !== runId) return;  // a newer run took over while awaiting
      setProgress(r.data.progress || null);
      if (["completed", "partial", "failed"].includes(r.data.state)) {
        stopPolling();
        setRun(r.data);
        setSelectedRunId(runId);
        setBusy(false);
        setProgress(null);
        if (r.data.estate_id != null) {
          api.get(`/api/feasibility/runs`, { params: { estate_id: r.data.estate_id } })
            .then((rr) => setRuns((rr.data.runs || []).slice(0, 10))).catch(() => {});
        }
        if (r.data.state === "failed") {
          setErr(r.data.error?.error || r.data.progress?.error || "Evaluation failed.");
        }
      }
    } catch { /* transient — keep polling */ }
    finally { polling.current = false; }
  }

  function startPolling(runId: number) {
    stopPolling();
    activeRunId.current = runId;
    void pollTick(runId);  // immediate first read so the bar shows at once
    pollTimer.current = window.setInterval(() => { void pollTick(runId); }, 1500);
  }

  // Tear down the poll on unmount so a completed page never leaks a timer.
  useEffect(() => () => stopPolling(), []);
  // A change of estate abandons any in-flight poll (the run belongs to the old
  // estate); the estate-change effect already resets run/busy state.
  useEffect(() => { stopPolling(); setBusy(false); setProgress(null); }, [estateId]);

  async function evaluate() {
    if (!canEvaluate || estateId == null) return;
    setBusy(true); setErr(""); setRun(null); setProgress(null);
    try {
      const r = await api.post("/api/feasibility/evaluate", {
        estate_id: estateId,
        domain: domain || null,
        spec_ids: Array.from(selectedSpecIds),
        scope_to_schemas: scopeToSchemas,
        schema_relevance_floor: schemaFloor,
        schema_shortlist_threshold: schemaThreshold,
        max_schemas_per_spec: maxSchemas,
      });
      startPolling(r.data.id);
    } catch (e: any) {
      setErr(e?.response?.data?.detail?.message || e?.response?.data?.message || "Evaluation failed to start.");
      setBusy(false);
    }
  }

  function renderProgress() {
    if (!busy) return null;
    const p = progress || {};
    const stage = p.stage || "shortlisting";
    const total = p.specs_total || 0;
    const done = p.specs_done || 0;
    const frac = total > 0 ? done / total : 0;
    const pct = Math.max(4, Math.min(100, Math.round(frac * 100)));
    return (
      <div style={{ marginBottom: 14, maxWidth: 520 }}>
        <div style={{ fontSize: 12, color: "#5b21b6", marginBottom: 4 }}>
          {STAGE_LABEL[stage] || stage}
          {total > 0 ? ` · ${done}/${total} specs` : ""}
          {p.domains_total ? ` · ${p.domains_done ?? 0}/${p.domains_total} domains` : ""}
          {p.current_domain ? ` · ${p.current_domain}` : ""}
          {p.current_spec ? ` · ${p.current_spec}` : ""}
        </div>
        <div style={{ height: 8, borderRadius: 4, background: "#ede9fe", overflow: "hidden" }}>
          <div style={{ width: `${pct}%`, height: "100%", background: "#7c3aed", transition: "width 0.4s ease" }} />
        </div>
      </div>
    );
  }

  const tallies = useMemo(() => run?.summary?.tier_counts || {}, [run]);

  // Load candidates so the save toggle starts in the right state.
  useEffect(() => {
    api.get("/api/feasibility/candidates").then((r) => {
      const ids = new Set<string>((r.data.candidates || []).map((c: any) => c.spec_id as string));
      setSavedSpecIds(ids);
    }).catch(() => {});
    // In-progress assembly workspaces (saved but not yet scaffolded).
    api.get("/api/assembly").then((r) => setAssemblies(r.data.assemblies || [])).catch(() => {});
  }, []);

  function handleSave(specId: string, saved: boolean) {
    setSavedSpecIds((prev) => {
      const next = new Set(prev);
      if (saved) next.add(specId); else next.delete(specId);
      return next;
    });
  }

  return (
    <div style={s.page}>
      <style>{`@keyframes feas-spin { to { transform: rotate(360deg); } } @keyframes feas-shimmer { 0% { background-position: 100% 0; } 100% { background-position: 0 0; } }`}</style>
      <div style={s.h1}>Data-Product Feasibility</div>
      <div style={s.sub}>
        Which of your reference data products can you build right now? Select an estate, a domain,
        and the specific product definitions to evaluate — then assess across{" "}
        <strong>every catalog in the estate</strong> + the marketplace.
      </div>

      {assemblies.length > 0 && (
        <div style={{ border: "1px solid #ddd6fe", background: "#f5f3ff", borderRadius: 10, padding: "10px 14px", marginBottom: 14 }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: "#5b21b6", marginBottom: 6 }}>Products being assembled ({assemblies.length})</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {assemblies.map((a: any) => (
              <a key={a.id} href={`/product/assembly/${a.id}`} style={{ textDecoration: "none", border: "1px solid #ddd6fe", background: "#fff", borderRadius: 8, padding: "6px 10px", fontSize: 12, color: "#334155" }}>
                <span style={{ fontWeight: 700, color: "#5b21b6" }}>{a.spec_name}</span>
                <span style={{ color: "#94a3b8" }}> · {a.domain || "—"} · {a.status}</span>
                <span style={{ color: "#7c3aed", marginLeft: 6 }}>Resume →</span>
              </a>
            ))}
          </div>
        </div>
      )}

      <div style={s.bar}>
        <div style={s.field}>
          <span style={s.label}>Estate</span>
          <select style={s.select} value={estateId ?? ""} onChange={(e) => setEstateId(e.target.value ? Number(e.target.value) : null)}>
            <option value="">Select an estate…</option>
            {estates.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
        </div>
        <div style={s.field}>
          <span style={s.label}>Domain</span>
          <select style={s.select} value={domain} onChange={(e) => setDomain(e.target.value)}>
            <option value="">All domains</option>
            {domains.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
        <div style={s.field}>
          <span
            style={s.label}
            title="Hard relevance cutoff (0–100%): a schema scoring below this is ignored entirely and contributes no columns. Also governs 'no relevant schema → absent'. Lower it if a product comes back 'absent' with no relevant schema."
          >Schema floor</span>
          <div style={{ display: "flex", alignItems: "center", gap: 8, opacity: scopeToSchemas ? 1 : 0.5 }}>
            <input
              type="range" min={0} max={100} step={5}
              style={{ width: 120 }}
              value={schemaFloor}
              disabled={!scopeToSchemas}
              onChange={(e) => setSchemaFloor(Math.max(0, Math.min(100, Number(e.target.value) || 0)))}
            />
            <span style={{ fontSize: 12, fontWeight: 700, color: "#475569", minWidth: 34 }}>{schemaFloor}%</span>
          </div>
        </div>
        <div style={s.field}>
          <span
            style={s.label}
            title="Confident-relevance bar (0–100%): schemas at/above this are shortlisted and evaluated (up to Max). If none reach it, the single best above-floor schema is still evaluated at low confidence."
          >Shortlist ≥</span>
          <div style={{ display: "flex", alignItems: "center", gap: 8, opacity: scopeToSchemas ? 1 : 0.5 }}>
            <input
              type="range" min={0} max={100} step={5}
              style={{ width: 120 }}
              value={schemaThreshold}
              disabled={!scopeToSchemas}
              onChange={(e) => setSchemaThreshold(Math.max(0, Math.min(100, Number(e.target.value) || 0)))}
            />
            <span style={{ fontSize: 12, fontWeight: 700, color: "#475569", minWidth: 34 }}>{schemaThreshold}%</span>
          </div>
        </div>
        <div style={s.field}>
          <span style={s.label} title="Ceiling on how many strong (at/above the shortlist threshold) schemas are shortlisted per product — cross-domain products legitimately keep several.">Max schemas</span>
          <input
            type="number" min={1} max={20} step={1}
            style={{ ...s.select, minWidth: 70, opacity: scopeToSchemas ? 1 : 0.5 }}
            value={maxSchemas}
            disabled={!scopeToSchemas}
            onChange={(e) => setMaxSchemas(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
          />
        </div>
        <label
          style={{ ...s.field, flexDirection: "row", alignItems: "center", gap: 6, alignSelf: "flex-end", paddingBottom: 8, cursor: "pointer" }}
          title="When on, each product is matched to its relevant schemas first, then columns are assigned only within those — so an unrelated schema can't produce false-positive matches. Off restores whole-estate matching."
        >
          <input type="checkbox" checked={scopeToSchemas} onChange={(e) => setScopeToSchemas(e.target.checked)} />
          <span style={{ fontSize: 12, color: "#475569", fontWeight: 600 }}>Scope to matched schemas</span>
        </label>
        <div style={{ alignSelf: "flex-end", display: "flex", gap: 8 }}>
          <button
            style={{ ...s.btn, opacity: canEvaluate ? 1 : 0.5 }}
            disabled={!canEvaluate}
            title={selectedSpecIds.size === 0 ? "Select at least one product definition to evaluate" : undefined}
            onClick={evaluate}
          >
            {busy ? "Evaluating…" : `Evaluate (${selectedSpecIds.size} spec${selectedSpecIds.size !== 1 ? "s" : ""})`}
          </button>
          {run != null && (
            <button
              style={{ ...s.btn, background: "#f1f5f9", color: "#475569" }}
              onClick={() => { stopPolling(); setBusy(false); setProgress(null); setRun(null); setSelectedRunId(null); }}
              title="Clear results and start a new evaluation"
            >
              ▶ New evaluation
            </button>
          )}
        </div>
      </div>

      {estateId != null && (
        <div style={{ fontSize: 12, color: "#64748b", marginBottom: 12 }}>
          {assessed.length === 0
            ? "No completed scans yet — scan at least one source on the Connected Estate page first."
            : <>Will assess {assessed.length} catalog{assessed.length === 1 ? "" : "s"}:{" "}
                {assessed.map(({ src, scan }) => `${src.catalog || src.name} (v${scan.scan_version})`).join(" · ")}</>}
        </div>
      )}
      {assessed.length > 0 && assessed.some(({ scan }) => !scan.enrichment_state || scan.enrichment_state === "failed") && (
        <div style={{ fontSize: 12, color: "#78350f", background: "#fef3c7", border: "1px solid #fcd34d", borderRadius: 6, padding: "8px 12px", marginBottom: 12 }}>
          Metadata descriptions haven't been generated yet — matching accuracy improves significantly after enrichment.
          {" "}<a href="/product/estate" style={{ color: "#92400e", fontWeight: 700 }}>Enrich the estate first</a> for better results, or evaluate now with name-only matching.
        </div>
      )}

      {/* Loading cue — the reference-spec catalog is read live from the graph, which
          takes a beat; show a skeleton so the picker isn't a silent blank gap while
          the definitions stream in. */}
      {specsLoading && specIndex.length === 0 && (
        <div style={s.pickerBox} aria-busy="true">
          <div style={s.pickerHeader}>
            <span style={s.pickerTitle}>Product definitions to evaluate</span>
            <span style={s.loadingChip}>
              <span style={s.spinner} />
              Loading definitions from the graph…
            </span>
          </div>
          {[68, 54, 61, 47, 58].map((w, i) => (
            <div key={i} style={{ ...s.skelRow, width: `${w}%` }} />
          ))}
        </div>
      )}

      {/* Spec picker */}
      {specIndex.length > 0 && (
        <div style={s.pickerBox}>
          <div style={s.pickerHeader}>
            <span style={s.pickerTitle}>
              Product definitions to evaluate
              {specsLoading && (
                <span style={{ ...s.loadingChip, marginLeft: 8 }}>
                  <span style={s.spinner} />
                  Refreshing…
                </span>
              )}
            </span>
            <div style={s.selActions}>
              <button
                style={{ ...s.selBtn, opacity: estateId == null || recommending ? 0.5 : 1 }}
                disabled={estateId == null || recommending}
                onClick={recommend}
                title="Let AI read your enriched estate and pre-check the definitions worth evaluating"
              >
                {recommending ? "Recommending…" : "✨ Recommend definitions"}
              </button>
              <span style={{ color: "#cbd5e1" }}>·</span>
              <button style={s.selBtn} onClick={selectAll}>Select all</button>
              <span style={{ color: "#cbd5e1" }}>·</span>
              <button style={s.selBtn} onClick={selectNone}>Clear</button>
              <span style={{ color: "#94a3b8", fontSize: 11 }}>{selectedSpecIds.size} / {specIndex.length} selected</span>
            </div>
          </div>
          {recommendNote && (
            <div style={{ fontSize: 12, color: "#5b21b6", background: "#f5f3ff", border: "1px solid #ddd6fe", borderRadius: 6, padding: "6px 10px", marginBottom: 10 }}>
              {recommendNote}
            </div>
          )}
          {Array.from(specsByDomain.entries()).map(([dom, specs]) => (
            <div key={dom} style={s.domainGroup}>
              <div style={s.domainLabel}>{dom}</div>
              {specs.map((spec) => (
                <label key={spec.spec_id} style={s.specRow}>
                  <input
                    type="checkbox"
                    checked={selectedSpecIds.has(spec.spec_id)}
                    onChange={() => toggleSpec(spec.spec_id)}
                    style={{ marginTop: 2, flexShrink: 0 }}
                  />
                  <div>
                    <div style={s.specName}>{spec.name}</div>
                    <div style={s.specMeta}>
                      {spec.product_kind} · {spec.required_count} required / {spec.total_count} total attributes
                      {spec.description ? ` · ${spec.description.slice(0, 80)}${spec.description.length > 80 ? "…" : ""}` : ""}
                    </div>
                  </div>
                </label>
              ))}
            </div>
          ))}
        </div>
      )}

      {selectedSpecIds.size === 0 && specIndex.length > 0 && (
        <div style={{ color: "#b45309", fontSize: 12, marginBottom: 10 }}>
          Select at least one product definition before evaluating.
        </div>
      )}

      {err && <div style={{ color: "#991b1b", marginBottom: 12 }}>{err}</div>}
      {renderProgress()}

      {/* Run history */}
      {runs.length > 0 && (
        <div style={s.historyBox}>
          <div style={s.historyTitle}>Previous evaluations</div>
          {runs.map((r) => {
            const tc = r.summary?.tier_counts || {};
            const dateStr = r.created_at ? new Date(r.created_at).toLocaleDateString() : "—";
            const isActive = r.id === selectedRunId;
            return (
              <div
                key={r.id}
                style={s.historyRow(isActive)}
                onClick={() => { if (!isActive) loadRun(r.id); }}
                title={isActive ? "Currently displayed" : "View this run's results"}
              >
                <span style={s.historyRowDate}>{dateStr}</span>
                <span style={s.historyRowMeta}>
                  {r.domain || "all domains"} · {(r.spec_ids?.length ?? r.summary?.spec_count ?? 0)} specs
                </span>
                <span style={{ display: "flex", gap: 4 }}>
                  {["ready", "adaptable", "assemblable", "absent"].map((t) => tc[t] != null && tc[t] > 0 ? (
                    <span key={t} style={{ fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 4, background: TALLY[t][0], color: TALLY[t][1] }}>
                      {t.slice(0, 3)} {tc[t]}
                    </span>
                  ) : null)}
                </span>
                {isActive && <span style={{ fontSize: 10, color: "#7c3aed", fontWeight: 700, marginLeft: 4 }}>▶ Viewing</span>}
              </div>
            );
          })}
        </div>
      )}

      {run && (
        <>
          <div style={s.tallies}>
            {["ready", "adaptable", "assemblable", "absent"].map((t) => (
              <span
                key={t}
                style={s.tally(TALLY[t][0], TALLY[t][1])}
                title={TALLY[t][2]}
              >
                {t.charAt(0).toUpperCase() + t.slice(1)} · {tallies[t] || 0}
              </span>
            ))}
          </div>
          <div style={s.meta}>
            {run.corpus_version === "graph" ? "corpus: graph-live" : `corpus v${run.corpus_version}`} · {run.used_skill ? `skill ${run.skill_version}` : "heuristic (no skill)"} · embeddings: {run.embedding_model}
            {run.scan_ids?.length ? ` · ${run.scan_ids.length} catalog${run.scan_ids.length === 1 ? "" : "s"} assessed` : ""}
            {run.spec_ids?.length ? ` · ${run.spec_ids.length} spec${run.spec_ids.length !== 1 ? "s" : ""} evaluated` : ""}
            {run.schema_scoping?.enabled === false
              ? " · whole-estate matching"
              : run.schema_scoping
                ? ` · schema floor ${run.schema_scoping.floor} · max ${run.schema_scoping.cap}`
                : ""}
            {run.scoring?.scoring_version ? ` · scoring ${run.scoring.scoring_version}` : ""}
            {" "}· run #{run.id}
          </div>
          {Array.isArray(run.summary?.description_coverage?.weak_schemas)
            && run.summary.description_coverage.weak_schemas.length > 0 && (
            <div
              title="These estate schemas have missing or all-empty column descriptions (a failed or skipped enrichment). Matching accuracy is degraded for them — re-run enrichment to strengthen it."
              style={{
                fontSize: 12, color: "#92400e", background: "#fffbeb",
                border: "1px solid #fde68a", borderRadius: 6, padding: "6px 10px",
                margin: "8px 0",
              }}
            >
              <strong>Description quality:</strong> {run.summary.description_coverage.weak_schemas.length}{" "}
              schema(s) have weak/missing descriptions (re-enrich recommended):{" "}
              {run.summary.description_coverage.weak_schemas.join(", ")}
            </div>
          )}
          <FeasibilityGrid
            runId={run.id}
            scores={run.scores || []}
            savedSpecIds={savedSpecIds}
            onSave={handleSave}
            runMeta={{
              scoring_version: run.scoring?.scoring_version,
              corpus_version: run.corpus_version,
              skill_version: run.used_skill ? run.skill_version : "",
              embedding_model: run.embedding_model,
              scan_ids: run.scan_ids,
              created_at: run.created_at,
              summary: run.summary,
            }}
          />
        </>
      )}
    </div>
  );
}
