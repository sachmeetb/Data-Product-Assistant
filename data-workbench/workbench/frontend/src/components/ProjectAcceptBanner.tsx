import { useState } from "react";
import api from "../api/client";
import { useCurrentUserEmail } from "../AuthContext";

/**
 * Engineer-side banner: a submitted product request hasn't been Accepted yet.
 *
 * For source-aligned (dpe-sa) products, the PO's source-validation gate stays
 * LOCKED until the engineer accepts the request (submitted → accepted). Nothing
 * else surfaced this — the engineer could run discovery/enrichment and hand off
 * to the PO, but the PO's validation gate never appeared. This makes the
 * prerequisite visible and one-click actionable (mirrors the Incoming queue's
 * Accept, without the detour).
 */

interface Props {
  projectId: number;
  requestId: number | null;
  latestRequestKind: string | null;
  latestRequestStatus: string | null;
  archetype?: string;
  onAccepted: () => void;
}

export default function ProjectAcceptBanner({
  projectId, requestId, latestRequestKind, latestRequestStatus, archetype, onAccepted,
}: Props) {
  const currentEngineer = useCurrentUserEmail();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Only when a request is genuinely awaiting engineer acceptance. dpe-sa is the
  // archetype whose PO gate is blocked by this; render there.
  const show =
    latestRequestStatus === "submitted" &&
    latestRequestKind !== "edit" &&
    !!requestId &&
    (archetype || "").startsWith("dpe-sa");
  if (!show) return null;

  const accept = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/product-requests/${requestId}/accept`, { engineer: currentEngineer });
      onAccepted();
    } catch (e) {
      setError(`Accept failed: ${(e as Error).message || e}`);
      setBusy(false);
    }
  };

  return (
    <div style={{
      border: "1px solid #fde68a", background: "#fffbeb", borderRadius: 8,
      padding: "10px 14px", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#92400e" }}>
          This request hasn't been accepted yet
        </div>
        <div style={{ fontSize: 12, color: "#78716c", marginTop: 2 }}>
          The Product Owner's <strong>Validate Source Product</strong> gate stays locked until you accept the request. Accept it to unlock their review.
        </div>
        {error && <div style={{ fontSize: 12, color: "#b91c1c", marginTop: 4 }}>{error}</div>}
      </div>
      <button
        onClick={accept}
        disabled={busy}
        style={{
          padding: "6px 16px", borderRadius: 6, border: "none",
          background: busy ? "#cbd5e1" : "#b45309", color: "#fff",
          fontSize: 13, fontWeight: 700, cursor: busy ? "default" : "pointer", whiteSpace: "nowrap",
        }}
      >
        {busy ? "Accepting…" : "Accept request"}
      </button>
    </div>
  );
}
