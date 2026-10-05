// Semantic-Layer Recommender — Phase 4 of the design doc.
//
// Cross-product, read-only triage queue. Steward kicks off a run, the
// business-concept-advisor skill walks the portfolio + domain catalogs
// and proposes ranked concept candidates with column-level evidence.
// Steward edits / rejects / exports as YAML. v1 does NOT mutate the
// graph (no :BusinessConcept nodes created) — Accept is deferred.

import { useEffect, useState, type CSSProperties } from "react";
import api from "../api/client";
import ConceptsManagementTab from "../components/ConceptsManagementTab";
import DiscoveryTab from "../components/DiscoveryTab";
import MarketplaceGapsPage from "./product/MarketplaceGapsPage";
import MarkdownMessage from "../components/chat/MarkdownMessage";
import { useConfirm, usePrompt } from "../components/dialogContext";

interface EvidenceColumn {
  column_uri: string;
  product_name?: string;
  score?: number;
  why?: string;
}

interface SuggestedRelationship {
  to_concept?: string;
  kind?: string;
}

interface Concept {
  uri: string;
  concept_name: string;
  definition: string;
  confidence: number;
  status: "pending" | "rejected" | "edited" | "accepted";
  rejection_reason?: string | null;
  edited_at?: string | null;
  evidence_terms: string[];
  evidence_columns: EvidenceColumn[];
  suggested_relationships: SuggestedRelationship[];
  synonyms?: string[];
  values?: Array<{ name: string; definition: string; value_token: string }>;
}

interface Batch {
  batch_uri: string;
  batch_id: string;
  narrative: string;
  products_considered: number | null;
  evaluated_at?: string | null;
  triggered_by?: string | null;
  advisor_error?: string | null;
  concepts: Concept[];
}

const styles: Record<string, CSSProperties> = {
  page: { padding: "32px 40px", maxWidth: 1200, margin: "0 auto" },
  header: { display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 },
  title: { fontSize: 24, fontWeight: 700, color: "#0f172a", margin: 0 },
  subtitle: { fontSize: 13, color: "#64748b", marginTop: 4 },
  actions: { display: "flex", gap: 8 },
  primaryBtn: {
    fontSize: 13, padding: "8px 14px", border: "none", borderRadius: 6,
    backgroundColor: "#3b82f6", color: "#fff", cursor: "pointer", fontWeight: 600,
  },
  secondaryBtn: {
    fontSize: 13, padding: "8px 12px", border: "1px solid #cbd5e1", borderRadius: 6,
    backgroundColor: "#fff", color: "#334155", cursor: "pointer", fontWeight: 600,
  },
  banner: { padding: 12, borderRadius: 6, marginBottom: 16, fontSize: 13 },
  empty: {
    padding: 32, borderRadius: 8, textAlign: "center",
    backgroundColor: "#f8fafc", color: "#64748b", border: "1px dashed #cbd5e1",
  },
  narrative: {
    padding: 16, borderRadius: 8, backgroundColor: "#fff",
    border: "1px solid #e2e8f0", marginBottom: 20,
    fontSize: 13, lineHeight: 1.55, color: "#0f172a",
  },
  card: {
    padding: 16, borderRadius: 8, backgroundColor: "#fff",
    border: "1px solid #e2e8f0", marginBottom: 12,
  },
  cardHeader: { display: "flex", alignItems: "center", gap: 12, marginBottom: 8 },
  conceptName: { fontSize: 16, fontWeight: 700, color: "#0f172a" },
  confidenceBar: {
    width: 60, height: 6, backgroundColor: "#e2e8f0", borderRadius: 3,
    overflow: "hidden", display: "inline-block",
  },
  confidenceFill: { height: "100%", backgroundColor: "#16a34a", borderRadius: 3 },
  statusChip: {
    fontSize: 10, padding: "2px 7px", borderRadius: 4, fontWeight: 700,
    textTransform: "uppercase", letterSpacing: "0.05em",
  },
  definition: { fontSize: 13, color: "#334155", marginBottom: 10, lineHeight: 1.5 },
  evidenceLabel: { fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 4 },
  evidenceRow: { fontSize: 12, padding: "4px 0", borderBottom: "1px solid #f1f5f9" },
  uriCode: { fontFamily: "'Fira Code', monospace", fontSize: 11, color: "#475569", wordBreak: "break-all" },
  termsChips: { display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 8 },
  termChip: {
    fontSize: 11, padding: "1px 6px", borderRadius: 4,
    backgroundColor: "#f1f5f9", color: "#475569", fontFamily: "'Fira Code', monospace",
  },
  cardActions: { display: "flex", gap: 8, marginTop: 12 },
  smallBtn: {
    fontSize: 12, padding: "4px 10px", border: "1px solid #cbd5e1", borderRadius: 5,
    backgroundColor: "#fff", color: "#334155", cursor: "pointer", fontWeight: 600,
  },
  rejectBtn: {
    fontSize: 12, padding: "4px 10px", border: "1px solid #fca5a5", borderRadius: 5,
    backgroundColor: "#fff", color: "#991b1b", cursor: "pointer", fontWeight: 600,
  },
  editForm: {
    marginTop: 10, padding: 10, backgroundColor: "#f8fafc",
    border: "1px solid #e2e8f0", borderRadius: 6,
  },
  input: {
    width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1",
    borderRadius: 4, fontSize: 13, marginBottom: 8,
  },
  textarea: {
    width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1",
    borderRadius: 4, fontSize: 13, minHeight: 60, marginBottom: 8,
    fontFamily: "inherit", boxSizing: "border-box",
  },
};

const STATUS_COLOR: Record<string, { bg: string; fg: string }> = {
  pending: { bg: "#dbeafe", fg: "#1e40af" },
  edited: { bg: "#fef3c7", fg: "#92400e" },
  rejected: { bg: "#fee2e2", fg: "#991b1b" },
};

type Tab = "discovery" | "concepts" | "recommendations" | "gaps";

const TAB_LABELS: Record<Tab, string> = {
  discovery: "Discovery",
  concepts: "Concepts",
  recommendations: "Review Queue",
  gaps: "Gaps",
};

export default function SemanticRecommenderPage() {
  const confirm = useConfirm();
  const prompt = usePrompt();
  const [activeTab, setActiveTab] = useState<Tab>("discovery");
  const [domains, setDomains] = useState<string[]>([]);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingUri, setEditingUri] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editDef, setEditDef] = useState("");

  // Accept-as-concept modal state.
  const [acceptingUri, setAcceptingUri] = useState<string | null>(null);
  const [acceptName, setAcceptName] = useState("");
  const [acceptDef, setAcceptDef] = useState("");
  const [acceptDomain, setAcceptDomain] = useState("");
  const [acceptLevel, setAcceptLevel] = useState<"entity" | "attribute" | "value">("attribute");
  const [acceptValueToken, setAcceptValueToken] = useState("");
  const [acceptPredicate, setAcceptPredicate] = useState("");
  const [acceptParent, setAcceptParent] = useState("");
  const [acceptRepUris, setAcceptRepUris] = useState("");
  const [acceptSynonyms, setAcceptSynonyms] = useState("");  // comma-separated

  const fetchLatest = () => {
    setLoading(true);
    setError(null);
    api.get("/api/semantic/concepts/latest")
      .then((r) => setBatch((r.data?.batch as Batch | null) || null))
      .catch((e) => setError(String(e?.response?.data?.detail || e?.message || e)))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchLatest();
    api.get("/api/domains").then((r) => {
      const list = (r.data?.domains as string[]) || [];
      setDomains(list);
    }).catch(() => setDomains([]));
  }, []);

  const openAcceptModal = (c: Concept) => {
    setAcceptingUri(c.uri);
    setAcceptName(c.concept_name);
    setAcceptDef(c.definition);
    setAcceptDomain(domains[0] || "products_sales");
    setAcceptLevel("attribute");
    setAcceptValueToken("");
    setAcceptPredicate("");
    setAcceptParent("");
    // Pre-fill represented_by from the recommendation's evidence column URIs.
    const uris = (c.evidence_columns || []).map((e) => e.column_uri).filter(Boolean);
    setAcceptRepUris(uris.join("\n"));
    setAcceptSynonyms((c.synonyms || []).join(", "));
  };

  const submitAccept = async () => {
    if (!acceptingUri) return;
    try {
      const body: Record<string, unknown> = {
        name: acceptName.trim() || undefined,
        definition: acceptDef.trim() || undefined,
        domain: acceptDomain,
        level: acceptLevel,
        // Auto-deprecate a colliding active concept with the same
        // (domain, name) instead of erroring out. Matches the bulk-
        // accept behavior so the Steward isn't forced through a
        // manual two-step.
        if_exists: "deprecate_existing",
      };
      if (acceptLevel === "value") {
        if (acceptValueToken.trim()) body.value_token = acceptValueToken.trim();
        if (acceptPredicate.trim()) body.predicate_template = acceptPredicate.trim();
        if (acceptParent.trim()) body.parent_uri = acceptParent.trim();
      }
      const uris = acceptRepUris.split("\n").map((s) => s.trim()).filter(Boolean);
      if (uris.length > 0) body.represented_by_uris = uris;
      const synonyms = acceptSynonyms.split(",").map((s) => s.trim()).filter(Boolean);
      if (synonyms.length > 0) body.synonyms = synonyms;
      await api.post(
        `/api/semantic/concepts/from-recommendation/${encodeURIComponent(acceptingUri)}`,
        body,
      );
      setAcceptingUri(null);
      fetchLatest();  // re-read so the rec shows the acceptedAsConcept marker
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Accept failed");
    }
  };

  const runRecommend = async () => {
    if (running) return;
    setRunning(true);
    setError(null);
    try {
      const res = await api.post("/api/semantic/concepts/recommend", { trigger: "manual" });
      // Refresh from latest so the persisted batch URIs match
      fetchLatest();
      if (res.data?.advisor_error) {
        setError(`Advisor: ${res.data.advisor_error}`);
      }
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Recommend failed");
    } finally {
      setRunning(false);
    }
  };

  const startEdit = (c: Concept) => {
    setEditingUri(c.uri);
    setEditName(c.concept_name);
    setEditDef(c.definition);
  };

  const saveEdit = async (concept_uri: string) => {
    try {
      await api.patch(`/api/semantic/concepts/${encodeURIComponent(concept_uri)}`, {
        concept_name: editName,
        definition: editDef,
      });
      setEditingUri(null);
      fetchLatest();
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  };

  const reject = async (concept_uri: string) => {
    const reason = await prompt({
      title: "Reject concept",
      label: "Reason for rejecting this concept?",
      multiline: true,
      confirmLabel: "Reject",
    });
    if (reason === null) return;
    try {
      await api.post(`/api/semantic/concepts/${encodeURIComponent(concept_uri)}/reject`, {
        reason, category: "other",
      });
      fetchLatest();
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  };

  const exportYaml = () => {
    // Open in a new tab — browser handles the Content-Disposition download.
    window.open("/api/semantic/concepts/export.yaml", "_blank");
  };

  // Bulk accept of every pending rec in the latest batch. Domain per
  // rec is auto-inferred from the evidence-column lineage server-side.
  // Auto-deprecates any name collisions in the same domain.
  const [bulkAccepting, setBulkAccepting] = useState(false);
  const acceptAllPending = async () => {
    const pending = (batch?.concepts || []).filter((c) => c.status === "pending");
    if (pending.length === 0) return;
    const ok = await confirm({
      title: `Accept all ${pending.length} pending recommendation${pending.length === 1 ? "" : "s"}?`,
      message: (
        <>
          Accept them as concepts?
          <ul style={{ margin: "8px 0", paddingLeft: 18 }}>
            <li>Domain is auto-inferred per rec from its evidence-column lineage.</li>
            <li>Same-name collisions in the same domain auto-deprecate the prior concept.</li>
          </ul>
        </>
      ),
      confirmLabel: "Accept all",
    });
    if (!ok) return;
    setBulkAccepting(true);
    setError(null);
    try {
      const res = await api.post("/api/semantic/recommendations/accept-all", {});
      const { accepted = [], skipped = [], failures = [] } = res.data || {};
      // Surface a banner so the Steward sees what got through and what
      // didn't, without forcing them into a modal.
      const summary: string[] = [];
      if (accepted.length) summary.push(`${accepted.length} accepted`);
      if (skipped.length) summary.push(`${skipped.length} skipped (no inferrable domain)`);
      if (failures.length) summary.push(`${failures.length} failed`);
      if (skipped.length > 0 || failures.length > 0) {
        const detail = [
          ...skipped.map((s: { name?: string }) => `skipped: ${s.name || "?"}`),
          ...failures.map((f: { name?: string; error?: string }) => `failed: ${f.name || "?"} — ${f.error || "?"}`),
        ].slice(0, 6).join("; ");
        setError(`Bulk accept: ${summary.join(", ")}. ${detail}${(skipped.length + failures.length) > 6 ? " …" : ""}`);
      }
      fetchLatest();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Bulk accept failed");
    } finally {
      setBulkAccepting(false);
    }
  };

  const allConcepts = batch?.concepts || [];
  // Triage queue: hide recommendations that have been promoted to a
  // :BusinessConcept. The "Accepted (N hidden)" badge below the narrative
  // lets the steward toggle them back into view for audit.
  const [showAccepted, setShowAccepted] = useState(false);
  const acceptedCount = allConcepts.filter((c) => c.status === "accepted").length;
  const concepts = showAccepted ? allConcepts : allConcepts.filter((c) => c.status !== "accepted");

  return (
    <div style={styles.page}>
      <div style={styles.header}>
        <div>
          <h1 style={styles.title}>Semantic Explorer</h1>
          <div style={styles.subtitle}>
            A space to poke around your data's meaning — extract concepts and test ideas, not a
            production semantic layer. <strong>Discovery</strong> builds the concept layer (derive →
            find → enrich). <strong>Concepts</strong> is the active graph state (manual edits +
            diagram). <strong>Review Queue</strong> triages proposals below the auto-promote
            threshold. <strong>Gaps</strong> surfaces unmet needs consumers reported against products.
          </div>
        </div>
        <div style={styles.actions}>
          {activeTab === "recommendations" && batch && batch.concepts.some((c) => c.status === "pending") && (
            <button
              type="button"
              style={{ ...styles.secondaryBtn, color: "#15803d", borderColor: "#86efac" }}
              onClick={acceptAllPending}
              disabled={bulkAccepting || loading}
              title="Promote every pending recommendation to a :BusinessConcept. Domain is auto-inferred per rec from its evidence-column lineage. Same-name collisions auto-deprecate the prior concept."
            >
              {bulkAccepting
                ? "Accepting…"
                : `Accept all pending (${batch.concepts.filter((c) => c.status === "pending").length})`}
            </button>
          )}
          {activeTab === "recommendations" && batch && batch.concepts.length > 0 && (
            <button type="button" style={styles.secondaryBtn} onClick={exportYaml}>
              Export YAML
            </button>
          )}
          {activeTab === "recommendations" && (
            <button type="button" style={styles.primaryBtn} onClick={runRecommend} disabled={running || loading}>
              {running ? "Generating…" : (batch ? "Regenerate" : "Generate recommendations")}
            </button>
          )}
        </div>
      </div>

      <div style={{ display: "flex", gap: 4, marginBottom: 16, borderBottom: "1px solid #e2e8f0" }}>
        {(["discovery", "concepts", "recommendations", "gaps"] as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setActiveTab(t)}
            style={{
              padding: "8px 16px",
              border: "none",
              borderBottom: activeTab === t ? "2px solid #3b82f6" : "2px solid transparent",
              backgroundColor: "transparent",
              color: activeTab === t ? "#0f172a" : "#64748b",
              fontWeight: activeTab === t ? 700 : 500,
              cursor: "pointer",
              fontSize: 14,
            }}
          >
            {TAB_LABELS[t]}
          </button>
        ))}
      </div>

      {error && (
        <div style={{ ...styles.banner, backgroundColor: "#fef2f2", color: "#991b1b", border: "1px solid #fecaca" }}>
          {error}
        </div>
      )}

      {activeTab === "discovery" && (
        <DiscoveryTab availableDomains={domains} />
      )}

      {activeTab === "concepts" && (
        <ConceptsManagementTab availableDomains={domains} />
      )}

      {activeTab === "gaps" && <MarketplaceGapsPage />}

      {activeTab === "recommendations" && loading && <div style={styles.empty}>Loading…</div>}

      {activeTab === "recommendations" && !loading && !batch && (
        <div style={styles.empty}>
          <div style={{ fontSize: 14, marginBottom: 8 }}>No recommendation batch yet.</div>
          <div style={{ fontSize: 12 }}>
            Click <strong>Generate recommendations</strong> to scan the portfolio
            and surface candidate business concepts. Expect 30-90s for the LLM call.
          </div>
        </div>
      )}

      {activeTab === "recommendations" && !loading && batch && (
        <>
          <div style={{ fontSize: 12, color: "#64748b", marginBottom: 12 }}>
            Batch <code>{batch.batch_id}</code> · {batch.products_considered ?? "?"} product
            {batch.products_considered === 1 ? "" : "s"} considered
            {batch.evaluated_at && ` · ${new Date(batch.evaluated_at).toLocaleString()}`}
          </div>

          {batch.advisor_error && (
            <div style={{ ...styles.banner, backgroundColor: "#fef2f2", color: "#991b1b", border: "1px solid #fecaca" }}>
              Advisor error: {batch.advisor_error}
            </div>
          )}

          {batch.narrative && (
            <div style={styles.narrative}>
              <MarkdownMessage content={batch.narrative} />
            </div>
          )}

          {acceptedCount > 0 && (
            <div style={{ marginBottom: 12, fontSize: 12, color: "#475569" }}>
              {showAccepted
                ? `Showing ${acceptedCount} accepted recommendation${acceptedCount === 1 ? "" : "s"} alongside the queue. `
                : `${acceptedCount} recommendation${acceptedCount === 1 ? "" : "s"} already accepted (hidden). `}
              <button
                type="button"
                onClick={() => setShowAccepted((v) => !v)}
                style={{
                  border: "none", background: "none", padding: 0,
                  color: "#3b82f6", cursor: "pointer", fontWeight: 600, fontSize: 12,
                }}
              >
                {showAccepted ? "Hide accepted" : "Show accepted"}
              </button>
            </div>
          )}

          {concepts.length === 0 ? (
            <div style={styles.empty}>
              {acceptedCount > 0 && !showAccepted
                ? "All recommendations from this batch have been accepted. Use 'Show accepted' above to review them, or regenerate for a fresh queue."
                : "The advisor returned no concept candidates for this run."}
            </div>
          ) : (
            concepts.map((c) => {
              const statusColor = STATUS_COLOR[c.status] || STATUS_COLOR.pending;
              const confidencePct = Math.max(0, Math.min(100, Math.round((c.confidence || 0) * 100)));
              const isEditing = editingUri === c.uri;
              return (
                <div key={c.uri} style={{
                  ...styles.card,
                  opacity: c.status === "rejected" ? 0.6 : 1,
                  borderColor: c.status === "rejected" ? "#fca5a5" : "#e2e8f0",
                }}>
                  <div style={styles.cardHeader}>
                    <div style={styles.conceptName}>{c.concept_name}</div>
                    <span
                      style={{ ...styles.statusChip, ...statusColor }}
                      title={
                        c.status === "pending"
                          ? "Triage status: the Steward has not accepted, edited, or rejected this recommendation yet."
                          : c.status === "edited"
                          ? "Triage status: the Steward edited the name or definition. Re-accept to promote."
                          : c.status === "rejected"
                          ? "Triage status: the Steward rejected this recommendation. It will not be promoted."
                          : c.status === "accepted"
                          ? "Triage status: promoted to a :BusinessConcept node in the graph."
                          : `Triage status: ${c.status}`
                      }
                    >
                      {c.status}
                    </span>
                    <span
                      style={{ fontSize: 11, color: "#64748b" }}
                      title="Advisor confidence — how strongly the LLM ranked this candidate based on column-evidence overlap and cross-product reuse signals. Higher = more evidence the concept generalises beyond one product."
                    >
                      <span style={styles.confidenceBar}>
                        <span style={{ ...styles.confidenceFill, width: `${confidencePct}%` }} />
                      </span>
                      {" "}{confidencePct}%
                    </span>
                  </div>

                  {isEditing ? (
                    <div style={styles.editForm}>
                      <label style={{ fontSize: 11, color: "#475569", fontWeight: 600 }}>Concept name</label>
                      <input style={styles.input} value={editName} onChange={(e) => setEditName(e.target.value)} />
                      <label style={{ fontSize: 11, color: "#475569", fontWeight: 600 }}>Definition</label>
                      <textarea style={styles.textarea} value={editDef} onChange={(e) => setEditDef(e.target.value)} />
                      <div style={{ display: "flex", gap: 6 }}>
                        <button style={styles.primaryBtn} onClick={() => saveEdit(c.uri)}>Save</button>
                        <button style={styles.smallBtn} onClick={() => setEditingUri(null)}>Cancel</button>
                      </div>
                    </div>
                  ) : (
                    <div style={styles.definition}>{c.definition}</div>
                  )}

                  {c.evidence_terms && c.evidence_terms.length > 0 && (
                    <div style={styles.termsChips}>
                      {c.evidence_terms.map((t) => (
                        <span key={t} style={styles.termChip}>{t}</span>
                      ))}
                    </div>
                  )}

                  {c.synonyms && c.synonyms.length > 0 && (
                    <div style={{ ...styles.termsChips, marginTop: 4 }}>
                      <span style={{ fontSize: 11, fontWeight: 600, color: "#5b21b6", marginRight: 6 }}>synonyms:</span>
                      {c.synonyms.map((s) => (
                        <span key={s} style={{ ...styles.termChip, backgroundColor: "#ede9fe", color: "#5b21b6" }}>{s}</span>
                      ))}
                    </div>
                  )}

                  {c.values && c.values.length > 0 && (
                    <div style={{ ...styles.termsChips, marginTop: 4 }}>
                      <span style={{ fontSize: 11, fontWeight: 600, color: "#92400e", marginRight: 6 }}>
                        values ({c.values.length} will be auto-created on accept):
                      </span>
                      {c.values.map((v) => (
                        <span key={v.value_token} style={{ ...styles.termChip, backgroundColor: "#fef3c7", color: "#92400e" }}
                              title={v.definition}>
                          {v.name} <span style={{ fontFamily: "'Fira Code', monospace", opacity: 0.7 }}>= {v.value_token}</span>
                        </span>
                      ))}
                    </div>
                  )}

                  {c.evidence_columns && c.evidence_columns.length > 0 && (
                    <details>
                      <summary style={{ cursor: "pointer", fontSize: 12, fontWeight: 600, color: "#475569" }}>
                        Evidence: {c.evidence_columns.length} column{c.evidence_columns.length === 1 ? "" : "s"}
                      </summary>
                      <div style={{ marginTop: 8 }}>
                        <div style={styles.evidenceLabel}>Evidence columns</div>
                        {c.evidence_columns.map((ev, i) => (
                          <div key={i} style={styles.evidenceRow}>
                            <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                              <span style={{ fontWeight: 600, color: "#0f172a" }}>{ev.product_name || "(product)"}</span>
                              <span style={{ color: "#64748b" }}>score {(ev.score ?? 0).toFixed(2)}</span>
                            </div>
                            <div style={styles.uriCode}>{ev.column_uri}</div>
                            {ev.why && <div style={{ color: "#475569", marginTop: 2 }}>{ev.why}</div>}
                          </div>
                        ))}
                      </div>
                      {c.suggested_relationships && c.suggested_relationships.length > 0 && (
                        <div style={{ marginTop: 8 }}>
                          <div style={styles.evidenceLabel}>Suggested relationships</div>
                          {c.suggested_relationships.map((r, i) => (
                            <div key={i} style={{ fontSize: 12, color: "#334155" }}>
                              <strong>{r.kind || "?"}</strong>{" → "}{r.to_concept || "?"}
                            </div>
                          ))}
                        </div>
                      )}
                    </details>
                  )}

                  {c.status === "rejected" && c.rejection_reason && (
                    <div style={{ marginTop: 8, fontSize: 12, color: "#991b1b" }}>
                      <strong>Rejected:</strong> {c.rejection_reason}
                    </div>
                  )}

                  {!isEditing && c.status !== "rejected" && (
                    <div style={styles.cardActions}>
                      <button
                        style={{
                          ...styles.smallBtn,
                          backgroundColor: "#16a34a", color: "#fff", borderColor: "#16a34a",
                        }}
                        onClick={() => openAcceptModal(c)}
                      >
                        Accept as concept
                      </button>
                      <button style={styles.smallBtn} onClick={() => startEdit(c)}>Edit</button>
                      <button style={styles.rejectBtn} onClick={() => reject(c.uri)}>Reject</button>
                    </div>
                  )}
                </div>
              );
            })
          )}
        </>
      )}

      {acceptingUri && (
        <div style={{
          position: "fixed", inset: 0, backgroundColor: "rgba(15,23,42,0.5)",
          display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100,
        }} onClick={() => setAcceptingUri(null)}>
          <div style={{
            backgroundColor: "#fff", borderRadius: 8, padding: 24,
            width: 520, maxWidth: "90vw", maxHeight: "90vh", overflowY: "auto",
            boxShadow: "0 10px 40px rgba(0,0,0,0.25)",
          }} onClick={(e) => e.stopPropagation()}>
            <div style={{ fontSize: 18, fontWeight: 700, color: "#0f172a", marginBottom: 4 }}>
              Accept as :BusinessConcept
            </div>
            <div style={{ fontSize: 12, color: "#64748b", marginBottom: 16 }}>
              Promote this recommendation into a real graph node. Read-only consumers and the marketplace
              chat will be able to use it for concept-grounded queries.
            </div>
            <label style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em" }}>Name</label>
            <input
              style={{ width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 4, fontSize: 13, marginTop: 4, marginBottom: 10, boxSizing: "border-box" }}
              value={acceptName}
              onChange={(e) => setAcceptName(e.target.value)}
            />
            <label style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em" }}>Definition</label>
            <textarea
              style={{ width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 4, fontSize: 13, minHeight: 60, marginTop: 4, marginBottom: 10, boxSizing: "border-box", fontFamily: "inherit" }}
              value={acceptDef}
              onChange={(e) => setAcceptDef(e.target.value)}
            />
            <label style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em" }}>Synonyms (comma-separated)</label>
            <input
              style={{ width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 4, fontSize: 13, marginTop: 4, marginBottom: 10, boxSizing: "border-box" }}
              placeholder="e.g. loyalty tier, membership level, customer level"
              value={acceptSynonyms}
              onChange={(e) => setAcceptSynonyms(e.target.value)}
            />
            <label style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em" }}>Domain</label>
            <select
              style={{ width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 4, fontSize: 13, marginTop: 4, marginBottom: 10, backgroundColor: "#fff" }}
              value={acceptDomain}
              onChange={(e) => setAcceptDomain(e.target.value)}
            >
              {(domains.length > 0 ? domains : ["customer", "products_sales", "finance", "hr", "common"]).map(d => (
                <option key={d} value={d}>{d}</option>
              ))}
            </select>
            <label style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em" }}>Level</label>
            <select
              style={{ width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 4, fontSize: 13, marginTop: 4, marginBottom: 10, backgroundColor: "#fff" }}
              value={acceptLevel}
              onChange={(e) => setAcceptLevel(e.target.value as "entity" | "attribute" | "value")}
            >
              <option value="entity">Entity (business object — e.g. Customer)</option>
              <option value="attribute">Attribute (property — e.g. Order Status)</option>
              <option value="value">Value (specific — e.g. Cancelled Orders)</option>
            </select>
            {acceptLevel === "value" && (
              <>
                <label style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em" }}>Value token</label>
                <input
                  style={{ width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 4, fontSize: 13, marginTop: 4, marginBottom: 10, boxSizing: "border-box" }}
                  placeholder="e.g. cancelled"
                  value={acceptValueToken}
                  onChange={(e) => setAcceptValueToken(e.target.value)}
                />
                <label style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em" }}>Predicate template (optional override)</label>
                <input
                  style={{ width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 4, fontSize: 13, marginTop: 4, marginBottom: 10, boxSizing: "border-box" }}
                  placeholder={`e.g. status IN ('cancel_pending','cancelled')`}
                  value={acceptPredicate}
                  onChange={(e) => setAcceptPredicate(e.target.value)}
                />
                <label style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em" }}>Parent concept URI (super-concept)</label>
                <input
                  style={{ width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 4, fontSize: 13, marginTop: 4, marginBottom: 10, boxSizing: "border-box" }}
                  placeholder="concept:..."
                  value={acceptParent}
                  onChange={(e) => setAcceptParent(e.target.value)}
                />
              </>
            )}
            <label style={{ fontSize: 11, fontWeight: 700, color: "#475569", textTransform: "uppercase", letterSpacing: "0.05em" }}>Represented by (column URIs, one per line)</label>
            <textarea
              style={{ width: "100%", padding: "6px 8px", border: "1px solid #cbd5e1", borderRadius: 4, fontSize: 13, minHeight: 80, marginTop: 4, marginBottom: 10, boxSizing: "border-box", fontFamily: "'Fira Code', monospace" }}
              value={acceptRepUris}
              onChange={(e) => setAcceptRepUris(e.target.value)}
            />
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 16 }}>
              <button
                style={{ fontSize: 13, padding: "8px 14px", border: "1px solid #cbd5e1", borderRadius: 6, backgroundColor: "#fff", color: "#334155", cursor: "pointer", fontWeight: 600 }}
                onClick={() => setAcceptingUri(null)}
              >
                Cancel
              </button>
              <button
                style={{ fontSize: 13, padding: "8px 14px", border: "none", borderRadius: 6, backgroundColor: "#16a34a", color: "#fff", cursor: "pointer", fontWeight: 600 }}
                onClick={submitAccept}
                disabled={!acceptName.trim() || !acceptDef.trim() || !acceptDomain.trim()}
              >
                Promote
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
