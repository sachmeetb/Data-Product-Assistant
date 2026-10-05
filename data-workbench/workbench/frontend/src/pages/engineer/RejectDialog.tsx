import { useEffect, useState } from "react";
import api from "../../api/client";
import { engineerTheme } from "../../theme";

interface RejectionCategory {
  value: string;
  label: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  onSubmit: (payload: { category: string; reason: string }) => void | Promise<void>;
  /** Optional title for additional context (e.g. the request's product name). */
  subject?: string;
  submitting?: boolean;
}

/**
 * Rejection dialog the engineer uses to send a product request back to
 * the Data Product Owner. A categorical dropdown (server-driven list)
 * plus a freeform detail textarea; both travel to the backend which
 * persists them on the contract as PROV-O and flips lifecycleState to
 * 'rejected' so the PO's My Products view shows a distinct "Returned"
 * pill with the reason attached.
 */
export default function RejectDialog({ open, onClose, onSubmit, subject, submitting = false }: Props) {
  const [categories, setCategories] = useState<RejectionCategory[]>([]);
  const [category, setCategory] = useState<string>("");
  const [reason, setReason] = useState<string>("");

  useEffect(() => {
    if (!open) return;
    api.get("/api/product-requests/rejection-categories").then((res) => {
      const cats: RejectionCategory[] = res.data?.categories || [];
      setCategories(cats);
      if (cats.length && !category) setCategory(cats[0].value);
    }).catch(() => {
      // Fallback if the backend can't reach / endpoint missing.
      const fallback: RejectionCategory[] = [
        { value: "other", label: "Other" },
      ];
      setCategories(fallback);
      setCategory("other");
    });
    // Reset form each time the dialog opens.
    setReason("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const canSubmit = category.length > 0 && !submitting;

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        backgroundColor: "rgba(15, 23, 42, 0.55)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 100,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 480,
          maxWidth: "calc(100vw - 32px)",
          backgroundColor: "#fff",
          borderRadius: 12,
          padding: 20,
          boxShadow: "0 20px 40px rgba(15, 23, 42, 0.25)",
          display: "flex",
          flexDirection: "column",
          gap: 14,
        }}
      >
        <div>
          <div style={{ fontSize: 16, fontWeight: 700, color: "#0f172a" }}>Reject product request</div>
          {subject && <div style={{ fontSize: 12, color: "#64748b", marginTop: 4 }}>{subject}</div>}
          <div style={{ fontSize: 12, color: "#64748b", marginTop: 6, lineHeight: 1.5 }}>
            The product owner will see the category + reason in their My Products view and the wizard
            when they reopen the request. Both are persisted as PROV-O on the contract.
          </div>
        </div>

        <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>Reason category</span>
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            style={{
              padding: "8px 12px",
              borderRadius: 6,
              border: "1px solid #cbd5e1",
              fontSize: 14,
              fontFamily: "inherit",
            }}
          >
            {categories.map((c) => (
              <option key={c.value} value={c.value}>{c.label}</option>
            ))}
          </select>
        </label>

        <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>
            Detail <span style={{ color: "#94a3b8", fontWeight: 400 }}>(shown to the PO)</span>
          </span>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={5}
            placeholder="What specifically needs to change? Be concrete — the PO will edit based on this."
            style={{
              padding: "8px 12px",
              borderRadius: 6,
              border: "1px solid #cbd5e1",
              fontSize: 14,
              fontFamily: "inherit",
              resize: "vertical",
            }}
          />
        </label>

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 4 }}>
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            style={{
              padding: "8px 16px",
              borderRadius: 6,
              backgroundColor: "#fff",
              color: "#334155",
              border: "1px solid #cbd5e1",
              fontSize: 13,
              fontWeight: 600,
              cursor: submitting ? "not-allowed" : "pointer",
            }}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onSubmit({ category, reason })}
            disabled={!canSubmit}
            style={{
              padding: "8px 16px",
              borderRadius: 6,
              backgroundColor: canSubmit ? "#dc2626" : "#fca5a5",
              color: "#fff",
              border: "none",
              fontSize: 13,
              fontWeight: 700,
              cursor: canSubmit ? "pointer" : "not-allowed",
            }}
          >
            {submitting ? "Sending…" : "Send rejection"}
          </button>
        </div>
      </div>
    </div>
  );
}

// Keep engineerTheme reachable so future styling pulls from the same source.
void engineerTheme;
