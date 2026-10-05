import { useEffect, useState } from "react";
import api from "../api/client";

interface CosmeticPatch {
  occurred_at: string | null;
  change_kind: string | null;
  revision_notes: string | null;
  actor: string | null;
}

interface VersionEntry {
  version: number;
  lifecycle_state: string | null;
  change_kind: string | null;
  revision_notes: string | null;
  published_at: string | null;
  occurred_at: string | null;
  actor: string | null;
  patches: CosmeticPatch[];
}

interface RevisionsResponse {
  contract_id: string;
  versions: VersionEntry[];
}

interface Props {
  contractId: string;
  /** Optional callback so callers can jump the surrounding detail view to a
   *  specific version when the user clicks "View this version". */
  onSelectVersion?: (version: number) => void;
  /** Highlights the chip for the currently-rendered version. */
  selectedVersion?: number | null;
}

const STATE_COLORS: Record<string, string> = {
  draft: "#9ca3af",
  ingesting: "#6366f1",
  submitted: "#06b6d4",
  in_engineering: "#0ea5e9",
  approved: "#22c55e",
  published: "#16a34a",
  superseded: "#a16207",
  rejected: "#dc2626",
};

function fmt(ts: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export default function RevisionTimeline({ contractId, onSelectVersion, selectedVersion }: Props) {
  const [data, setData] = useState<RevisionsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .get<RevisionsResponse>(`/api/marketplace/${contractId}/revisions`)
      .then((res) => {
        if (cancelled) return;
        setData(res.data);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [contractId]);

  if (loading) return <div style={{ fontSize: 13, color: "#6b7280" }}>Loading revision history…</div>;
  if (error) return <div style={{ fontSize: 13, color: "#dc2626" }}>Failed to load history: {error}</div>;
  if (!data || data.versions.length === 0) return null;

  return (
    <div
      style={{
        background: "#ffffff",
        border: "1px solid #e5e7eb",
        borderRadius: 8,
        padding: 12,
        marginBottom: 16,
      }}
    >
      <button
        onClick={() => setCollapsed((c) => !c)}
        style={{
          background: "transparent",
          border: "none",
          padding: 0,
          fontSize: 14,
          fontWeight: 600,
          cursor: "pointer",
          color: "#1f2937",
        }}
      >
        {collapsed ? "▶" : "▼"} Revision history ({data.versions.length} version
        {data.versions.length !== 1 ? "s" : ""})
      </button>
      {!collapsed && (
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 10 }}>
          {data.versions.map((v) => {
            const stateColor = STATE_COLORS[v.lifecycle_state ?? "draft"] ?? "#6b7280";
            const isSelected = selectedVersion === v.version;
            return (
              <div
                key={v.version}
                style={{
                  borderLeft: `3px solid ${stateColor}`,
                  paddingLeft: 12,
                  background: isSelected ? "#f0f9ff" : "transparent",
                  borderRadius: 4,
                  padding: 10,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                  <span style={{ fontWeight: 600, fontSize: 14 }}>v{v.version}</span>
                  <span
                    style={{
                      padding: "2px 8px",
                      background: stateColor,
                      color: "white",
                      borderRadius: 12,
                      fontSize: 11,
                      fontWeight: 600,
                      textTransform: "uppercase",
                      letterSpacing: 0.3,
                    }}
                  >
                    {v.lifecycle_state}
                  </span>
                  {v.change_kind && v.change_kind !== "initial" && (
                    <span style={{ fontSize: 12, color: "#6b7280" }}>{v.change_kind} change</span>
                  )}
                  <span style={{ fontSize: 12, color: "#6b7280" }}>
                    {fmt(v.published_at ?? v.occurred_at)}
                  </span>
                  {v.actor && (
                    <span style={{ fontSize: 12, color: "#6b7280" }}>by {v.actor}</span>
                  )}
                  {onSelectVersion && !isSelected && (
                    <button
                      onClick={() => onSelectVersion(v.version)}
                      style={{
                        marginLeft: "auto",
                        fontSize: 12,
                        padding: "2px 8px",
                        background: "#f3f4f6",
                        border: "1px solid #d1d5db",
                        borderRadius: 4,
                        cursor: "pointer",
                      }}
                    >
                      View this version
                    </button>
                  )}
                </div>
                {v.revision_notes && (
                  <div style={{ marginTop: 6, fontSize: 13, whiteSpace: "pre-wrap", color: "#374151" }}>
                    {v.revision_notes}
                  </div>
                )}
                {v.patches.length > 0 && (
                  <div style={{ marginTop: 6, paddingLeft: 8, borderLeft: "2px dashed #e5e7eb" }}>
                    <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 4 }}>
                      Cosmetic patches:
                    </div>
                    {v.patches.map((p, i) => (
                      <div key={i} style={{ fontSize: 12, marginBottom: 4 }}>
                        <span style={{ color: "#6b7280" }}>{fmt(p.occurred_at)}</span>
                        {p.actor && <span style={{ color: "#6b7280" }}> by {p.actor}</span>}
                        {p.revision_notes && (
                          <div style={{ color: "#374151", marginTop: 2, whiteSpace: "pre-wrap" }}>
                            {p.revision_notes}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
