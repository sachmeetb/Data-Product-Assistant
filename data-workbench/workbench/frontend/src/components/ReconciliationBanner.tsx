import { useEffect, useState } from "react";
import api from "../api/client";

/**
 * Pre-serving reconciliation (design B): warns when sensitive source columns map
 * into the product WITHOUT a protective transform (mask/hash/suppress) — including
 * already-approved mappings the review-time chip no longer surfaces. Non-blocking;
 * links the engineer to the mappings review to apply the recommendation.
 */
interface Unmet {
  product_col_name: string;
  source_col_name: string;
  recommended_kind: string;
}
interface Props {
  projectId: number;
  refreshKey?: number;
  onReview?: () => void;
}

export default function ReconciliationBanner({ projectId, refreshKey, onReview }: Props) {
  const [unmet, setUnmet] = useState<Unmet[]>([]);

  useEffect(() => {
    let cancelled = false;
    api
      .get(`/api/projects/${projectId}/reviews/mappings/reconciliation`)
      .then((r) => { if (!cancelled) setUnmet(r.data?.unmet || []); })
      .catch(() => { if (!cancelled) setUnmet([]); });
    return () => { cancelled = true; };
  }, [projectId, refreshKey]);

  if (unmet.length === 0) return null;
  const cols = Array.from(new Set(unmet.map((u) => u.product_col_name).filter(Boolean))).slice(0, 6).join(", ");

  return (
    <div style={{
      border: "1px solid #fde68a", background: "#fffbeb", borderRadius: 8,
      padding: "10px 14px", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#92400e" }}>
          ⚠ {unmet.length} sensitive column{unmet.length > 1 ? "s" : ""} map through unprotected
        </div>
        <div style={{ fontSize: 12, color: "#78716c", marginTop: 2 }}>
          {cols} — recommended to mask/hash before serving so the deployed view doesn't expose raw PII.
        </div>
      </div>
      {onReview && (
        <button
          onClick={onReview}
          style={{
            padding: "6px 16px", borderRadius: 6, border: "1px solid #b45309",
            background: "#fff", color: "#b45309", fontSize: 13, fontWeight: 700,
            cursor: "pointer", whiteSpace: "nowrap",
          }}
        >
          Review mappings
        </button>
      )}
    </div>
  );
}
