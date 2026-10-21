import { useEffect, useState } from "react";
import api from "../api/client";
import { useCurrentUserEmail } from "../AuthContext";
import type { PendingDescription } from "../types";
import { DESCRIPTION_REJECTION_CATEGORIES } from "../types";
import { useConfirm } from "./dialogContext";

interface Props {
  projectId: number;
  onReviewComplete: () => void;
}

export default function DescriptionReviewPanel({ projectId, onReviewComplete }: Props) {
  const reviewer = useCurrentUserEmail();
  const confirm = useConfirm();
  const [items, setItems] = useState<PendingDescription[]>([]);
  const [loading, setLoading] = useState(true);
  const [currentIdx, setCurrentIdx] = useState(0);
  const [mode, setMode] = useState<"view" | "reject">("view");
  const [category, setCategory] = useState("incorrect_meaning");
  const [detail, setDetail] = useState("");
  const [correctedText, setCorrectedText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [stats, setStats] = useState({ approved: 0, rejected: 0, skipped: 0 });

  const loadItems = async () => {
    setLoading(true);
    try {
      const res = await api.get(`/api/projects/${projectId}/reviews/descriptions`);
      setItems(res.data.items);
      setCurrentIdx(0);
      setMode("view");
    } catch {
      setItems([]);
    }
    setLoading(false);
  };

  useEffect(() => { loadItems(); }, [projectId]);

  const current = items[currentIdx];

  const handleApprove = async (quality: number) => {
    if (!current) return;
    setSubmitting(true);
    try {
      await api.post(`/api/projects/${projectId}/reviews/descriptions`, {
        action: "approve",
        desc_uri: current.desc_uri,
        quality,
        reviewer,
      });
      setStats((s) => ({ ...s, approved: s.approved + 1 }));
      advance();
    } catch (err) {
      console.error(err);
    }
    setSubmitting(false);
  };

  const handleReject = async () => {
    if (!current || !correctedText.trim()) return;
    setSubmitting(true);
    try {
      await api.post(`/api/projects/${projectId}/reviews/descriptions`, {
        action: "reject",
        desc_uri: current.desc_uri,
        col_uri: current.col_uri,
        corrected_text: correctedText,
        category,
        detail,
        reviewer,
      });
      setStats((s) => ({ ...s, rejected: s.rejected + 1 }));
      setMode("view");
      setCorrectedText("");
      setDetail("");
      advance();
    } catch (err) {
      console.error(err);
    }
    setSubmitting(false);
  };

  const handleSkip = () => {
    setStats((s) => ({ ...s, skipped: s.skipped + 1 }));
    setMode("view");
    advance();
  };

  const advance = () => {
    if (currentIdx + 1 < items.length) {
      setCurrentIdx((i) => i + 1);
      setMode("view");
      setCorrectedText("");
      setDetail("");
    } else {
      // All done
      setItems([]);
      onReviewComplete();
    }
  };

  if (loading) return <div style={{ color: "#64748b" }}>Loading pending descriptions...</div>;

  if (items.length === 0) {
    const didReview = stats.approved + stats.rejected > 0;
    return (
      <div style={{ padding: 16 }}>
        {didReview ? (
          <div style={{ color: "#22c55e", fontWeight: 600 }}>
            All descriptions reviewed.
            <span style={{ color: "#64748b", fontWeight: 400, marginLeft: 12 }}>
              Approved: {stats.approved} | Rejected: {stats.rejected} | Skipped: {stats.skipped}
            </span>
          </div>
        ) : (
          <div style={{ color: "#f59e0b", fontSize: 14 }}>
            No descriptions with <code>pending_review</code> status found in Neo4j.
            <div style={{ color: "#64748b", fontSize: 13, marginTop: 6 }}>
              Check that the metadata enrichment skill created ColumnDescription nodes with
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
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Progress */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ margin: 0, color: "#334155" }}>
          Description Review ({currentIdx + 1} / {items.length})
        </h3>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 12, color: "#64748b" }}>
            Approved: {stats.approved} | Rejected: {stats.rejected} | Skipped: {stats.skipped}
          </span>
          <span
            onClick={async () => {
              const remaining = items.length - currentIdx;
              if (!(await confirm({
                title: "Approve all remaining",
                message: `Approve all ${remaining} remaining descriptions as "Good"?`,
                confirmLabel: "Approve all",
              }))) return;
              try {
                await api.post(`/api/projects/${projectId}/reviews/descriptions/approve-all`, { quality: 2 });
                onReviewComplete();
                loadItems();
              } catch { /* ignore */ }
            }}
            style={{
              fontSize: 11, color: "#94a3b8", cursor: "pointer",
              textDecoration: "underline", fontWeight: 500,
            }}
            title="Approve all remaining descriptions with 'Good' quality rating"
          >
            Approve all remaining
          </span>
        </div>
      </div>

      {/* Current item card */}
      <div style={{ backgroundColor: "#fff", borderRadius: 8, border: "1px solid #e2e8f0", padding: 20 }}>
        {/* Table + Column info */}
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1 }}>
            Table
          </div>
          <div style={{ fontWeight: 600, fontSize: 15 }}>
            {current.schema}.{current.table_name}
          </div>
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 }}>
          <div>
            <div style={{ fontSize: 12, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1 }}>
              Column
            </div>
            <div style={{ fontWeight: 600 }}>{current.col_name}</div>
          </div>
          <div>
            <div style={{ fontSize: 12, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1 }}>
              Data Type
            </div>
            <div style={{ fontWeight: 500, fontFamily: "monospace" }}>{current.data_type}</div>
          </div>
        </div>

        {/* AI-generated description */}
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 12, color: "#94a3b8", textTransform: "uppercase", letterSpacing: 1, marginBottom: 4 }}>
            AI-Generated Description
          </div>
          <div
            style={{
              padding: 12,
              backgroundColor: "#f1f5f9",
              borderRadius: 6,
              borderLeft: "3px solid #8b5cf6",
              fontSize: 14,
              lineHeight: 1.5,
            }}
          >
            {current.description_text}
          </div>
        </div>

        {/* Action buttons (view mode) */}
        {mode === "view" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5 }}>
              Approve with quality rating
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                onClick={() => handleApprove(1)}
                disabled={submitting}
                style={{
                  padding: "8px 16px", borderRadius: 6, border: "none",
                  backgroundColor: "#86efac", color: "#14532d", fontWeight: 600,
                  cursor: "pointer", fontSize: 13,
                }}
                title="Meets minimum bar, but not great"
              >
                Acceptable
              </button>
              <button
                onClick={() => handleApprove(2)}
                disabled={submitting}
                style={{
                  padding: "8px 16px", borderRadius: 6, border: "none",
                  backgroundColor: "#22c55e", color: "#fff", fontWeight: 600,
                  cursor: "pointer", fontSize: 13,
                }}
                title="Solid description, no complaints"
              >
                Good
              </button>
              <button
                onClick={() => handleApprove(3)}
                disabled={submitting}
                style={{
                  padding: "8px 16px", borderRadius: 6, border: "none",
                  backgroundColor: "#15803d", color: "#fff", fontWeight: 600,
                  cursor: "pointer", fontSize: 13,
                }}
                title="Exemplary, could be used as a template"
              >
                Excellent
              </button>
              <div style={{ borderLeft: "1px solid #e2e8f0", margin: "0 4px" }} />
              <button
                onClick={() => {
                  setMode("reject");
                  setCorrectedText(current.description_text);
                }}
                disabled={submitting}
                style={{
                  padding: "8px 16px", borderRadius: 6, border: "none",
                  backgroundColor: "#ef4444", color: "#fff", fontWeight: 600,
                  cursor: "pointer", fontSize: 13,
                }}
              >
                Reject
              </button>
              <button
                onClick={handleSkip}
                style={{
                  padding: "8px 16px", borderRadius: 6, border: "1px solid #cbd5e1",
                  backgroundColor: "#fff", color: "#64748b", fontWeight: 600,
                  cursor: "pointer", fontSize: 13,
                }}
              >
                Skip
              </button>
            </div>
          </div>
        )}

        {/* Rejection form */}
        {mode === "reject" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 12, borderTop: "1px solid #e2e8f0", paddingTop: 16 }}>
            <div>
              <label style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>Rejection Category</label>
              <select
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                style={{
                  display: "block", width: "100%", padding: "6px 10px", borderRadius: 6,
                  border: "1px solid #cbd5e1", fontSize: 13, marginTop: 4,
                }}
              >
                {DESCRIPTION_REJECTION_CATEGORIES.map((c) => (
                  <option key={c.value} value={c.value}>{c.label}</option>
                ))}
              </select>
            </div>

            <div>
              <label style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>Corrected Description</label>
              <textarea
                value={correctedText}
                onChange={(e) => setCorrectedText(e.target.value)}
                rows={3}
                style={{
                  display: "block", width: "100%", padding: 8, borderRadius: 6,
                  border: "1px solid #cbd5e1", fontSize: 13, marginTop: 4,
                  resize: "vertical", boxSizing: "border-box",
                }}
              />
            </div>

            <div>
              <label style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>Additional Detail (optional)</label>
              <input
                value={detail}
                onChange={(e) => setDetail(e.target.value)}
                style={{
                  display: "block", width: "100%", padding: "6px 10px", borderRadius: 6,
                  border: "1px solid #cbd5e1", fontSize: 13, marginTop: 4,
                  boxSizing: "border-box",
                }}
                placeholder="Why is this description incorrect?"
              />
            </div>

            <div style={{ display: "flex", gap: 8 }}>
              <button
                onClick={handleReject}
                disabled={submitting || !correctedText.trim()}
                style={{
                  padding: "8px 20px", borderRadius: 6, border: "none",
                  backgroundColor: "#ef4444", color: "#fff", fontWeight: 600,
                  cursor: submitting ? "wait" : "pointer", fontSize: 14,
                  opacity: !correctedText.trim() ? 0.5 : 1,
                }}
              >
                Submit Rejection
              </button>
              <button
                onClick={() => setMode("view")}
                style={{
                  padding: "8px 20px", borderRadius: 6, border: "1px solid #cbd5e1",
                  backgroundColor: "#fff", color: "#64748b", fontWeight: 600,
                  cursor: "pointer", fontSize: 14,
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
