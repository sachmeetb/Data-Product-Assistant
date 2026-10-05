// Discovery tab — the single home for *building* the concept layer.
//
// Consolidates the three concept-generation steps (previously scattered across
// the Concepts + Recommendations tabs) into one well-defined, ordered sequence
// with run-status, last-run timestamps, staleness warnings, a clear-out action,
// and a post-run validation panel (concept counts + stranded concepts).
//
// Backend: GET/POST /api/semantic/discovery/{status,run,reset}.

import { useCallback, useEffect, useState, type CSSProperties } from "react";
import api from "../api/client";
import { useConfirm } from "./dialogContext";

interface StepStatus {
  step: "scaffold" | "recommend" | "enrich";
  has_run: boolean;
  status: string;
  last_run_at: string | null;
  duration_ms: number | null;
  triggered_by: string | null;
  stats: Record<string, unknown>;
  error: string | null;
  stale: boolean;
  stale_reasons: string[];
}

interface StrandedItem {
  uri: string;
  name: string;
  level: string;
}

interface DiscoveryStatus {
  domain: string;
  steps: StepStatus[];
  concept_counts: { entity: number; attribute: number; value: number; total: number };
  stranded: { items: StrandedItem[]; counts: Record<string, number>; total: number };
  source_fingerprint: Record<string, unknown>;
  last_reset_at: string | null;
  confidence_threshold: number;
}

interface Props {
  availableDomains: string[];
}

const STEP_META: Record<
  string,
  { n: number; title: string; verb: string; description: string }
> = {
  scaffold: {
    n: 1,
    title: "Derive entities from schema",
    verb: "Derive",
    description:
      "Deterministically builds the entity → attribute → value tree directly from this domain's data-product schema, binding each entity to its table(s), each attribute to its column(s), and seeding FK relationships. Run this first. Re-running deprecates the current concepts and rebuilds from scratch.",
  },
  recommend: {
    n: 2,
    title: "Find cross-product concepts",
    verb: "Find",
    description:
      "Uses the LLM advisor to find business concepts that span multiple products in this domain, plus synonyms. Proposals at or above the confidence threshold are auto-promoted — bound to their evidence columns and parented under the matching entity. Lower-confidence proposals are queued for manual review in the Review Queue tab.",
  },
  enrich: {
    n: 3,
    title: "Enrich names & definitions",
    verb: "Enrich",
    description:
      "LLM-polishes the names, definitions, and synonyms of this domain's concepts in place (and re-embeds them for semantic search). Run last. Non-destructive — bindings and hierarchy are untouched.",
  },
};

const STALE_REASON_TEXT: Record<string, string> = {
  sources_changed: "The domain's data products have changed since this step last ran.",
  upstream_rerun: "An earlier step in the sequence ran more recently — re-run to incorporate it.",
};

const s: Record<string, CSSProperties> = {
  wrap: { display: "flex", flexDirection: "column", gap: 16 },
  toolbar: { display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" },
  select: { padding: "6px 10px", border: "1px solid #cbd5e1", borderRadius: 6, fontSize: 13 },
  clearBtn: {
    fontSize: 13, padding: "7px 12px", border: "1px solid #fca5a5", borderRadius: 6,
    backgroundColor: "#fff", color: "#991b1b", cursor: "pointer", fontWeight: 600,
  },
  card: {
    display: "flex", gap: 14, padding: 16, borderRadius: 10,
    backgroundColor: "#fff", border: "1px solid #e2e8f0",
  },
  num: {
    flexShrink: 0, width: 28, height: 28, borderRadius: "50%", display: "flex",
    alignItems: "center", justifyContent: "center", fontWeight: 700, fontSize: 13,
    backgroundColor: "#eef2ff", color: "#4338ca",
  },
  body: { flex: 1, minWidth: 0 },
  rowTop: { display: "flex", alignItems: "center", gap: 10, marginBottom: 4, flexWrap: "wrap" },
  title: { fontSize: 15, fontWeight: 700, color: "#0f172a" },
  desc: { fontSize: 12.5, color: "#475569", lineHeight: 1.5, marginBottom: 8 },
  chip: {
    fontSize: 10, padding: "2px 8px", borderRadius: 4, fontWeight: 700,
    textTransform: "uppercase", letterSpacing: "0.04em",
  },
  meta: { fontSize: 11.5, color: "#64748b", marginBottom: 6 },
  statsRow: { fontSize: 12, color: "#334155", marginBottom: 8 },
  staleBanner: {
    fontSize: 12, padding: "6px 10px", borderRadius: 6, marginBottom: 8,
    backgroundColor: "#fffbeb", color: "#92400e", border: "1px solid #fde68a",
  },
  errBanner: {
    fontSize: 12, padding: "6px 10px", borderRadius: 6, marginBottom: 8,
    backgroundColor: "#fef2f2", color: "#991b1b", border: "1px solid #fecaca",
  },
  runBtn: {
    fontSize: 13, padding: "7px 14px", border: "none", borderRadius: 6,
    backgroundColor: "#3b82f6", color: "#fff", cursor: "pointer", fontWeight: 600,
  },
  ghostBtn: {
    fontSize: 12, padding: "6px 10px", border: "1px solid #cbd5e1", borderRadius: 6,
    backgroundColor: "#fff", color: "#334155", cursor: "pointer", fontWeight: 600,
  },
  btnRow: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" },
  previewBox: {
    marginTop: 8, padding: 10, borderRadius: 6, backgroundColor: "#f8fafc",
    border: "1px solid #e2e8f0", fontSize: 12, color: "#334155",
  },
  panel: {
    padding: 16, borderRadius: 10, backgroundColor: "#fff", border: "1px solid #e2e8f0",
  },
  panelTitle: { fontSize: 14, fontWeight: 700, color: "#0f172a", marginBottom: 10 },
  countGrid: { display: "flex", gap: 20, flexWrap: "wrap", marginBottom: 8 },
  countItem: { display: "flex", flexDirection: "column" },
  countNum: { fontSize: 22, fontWeight: 700, color: "#0f172a" },
  countLabel: { fontSize: 11, color: "#64748b", textTransform: "uppercase", letterSpacing: "0.04em" },
  strandedOk: {
    fontSize: 13, padding: "8px 12px", borderRadius: 6,
    backgroundColor: "#f0fdf4", color: "#166534", border: "1px solid #bbf7d0",
  },
  strandedWarn: {
    fontSize: 13, padding: "8px 12px", borderRadius: 6,
    backgroundColor: "#fffbeb", color: "#92400e", border: "1px solid #fde68a",
  },
  strandedRow: {
    fontSize: 12, padding: "4px 0", borderBottom: "1px solid #f1f5f9",
    display: "flex", gap: 8, alignItems: "center",
  },
  levelChip: {
    fontSize: 9, padding: "1px 6px", borderRadius: 4, fontWeight: 700,
    textTransform: "uppercase", backgroundColor: "#f1f5f9", color: "#475569",
  },
  banner: { padding: 12, borderRadius: 6, fontSize: 13 },
};

function chipColors(st: StepStatus): { bg: string; fg: string; label: string } {
  if (!st.has_run) return { bg: "#f1f5f9", fg: "#64748b", label: "Never run" };
  if (st.status === "failed") return { bg: "#fee2e2", fg: "#991b1b", label: "Failed" };
  if (st.stale) return { bg: "#fef3c7", fg: "#92400e", label: "Needs re-run" };
  return { bg: "#dcfce7", fg: "#166534", label: "Done" };
}

function fmtWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function fmtDuration(ms: number | null): string {
  if (!ms || ms < 0) return "";
  const sec = Math.round(ms / 1000);
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m ${sec % 60}s`;
}

function statsSummary(step: string, stats: Record<string, unknown>): string {
  if (!stats || Object.keys(stats).length === 0) return "";
  if (step === "scaffold") {
    return `${stats.entities ?? 0} entities · ${stats.attributes ?? 0} attributes · ${stats.values ?? 0} values · ${stats.relationships ?? 0} relationships`;
  }
  if (step === "recommend") {
    const adv = stats.advisor_error ? " · advisor error" : "";
    return `${stats.proposals ?? 0} proposals · ${stats.promoted ?? 0} promoted · ${stats.queued ?? 0} queued (≥ ${stats.threshold ?? 0.7})${adv}`;
  }
  if (step === "enrich") {
    return `${stats.enriched ?? 0} concepts enriched`;
  }
  return "";
}

export default function DiscoveryTab({ availableDomains }: Props) {
  const realDomains = availableDomains.filter((d) => d && d !== "__all__");
  const confirm = useConfirm();
  const [domain, setDomain] = useState<string>(realDomains[0] || "");
  const [status, setStatus] = useState<DiscoveryStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyStep, setBusyStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    if (!domain && realDomains.length) setDomain(realDomains[0]);
  }, [realDomains, domain]);

  const fetchStatus = useCallback(async () => {
    if (!domain) return;
    setLoading(true);
    setError(null);
    try {
      const r = await api.get("/api/semantic/discovery/status", { params: { domain } });
      setStatus(r.data as DiscoveryStatus);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Failed to load status");
    } finally {
      setLoading(false);
    }
  }, [domain]);

  useEffect(() => {
    void fetchStatus();
  }, [fetchStatus]);

  const runStep = async (step: string, dryRun = false) => {
    if (busyStep) return;
    setBusyStep(step);
    setError(null);
    setPreview(null);
    try {
      const r = await api.post("/api/semantic/discovery/run", {
        domain,
        step,
        dry_run: dryRun,
      });
      if (dryRun) {
        setPreview(r.data as Record<string, unknown>);
      } else {
        await fetchStatus();
      }
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || `${step} failed`);
    } finally {
      setBusyStep(null);
    }
  };

  const clearConcepts = async () => {
    if (busyStep) return;
    if (
      !(await confirm({
        title: `Clear the concept layer for "${domain}"`,
        message: "Every active concept and its bindings to data products will be deprecated (hidden, but kept for audit history). You can rebuild with the steps below.",
        confirmLabel: "Clear",
        tone: "danger",
      }))
    )
      return;
    setBusyStep("reset");
    setError(null);
    try {
      await api.post("/api/semantic/discovery/reset", { domain });
      await fetchStatus();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Clear failed");
    } finally {
      setBusyStep(null);
    }
  };

  if (!realDomains.length) {
    return (
      <div style={{ ...s.banner, backgroundColor: "#f8fafc", color: "#64748b", border: "1px dashed #cbd5e1" }}>
        No domains with published data products yet. Publish a product first, then build its concept layer here.
      </div>
    );
  }

  const stepByName: Record<string, StepStatus> = {};
  (status?.steps || []).forEach((st) => {
    stepByName[st.step] = st;
  });

  return (
    <div style={s.wrap}>
      <div style={s.toolbar}>
        <label style={{ fontSize: 13, color: "#475569", fontWeight: 600 }}>Domain</label>
        <select style={s.select} value={domain} onChange={(e) => setDomain(e.target.value)}>
          {realDomains.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
        <div style={{ flex: 1 }} />
        {status?.last_reset_at && (
          <span style={{ fontSize: 11.5, color: "#94a3b8" }}>
            Last cleared {fmtWhen(status.last_reset_at)}
          </span>
        )}
        <button
          style={{ ...s.clearBtn, opacity: busyStep ? 0.5 : 1 }}
          onClick={clearConcepts}
          disabled={!!busyStep}
          title="Soft-deprecate every concept in this domain so the sequence can be re-run from a clean slate"
        >
          {busyStep === "reset" ? "Clearing…" : "Clear concepts"}
        </button>
      </div>

      <div style={{ fontSize: 12.5, color: "#64748b" }}>
        Run these in order for the most complete concept layer. Each step is independently
        re-runnable; the badge tells you whether it has run and whether your data products have
        changed since.
      </div>

      {error && <div style={{ ...s.banner, backgroundColor: "#fef2f2", color: "#991b1b" }}>{error}</div>}

      {(["scaffold", "recommend", "enrich"] as const).map((stepKey) => {
        const meta = STEP_META[stepKey];
        const st = stepByName[stepKey];
        const cc = st ? chipColors(st) : { bg: "#f1f5f9", fg: "#64748b", label: "Never run" };
        const running = busyStep === stepKey;
        return (
          <div key={stepKey} style={s.card}>
            <div style={s.num}>{meta.n}</div>
            <div style={s.body}>
              <div style={s.rowTop}>
                <span style={s.title}>{meta.title}</span>
                <span style={{ ...s.chip, backgroundColor: cc.bg, color: cc.fg }}>{cc.label}</span>
              </div>
              <div style={s.desc}>{meta.description}</div>

              {st?.has_run && (
                <div style={s.meta}>
                  Last run {fmtWhen(st.last_run_at)}
                  {fmtDuration(st.duration_ms) ? ` · ${fmtDuration(st.duration_ms)}` : ""}
                  {st.triggered_by ? ` · by ${st.triggered_by}` : ""}
                </div>
              )}
              {st?.has_run && statsSummary(stepKey, st.stats) && (
                <div style={s.statsRow}>{statsSummary(stepKey, st.stats)}</div>
              )}
              {st?.stale &&
                st.stale_reasons.map((r) => (
                  <div key={r} style={s.staleBanner}>
                    ⚠ {STALE_REASON_TEXT[r] || "This step may be out of date."}
                  </div>
                ))}
              {st?.status === "failed" && st.error && <div style={s.errBanner}>✕ {st.error}</div>}

              <div style={s.btnRow}>
                <button
                  style={{ ...s.runBtn, opacity: busyStep ? 0.5 : 1 }}
                  onClick={() => runStep(stepKey)}
                  disabled={!!busyStep}
                >
                  {running
                    ? stepKey === "recommend" || stepKey === "enrich"
                      ? "Running (LLM)…"
                      : "Running…"
                    : st?.has_run
                      ? `Re-run ${meta.verb.toLowerCase()}`
                      : `${meta.verb}`}
                </button>
                {stepKey === "scaffold" && (
                  <button
                    style={{ ...s.ghostBtn, opacity: busyStep ? 0.5 : 1 }}
                    onClick={() => runStep("scaffold", true)}
                    disabled={!!busyStep}
                    title="Preview the entity model without writing"
                  >
                    Preview
                  </button>
                )}
              </div>

              {stepKey === "scaffold" && preview && (
                <div style={s.previewBox}>
                  <strong>Preview</strong> — would create{" "}
                  {(preview.counts as { entities?: number })?.entities ?? 0} entities ·{" "}
                  {(preview.counts as { attributes?: number })?.attributes ?? 0} attributes ·{" "}
                  {(preview.counts as { values?: number })?.values ?? 0} values ·{" "}
                  {(preview.counts as { relationships?: number })?.relationships ?? 0} relationships.
                  Click <em>Re-run derive</em> to apply (deprecates + rebuilds).
                </div>
              )}
            </div>
          </div>
        );
      })}

      <div style={s.panel}>
        <div style={s.panelTitle}>Concept layer {loading ? "· loading…" : ""}</div>
        {status && (
          <>
            <div style={s.countGrid}>
              {(["entity", "attribute", "value"] as const).map((lvl) => (
                <div key={lvl} style={s.countItem}>
                  <span style={s.countNum}>{status.concept_counts[lvl]}</span>
                  <span style={s.countLabel}>{lvl === "entity" ? "entities" : lvl + "s"}</span>
                </div>
              ))}
              <div style={s.countItem}>
                <span style={s.countNum}>{status.concept_counts.total}</span>
                <span style={s.countLabel}>total</span>
              </div>
            </div>

            {status.stranded.total === 0 ? (
              <div style={s.strandedOk}>✓ Every active concept is bound to a data product.</div>
            ) : (
              <div>
                <div style={s.strandedWarn}>
                  ⚠ {status.stranded.total} stranded concept(s) with no data-product binding —{" "}
                  {status.stranded.counts.entity || 0} entities (no table),{" "}
                  {status.stranded.counts.attribute || 0} attributes (no column),{" "}
                  {status.stranded.counts.value || 0} values (no parent). Review them in the
                  Concepts tab.
                </div>
                <div style={{ marginTop: 8 }}>
                  {status.stranded.items.slice(0, 30).map((it) => (
                    <div key={it.uri} style={s.strandedRow}>
                      <span style={s.levelChip}>{it.level}</span>
                      <span>{it.name}</span>
                    </div>
                  ))}
                  {status.stranded.total > 30 && (
                    <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 4 }}>
                      …{status.stranded.total - 30} more.
                    </div>
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
