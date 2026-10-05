// Marketplace-level gap log — questions the Semantic Q&A chat couldn't answer.
// Spans data products (not project-scoped). One unified list; each gap carries
// an audience tag (triage / po / engineer) and a status the steward advances.

import { useEffect, useState, type CSSProperties } from "react";
import api from "../../api/client";

interface Gap {
  id: number;
  domain: string;
  contract_id: string | null;
  product_uri: string | null;
  question: string;
  refused_reason: string | null;
  analysis: string | null;
  status: string;        // open | triaged | resolved | dismissed
  audience: string;      // triage | po | engineer
  created_at: string | null;
  resolved_by: string | null;
  notes: string | null;
}

const chip = (bg: string, fg: string): CSSProperties => ({
  fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.3,
  padding: "1px 7px", borderRadius: 4, backgroundColor: bg, color: fg,
});

const styles: Record<string, CSSProperties> = {
  page: { maxWidth: 980, margin: "0 auto", padding: "8px 0" },
  card: {
    padding: 14, marginBottom: 10, borderRadius: 8,
    background: "#fff7ed", border: "1px solid #fdba74",
  },
  q: { fontSize: 15, fontWeight: 700, color: "#0f172a" },
  meta: { fontSize: 11, color: "#9a3412", marginTop: 2 },
  reason: { fontSize: 13, color: "#374151", marginTop: 6 },
  btn: {
    fontSize: 12, fontWeight: 600, padding: "5px 10px", borderRadius: 5,
    border: "1px solid #cbd5e1", backgroundColor: "#fff", color: "#334155", cursor: "pointer",
  },
  select: {
    fontSize: 13, padding: "5px 8px", border: "1px solid #cbd5e1", borderRadius: 6, backgroundColor: "#fff",
  },
};

const AUDIENCE_CHIP: Record<string, [string, string]> = {
  triage: ["#e2e8f0", "#475569"],
  po: ["#ede9fe", "#6d28d9"],
  engineer: ["#dbeafe", "#1e40af"],
};
const STATUS_CHIP: Record<string, [string, string]> = {
  open: ["#fee2e2", "#991b1b"],
  triaged: ["#fef3c7", "#92400e"],
  resolved: ["#dcfce7", "#166534"],
  dismissed: ["#e2e8f0", "#475569"],
};

export default function MarketplaceGapsPage() {
  const [gaps, setGaps] = useState<Gap[]>([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchGaps = () => {
    setLoading(true);
    api.get("/api/marketplace/gaps", { params: statusFilter ? { status: statusFilter } : {} })
      .then((r) => setGaps((r.data?.gaps as Gap[]) || []))
      .catch((e) => setError(String(e?.response?.data?.detail || e?.message || e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { fetchGaps(); }, [statusFilter]);

  const resolve = async (id: number, body: Record<string, unknown>) => {
    try {
      await api.post(`/api/marketplace/gaps/${id}/resolve`, body);
      fetchGaps();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "Update failed");
    }
  };

  return (
    <div style={styles.page}>
      <h1 style={{ fontSize: 26, fontWeight: 800, color: "#0f172a", marginBottom: 4 }}>Gaps</h1>
      <p style={{ color: "#64748b", marginTop: 0 }}>
        Questions the Semantic Q&amp;A couldn't answer — a backlog of what the data products are missing.
      </p>

      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <label style={{ fontSize: 12, fontWeight: 600, color: "#475569" }}>Status</label>
        <select style={styles.select} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">Open + triaged</option>
          <option value="open">Open</option>
          <option value="triaged">Triaged</option>
          <option value="resolved">Resolved</option>
          <option value="dismissed">Dismissed</option>
        </select>
        <button type="button" style={styles.btn} onClick={fetchGaps}>Refresh</button>
      </div>

      {error && (
        <div style={{ padding: 10, borderRadius: 6, backgroundColor: "#fef2f2", color: "#991b1b", border: "1px solid #fecaca", marginBottom: 12, fontSize: 13 }}>{error}</div>
      )}
      {loading && <div style={{ color: "#64748b" }}>Loading…</div>}
      {!loading && gaps.length === 0 && (
        <div style={{ color: "#94a3b8", fontStyle: "italic", padding: 16 }}>
          No gaps logged. When a Semantic Q&amp;A question can't be answered, use "⚑ Report this gap" to log it here.
        </div>
      )}

      {gaps.map((g) => {
        const [ab, af] = AUDIENCE_CHIP[g.audience] || AUDIENCE_CHIP.triage;
        const [sb, sf] = STATUS_CHIP[g.status] || STATUS_CHIP.open;
        return (
          <div key={g.id} style={styles.card}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={styles.q}>{g.question || "(no question text)"}</span>
              <span style={chip(sb, sf)}>{g.status}</span>
              <span style={chip(ab, af)}>{g.audience}</span>
              <span style={{ marginLeft: "auto", fontSize: 11, color: "#94a3b8" }}>
                {g.domain}{g.created_at ? ` · ${new Date(g.created_at).toLocaleDateString()}` : ""}
              </span>
            </div>
            {g.refused_reason && <div style={styles.reason}>{g.refused_reason}</div>}
            {g.analysis && g.analysis !== g.refused_reason && (
              <div style={{ ...styles.reason, color: "#64748b", fontSize: 12 }}>{g.analysis}</div>
            )}
            {g.notes && <div style={{ ...styles.meta, color: "#166534" }}>Note: {g.notes}</div>}
            {g.status !== "resolved" && g.status !== "dismissed" && (
              <div style={{ display: "flex", gap: 6, marginTop: 10, flexWrap: "wrap" }}>
                <button type="button" style={styles.btn} onClick={() => resolve(g.id, { status: "triaged", audience: "engineer" })}>→ Engineer</button>
                <button type="button" style={styles.btn} onClick={() => resolve(g.id, { status: "triaged", audience: "po" })}>→ PO</button>
                <button type="button" style={{ ...styles.btn, borderColor: "#86efac", color: "#166534" }} onClick={() => resolve(g.id, { status: "resolved" })}>Resolve</button>
                <button type="button" style={{ ...styles.btn, color: "#991b1b" }} onClick={() => resolve(g.id, { status: "dismissed" })}>Dismiss</button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
